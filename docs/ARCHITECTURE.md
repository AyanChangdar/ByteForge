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

The diagnostic model, minimal parser and minimal instruction encoder are
implemented. The rest of this tree describes planned components.

## 3. Shared data model and interfaces

`model.py` is the contract between modules. Use dataclasses and enums rather
than passing loosely structured dictionaries through the assembler core.

Implemented parser types (all frozen dataclasses):

```python
SourceLocation(filename: str, line: int, column: int, text: str)
Register(name: str, code: int, width: int, location: SourceLocation)
Immediate(value: int, location: SourceLocation)
SymbolReference(name: str, location: SourceLocation)
Instruction(mnemonic: str,
            operands: tuple[Register | Immediate | SymbolReference, ...],
            location: SourceLocation)
Label(name: str, location: SourceLocation)
DataDirective(data: bytes, location: SourceLocation)
Program(statements: list[Instruction | Label | DataDirective])
```

`Program` preserves source statement order. Its field cannot be reassigned,
but its list remains mutable. A labeled instruction or directive produces a
separate `Label` followed by that statement; data directives never own labels.
Operands retain their own locations for later encoder/resolver diagnostics.
Register codes are `rax=0`, `rcx=1`, `rdx=2`, `rbx=3`, `rsp=4`, `rbp=5`,
`rsi=6`, `rdi=7`, with width 64.

Planned downstream types:

```python
EncodedItem(address: int, data: bytes, statement, resolution: dict)
AssemblyResult(code: bytes, data: bytes, symbols: dict, items: list)
ElfLayout(entry: int, segments: list, file_size: int)
```

The implemented diagnostic contract is `SourceLocation` and
`ByteForgeError(location: SourceLocation, message: str)` in `model.py`.
`SourceLocation` is an immutable dataclass. Its `text` field preserves the
original source line, including whitespace, without the line terminator.
Callers supply one-based line and column numbers; columns count Python string
characters, not terminal display cells.

`ByteForgeError` exposes `location` and `message`. Calling `str(error)` returns:

```text
filename:line:column: message
original source line
    ^
```

The caret uses `column - 1` characters of padding. For defensive formatting,
columns below 1 place it at the start, and columns beyond the line place it
immediately after the last character. Empty lines place it at the start.
The stored location and header values are never clamped or rewritten. Tabs
are preserved in caret padding; other preceding characters become spaces.
Terminal display widths for Unicode characters are not calculated.
Neither class reads or writes files or prints output. The parser will attach
locations to errors; the CLI will eventually catch and display them.

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

`parse(source: str, filename: str) -> Program` scans one line at a time,
recognizes quoted strings before interpreting `;` comments, and produces typed
statements without file I/O or printing. LF, CRLF and CR line endings are
accepted. Original lines, including whitespace and comments, are retained in
source locations without line terminators. Mnemonic/directive locations point
to their first character; labels and operands point to their own first
characters. Missing input is marked immediately after the last syntactic token,
before trailing whitespace or a comment. Parsing stops at the first
`ByteForgeError`.

Implemented Milestone 2 grammar:

```text
line       := [label ":"] [instruction | data] [";" comment]
instruction:= "mov" register "," (integer | identifier) | "syscall"
data       := "db" value ["," value ...]
value      := string | integer
integer    := ["+" | "-"] (decimal_digits | "0x" hexadecimal_digits)
```

Mnemonics and registers are case-insensitive and normalize to lowercase;
hexadecimal prefixes may also be `0X`. Decimal literals with leading zeros
remain decimal. Labels and symbol references match `[A-Za-z_][A-Za-z0-9_]*`
and preserve case. Only one label is allowed per line. References remain
unresolved; duplicate/undefined symbols and the required `_start` entry are
deferred to assembly. The parser does not require an entry point.

MOV destinations must be one of the eight supported registers. Supported
registers in source position are rejected because register-to-register MOV is
deferred. Unsupported general-purpose register spellings (`eax`, `ax`, `al`,
`r8` through `r15` and their width variants, and instruction-pointer names)
are reserved and rejected in operand positions. Other identifiers, including
`raq` or `r8_buffer`, are permitted as symbolic sources. MOV immediate values
retain their integer value; encoding-width validation belongs to the encoder.
Python's integer conversion limits may reject exceptionally large literals.

