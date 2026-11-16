"""Lay out instructions and collect labels without encoding symbol references."""

from byteforge.encoder import encode, encoded_size
from byteforge.model import (
    AssemblyResult, ByteForgeError, DataDirective, EncodedItem, Instruction,
    Label, Program, SourceLocation,
)


def assemble(
    program: Program, base_address: int, *, filename: str = "<input>"
) -> AssemblyResult:
    """Assemble code at base_address, the virtual address of its first byte."""
    fallback = (program.statements[0].location if program.statements
                else SourceLocation(filename, 1, 1, ""))
    if type(base_address) is not int or not 0 <= base_address < (1 << 64):
        raise ByteForgeError(fallback, "Code base address must be an unsigned 64-bit integer")

    symbols: dict[str, int] = {}
    definitions: dict[str, SourceLocation] = {}
    planned: list[tuple[Instruction, int, int]] = []
    address = base_address
    for statement in program.statements:
        if isinstance(statement, Label):
            if statement.name in symbols:
                raise ByteForgeError(statement.location, f"Duplicate label: {statement.name}")
            symbols[statement.name] = address
            definitions[statement.name] = statement.location
        elif isinstance(statement, DataDirective):
            raise ByteForgeError(statement.location, "DB directives are not supported in this milestone")
        else:
            size = encoded_size(statement)
            if address + size > (1 << 64):
                raise ByteForgeError(statement.location, "Instruction exceeds the 64-bit address range")
            planned.append((statement, address, size))
            address += size

    if "_start" not in symbols:
        raise ByteForgeError(fallback, "Required entry label _start is missing")
    if symbols["_start"] not in {item_address for _, item_address, _ in planned}:
        raise ByteForgeError(definitions["_start"], "_start must identify an instruction, not the end of code")

    items = []
    for instruction, item_address, size in planned:
        data = encode(instruction, item_address, symbols)
        if len(data) != size:
            raise ByteForgeError(instruction.location, "Encoder size mismatch during assembly")
        items.append(EncodedItem(item_address, data, instruction))
    return AssemblyResult(b"".join(item.data for item in items), b"", symbols, items)
