"""CLI tests; native tests execute only the fixed demonstration fixtures."""

import hashlib
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
HELLO_SOURCE = (
    '_start:\n    mov rax, 1\n    mov rdi, 1\n    mov rsi, msg\n'
    '    mov rdx, 14\n    syscall\n\n'
    '    mov rax, 60\n    mov rdi, 0\n    syscall\n\n'
    'msg: db "Hello, World!", 10\n'
)
ARITHMETIC_SOURCE = (
    "_start:\n    xor rdi, rdi\n    add rdi, 10\n    sub rdi, 3\n"
    "    cmp rdi, 7\n    mov rax, 60\n    syscall\n"
)
LOOP_SOURCE = (
    "_start:\n    mov rcx, 3\n\nloop:\n    sub rcx, 1\n    cmp rcx, 0\n"
    "    jne loop\n\n    mov rax, 60\n    mov rdi, 7\n    syscall\n"
)
CONDITIONAL_SOURCE = (
    "_start:\n    mov rcx, 0\n    mov rdi, 99\n    cmp rcx, 0\n"
    "    jne done             ; not taken\n    je equal             ; taken\n"
    "    jmp done\nequal:\n    mov rdi, 7\ndone:\n    mov rax, 60\n    syscall\n"
)
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

    def test_cli_builds_arithmetic_example(self):
        process = self.run_cli(source=ROOT / "examples" / "arithmetic.asm")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("4132 bytes, entry 0x401000", process.stdout)
        image = self.output.read_bytes()
        self.assertEqual(len(image), 4132)
        self.assertEqual(struct.unpack_from("<Q", image, 24)[0], 0x401000)
        self.assertEqual(struct.unpack_from("<QQ", image, 96), (4132, 4132))
        self.assertEqual(image[4096:], bytes.fromhex(
            "48 31 FF 48 81 C7 0A 00 00 00 48 81 EF 03 00 00 00 "
            "48 81 FF 07 00 00 00 48 B8 3C 00 00 00 00 00 00 00 0F 05"
        ))

    def test_checked_in_arithmetic_example_matches_fixture(self):
        self.assertEqual((ROOT / "examples" / "arithmetic.asm").read_text(encoding="utf-8"),
                         ARITHMETIC_SOURCE)

    def test_arithmetic_cli_is_deterministic(self):
        self.source.write_text(ARITHMETIC_SOURCE, encoding="utf-8")
        first = self.run_cli()
        other = self.directory / "arithmetic-second"
        second = self.run_cli(output=other)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.output.read_bytes(), other.read_bytes())

    def test_arithmetic_cli_range_diagnostic(self):
        self.source.write_text("_start:\n    add rdi, 2147483648 ; original", encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn(f"{self.source}:2:14:", process.stderr)
        self.assertIn("signed 32-bit", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_still_rejects_unsupported_instructions(self):
        for instruction in ("call target", "loop target", "nop"):
            with self.subTest(instruction=instruction):
                self.source.write_text("_start: " + instruction, encoding="utf-8")
                process = self.run_cli()
                self.assertEqual(process.returncode, 1)
                self.assertIn("Unknown instruction", process.stderr)
                self.assertFalse(self.output.exists())

    def test_existing_examples_complete_binary_regression(self):
        # Captured from the complete Milestone 5A images before arithmetic changes.
        expected = {
            "hello": (4174, "55410a57a5cf142e44835579df407658b25487a457378e893252f8ecfd12e867"),
            "exit": (4118, "954fe1ffdda4594e9c90f21d383e07632e93477a631d47338e59d5aef6d0c8ae"),
            "arithmetic": (4132, "218ac72a738d9d19cfc00b893f3da818fe3297bcbafaf960676b23ada13abb1f"),
        }
        for name, (size, digest) in expected.items():
            with self.subTest(example=name):
                output = self.directory / name
                process = self.run_cli(source=ROOT / "examples" / (name + ".asm"), output=output)
                self.assertEqual(process.returncode, 0, process.stderr)
                image = output.read_bytes()
                self.assertEqual(len(image), size)
                self.assertEqual(hashlib.sha256(image).hexdigest(), digest)
                self.assertEqual(struct.unpack_from("<Q", image, 24)[0], 0x401000)
                self.assertEqual(struct.unpack_from("<QQ", image, 96), (size, size))

    def test_cli_builds_loop_example_with_exact_bytes(self):
        process = self.run_cli(source=ROOT / "examples" / "loop.asm")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("4148 bytes, entry 0x401000", process.stdout)
        image = self.output.read_bytes()
        self.assertEqual(len(image), 4148)
        self.assertEqual(image[4096:], bytes.fromhex(
            "48 B9 03 00 00 00 00 00 00 00 48 81 E9 01 00 00 00 "
            "48 81 F9 00 00 00 00 0F 85 EC FF FF FF "
            "48 B8 3C 00 00 00 00 00 00 00 48 BF 07 00 00 00 00 00 00 00 0F 05"
        ))
        self.assertEqual(struct.unpack_from("<Q", image, 24)[0], 0x401000)
        self.assertEqual(struct.unpack_from("<QQ", image, 96), (4148, 4148))

    def test_cli_builds_conditional_example_with_exact_bytes(self):
        process = self.run_cli(source=ROOT / "examples" / "conditional.asm")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("4162 bytes, entry 0x401000", process.stdout)
        image = self.output.read_bytes()
        self.assertEqual(len(image), 4162)
        self.assertEqual(image[4096:], bytes.fromhex(
            "48 B9 00 00 00 00 00 00 00 00 48 BF 63 00 00 00 00 00 00 00 "
            "48 81 F9 00 00 00 00 0F 85 15 00 00 00 0F 84 05 00 00 00 E9 0A 00 00 00 "
            "48 BF 07 00 00 00 00 00 00 00 48 B8 3C 00 00 00 00 00 00 00 0F 05"
        ))
        self.assertEqual(struct.unpack_from("<Q", image, 24)[0], 0x401000)
        self.assertEqual(struct.unpack_from("<QQ", image, 96), (4162, 4162))

    def test_checked_in_branch_examples_match_trusted_fixtures(self):
        for name, fixture in (("loop", LOOP_SOURCE), ("conditional", CONDITIONAL_SOURCE)):
            with self.subTest(example=name):
                self.assertEqual((ROOT / "examples" / (name + ".asm")).read_text(encoding="utf-8"), fixture)

    def test_branch_cli_output_is_deterministic(self):
        for name, fixture in (("loop", LOOP_SOURCE), ("conditional", CONDITIONAL_SOURCE)):
            with self.subTest(example=name):
                self.source.write_text(fixture, encoding="utf-8")
                first = self.directory / (name + "-first")
                second = self.directory / (name + "-second")
                first_process = self.run_cli(output=first)
                second_process = self.run_cli(output=second)
                self.assertEqual(first_process.returncode, 0, first_process.stderr)
                self.assertEqual(second_process.returncode, 0, second_process.stderr)
                self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_cli_branch_error_diagnostics(self):
        for source, message in (
            ("_start: jmp missing", "Undefined symbol: missing"),
            ('_start: je target\nsyscall\ntarget: db "x"', "instruction boundary"),
            ('_start: jne target\nsyscall\ntarget: db ""', "instruction boundary"),
            ("_start: jmp target\nsyscall\ntarget:", "instruction boundary"),
            ("_start: jmp 123", "label identifier"),
        ):
            with self.subTest(source=source):
                self.source.write_text(source, encoding="utf-8")
                process = self.run_cli()
                self.assertEqual(process.returncode, 1)
                self.assertIn(message, process.stderr)
                self.assertIn(f"{self.source}:1:", process.stderr)
                self.assertNotIn("Traceback", process.stderr)
                self.assertFalse(self.output.exists())

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

    def test_cli_rejects_interleaving_and_undefined_symbols(self):
        for source, message in (('_start: syscall\ndb ""\nsyscall', "cannot follow a DB"),
                                ("_start: mov rsi, target", "Undefined symbol: target")):
            with self.subTest(source=source):
                self.source.write_text(source, encoding="utf-8")
                process = self.run_cli()
                self.assertEqual(process.returncode, 1)
                self.assertIn(message, process.stderr)
                self.assertFalse(self.output.exists())

    def test_cli_builds_hello_world(self):
        self.source.write_text(HELLO_SOURCE, encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("4174 bytes, entry 0x401000", process.stdout)
        image = self.output.read_bytes()
        self.assertEqual(len(image), 4174)
        self.assertEqual(image[0x1040:], b"Hello, World!\n")
        self.assertEqual(image[0x1014:0x101E], bytes.fromhex("48 BE 40 10 40 00 00 00 00 00"))
        self.assertEqual(struct.unpack_from("<Q", image, 24)[0], 0x401000)
        self.assertEqual(struct.unpack_from("<QQ", image, 96), (4174, 4174))

    def test_cli_undefined_symbol_has_precise_location(self):
        line = "    mov rsi, missing ; preserve source"
        self.source.write_text("_start:\n" + line, encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn(f"{self.source}:2:14: Undefined symbol: missing", process.stderr)
        self.assertIn(line + "\n             ^", process.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_hello_world_is_deterministic(self):
        self.source.write_text(HELLO_SOURCE, encoding="utf-8")
        first = self.run_cli()
        other = self.directory / "hello-second"
        second = self.run_cli(output=other)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.output.read_bytes(), other.read_bytes())

    def test_checked_in_hello_example_matches_fixture(self):
        source = (ROOT / "examples" / "hello.asm").read_text(encoding="utf-8")
        self.assertEqual(source, HELLO_SOURCE)

    def test_cli_accepts_empty_data(self):
        self.source.write_text(EXIT_SOURCE + 'empty: db ""\n', encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(self.output.read_bytes()), 4118)

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

    @unittest.skipUnless(NATIVE_LINUX, "Native Linux execution not tested: requires Linux x86-64")
    def test_native_linux_hello_world(self):
        # Only this known fixture is executed, never arbitrary user assembly.
        self.source.write_text(HELLO_SOURCE, encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        native = subprocess.run(
            [str(self.output)], cwd=self.directory, capture_output=True,
            timeout=5, check=False,
        )
        self.assertEqual(native.stdout, b"Hello, World!\n")
        self.assertEqual(native.returncode, 0)
        self.assertEqual(native.stderr, b"")

    @unittest.skipUnless(NATIVE_LINUX, "Native Linux execution not tested: requires Linux x86-64")
    def test_native_linux_arithmetic_exit_status_seven(self):
        # Execute only the fixed arithmetic fixture, never arbitrary user input.
        self.source.write_text(ARITHMETIC_SOURCE, encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        native = subprocess.run(
            [str(self.output)], cwd=self.directory, capture_output=True,
            timeout=5, check=False,
        )
        self.assertEqual(native.returncode, 7)
        self.assertEqual(native.stdout, b"")
        self.assertEqual(native.stderr, b"")

    @unittest.skipUnless(NATIVE_LINUX, "Native Linux execution not tested: requires Linux x86-64")
    def test_native_linux_loop_exit_status_seven(self):
        self.source.write_text(LOOP_SOURCE, encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        native = subprocess.run([str(self.output)], cwd=self.directory, capture_output=True,
                                timeout=5, check=False)
        self.assertEqual(native.returncode, 7)
        self.assertEqual(native.stdout, b"")
        self.assertEqual(native.stderr, b"")

    @unittest.skipUnless(NATIVE_LINUX, "Native Linux execution not tested: requires Linux x86-64")
    def test_native_linux_conditional_exit_status_seven(self):
        self.source.write_text(CONDITIONAL_SOURCE, encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        native = subprocess.run([str(self.output)], cwd=self.directory, capture_output=True,
                                timeout=5, check=False)
        self.assertEqual(native.returncode, 7)
        self.assertEqual(native.stdout, b"")
        self.assertEqual(native.stderr, b"")


if __name__ == "__main__":
    unittest.main()
