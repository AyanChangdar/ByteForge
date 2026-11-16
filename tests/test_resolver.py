"""Tests for the minimal instruction layout and label pass."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from byteforge.model import ByteForgeError, SourceLocation, SymbolReference
from byteforge.parser import parse
from byteforge.resolver import assemble, _validate_branch_target


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
        program.statements[1] = replace(program.statements[1], mnemonic="nop")
        with self.assertRaisesRegex(ByteForgeError, "Unsupported instruction"):
            assemble(program, 0x401000)

    def test_forward_branches_skip_exact_instruction_bytes(self):
        for mnemonic, opcode, size in (("jmp", "E9", 5), ("je", "0F 84", 6), ("jne", "0F 85", 6)):
            with self.subTest(mnemonic=mnemonic):
                program = parse(f"_start: {mnemonic} target\nmov rax, 0\ntarget: syscall", "a.asm")
                result = assemble(program, 0x401000)
                self.assertEqual(result.symbols["target"], 0x401000 + size + 10)
                self.assertEqual(result.items[0].data, bytes.fromhex(opcode + " 0A 00 00 00"))
                self.assertEqual(result.items[0].resolution, {"target": 0x401000 + size + 10})
                self.assertEqual(result.items[-1].address, result.symbols["target"])

    def test_backward_branches_use_negative_displacement(self):
        for mnemonic, golden in (("jmp", "E9 F4 FF FF FF"), ("je", "0F 84 F3 FF FF FF"),
                                  ("jne", "0F 85 F3 FF FF FF")):
            with self.subTest(mnemonic=mnemonic):
                source = f"_start: xor rax, rax\ntarget: add rdi, 1\n{mnemonic} target"
                result = assemble(parse(source, "a.asm"), 0x401000)
                self.assertEqual(result.symbols["target"], 0x401003)
                self.assertEqual(result.items[-1].address, 0x40100A)
                self.assertEqual(result.items[-1].data, bytes.fromhex(golden))

    def test_zero_displacement_to_next_instruction(self):
        for mnemonic, opcode in (("jmp", "E9"), ("je", "0F 84"), ("jne", "0F 85")):
            with self.subTest(mnemonic=mnemonic):
                result = assemble(parse(f"_start: {mnemonic} next\nnext: syscall", "a.asm"), 0x401000)
                self.assertEqual(result.items[0].data, bytes.fromhex(opcode + " 00 00 00 00"))

    def test_branch_to_self_is_valid_boundary(self):
        for mnemonic, golden in (("jmp", "E9 FB FF FF FF"), ("je", "0F 84 FA FF FF FF"),
                                  ("jne", "0F 85 FA FF FF FF")):
            with self.subTest(mnemonic=mnemonic):
                result = assemble(parse(f"_start: {mnemonic} _start", "a.asm"), 0x401000)
                self.assertEqual(result.code, bytes.fromhex(golden))

    def test_branch_undefined_and_case_mismatched_labels(self):
        for mnemonic in ("jmp", "je", "jne"):
            for suffix in ("", "\ntarget: syscall"):
                with self.subTest(mnemonic=mnemonic, suffix=suffix):
                    program = parse(f"_start: {mnemonic} Target" + suffix, "a.asm")
                    with self.assertRaises(ByteForgeError) as caught:
                        assemble(program, 0x401000)
                    self.assertIn("Undefined symbol: Target", caught.exception.message)
                    self.assertEqual(caught.exception.location, program.statements[1].operands[0].location)

    def test_branches_to_data_empty_data_and_trailing_labels_rejected(self):
        for mnemonic in ("jmp", "je", "jne"):
            for suffix in ('target: db "x"', 'target: db ""', "target:"):
                with self.subTest(mnemonic=mnemonic, suffix=suffix):
                    program = parse(f"_start: {mnemonic} target\nsyscall\n" + suffix, "a.asm")
                    with patch("byteforge.resolver.encode") as encoder:
                        with self.assertRaises(ByteForgeError) as caught:
                            assemble(program, 0x401000)
                        encoder.assert_not_called()
                    self.assertIn("instruction boundary", caught.exception.message)
                    self.assertEqual(caught.exception.location, program.statements[1].operands[0].location)

    def test_boundary_validator_rejects_inside_instruction_address(self):
        target = SymbolReference("middle", SourceLocation("a.asm", 1, 5, "jmp middle"))
        with self.assertRaises(ByteForgeError) as caught:
            _validate_branch_target(target, {"middle": 0x401001}, {0x401000, 0x40100A})
        self.assertEqual(caught.exception.location, target.location)
        self.assertIn("instruction boundary", caught.exception.message)

    def test_multiple_code_labels_can_alias_one_instruction(self):
        source = "_start: jmp first\nje second\njne third\nfirst:\nsecond:\nthird: syscall"
        result = assemble(parse(source, "a.asm"), 0x401000)
        for name in ("first", "second", "third"):
            self.assertEqual(result.symbols[name], 0x401011)
        self.assertEqual(result.items[0].data, bytes.fromhex("E9 0C 00 00 00"))
        self.assertEqual(result.items[1].data, bytes.fromhex("0F 84 06 00 00 00"))
        self.assertEqual(result.items[2].data, bytes.fromhex("0F 85 00 00 00 00"))

    def test_mixed_sizes_and_entry_offset_remain_stable(self):
        source = ("xor rdi, rdi\n_start: jmp target\nadd rax, 1\nmov rdi, 0\n"
                  "cmp rax, 0\ntarget: syscall")
        result = assemble(parse(source, "a.asm"), 0x401000)
        self.assertEqual(result.symbols["_start"], 0x401003)
        self.assertEqual(result.symbols["target"], 0x401020)
        self.assertEqual([item.address for item in result.items],
                         [0x401000, 0x401003, 0x401008, 0x40100F, 0x401019, 0x401020])
        self.assertEqual(result.items[1].data, bytes.fromhex("E9 18 00 00 00"))

    def test_symbolic_mov_still_accepts_data_with_branches(self):
        source = '_start: mov rsi, msg\njmp done\ndone: syscall\nmsg: db "x"'
        result = assemble(parse(source, "a.asm"), 0x401000)
        self.assertEqual(result.symbols["msg"], 0x401011)
        self.assertEqual(result.items[0].data, bytes.fromhex("48 BE 11 10 40 00 00 00 00 00"))
        self.assertEqual(result.items[1].data, bytes.fromhex("E9 00 00 00 00"))
        self.assertEqual(result.data, b"x")

    def test_branch_duplicate_labels_and_data_restriction(self):
        with self.assertRaisesRegex(ByteForgeError, "Duplicate label: target"):
            assemble(parse("_start: jmp target\ntarget: syscall\ntarget:", "a.asm"), 0x401000)
        for mnemonic in ("jmp", "je", "jne"):
            with self.subTest(mnemonic=mnemonic):
                with self.assertRaisesRegex(ByteForgeError, "cannot follow a DB"):
                    assemble(parse(f'_start: syscall\ndb ""\n{mnemonic} _start', "a.asm"), 0x401000)

    def test_branch_size_mismatch_is_detected(self):
        program = parse("_start: jmp _start", "a.asm")
        with patch("byteforge.resolver.encode", return_value=b"\xe9\x00"):
            with self.assertRaisesRegex(ByteForgeError, "size mismatch"):
                assemble(program, 0x401000)

    def test_branch_assembly_is_deterministic(self):
        program = parse("_start: jmp done\nxor rax, rax\ndone: syscall", "a.asm")
        before = repr(program)
        self.assertEqual(assemble(program, 0x401000), assemble(program, 0x401000))
        self.assertEqual(repr(program), before)

    def test_missing_branch_operand_is_rejected_by_encoder_contract(self):
        program = parse("_start: syscall", "a.asm")
        program.statements[1] = replace(program.statements[1], mnemonic="jmp")
        with self.assertRaisesRegex(ByteForgeError, "requires 1 operands"):
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
