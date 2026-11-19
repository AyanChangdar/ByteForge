"""Shared source-location and diagnostic contracts."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceLocation:
    """An original source line and its one-based character position."""

    filename: str
    line: int
    column: int
    text: str


class ByteForgeError(Exception):
    """A user-input error with source context; formatting has no side effects."""

    def __init__(self, location: SourceLocation, message: str) -> None:
        self.location = location
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        location = self.location
        caret_offset = min(max(location.column - 1, 0), len(location.text))
        padding = "".join(
            "\t" if character == "\t" else " "
            for character in location.text[:caret_offset]
        )
        return (
            f"{location.filename}:{location.line}:{location.column}: {self.message}\n"
            f"{location.text}\n"
            f"{padding}^"
        )


@dataclass(frozen=True)
class Register:
    name: str
    code: int
    width: int
    location: SourceLocation


@dataclass(frozen=True)
class Immediate:
    value: int
    location: SourceLocation


@dataclass(frozen=True)
class SymbolReference:
    name: str
    location: SourceLocation


@dataclass(frozen=True)
class Instruction:
    mnemonic: str
    operands: tuple[Register | Immediate | SymbolReference, ...]
    location: SourceLocation


@dataclass(frozen=True)
class Label:
    name: str
    location: SourceLocation


@dataclass(frozen=True)
class DataDirective:
    data: bytes
    location: SourceLocation


@dataclass(frozen=True)
class Program:
    statements: list[Instruction | Label | DataDirective]


@dataclass(frozen=True)
class EncodedItem:
    address: int
    data: bytes
    statement: Instruction
    resolution: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AssemblyResult:
    code: bytes
    data: bytes
    symbols: dict[str, int]
    items: list[EncodedItem]


@dataclass(frozen=True)
class ElfConfig:
    image_base: int = 0x400000
    code_offset: int = 0x1000
    alignment: int = 0x1000


@dataclass(frozen=True)
class ElfSegment:
    p_type: int
    p_flags: int
    p_offset: int
    p_vaddr: int
    p_paddr: int
    p_filesz: int
    p_memsz: int
    p_align: int


@dataclass(frozen=True)
class ElfLayout:
    entry: int
    segments: list[ElfSegment]
    file_size: int
