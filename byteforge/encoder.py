"""Handwritten encodings for MOV r64, integer/symbol and SYSCALL."""

from collections.abc import Mapping
import struct

from byteforge.model import (
    ByteForgeError, Immediate, Instruction, Register, SourceLocation, SymbolReference,
)


_BASE_REGISTER_CODES = {
    "rax": 0, "rcx": 1, "rdx": 2, "rbx": 3,
    "rsp": 4, "rbp": 5, "rsi": 6, "rdi": 7,
}
_FORM_SIZES = {
    ("mov", (Register, Immediate)): 10,
    ("mov", (Register, SymbolReference)): 10,
    ("syscall", ()): 2,
}
_MIN_IMM64 = -(1 << 63)
_MAX_IMM64 = (1 << 64) - 1


def _base_register_code(register: Register) -> int:
    """Validate the complete register contract, including constructed models."""
    expected = _BASE_REGISTER_CODES.get(register.name)
    if (expected is None or type(register.code) is not int
            or register.code != expected or type(register.width) is not int
            or register.width != 64):
        raise ByteForgeError(
            register.location, "Expected a base 64-bit register with matching name and code"
        )
    return register.code


def _rex(w: int, r: int = 0, x: int = 0, b: int = 0) -> bytes:
    """Compose a REX prefix from single-bit fields supplied by the encoder."""
    if any(bit not in (0, 1) for bit in (w, r, x, b)):
        raise ValueError("REX fields must be single bits")
    return bytes((0x40 | (w << 3) | (r << 2) | (x << 1) | b,))


def _imm64_bits(value: int, location: SourceLocation) -> int:
    if type(value) is not int:
        raise ByteForgeError(location, "MOV immediate must be an integer")
    if not _MIN_IMM64 <= value <= _MAX_IMM64:
        raise ByteForgeError(location, "MOV immediate must be in range -2^63 through 2^64-1")
    # Mask only after checking the range, so overflow is never truncated.
    return value & _MAX_IMM64


def _pack_imm64(value: int, location: SourceLocation) -> bytes:
    return struct.pack("<Q", _imm64_bits(value, location))


def _validate(instruction: Instruction) -> int:
    """Share form, register and range checks between sizing and emission."""
    mnemonic = instruction.mnemonic
    if mnemonic not in ("mov", "syscall"):
        raise ByteForgeError(instruction.location, f"Unsupported instruction: {mnemonic}")
    expected = (Register, (Immediate, SymbolReference)) if mnemonic == "mov" else ()
    if len(instruction.operands) != len(expected):
        raise ByteForgeError(
            instruction.location,
            f"{mnemonic.upper()} requires {len(expected)} operands",
        )
    for operand, kind in zip(instruction.operands, expected):
        if not isinstance(operand, kind):
            raise ByteForgeError(
                operand.location,
                "MOV requires a register destination and an integer immediate or symbol source",
            )
    if mnemonic == "mov":
        destination, source = instruction.operands
        _base_register_code(destination)
        if isinstance(source, Immediate):
            _imm64_bits(source.value, source.location)
            return _FORM_SIZES[(mnemonic, (Register, Immediate))]
        return _FORM_SIZES[(mnemonic, (Register, SymbolReference))]
    return _FORM_SIZES[(mnemonic, ())]


def _symbol_address(source: SymbolReference, symbols: Mapping[str, int]) -> int:
    try:
        value = symbols[source.name]
    except KeyError:
        raise ByteForgeError(source.location, f"Undefined symbol: {source.name}") from None
    if type(value) is not int or not 0 <= value <= _MAX_IMM64:
        raise ByteForgeError(source.location, "Symbol address must be an unsigned 64-bit integer")
    return value


def encoded_size(instruction: Instruction) -> int:
    """Return the fixed size of a valid supported instruction."""
    return _validate(instruction)


def encode(
    instruction: Instruction, address: int, symbols: Mapping[str, int]
) -> bytes:
    """Encode one instruction using final absolute symbol addresses.

    The instruction address is reserved for future relative branch forms.
    No file I/O or printing occurs.
    """
    _validate(instruction)
    if instruction.mnemonic == "syscall":
        return b"\x0f\x05"
    destination, source = instruction.operands
    code = _base_register_code(destination)
    value = (_symbol_address(source, symbols) if isinstance(source, SymbolReference)
             else source.value)
    return (
        _rex(w=1)
        + bytes((0xB8 + code,))
        + _pack_imm64(value, source.location)
    )
