"""Pure preparation and bounded simulation of ByteForge's typed instructions."""

from dataclasses import replace

from byteforge.elf import build_executable
from byteforge.model import ByteForgeError, ElfConfig, EncodedItem, Immediate, Instruction, SourceLocation
from byteforge.parser import parse
from byteforge.resolver import assemble
from byteforge.trace_model import (
    CPUState, MASK64, REGISTER_NAMES, TraceFault, TraceLimits, TraceProgram, TraceStatus, TraceStep,
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


def step(program: TraceProgram, state: CPUState) -> TraceStep:
    """Execute at most one typed instruction; return faults without native execution."""
    item = program.instructions.get(state.rip)
    if state.status is not TraceStatus.RUNNING:
        location = item.statement.location if item is not None else None
        return TraceStep(location, state.rip, item.data if item is not None else b"",
                         state, state, fault=TraceFault(
                             "state_stopped", "Reset to an initial state before stepping again", location))
    if item is None:
        return _stopped_step(state, None, "invalid_rip", "RIP is not an assembled instruction address")
    if state.step_count >= program.limits.max_steps:
        return _stopped_step(state, item, "step_limit",
                             f"Trace stopped at the {program.limits.max_steps}-step limit",
                             TraceStatus.HALTED)

    instruction = item.statement
    mnemonic, operands = instruction.mnemonic, instruction.operands
    if mnemonic == "syscall":
        return _stopped_step(state, item, "unsupported_syscall",
                             "SYSCALL simulation is not supported in Stage 6A.1")

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

    after = CPUState(next_rip, registers, zf, state.step_count + 1)
    fault = None
    if next_rip not in program.instructions:
        fault = TraceFault("invalid_rip", "Execution left the assembled instructions", instruction.location)
        after = replace(after, status=TraceStatus.FAULTED, halt_reason=fault.code)
    changed = tuple(name for name in REGISTER_NAMES if registers[name] != state.registers[name])
    return TraceStep(instruction.location, state.rip, item.data, state, after,
                     changed, branch_taken, branch_target, True, fault)
