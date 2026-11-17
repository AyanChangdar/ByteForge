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
|   |-- arithmetic.asm    # implemented arithmetic exit-status-7 demo
|   |-- exit.asm          # implemented exit-status-7 demo
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

Stage 5B.2 adds fixed-width JMP, JE and JNE to the existing MOV, arithmetic,
XOR and SYSCALL pipeline. Loop and conditional examples supplement the exit,
Hello World and arithmetic demos. Code/data layout and the fixed ELF format
are preserved. Reports and visualization remain planned.

Stage 6A.1 adds `byteforge/trace_model.py`, `byteforge/trace.py` and
`tests/test_trace.py` without changing any assembler interfaces or output.
`prepare_trace(source, filename, limits)` reuses parse, assemble and ELF
generation, then copies instruction-address and symbol lookups from the actual
metadata. `initial_state(program)` uses the resolved entry, and
`step(program, state)` simulates at most one supported typed instruction.
Trace-only contracts live in `trace_model.py`; assembler contracts remain in
`model.py`. States are immutable with read-only register mappings. Registers
start at zero and ZF starts false; these are educational values, not Linux
process-entry registers. Source/instruction/step limits bound execution.
SYSCALL returns an explicit unsupported fault: stdout, exit semantics, reverse
history, a trace CLI and the web interface are not implemented in this stage.
See [TraceLab contracts and semantics](TRACELAB.md) for exact state, stepping,
address-validation and fault behavior. Earlier static-report proposals remain
unimplemented and do not describe the current trace engine.

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

Implemented downstream contracts (also frozen dataclasses):

```python
EncodedItem(address: int, data: bytes, statement: Instruction,
            resolution: dict[str, int])
AssemblyResult(code: bytes, data: bytes, symbols: dict[str, int],
               items: list[EncodedItem])
ElfConfig(image_base: int = 0x400000, code_offset: int = 0x1000,
          alignment: int = 0x1000)
ElfSegment(p_type: int, p_flags: int, p_offset: int, p_vaddr: int,
           p_paddr: int, p_filesz: int, p_memsz: int, p_align: int)
ElfLayout(entry: int, segments: list[ElfSegment], file_size: int)
```

Each encoded item retains its original instruction and location. Symbolic MOV
records `{symbol_name: absolute_address}` in its resolution metadata; numeric
instructions retain an empty dictionary. Items describe instructions only.
`AssemblyResult.data` holds concatenated DB bytes separately from code.
Frozen result objects contain ordinary mutable collections, which callers
should treat as read-only. ElfConfig names
the fixed layout; the writer rejects configurations other than approved defaults.

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
- `resolver.assemble(program: Program, base_address: int,
  *, filename: str = "<input>") -> AssemblyResult`
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

Implemented Stage 5B.2 grammar:

