"""Focused tests for the minimal assembly parser."""

from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from byteforge.model import (
    ByteForgeError, DataDirective, Immediate, Instruction, Label, Program,
    Register, SourceLocation, SymbolReference,
)
from byteforge.parser import parse


class ParserTests(unittest.TestCase):
    def instruction(self, source):
        return parse(source, "test.asm").statements[0]

    def assert_invalid(self, source, message, column=None):
        with self.assertRaises(ByteForgeError) as caught:
            parse(source, "bad.asm")
        error = caught.exception
        self.assertIn(message, error.message)
        self.assertEqual(error.location.filename, "bad.asm")
        self.assertEqual(error.location.line, 1)
        self.assertEqual(error.location.text, source)
        if column is not None:
            self.assertEqual(error.location.column, column)

    def test_minimal_linux_exit_program(self):
        program = parse("_start:\n    mov rax, 60\n    mov rdi, 0\n    syscall\n", "exit.asm")
        self.assertIsInstance(program, Program)
        self.assertEqual(len(program.statements), 4)
        label, first, second, syscall = program.statements
        self.assertEqual(label, Label("_start", SourceLocation("exit.asm", 1, 1, "_start:")))
        self.assertEqual(first.mnemonic, "mov")
        self.assertEqual(first.operands[1].value, 60)
        self.assertEqual(second.operands[0].name, "rdi")
        self.assertEqual(second.operands[1].value, 0)
        self.assertEqual(syscall.operands, ())

    def test_register_codes(self):
        for code, name in enumerate(("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi")):
            with self.subTest(register=name):
                register = self.instruction(f"mov {name}, 1").operands[0]
                self.assertIsInstance(register, Register)
                self.assertEqual((register.name, register.code, register.width), (name, code, 64))

    def test_integer_values(self):
        for literal, expected in (
            ("60", 60), ("0", 0), ("-12", -12), ("+12", 12), ("0012", 12),
            ("0xFF", 255), ("0Xff", 255), ("-0x10", -16), ("+0x10", 16),
        ):
            with self.subTest(literal=literal):
                value = self.instruction(f"mov rax, {literal}").operands[1]
                self.assertIsInstance(value, Immediate)
                self.assertEqual(value.value, expected)

    def test_mov_encoding_range_is_deferred(self):
        value = 1 << 80
        self.assertEqual(self.instruction(f"mov rax, {value}").operands[1].value, value)

    def test_forward_symbol_and_separate_data_label(self):
        program = parse('mov rsi, Message\nMessage: db "Hi", 10', "hello.asm")
        instruction, label, data = program.statements
        self.assertIsInstance(instruction.operands[1], SymbolReference)
        self.assertEqual(instruction.operands[1].name, "Message")
        self.assertEqual(label.name, "Message")
        self.assertEqual(data.data, b"Hi\n")

    def test_undefined_symbol_is_preserved(self):
        self.assertEqual(self.instruction("mov rsi, Missing").operands[1].name, "Missing")

    def test_register_resembling_symbols_are_allowed(self):
        for symbol in ("raq", "r8_buffer", "rax_value", "eaxCount", "r16", "_data"):
            with self.subTest(symbol=symbol):
                operand = self.instruction(f"mov rax, {symbol}").operands[1]
                self.assertIsInstance(operand, SymbolReference)
                self.assertEqual(operand.name, symbol)

    def test_case_normalization_and_symbol_case(self):
        program = parse("Entry: MoV RAX, entry\nSYSCALL", "a.asm")
        self.assertEqual(program.statements[0].name, "Entry")
        instruction = program.statements[1]
        self.assertEqual(instruction.mnemonic, "mov")
        self.assertEqual(instruction.operands[0].name, "rax")
        self.assertEqual(instruction.operands[1].name, "entry")
        self.assertEqual(program.statements[2].mnemonic, "syscall")

    def test_blank_lines_and_comments(self):
        self.assertEqual(parse("\n \t\n; comment\n  ; another", "a.asm").statements, [])
        self.assertEqual(self.instruction("syscall ; ignored , : \"").mnemonic, "syscall")

    def test_label_only_and_same_line_instruction(self):
        program = parse("loop: ; comment\n_next: syscall", "a.asm")
        self.assertEqual([type(item) for item in program.statements], [Label, Label, Instruction])
        self.assertEqual(program.statements[2].location.column, 8)

    def test_duplicate_labels_are_deferred(self):
        program = parse("same:\nsame:", "a.asm")
        self.assertEqual([item.name for item in program.statements], ["same", "same"])

    def test_db_strings_and_numeric_bytes(self):
        data = self.instruction('DB "Hello", 10, "!", 0x00, 255, +1')
        self.assertIsInstance(data, DataDirective)
        self.assertEqual(data.data, b"Hello\n!\x00\xff\x01")

    def test_db_only_numeric_bytes(self):
        self.assertEqual(self.instruction("db 0, 0xff, 10").data, b"\x00\xff\n")

    def test_db_empty_string(self):
        self.assertEqual(self.instruction('db ""').data, b"")

    def test_db_quoted_punctuation(self):
        data = self.instruction('db "a;b,c:d", 10 ; comment "')
        self.assertEqual(data.data, b"a;b,c:d\n")

    def test_all_approved_escapes(self):
        data = self.instruction(r'db "\n\r\t\0\\\""')
        self.assertEqual(data.data, b'\n\r\t\x00\\"')

    def test_escaped_quote_does_not_start_comment(self):
        data = self.instruction(r'db "a\";b", 10')
        self.assertEqual(data.data, b'a";b\n')

    def test_original_text_and_operand_locations(self):
        line = '\tMsg:  MoV RSI, Target ; keep spacing  '
        program = parse("\n" + line, "source.asm")
        label, instruction = program.statements
        for item, column in ((label, 2), (instruction, 8),
                             (instruction.operands[0], 12), (instruction.operands[1], 17)):
            self.assertEqual(item.location, SourceLocation("source.asm", 2, column, line))

    def test_db_location_after_label(self):
        line = '  msg: db "x"'
        label, data = parse(line, "a.asm").statements
        self.assertEqual(label.location.column, 3)
        self.assertEqual(data.location, SourceLocation("a.asm", 1, 8, line))

    def test_line_endings(self):
        for separator in ("\n", "\r\n", "\r"):
            with self.subTest(separator=separator):
                program = parse(separator.join(("; comment", "  syscall", "")), "a.asm")
                self.assertEqual(program.statements[0].location,
                                 SourceLocation("a.asm", 2, 3, "  syscall"))

    def test_unknown_instructions(self):
        for source in ("nop", "jmp loop", "je loop", "jne loop"):
            with self.subTest(source=source):
                self.assert_invalid(source, "Unknown instruction", 1)

    def test_arithmetic_register_and_immediate_operands(self):
        for mnemonic in ("add", "sub", "cmp"):
            for code, name in enumerate(("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi")):
                with self.subTest(mnemonic=mnemonic, register=name):
                    instruction = self.instruction(f"{mnemonic} {name}, -5")
                    self.assertEqual(instruction.mnemonic, mnemonic)
                    self.assertEqual((instruction.operands[0].name, instruction.operands[0].code,
                                      instruction.operands[0].width), (name, code, 64))
                    self.assertIsInstance(instruction.operands[1], Immediate)
                    self.assertEqual(instruction.operands[1].value, -5)

    def test_arithmetic_literal_formats_and_range_deferred_to_encoder(self):
        for literal, value in (("0", 0), ("+12", 12), ("-0x10", -16),
                               ("0X7fffffff", 2147483647), ("2147483648", 2147483648)):
            with self.subTest(literal=literal):
                self.assertEqual(self.instruction(f"add rax, {literal}").operands[1].value, value)

    def test_xor_parses_all_register_pairs(self):
        registers = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi")
        for destination in registers:
            for source in registers:
                with self.subTest(destination=destination, source=source):
                    instruction = self.instruction(f"xor {destination}, {source}")
                    self.assertEqual(instruction.mnemonic, "xor")
                    self.assertEqual([operand.name for operand in instruction.operands], [destination, source])
                    self.assertTrue(all(isinstance(operand, Register) for operand in instruction.operands))

    def test_arithmetic_preserves_case_normalization_and_locations(self):
        line = "\tHere: AdD RDI, -0x10 ; keep original  "
        label, instruction = parse("\n" + line, "arithmetic.asm").statements
        self.assertEqual(label.name, "Here")
        self.assertEqual(instruction.mnemonic, "add")
        self.assertEqual(instruction.operands[0].name, "rdi")
        for item, column in ((label, 2), (instruction, 8),
                             (instruction.operands[0], 12), (instruction.operands[1], 17)):
            self.assertEqual(item.location, SourceLocation("arithmetic.asm", 2, column, line))
        self.assertEqual(self.instruction("XoR RAX, RBX").operands[1].name, "rbx")

    def test_arithmetic_rejects_noninteger_sources(self):
        for mnemonic in ("add", "sub", "cmp"):
            for operand in ("msg", "rbx", "eax", "r8", "[rax]", '"x"', "0xGG"):
                with self.subTest(mnemonic=mnemonic, operand=operand):
                    self.assert_invalid(f"{mnemonic} rax, {operand}", "Invalid integer literal", 10)

    def test_xor_rejects_nonregister_sources(self):
        for operand in ("1", "-1", "msg", "eax", "r8", "[rax]", '"x"'):
            with self.subTest(operand=operand):
                self.assert_invalid(f"xor rax, {operand}", "source must be a supported 64-bit register", 10)

    def test_arithmetic_rejects_invalid_destinations(self):
        for mnemonic in ("add", "sub", "cmp", "xor"):
            for destination in ("eax", "r8", "raq", "1", "[rax]"):
                with self.subTest(mnemonic=mnemonic, destination=destination):
                    source = "rax" if mnemonic == "xor" else "1"
                    self.assert_invalid(f"{mnemonic} {destination}, {source}",
                                        "destination must be a supported 64-bit register", 5)

    def test_arithmetic_operand_counts_and_separators(self):
        for mnemonic in ("add", "sub", "cmp", "xor"):
            with self.subTest(mnemonic=mnemonic):
                self.assert_invalid(mnemonic, "two operands", 4)
                self.assert_invalid(f"{mnemonic} rax", "two operands", 8)
                self.assert_invalid(f"{mnemonic} rax,", "Missing operand", 9)
                self.assert_invalid(f"{mnemonic} , 1", "Missing operand", 5)
                self.assert_invalid(f"{mnemonic} rax 1", "Expected comma", 9)
                self.assert_invalid(f"{mnemonic} rax, 1, 2", "Extra operand", 13)

    def test_invalid_destination_registers(self):
        for name in ("eax", "r8", "raq", "R15D", "1", "[rax]"):
            with self.subTest(name=name):
                self.assert_invalid(f"mov {name}, 1", "supported 64-bit register", 5)

    def test_unsupported_source_registers(self):
        for name in ("eax", "R8", "r15w", "ax", "al", "rip"):
            with self.subTest(name=name):
                self.assert_invalid(f"mov rax, {name}", "Unsupported register", 10)

    def test_incorrect_operand_combinations(self):
        self.assert_invalid("mov rax, rbx", "Register-to-register", 10)
        self.assert_invalid('mov rax, "text"', "integer or symbol", 10)
        self.assert_invalid('mov "rax", 1', "supported 64-bit register", 5)
        self.assert_invalid("mov rax, [msg]", "Invalid integer", 10)

    def test_missing_mov_operands(self):
        self.assert_invalid("mov", "two operands", 4)
        self.assert_invalid("mov rax", "two operands", 8)
        self.assert_invalid("mov rax,", "Missing operand", 9)
        self.assert_invalid("mov , 1", "Missing operand", 5)

    def test_extra_operands(self):
        self.assert_invalid("mov rax, 1, 2", "Extra operand", 13)
        self.assert_invalid("syscall 1", "Extra operand", 9)
        self.assert_invalid("syscall rax, 2", "Extra operand", 9)

    def test_missing_commas_and_empty_operands(self):
        for source, column in (("mov rax 1", 9), ('db "a" "b"', 8),
                               ("mov rax,,1", 9), ("db ,1", 4), ("db 1,", 6)):
            with self.subTest(source=source):
                self.assert_invalid(source, "operand", column)

    def test_syscall_rejects_punctuation(self):
        for source in ("syscall ,", "syscall ,1", "syscall 1,", "syscall 1 2"):
            with self.subTest(source=source):
                with self.assertRaises(ByteForgeError):
                    parse(source, "a.asm")

    def test_malformed_integer_literals(self):
        for literal in ("0x", "0xGG", "12abc", "1.5", "--1", "+", "-", "0b10", "1_000", "- 1"):
            with self.subTest(literal=literal):
                with self.assertRaises(ByteForgeError):
                    parse(f"mov rax, {literal}", "a.asm")
        self.assert_invalid("mov rax, 0xGG", "Invalid integer", 10)

    def test_unterminated_strings(self):
        self.assert_invalid('db "hello', "Unterminated string", 4)
        self.assert_invalid('db "hello\\', "Unterminated string", 4)

    def test_unsupported_escape_and_non_ascii(self):
        self.assert_invalid(r'db "\x41"', "Unsupported string escape", 5)
        self.assert_invalid('db "café"', "ASCII", 8)

    def test_invalid_db_values(self):
        self.assert_invalid("db", "at least one value", 3)
        self.assert_invalid("db -1", "between 0 and 255", 4)
        self.assert_invalid("db 256", "between 0 and 255", 4)
        self.assert_invalid("db symbol", "Invalid integer", 4)
        self.assert_invalid("db 'Hi'", "Invalid integer", 4)

    def test_malformed_labels(self):
        for source, column in (("1bad: syscall", 1), ("bad-name:", 1),
                               (": syscall", 1), ("a: b: syscall", 5),
                               ("bad label: syscall", 10)):
            with self.subTest(source=source):
                self.assert_invalid(source, "label", column)

    def test_missing_operand_location_precedes_comment(self):
        source = "mov rax,   ; missing"
        self.assert_invalid(source, "Missing operand", 9)

    def test_error_preserves_full_source_on_later_line(self):
        line = "\tMOV EAX, 1 ; preserve me  "
        with self.assertRaises(ByteForgeError) as caught:
            parse("; first\n" + line, "a.asm")
        self.assertEqual(caught.exception.location, SourceLocation("a.asm", 2, 6, line))

    def test_models_are_frozen(self):
        program = parse('name: mov rax, symbol\nmov rax, 1\ndb "x"', "a.asm")
        objects = [program, *program.statements,
                   *program.statements[1].operands, program.statements[2].operands[1]]
        for item in objects:
            field = next(iter(item.__dataclass_fields__))
            with self.subTest(model=type(item).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(item, field, getattr(item, field))

    def test_parser_does_not_print_or_open_files(self):
        with patch("builtins.open") as open_file, patch("sys.stdout") as stdout, patch(
            "sys.stderr"
        ) as stderr:
            parse("mov rax, 60\nsyscall", "a.asm")
            open_file.assert_not_called()
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
