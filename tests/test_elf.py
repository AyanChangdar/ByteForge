"""Independent unpacking tests for the fixed ELF64 file layout."""

from dataclasses import replace
import struct
import unittest
from unittest.mock import patch

from byteforge.elf import build_executable
from byteforge.model import ElfConfig
from byteforge.parser import parse
from byteforge.resolver import assemble


class ElfTests(unittest.TestCase):
    def setUp(self):
        self.config = ElfConfig()
        source = "_start:\nmov rax, 60\nmov rdi, 7\nsyscall"
        self.result = assemble(parse(source, "exit.asm"), 0x401000)
        self.image, self.layout = build_executable(self.result, self.config)
        self.header = struct.unpack_from("<16sHHIQQQIHHHHHH", self.image)
        self.segment = struct.unpack_from("<IIQQQQQQ", self.image, 64)

    def test_magic_and_identity(self):
        self.assertEqual(self.image[:4], b"\x7fELF")
        self.assertEqual(self.image[4:9], bytes((2, 1, 1, 0, 0)))
        self.assertEqual(self.image[9:16], bytes(7))

    def test_executable_machine_and_version(self):
        self.assertEqual(self.header[1:4], (2, 62, 1))

    def test_header_sizes_and_offsets(self):
        self.assertEqual(self.header[5], 64)
        self.assertEqual(self.header[8:11], (64, 56, 1))
        self.assertEqual(struct.calcsize("<16sHHIQQQIHHHHHH"), 64)
        self.assertEqual(struct.calcsize("<IIQQQQQQ"), 56)

    def test_no_sections_or_processor_flags(self):
        self.assertEqual(self.header[6:8], (0, 0))
        self.assertEqual(self.header[11:], (0, 0, 0))

    def test_single_read_execute_load_segment(self):
        self.assertEqual(self.segment[:2], (1, 5))
        self.assertEqual(self.header[10], 1)
        self.assertEqual(len(self.layout.segments), 1)

    def test_segment_offset_base_and_alignment(self):
        self.assertEqual(self.segment[2:5], (0, 0x400000, 0x400000))
        self.assertEqual(self.segment[7], 0x1000)
        self.assertEqual(self.segment[2] % self.segment[7], self.segment[3] % self.segment[7])

    def test_segment_sizes_and_file_length(self):
        self.assertEqual(len(self.image), 4118)
        self.assertEqual(self.segment[5], len(self.image))
        self.assertEqual(self.segment[6], len(self.image))
        self.assertGreaterEqual(self.segment[6], self.segment[5])
        self.assertEqual(self.layout.file_size, 4118)

    def test_entry_and_layout_metadata(self):
        self.assertEqual(self.header[4], 0x401000)
        self.assertEqual(self.layout.entry, self.result.symbols["_start"])
        segment = self.layout.segments[0]
        self.assertEqual((segment.p_type, segment.p_flags, segment.p_offset, segment.p_vaddr,
                          segment.p_paddr, segment.p_filesz, segment.p_memsz, segment.p_align),
                         self.segment)

    def test_exact_code_at_code_offset(self):
        self.assertEqual(self.image[0x1000:], bytes.fromhex(
            "48 B8 3C 00 00 00 00 00 00 00 48 BF 07 00 00 00 00 00 00 00 0F 05"
        ))

    def test_zero_padding(self):
        self.assertEqual(self.image[120:4096], bytes(4096 - 120))

    def test_nonzero_entry_offset(self):
        result = assemble(parse("mov rdi, 0\n_start: syscall", "a.asm"), 0x401000)
        image, layout = build_executable(result, self.config)
        self.assertEqual(struct.unpack_from("<Q", image, 24)[0], 0x40100A)
        self.assertEqual(layout.entry, 0x40100A)

    def test_entry_must_be_within_code_at_instruction_boundary(self):
        for entry in (0x400000, 0x401001, 0x401016, 0x401017, None):
            with self.subTest(entry=entry):
                result = replace(self.result, symbols={"_start": entry})
                with self.assertRaises(ValueError):
                    build_executable(result, self.config)

    def test_missing_entry_contract(self):
        with self.assertRaisesRegex(ValueError, "entry"):
            build_executable(replace(self.result, symbols={}), self.config)

    def test_assembly_address_mismatch(self):
        result = assemble(parse("_start: syscall", "a.asm"), 0x400000)
        with self.assertRaisesRegex(ValueError, "address"):
            build_executable(result, self.config)

    def test_payload_item_mismatch(self):
        with self.assertRaisesRegex(ValueError, "payload"):
            build_executable(replace(self.result, code=b"\x00"), self.config)

    def test_data_is_appended_after_code(self):
        image, layout = build_executable(replace(self.result, data=b"x"), self.config)
        self.assertEqual(image[0x1000:], self.result.code + b"x")
        self.assertEqual(layout.file_size, 4119)
        self.assertEqual(struct.unpack_from("<QQ", image, 96), (4119, 4119))

    def test_empty_code_rejected_even_with_data(self):
        for result in (replace(self.result, code=b""), replace(self.result, items=[]),
                       replace(self.result, code=b"", items=[], data=b"x")):
            with self.subTest(result=result):
                with self.assertRaises(ValueError):
                    build_executable(result, self.config)

    def test_hello_world_layout_and_symbol_bytes(self):
        source = ('_start:\nmov rax, 1\nmov rdi, 1\nmov rsi, msg\nmov rdx, 14\nsyscall\n'
                  'mov rax, 60\nmov rdi, 0\nsyscall\nmsg: db "Hello, World!", 10')
        result = assemble(parse(source, "hello.asm"), 0x401000)
        image, layout = build_executable(result, self.config)
        header = struct.unpack_from("<16sHHIQQQIHHHHHH", image)
        segment = struct.unpack_from("<IIQQQQQQ", image, 64)
        self.assertEqual(len(result.code), 64)
        self.assertEqual(result.data, b"Hello, World!\n")
        self.assertEqual(result.symbols["msg"], 0x401040)
        self.assertEqual(image[0x1040:], b"Hello, World!\n")
        self.assertEqual(result.symbols["msg"] - 0x400000, 0x1040)
        self.assertEqual(image[0x1014:0x101E], bytes.fromhex("48 BE 40 10 40 00 00 00 00 00"))
        self.assertEqual(image[0x1000:0x1040], bytes.fromhex(
            "48 B8 01 00 00 00 00 00 00 00 48 BF 01 00 00 00 00 00 00 00 "
            "48 BE 40 10 40 00 00 00 00 00 48 BA 0E 00 00 00 00 00 00 00 "
            "0F 05 48 B8 3C 00 00 00 00 00 00 00 48 BF 00 00 00 00 00 00 00 00 0F 05"
        ))
        self.assertEqual(header[1:4], (2, 62, 1))
        self.assertEqual(header[4], 0x401000)
        self.assertEqual(segment, (1, 5, 0, 0x400000, 0x400000, 4174, 4174, 0x1000))
        self.assertEqual(len(image), 4174)
        self.assertEqual(layout.file_size, 4174)
        self.assertEqual(build_executable(result, self.config), (image, layout))

    def test_empty_data_does_not_change_exit_binary(self):
        source = '_start:\nmov rax, 60\nmov rdi, 7\nsyscall\nempty: db ""'
        result = assemble(parse(source, "a.asm"), 0x401000)
        image, layout = build_executable(result, self.config)
        self.assertEqual(result.data, b"")
        self.assertEqual(image, self.image)
        self.assertEqual(layout, self.layout)

    def test_entry_in_data_is_rejected_by_writer(self):
        result = replace(self.result, data=b"x", symbols={"_start": 0x401016})
        with self.assertRaisesRegex(ValueError, "inside the code"):
            build_executable(result, self.config)

    def test_invalid_configurations(self):
        configurations = (
            replace(self.config, code_offset=64),
            replace(self.config, code_offset=4097),
            replace(self.config, image_base=0x400001),
            replace(self.config, alignment=0),
            replace(self.config, alignment=3),
            replace(self.config, image_base=-4096),
            replace(self.config, code_offset=4096.0),
            replace(self.config, code_offset=8192),
        )
        for config in configurations:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    build_executable(self.result, config)

    def test_deterministic_binary_and_metadata(self):
        self.assertEqual(build_executable(self.result, self.config), (self.image, self.layout))

    def test_no_file_io_or_printing(self):
        with patch("builtins.open") as files, patch("sys.stdout") as stdout, patch("sys.stderr") as stderr:
            build_executable(self.result, self.config)
            files.assert_not_called()
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
