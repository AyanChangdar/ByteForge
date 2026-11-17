"""Trace-only contracts for the bounded educational instruction simulator."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from byteforge.model import AssemblyResult, EncodedItem, ElfLayout, Program, SourceLocation


MASK64 = (1 << 64) - 1
REGISTER_NAMES = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi")


class TraceStatus(Enum):
    RUNNING = "running"
    HALTED = "halted"
    FAULTED = "faulted"


@dataclass(frozen=True)
class TraceLimits:
    max_source_bytes: int = 65536
    max_instructions: int = 2048
    max_steps: int = 10000

    def __post_init__(self) -> None:
        for name, maximum in (("max_source_bytes", 65536), ("max_instructions", 2048),
                              ("max_steps", 10000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer in 1..{maximum}")


@dataclass(frozen=True)
class CPUState:
    rip: int
    registers: Mapping[str, int] = field(default_factory=lambda: dict.fromkeys(REGISTER_NAMES, 0))
    zf: bool = False
    step_count: int = 0
    status: TraceStatus = TraceStatus.RUNNING
    halt_reason: str | None = None

    def __post_init__(self) -> None:
        registers = dict(self.registers)
        if set(registers) != set(REGISTER_NAMES):
            raise ValueError("CPUState requires exactly the eight base registers")
        for value in (*registers.values(), self.rip, self.step_count):
            if type(value) is not int or not 0 <= value <= MASK64:
                raise ValueError("Machine values and step count must be unsigned 64-bit integers")
        if type(self.zf) is not bool or not isinstance(self.status, TraceStatus):
            raise ValueError("CPUState requires a boolean ZF and a TraceStatus")
        object.__setattr__(self, "registers", MappingProxyType(registers))


@dataclass(frozen=True)
class TraceProgram:
    source: str
    program: Program
    assembly: AssemblyResult
    elf_image: bytes
    elf_layout: ElfLayout
    instructions: Mapping[int, EncodedItem]
    symbols: Mapping[str, int]
    entry: int
    code_base: int
    data_base: int
    data: bytes
    limits: TraceLimits

    def __post_init__(self) -> None:
        # The legacy assembly objects are retained, but execution uses separate
        # read-only lookup copies, never their mutable lists/dictionaries.
        object.__setattr__(self, "instructions", MappingProxyType(dict(self.instructions)))
        object.__setattr__(self, "symbols", MappingProxyType(dict(self.symbols)))


@dataclass(frozen=True)
class TraceFault:
    code: str
    message: str
    location: SourceLocation | None


@dataclass(frozen=True)
class TraceStep:
    location: SourceLocation | None
    address: int
    data: bytes
    before: CPUState
    after: CPUState
    changed_registers: tuple[str, ...] = ()
    branch_taken: bool | None = None
    branch_target: int | None = None
    executed: bool = False
    fault: TraceFault | None = None
