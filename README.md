# ByteForge

ByteForge is an educational, handwritten x86-64 assembler and native Linux
ELF64 executable generator. It translates a deliberately small assembly
language directly into machine-code bytes and packages the result as a
fixed-address executable without invoking NASM, GAS, GCC, or an external linker.

The project is designed for a three-person, three-day Computer Organization
and Architecture demonstration. Its scope is intentionally limited to a
documented subset of `MOV`, `ADD`, `SUB`, `XOR`, `CMP`, `JMP`, `JE`, `JNE`, and
`SYSCALL`, essential 64-bit registers, decimal/hexadecimal immediates, labels,
comments, and a string-data directive.

## Planned pipeline

```text
source.asm -> parser -> symbol/layout passes -> x86-64 encoder
           -> machine code + data -> ELF64 writer -> native executable
                                    -> JSON report -> HTML visualizer
```

Stage 5B.2 implements source parsing, two-pass code/data layout, label
collection, MOV-immediate/symbol, fixed-imm32 ADD/SUB/CMP, register XOR,
JMP/JE/JNE rel32 branches, SYSCALL encoding, ELF generation and a CLI.
A case-sensitive `_start` label must identify an instruction. DB bytes follow
instructions; any instruction after the first DB (even `db ""`) is rejected.
Structured reports and the browser TraceLab Playground are implemented.

The HTML/CSS/JavaScript Playground displays source, virtual addresses, actual
encoded bytes, symbols, read-only DB data and simulated CPU execution. The
browser and terminal debugger share the same bounded Python trace engine;
neither runs the generated ELF. Detailed opcode and ELF exploration remain
deferred to Stage 6C.

## Usage (Stage 5B.2)

Python 3.10 or newer is required to build files; no packages are needed.
From the repository root:

```bash
python -m byteforge.cli examples/exit.asm -o build/exit
python -m byteforge.cli examples/hello.asm -o build/hello
python -m byteforge.cli examples/arithmetic.asm -o build/arithmetic
python -m byteforge.cli examples/loop.asm -o build/loop
python -m byteforge.cli examples/conditional.asm -o build/conditional
```

The CLI creates missing output directories and refuses to overwrite an
existing output or the source file. It sets owner-execute permission on POSIX.
Assembly errors show filename, line, column, original text and a caret.
Success returns status 0; build/file errors return 1; argument errors return 2.

The exit-status-7 regression example is:

```asm
_start:
    mov rax, 60
    mov rdi, 7
    syscall
```

The resulting file is 4118 bytes, with 22 code bytes at file offset `0x1000`
and entry address `0x401000`. It has one read/execute load segment at fixed
image base `0x400000`. No Python runtime or dynamic loader is required to run it.

Run on Linux x86-64 (including compatible WSL):

```bash
chmod +x build/exit
./build/exit
echo $?                 # expected: 7
```

The Hello World example uses `mov rsi, msg` to load an absolute data address
and `msg: db "Hello, World!", 10` to define 14 bytes. Its ELF contains 64 code
bytes followed immediately by those bytes at file offset `0x1040` / virtual
address `0x401040`. File size is 4174 bytes and entry remains `0x401000`.

```bash
chmod +x build/hello
./build/hello           # prints Hello, World! followed by a newline
echo $?                # expected: 0
```

Labels preserve case. Undefined symbols and duplicate labels produce source
diagnostics. Numeric MOV retains its original `-2^63..2^64-1` range; symbolic
MOV uses unsigned 64-bit virtual addresses. Both encodings are always 10 bytes.
Strings are ASCII with the documented escape set. Data has no implicit NUL
terminator, and syscall byte counts remain literal values in source.

The arithmetic example computes exit status 7 without using branches:

```asm
_start:
    xor rdi, rdi
    add rdi, 10
    sub rdi, 3
    cmp rdi, 7
    mov rax, 60
    syscall
```

Its ELF is 4132 bytes, with 36 code bytes and entry `0x401000`. On Linux x86-64:

```bash
chmod +x build/arithmetic
./build/arithmetic
echo $?                 # expected: 7
```

