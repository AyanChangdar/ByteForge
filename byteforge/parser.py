"""Minimal assembly parser; no encoding, symbol resolution, or file I/O."""

from dataclasses import dataclass
import re

from byteforge.model import (
    ByteForgeError, DataDirective, Immediate, Instruction, Label, Program,
    Register, SourceLocation, SymbolReference,
)


_REGISTER_CODES = {
    "rax": 0, "rcx": 1, "rdx": 2, "rbx": 3,
    "rsp": 4, "rbp": 5, "rsi": 6, "rdi": 7,
}
_UNSUPPORTED_REGISTERS = set(
    "eax ecx edx ebx esp ebp esi edi ax cx dx bx sp bp si di "
    "al cl dl bl ah ch dh bh spl bpl sil dil rip eip ip".split()
) | {f"r{number}{suffix}" for number in range(8, 16) for suffix in ("", "d", "w", "b")}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_INTEGER = re.compile(r"[+-]?(?:[0-9]+|0[xX][0-9a-fA-F]+)\Z")
_ESCAPES = {"\\": 92, '"': 34, "n": 10, "r": 13, "t": 9, "0": 0}


@dataclass(frozen=True)
class _Token:
    text: str
    location: SourceLocation
    data: bytes | None = None


def _tokenize(line: str, filename: str, line_number: int) -> list[_Token]:
    """Scan quotes before interpreting comments or separators."""
    tokens = []
    index = 0
    while index < len(line):
        character = line[index]
        if character.isspace():
            index += 1
            continue
        if character == ";":
            break
        start = index
        location = SourceLocation(filename, line_number, start + 1, line)
        if character == '"':
            data = bytearray()
            index += 1
            while index < len(line) and line[index] != '"':
                character = line[index]
                if character == "\\":
                    escape_column = index + 1
                    index += 1
                    if index == len(line):
                        raise ByteForgeError(location, "Unterminated string")
                    if line[index] not in _ESCAPES:
                        raise ByteForgeError(
                            SourceLocation(filename, line_number, escape_column, line),
                            "Unsupported string escape",
                        )
                    data.append(_ESCAPES[line[index]])
                else:
                    if ord(character) > 127:
                        raise ByteForgeError(
                            SourceLocation(filename, line_number, index + 1, line),
                            "DB strings must contain ASCII characters",
                        )
                    data.append(ord(character))
                index += 1
            if index == len(line):
                raise ByteForgeError(location, "Unterminated string")
            index += 1
            tokens.append(_Token(line[start:index], location, bytes(data)))
        elif character in ",:":
            index += 1
            tokens.append(_Token(character, location))
        else:
            while (index < len(line) and not line[index].isspace()
                   and line[index] not in ',:;"'):
                index += 1
            tokens.append(_Token(line[start:index], location))
    return tokens


def _integer(token: _Token) -> int:
    if token.data is not None or not _INTEGER.fullmatch(token.text):
        raise ByteForgeError(token.location, "Invalid integer literal")
    unsigned = token.text.lstrip("+-")
    try:
        return int(token.text, 16 if unsigned.lower().startswith("0x") else 10)
    except ValueError:
        raise ByteForgeError(token.location, "Integer literal is too large to parse") from None


def _operands(tokens: list[_Token], end: SourceLocation) -> list[_Token]:
    """Require one token per operand and a comma between each pair."""
    operands = []
    expect_operand = True
    for token in tokens:
        if expect_operand:
            if token.text in (",", ":") and token.data is None:
                raise ByteForgeError(token.location, "Missing operand")
            operands.append(token)
        elif token.text != "," or token.data is not None:
            raise ByteForgeError(token.location, "Expected comma between operands")
        expect_operand = not expect_operand
    if tokens and expect_operand:
        raise ByteForgeError(end, "Missing operand after comma")
    return operands


def _instruction(head: _Token, operands: list[_Token], end: SourceLocation) -> Instruction:
    mnemonic = head.text.lower()
    expected = 2 if mnemonic == "mov" else 0
    if len(operands) < expected:
        raise ByteForgeError(end, "MOV requires two operands")
    if len(operands) > expected:
        raise ByteForgeError(operands[expected].location, f"Extra operand for {mnemonic.upper()}")
    if mnemonic == "syscall":
        return Instruction(mnemonic, (), head.location)
    destination, source = operands
    name = destination.text.lower()
    if destination.data is not None or name not in _REGISTER_CODES:
        raise ByteForgeError(destination.location, "MOV destination must be a supported 64-bit register")
    register = Register(name, _REGISTER_CODES[name], 64, destination.location)
    source_name = source.text.lower()
    if source.data is not None:
        raise ByteForgeError(source.location, "MOV source must be an integer or symbol")
    if source_name in _REGISTER_CODES:
        raise ByteForgeError(source.location, "Register-to-register MOV is not supported")
    if source_name in _UNSUPPORTED_REGISTERS:
        raise ByteForgeError(source.location, "Unsupported register")
    if _IDENTIFIER.fullmatch(source.text):
        value = SymbolReference(source.text, source.location)
    else:
        value = Immediate(_integer(source), source.location)
    return Instruction(mnemonic, (register, value), head.location)


def parse(source: str, filename: str) -> Program:
    """Parse MOV, SYSCALL, labels and DB; preserve unresolved references."""
    statements: list[Instruction | Label | DataDirective] = []
    # Only CR/LF terminate lines; other characters remain in the source text.
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line_number, line in enumerate(lines, 1):
        tokens = _tokenize(line, filename, line_number)
        if not tokens:
            continue
        if len(tokens) > 1 and tokens[1].text == ":" and tokens[1].data is None:
            label = tokens[0]
            if label.data is not None or not _IDENTIFIER.fullmatch(label.text):
                raise ByteForgeError(label.location, "Malformed label")
            statements.append(Label(label.text, label.location))
            tokens = tokens[2:]
        if not tokens:
            continue
        for token in tokens:
            if token.text == ":" and token.data is None:
                raise ByteForgeError(token.location, "Malformed label or multiple labels on one line")
        head = tokens[0]
        mnemonic = head.text.lower()
        if head.data is not None or mnemonic not in ("mov", "syscall", "db"):
            raise ByteForgeError(head.location, f"Unknown instruction: {head.text}")
        # Missing input is indicated immediately after the last syntactic token,
        # before any trailing whitespace or comment.
        last = tokens[-1]
        end = SourceLocation(filename, line_number, last.location.column + len(last.text), line)
        operands = _operands(tokens[1:], end)
        if mnemonic == "db":
            if not operands:
                raise ByteForgeError(end, "DB requires at least one value")
            data = bytearray()
            for operand in operands:
                if operand.data is not None:
                    data.extend(operand.data)
                else:
                    value = _integer(operand)
                    if not 0 <= value <= 255:
                        raise ByteForgeError(operand.location, "DB byte must be between 0 and 255")
                    data.append(value)
            statements.append(DataDirective(bytes(data), head.location))
        else:
            statements.append(_instruction(head, operands, end))
    return Program(statements)
