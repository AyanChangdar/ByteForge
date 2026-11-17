# ByteForge TraceLab: Stage 6B.1

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
all 64 bits exactly. The Stage 6B.1 adapter below serializes them losslessly.

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

Stage 6B.1 adds the local HTTP and JSON adapters described below. Browser/UI
work and byte explanations remain deferred. No native execution or full
CPU/OS emulation is added.

## Local HTTP API (Stage 6B.1)

```bash
python -m byteforge.trace_server --port 8765
```

This works from the repository root on Windows or Linux/WSL. The server binds
only IPv4 `127.0.0.1`, prints its local URL and exits on Ctrl+C. There is no
host option, frontend, repository file server or browser launch. Tests use
ephemeral loopback ports and close their servers. This is a local development
tool, not a public service.

### Transport and workspace contract

Every JSON response has `schema_version: 1` and `ok: true|false`. Successful
workspace responses include `session_id`, numeric `revision`, and `view`.
Before assembly these are `null`, `0`, and a `not_assembled` view with null CPU
and stdout. Revisions are monotonic across assembly replacement and trace
requests; a new successful assembly generates a fresh opaque session ID.
Clients must treat revisions as request preconditions, not execution counts.

Fetch `/api/bootstrap` to obtain `mutation_token`, current session ID/revision,
the seven existing `TraceLimits`, HTTP limits and supported capabilities.
Send the token as `X-ByteForge-Token` on every POST. Token lifetime is the Python
server process lifetime. Use `Content-Type: application/json` and an exact
Content-Length; compressed or chunked requests are unsupported.

Both assembly and trace POSTs require the currently observed `session_id` and
`revision`. First assembly uses `null` and `0`. Stale preconditions return 409
without mutation. Missing/malformed fields return 400. All requests are
serialized by one `HTTPServer` request loop; no worker mutates state in parallel.

Successful trace requests increment the revision even for no-ops, such as back
at the initial state or next after exit. Successful assembly replaces the old
session and clears timeline metadata. Invalid assembly leaves the previous
workspace and revision intact. After a network failure, fetch `/api/state` to
resynchronize rather than blindly retrying the old revision. Assembly metadata
is returned by successful assembly; clients retain it alongside its session ID.

### Routes

| Method and route | Request | Response fields beyond schema/ok |
|---|---|---|
| GET `/api/bootstrap` | no query | `mutation_token`, `session_id`, `revision`, `limits`, `http_limits`, `capabilities` |
| GET `/api/examples` | no query | `examples: [{name, filename, source}]` |
| POST `/api/assemble` | `{source, filename?, session_id, revision}` | workspace plus `assembly` report |
| POST `/api/trace` | `{operation, count?, session_id, revision}` | workspace plus `transition` (nullable step) |
| GET `/api/state` | no query | current workspace |
| GET `/api/timeline` | `session_id`, `revision`, optional `offset=0`, `limit=50` | context, `cursor`, `total`, `offset`, `events`, `next_offset` |
| GET `/api/memory` | context plus hex `address`, optional `length=16` | context plus `memory` |
| GET `/api/elf` | `session_id`, `revision` | exact current ELF bytes as an attachment, not JSON |

Context query parameters are required on timeline/memory/ELF to prevent stale
tabs from reading a replacement session's artifact. Unknown/duplicate fields
are rejected. There are no extra routes or static assets. Known routes with
wrong methods return 405; unknown routes return 404 and unknown HTTP verbs
return 501. Errors are JSON, including these method/route errors.

Example initial assembly request body:

```json
{
  "source": "_start: mov rax, 60\nmov rdi, 7\nsyscall",
  "filename": "exit.asm",
  "session_id": null,
  "revision": 0
}
```

`filename` is a display name only: at most 64 ASCII characters, starting with
an alphanumeric character, followed by letters/digits/underscore/dot/hyphen,
ending in `.asm`, and containing no `..`. It is never opened or written.
Missing filename defaults to `input.asm`. Source must be valid Unicode and
meet the existing UTF-8 byte/instruction limits.

Trace operations are `next`, `back`, `reset` and `continue`. Only continue
accepts `count`, a positive integer at most `max_continue_steps`; its default
is that limit. The controller calls the existing public `TraceSession.next()`
in a bounded loop. It records returned immutable transitions only when history
grows, without accessing private engine fields. Back/forward therefore retain
cached event identity; reset clears both engine history and event references.
CPU simulation remains entirely in the original trace engine.

Timeline pages contain `{index, visible, step}` events. Indices are zero-based;
`visible` means index < cursor. Recorded future events remain available after
back, matching cached forward playback. Pages are limited to 100 events.
`next_offset` is null at the end. Memory reads are limited to 256 bytes and
use the existing read-only DB inspection API, including its zero-length rule.
ELF downloads use `application/octet-stream` and fixed attachment filename
`byteforge.elf`; the server never executes or automatically saves them.

### Serialization schemas

`byteforge/report.py` is pure and independent of HTTP and HTML. The documented
three-argument `build_report(program, result, elf_layout)` interface is retained.
`assembly_report(trace_program)` extends it using the original source and
actual ELF header bytes. No parser, encoder or decoder is duplicated.

