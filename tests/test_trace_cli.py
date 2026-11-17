"""Terminal debugger tests use mocked input; no generated binary is executed."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from byteforge.trace_cli import main


ROOT = Path(__file__).resolve().parents[1]


class TraceCLITests(unittest.TestCase):
    def run_debugger(self, commands, example="loop"):
        output = io.StringIO()
        with patch("builtins.input", side_effect=commands), redirect_stdout(output):
            code = main([str(ROOT / "examples" / (example + ".asm")), "--interactive"])
        return code, output.getvalue()

    def test_launch_identifies_simulation_and_entry(self):
        code, output = self.run_debugger(["quit"])
        self.assertEqual(code, 0)
        self.assertIn("SIMULATED CPU STATE", output)
        self.assertIn("no native ELF execution", output)
        self.assertIn("Entry: 0x0000000000401000", output)

    def test_next_registers_and_actual_bytes(self):
        code, output = self.run_debugger(["next", "registers", "q"])
        self.assertEqual(code, 0)
        self.assertIn("mov rcx, 3", output)
        self.assertIn("48 B9 03 00 00 00 00 00 00 00", output)
        self.assertIn("RCX=0x0000000000000003", output)
        self.assertIn("RAX=0x0000000000000000", output)
        self.assertIn("RIP=0x000000000040100A", output)
        self.assertIn("ZF 0 -> 0", output)

    def test_branch_decisions_are_shown(self):
        code, output = self.run_debugger(["c 4", "c 3", "c 3", "q"])
        self.assertEqual(code, 0)
        self.assertIn("Branch TAKEN; target 0x000000000040100A", output)
        self.assertIn("Branch NOT TAKEN; target 0x000000000040100A", output)
        self.assertIn("0F 85 EC FF FF FF", output)

    def test_back_cached_forward_and_reset(self):
        code, output = self.run_debugger(["n", "back", "n", "reset", "registers", "q"])
        self.assertEqual(code, 0)
        self.assertIn("history=0/1", output)
        self.assertEqual(output.count("history=1/1"), 2)
        self.assertIn("history=0/0", output)
        self.assertIn("RCX=0x0000000000000000", output)

    def test_memory_inspection_shows_exact_db_bytes(self):
        code, output = self.run_debugger(["memory 0x401040 14", "q"], "hello")
        self.assertEqual(code, 0)
        self.assertIn("48 65 6C 6C 6F 2C 20 57 6F 72 6C 64 21 0A", output)
        self.assertIn("b'Hello, World!\\n'", output)

    def test_memory_default_length_is_sixteen(self):
        source = b'_start: mov rax, 60\nsyscall\nmsg: db "ABCDEFGHIJKLMNOP"'
        output = io.StringIO()
        with patch("pathlib.Path.open", return_value=io.BytesIO(source)), \
                patch("builtins.input", side_effect=["memory 0x40100c", "q"]), redirect_stdout(output):
            code = main(["memory.asm", "--interactive"])
        self.assertEqual(code, 0)
        self.assertIn("b'ABCDEFGHIJKLMNOP'", output.getvalue())

    def test_hello_output_and_exit_status_are_separate_from_debugger_code(self):
        code, output = self.run_debugger(["continue", "quit"], "hello")
        self.assertEqual(code, 0)
        self.assertIn("Captured stdout: b'Hello, World!\\n'", output)
        self.assertIn("Simulated exit status: 0 (separate from debugger exit code)", output)

    def test_exit_seven_still_returns_debugger_zero(self):
        code, output = self.run_debugger(["c", "n", "q"], "exit")
        self.assertEqual(code, 0)
        self.assertIn("Simulated exit status: 7 (separate from debugger exit code)", output)
        self.assertIn("Fault state_stopped", output)

    def test_help_and_blank_input(self):
        code, output = self.run_debugger(["", "help", "quit"])
        self.assertEqual(code, 0)
        for command in ("next / n", "back", "registers", "memory ADDRESS", "continue / c", "reset", "quit / q"):
            self.assertIn(command, output)

    def test_invalid_commands_do_not_crash_or_prevent_later_steps(self):
        commands = ["unknown", "next extra", '"unterminated', "memory", "memory nope 2",
                    "memory 0x401040 257", "memory -1 1", "memory 0 1", "c 101", "c 0",
                    "continue 1 2", "quit extra", "n", "q"]
        code, output = self.run_debugger(commands)
        self.assertEqual(code, 0)
        self.assertEqual(output.count("Command error:"), 12)
        self.assertIn("RCX=0x0000000000000003", output)

    def test_faults_show_source_and_reason(self):
        source = b"_start: syscall"
        output = io.StringIO()
        with patch("pathlib.Path.open", return_value=io.BytesIO(source)), \
                patch("builtins.input", side_effect=["n", "q"]), redirect_stdout(output):
            code = main(["fault.asm", "--interactive"])
        self.assertEqual(code, 0)
        self.assertIn("Source fault.asm:1:9", output.getvalue())
        self.assertIn("Unsupported syscall number 0", output.getvalue())
        self.assertIn("reason=unsupported_syscall", output.getvalue())

    def test_eof_exits_cleanly(self):
        code, output = self.run_debugger([EOFError()])
        self.assertEqual(code, 0)
        self.assertIn("End of input", output)

    def test_ctrl_c_exits_cleanly(self):
        code, output = self.run_debugger([KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertIn("Debugger interrupted", output)

    def test_missing_source_is_reported(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([str(ROOT / "examples" / "does-not-exist.asm"), "--interactive"])
        self.assertEqual(code, 1)
        self.assertIn("TraceLab error:", output.getvalue())

    def test_parser_diagnostics_preserve_original_source(self):
        output = io.StringIO()
        with patch("pathlib.Path.open", return_value=io.BytesIO(b"_start: nop")), redirect_stdout(output):
            code = main(["invalid.asm", "--interactive"])
        self.assertEqual(code, 1)
        self.assertIn("invalid.asm:1:9", output.getvalue())
        self.assertIn("_start: nop", output.getvalue())
        self.assertIn("^", output.getvalue())

    def test_source_read_is_bounded_before_preparation(self):
        output = io.StringIO()
        with patch("pathlib.Path.open", return_value=io.BytesIO(b";" * 65537)), \
                patch("byteforge.trace_cli.prepare_trace") as prepare, redirect_stdout(output):
            code = main(["large.asm", "--interactive"])
        self.assertEqual(code, 1)
        prepare.assert_not_called()
        self.assertIn("Source exceeds 65536", output.getvalue())

    def test_invalid_utf8_is_reported(self):
        output = io.StringIO()
        with patch("pathlib.Path.open", return_value=io.BytesIO(b"\xff")), redirect_stdout(output):
            code = main(["invalid-utf8.asm", "--interactive"])
        self.assertEqual(code, 1)
        self.assertIn("TraceLab error:", output.getvalue())

    def test_debugger_never_launches_native_binaries_or_writes_files(self):
        actual_open = Path.open
        modes = []
        def source_only(path, mode="r", *args, **kwargs):
            modes.append(mode)
            return actual_open(path, mode, *args, **kwargs)
        with patch("pathlib.Path.open", source_only), patch("subprocess.Popen") as process, \
                patch("os.system") as system, patch("os.write") as write, patch("os._exit") as exit_:
            code, output = self.run_debugger(["c", "q"], "hello")
        self.assertEqual(code, 0)
        self.assertIn("Hello, World!", output)
        self.assertEqual(modes, ["rb"])
        for mocked in (process, system, write, exit_):
            mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
