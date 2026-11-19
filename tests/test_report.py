"""Golden serialization tests over the existing assembler and trace engine."""

from dataclasses import replace
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from byteforge.model import SourceLocation
from byteforge.report import (
    SCHEMA_VERSION, assembly_report, build_report, bytes_report, cpu_report,
    error_report, hex64, location_report, memory_report, step_report, view_report,
)
from byteforge.trace import TraceSession, initial_state, prepare_trace, step
from byteforge.trace_model import MASK64, TraceLimits


class ReportTests(unittest.TestCase):
    def prepare(self, source='_start: mov rax, 60\nmov rdi, 7\nsyscall'):
        return prepare_trace(source, "example.asm", TraceLimits())

    def example(self, name):
        source = (Path(__file__).resolve().parents[1] / "examples" / (name + ".asm")).read_text(encoding="utf-8")
        return self.prepare(source)

    def test_hex64_is_exact_and_fixed_width(self):
        self.assertEqual(hex64(MASK64), "0xffffffffffffffff")
        self.assertEqual(hex64(0), "0x0000000000000000")
        for value in (-1, 1 << 64, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hex64(value)

    def test_cpu_values_above_javascript_safe_integer_range(self):
        state = initial_state(self.prepare())
        state = replace(state, rip=MASK64, step_count=MASK64, stdout_size=MASK64,
                        registers={**state.registers, "rax": (1 << 53) + 1, "rdi": MASK64})
        result = json.loads(json.dumps(cpu_report(state)))
        self.assertEqual(result["registers"]["rax"], "0x0020000000000001")
        self.assertEqual(result["registers"]["rdi"], "0xffffffffffffffff")
        for name in ("rip", "step_count", "stdout_size"):
            self.assertEqual(result[name], "0xffffffffffffffff")

    def test_locations_preserve_exact_text_and_columns(self):
        location = SourceLocation("sample.asm", 3, 5, "\t  mov rax, 60 ; keep ")
        self.assertEqual(location_report(location), {"filename": "sample.asm", "line": 3,
                         "column": 5, "text": location.text})
        self.assertIsNone(location_report(None))

    def test_binary_bytes_are_lossless_and_display_safe(self):
        data = b"A\x00\xff\n\x1b\\"
        result = bytes_report(data)
        self.assertEqual(bytes.fromhex(result["hex"]), data)
        self.assertEqual(result["length"], len(data))
        self.assertNotIn("\n", result["display"])
        self.assertNotIn("\x1b", result["display"])
        self.assertTrue(result["display"].isascii())
        self.assertEqual(bytes_report(b""), {"hex": "", "display": "", "length": 0})

    def test_instruction_bytes_offsets_and_operands_are_actual_metadata(self):
        program = self.prepare("\n_start: mov rax, 60\nmov rdi, -1\nsyscall")
        result = assembly_report(program)
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        for item, serialized in zip(program.assembly.items, result["instructions"]):
            self.assertEqual(bytes.fromhex(serialized["bytes_hex"]), item.data)
            self.assertEqual(int(serialized["address"], 16), item.address)
            self.assertEqual(serialized["size"], len(item.data))
            offset = serialized["file_offset"]
            self.assertEqual(program.elf_image[offset:offset + len(item.data)], item.data)
        self.assertEqual(result["instructions"][0]["bytes_hex"], "48 B8 3C 00 00 00 00 00 00 00")
        immediate = result["instructions"][1]["operands"][1]
        self.assertEqual(immediate["value"], "-1")
        self.assertEqual(immediate["bits"], "0xffffffffffffffff")
        self.assertEqual(result["instructions"][0]["location"]["line"], 2)

    def test_symbolic_mov_and_symbol_addresses(self):
        program = self.example("hello")
        result = assembly_report(program)
        msg = next(item for item in result["symbols"] if item["name"] == "msg")
        self.assertEqual(msg, {"name": "msg", "address": "0x0000000000401040", "file_offset": 0x1040})
        instruction = result["instructions"][2]
        self.assertEqual(instruction["operands"][1]["name"], "msg")
        self.assertEqual(instruction["resolution"], {"msg": msg["address"]})
        self.assertEqual(int.from_bytes(bytes.fromhex(instruction["bytes_hex"])[2:], "little"), 0x401040)

    def test_elf_header_and_regions_match_real_image(self):
        program = self.example("hello")
        result = assembly_report(program)["elf"]
        header = result["header"]
        raw = struct.unpack_from("<16sHHIQQQIHHHHHH", program.elf_image)
        self.assertEqual(header["magic_hex"], "7F 45 4C 46")
        self.assertEqual((header["elf_class"], header["byte_order"], header["e_type"], header["e_machine"]),
                         (2, 1, 2, 62))
        self.assertEqual(int(header["e_entry"], 16), raw[4])
        self.assertEqual(header["e_phoff"], raw[5])
        self.assertEqual((header["e_ehsize"], header["e_phentsize"], header["e_phnum"]), (64, 56, 1))
        self.assertEqual(result["file_size"], 4174)
        self.assertEqual(result["segments"][0]["p_filesz"], len(program.elf_image))
        self.assertEqual(result["segments"][0]["p_vaddr"], "0x0000000000400000")
        self.assertEqual(result["segments"][0]["p_flags"], 5)
        self.assertEqual([(item["name"], item["file_offset"], item["size"]) for item in result["regions"]],
                         [("elf_header", 0, 64), ("program_headers", 64, 56), ("padding", 120, 3976),
                          ("code", 4096, 64), ("data", 4160, 14)])
        self.assertEqual(sum(item["size"] for item in result["regions"]), len(program.elf_image))

    def test_entry_after_first_instruction_does_not_shift_code_or_data(self):
        result = assembly_report(self.prepare('mov rax, 0\n_start: syscall\nmsg: db "X"'))
        self.assertEqual(result["elf"]["entry"], "0x000000000040100a")
        self.assertEqual(result["code_base"], "0x0000000000401000")
        self.assertEqual(result["data_base"], "0x000000000040100c")

    def test_build_report_keeps_documented_three_argument_contract(self):
        program = self.prepare()
        result = build_report(program.program, program.assembly, program.elf_layout)
        self.assertEqual(result["instructions"], assembly_report(program)["instructions"])

    def test_steps_include_branches_changes_and_fault_locations(self):
        program = self.prepare("_start: jmp _start")
        result = step_report(step(program, initial_state(program)))
        self.assertTrue(result["branch_taken"])
        self.assertEqual(result["branch_target"], "0x0000000000401000")
        self.assertEqual(result["bytes_hex"], "E9 FB FF FF FF")
        self.assertEqual(result["changed_registers"], [])
        unknown = self.prepare("_start: syscall")
        fault = step_report(step(unknown, initial_state(unknown)))["fault"]
        self.assertEqual(fault["phase"], "trace")
        self.assertEqual(fault["code"], "unsupported_syscall")
        self.assertEqual(fault["location"]["column"], 9)
        self.assertIsNone(step_report(None))

    def test_view_hello_output_and_exit(self):
        result = view_report(TraceSession(self.example("hello")).continue_())
        self.assertEqual(result["execution_status"], "halted_successfully")
        self.assertEqual(bytes.fromhex(result["stdout"]["hex"]), b"Hello, World!\n")
        self.assertEqual(result["cpu"]["exit_status"], 0)

    def test_view_statuses_are_distinct(self):
        session = TraceSession(self.prepare())
        self.assertEqual(view_report(session.view())["execution_status"], "ready")
        session.next()
        self.assertEqual(view_report(session.view())["execution_status"], "running")
        unknown = TraceSession(self.prepare("_start: syscall"))
        self.assertEqual(view_report(unknown.continue_())["execution_status"], "unsupported_operation")
        fallthrough = TraceSession(self.prepare("_start: mov rax, 0"))
        self.assertEqual(view_report(fallthrough.continue_())["execution_status"], "faulted")
        limited = TraceSession(prepare_trace("_start: jmp _start", "limit.asm", TraceLimits(max_steps=1)))
        self.assertEqual(view_report(limited.continue_())["execution_status"], "step_limit_reached")

    def test_memory_report_maps_to_real_elf_bytes(self):
        program = self.example("hello")
        result = memory_report(program, program.data_base, program.data)
        self.assertEqual(result["file_offset"], 0x1040)
        self.assertEqual(bytes.fromhex(result["data"]["hex"]), program.elf_image[0x1040:])
        self.assertTrue(result["read_only"])

    def test_error_shape_with_optional_source_location(self):
        self.assertEqual(error_report("request", "bad_json", "Invalid JSON"),
                         {"phase": "request", "code": "bad_json", "message": "Invalid JSON", "location": None})

    def test_deterministic_json_and_no_io(self):
        program = self.example("hello")
        with patch("builtins.open") as opened, patch("builtins.print") as printed, \
                patch("subprocess.Popen") as process:
            first = json.dumps(assembly_report(program), sort_keys=True)
            second = json.dumps(assembly_report(program), sort_keys=True)
        self.assertEqual(first, second)
        for mocked in (opened, printed, process):
            mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
