# ByteForge TraceLab: Stage 6A.2

TraceLab is a bounded educational simulator of ByteForge's existing typed
instruction forms. The real assembler remains the source of machine-code
bytes, symbol addresses and ELF layout. Tracing does not execute those bytes,
decode machine code, inspect hardware registers or launch a Linux process.
The native ELF pipeline and all existing assembler interfaces are unchanged.

## Public interfaces

```python
prepare_trace(source: str, filename: str, limits: TraceLimits) -> TraceProgram
initial_state(program: TraceProgram) -> CPUState
step(program: TraceProgram, state: CPUState) -> TraceStep
read_memory(program: TraceProgram, address: int, length: int = 16) -> bytes
TraceSession(program: TraceProgram)
```

These functions perform no file I/O, printing, subprocess calls or native
execution. Callers supply source text. Preparation preserves existing
`ByteForgeError` diagnostics from parsing, resolution and encoding. Trace
source/instruction limit errors also use `ByteForgeError`. Invalid internal
ELF address mapping raises `ValueError`, as with existing ELF contract errors.
Runtime tracing faults are returned explicitly in `TraceStep`.

```python
from byteforge.trace import initial_state, prepare_trace, step
from byteforge.trace_model import TraceLimits

program = prepare_trace('_start: mov rax, 60\nsyscall', 'example.asm', TraceLimits())
first = step(program, initial_state(program))
assert first.after.registers['rax'] == 60
exited = step(program, first.after)
assert exited.after.exit_status == 0
assert exited.after.step_count == 2
```

This records a simulated exit; it does not terminate the Python process.

## Trace-only contracts

All contracts are frozen dataclasses except the `TraceStatus` enum. They live
in `byteforge/trace_model.py`, independently of the assembler's shared models.

| Contract | Fields |
|---|---|
| `TraceLimits` | `max_source_bytes`, `max_instructions`, `max_steps`, `max_output_bytes`, `max_history`, `max_continue_steps`, `max_memory_bytes` |
| `CPUState` | `rip`, `registers`, `zf`, `step_count`, `status`, `halt_reason`, `stdout_size`, `exit_status` |
| `TraceProgram` | `source`, parsed `program`, `assembly`, `elf_image`, `elf_layout`, `instructions`, `symbols`, `entry`, `code_base`, `data_base`, `data`, `limits` |
| `TraceFault` | `code`, `message`, `location` (optional `SourceLocation`) |
| `TraceStep` | `location`, `address`, actual `data` bytes, `before`, `after`, `changed_registers`, `branch_taken`, `branch_target`, `executed`, `fault`, emitted `output` bytes |
| `TraceView` | current `state`, visible `stdout` bytes, history `cursor`, `history_size`, `last_step` |

The register mapping contains exactly `rax`, `rcx`, `rdx`, `rbx`, `rsp`, `rbp`,
`rsi`, `rdi`. Values, RIP and the step count must be integers in `0..2^64-1`;
booleans are not accepted as integers. ZF is a boolean. Python integers retain
all 64 bits exactly. No JSON serialization is introduced in this stage.

Registers are defensively copied into read-only mappings. Each step creates a
new state; it never changes the input or earlier results. `changed_registers`
lists only values that actually changed, in the register order above; it does
not include RIP or flags. The pure step function retains no history itself.
`stdout_size` counts captured bytes, while `TraceStep.output` stores only bytes
emitted by that transition. `exit_status` is `None` until a simulated exit,
then an integer in `0..255`.

`TraceProgram.instructions` maps virtual addresses to actual `EncodedItem`
objects, retaining their exact encoded bytes and original typed instructions.
Instruction and symbol mappings are separate read-only copies. Retained legacy
`Program`, `AssemblyResult` and `ElfLayout` containers still contain mutable
lists/dictionaries under their existing contracts: treat them as read-only.
Stepping uses the independent lookups and immutable instruction fields, not
those legacy lists or `EncodedItem.resolution` dictionaries.

## Preparation and addresses

Preparation checks UTF-8 source length, calls `parse()`, limits the instruction
count, calls `assemble()` at the default ELF code address, and calls
`build_executable()` with the existing `ElfConfig`. No second parser, encoder,
resolver or decoder is added. The full ELF image is retained in memory.

- Initial RIP is the actual ELF entry / resolved `_start` address. Instructions
  preceding `_start` are assembled but not initially executed.
- Code base is the first encoded item's address, not necessarily `_start`.
- Data base is `code_base + len(assembly.code)`. Read-only DB bytes occupy
  `[data_base, data_base + len(data))`. Empty DB strings retain existing layout.
