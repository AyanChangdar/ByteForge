"""Tests for the minimal instruction layout and label pass."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from byteforge.model import ByteForgeError, SourceLocation
from byteforge.parser import parse
from byteforge.resolver import assemble


EXIT_SOURCE = "_start:\nmov rax, 60\nmov rdi, 7\nsyscall"


class ResolverTests(unittest.TestCase):
    def test_exit_code_and_layout(self):
        program = parse(EXIT_SOURCE, "exit.asm")
        result = assemble(program, 0x401000)
        self.assertEqual(result.code, bytes.fromhex(
            "48 B8 3C 00 00 00 00 00 00 00 48 BF 07 00 00 00 00 00 00 00 0F 05"
        ))
        self.assertEqual(result.data, b"")
        self.assertEqual(result.symbols, {"_start": 0x401000})
        self.assertEqual([item.address for item in result.items], [0x401000, 0x40100A, 0x401014])
        self.assertEqual([len(item.data) for item in result.items], [10, 10, 2])
        self.assertIs(result.items[0].statement, program.statements[1])
        self.assertEqual(result.items[0].resolution, {})

    def test_entry_after_other_instructions(self):
        result = assemble(parse("mov rdi, 1\n_start: syscall", "a.asm"), 0x401000)
        self.assertEqual(result.symbols["_start"], 0x40100A)

    def test_consecutive_and_trailing_labels(self):
        result = assemble(parse("alias:\n_start: syscall\nend:", "a.asm"), 100)
        self.assertEqual(result.symbols, {"alias": 100, "_start": 100, "end": 102})

    def test_missing_entry(self):
        program = parse("mov rax, 60", "a.asm")
        with self.assertRaises(ByteForgeError) as caught:
            assemble(program, 0x401000)
        self.assertIn("_start is missing", caught.exception.message)
        self.assertEqual(caught.exception.location, program.statements[0].location)

    def test_empty_program_has_filename(self):
        with self.assertRaises(ByteForgeError) as caught:
            assemble(parse("; empty", "empty.asm"), 0x401000, filename="empty.asm")
        self.assertEqual(caught.exception.location, SourceLocation("empty.asm", 1, 1, ""))

    def test_entry_is_case_sensitive(self):
        with self.assertRaisesRegex(ByteForgeError, "_start is missing"):
            assemble(parse("_Start: syscall", "a.asm"), 0x401000)

    def test_duplicate_labels_report_second_definition(self):
        for name in ("_start", "other"):
            with self.subTest(name=name):
                program = parse(f"_start: syscall\n{name}:\n{name}:", "dup.asm")
                with self.assertRaises(ByteForgeError) as caught:
                    assemble(program, 0x401000)
                expected_line = 2 if name == "_start" else 3
                self.assertEqual(caught.exception.location.line, expected_line)
                self.assertIn(f"Duplicate label: {name}", caught.exception.message)

    def test_entry_at_end_of_code(self):
        for source in ("syscall\n_start:", "_start:"):
            with self.subTest(source=source):
                program = parse(source, "end.asm")
                with self.assertRaises(ByteForgeError) as caught:
                    assemble(program, 0x401000)
                self.assertIn("end of code", caught.exception.message)
                self.assertEqual(caught.exception.location, program.statements[-1].location)

    def test_db_is_rejected_even_when_empty(self):
        for text in ('db "hello"', 'db ""', 'db 0'):
            with self.subTest(text=text):
                program = parse("_start: syscall\n" + text, "data.asm")
                with self.assertRaises(ByteForgeError) as caught:
                    assemble(program, 0x401000)
                self.assertIn("DB directives", caught.exception.message)
                self.assertEqual(caught.exception.location, program.statements[-1].location)

    def test_symbolic_mov_is_rejected_with_operand_location(self):
        program = parse("_start: mov rsi, target\ntarget: syscall", "a.asm")
        with self.assertRaises(ByteForgeError) as caught:
            assemble(program, 0x401000)
        self.assertEqual(caught.exception.location, program.statements[1].operands[1].location)
        self.assertIn("integer immediate", caught.exception.message)

    def test_unsupported_instruction_is_rejected(self):
        program = parse("_start: syscall", "a.asm")
        program.statements[1] = replace(program.statements[1], mnemonic="jmp")
        with self.assertRaisesRegex(ByteForgeError, "Unsupported instruction"):
            assemble(program, 0x401000)

    def test_immediate_error_is_preserved(self):
        program = parse(f"_start: mov rax, {1 << 64}", "overflow.asm")
        with self.assertRaises(ByteForgeError) as caught:
            assemble(program, 0x401000)
        self.assertEqual(caught.exception.location, program.statements[1].operands[1].location)
        self.assertIn("range", caught.exception.message)

    def test_size_mismatch_is_detected(self):
        program = parse("_start: syscall", "a.asm")
        with patch("byteforge.resolver.encode", return_value=b"\x0f"):
            with self.assertRaises(ByteForgeError) as caught:
                assemble(program, 0x401000)
        self.assertIn("size mismatch", caught.exception.message)
        self.assertEqual(caught.exception.location, program.statements[1].location)

    def test_encoder_receives_final_addresses_and_labels(self):
        program = parse("_start: syscall\nnext: syscall", "a.asm")
        from byteforge.encoder import encode
        with patch("byteforge.resolver.encode", wraps=encode) as encoder:
            result = assemble(program, 0x401000)
        self.assertEqual([call.args[1] for call in encoder.call_args_list], [0x401000, 0x401002])
        self.assertEqual(encoder.call_args_list[0].args[2], result.symbols)

    def test_address_bounds(self):
        program = parse("_start: syscall", "a.asm")
        for address in (-1, 1 << 64, (1 << 64) - 1):
            with self.subTest(address=address):
                with self.assertRaises(ByteForgeError):
                    assemble(program, address)

    def test_deterministic_without_modifying_program(self):
        program = parse(EXIT_SOURCE, "exit.asm")
        before = repr(program)
        self.assertEqual(assemble(program, 0x401000), assemble(program, 0x401000))
        self.assertEqual(repr(program), before)

    def test_no_file_io_or_printing(self):
        program = parse(EXIT_SOURCE, "exit.asm")
        with patch("builtins.open") as files, patch("sys.stdout") as stdout, patch("sys.stderr") as stderr:
            assemble(program, 0x401000)
            files.assert_not_called()
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
