"""Pure preparation and bounded simulation of ByteForge's typed instructions."""

from dataclasses import replace

from byteforge.elf import build_executable
from byteforge.model import ByteForgeError, ElfConfig, EncodedItem, Immediate, Instruction, SourceLocation
from byteforge.parser import parse
from byteforge.resolver import assemble
from byteforge.trace_model import (
    CPUState, MASK64, REGISTER_NAMES, TraceFault, TraceLimits, TraceProgram, TraceStatus, TraceStep, TraceView,
)


def prepare_trace(source: str, filename: str, limits: TraceLimits) -> TraceProgram:
    """Use the real assembler/ELF pipeline; never read files or execute code."""
    if len(source.encode("utf-8")) > limits.max_source_bytes:
        first_line = source.partition("\n")[0].partition("\r")[0]
        raise ByteForgeError(SourceLocation(filename, 1, 1, first_line),
                             f"Trace source exceeds {limits.max_source_bytes} UTF-8 bytes")
    program = parse(source, filename)
    instructions = [item for item in program.statements if isinstance(item, Instruction)]
    if len(instructions) > limits.max_instructions:
        raise ByteForgeError(instructions[limits.max_instructions].location,
                             f"Trace program exceeds {limits.max_instructions} instructions")
    config = ElfConfig()
    assembly = assemble(program, config.image_base + config.code_offset, filename=filename)
    image, layout = build_executable(assembly, config)
    code_base = assembly.items[0].address
    data_base = code_base + len(assembly.code)
    segment = layout.segments[0]
    code_offset = segment.p_offset + code_base - segment.p_vaddr
    data_offset = segment.p_offset + data_base - segment.p_vaddr
    if (code_offset != config.code_offset or layout.entry != assembly.symbols["_start"]
            or image[code_offset:data_offset] != assembly.code
            or image[data_offset:] != assembly.data
            or segment.p_offset + segment.p_filesz != len(image)
            or segment.p_memsz < segment.p_filesz):
        raise ValueError("Trace addresses do not match the assembled ELF layout")
    return TraceProgram(
        source, program, assembly, image, layout,
        {item.address: item for item in assembly.items}, assembly.symbols,
        layout.entry, code_base, data_base, assembly.data, limits,
    )


def initial_state(program: TraceProgram) -> CPUState:
    """Return educational zero registers/ZF, not Linux process-entry state."""
    return CPUState(rip=program.entry)


def _stopped_step(state: CPUState, item: EncodedItem | None, code: str,
                  message: str, status: TraceStatus = TraceStatus.FAULTED) -> TraceStep:
    location = item.statement.location if item is not None else None
    after = replace(state, status=status, halt_reason=code)
    return TraceStep(location, state.rip, item.data if item is not None else b"",
                     state, after, fault=TraceFault(code, message, location))


def _read_data(program: TraceProgram, address: int, length: int) -> bytes:
    offset = address - program.data_base
    if offset < 0 or offset > len(program.data) or length > len(program.data) - offset:
        raise ValueError("Memory range is outside mapped read-only DB data")
    return program.data[offset:offset + length]


def read_memory(program: TraceProgram, address: int, length: int = 16) -> bytes:
    """Inspect only mapped DB bytes, with a bounded request length."""
    if type(address) is not int or not 0 <= address <= MASK64:
        raise ValueError("Memory address must be an unsigned 64-bit integer")
    if type(length) is not int or not 0 <= length <= program.limits.max_memory_bytes:
        raise ValueError(f"Memory length must be in 0..{program.limits.max_memory_bytes}")
    return _read_data(program, address, length)


def _syscall(program: TraceProgram, state: CPUState, item: EncodedItem) -> TraceStep:
    registers = dict(state.registers)
    number = registers["rax"]
    output = b""
    if number == 1:
        if registers["rdi"] != 1:
            return _stopped_step(state, item, "unsupported_descriptor", "Only stdout (fd 1) is supported")
        count = registers["rdx"]
        try:
            output = _read_data(program, registers["rsi"], count) if count else b""
        except ValueError as error:
            return _stopped_step(state, item, "invalid_memory", str(error))
        if state.stdout_size + count > program.limits.max_output_bytes:
            return _stopped_step(state, item, "output_limit",
                                 f"Captured stdout exceeds the {program.limits.max_output_bytes}-byte limit")
        registers["rax"] = count
    elif number != 60:
        return _stopped_step(state, item, "unsupported_syscall", f"Unsupported syscall number {number}")

    next_rip = (state.rip + len(item.data)) & MASK64
    registers["rcx"] = next_rip
    after = replace(state, rip=next_rip, registers=registers, step_count=state.step_count + 1,
                    stdout_size=state.stdout_size + len(output))
    fault = None
    if number == 60:
        after = replace(after, status=TraceStatus.HALTED, halt_reason="exit",
                        exit_status=registers["rdi"] & 0xFF)
    elif next_rip not in program.instructions:
        fault = TraceFault("invalid_rip", "Execution left the assembled instructions", item.statement.location)
        after = replace(after, status=TraceStatus.FAULTED, halt_reason=fault.code)
    changed = tuple(name for name in REGISTER_NAMES if registers[name] != state.registers[name])
    return TraceStep(item.statement.location, state.rip, item.data, state, after,
                     changed, executed=True, fault=fault, output=output)


