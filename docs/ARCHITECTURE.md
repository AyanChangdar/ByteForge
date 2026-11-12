# ByteForge Architecture

## 1. Design goals

ByteForge is a small educational assembler, not a general-purpose toolchain.
The architecture prioritizes a working native Linux x86-64 executable, clear
intermediate results for visualization, deterministic tests, and module
boundaries that three developers can implement independently in three days.

The core uses only Python 3 and its standard library. The browser UI uses only
HTML, CSS, and vanilla JavaScript. Generated executables contain machine code
and data only; they do not need Python or any assembler, compiler, linker,
runtime, or dynamic loader.

## 2. Proposed repository structure

```text
ByteForge/
|-- AGENTS.md
|-- README.md
|-- docs/
|   |-- ARCHITECTURE.md
|   `-- TEAM_TASKS.md
|-- byteforge/
|   |-- __init__.py
|   |-- cli.py             # arguments, file I/O, diagnostics
|   |-- model.py           # shared dataclasses and operand types
|   |-- parser.py          # source text -> parsed statements
|   |-- encoder.py         # instruction forms -> x86-64 bytes
|   |-- resolver.py        # layout, symbols, branch displacements
|   |-- elf.py             # bytes/data -> ELF64 ET_EXEC image
|   `-- report.py          # assembly result -> JSON-safe report
|-- visualizer/
|   |-- index.html
|   |-- styles.css
|   `-- app.js
|-- examples/
|   |-- hello.asm
|   |-- loop.asm
|   `-- conditional.asm
|-- tests/
|   |-- test_parser.py
|   |-- test_encoder.py
|   |-- test_resolver.py
|   |-- test_elf.py
|   `-- test_integration.py
`-- build/                 # ignored generated artifacts
```

Only the documentation files exist at the architecture stage. Implementation
folders should be added after architecture review.

## 3. Shared data model and interfaces

`model.py` is the contract between modules. Use dataclasses and enums rather
than passing loosely structured dictionaries through the assembler core.

Suggested types:

```python
SourceLocation(line: int, column: int, text: str)
Register(name: str, code: int, width: int)
Immediate(value: int)
SymbolRef(name: str)
StringLiteral(value: bytes)
Instruction(mnemonic: str, operands: tuple, location: SourceLocation)
Label(name: str, location: SourceLocation)
DataDirective(label: str | None, data: bytes, location: SourceLocation)
Program(statements: list)
EncodedItem(address: int, data: bytes, statement, resolution: dict)
AssemblyResult(code: bytes, data: bytes, symbols: dict, items: list)
ElfLayout(entry: int, segments: list, file_size: int)
```

The concrete fields may be refined during implementation, but module
ownership and return directions should remain stable:

- `parser.parse(source: str, filename: str) -> Program`
- `encoder.encoded_size(instruction: Instruction) -> int`
- `encoder.encode(instruction: Instruction, address: int,
  symbols: Mapping[str, int]) -> bytes`
- `resolver.assemble(program: Program, base_address: int) -> AssemblyResult`
- `elf.build_executable(result: AssemblyResult,
  config: ElfConfig) -> tuple[bytes, ElfLayout]`
- `report.build_report(program, result, elf_layout) -> dict`

The CLI is the composition root. Lower-level modules must not read files,
print output, parse command-line arguments, or import the visualizer.

## 4. Assembly-to-ELF pipeline

### Stage 1: Parse and validate syntax

The parser reads one line at a time, strips `;` comments outside quoted
strings, recognizes optional labels, and converts instructions and data
directives into typed statements. Mnemonics and register names are normalized
to lowercase. Integers use decimal or `0x` hexadecimal syntax, with an
optional leading minus sign where the selected encoding permits it.

Recommended grammar for the first version:

```text
line       := [label ":"] [instruction | data] [";" comment]
instruction:= mnemonic [operand ["," operand]]
operand    := register | integer | identifier
data       := "db" string ["," integer ...]
```

Labels use `[A-Za-z_][A-Za-z0-9_]*`. Reject duplicate labels, malformed
strings, unsupported mnemonics, wrong operand counts, unknown registers, and
out-of-range immediates with filename, line, column, and source context.

