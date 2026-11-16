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
        for mnemonic in ("jmp", "je", "jne", "nop"):
            with self.subTest(mnemonic=mnemonic):
                self.assert_rejected(replace(instruction, mnemonic=mnemonic),
                                     instruction.location, "Unsupported instruction")

    def test_arithmetic_documented_golden_bytes(self):
        for source, expected in (
            ("add rax, 5", "48 81 C0 05 00 00 00"),
            ("sub rcx, 1", "48 81 E9 01 00 00 00"),
            ("cmp rcx, 0", "48 81 F9 00 00 00 00"),
            ("xor rdi, rdi", "48 31 FF"),
            ("xor rax, rbx", "48 31 D8"),
        ):
            with self.subTest(source=source):
                instruction = self.instruction(source)
                data = encode(instruction, 0, {})
                self.assertEqual(data, bytes.fromhex(expected))
                self.assertEqual(encoded_size(instruction), len(data))

    def test_group1_golden_bytes_for_every_register(self):
        registers = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi")
        modrm_bytes = {
            "add": ("C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7"),
            "sub": ("E8", "E9", "EA", "EB", "EC", "ED", "EE", "EF"),
            "cmp": ("F8", "F9", "FA", "FB", "FC", "FD", "FE", "FF"),
        }
        for mnemonic, golden in modrm_bytes.items():
            for register, modrm in zip(registers, golden):
                with self.subTest(mnemonic=mnemonic, register=register):
                    instruction = self.instruction(f"{mnemonic} {register}, 5")
                    data = encode(instruction, 0, {})
                    self.assertEqual(data, bytes.fromhex(f"48 81 {modrm} 05 00 00 00"))
                    self.assertEqual(len(data), 7)
                    self.assertEqual(encoded_size(instruction), 7)

    def test_all_64_xor_register_pairs(self):
        registers = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi")
        # Rows are destinations, columns are sources. These literals form the oracle.
        golden = (
            "C0 C8 D0 D8 E0 E8 F0 F8", "C1 C9 D1 D9 E1 E9 F1 F9",
            "C2 CA D2 DA E2 EA F2 FA", "C3 CB D3 DB E3 EB F3 FB",
            "C4 CC D4 DC E4 EC F4 FC", "C5 CD D5 DD E5 ED F5 FD",
            "C6 CE D6 DE E6 EE F6 FE", "C7 CF D7 DF E7 EF F7 FF",
        )
        for destination, row in zip(registers, golden):
            for source, modrm in zip(registers, row.split()):
                with self.subTest(destination=destination, source=source):
                    instruction = self.instruction(f"xor {destination}, {source}")
                    data = encode(instruction, 0, {})
                    self.assertEqual(data, bytes.fromhex(f"48 31 {modrm}"))
                    self.assertEqual(len(data), 3)
                    self.assertEqual(encoded_size(instruction), 3)

    def test_signed_imm32_values_and_little_endian(self):
        for mnemonic, modrm in (("add", "C0"), ("sub", "E8"), ("cmp", "F8")):
            for value, golden in (
                (0, "00 00 00 00"), (1, "01 00 00 00"), (-1, "FF FF FF FF"),
                (-256, "00 FF FF FF"), (0x12345678, "78 56 34 12"),
                (-2147483648, "00 00 00 80"), (2147483647, "FF FF FF 7F"),
            ):
                with self.subTest(mnemonic=mnemonic, value=value):
                    instruction = self.instruction(f"{mnemonic} rax, {value}")
                    self.assertEqual(encode(instruction, 0, {}), bytes.fromhex(f"48 81 {modrm} {golden}"))
                    self.assertEqual(encoded_size(instruction), 7)

    def test_arithmetic_immediate_overflow_and_underflow(self):
        for mnemonic in ("add", "sub", "cmp"):
            for value in (-2147483649, 2147483648, 0xFFFFFFFF, 1 << 64):
                with self.subTest(mnemonic=mnemonic, value=value):
                    instruction = self.instruction(f"{mnemonic} rax, {value}")
                    self.assert_rejected(instruction, instruction.operands[1].location, "signed 32-bit")

    def test_arithmetic_rejects_noninteger_immediate_models(self):
        for mnemonic in ("add", "sub", "cmp"):
            instruction = self.instruction(f"{mnemonic} rax, 1")
            for value in (True, 1.0, "1", None):
                with self.subTest(mnemonic=mnemonic, value=value):
                    operand = replace(instruction.operands[1], value=value)
                    self.assert_rejected(replace(instruction, operands=(instruction.operands[0], operand)),
                                         operand.location, "signed 32-bit")

    def test_arithmetic_rejects_wrong_operand_models(self):
        for mnemonic in ("add", "sub", "cmp"):
            instruction = self.instruction(f"{mnemonic} rax, 1")
            register, immediate = instruction.operands
            for source in (register, SymbolReference("msg", immediate.location)):
                with self.subTest(mnemonic=mnemonic, source=source):
                    self.assert_rejected(replace(instruction, operands=(register, source)),
                                         source.location, "integer immediate")
            self.assert_rejected(replace(instruction, operands=(immediate, immediate)),
                                 immediate.location, "register destination")
        instruction = self.instruction("xor rax, rbx")
        for index in (0, 1):
            operands = list(instruction.operands)
            location = operands[index].location
            operands[index] = Immediate(0, location)
            self.assert_rejected(replace(instruction, operands=tuple(operands)), location, "two register")

    def test_arithmetic_operand_counts(self):
        for source in ("add rax, 1", "sub rax, 1", "cmp rax, 1", "xor rax, rbx"):
            instruction = self.instruction(source)
            for operands in ((), instruction.operands[:1], instruction.operands + instruction.operands[:1]):
                with self.subTest(source=source, count=len(operands)):
                    self.assert_rejected(replace(instruction, operands=operands),
                                         instruction.location, "requires 2 operands")

    def test_arithmetic_rejects_incorrect_register_metadata(self):
        for source in ("add rax, 1", "sub rax, 1", "cmp rax, 1", "xor rax, rbx"):
            instruction = self.instruction(source)
            indexes = (0, 1) if instruction.mnemonic == "xor" else (0,)
            for index in indexes:
                for fields in ({"code": 8}, {"code": -1}, {"width": 32},
                               {"name": "r8"}, {"name": "raq"}, {"code": True}):
                    with self.subTest(source=source, index=index, fields=fields):
                        operands = list(instruction.operands)
                        operands[index] = replace(operands[index], **fields)
                        self.assert_rejected(replace(instruction, operands=tuple(operands)),
                                             operands[index].location, "matching name and code")

    def test_arithmetic_range_diagnostic_preserves_source(self):
        line = "\tADD RDI, 2147483648 ; preserve this  "
        instruction = parse("; first\n" + line, "arithmetic.asm").statements[0]
        self.assert_rejected(instruction, SourceLocation("arithmetic.asm", 2, 11, line), "signed 32-bit")

    def test_arithmetic_output_is_deterministic_without_io(self):
        instructions = [self.instruction(source) for source in
                        ("add rax, -1", "sub rcx, 1", "cmp rcx, 0", "xor rdi, rax")]
        before = repr(instructions)
        symbols = {"unchanged": 123}
        with patch("builtins.open") as files, patch("sys.stdout") as stdout, patch("sys.stderr") as stderr:
            for instruction in instructions:
                self.assertEqual(encode(instruction, 0, symbols), encode(instruction, 100, {}))
            files.assert_not_called()
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()
        self.assertEqual(repr(instructions), before)
        self.assertEqual(symbols, {"unchanged": 123})

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