```text
line       := [label ":"] [instruction | data] [";" comment]
instruction:= "mov" register "," (integer | identifier) | "syscall"
            | ("add" | "sub" | "cmp") register "," integer
            | "xor" register "," register
            | ("jmp" | "je" | "jne") identifier
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

ADD, SUB and CMP require a base 64-bit register destination and an integer
source. XOR requires two base 64-bit registers, with destination first.
The parser rejects arithmetic symbols/register sources, XOR immediates,
memory operands, unsupported registers and wrong operand counts. Encoding-width
range checks remain in the encoder. Existing quoting, comments, labels, DB,
numeric literal rules and source-location conventions are unchanged. JMP,
JE and JNE require exactly one label identifier and preserve its spelling.
Numeric, register, string, expression and memory branch targets are rejected.
Register spellings are reserved in branch operand position, as in symbolic MOV.

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
each instruction's exact size, records instruction addresses and collects
labels at the current address cursor. `base_address` means the virtual address
of the first code byte, not the ELF image base. The first DB starts the data
region, even for `db ""`. Every later instruction is rejected at its source
location; code/data interleaving is never reordered. DB bytes advance the
cursor by their exact length, without padding or an implicit terminator.
Every duplicate label is an error at the second definition. `_start` remains
case-sensitive, required, and must identify a code instruction boundary;
an entry in data or at end-of-code is rejected.

Symbol values are virtual addresses. The CLI passes
`ElfConfig.image_base + ElfConfig.code_offset` as the code base:

```text
code base = image base + code file offset
code symbol address = code base + instruction offset
data symbol address = code base + total code size + data offset
```

For a missing `_start`, the diagnostic uses the first statement's location.
An empty program uses `filename:1:1` and empty source text; the CLI supplies
the input filename through the optional keyword. Separate and consecutive
labels bind to the same current cursor as inline labels. Trailing labels may
mark one-past-end positions, but `_start` must identify an instruction. Code,
data and label addresses are checked for 64-bit overflow. Bare `db` is still
invalid syntax; empty data uses the existing `db ""` form.

### Stage 3: Encode and resolve references

Pass 2 calls the existing `encode(instruction, address, symbols)` with the
final addresses and label table. Every emitted length must match its planned
size; a mismatch raises a source-located `ByteForgeError`. No file I/O or
printing occurs. Symbolic MOV looks up the case-sensitive symbol name and
encodes its absolute address using the same ten-byte form as numeric MOV.
Undefined symbols raise `ByteForgeError` at the symbol operand's location.
`AssemblyResult.items` preserves code bytes and source/resolution metadata;
`code` and `data` remain separate payloads.

After pass 1, the resolver constructs the set of planned instruction-start
addresses and uses it for entry and branch validation. Before encoding each
JMP/JE/JNE, it checks that the case-sensitive target exists and belongs to that
set. Code aliases are allowed; targets in DB data (including empty directives),
at end-of-code or inside an instruction are rejected at the operand location.
Instructions after DB remain unsupported. Symbolic MOV still accepts code or
data addresses, including addresses that are not valid branch destinations.

Branch displacement is `target - (instruction_address + fixed_size)`.
Forward/backward references use the completed table; no instruction size or
address changes during pass 2. Existing size-mismatch checks remain in force.
Symbol expressions, indirect branches and branch relaxation remain unsupported.

### Stage 4: Build the ELF64 executable

`elf.py` builds bytes in memory using explicit little-endian struct formats:
`<16sHHIQQQIHHHHHH` (64-byte ELF header) and `<IIQQQQQQ` (56-byte program
header). The single PT_LOAD maps the entire file, including headers and padding.

```text
+------------------------+ file offset 0
| ELF64 header           | 64 bytes
+------------------------+
| Program header         | 56 bytes, PT_LOAD
+------------------------+
| padding/alignment      |
+------------------------+ code file offset 0x1000
| encoded instructions   | entry points to _start
+------------------------+
| DB bytes               | immediately after code
+------------------------+
```

The fixed image base is `0x400000`; code starts at virtual address `0x401000`.
The header identity is ELFCLASS64, little endian, current version, System V
ABI. Type is ET_EXEC (2), machine EM_X86_64 (62), version 1 and flags 0.
`e_phoff=64`, `e_ehsize=64`, `e_phentsize=56`, `e_phnum=1`. All section-header
fields are zero. There is no interpreter or dynamic-linking metadata.

The segment has `p_type=1`, `p_flags=5` (read/execute), `p_offset=0`,
`p_vaddr=p_paddr=0x400000`, `p_filesz=p_memsz=len(image)`, and `p_align=0x1000`.
Virtual address and file offset are congruent modulo alignment. Padding from
file offset 120 to 4096 is zero. `_start` supplies `e_entry`; it is not assumed
to be the first instruction. The exit example has entry `0x401000`, 22 code
bytes and a 4118-byte file. The Hello World example has 64 code bytes followed
by `b"Hello, World!\n"` (14 bytes): `msg=0x401040`, data file offset `0x1040`,
and file/segment size 4174 bytes. Both examples enter at `0x401000`.
File size is `code_offset + len(code) + len(data)`; memory size equals it.

The writer validates struct sizes, the fixed layout/alignment, assembly item
addresses, payload consistency, total length, 64-bit bounds and entry range
and instruction boundary. Invalid internal configuration/result contracts
raise `ValueError`; source errors use `ByteForgeError` earlier in the pipeline.
The writer performs no file I/O or printing.

The CLI uses `python -m byteforge.cli examples/exit.asm -o build/exit` and
composes parse, assemble and build_executable. It reads UTF-8 input and builds
the complete image before creating output directories and exclusively creating
the output file. Existing output and input/output path equality are rejected.
It adds owner-execute permission on POSIX, displays a byte count and entry on
success, and displays structured source diagnostics or file errors on failure.
Exit codes are 0 for success, 1 for build/file errors and 2 for argument errors.
It never runs generated programs. On Windows the file must be copied to Linux
or compatible WSL and given executable permission there as needed.

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

Keep the encoding matrix explicit and small. Stage 5B.2 supports exactly
the following instruction forms, using only the eight base 64-bit registers:

| Instruction | Operand form | Bytes | Size |
|---|---|---|---|
| `MOV` | base `r64, integer` | `48`, `B8 + register_code`, little-endian imm64 | 10 |
| `MOV` | base `r64, symbol` | same form, with the resolved absolute virtual address | 10 |
| `SYSCALL` | no operands | `0F 05` | 2 |
| `ADD` | base `r64, signed imm32` | `48 81`, ModR/M `/0`, imm32 | 7 |
| `SUB` | base `r64, signed imm32` | `48 81`, ModR/M `/5`, imm32 | 7 |
| `CMP` | base `r64, signed imm32` | `48 81`, ModR/M `/7`, imm32 | 7 |
| `XOR` | base `r64, r64` | `48 31`, register-direct ModR/M | 3 |
| `JMP` | label | `E9`, signed little-endian rel32 | 5 |
| `JE` | label | `0F 84`, signed little-endian rel32 | 6 |
| `JNE` | label | `0F 85`, signed little-endian rel32 | 6 |

DB is supported only after instructions. Arithmetic imm8 optimizations,
register-register ADD/SUB/CMP, register-register MOV, XOR immediates, indirect
branches and other branch mnemonics are unsupported. The broader target matrix below includes deferred
forms and must not be read as the implemented subset:

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
    mov rsi, msg        ; fixed virtual address of data label
    mov rdx, 14         ; byte count
    syscall
    mov rax, 60         ; Linux exit syscall
    mov rdi, 0          ; status
    syscall

msg: db "Hello, World!", 10
```

