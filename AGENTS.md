# ByteForge Agent Rules

## Project scope

ByteForge is a three-day educational project that hand-assembles a small,
documented x86-64 subset and writes a native Linux ELF64 executable. Keep every
change small enough to explain during a Computer Organization and Architecture
demonstration.

## Required constraints

- Use Python 3 standard library for assembler, resolver, CLI, ELF, and reports.
- Use HTML, CSS, and vanilla JavaScript for visualization.
- Use `unittest`; do not introduce a test framework dependency.
- Do not invoke NASM, GAS, GCC, `ld`, or another assembler/linker from product
  code. Development tests may use `readelf` or `objdump` only as optional
  validation tools.
- Emit fixed-address Linux x86-64 `ET_EXEC` files. Do not add dynamic linking,
  PIE, object-file generation, or additional executable formats unless the user
  explicitly expands scope.
- Do not add CPU emulation, KVM, a web framework, a GUI framework, databases,
  or package dependencies.
  Limited exception: TraceLab may provide a bounded educational simulator of
  ByteForge's existing typed instruction subset. This does not permit a general
  x86 emulator, machine-code decoding/execution, external VMs, or replacement
  of native ELF generation.
- Do not commit, push, rewrite history, or modify files outside this repository
  unless explicitly requested.

## Architecture rules

- Follow `docs/ARCHITECTURE.md` and keep shared contracts in `byteforge/model.py`.
- Keep parser, encoder, resolver, ELF writer, report generator, CLI, and
  visualizer responsibilities separate.
- Lower-level modules must not perform file I/O or print to the terminal.
- Keep the supported operand matrix explicit. Reject unsupported forms with a
  source-located diagnostic instead of guessing an encoding.
- Use deterministic fixed-size near `rel32` branches; do not implement branch
  relaxation in the initial scope.
- Keep `encoded_size()` and actual emitted byte lengths consistent and tested.
- Serialize visualization data from assembly metadata; do not disassemble the
  finished executable in the browser.

## Code and tests

- Prefer dataclasses, enums, type hints, descriptive names, and small pure
  functions. Avoid unnecessary abstraction and metaprogramming.
- Pack binary values with explicit little-endian `struct` formats.
- Raise project-specific exceptions carrying filename, line, column, and a
  useful message for user input errors.
- Add golden-byte tests for every instruction form and register family changed.
- Add forward and backward label tests for every branch form.
- Add structural tests for ELF fields, offsets, sizes, alignments, and entry.
- Skip native execution tests cleanly when not running on Linux x86-64.
- Preserve user changes and avoid unrelated formatting or refactoring.

## Validation

Run focused tests first, then the full suite:

```bash
python -m unittest discover -s tests
```

When Linux tools are available, optional checks are:

```bash
file build/hello
readelf -h -l build/hello
objdump -d -Mintel build/hello
```

These commands validate output but must never become runtime dependencies.

## Documentation

- Update the supported instruction table when adding or removing an operand form.
- Update CLI examples if arguments or output behavior change.
- Record deferred features as limitations rather than partially implementing
  them.
- Never claim a reference project or external source was reviewed unless it was
  actually accessed in the current task or its files are available locally.
