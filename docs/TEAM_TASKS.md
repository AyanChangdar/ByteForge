# ByteForge Team Tasks

## Working agreement

The team has three owners with narrow modules and shared contracts. Merge small
changes frequently. No developer should wait for another module to be complete:
use agreed dataclasses and fixture objects from day 1. Every supported encoding
form and every ELF field change must include a focused test.

## Module ownership

| Developer | Primary ownership | Deliverables |
|---|---|---|
| Developer 1: Front end | `model.py`, `parser.py`, `cli.py` | typed IR, parsing, diagnostics, CLI/file output |
| Developer 2: Assembler core | `encoder.py`, `resolver.py` | opcode/REX/ModR/M encoding, sizes, symbols, branches |
| Developer 3: Output and UI | `elf.py`, `report.py`, `visualizer/` | ELF64 image, report schema, static browser visualization |

Shared ownership covers integration tests, examples, README corrections, and
the final demonstration. Developers review changes that cross their interface;
the primary owner makes the final call within the frozen architecture.

## Frozen integration contracts

Agree on these before parallel work begins:

1. Shared dataclasses and exception/diagnostic shape in `model.py`.
2. Supported instruction-form matrix in `docs/ARCHITECTURE.md`.
3. Fixed image base, segment offset/alignment, and `_start` requirement.
4. `AssemblyResult` fields consumed by ELF and report modules.
5. Version 1 JSON report fields and sample fixture.

Use fixed-size near `rel32` branches. Do not add automatic short-branch
selection during the three-day build.

## Day 1: Contracts and vertical skeleton

### Developer 1

- Define shared model dataclasses and structured diagnostics.
- Implement line parsing for labels, comments, registers, decimal/hex numbers,
  instructions, and `db` string data.
- Add parser unit tests, including malformed input and quoted semicolons.
- Add a CLI skeleton that can parse input and report errors.

### Developer 2

- Freeze register codes and the supported operand-form table.
- Implement and test `MOV r64, imm64` and `SYSCALL` first.
- Define `encoded_size()` and prove it matches emitted lengths.
- Implement the two-pass layout skeleton with duplicate/undefined symbol errors.

### Developer 3

- Define the minimal fixed-address `ET_EXEC` layout and `ElfConfig`.
- Implement a fixture-driven ELF writer independently of the parser.
- Verify header/program-header values with `struct.unpack` tests.
- Define the versioned JSON report fixture and build the visualizer shell.

### Day 1 checkpoint

A hard-coded/fixture Hello World instruction stream can be wrapped as an ELF,
and the parser can represent the real Hello World source. Interfaces are frozen
before ending the day.

## Day 2: Complete core features and integrate

### Developer 1

- Finish operand validation and user-facing diagnostics.
- Complete CLI output paths, report option, and POSIX executable permissions.
- Write `hello.asm`, `loop.asm`, and `conditional.asm` with only approved forms.
- Add parser-to-model integration fixtures for all demos.

### Developer 2

- Add register-register and immediate forms for `ADD`, `SUB`, `XOR`, and `CMP`.
- Add fixed near forms for `JMP`, `JE`, and `JNE`.
- Complete forward/backward resolution and branch range checking.
- Add golden-byte and symbol-resolution tests for every supported form.

### Developer 3

- Connect real `AssemblyResult` data to the ELF writer.
- Implement report serialization for instructions, symbols, branches, and ELF.
- Build the instruction table, symbols panel, and ELF layout visualization.
- Add file-picker loading and clear invalid-report errors.

### Day 2 checkpoint

The CLI assembles all three examples. Hello World runs on Linux x86-64, branch
examples exit as expected, and their JSON reports load in the browser.

## Day 3: Stabilize and prepare demonstration

### Developer 1

- Test CLI failures, output behavior, and error source locations.
- Polish user documentation and command examples.
- Prepare the live demonstration script and backup screenshots/output.

### Developer 2

- Cross-check machine bytes with `objdump` as a development-only oracle.
- Fix only in-scope encoding/resolution defects.
- Help expand end-to-end tests around boundary values and invalid forms.

### Developer 3

- Validate ELF structure with `readelf` and `file` on Linux.
- Polish the static visualization for projector readability.
- Ensure the report schema and UI tolerate assembly diagnostics gracefully.

### Shared final gate

- Run `python -m unittest discover -s tests` from a clean checkout.
- Assemble all examples without NASM, GAS, GCC, or a linker.
- Run all generated programs on Linux x86-64 or WSL2.
- Inspect one executable with `readelf -h -l` and `objdump -d`.
- Rehearse source-to-bytes-to-ELF-to-native-execution in under five minutes.
- Tag nonessential bugs as known limitations instead of expanding scope.

## Integration order

1. Parser produces the shared `Program` model.
2. Resolver and encoder produce `AssemblyResult` from `Program`.
3. ELF writer produces bytes and `ElfLayout` from `AssemblyResult`.
4. Reporter combines all stage outputs into JSON.
5. CLI owns file I/O and invokes the pipeline.
6. Visualizer reads JSON only and never imports Python code.

## Deferred work

Do not schedule these before the final gate passes: memory operands, short-jump
relaxation, computed expressions, macros, sections/section headers, object
files, dynamic linking, PIE, function calls/stack conventions, floating point,
SIMD, emulation, debugging, KVM, or a web framework.

## Demonstration responsibilities

- Developer 1 explains syntax, parser output, diagnostics, and CLI usage.
- Developer 2 explains one REX/ModR/M encoding and a resolved branch displacement.
- Developer 3 explains ELF headers/segments and drives the visual report.
- Any developer can run the prepared Hello World and branch examples if another
  machine or presentation step fails.