DB accepts a nonempty comma-separated sequence of double-quoted ASCII strings
and numeric bytes in `0..255`. Empty strings are allowed. Strings support only
`\\`, `\"`, `\n`, `\r`, `\t`, and `\0` escapes. Unknown escapes,
non-ASCII characters, unterminated strings and invalid numeric bytes are errors.
Commas, colons and semicolons inside strings are literal characters.

Missing/extra operands, missing commas, malformed labels/numbers, unsupported
instructions/registers and incorrect operand combinations raise source-located
diagnostics. Expressions, memory operands, register-to-register MOV,
single-quoted/multiline strings, sections and macros are unsupported. No
addresses, instruction sizes or machine code are calculated by the parser.

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

Keep the encoding matrix explicit and small. The parser accepts
`MOV r64, integer/symbol` and `SYSCALL`. Milestone 3 encodes only these forms:

| Instruction | Operand form | Bytes | Size |
|---|---|---|---|
| `MOV` | base `r64, integer` | `48`, `B8 + register_code`, little-endian imm64 | 10 |
| `SYSCALL` | no operands | `0F 05` | 2 |

Symbolic MOV remains parser-only and is rejected by the encoder, even when
the supplied symbol mapping contains its name. The complete target matrix is:

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
width/range checks, and little-endian packing. The minimal implementation uses
a small form-size table keyed by `(mnemonic, operand kinds)`, shared validation
and direct encoding functions. Extend this with dedicated handlers as forms
are added; do not introduce a generic x86 description language.

Implemented public interfaces remain:

```python
encoded_size(instruction: Instruction) -> int
encode(instruction: Instruction, address: int,
       symbols: Mapping[str, int]) -> bytes
```

Both functions validate the form, operand count/types, base-register
name/code/width consistency and immediate range. They consume the parser's
normalized lowercase models. Invalid operands use their own source location;
unsupported mnemonics or wrong operand counts use the instruction location.
Errors are `ByteForgeError`, with no file I/O or printing. Address and symbol
arguments are retained for future forms but are unused in Milestone 3.

MOV accepts integers from `-2^63` through `2^64-1` inclusive. Negative values
use their 64-bit two's-complement representation. Values are range-checked
before masking and packed with `struct.pack("<Q", ...)`; out-of-range values
are rejected rather than truncated. Noninteger values in constructed
`Immediate` models are rejected. MOV always uses the ten-byte form, including
for zero or small immediates. SYSCALL is always two bytes.

Current private helpers are `_base_register_code(register)` for register
validation, `_rex(w, r=0, x=0, b=0)` for prefix composition, and
`_pack_imm64(value, location)` for validated little-endian immediate bytes.
`_imm64_bits(value, location)` shares range validation between sizing and
packing. REX is `0x40 | W<<3 | R<<2 | X<<1 | B`; current MOV sets only W.
The encoder checks register metadata independently of the parser, so manually
constructed mismatched registers cannot silently select another destination.

Future helper designs (not implemented in this milestone):

- `_modrm_register(reg_code, rm_code)` will compose register-only ModR/M:
  `0xC0 | (reg_code << 3) | rm_code`, after base-register validation. Opcode
  handlers will explicitly choose register direction or the opcode extension.
- `_pack_signed(value, bits, location)` will check signed ranges and use
  explicit little-endian formats for arithmetic immediates and displacements.
- `_rel32(target, address, instruction_size, location)` will calculate
  `target - (address + instruction_size)`, require signed 32-bit range and
  pack with `<i`. JMP remains five bytes, JE/JNE six; no branch relaxation.

Later symbolic MOV and branch handlers can consume the existing
`SymbolReference`, address and symbol mapping without changing public
interfaces. Sizing must accept unresolved references once those forms are
introduced. Arithmetic/register forms fit the existing operand models, but
will require approved parser extensions. The future resolver will own layout
and symbols; the ELF writer will consume assembled bytes downstream. No
additional instruction, symbol lookup or branch logic is implemented yet.

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
