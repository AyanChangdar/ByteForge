# ByteForge TraceLab: Stage 6A.1

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
blocked = step(program, first.after)
assert blocked.fault.code == 'unsupported_syscall'
assert blocked.after.step_count == 1
```

This example does not perform a Linux exit.

## Trace-only contracts

All contracts are frozen dataclasses except the `TraceStatus` enum. They live
in `byteforge/trace_model.py`, independently of the assembler's shared models.

| Contract | Fields |
|---|---|
| `TraceLimits` | `max_source_bytes`, `max_instructions`, `max_steps` |
| `CPUState` | `rip`, `registers`, `zf`, `step_count`, `status`, `halt_reason` |
| `TraceProgram` | `source`, parsed `program`, `assembly`, `elf_image`, `elf_layout`, `instructions`, `symbols`, `entry`, `code_base`, `data_base`, `data`, `limits` |
| `TraceFault` | `code`, `message`, `location` (optional `SourceLocation`) |
| `TraceStep` | `location`, `address`, actual `data` bytes, `before`, `after`, `changed_registers`, `branch_taken`, `branch_target`, `executed`, `fault` |

The register mapping contains exactly `rax`, `rcx`, `rdx`, `rbx`, `rsp`, `rbp`,
`rsi`, `rdi`. Values, RIP and the step count must be integers in `0..2^64-1`;
booleans are not accepted as integers. ZF is a boolean. Python integers retain
all 64 bits exactly. No JSON serialization is introduced in this stage.

Registers are defensively copied into read-only mappings. Each step creates a
new state; it never changes the input or earlier results. `changed_registers`
lists only values that actually changed, in the register order above; it does
not include RIP or flags. No internal history is retained.

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
There is no simulated stack, writable memory, stdout or exit status yet.

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
| `SYSCALL` | unsupported; no execution/effects | unchanged | unchanged |

Only ZF is modeled. Other x86 flags, exceptions, addressing modes and operating
system behavior are outside this stage. Branch results record the resolved
target even when not taken. Non-branch results have `None` branch fields.

## Limits and stop behavior

Defaults are also hard upper bounds; callers may lower them:

| Limit | Default / maximum |
|---|---:|
| UTF-8 source bytes | 65,536 |
| Assembled instructions | 2,048 |
| Executed steps | 10,000 |

Limits must be positive integers; unlimited configurations are rejected.
`step()` never contains a playback loop. After the allowed number of executed
steps, the next request stops before any additional instruction is executed.

| Fault code | Result |
|---|---|
| `unsupported_syscall` | `FAULTED`, no RIP/register/ZF/count effects; actual syscall source location |
| `invalid_rip` on input | `FAULTED`, no execution; location is `None` and bytes are empty because no instruction exists there |
| `invalid_rip` after sequential execution | `FAULTED`, completed instruction effects/count retained; location identifies that instruction |
| `invalid_target` | defensive branch-boundary failure before execution; operand location in the fault |
| `unsupported_instruction` | defensive unsupported typed instruction failure before execution |
| `step_limit` | `HALTED`, unchanged CPU values/count; current instruction location |
| `state_stopped` | no execution; returns the same stopped state, preserving its prior reason |

`halt_reason` stores the fault/limit code. `TraceFault.message` explains it.
`executed` distinguishes a completed instruction that subsequently left code
from a blocked attempt. A stopped state cannot resume through `step()`; call
`initial_state()` to restart. Reset/history management belongs to later stages.

SYSCALL is always unsupported regardless of RAX. It produces no fake Linux
return value, output, exit status or RCX clobber. Reaching the end of code or
DB data is a fault, never an invented successful process exit.

## Validation and deferred work

```bash
python -B -m unittest discover -s tests -p test_trace.py
python -B -m unittest discover -s tests
git diff --check
```

Tests cover preparation metadata, entry offsets, ELF mapping, immutable
states, unsigned wrapping, signed immediates, ZF, both conditional outcomes,
forward/backward branches, bounded loops, diagnostics and unsupported behavior.
The existing loop's first twelve instructions are checked against actual
addresses/bytes. Its following syscall attempt is explicitly blocked.
All five existing examples are also compared against the original assembler
pipeline for byte-identical ELF images; native tests retain platform skips.

Deferred: write/exit syscalls, stdout, syscall RCX effects, reverse history,
terminal debugger, HTTP server, frontend, serialization and byte explanations.
No browser or terminal debugging interface is available in Stage 6A.1.
