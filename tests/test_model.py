"""Tests for the shared diagnostic contract."""

import unittest
from unittest.mock import patch

from byteforge.model import ByteForgeError, SourceLocation


class SourceLocationTests(unittest.TestCase):
    def test_fields_preserve_original_values(self):
        text = "\tMOV rax, 1  ; original spacing  "
        location = SourceLocation("examples/hello.asm", 7, 6, text)
        self.assertEqual(location.filename, "examples/hello.asm")
        self.assertEqual(location.line, 7)
        self.assertEqual(location.column, 6)
        self.assertEqual(location.text, text)


class ByteForgeErrorTests(unittest.TestCase):
    def test_preserves_location_and_message(self):
        location = SourceLocation("hello.asm", 2, 1, "bad")
        error = ByteForgeError(location, "Unsupported instruction: bad")
        self.assertIs(error.location, location)
        self.assertEqual(error.message, "Unsupported instruction: bad")
        self.assertEqual(error.args, ("Unsupported instruction: bad",))

    def test_formats_filename_line_column_and_source(self):
        error = ByteForgeError(
            SourceLocation("hello.asm", 12, 5, "mov wrong, 1"), "unknown operand"
        )
        self.assertEqual(
            str(error), "hello.asm:12:5: unknown operand\nmov wrong, 1\n    ^"
        )

    def test_caret_at_first_column(self):
        error = ByteForgeError(SourceLocation("a.asm", 1, 1, "bad"), "invalid")
        self.assertEqual(str(error), "a.asm:1:1: invalid\nbad\n^")

    def test_caret_at_last_character(self):
        error = ByteForgeError(SourceLocation("a.asm", 1, 3, "bad"), "invalid")
        self.assertEqual(str(error).split("\n")[-1], "  ^")

    def test_empty_source_text(self):
        error = ByteForgeError(SourceLocation("a.asm", 1, 1, ""), "missing input")
        self.assertEqual(str(error), "a.asm:1:1: missing input\n\n^")

    def test_column_after_end_clamps_caret_and_preserves_location(self):
        location = SourceLocation("a.asm", 1, 1000000, "mov")
        error = ByteForgeError(location, "missing operand")
        self.assertEqual(str(error), "a.asm:1:1000000: missing operand\nmov\n   ^")
        self.assertEqual(location.column, 1000000)

    def test_nonpositive_columns_clamp_caret(self):
        for column in (0, -5):
            with self.subTest(column=column):
                location = SourceLocation("a.asm", 1, column, "bad")
                self.assertEqual(
                    str(ByteForgeError(location, "invalid")),
                    f"a.asm:1:{column}: invalid\nbad\n^",
                )
                self.assertEqual(location.column, column)

    def test_empty_source_with_out_of_range_column(self):
        error = ByteForgeError(SourceLocation("a.asm", 1, 99, ""), "invalid")
        self.assertEqual(str(error), "a.asm:1:99: invalid\n\n^")

    def test_tabs_are_preserved_in_caret_padding(self):
        error = ByteForgeError(SourceLocation("a.asm", 1, 3, "\t bad"), "invalid")
        self.assertEqual(str(error), "a.asm:1:3: invalid\n\t bad\n\t ^")

    def test_can_be_raised_and_caught(self):
        error = ByteForgeError(SourceLocation("a.asm", 1, 1, "bad"), "invalid")
        with self.assertRaises(ByteForgeError) as caught:
            raise error
        self.assertIs(caught.exception, error)

    def test_construction_and_formatting_do_not_print_or_open_files(self):
        with patch("builtins.open") as open_file, patch("sys.stdout") as stdout, patch(
            "sys.stderr"
        ) as stderr:
            error = ByteForgeError(SourceLocation("a.asm", 1, 1, "bad"), "invalid")
            str(error)
            open_file.assert_not_called()
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
