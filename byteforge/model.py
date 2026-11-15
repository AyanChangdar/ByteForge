"""Shared source-location and diagnostic contracts."""

from dataclasses import dataclass


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
