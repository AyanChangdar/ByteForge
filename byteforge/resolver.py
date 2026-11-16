"""Two-pass assembly of instructions followed by read-only DB bytes."""

from byteforge.encoder import encode, encoded_size
from byteforge.model import (
    AssemblyResult, ByteForgeError, DataDirective, EncodedItem, Instruction,
    Label, Program, SourceLocation, SymbolReference,
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
    data_payload = bytearray()
    in_data = False
    address = base_address
    for statement in program.statements:
        if isinstance(statement, Label):
            if statement.name in symbols:
                raise ByteForgeError(statement.location, f"Duplicate label: {statement.name}")
            if address >= (1 << 64):
                raise ByteForgeError(statement.location, "Label exceeds the 64-bit address range")
            symbols[statement.name] = address
            definitions[statement.name] = statement.location
        elif isinstance(statement, DataDirective):
            in_data = True
            if address + len(statement.data) > (1 << 64):
                raise ByteForgeError(statement.location, "Data exceeds the 64-bit address range")
            data_payload.extend(statement.data)
            address += len(statement.data)
        else:
            if in_data:
                raise ByteForgeError(statement.location, "Instructions cannot follow a DB directive")
            size = encoded_size(statement)
            if address + size > (1 << 64):
                raise ByteForgeError(statement.location, "Instruction exceeds the 64-bit address range")
            planned.append((statement, address, size))
            address += size

    if "_start" not in symbols:
        raise ByteForgeError(fallback, "Required entry label _start is missing")
    if symbols["_start"] not in {item_address for _, item_address, _ in planned}:
        raise ByteForgeError(definitions["_start"], "_start must identify an instruction, not the end of code or data")

    items = []
    for instruction, item_address, size in planned:
        data = encode(instruction, item_address, symbols)
        if len(data) != size:
            raise ByteForgeError(instruction.location, "Encoder size mismatch during assembly")
        resolution = {operand.name: symbols[operand.name] for operand in instruction.operands
                      if isinstance(operand, SymbolReference)}
        items.append(EncodedItem(item_address, data, instruction, resolution))
    return AssemblyResult(b"".join(item.data for item in items), bytes(data_payload), symbols, items)
