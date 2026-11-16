"""Tests for the minimal instruction layout and label pass."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from byteforge.model import ByteForgeError, SourceLocation
from byteforge.parser import parse
from byteforge.resolver import assemble


EXIT_SOURCE = "_start:\nmov rax, 60\nmov rdi, 7\nsyscall"


class ResolverTests(unittest.TestCase):
    def test_arithmetic_layout_and_exact_bytes(self):
        source = ("_start:\nxor rdi, rdi\nadd rdi, 10\nsub rdi, 3\n"
                  "cmp rdi, 7\nmov rax, 60\nsyscall")
        result = assemble(parse(source, "arithmetic.asm"), 0x401000)
        self.assertEqual(result.code, bytes.fromhex(
            "48 31 FF 48 81 C7 0A 00 00 00 48 81 EF 03 00 00 00 "
            "48 81 FF 07 00 00 00 48 B8 3C 00 00 00 00 00 00 00 0F 05"
        ))
        self.assertEqual(len(result.code), 36)
        self.assertEqual([item.address for item in result.items],
                         [0x401000, 0x401003, 0x40100A, 0x401011, 0x401018, 0x401022])
        self.assertEqual([len(item.data) for item in result.items], [3, 7, 7, 7, 10, 2])
        self.assertEqual(result.symbols, {"_start": 0x401000})
        self.assertEqual(result.data, b"")

    def test_arithmetic_before_symbolic_mov_and_data(self):
        source = '_start: add rax, -1\nxor rcx, rdx\nmov rsi, msg\nsyscall\nmsg: db "x"'
        result = assemble(parse(source, "a.asm"), 0x401000)
        self.assertEqual(result.symbols["msg"], 0x401016)
        self.assertEqual(len(result.code), 22)
        self.assertEqual(result.data, b"x")
        self.assertEqual(result.items[2].data, bytes.fromhex("48 BE 16 10 40 00 00 00 00 00"))
        self.assertEqual(result.items[2].resolution, {"msg": 0x401016})

    def test_arithmetic_range_error_retains_operand_location(self):
        program = parse("_start: add rax, 2147483648", "a.asm")
        with self.assertRaises(ByteForgeError) as caught:
            assemble(program, 0x401000)
        self.assertIn("signed 32-bit", caught.exception.message)
        self.assertEqual(caught.exception.location, program.statements[1].operands[1].location)

    def test_arithmetic_cannot_follow_data(self):
        for instruction in ("add rax, 1", "sub rax, 1", "cmp rax, 0", "xor rax, rbx"):
            with self.subTest(instruction=instruction):
                program = parse('_start: syscall\ndb ""\n' + instruction, "a.asm")
                with self.assertRaisesRegex(ByteForgeError, "cannot follow a DB"):
                    assemble(program, 0x401000)

    def test_arithmetic_size_mismatch_is_detected(self):
        program = parse("_start: add rax, 1", "a.asm")
        with patch("byteforge.resolver.encode", return_value=b"\x48\x81\xc0"):
            with self.assertRaisesRegex(ByteForgeError, "size mismatch"):
                assemble(program, 0x401000)

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

    def test_db_bytes_are_collected_including_empty_strings(self):
        for text, expected in (('db "hello"', b"hello"), ('db ""', b""), ('db 0', b"\x00")):
            with self.subTest(text=text):
                program = parse("_start: syscall\n" + text, "data.asm")
                result = assemble(program, 0x401000)
                self.assertEqual(result.code, b"\x0f\x05")
                self.assertEqual(result.data, expected)

    def test_forward_code_label_reference(self):
        program = parse("_start: mov rsi, target\ntarget: syscall", "a.asm")
        result = assemble(program, 0x401000)
        self.assertEqual(result.symbols["target"], 0x40100A)
        self.assertEqual(result.code[:10], bytes.fromhex("48 BE 0A 10 40 00 00 00 00 00"))
        self.assertEqual(result.items[0].resolution, {"target": 0x40100A})

    def test_undefined_symbol_has_operand_location(self):
        program = parse("_start: mov rsi, missing", "a.asm")
        with self.assertRaises(ByteForgeError) as caught:
            assemble(program, 0x401000)
        self.assertEqual(caught.exception.location, program.statements[1].operands[1].location)
        self.assertIn("Undefined symbol: missing", caught.exception.message)

    def test_forward_data_reference_and_separate_label(self):
        program = parse('_start: mov rsi, msg\nsyscall\nmsg:\ndb "Hi", 10', "a.asm")
        result = assemble(program, 0x401000)
        self.assertEqual(len(result.code), 12)
        self.assertEqual(result.data, b"Hi\n")
        self.assertEqual(result.symbols, {"_start": 0x401000, "msg": 0x40100C})
        self.assertEqual(result.code[:10], bytes.fromhex("48 BE 0C 10 40 00 00 00 00 00"))
        self.assertEqual(result.items[0].resolution, {"msg": 0x40100C})

    def test_multiple_data_directives_and_aliases(self):
        source = '_start: syscall\nfirst:\nalias: db "AB"\nempty: db ""\nsecond: db 0, 255\nend:'
        result = assemble(parse(source, "data.asm"), 100)
        self.assertEqual(result.data, b"AB\x00\xff")
        self.assertEqual(result.symbols, {"_start": 100, "first": 102, "alias": 102,
                                         "empty": 104, "second": 104, "end": 106})

    def test_instructions_after_data_are_rejected_even_after_empty_db(self):
        for directive in ('db "x"', 'db ""'):
            with self.subTest(directive=directive):
                program = parse(f"_start: syscall\n{directive}\nmov rax, 60", "a.asm")
                with self.assertRaises(ByteForgeError) as caught:
                    assemble(program, 0x401000)
                self.assertIn("cannot follow a DB", caught.exception.message)
                self.assertEqual(caught.exception.location, program.statements[-1].location)

    def test_data_before_instruction_region_is_rejected(self):
        program = parse('db "x"\n_start: syscall', "a.asm")
        with self.assertRaisesRegex(ByteForgeError, "cannot follow a DB"):
            assemble(program, 0x401000)

    def test_start_in_data_is_rejected(self):
        program = parse('syscall\n_start: db "x"', "a.asm")
        with self.assertRaises(ByteForgeError) as caught:
            assemble(program, 0x401000)
        self.assertEqual(caught.exception.location, program.statements[1].location)
        self.assertIn("must identify an instruction", caught.exception.message)

    def test_duplicate_symbols_across_code_and_data(self):
        for source in ('_start: syscall\n_start: db "x"',
                       '_start: syscall\nmsg: db "x"\nmsg: db "y"'):
            with self.subTest(source=source):
                program = parse(source, "a.asm")
                with self.assertRaisesRegex(ByteForgeError, "Duplicate label"):
                    assemble(program, 0x401000)

    def test_data_symbol_names_are_case_sensitive(self):
        result = assemble(parse('_start: syscall\nmsg: db 1\nMsg: db 2', "a.asm"), 100)
        self.assertEqual(result.symbols["msg"], 102)
        self.assertEqual(result.symbols["Msg"], 103)
        with self.assertRaisesRegex(ByteForgeError, "Undefined symbol: MSG"):
            assemble(parse('_start: mov rsi, MSG\nmsg: db 1', "a.asm"), 100)

    def test_data_overflow_and_one_past_end_label(self):
        for source in ('_start: syscall\ndb "AB"', '_start: syscall\nend:'):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ByteForgeError, "64-bit address range"):
                    assemble(parse(source, "a.asm"), (1 << 64) - 2)

    def test_data_assembly_is_deterministic(self):
        program = parse('_start: mov rsi, msg\nsyscall\nmsg: db "x"', "a.asm")
        before = repr(program)
        self.assertEqual(assemble(program, 100), assemble(program, 100))
        self.assertEqual(repr(program), before)

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
