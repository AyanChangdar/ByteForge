# ByteForge

ByteForge is an educational, handwritten x86-64 assembler and native Linux
ELF64 executable generator. It will translate a deliberately small assembly
language directly into machine-code bytes, resolve labels, and package the
result as a statically loadable executable without invoking NASM, GAS, GCC, or
an external linker.

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

The HTML/CSS/JavaScript visualizer will display source instructions, virtual
addresses, encoded bytes, resolved symbols, and the generated ELF layout. It
is a build-time explanation tool, not a CPU emulator.

## Planned usage

```bash
python -m byteforge.cli examples/hello.asm -o build/hello \
  --report build/hello.json
chmod +x build/hello
./build/hello
```

The generated `build/hello` file is intended to run on Linux x86-64 without
Python or an assembler toolchain. Open `visualizer/index.html` in a browser and
load `build/hello.json` to inspect the assembly result.

## Development

```bash
python -m unittest discover -s tests
```

See [Architecture](docs/ARCHITECTURE.md) for module boundaries and the binary
pipeline, and [Team Tasks](docs/TEAM_TASKS.md) for ownership and the three-day
schedule.

## Scope boundaries

ByteForge will not implement dynamic linking, object files, macros, complex
expressions, full x86-64 coverage, CPU emulation, KVM integration, or external
assembler/linker invocation. Linux x86-64 is the only execution target.
