"""Command-line composition and file I/O for the minimal ELF pipeline."""

import argparse
import os
from pathlib import Path
import stat
import sys

from byteforge.elf import build_executable
from byteforge.model import ByteForgeError, ElfConfig
from byteforge.parser import parse
from byteforge.resolver import assemble


def main(argv: list[str] | None = None) -> int:
    arguments = argparse.ArgumentParser(description="Assemble the ByteForge subset into Linux ELF64")
    arguments.add_argument("source", type=Path, help="source assembly file")
    arguments.add_argument("-o", "--output", required=True, type=Path, help="new output executable")
    options = arguments.parse_args(argv)
    try:
        if options.source.resolve() == options.output.resolve():
            raise ValueError("Output path must differ from the source path")
        if options.output.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {options.output}")
        source = options.source.read_text(encoding="utf-8")
        program = parse(source, str(options.source))
        config = ElfConfig()
        result = assemble(program, config.image_base + config.code_offset,
                          filename=str(options.source))
        image, layout = build_executable(result, config)
        options.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also protects against a file appearing after validation.
        with options.output.open("xb") as output:
            output.write(image)
        if os.name == "posix":
            options.output.chmod(options.output.stat().st_mode | stat.S_IXUSR)
    except ByteForgeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError) as error:
        print(f"byteforge: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {options.output} ({layout.file_size} bytes, entry {layout.entry:#x})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
