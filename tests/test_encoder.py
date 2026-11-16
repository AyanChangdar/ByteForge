"""Golden-byte and validation tests for the minimal instruction encoder."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from byteforge.encoder import encode, encoded_size
from byteforge.model import (
    ByteForgeError, Immediate, Instruction, Register, SourceLocation, SymbolReference,
)
from byteforge.parser import parse


class EncoderTests(unittest.TestCase):
    def instruction(self, source):
        return parse(source, "test.asm").statements[0]

    def assert_rejected(self, instruction, location, message):
        for operation in (encoded_size, lambda item: encode(item, 0, {})):
            with self.subTest(operation=operation):
                with self.assertRaises(ByteForgeError) as caught:
                    operation(instruction)
                self.assertEqual(caught.exception.location, location)
                self.assertIn(message, caught.exception.message)

    def test_mov_rax_60(self):
        instruction = self.instruction("mov rax, 60")
        self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 B8 3C 00 00 00 00 00 00 00"))

    def test_mov_rdi_zero(self):
        instruction = self.instruction("mov rdi, 0")
        self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 BF 00 00 00 00 00 00 00 00"))

    def test_golden_bytes_for_all_base_registers(self):
        for name, opcode in (
            ("rax", "B8"), ("rcx", "B9"), ("rdx", "BA"), ("rbx", "BB"),
            ("rsp", "BC"), ("rbp", "BD"), ("rsi", "BE"), ("rdi", "BF"),
        ):
            with self.subTest(register=name):
                instruction = self.instruction(f"mov {name}, 60")
                expected = bytes.fromhex(f"48 {opcode} 3C 00 00 00 00 00 00 00")
                self.assertEqual(encode(instruction, 0, {}), expected)
                self.assertEqual(encoded_size(instruction), 10)
                self.assertEqual(len(encode(instruction, 0, {})), 10)

    def test_syscall_bytes_and_size(self):
        instruction = self.instruction("syscall")
        self.assertEqual(encode(instruction, 0, {}), b"\x0f\x05")
        self.assertEqual(encoded_size(instruction), 2)
        self.assertEqual(len(encode(instruction, 0, {})), 2)

    def test_little_endian_immediate(self):
        instruction = self.instruction("mov rax, 0x0123456789ABCDEF")
        self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 B8 EF CD AB 89 67 45 23 01"))

    def test_negative_immediates(self):
        for value, expected in (
            (-1, "FF FF FF FF FF FF FF FF"),
            (-2, "FE FF FF FF FF FF FF FF"),
            (-256, "00 FF FF FF FF FF FF FF"),
        ):
            with self.subTest(value=value):
                instruction = self.instruction(f"mov rax, {value}")
                self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 B8 " + expected))
                self.assertEqual(encoded_size(instruction), 10)

    def test_signed_minimum(self):
        instruction = self.instruction(f"mov rax, {-(1 << 63)}")
        self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 B8 00 00 00 00 00 00 00 80"))
        self.assertEqual(encoded_size(instruction), 10)

    def test_signed_maximum(self):
        instruction = self.instruction(f"mov rax, {(1 << 63) - 1}")
        self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 B8 FF FF FF FF FF FF FF 7F"))

    def test_unsigned_upper_half(self):
        instruction = self.instruction(f"mov rax, {1 << 63}")
        self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 B8 00 00 00 00 00 00 00 80"))

    def test_unsigned_maximum(self):
        instruction = self.instruction(f"mov rax, {(1 << 64) - 1}")
        self.assertEqual(encode(instruction, 0, {}), bytes.fromhex("48 B8 FF FF FF FF FF FF FF FF"))
        self.assertEqual(encoded_size(instruction), 10)

    def test_immediate_overflow_and_underflow(self):
        for value in (1 << 64, -(1 << 63) - 1, 1 << 100, -(1 << 100)):
            with self.subTest(value=value):
                instruction = self.instruction(f"mov rax, {value}")
                self.assert_rejected(instruction, instruction.operands[1].location, "range")

    def test_noninteger_immediate_values(self):
        instruction = self.instruction("mov rax, 1")
        for value in (1.5, "1", True, None):
            with self.subTest(value=value):
                immediate = replace(instruction.operands[1], value=value)
                self.assert_rejected(
                    replace(instruction, operands=(instruction.operands[0], immediate)),
                    immediate.location, "must be an integer",
                )

    def test_unsupported_mnemonics(self):
        instruction = self.instruction("mov rax, 1")
        for mnemonic in ("add", "sub", "xor", "cmp", "jmp", "je", "jne", "nop"):
            with self.subTest(mnemonic=mnemonic):
                self.assert_rejected(replace(instruction, mnemonic=mnemonic),
                                     instruction.location, "Unsupported instruction")

    def test_wrong_mov_operand_counts(self):
        instruction = self.instruction("mov rax, 1")
        for operands in ((), instruction.operands[:1], instruction.operands + (instruction.operands[1],)):
            with self.subTest(count=len(operands)):
                self.assert_rejected(replace(instruction, operands=operands),
                                     instruction.location, "requires 2 operands")

    def test_syscall_rejects_operands(self):
        instruction = self.instruction("syscall")
        immediate = Immediate(1, instruction.location)
        self.assert_rejected(replace(instruction, operands=(immediate,)),
                             instruction.location, "requires 0 operands")

    def test_symbolic_mov_uses_resolved_address(self):
        instruction = self.instruction("mov rsi, Message")
        self.assertIsInstance(instruction.operands[1], SymbolReference)
        self.assertEqual(encoded_size(instruction), 10)
        self.assertEqual(encode(instruction, 0x401000, {"Message": 0x401040}),
                         bytes.fromhex("48 BE 40 10 40 00 00 00 00 00"))

    def test_undefined_symbol_reports_operand_location(self):
        instruction = self.instruction("mov rsi, Message")
        self.assertEqual(encoded_size(instruction), 10)
        with self.assertRaises(ByteForgeError) as caught:
            encode(instruction, 0x401000, {})
        self.assertEqual(caught.exception.location, instruction.operands[1].location)
        self.assertIn("Undefined symbol: Message", caught.exception.message)

    def test_symbol_names_are_case_sensitive(self):
        instruction = self.instruction("mov rsi, Message")
        with self.assertRaisesRegex(ByteForgeError, "Undefined symbol: Message"):
            encode(instruction, 0, {"message": 0x401040})

    def test_invalid_symbol_addresses(self):
        instruction = self.instruction("mov rsi, Message")
        for value in (-1, 1 << 64, True, None, 1.5, "0x401040"):
            with self.subTest(value=value):
                with self.assertRaises(ByteForgeError) as caught:
                    encode(instruction, 0, {"Message": value})
                self.assertEqual(caught.exception.location, instruction.operands[1].location)
                self.assertIn("unsigned 64-bit", caught.exception.message)

    def test_symbol_address_boundaries(self):
        instruction = self.instruction("mov rax, symbol")
        for address, expected in ((0, "00 00 00 00 00 00 00 00"),
                                  ((1 << 64) - 1, "FF FF FF FF FF FF FF FF")):
            with self.subTest(address=address):
                self.assertEqual(encode(instruction, 0, {"symbol": address}),
                                 bytes.fromhex("48 B8 " + expected))

    def test_symbolic_golden_bytes_all_base_registers(self):
        for name, opcode in (("rax", "B8"), ("rcx", "B9"), ("rdx", "BA"), ("rbx", "BB"),
                             ("rsp", "BC"), ("rbp", "BD"), ("rsi", "BE"), ("rdi", "BF")):
            with self.subTest(register=name):
                instruction = self.instruction(f"mov {name}, msg")
                data = encode(instruction, 0, {"msg": 0x0123456789ABCDEF})
                self.assertEqual(data, bytes.fromhex(f"48 {opcode} EF CD AB 89 67 45 23 01"))
                self.assertEqual(encoded_size(instruction), len(data))

    def test_symbolic_output_is_deterministic_and_does_not_mutate_inputs(self):
        instruction = self.instruction("mov rsi, msg")
        before = repr(instruction)
        symbols = {"msg": 0x401040}
        self.assertEqual(encode(instruction, 0, symbols), encode(instruction, 100, symbols))
        self.assertEqual(symbols, {"msg": 0x401040})
        self.assertEqual(repr(instruction), before)

    def test_register_to_register_mov_is_rejected(self):
        instruction = self.instruction("mov rax, 1")
        register = Register("rbx", 3, 64, SourceLocation("test.asm", 1, 10, "mov rax, rbx"))
        self.assert_rejected(replace(instruction, operands=(instruction.operands[0], register)),
                             register.location, "integer immediate")

    def test_nonregister_destination_is_rejected(self):
        instruction = self.instruction("mov rax, 1")
        immediate = Immediate(0, instruction.operands[0].location)
        self.assert_rejected(replace(instruction, operands=(immediate, instruction.operands[1])),
                             immediate.location, "register destination")

    def test_invalid_register_codes(self):
        instruction = self.instruction("mov rax, 1")
        for code in (-1, 1, 8, 15, True, 0.0):
            with self.subTest(code=code):
                register = replace(instruction.operands[0], code=code)
                self.assert_rejected(replace(instruction, operands=(register, instruction.operands[1])),
                                     register.location, "matching name and code")

    def test_invalid_register_widths(self):
        instruction = self.instruction("mov rax, 1")
        for width in (8, 16, 32, 128, 64.0):
            with self.subTest(width=width):
                register = replace(instruction.operands[0], width=width)
                self.assert_rejected(replace(instruction, operands=(register, instruction.operands[1])),
                                     register.location, "64-bit register")

    def test_invalid_register_names(self):
        instruction = self.instruction("mov rax, 1")
        for name, code in (("eax", 0), ("r8", 8), ("raq", 0)):
            with self.subTest(name=name):
                register = replace(instruction.operands[0], name=name, code=code)
                self.assert_rejected(replace(instruction, operands=(register, instruction.operands[1])),
                                     register.location, "64-bit register")

    def test_immediate_diagnostic_preserves_parser_source_location(self):
        line = "\tMOV RAX, 18446744073709551616 ; too large  "
        instruction = parse("; comment\n" + line, "overflow.asm").statements[0]
        self.assert_rejected(instruction, SourceLocation("overflow.asm", 2, 11, line), "range")

    def test_parser_to_encoder_exit_stream(self):
        program = parse("_start:\nmov rax, 60\nmov rdi, 0\nsyscall", "exit.asm")
        instructions = [item for item in program.statements if isinstance(item, Instruction)]
        result = b"".join(encode(item, 0, {}) for item in instructions)
        self.assertEqual(result, bytes.fromhex(
            "48 B8 3C 00 00 00 00 00 00 00 48 BF 00 00 00 00 00 00 00 00 0F 05"
        ))
        self.assertEqual(sum(encoded_size(item) for item in instructions), len(result))

    def test_deterministic_bytes_and_unchanged_models(self):
        instruction = self.instruction("mov rax, -1")
        original = repr(instruction)
        symbols = {"unused": 123}
        first = encode(instruction, 0, symbols)
        second = encode(instruction, 0x401000, symbols)
        self.assertIs(type(first), bytes)
        self.assertEqual(first, second)
        self.assertEqual(first, encode(instruction, 0, {}))
        self.assertEqual(symbols, {"unused": 123})
        self.assertEqual(repr(instruction), original)

    def test_no_file_io_or_printing(self):
        instructions = [self.instruction("mov rax, 60"), self.instruction("syscall")]
        with patch("builtins.open") as open_file, patch("sys.stdout") as stdout, patch(
            "sys.stderr"
        ) as stderr:
            for instruction in instructions:
                encoded_size(instruction)
                encode(instruction, 0, {})
            open_file.assert_not_called()
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