ADD, SUB and CMP accept only a base 64-bit register and a signed 32-bit integer
(`-2147483648..2147483647`). They always use seven-byte `48 81` encodings;
the CPU sign-extends the immediate, and there is no imm8 optimization.
XOR accepts two base registers and uses three-byte `48 31` encoding, with
the first operand as destination. Other arithmetic forms, memory operands,
extended registers and indirect branches remain unsupported. CMP affects flags
only; JE branches when ZF is set, and JNE branches when ZF is clear.

JMP, JE and JNE accept one case-sensitive code label. They always use fixed
near rel32 forms (5, 6 and 6 bytes) with displacement calculated from the next
instruction. Forward/backward references and instruction aliases are supported.
Undefined labels, data targets, trailing end-of-code targets and non-instruction
boundaries produce source diagnostics. Symbolic MOV may still reference data.

`loop.asm` counts RCX from 3 to zero with SUB/CMP/JNE and exits with status 7.
Its backward JNE is `0F 85 EC FF FF FF` (displacement -20); its ELF is 4148
bytes. `conditional.asm` demonstrates a non-taken JNE and taken JE, selecting
exit status 7 instead of failure status 99; its ELF is 4162 bytes.

On Linux x86-64, after generating the files:

```bash
chmod +x build/loop build/conditional
./build/loop
echo $?                 # expected: 7
./build/conditional
echo $?                 # expected: 7
```

There is no short-jump optimization, branch relaxation or support for numeric,
register, memory or expression branch targets. Branching does not change the
existing instruction-before-data restriction.

Windows can generate the same ELF file but cannot execute it directly. Copy
it to Linux/WSL and apply executable permissions there if necessary. The CLI
only builds files; it never runs generated programs.

## TraceLab terminal debugger (Stage 6A.2)

On Windows PowerShell or Linux/WSL, from the repository root:

```bash
python -m byteforge.trace_cli examples/loop.asm --interactive
python -m byteforge.trace_cli examples/hello.asm --interactive
```

Use `next`/`n`, `back`, `registers`, `memory ADDRESS [LENGTH]`,
`continue`/`c [STEPS]`, `reset`, `help`, and `quit`/`q`. For Hello World,
`memory 0x401040 14` inspects the DB bytes, and `continue` captures exactly
`b"Hello, World!\n"` with simulated exit status 0. The loop and conditional
examples simulate exit status 7. Debugger quit/EOF returns 0 independently of
that simulated status; Ctrl+C returns 130.

This is **SIMULATED CPU STATE**, starting with zero registers and ZF=false,
not Linux process-entry registers or hardware inspection. Only ByteForge's
existing typed instructions and modeled stdout write/exit syscalls are traced.
Successful syscalls set RCX to the following instruction address; ZF is
preserved, and R11/full RFLAGS are not modeled. Other syscalls, descriptors and
unmapped memory accesses fault explicitly. Captured output is displayed as a
byte string; the engine does not print or invoke OS syscalls.

Back restores registers, RIP, flags, halt/exit state and visible output.
Forward reuses cached history; reset clears history and output. Defaults cap
source/output at 64 KiB each, instructions at 2,048, executed steps/history
transitions at 10,000, continue requests at 100 steps and memory inspections
at 256 bytes. The browser is available below; detailed opcode inspection is deferred.
See [TraceLab contracts](docs/TRACELAB.md) for exact limits and semantics.

## TraceLab Playground and local API (Stage 6B.2)

```bash
python -m byteforge.trace_server --port 8765
```

Open **http://127.0.0.1:8765** in your browser. The server binds only to loopback
and serves four allowlisted visualizer files, never the repository directory.
Stop with Ctrl+C. Python's standard library
is sufficient on Windows and Linux/WSL; no generated native binary is run.

Fetch `GET /api/bootstrap` for schema version 1, the process mutation token,
limits and current session/revision. Send `X-ByteForge-Token` and JSON bodies
on POSTs. Initial `POST /api/assemble` accepts source, a display filename,
`session_id: null` and `revision: 0`. Subsequent assembly/trace requests must
send the current session ID and revision; stale requests return 409.

