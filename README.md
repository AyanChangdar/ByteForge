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

Milestone 4 implements source parsing, instruction layout, label collection,
MOV-immediate/SYSCALL encoding, ELF generation and a CLI. A case-sensitive
`_start` label must identify an instruction. DB and symbolic MOV can be parsed
but are rejected during assembly. Arithmetic, branches, string printing,
reports and visualization remain planned.

The HTML/CSS/JavaScript visualizer will display source instructions, virtual
addresses, encoded bytes, resolved symbols, and the generated ELF layout. It
is a build-time explanation tool, not a CPU emulator.

## Usage (Milestone 4)

Python 3.10 or newer is required to build files; no packages are needed.
From the repository root:

```bash
python -m byteforge.cli examples/exit.asm -o build/exit
```

The CLI creates missing output directories and refuses to overwrite an
existing output or the source file. It sets owner-execute permission on POSIX.
Assembly errors show filename, line, column, original text and a caret.
Success returns status 0; build/file errors return 1; argument errors return 2.

The example is:

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
python -m unittest discover -s tests -p test_elf.py
python -m unittest discover -s tests -p test_integration.py
```

Structural and CLI tests run on Windows and Linux. The native test executes
only a fixed exit-status-7 fixture, with a timeout, on Linux x86-64. It is
explicitly skipped elsewhere; a skip does not verify native execution.

Optional development-only Linux checks:

```bash
file build/exit
readelf -h -l build/exit
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
terminate or use Linux syscalls correctly. Symbol-reference encoding and
data payloads are deferred to a later milestone.
