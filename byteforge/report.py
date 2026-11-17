"""Pure JSON adapters over real assembler and TraceLab metadata."""

import struct

from byteforge.model import AssemblyResult, ElfLayout, Immediate, Program, Register, SourceLocation
from byteforge.trace_model import CPUState, MASK64, TraceProgram, TraceStep, TraceView


SCHEMA_VERSION = 1


def hex64(value: int) -> str:
    if type(value) is not int or not 0 <= value <= MASK64:
        raise ValueError("Expected an unsigned 64-bit value")
    return f"0x{value:016x}"


def location_report(location: SourceLocation | None) -> dict | None:
    if location is None:
        return None
    return {"filename": location.filename, "line": location.line,
            "column": location.column, "text": location.text}


def bytes_report(data: bytes) -> dict:
    # repr escapes control/non-ASCII bytes. Consumers must render display as text.
    return {"hex": data.hex(" ").upper(), "display": repr(data)[2:-1], "length": len(data)}


def error_report(phase: str, code: str, message: str, location: SourceLocation | None = None) -> dict:
    return {"phase": phase, "code": code, "message": message, "location": location_report(location)}


def cpu_report(state: CPUState) -> dict:
    return {"registers": {name: hex64(value) for name, value in state.registers.items()},
            "rip": hex64(state.rip), "zf": state.zf, "step_count": hex64(state.step_count),
            "status": state.status.value, "halt_reason": state.halt_reason,
            "stdout_size": hex64(state.stdout_size), "exit_status": state.exit_status}


def step_report(step: TraceStep | None) -> dict | None:
    if step is None:
        return None
    fault = step.fault
    return {"location": location_report(step.location), "address": hex64(step.address),
            "bytes_hex": step.data.hex(" ").upper(), "size": len(step.data),
            "before": cpu_report(step.before), "after": cpu_report(step.after),
            "changed_registers": list(step.changed_registers), "branch_taken": step.branch_taken,
            "branch_target": hex64(step.branch_target) if step.branch_target is not None else None,
            "executed": step.executed, "output": bytes_report(step.output),
            "fault": error_report("trace", fault.code, fault.message, fault.location) if fault else None}


def view_report(view: TraceView) -> dict:
    state = view.state
    if state.halt_reason == "exit":
        status = "halted_successfully"
    elif state.halt_reason == "step_limit":
        status = "step_limit_reached"
    elif state.halt_reason and state.halt_reason.startswith("unsupported_"):
        status = "unsupported_operation"
    elif state.status.value != "running":
        status = "faulted"
    else:
        status = "ready" if state.step_count == 0 else "running"
    return {"execution_status": status, "cpu": cpu_report(state), "stdout": bytes_report(view.stdout),
            "cursor": view.cursor, "history_size": view.history_size,
            "last_step": step_report(view.last_step)}


def file_offset(address: int, layout: ElfLayout) -> int:
    segment = layout.segments[0]
    return segment.p_offset + address - segment.p_vaddr


def memory_report(program: TraceProgram, address: int, data: bytes) -> dict:
    return {"address": hex64(address), "file_offset": file_offset(address, program.elf_layout),
            "data": bytes_report(data), "read_only": True}


def _operand_report(operand) -> dict:
    result = {"location": location_report(operand.location)}
    if isinstance(operand, Register):
        result.update(kind="register", name=operand.name, code=operand.code, width=operand.width)
    elif isinstance(operand, Immediate):
        result.update(kind="immediate", value=str(operand.value), bits=hex64(operand.value & MASK64))
    else:
        result.update(kind="symbol", name=operand.name)
    return result


def build_report(program: Program, result: AssemblyResult, elf_layout: ElfLayout) -> dict:
    """Honor the documented report interface without reassembling any instruction."""
    instructions = [
        {"address": hex64(item.address), "file_offset": file_offset(item.address, elf_layout),
         "size": len(item.data), "bytes_hex": item.data.hex(" ").upper(),
         "mnemonic": item.statement.mnemonic,
         "operands": [_operand_report(operand) for operand in item.statement.operands],
         "location": location_report(item.statement.location),
         "resolution": {name: hex64(address) for name, address in sorted(item.resolution.items())}}
        for item in result.items
    ]
    segments = [{"p_type": item.p_type, "p_flags": item.p_flags, "p_offset": item.p_offset,
                 "p_vaddr": hex64(item.p_vaddr), "p_paddr": hex64(item.p_paddr),
                 "p_filesz": item.p_filesz, "p_memsz": item.p_memsz, "p_align": item.p_align}
                for item in elf_layout.segments]
    return {"schema_version": SCHEMA_VERSION, "instructions": instructions,
            "symbols": [{"name": name, "address": hex64(address),
                         "file_offset": file_offset(address, elf_layout)}
                        for name, address in sorted(result.symbols.items())],
            "statement_locations": [location_report(item.location) for item in program.statements],
            "code_size": len(result.code), "data_size": len(result.data),
            "elf": {"entry": hex64(elf_layout.entry), "file_size": elf_layout.file_size,
                    "segments": segments}}


def assembly_report(program: TraceProgram) -> dict:
    result = build_report(program.program, program.assembly, program.elf_layout)
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", program.elf_image)
    names = ("e_type", "e_machine", "e_version", "e_entry", "e_phoff", "e_shoff", "e_flags",
             "e_ehsize", "e_phentsize", "e_phnum", "e_shentsize", "e_shnum", "e_shstrndx")
    fields = dict(zip(names, header[1:]))
    fields["e_entry"] = hex64(fields["e_entry"])
    identity = header[0]
    fields.update(magic_hex=identity[:4].hex(" ").upper(), elf_class=identity[4],
                  byte_order=identity[5], os_abi=identity[7])
    result["elf"]["header"] = fields
    code_offset = file_offset(program.code_base, program.elf_layout)
    ph_end = header[5] + header[9] * header[10]
    ranges = (("elf_header", 0, header[8]), ("program_headers", header[5], ph_end - header[5]),
              ("padding", ph_end, code_offset - ph_end),
              ("code", code_offset, len(program.assembly.code)),
              ("data", file_offset(program.data_base, program.elf_layout), len(program.data)))
    segment = program.elf_layout.segments[0]
    result["elf"]["regions"] = [
        {"name": name, "file_offset": offset, "size": size,
         "address": hex64(segment.p_vaddr + offset - segment.p_offset)}
        for name, offset, size in ranges
    ]
    result.update(source=program.source, source_lines=program.source.splitlines(),
                  code_base=hex64(program.code_base), data_base=hex64(program.data_base),
                  data=bytes_report(program.data))
    return result