This avoids memory-addressing modes and linker relocations. `msg` is
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
Errors are `ByteForgeError`, with no file I/O or printing. `encoded_size()`
accepts unresolved symbols and returns 10 without a symbol lookup, enabling
forward references. `encode()` resolves symbolic operands through the supplied
mapping. Absolute symbol values must be integers in `0..2^64-1`; negative,
overflowing and noninteger mapped values are rejected at the symbol location.
For JMP/JE/JNE, sizing accepts unresolved symbols and returns the fixed size
without lookup. Encoding uses the actual instruction address and final symbol
mapping to compute the next-instruction-relative displacement. Out-of-range
signed rel32 values raise `ByteForgeError` at the target operand. The encoder
also rejects invalid unsigned target addresses and invalid instruction-address
ranges. Direct `encode()` callers supply addresses only; instruction-boundary
validation requires the complete program and belongs to the resolver.

MOV accepts integers from `-2^63` through `2^64-1` inclusive. Negative values
use their 64-bit two's-complement representation. Values are range-checked
before masking and packed with `struct.pack("<Q", ...)`; out-of-range values
are rejected rather than truncated. Noninteger values in constructed
`Immediate` models are rejected. MOV always uses the ten-byte form, including
for zero or small immediates. SYSCALL is always two bytes.

ADD/SUB/CMP always use Group 1 opcode `81`, REX.W and a signed imm32 packed
with `<i`. Accept exactly `-2147483648..2147483647`; the CPU sign-extends the
immediate to 64 bits. Positive `0xFFFFFFFF` is rejected while `-1` is accepted.
ModR/M is `0xC0 | (extension << 3) | destination_code`, where ADD is `/0`,
SUB `/5` and CMP `/7`. No imm8 or accumulator-specific encodings are selected.
Lengths remain seven bytes for every register and immediate value.

XOR uses `48 31` and ModR/M `0xC0 | (source_code << 3) | destination_code`.
Opcode 31 writes the r/m operand: the first assembly operand is the destination
and the second occupies reg. Thus `xor rax, rbx` is `48 31 D8`; reversing the
operands gives different bytes and changes the destination. XOR is three bytes.
All register metadata is validated by both sizing and encoding. The arithmetic
demo is 36 code bytes (4132 ELF bytes), enters at `0x401000` and computes exit
status 7. CMP changes flags only; JE tests ZF=1 and JNE tests ZF=0.

Branch encodings contain no REX prefix. `_rel32(target, address, size, location)`
checks `-2147483648..2147483647` and packs the displacement using `<i`.
The emitted branch length is checked against the form-size table.
At `0x401000` targeting `0x401010`, JMP is `E9 0B 00 00 00`, JE is
`0F 84 0A 00 00 00` and JNE is `0F 85 0A 00 00 00`. No short forms are selected.

The loop demo decrements RCX from 3, compares it with zero and branches back.
Its JNE at `0x401018` targets `0x40100A`: displacement
`0x40100A - 0x40101E = -20`, bytes `0F 85 EC FF FF FF`. It has 52 code bytes
and a 4148-byte ELF. The conditional demo uses a non-taken JNE followed by a
taken JE; success selects exit status 7 instead of the failure status 99.
It has 66 code bytes and a 4162-byte ELF. Both enter at `0x401000`.

Current private helpers are `_base_register_code(register)` for register
validation, `_rex(w, r=0, x=0, b=0)` for prefix composition, and
`_pack_imm64(value, location)` for validated little-endian immediate bytes.
`_imm64_bits(value, location)` shares range validation between sizing and
packing. REX is `0x40 | W<<3 | R<<2 | X<<1 | B`; current MOV sets only W.
`_modrm_register(reg_code, rm_code)` composes register-only ModR/M from validated
codes/opcode extensions. `_pack_imm32(value, location)` validates and packs a
signed 32-bit value for arithmetic; shared validation calls it during sizing
as well. The encoder checks register metadata independently of the parser, so manually
constructed mismatched registers cannot silently select another destination.

Future helper designs (not implemented in this milestone):

- `_pack_signed(value, bits, location)` will check signed ranges and use
  explicit little-endian formats if additional signed widths become necessary.

Symbolic MOV uses the existing `SymbolReference` and symbol mapping without
changing public interfaces. Branch handlers use the existing address and
mapping arguments and the existing SymbolReference model. The resolver owns
instruction/data layout, label collection and branch-boundary validation;
the unchanged ELF writer consumes the separate payloads. No additional
executable format or instruction family is implemented.

Important fixed choices:

- register-to-register operations use 64-bit operand size (`REX.W=1`);
- `MOV r64, imm64` uses `REX.W + B8+rd + imm64`;
- immediate arithmetic uses fixed seven-byte `81` forms; size decisions
  are identical in `encoded_size()` and `encode()`;
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