- Segment mapping uses `file_offset = p_offset + virtual_address - p_vaddr`.
  Preparation verifies the entry, code/data slices, final segment length and
  memory size against the actual generated ELF.
- Symbol spelling and resolver validation remain unchanged. Branch labels must
  identify assembled instruction boundaries; data is not executable.

## Initial simulated state

All eight registers are zero, RIP is the resolved entry, ZF is false, step
count is zero, status is `TraceStatus.RUNNING`, and `halt_reason` is `None`.
These are deterministic educational values, **not Linux process-entry values**.
Initial `stdout_size` is zero and `exit_status` is `None`. There is no simulated
stack or writable memory.

## Instruction semantics

Every executed instruction increments the step count once. Sequential RIP
uses the actual encoded byte length. `MASK64 = (1 << 64) - 1` limits arithmetic
and MOV results to 64 bits. Existing encoder validation establishes operand
forms and immediate ranges before tracing.

| Form | Register effect | ZF | Next RIP |
|---|---|---|---|
| `MOV r64, integer` | destination = integer & MASK64 | preserved | current RIP + byte length |
| `MOV r64, symbol` | destination = final symbol address | preserved | sequential |
| `ADD r64, signed imm32` | destination = (old + signed immediate) & MASK64 | result == 0 | sequential |
| `SUB r64, signed imm32` | destination = (old - signed immediate) & MASK64 | result == 0 | sequential |
| `CMP r64, signed imm32` | no register write; compare (old - immediate) & MASK64 | comparison result == 0 | sequential |
| `XOR r64, r64` | destination = original destination XOR original source | result == 0 | sequential |
| `JMP label` | no register write | preserved | final symbol address |
| `JE label` | no register write | preserved | target when ZF, else sequential |
| `JNE label` | no register write | preserved | target when not ZF, else sequential |
| `SYSCALL`, RAX=1 | modeled stdout write; RAX=count, RCX=next instruction | preserved | sequential |
| `SYSCALL`, RAX=60 | record RDI & 255; RCX=next instruction; halt | preserved | sequential address, with no further execution |

Only ZF is modeled. Other x86 flags, exceptions, addressing modes and operating
system behavior beyond the two modeled syscalls are outside this stage. Branch results record the resolved
target even when not taken. Non-branch results have `None` branch fields.

## Limits and stop behavior

Defaults are also hard upper bounds; callers may lower them:

| Limit | Default / maximum |
|---|---:|
| UTF-8 source bytes | 65,536 |
| Assembled instructions | 2,048 |
| Executed steps | 10,000 |
| Captured stdout bytes | 65,536 |
| Saved instruction transitions | 10,000 |
| Steps per continue request | 100 |
| Bytes per memory inspection | 256 |

Limits must be positive integers; unlimited configurations are rejected.
`step()` never contains a playback loop. After the allowed number of executed
steps, the next request stops before any additional instruction is executed.

| Fault code | Result |
|---|---|
| `unsupported_syscall` | unknown syscall number; `FAULTED`, no effects; actual syscall location |
| `unsupported_descriptor` | write to a descriptor other than 1; fault with no effects |
| `invalid_memory` | write range outside DB data; fault with no effects |
| `output_limit` | write would exceed captured-output limit; fault with no partial write |
| `invalid_rip` on input | `FAULTED`, no execution; location is `None` and bytes are empty because no instruction exists there |
| `invalid_rip` after sequential execution | `FAULTED`, completed instruction effects/count retained; location identifies that instruction |
| `invalid_target` | defensive branch-boundary failure before execution; operand location in the fault |
| `unsupported_instruction` | defensive unsupported typed instruction failure before execution |
| `step_limit` | `HALTED`, unchanged CPU values/count; current instruction location |
| `history_limit` | `HALTED` before another execution; current instruction location |
| `state_stopped` | no execution; returns the same stopped state, preserving its prior reason |

`halt_reason` stores the fault/limit code, or `exit` for a successful simulated
termination. `TraceFault.message` explains a fault or limit.
`executed` distinguishes a completed instruction that subsequently left code
from a blocked attempt. A stopped state cannot resume through `step()`; call
`initial_state()` to restart, or use the session's back/reset operations.

## Modeled Linux syscalls

For syscall 1, only RDI=1 (stdout) is supported. RSI is an actual mapped DB
virtual address and RDX is the byte count. The entire positive-length range
must be inside DB data; headers, code, stack and other memory are unavailable.
A zero count succeeds without reading memory, even with an unmapped pointer,
but still requires descriptor 1. Writes capture exact bytes, including NUL and
non-text bytes, and return the full count in RAX. They never invoke an OS write
or print from the engine. Invalid ranges/descriptors and output overflow fault
before any effects; no partial output or invented errno return is produced.