def step(program: TraceProgram, state: CPUState) -> TraceStep:
    """Execute at most one typed instruction; return faults without native execution."""
    item = program.instructions.get(state.rip)
    if state.status is not TraceStatus.RUNNING:
        location = item.statement.location if item is not None else None
        return TraceStep(location, state.rip, item.data if item is not None else b"",
                         state, state, fault=TraceFault(
                             "state_stopped", "Restore a running state before stepping again", location))
    if item is None:
        return _stopped_step(state, None, "invalid_rip", "RIP is not an assembled instruction address")
    if state.step_count >= program.limits.max_steps:
        return _stopped_step(state, item, "step_limit",
                             f"Trace stopped at the {program.limits.max_steps}-step limit",
                             TraceStatus.HALTED)

    instruction = item.statement
    mnemonic, operands = instruction.mnemonic, instruction.operands
    if mnemonic == "syscall":
        return _syscall(program, state, item)

    registers = dict(state.registers)
    zf = state.zf
    next_rip = (state.rip + len(item.data)) & MASK64
    branch_taken = branch_target = None
    if mnemonic == "mov":
        destination, source = operands
        value = source.value if isinstance(source, Immediate) else program.symbols[source.name]
        registers[destination.name] = value & MASK64
    elif mnemonic in ("add", "sub", "cmp"):
        destination, source = operands
        old = state.registers[destination.name]
        value = (old + source.value if mnemonic == "add" else old - source.value) & MASK64
        zf = value == 0
        if mnemonic != "cmp":
            registers[destination.name] = value
    elif mnemonic == "xor":
        destination, source = operands
        value = (state.registers[destination.name] ^ state.registers[source.name]) & MASK64
        registers[destination.name] = value
        zf = value == 0
    elif mnemonic in ("jmp", "je", "jne"):
        target = operands[0]
        branch_target = program.symbols[target.name]
        if branch_target not in program.instructions:
            result = _stopped_step(state, item, "invalid_target",
                                   "Branch target is not an assembled instruction address")
            return replace(result, fault=TraceFault("invalid_target", result.fault.message, target.location))
        branch_taken = mnemonic == "jmp" or (state.zf if mnemonic == "je" else not state.zf)
        if branch_taken:
            next_rip = branch_target
    else:
        return _stopped_step(state, item, "unsupported_instruction",
                             f"Unsupported trace instruction: {mnemonic}")

    after = replace(state, rip=next_rip, registers=registers, zf=zf, step_count=state.step_count + 1)
    fault = None
    if next_rip not in program.instructions:
        fault = TraceFault("invalid_rip", "Execution left the assembled instructions", instruction.location)
        after = replace(after, status=TraceStatus.FAULTED, halt_reason=fault.code)
    changed = tuple(name for name in REGISTER_NAMES if registers[name] != state.registers[name])
    return TraceStep(instruction.location, state.rip, item.data, state, after,
                     changed, branch_taken, branch_target, True, fault)


class TraceSession:
    """A bounded history cursor over immutable transitions and one output buffer."""

    def __init__(self, program: TraceProgram):
        self._program = program
        self._initial = initial_state(program)
        self._history: list[TraceStep] = []
        self._cursor = 0
        self._output = bytearray()

    @property
    def program(self) -> TraceProgram:
        return self._program

    def _state(self) -> CPUState:
        return self._history[self._cursor - 1].after if self._cursor else self._initial

    def view(self) -> TraceView:
        last = self._history[self._cursor - 1] if self._cursor else None
        state = self._state()
        return TraceView(state, bytes(self._output[:state.stdout_size]), self._cursor,
                         len(self._history), last)

    def next(self) -> TraceStep:
        if self._cursor < len(self._history):
            result = self._history[self._cursor]
            self._cursor += 1
            return result
        state = self._state()
        if (state.status is TraceStatus.RUNNING and len(self._history) >= self.program.limits.max_history
                and state.step_count < self.program.limits.max_steps):
            result = _stopped_step(state, self.program.instructions.get(state.rip), "history_limit",
                                   "Trace history transition limit reached", TraceStatus.HALTED)
        else:
            result = step(self.program, state)
        if state.status is TraceStatus.RUNNING:
            self._history.append(result)
            self._output.extend(result.output)
            self._cursor += 1
        return result

    def back(self) -> TraceView:
        self._cursor = max(0, self._cursor - 1)
        return self.view()

    def reset(self) -> TraceView:
        self._history.clear()
        self._output.clear()
        self._cursor = 0
        return self.view()

    def continue_(self, max_steps: int = 100) -> TraceView:
        """Advance at most the request bound; Python reserves the name 'continue'."""
        if type(max_steps) is not int or not 1 <= max_steps <= self.program.limits.max_continue_steps:
            raise ValueError(f"Continue count must be in 1..{self.program.limits.max_continue_steps}")
        for _ in range(max_steps):
            if self._state().status is not TraceStatus.RUNNING:
                break
            self.next()
        return self.view()
