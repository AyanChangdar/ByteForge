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
Reports and visualization remain planned.

The HTML/CSS/JavaScript visualizer will display source instructions, virtual
addresses, encoded bytes, resolved symbols, and the generated ELF layout. It
is a build-time explanation tool, not a CPU emulator.

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
expressions, full x86-64 coverage, CPU emulation, KVM integration, or external
assembler/linker invocation. Linux x86-64 is the only execution target.
The current assembler does not prove that arbitrary instruction sequences
terminate or use Linux syscalls correctly. Writable data, computed expressions,
additional arithmetic forms, other branch forms and the visualizer remain deferred.