| Object | Fields and representation |
|---|---|
| Source location | `{filename, line, column, text}` or null; original text, 1-based positions |
| Bytes | `{hex, display, length}`; uppercase space-separated hex, escaped ASCII display, byte count |
| CPU | `registers`, `rip`, `zf`, `step_count`, `status`, `halt_reason`, `stdout_size`, `exit_status` |
| View | `execution_status`, `cpu`, `stdout`, `cursor`, `history_size`, `last_step` |
| Step | location, `address`, `bytes_hex`, `size`, `before`, `after`, `changed_registers`, `branch_taken`, `branch_target`, `executed`, `output`, `fault` |
| Instruction | address, file offset, size, actual bytes, mnemonic, operands, source location and symbol-resolution mapping |
| Symbol | `{name, address, file_offset}` sorted by name |
| Memory | `{address, file_offset, data, read_only: true}` |
| Error | `{phase, code, message, location}` |

All registers, virtual addresses, CPU step counts and stdout-size counters are
fixed-width `0x` + 16 lowercase hex digits. Symbol/branch targets and ELF entry,
segment virtual/physical addresses use the same representation. Immediate
operands include exact signed decimal `value` text plus hexadecimal `bits`.
Registers also retain name/code/width; symbol operands retain spelling.
No machine integer relies on JavaScript floating-point precision.

Byte lengths, file offsets, history/page indices, revisions, exit status and
small ELF fields are bounded ordinary JSON integers. ZF and branch decisions
are booleans; non-branch decisions/targets are null. `stdout.hex` is lossless
even for NUL/non-UTF-8 bytes; display escapes controls rather than sending raw
terminal controls. A future frontend must render source/display with
`textContent`, never interpret them as HTML.

The assembly report includes `source`, `source_lines`, `statement_locations`,
`code_base`, `data_base`, `code_size`, `data_size`, DB `data`, `instructions`,
`symbols`, and `elf`. ELF metadata includes entry, total size, actual program
segments, unpacked header fields and five regions: ELF header, program
headers, padding, code and data. Each region has a file offset, virtual address
and size. Instruction bytes come directly from `EncodedItem.data`; source and
symbols come from existing parsed/assembled models. File offsets use the real
segment mapping, not the entry address.

Execution statuses distinguish `not_assembled`, `ready`, `running`,
`halted_successfully`, `unsupported_operation`, `faulted`, and
`step_limit_reached`. Output/history limits retain their explicit CPU halt
reason under `faulted`. A valid trace request that encounters a simulated
fault returns HTTP 200 with the fault in its transition/view; malformed
requests return HTTP errors. Last-step register changes describe that saved
transition; consumers can compare states for their own back/reset highlights.

An invalid instruction response is, for example:

```json
{
  "schema_version": 1,
  "ok": false,
  "error": {
    "phase": "assembly",
    "code": "assembly_error",
    "message": "Unknown instruction: nop",
    "location": {
      "filename": "input.asm",
      "line": 1,
      "column": 9,
      "text": "_start: nop"
    }
  }
}
```

Phases are request, security, session, assembly, trace, memory and server.
Stale context is `409 / session / stale_workspace`. Before assembly, trace
and artifact reads return `409 / session / not_assembled`. Internal errors
return a generic 500 without exception paths or traceback text.

### Local security and resource boundary

- Exact loopback Host/port is required. If Origin is present it must exactly
  match the server URL; missing Origin permits terminal clients. Duplicate
  Host/Origin/token headers are rejected. Cross-site browser requests are
  rejected, and permissive CORS is never enabled.
- POST tokens, strict JSON object parsing, duplicate-key rejection, session
  preconditions and fixed route/example allowlists protect state changes.
- Body limit: 512 KiB (allowing JSON escapes around 64 KiB of source). Header
  block limit: 16 KiB; request-line limit: 2,048 bytes. GET bodies, chunking,
  compressed bodies and arbitrary uploads are unsupported.
- Each accepted connection has a three-second absolute deadline, socket
  timeout and connection shutdown timer, including slow headers/body reads.
  Assembly checks the deadline before workspace replacement. CPU work is
  bounded by source/instruction/continue limits; this is not hard CPU
  preemption of Python internals. A timeout may close the connection without
  delivering JSON. A response failure does not undo an accepted mutation.
- Existing source, instruction, step, history and output limits are unchanged.
  The server never invokes a subprocess, shell or native ELF. Examples are
  read only from the five fixed repository examples at startup; requests
  perform no filesystem reads or writes.
- Responses include no-store, nosniff, DENY framing, same-origin resource
  policy and restrictive CSP headers. Access logging is disabled; source,
  URLs and tokens are not logged by default.

The token is not authentication against other local processes. Single-request
serialization means a slow client can delay other clients until its short
deadline. No public deployment, TLS, multi-user workspaces, frontend/static
hosting or hard cancellation of already accepted CPU steps is provided.

### API validation

```bash
python -B -m unittest discover -s tests -p test_report.py
python -B -m unittest discover -s tests -p test_trace_server.py
python -B -m unittest discover -s tests
git diff --check
```

Tests use actual loopback connections on ephemeral ports, bounded request
timeouts, clean server shutdown, concurrent revision conflicts, known example
fixtures and real assembler/trace metadata. They never execute generated ELF.
