"""Build a fixed-address, sectionless Linux x86-64 ELF executable in memory."""

import struct

from byteforge.model import AssemblyResult, ElfConfig, ElfLayout, ElfSegment


_ELF_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
_PROGRAM_HEADER = struct.Struct("<IIQQQQQQ")


def _validate_config(config: ElfConfig) -> None:
    if _ELF_HEADER.size != 64 or _PROGRAM_HEADER.size != 56:
        raise ValueError("Unexpected ELF64 struct sizes")
    values = (config.image_base, config.code_offset, config.alignment)
    if any(type(value) is not int for value in values):
        raise ValueError("ELF configuration values must be integers")
    # Only the approved layout is exposed in this milestone.
    if config.alignment != 0x1000 or config.image_base % config.alignment:
        raise ValueError("ELF image base must be aligned to 0x1000")
    if config.code_offset < _ELF_HEADER.size + _PROGRAM_HEADER.size:
        raise ValueError("Code offset overlaps the ELF headers")
    if config.code_offset % config.alignment:
        raise ValueError("Code offset must be aligned to 0x1000")
    if config != ElfConfig():
        raise ValueError("Only the fixed Milestone 4 ELF layout is supported")


def _validate_result(result: AssemblyResult, code_address: int) -> int:
    """Reject inconsistent result objects before serializing an executable."""
    if not result.code or not result.items:
        raise ValueError("Executable must contain instructions")
    address = code_address
    for item in result.items:
        if item.address != address or not item.data:
            raise ValueError("Assembly item address does not match the ELF code layout")
        address += len(item.data)
    if b"".join(item.data for item in result.items) != result.code:
        raise ValueError("Assembly items do not match the code payload")
    entry = result.symbols.get("_start")
    if type(entry) is not int or not code_address <= entry < address:
        raise ValueError("ELF entry must be inside the code payload")
    if entry not in {item.address for item in result.items}:
        raise ValueError("ELF entry must be an instruction boundary")
    return entry


def build_executable(
    result: AssemblyResult, config: ElfConfig
) -> tuple[bytes, ElfLayout]:
    """Return ELF bytes and layout metadata; perform no file I/O or printing.

    Invalid configuration/result contracts raise ValueError. Source-input
    diagnostics are produced by the parser, encoder and resolver beforehand.
    """
    _validate_config(config)
    entry = _validate_result(result, config.image_base + config.code_offset)
    file_size = config.code_offset + len(result.code) + len(result.data)
    if file_size >= (1 << 64) or config.image_base + file_size > (1 << 64):
        raise ValueError("ELF image exceeds the 64-bit address range")

    identity = b"\x7fELF" + bytes((2, 1, 1, 0, 0)) + bytes(7)
    header = _ELF_HEADER.pack(
        identity, 2, 62, 1, entry, 64, 0, 0, 64, 56, 1, 0, 0, 0
    )
    segment = ElfSegment(1, 5, 0, config.image_base, config.image_base,
                         file_size, file_size, config.alignment)
    program_header = _PROGRAM_HEADER.pack(
        segment.p_type, segment.p_flags, segment.p_offset, segment.p_vaddr,
        segment.p_paddr, segment.p_filesz, segment.p_memsz, segment.p_align,
    )
    padding_size = config.code_offset - len(header) - len(program_header)
    image = header + program_header + bytes(padding_size) + result.code + result.data
    if len(image) != file_size:
        raise ValueError("ELF output length does not match its segment size")
    return image, ElfLayout(entry, [segment], file_size)