Available routes are `GET /api/bootstrap`, `GET /api/examples`,
`POST /api/assemble`, `POST /api/trace`, `GET /api/state`, `GET /api/timeline`,
`GET /api/memory`, and `GET /api/elf`. Trace supports next/back/reset/bounded
continue over the same engine as the terminal debugger. Timeline, DB memory
and ELF reads require current session/revision query parameters. ELF is an
optional download only. Source/bytes/output remain data, never executed HTML.

Machine values and addresses use exact hex strings; binary output includes
lossless hex and an escaped display string. The server enforces exact local
Host/Origin, POST tokens, existing trace limits, bounded requests/pages and
short connection deadlines. It does not enable CORS or launch subprocesses.
This is for local development, not public hosting. See the
[endpoint schemas and security boundary](docs/TRACELAB.md#local-http-api-stage-6b1)
before building another client.

The single-page site has Home, Playground, Instruction Set and CLI Guide.
Load an example, click Assemble, then Step or Play. Back restores saved state
and output; Reset clears history. Play never overlaps mutation requests, and
Pause prevents the next scheduled step (an in-flight step may finish).
The editor shows the next instruction in teal and the last executed line in
amber. Selecting a machine-byte row inspects it without changing execution.

Editing source or changing examples clears the old displayed trace and
requires reassembly. Stale revisions stop playback and refresh server context;
reassemble to bind the editor again. Source text, bytes, state, output and
timeline all come from the Python API. Binary stdout retains its exact hex
alongside a UTF-8 or escaped preview. All rendered source/output is safe text.

Keyboard shortcuts: Ctrl+Enter Assemble, F10 Step, Shift+F10 Back, F9
Play/Pause. Inspector tabs also support arrow keys, Home and End. The layout
stacks on small screens. No Node/npm, build step, external font or CDN is used.
The [browser acceptance checklist](docs/TRACELAB.md#browser-acceptance-checklist)
documents the main flows. Advanced opcode fields and the ELF explorer remain
Stage 6C work.

## Development

```bash
python -m unittest discover -s tests
```

Focused checks for this milestone:

```bash
python -m unittest discover -s tests -p test_resolver.py
python -m unittest discover -s tests -p test_parser.py
python -m unittest discover -s tests -p test_encoder.py
python -m unittest discover -s tests -p test_elf.py
python -m unittest discover -s tests -p test_integration.py
python -m unittest discover -s tests -p test_trace.py
python -m unittest discover -s tests -p test_trace_cli.py
python -m unittest discover -s tests -p test_report.py
python -m unittest discover -s tests -p test_trace_server.py
```

Structural and CLI tests run on Windows and Linux. Native tests execute only
the fixed exit-status-7, Hello World, arithmetic, loop and conditional fixtures,
with timeouts, on Linux
x86-64. They are explicitly skipped elsewhere; a skip does not verify native
execution. The Hello World test checks exact stdout `b"Hello, World!\n"`
and return code 0; the original native exit test still checks return code 7.
Arithmetic, loop and conditional native tests check return code 7. Regression
tests pin the complete Hello World, exit and arithmetic ELF hashes captured
before adding branches.

Optional development-only Linux checks:

```bash
file build/exit
readelf -h -l build/exit
file build/hello
readelf -h -l build/hello
```

The ELF has no section table, so plain `objdump -d` may not display its code.
None of these external validation tools are build or runtime dependencies.

See [Architecture](docs/ARCHITECTURE.md) for module boundaries and the binary
pipeline, and [Team Tasks](docs/TEAM_TASKS.md) for ownership and the three-day
schedule.

## Scope boundaries

ByteForge will not implement dynamic linking, object files, macros, complex
expressions, full x86-64 coverage, general-purpose CPU emulation, KVM integration, or external
assembler/linker invocation. Linux x86-64 is the only execution target.
The current assembler does not prove that arbitrary instruction sequences
terminate or use Linux syscalls correctly. Writable data, computed expressions,
additional arithmetic forms, other branch forms and advanced opcode/ELF
inspectors remain deferred.