For syscall 60, the exit status is RDI & 0xFF. The transition is executed and
counted, then status becomes HALTED with reason `exit`. RIP records the
sequential address even if it is one-past-code; no following instruction runs.
RAX and RDI are preserved on exit. Both successful modeled syscalls put the
following instruction address in RCX. R11 and complete RFLAGS are not modeled.
ZF is preserved as an explicit educational simplification.

All other syscall numbers fault, including exit_group. This is a deterministic
model of complete writes and process exit, not a Linux kernel model: signals,
partial writes, scheduling, errno and other descriptors are unsupported.
Falling out of code after a successful write retains that write's output and
reports `invalid_rip`; it is never treated as a successful exit.

## Reversible sessions

```python
session = TraceSession(program)
transition = session.next()          # TraceStep
view = session.back()               # TraceView; initial state is the lower bound
view = session.continue_(100)        # TraceView; 'continue' is a Python keyword
view = session.reset()              # clear history/output and restore initial state
view = session.view()               # immutable current state and visible output
```

`continue_(max_steps=100)` validates a positive request up to the configured
per-request bound. When lowering that limit, supply a count within the lower
bound explicitly. It stops on exit, fault or limits; cached transitions also
consume the request budget. A still-running view simply means the request
budget was consumed, not that a process exit occurred.

History stores immutable transitions at its frontier only. Back moves a cursor
without inverse execution; next after back reuses the exact cached transition.
Back across exit/fault restores the previous runnable state and reason. Next
at a stopped frontier never grows history or executes another instruction.
Reset discards all cached transitions, exit state and captured output.

One bounded private byte buffer stores frontier output. Saved states retain
only output lengths, and transitions retain their own emitted chunks. A view
copies the visible prefix into immutable bytes. Back hides later output;
cached forward never appends it again. This avoids a full output copy in every
saved CPU state. History stores at most `max_history` transitions plus one
non-executed terminal limit result. Step-limit exhaustion takes precedence
when both limits are reached. Previous states are never silently evicted.

The session's program property has no setter. Source/context changes require
a new prepared program and session. As before, treat retained legacy assembler
lists as read-only; execution uses independent immutable lookups.

`read_memory()` returns immutable DB bytes only and validates unsigned 64-bit
addresses, the inspection length bound and mapped range. A zero-length
inspection must still address the DB interval or its end. This differs from
zero-length write's explicit no-read rule. Write byte counts are bounded by
mapped data and output capacity, not by the smaller inspection limit.

## Terminal debugger

From the repository root, on Windows or Linux/WSL:

```bash
python -m byteforge.trace_cli examples/loop.asm --interactive
```

Commands: `next`/`n`, `back`, `registers`, `memory ADDRESS [LENGTH]`,
`continue`/`c [STEPS]`, `reset`, `help`, `quit`/`q`. Addresses and lengths
accept decimal or 0x notation. Memory inspection defaults to 16 bytes and
continue defaults to 100 steps. Invalid commands are reported without ending
the debugger. Input files are read with the source-size bound before parsing.
The currently implemented entry point requires `--interactive`.

The CLI labels all state SIMULATED, displays source locations, actual bytes,
register changes, RIP/ZF, branch decisions, captured stdout and exit status.
Captured bytes are displayed with Python byte-string notation, avoiding raw
binary/control output. It never launches the generated ELF or writes files.
Quit/EOF returns debugger status 0 regardless of simulated exit status; Ctrl+C
returns 130. Source/file errors return 1 and argument errors return 2.

## Validation and deferred work

```bash
python -B -m unittest discover -s tests -p test_trace.py
python -B -m unittest discover -s tests -p test_trace_cli.py
python -B -m unittest discover -s tests
git diff --check
```

Tests cover preparation metadata, entry offsets, ELF mapping, immutable
states, unsigned wrapping, signed immediates, ZF, both conditional outcomes,
forward/backward branches, bounded loops, diagnostics and unsupported behavior.
The existing loop's first twelve instructions are checked against actual
addresses/bytes. Its following syscall now records simulated exit status 7.
All five existing examples are also compared against the original assembler
pipeline for byte-identical ELF images; native tests retain platform skips.

Deferred: HTTP server, browser/frontend, JSON serialization and byte
explanations. No native execution or full CPU/OS emulation is added.