### Stage 2: Layout and collect symbols

The resolver performs pass 1 over the parsed program. It asks the encoder for
each instruction's exact size and assigns offsets to instructions, labels, and
data. To keep layout deterministic, all `JMP`, `JE`, and `JNE` instructions use
their near `rel32` forms; ByteForge does not relax them into short branches.

The first implementation should place code followed by read-only string data
inside one loadable segment. This avoids section-table complexity and still
produces a valid native executable. Symbol values are final virtual addresses:

```text
symbol address = image base + load segment file offset + item offset
```

Pass 1 fails on duplicate definitions. References may remain unresolved until
pass 2, allowing forward branches and forward references to data.

### Stage 3: Encode and resolve references

Pass 2 encodes every instruction with the final symbol table.

- A near branch uses `disp32 = target - address_after_instruction`.
- A symbolic `MOV r64, label` uses the resolved 64-bit virtual address as its
  immediate. This is valid because the first version emits fixed-address
  `ET_EXEC`, not PIE.
- Missing symbols and values outside their encoding range are errors.

`AssemblyResult.items` retains source location, virtual address, bytes, and
resolution details so the visualizer does not need to reverse-engineer the
binary.

### Stage 4: Build the ELF64 executable

`elf.py` constructs the file with `struct.pack` using explicit little-endian
formats. The minimum image is:

```text
+------------------------+ file offset 0
| ELF64 header           | 64 bytes
+------------------------+
| Program header         | 56 bytes, PT_LOAD
+------------------------+
| padding/alignment      |
+------------------------+ load segment offset
| encoded instructions   | entry points to _start
| string data            |
+------------------------+
```

Required ELF properties are `ELFCLASS64`, little endian, System V ABI,
`ET_EXEC`, `EM_X86_64`, and one `PT_LOAD` segment. The segment can be `PF_R |
PF_X` for the demonstration because strings are read-only. No section-header
table is required for execution. File offset and virtual address congruence
must satisfy the selected `p_align` (normally `0x1000`). `_start` is required
and becomes `e_entry`.

The CLI writes the completed bytes and applies executable permission bits on
POSIX. On Windows it still creates the ELF file, which must be copied to Linux
or WSL for execution.

### Stage 5: Generate the visualization report

`report.py` serializes a versioned JSON document containing:

- source lines and diagnostics;
- each instruction's source text, address, size, and hexadecimal bytes;
- labels and resolved virtual addresses;
- branch source, target, and signed displacement;
- ELF header fields, segment offsets, flags, sizes, and entry point.

The static visualizer loads this JSON through a file picker. It displays an
instruction table, register names used by the program, symbol/branch cards,
and an ELF layout diagram. It does not execute or emulate instructions.

## 5. Supported instruction forms

Keep the encoding matrix explicit and small. The minimum planned forms are:

| Instruction | Operand form | Purpose |
|---|---|---|
| `MOV` | `r64, imm64` | syscall numbers, arguments, symbol addresses |
| `MOV` | `r64, r64` | useful register transfer for demos |
| `ADD` | `r64, imm8/imm32` | loop/counter arithmetic |
| `ADD` | `r64, r64` | required arithmetic subset |
| `SUB` | `r64, imm8/imm32` | decrement counters |
| `SUB` | `r64, r64` | required arithmetic subset |
| `XOR` | `r64, r64` | zeroing and bitwise demo |
| `CMP` | `r64, imm8/imm32` | conditional demo |
| `CMP` | `r64, r64` | register comparison |
| `JMP` | `rel32 label` | unconditional control flow |
| `JE` | `rel32 label` | branch when equal |
| `JNE` | `rel32 label` | loop/branch when not equal |
| `SYSCALL` | no operands | enter the Linux kernel |

Start with `RAX`, `RCX`, `RDX`, `RBX`, `RSP`, `RBP`, `RSI`, and `RDI`.
Supporting `R8`-`R15` is a small extension only after base-register REX and
ModR/M tests pass. Memory operands are not required for the three demos and
should be deferred.

### Minimum Hello World combinations

A working Hello World needs only `MOV r64, imm64`, `SYSCALL`, labels, and `db`:

```asm
_start:
    mov rax, 1          ; Linux write syscall
    mov rdi, 1          ; stdout
    mov rsi, message    ; fixed virtual address of data label
    mov rdx, 13         ; byte count
    syscall
    mov rax, 60         ; Linux exit syscall
    mov rdi, 0          ; status
    syscall

message: db "Hello World!", 10
```

This avoids memory-addressing modes and linker relocations. `message` is
resolved to an absolute `imm64` in the fixed-address executable. The string
length is a literal in version one; computed expressions such as
`message_end - message` are explicitly deferred.

## 6. Instruction encoding boundaries

The encoder owns opcode selection, REX prefixes, ModR/M bytes, immediate
width/range checks, and little-endian packing. It should use a small dispatch
table keyed by `(mnemonic, operand kinds)` and dedicated helpers, not a generic
x86 description language.

Important fixed choices:

- register-to-register operations use 64-bit operand size (`REX.W=1`);
- `MOV r64, imm64` uses `REX.W + B8+rd + imm64`;
- immediate arithmetic prefers a documented deterministic form; size decisions
  must be identical in `encoded_size()` and `encode()`;
- `JMP` uses `E9 rel32`; `JE` uses `0F 84 rel32`; `JNE` uses `0F 85 rel32`;
- `SYSCALL` uses `0F 05`;
- all multibyte values are little endian.

## 7. Dependencies and validation tools

Runtime/build dependencies for ByteForge itself:

- Python 3.10 or newer, standard library only;
- a modern browser for the static visualizer;
- Linux x86-64 or WSL2 to execute generated files.

Development-only validation tools may include `readelf`, `objdump`, `xxd`, and
`file` on Linux. They verify output during development but are not invoked by
ByteForge and are not required by generated executables. Tests use
`unittest`; subprocess execution tests must skip cleanly off Linux x86-64.

## 8. Integration risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Parser and encoder disagree on operand types | Late integration failures | Freeze `model.py` types and the form table on day 1 |
| Pass 1 size differs from pass 2 encoding | Wrong labels and branches | Fixed near branches; test every form's reported size |
| Branch displacement uses wrong origin | Jumps land at invalid bytes | Always subtract address after the instruction; test forward/backward cases |
| REX/ModR/M bit placement is wrong | Invalid or unintended instructions | Golden byte tests for every supported form/register |
| ELF offsets and virtual addresses diverge | Loader rejects or crashes | One segment, fixed base, one alignment rule, `readelf` integration tests |
| Absolute data address conflicts with PIE/ASLR | Hello World crashes | Emit fixed-address `ET_EXEC`; explicitly do not claim PIE support |
| String escapes or comments are parsed incorrectly | Corrupt demo output | Support a tiny documented escape set and test semicolons inside strings |
| Browser cannot fetch local JSON | Visualization demo fails | Use an HTML file input and `FileReader`, not `fetch(file://...)` |
| Windows executable-bit behavior differs | Demo setup confusion | Generate on any OS; run and chmod in Linux/WSL instructions |
| Scope expansion consumes schedule | Core demo remains incomplete | Treat all non-matrix instructions, memory operands, sections, and emulation as deferred |

## 9. Reference-project lessons

[AmmAsm](https://github.com/LinuxCoder13/AmmAsm) demonstrates that direct
x86-64 encoding, two-pass labels, inline data, relative control flow, and ELF
output can form one handwritten assembler pipeline. ByteForge borrows those
architectural ideas only; it excludes AmmAsm's broad ISA, expressions, macros,
PIE, relocatable objects, SIMD/APX, and cross-format output.

[x86Lab](https://github.com/JBouron/x86Lab) demonstrates the educational value
of presenting machine code alongside CPU-oriented information. ByteForge uses
that presentation principle for a static assembly report, but does not use
x86Lab's NASM dependency, KVM virtual machine, execution stepping, or native UI
stack.

## 10. Definition of done

The architecture is successfully implemented when all three examples assemble,
the output passes structural ELF checks, Hello World runs on Linux x86-64, both
forward and backward labels work, invalid input produces source diagnostics,
the report opens in the static visualizer, and the complete `unittest` suite
passes without requiring external assembler or linker commands.
