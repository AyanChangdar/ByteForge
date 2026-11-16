"""CLI and pipeline tests; execute only the fixed exit fixture on Linux x86-64."""

import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

from byteforge.elf import build_executable
from byteforge.model import ElfConfig
from byteforge.parser import parse
from byteforge.resolver import assemble


ROOT = Path(__file__).resolve().parents[1]
EXIT_SOURCE = "_start:\n    mov rax, 60\n    mov rdi, 7\n    syscall\n"
NATIVE_LINUX = sys.platform == "linux" and platform.machine().lower() in ("x86_64", "amd64")


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "exit.asm"
        self.output = self.directory / "nested" / "exit"
        self.source.write_text(EXIT_SOURCE, encoding="utf-8")

    def run_cli(self, source=None, output=None):
        return subprocess.run(
            [sys.executable, "-B", "-m", "byteforge.cli", str(source or self.source),
             "-o", str(output or self.output)],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )

    def test_cli_creates_directory_and_exact_executable(self):
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertIn("4118 bytes, entry 0x401000", process.stdout)
        self.assertTrue(self.output.is_file())
        config = ElfConfig()
        result = assemble(parse(EXIT_SOURCE, str(self.source)), 0x401000)
        expected, _ = build_executable(result, config)
        self.assertEqual(self.output.read_bytes(), expected)
        if os.name == "posix":
            self.assertTrue(self.output.stat().st_mode & stat.S_IXUSR)

    def test_cli_source_diagnostic_and_no_output(self):
        self.source.write_text("_start:\n    mov rax, 0xGG ; original\n", encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn(f"{self.source}:2:14:", process.stderr)
        self.assertIn("    mov rax, 0xGG ; original\n             ^", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.parent.exists())

    def test_cli_missing_entry_diagnostic(self):
        self.source.write_text("syscall", encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn(f"{self.source}:1:1:", process.stderr)
        self.assertIn("_start is missing", process.stderr)

    def test_cli_empty_source_diagnostic(self):
        self.source.write_text("", encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn(f"{self.source}:1:1:", process.stderr)

    def test_cli_rejects_data_and_symbolic_mov(self):
        for source, message in (('_start: syscall\ndb ""', "DB directives"),
                                ("_start: mov rsi, target", "integer immediate")):
            with self.subTest(source=source):
                self.source.write_text(source, encoding="utf-8")
                process = self.run_cli()
                self.assertEqual(process.returncode, 1)
                self.assertIn(message, process.stderr)
                self.assertFalse(self.output.exists())

    def test_cli_preserves_existing_output(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"existing work")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn("Refusing to overwrite", process.stderr)
        self.assertEqual(self.output.read_bytes(), b"existing work")

    def test_cli_preserves_source_when_paths_match(self):
        process = self.run_cli(output=self.source)
        self.assertEqual(process.returncode, 1)
        self.assertIn("differ from the source", process.stderr)
        self.assertEqual(self.source.read_text(encoding="utf-8"), EXIT_SOURCE)

    def test_cli_missing_input(self):
        process = self.run_cli(source=self.directory / "missing.asm")
        self.assertEqual(process.returncode, 1)
        self.assertIn("missing.asm", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_invalid_utf8(self):
        self.source.write_bytes(b"\xff")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn("byteforge:", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_output_directory_error(self):
        self.output.parent.write_bytes(b"not a directory")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("Traceback", process.stderr)
        self.assertEqual(self.output.parent.read_bytes(), b"not a directory")

    def test_cli_requires_output_argument(self):
        process = subprocess.run(
            [sys.executable, "-B", "-m", "byteforge.cli", str(self.source)],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("required", process.stderr)

    def test_cli_deterministic_across_output_paths(self):
        first = self.run_cli()
        second_path = self.directory / "second"
        second = self.run_cli(output=second_path)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.output.read_bytes(), second_path.read_bytes())

    def test_cli_entry_after_another_instruction(self):
        self.source.write_text("mov rdi, 0\n_start: syscall", encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(struct.unpack_from("<Q", self.output.read_bytes(), 24)[0], 0x40100A)

    def test_checked_in_exit_example_matches_fixture(self):
        source = (ROOT / "examples" / "exit.asm").read_text(encoding="utf-8")
        self.assertEqual(source, EXIT_SOURCE)

    @unittest.skipUnless(NATIVE_LINUX, "Native Linux execution not tested: requires Linux x86-64")
    def test_native_linux_exit_status_seven(self):
        # self.source contains the fixed fixture above, never arbitrary user input.
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        native = subprocess.run(
            [str(self.output)], cwd=self.directory, capture_output=True,
            timeout=5, check=False,
        )
        self.assertEqual(native.returncode, 7)
        self.assertEqual(native.stdout, b"")
        self.assertEqual(native.stderr, b"")


if __name__ == "__main__":
    unittest.main()
