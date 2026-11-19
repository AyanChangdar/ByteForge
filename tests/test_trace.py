"""Tests for TraceLab contracts and the bounded typed-instruction simulator."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import unittest
from unittest.mock import patch

from byteforge.elf import build_executable
from byteforge.model import ByteForgeError, ElfConfig, SourceLocation
from byteforge.parser import parse
from byteforge.resolver import assemble
from byteforge.trace import TraceSession, initial_state, prepare_trace, read_memory, step
from byteforge.trace_model import CPUState, MASK64, REGISTER_NAMES, TraceLimits, TraceStatus


class TraceModelTests(unittest.TestCase):
    def test_cpu_state_defaults(self):
        state = CPUState(rip=0x401000)
        self.assertEqual(dict(state.registers), dict.fromkeys(REGISTER_NAMES, 0))
        self.assertFalse(state.zf)
        self.assertEqual(state.step_count, 0)
        self.assertEqual(state.status, TraceStatus.RUNNING)
        self.assertIsNone(state.halt_reason)

    def test_registers_are_defensively_copied_and_read_only(self):
        registers = dict.fromkeys(REGISTER_NAMES, 0)
        state = CPUState(rip=0x401000, registers=registers)
        registers["rax"] = 123
        self.assertEqual(state.registers["rax"], 0)
        with self.assertRaises(TypeError):
            state.registers["rax"] = 1
        with self.assertRaises(FrozenInstanceError):
            state.rip = 0

    def test_cpu_state_requires_all_registers(self):
        for registers in ({"rax": 0}, {**dict.fromkeys(REGISTER_NAMES, 0), "r8": 0}):
            with self.subTest(registers=registers):
                with self.assertRaises(ValueError):
                    CPUState(rip=0x401000, registers=registers)

    def test_cpu_state_rejects_non_uint64_values(self):
        for value in (-1, 1 << 64, True, 1.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CPUState(rip=value)
                with self.assertRaises(ValueError):
                    CPUState(rip=0, step_count=value)
                with self.assertRaises(ValueError):
                    CPUState(rip=0, registers={**dict.fromkeys(REGISTER_NAMES, 0), "rax": value})
        self.assertEqual(CPUState(rip=MASK64).rip, MASK64)

    def test_cpu_state_validates_flag_and_status_types(self):
        with self.assertRaises(ValueError):
            CPUState(rip=0, zf=1)
        with self.assertRaises(ValueError):
            CPUState(rip=0, status="running")

    def test_default_and_lower_limits(self):
        self.assertEqual(TraceLimits(), TraceLimits(65536, 2048, 10000))
        self.assertEqual(TraceLimits(max_steps=1).max_steps, 1)

    def test_limits_reject_invalid_or_unbounded_values(self):
        for name, maximum in (("max_source_bytes", 65536), ("max_instructions", 2048), ("max_steps", 10000)):
            for value in (0, -1, True, 1.5, maximum + 1):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        TraceLimits(**{name: value})


class TracePreparationTests(unittest.TestCase):
    def test_preparation_reuses_real_code_symbols_and_elf(self):
        source = '_start: mov rsi, msg\nsyscall\nmsg: db "Hi", 10'
        trace = prepare_trace(source, "source.asm", TraceLimits())
        assembly = assemble(parse(source, "source.asm"), 0x401000)
        image, layout = build_executable(assembly, ElfConfig())
        self.assertEqual(trace.source, source)
        self.assertEqual(trace.program, parse(source, "source.asm"))
        self.assertEqual(trace.assembly, assembly)
        self.assertEqual(trace.elf_image, image)
        self.assertEqual(trace.elf_layout, layout)
        self.assertEqual(trace.code_base, 0x401000)
        self.assertEqual(trace.entry, 0x401000)
        self.assertEqual(trace.data_base, 0x40100C)
        self.assertEqual(trace.data, b"Hi\n")
        self.assertEqual(trace.symbols["msg"], trace.data_base)
        self.assertEqual(trace.elf_image[trace.data_base - 0x400000:], trace.data)

    def test_instruction_lookup_contains_actual_items_and_locations(self):
        line = "\t_start: mov rax, 60 ; original  "
        trace = prepare_trace(line + "\nsyscall", "real.asm", TraceLimits())
        item = trace.instructions[0x401000]
        self.assertIs(item, trace.assembly.items[0])
        self.assertEqual(item.data, bytes.fromhex("48 B8 3C 00 00 00 00 00 00 00"))
        self.assertEqual(item.statement.location, SourceLocation("real.asm", 1, 10, line))
        self.assertEqual(trace.instructions[0x40100A].data, b"\x0f\x05")

    def test_start_after_earlier_instructions(self):
        trace = prepare_trace("mov rdi, 99\n_start: syscall", "a.asm", TraceLimits())
        state = initial_state(trace)
        self.assertEqual(trace.code_base, 0x401000)
        self.assertEqual(trace.entry, 0x40100A)
        self.assertEqual(state.rip, 0x40100A)
        self.assertEqual(state.registers["rdi"], 0)
        self.assertFalse(state.zf)
        self.assertEqual(state.step_count, 0)

    def test_data_base_uses_code_base_not_nonzero_entry_offset(self):
        source = 'mov rdi, 99\n_start: mov rsi, msg\nsyscall\nempty: db ""\nmsg: db "X"'
        trace = prepare_trace(source, "data.asm", TraceLimits())
        self.assertEqual(trace.entry, 0x40100A)
        self.assertEqual(trace.data_base, 0x401016)
        self.assertEqual(trace.symbols["empty"], trace.data_base)
        self.assertEqual(trace.symbols["msg"], trace.data_base)
        self.assertEqual(trace.elf_image[0x1016:], b"X")

    def test_lookup_maps_are_separate_read_only_copies(self):
        trace = prepare_trace("_start: syscall", "a.asm", TraceLimits())
        with self.assertRaises(TypeError):
            trace.instructions[0] = trace.assembly.items[0]
        with self.assertRaises(TypeError):
            trace.symbols["_start"] = 0
        trace.assembly.symbols["_start"] = 0
        trace.assembly.items.clear()
        self.assertEqual(trace.symbols["_start"], 0x401000)
        self.assertEqual(trace.instructions[0x401000].data, b"\x0f\x05")

    def test_source_limit_is_enforced_before_parsing(self):
        with patch("byteforge.trace.parse") as parser:
            with self.assertRaises(ByteForgeError) as caught:
                prepare_trace("_start: syscall", "limit.asm", TraceLimits(max_source_bytes=5))
            parser.assert_not_called()
        self.assertEqual(caught.exception.location, SourceLocation("limit.asm", 1, 1, "_start: syscall"))
        self.assertIn("UTF-8 bytes", caught.exception.message)

    def test_source_limit_counts_utf8_bytes(self):
        source = "_start: syscall ; é"
        with self.assertRaisesRegex(ByteForgeError, "UTF-8 bytes"):
            prepare_trace(source, "a.asm", TraceLimits(max_source_bytes=len(source)))
        self.assertEqual(prepare_trace(source, "a.asm", TraceLimits(max_source_bytes=len(source.encode("utf-8")))).source,
                         source)

    def test_instruction_limit_uses_first_excess_instruction_location(self):
        source = "_start:\nmov rax, 60\nmov rdi, 7\nsyscall"
        with self.assertRaises(ByteForgeError) as caught:
            prepare_trace(source, "limit.asm", TraceLimits(max_instructions=1))
        self.assertEqual(caught.exception.location, SourceLocation("limit.asm", 3, 1, "mov rdi, 7"))

    def test_assembler_diagnostics_are_preserved(self):
        for source in ("_start: nop", "syscall", "_start: mov rsi, missing",
                       "_start: syscall\n_start:", '_start: syscall\ndb ""\nsyscall'):
            with self.subTest(source=source):
                with self.assertRaises(ByteForgeError) as original:
                    assemble(parse(source, "a.asm"), 0x401000, filename="a.asm")
                with self.assertRaises(ByteForgeError) as adapted:
                    prepare_trace(source, "a.asm", TraceLimits())
                self.assertEqual(str(adapted.exception), str(original.exception))

    def test_preparation_checks_elf_address_mapping(self):
        from byteforge.elf import build_executable as real_build
        def shifted_layout(result, config):
            image, layout = real_build(result, config)
            layout.segments[0] = replace(layout.segments[0], p_vaddr=0x500000)
            return image, layout
        with patch("byteforge.trace.build_executable", side_effect=shifted_layout):
            with self.assertRaisesRegex(ValueError, "ELF layout"):
                prepare_trace("_start: syscall", "a.asm", TraceLimits())

    def test_preparation_has_no_io_or_process_execution(self):
        with patch("builtins.open") as files, patch("sys.stdout") as stdout, patch("sys.stderr") as stderr, patch(
            "subprocess.Popen"
        ) as processes:
            prepare_trace("_start: syscall", "a.asm", TraceLimits())
            files.assert_not_called()
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()
            processes.assert_not_called()

    def test_all_existing_examples_have_identical_elf_images(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("exit", "hello", "arithmetic", "loop", "conditional"):
            with self.subTest(example=name):
                source = (root / "examples" / (name + ".asm")).read_text(encoding="utf-8")
                trace = prepare_trace(source, name + ".asm", TraceLimits())
                original = assemble(parse(source, name + ".asm"), 0x401000)
                image, layout = build_executable(original, ElfConfig())
                self.assertEqual(trace.assembly, original)
                self.assertEqual(trace.elf_image, image)
                self.assertEqual(trace.elf_layout, layout)


class TraceSemanticsTests(unittest.TestCase):
    def prepare(self, instruction):
        return prepare_trace(f"_start: {instruction}\nsyscall", "step.asm", TraceLimits())

    def state(self, program, *, zf=False, **registers):
        return replace(initial_state(program), zf=zf,
                       registers={**dict.fromkeys(REGISTER_NAMES, 0), **registers})

    def test_numeric_mov_values_and_zf_preservation(self):
        for value in (0, 1, -1, -(1 << 63), MASK64):
            with self.subTest(value=value):
                program = self.prepare(f"mov rax, {value}")
                before = self.state(program, zf=True)
                result = step(program, before)
                self.assertEqual(result.after.registers["rax"], value & MASK64)
                self.assertTrue(result.after.zf)
                self.assertEqual(result.after.rip, before.rip + 10)
                self.assertEqual(result.after.step_count, 1)
                self.assertTrue(result.executed)
                self.assertIsNone(result.fault)

    def test_mov_to_each_register_leaves_other_registers_unchanged(self):
        for name in REGISTER_NAMES:
            with self.subTest(register=name):
                program = self.prepare(f"mov {name}, 123")
                result = step(program, initial_state(program))
                self.assertEqual(result.changed_registers, (name,))
                self.assertEqual(dict(result.after.registers),
                                 {**dict.fromkeys(REGISTER_NAMES, 0), name: 123})

    def test_symbolic_mov_uses_final_code_and_data_addresses(self):
        for symbol in ("_start", "msg"):
            with self.subTest(symbol=symbol):
                source = f'_start: mov rsi, {symbol}\nsyscall\nmsg: db "Hi"'
                program = prepare_trace(source, "symbol.asm", TraceLimits())
                result = step(program, initial_state(program))
                self.assertEqual(result.after.registers["rsi"], program.symbols[symbol])
                self.assertEqual(int.from_bytes(result.data[2:], "little"), program.symbols[symbol])

    def test_symbol_spelling_remains_case_sensitive(self):
        source = '_start: mov rsi, Msg\nmov rdi, msg\nsyscall\nMsg: db "A"\nmsg: db "B"'
        program = prepare_trace(source, "symbols.asm", TraceLimits())
        first = step(program, initial_state(program))
        second = step(program, first.after)
        self.assertEqual(first.after.registers["rsi"], program.data_base)
        self.assertEqual(second.after.registers["rdi"], program.data_base + 1)

    def test_add_signed_immediates_and_wrapping(self):
        for old, immediate, expected in (
                (0, 5, 5), (9, 0, 9), (5, -5, 0), (0, -1, MASK64),
                (MASK64, 1, 0), (0, -(1 << 31), (-(1 << 31)) & MASK64),
                (1, (1 << 31) - 1, 1 << 31)):
            with self.subTest(old=old, immediate=immediate):
                program = self.prepare(f"add rax, {immediate}")
                result = step(program, self.state(program, rax=old, zf=expected != 0))
                self.assertEqual(result.after.registers["rax"], expected)
                self.assertEqual(result.after.zf, expected == 0)
                self.assertEqual(result.after.rip, program.entry + 7)

    def test_sub_signed_immediates_and_wrapping(self):
        for old, immediate, expected in (
                (8, 5, 3), (9, 0, 9), (5, 5, 0), (0, 1, MASK64),
                (0, -1, 1), (0, -(1 << 31), 1 << 31),
                (0, (1 << 31) - 1, (-((1 << 31) - 1)) & MASK64)):
            with self.subTest(old=old, immediate=immediate):
                program = self.prepare(f"sub rcx, {immediate}")
                result = step(program, self.state(program, rcx=old, zf=expected != 0))
                self.assertEqual(result.after.registers["rcx"], expected)
                self.assertEqual(result.after.zf, expected == 0)
                self.assertEqual(result.after.rip, program.entry + 7)

    def test_cmp_masks_subtraction_without_modifying_registers(self):
        for old, immediate, expected_zf in ((0, 0, True), (3, 1, False), (MASK64, -1, True),
                                             (1 << 63, -(1 << 31), False)):
            with self.subTest(old=old, immediate=immediate):
                program = self.prepare(f"cmp rcx, {immediate}")
                before = self.state(program, rcx=old, rdx=123, zf=not expected_zf)
                result = step(program, before)
                self.assertEqual(result.after.registers, before.registers)
                self.assertEqual(result.changed_registers, ())
                self.assertEqual(result.after.zf, expected_zf)

    def test_xor_distinct_registers_uses_original_values_and_destination_order(self):
        program = self.prepare("xor rax, rbx")
        before = self.state(program, rax=(1 << 63) | 3, rbx=(1 << 63) | 5, zf=True)
        result = step(program, before)
        self.assertEqual(result.after.registers["rax"], 6)
        self.assertEqual(result.after.registers["rbx"], (1 << 63) | 5)
        self.assertEqual(result.changed_registers, ("rax",))
        self.assertFalse(result.after.zf)
        self.assertEqual(result.after.rip, program.entry + 3)

    def test_xor_same_register_and_equal_distinct_registers_set_zf(self):
        for source in ("rdi", "rax"):
            with self.subTest(source=source):
                program = self.prepare(f"xor rdi, {source}")
                result = step(program, self.state(program, rdi=MASK64, rax=MASK64))
                self.assertEqual(result.after.registers["rdi"], 0)
                self.assertTrue(result.after.zf)

    def test_unchanged_destination_is_not_reported_as_changed(self):
        program = self.prepare("mov rax, 0")
        self.assertEqual(step(program, initial_state(program)).changed_registers, ())

    def test_conditional_branches_taken_and_not_taken_preserve_zf(self):
        for mnemonic, zf, taken in (("je", True, True), ("je", False, False),
                                    ("jne", False, True), ("jne", True, False)):
            with self.subTest(mnemonic=mnemonic, zf=zf):
                source = f"_start: {mnemonic} target\nmov rax, 1\ntarget: syscall"
                program = prepare_trace(source, "branch.asm", TraceLimits())
                result = step(program, self.state(program, zf=zf))
                self.assertEqual(result.branch_taken, taken)
                self.assertEqual(result.branch_target, program.symbols["target"])
                expected = program.symbols["target"] if taken else program.entry + 6
                self.assertEqual(result.after.rip, expected)
                self.assertEqual(result.after.zf, zf)
                self.assertEqual(result.changed_registers, ())

    def test_jmp_forward_and_backward_ignores_zf(self):
        source = "back: jmp _start\n_start: jmp forward\nforward: jmp back"
        program = prepare_trace(source, "jump.asm", TraceLimits())
        for zf in (False, True):
            with self.subTest(zf=zf):
                state = self.state(program, zf=zf)
                for symbol in ("forward", "back", "_start"):
                    result = step(program, state)
                    self.assertTrue(result.branch_taken)
                    self.assertEqual(result.after.rip, program.symbols[symbol])
                    self.assertEqual(result.after.zf, zf)
                    self.assertIsNone(result.fault)
                    state = result.after

    def test_each_conditional_can_branch_backward(self):
        for mnemonic, zf in (("je", True), ("jne", False)):
            with self.subTest(mnemonic=mnemonic):
                source = f"back: mov rax, 1\n_start: {mnemonic} back\nsyscall"
                program = prepare_trace(source, "back.asm", TraceLimits())
                result = step(program, self.state(program, zf=zf))
                self.assertEqual(result.after.rip, program.symbols["back"])
                self.assertTrue(result.branch_taken)
                self.assertLess(int.from_bytes(result.data[2:], "little", signed=True), 0)

    def test_existing_conditional_example_takes_and_skips_expected_branches(self):
        source = (Path(__file__).resolve().parents[1] / "examples" / "conditional.asm").read_text(encoding="utf-8")
        program = prepare_trace(source, "conditional.asm", TraceLimits())
        state = initial_state(program)
        decisions = []
        for _ in range(7):
            result = step(program, state)
            self.assertIsNone(result.fault)
            if result.branch_taken is not None:
                decisions.append((program.instructions[result.address].statement.mnemonic, result.branch_taken))
            state = result.after
        self.assertEqual(decisions, [("jne", False), ("je", True)])
        self.assertEqual(state.registers["rdi"], 7)
        self.assertEqual(state.registers["rax"], 60)
        self.assertEqual(program.instructions[state.rip].statement.mnemonic, "syscall")

    def test_step_begins_at_start_not_first_source_instruction(self):
        program = prepare_trace("mov rax, 99\n_start: mov rdi, 7\nsyscall", "entry.asm", TraceLimits())
        result = step(program, initial_state(program))
        self.assertEqual(result.location.line, 2)
        self.assertEqual(result.after.registers["rax"], 0)
        self.assertEqual(result.after.registers["rdi"], 7)

    def test_precise_source_location_and_actual_bytes(self):
        line = "\t_start:   ADD rcx, -1 ; keep this text"
        program = prepare_trace("; comment\n\n" + line + "\nsyscall", "exact.asm", TraceLimits())
        result = step(program, initial_state(program))
        self.assertEqual(result.location, SourceLocation("exact.asm", 3, 12, line))
        self.assertEqual(result.data, program.instructions[program.entry].data)
        self.assertEqual(result.data, bytes.fromhex("48 81 C1 FF FF FF FF"))
        self.assertEqual(result.address, program.entry)

    def test_previous_states_and_steps_cannot_be_modified(self):
        program = prepare_trace("_start: mov rax, 1\nadd rax, 2\nsyscall", "snapshots.asm", TraceLimits())
        initial = initial_state(program)
        first = step(program, initial)
        second = step(program, first.after)
        self.assertEqual(initial.registers["rax"], 0)
        self.assertEqual(first.after.registers["rax"], 1)
        self.assertEqual(second.after.registers["rax"], 3)
        self.assertIs(second.before, first.after)
        with self.assertRaises(TypeError):
            first.after.registers["rax"] = 99
        with self.assertRaises(FrozenInstanceError):
            first.after = second.after

    def test_first_twelve_steps_of_existing_loop(self):
        source = (Path(__file__).resolve().parents[1] / "examples" / "loop.asm").read_text(encoding="utf-8")
        program = prepare_trace(source, "loop.asm", TraceLimits())
        state = initial_state(program)
        expected = (
            ("mov", 0x40100A, 3, False, None),
            ("sub", 0x401011, 2, False, None),
            ("cmp", 0x401018, 2, False, None),
            ("jne", 0x40100A, 2, False, True),
            ("sub", 0x401011, 1, False, None),
            ("cmp", 0x401018, 1, False, None),
            ("jne", 0x40100A, 1, False, True),
            ("sub", 0x401011, 0, True, None),
            ("cmp", 0x401018, 0, True, None),
            ("jne", 0x40101E, 0, True, False),
            ("mov", 0x401028, 0, True, None),
            ("mov", 0x401032, 0, True, None),
        )
        for count, (mnemonic, rip, rcx, zf, taken) in enumerate(expected, 1):
            with self.subTest(count=count):
                result = step(program, state)
                self.assertEqual(program.instructions[result.address].statement.mnemonic, mnemonic)
                self.assertEqual((result.after.rip, result.after.registers["rcx"], result.after.zf,
                                  result.branch_taken), (rip, rcx, zf, taken))
                self.assertEqual(result.after.step_count, count)
                self.assertEqual(result.data, program.instructions[result.address].data)
                self.assertIsNone(result.fault)
                state = result.after
        self.assertEqual(state.registers["rax"], 60)
        self.assertEqual(state.registers["rdi"], 7)
        exited = step(program, state)
        self.assertIsNone(exited.fault)
        self.assertTrue(exited.executed)
        self.assertEqual(exited.after.step_count, 13)
        self.assertEqual(exited.after.exit_status, 7)
        self.assertEqual(exited.after.status, TraceStatus.HALTED)


class TraceProgressionTests(unittest.TestCase):
    def test_invalid_instruction_pointer_has_no_invented_source(self):
        program = prepare_trace('_start: mov rax, 1\nsyscall\nmsg: db "A"', "invalid.asm", TraceLimits())
        for address in (0, program.entry + 1, program.data_base, program.data_base + 1):
            with self.subTest(address=address):
                before = replace(initial_state(program), rip=address)
                result = step(program, before)
                self.assertEqual(result.fault.code, "invalid_rip")
                self.assertIsNone(result.fault.location)
                self.assertIsNone(result.location)
                self.assertEqual(result.data, b"")
                self.assertEqual(result.after.rip, before.rip)
                self.assertEqual(result.after.registers, before.registers)
                self.assertEqual(result.after.step_count, 0)
                self.assertFalse(result.executed)

    def test_syscall_is_explicitly_unsupported_with_no_cpu_effects(self):
        program = prepare_trace("_start: syscall", "syscall.asm", TraceLimits())
        before = replace(initial_state(program), zf=True,
                         registers={**dict.fromkeys(REGISTER_NAMES, 0), "rax": 999, "rcx": 9})
        result = step(program, before)
        self.assertEqual(result.fault.code, "unsupported_syscall")
        self.assertEqual(result.fault.location, SourceLocation("syscall.asm", 1, 9, "_start: syscall"))
        self.assertEqual(result.location, result.fault.location)
        self.assertEqual(result.data, b"\x0f\x05")
        self.assertEqual(result.after.status, TraceStatus.FAULTED)
        self.assertEqual(result.after.rip, before.rip)
        self.assertEqual(result.after.registers, before.registers)
        self.assertEqual(result.after.zf, before.zf)
        self.assertEqual(result.after.step_count, 0)
        self.assertEqual(result.changed_registers, ())
        self.assertFalse(result.executed)
        self.assertEqual(result.output, b"")
        self.assertEqual(result.after.stdout_size, 0)
        self.assertIsNone(result.after.exit_status)

    def test_step_limit_stops_infinite_loop_without_extra_execution(self):
        program = prepare_trace("_start: jmp _start", "limit.asm", TraceLimits(max_steps=3))
        state = initial_state(program)
        for count in range(1, 4):
            result = step(program, state)
            self.assertTrue(result.executed)
            self.assertEqual(result.after.step_count, count)
            state = result.after
        stopped = step(program, state)
        self.assertEqual(stopped.fault.code, "step_limit")
        self.assertIn("3-step limit", stopped.fault.message)
        self.assertEqual(stopped.after.status, TraceStatus.HALTED)
        self.assertEqual(stopped.after.step_count, 3)
        self.assertEqual(stopped.after.rip, state.rip)
        self.assertFalse(stopped.executed)

    def test_stopped_state_does_not_resume_or_change_reason(self):
        for status in (TraceStatus.HALTED, TraceStatus.FAULTED):
            with self.subTest(status=status):
                program = prepare_trace("_start: jmp _start", "stopped.asm", TraceLimits())
                state = replace(initial_state(program), status=status, halt_reason="previous_reason")
                result = step(program, state)
                self.assertIs(result.after, state)
                self.assertEqual(result.fault.code, "state_stopped")
                self.assertEqual(result.after.halt_reason, "previous_reason")
                self.assertFalse(result.executed)

    def test_fallthrough_outside_code_keeps_completed_effects_and_reports_fault(self):
        for suffix in ("", '\nmsg: db "A"'):
            with self.subTest(suffix=suffix):
                program = prepare_trace("_start: mov rax, 1" + suffix, "fallthrough.asm", TraceLimits())
                result = step(program, initial_state(program))
                self.assertEqual(result.after.registers["rax"], 1)
                self.assertEqual(result.after.rip, program.entry + len(result.data))
                self.assertEqual(result.after.step_count, 1)
                self.assertEqual(result.after.status, TraceStatus.FAULTED)
                self.assertEqual(result.fault.code, "invalid_rip")
                self.assertEqual(result.fault.location, result.location)
                self.assertTrue(result.executed)

    def test_defensive_invalid_branch_target_uses_operand_location(self):
        program = prepare_trace("_start: jmp target\ntarget: syscall", "target.asm", TraceLimits())
        program = replace(program, symbols={**program.symbols, "target": program.entry + 1})
        result = step(program, initial_state(program))
        self.assertEqual(result.fault.code, "invalid_target")
        self.assertEqual(result.fault.location.column, 13)
        self.assertEqual(result.after.step_count, 0)
        self.assertFalse(result.executed)

    def test_unknown_typed_instruction_cannot_be_silently_executed(self):
        program = prepare_trace("_start: syscall", "unsupported.asm", TraceLimits())
        item = program.instructions[program.entry]
        item = replace(item, statement=replace(item.statement, mnemonic="future_instruction"))
        program = replace(program, instructions={program.entry: item})
        result = step(program, initial_state(program))
        self.assertEqual(result.fault.code, "unsupported_instruction")
        self.assertEqual(result.after.step_count, 0)
        self.assertFalse(result.executed)

    def test_step_is_deterministic_and_has_no_io_or_process_execution(self):
        program = prepare_trace("_start: add rax, 5\nsyscall", "pure.asm", TraceLimits())
        before = initial_state(program)
        with patch("builtins.open") as opened, patch("builtins.print") as printed, \
                patch("subprocess.Popen") as process:
            first = step(program, before)
            second = step(program, before)
        self.assertEqual(first, second)
        opened.assert_not_called()
        printed.assert_not_called()
        process.assert_not_called()


class TraceSyscallTests(unittest.TestCase):
    def write_fixture(self, *, address=None, count=3, descriptor=1, limits=None):
        source = '_start: syscall\nmov rax, 60\nsyscall\nmsg: db 65, 0, 255'
        program = prepare_trace(source, "write.asm", limits or TraceLimits())
        state = replace(initial_state(program), zf=True, registers={
            **dict.fromkeys(REGISTER_NAMES, 0), "rax": 1, "rdi": descriptor,
            "rsi": program.data_base if address is None else address, "rdx": count,
        })
        return program, state

    def test_write_captures_exact_bytes_and_return_registers(self):
        program, before = self.write_fixture()
        result = step(program, before)
        self.assertEqual(result.output, b"A\x00\xff")
        self.assertEqual(result.after.stdout_size, 3)
        self.assertEqual(result.after.registers["rax"], 3)
        self.assertEqual(result.after.registers["rcx"], program.entry + 2)
        self.assertEqual(result.changed_registers, ("rax", "rcx"))
        for register in ("rbx", "rdx", "rsp", "rbp", "rsi", "rdi"):
            self.assertEqual(result.after.registers[register], before.registers[register])
        self.assertTrue(result.after.zf)
        self.assertEqual(result.after.step_count, 1)
        self.assertIsNone(result.fault)

    def test_zero_length_write_never_reads_memory(self):
        for address in (0, MASK64):
            with self.subTest(address=address):
                program, state = self.write_fixture(address=address, count=0)
                with patch("byteforge.trace._read_data") as memory:
                    result = step(program, state)
                memory.assert_not_called()
                self.assertEqual(result.output, b"")
                self.assertEqual(result.after.registers["rax"], 0)
                self.assertEqual(result.after.registers["rcx"], program.entry + 2)
                self.assertTrue(result.executed)

    def test_invalid_write_ranges_fault_without_partial_effects(self):
        program, state = self.write_fixture()
        for address, count in ((0, 1), (program.entry, 1), (program.data_base - 1, 1),
                               (program.data_base, 4), (program.data_base + 3, 1),
                               (MASK64, MASK64)):
            with self.subTest(address=address, count=count):
                before = replace(state, registers={**state.registers, "rsi": address, "rdx": count})
                result = step(program, before)
                self.assertEqual(result.fault.code, "invalid_memory")
                self.assertEqual(result.fault.location, program.instructions[program.entry].statement.location)
                self.assertEqual(result.after.registers, before.registers)
                self.assertEqual(result.after.rip, before.rip)
                self.assertEqual(result.after.stdout_size, 0)
                self.assertEqual(result.after.step_count, 0)
                self.assertFalse(result.executed)

    def test_unsupported_descriptors_rejected_even_for_empty_write(self):
        for descriptor in (0, 2, MASK64):
            with self.subTest(descriptor=descriptor):
                program, state = self.write_fixture(descriptor=descriptor, count=0)
                result = step(program, state)
                self.assertEqual(result.fault.code, "unsupported_descriptor")
                self.assertFalse(result.executed)

    def test_output_limit_allows_exact_boundary_then_blocks(self):
        program, state = self.write_fixture(limits=TraceLimits(max_output_bytes=3))
        exact = step(program, state)
        self.assertEqual(exact.after.stdout_size, 3)
        overflow = step(program, replace(state, stdout_size=1))
        self.assertEqual(overflow.fault.code, "output_limit")
        self.assertEqual(overflow.output, b"")
        self.assertEqual(overflow.after.stdout_size, 1)
        self.assertEqual(overflow.after.registers, state.registers)
        self.assertFalse(overflow.executed)

    def test_write_may_read_more_than_inspection_limit(self):
        program = prepare_trace('_start: syscall\nmov rax, 60\nsyscall\nmsg: db "' + 'A' * 300 + '"',
                                "long.asm", TraceLimits())
        state = replace(initial_state(program), registers={**dict.fromkeys(REGISTER_NAMES, 0),
                        "rax": 1, "rdi": 1, "rsi": program.data_base, "rdx": 300})
        self.assertEqual(step(program, state).output, b"A" * 300)
        with self.assertRaisesRegex(ValueError, "0..256"):
            read_memory(program, program.data_base, 300)

    def test_write_at_end_of_code_keeps_output_and_reports_fallthrough(self):
        program = prepare_trace('_start: syscall\nmsg: db "A"', "end.asm", TraceLimits())
        before = replace(initial_state(program), registers={**dict.fromkeys(REGISTER_NAMES, 0),
                         "rax": 1, "rdi": 1, "rsi": program.data_base, "rdx": 1})
        result = step(program, before)
        self.assertEqual(result.output, b"A")
        self.assertEqual(result.after.stdout_size, 1)
        self.assertEqual(result.fault.code, "invalid_rip")
        self.assertTrue(result.executed)

    def test_exit_status_is_low_byte_and_cannot_continue(self):
        program = prepare_trace("_start: syscall\nmov rdi, 99", "exit.asm", TraceLimits())
        for value in (0, 7, 256, 511, MASK64):
            with self.subTest(value=value):
                before = replace(initial_state(program), zf=True, registers={
                    **dict.fromkeys(REGISTER_NAMES, 0), "rax": 60, "rdi": value})
                result = step(program, before)
                self.assertEqual(result.after.exit_status, value & 255)
                self.assertEqual(result.after.status, TraceStatus.HALTED)
                self.assertEqual(result.after.halt_reason, "exit")
                self.assertEqual(result.after.rip, program.entry + 2)
                self.assertEqual(result.after.registers["rcx"], program.entry + 2)
                self.assertEqual(result.after.registers["rdi"], value)
                self.assertTrue(result.after.zf)
                self.assertTrue(result.executed)
                self.assertIsNone(result.fault)
                self.assertIs(step(program, result.after).after, result.after)

    def test_unsupported_syscall_numbers_remain_faults(self):
        program = prepare_trace("_start: syscall", "unknown.asm", TraceLimits())
        for number in (0, 2, 59, 231, MASK64):
            with self.subTest(number=number):
                before = replace(initial_state(program), registers={
                    **dict.fromkeys(REGISTER_NAMES, 0), "rax": number})
                result = step(program, before)
                self.assertEqual(result.fault.code, "unsupported_syscall")
                self.assertIn(str(number), result.fault.message)
                self.assertEqual(result.after.registers, before.registers)
                self.assertFalse(result.executed)

    def test_known_examples_have_exact_simulated_results(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("hello", "exit", "arithmetic", "loop", "conditional"):
            with self.subTest(example=name):
                program = prepare_trace((root / "examples" / (name + ".asm")).read_text(encoding="utf-8"),
                                        name + ".asm", TraceLimits())
                view = TraceSession(program).continue_()
                self.assertEqual(view.state.halt_reason, "exit")
                self.assertEqual(view.state.exit_status, 0 if name == "hello" else 7)
                self.assertEqual(view.stdout, b"Hello, World!\n" if name == "hello" else b"")

    def test_syscalls_perform_no_real_io_or_process_execution(self):
        program, state = self.write_fixture()
        with patch("builtins.open") as opened, patch("builtins.print") as printed, \
                patch("os.write") as written, patch("os._exit") as exited, \
                patch("subprocess.Popen") as process:
            result = step(program, state)
            step(program, replace(result.after, registers={**result.after.registers, "rax": 60}))
        for mocked in (opened, printed, written, exited, process):
            mocked.assert_not_called()


class TraceSessionTests(unittest.TestCase):
    def session(self, source="_start: mov rax, 1\nadd rax, -1\nmov rax, 60\nsyscall", **limits):
        return TraceSession(prepare_trace(source, "session.asm", TraceLimits(**limits)))

    def hello(self, **limits):
        source = (Path(__file__).resolve().parents[1] / "examples" / "hello.asm").read_text(encoding="utf-8")
        return self.session(source, **limits)

    def test_next_back_and_cached_forward_restore_complete_states(self):
        session = self.session()
        initial = session.view()
        first = session.next()
        second = session.next()
        self.assertTrue(second.after.zf)
        self.assertEqual(session.back().state, first.after)
        self.assertIs(session.next(), second)
        self.assertEqual(session.view().history_size, 2)
        self.assertEqual(session.back().state, first.after)
        self.assertEqual(session.back().state, initial.state)
        self.assertEqual(session.back().state, initial.state)

    def test_back_across_write_and_replay_does_not_duplicate_output(self):
        session = self.hello()
        for _ in range(5):
            session.next()
        written = session.view()
        self.assertEqual(written.stdout, b"Hello, World!\n")
        self.assertEqual(written.state.registers["rax"], 14)
        self.assertEqual(written.state.registers["rcx"], 0x40102A)
        back = session.back()
        self.assertEqual(back.stdout, b"")
        self.assertEqual(back.state.registers["rax"], 1)
        self.assertEqual(back.state.registers["rcx"], 0)
        self.assertIs(session.next(), written.last_step)
        self.assertEqual(session.view(), written)
        session.continue_()
        self.assertEqual(session.view().stdout, b"Hello, World!\n")

    def test_multiple_writes_keep_prefixes_and_append_only_at_frontier(self):
        source = ('_start: mov rax, 1\nmov rdi, 1\nmov rsi, msg\nmov rdx, 1\nsyscall\n'
                  'mov rax, 1\nsyscall\nmov rax, 60\nsyscall\nmsg: db "A"')
        session = self.session(source)
        session.continue_(7)
        two = session.view()
        self.assertEqual(two.stdout, b"AA")
        self.assertEqual(session.back().stdout, b"A")
        session.next()
        self.assertEqual(session.view(), two)
        self.assertEqual(session.continue_().stdout, b"AA")

    def test_back_across_exit_restores_running_state_then_cached_exit(self):
        session = self.session()
        exited = session.continue_()
        self.assertEqual(exited.state.exit_status, 0)
        prior = session.back()
        self.assertEqual(prior.state.status, TraceStatus.RUNNING)
        self.assertIsNone(prior.state.exit_status)
        self.assertIs(session.next(), exited.last_step)
        self.assertEqual(session.view(), exited)
        self.assertFalse(session.next().executed)
        self.assertEqual(session.view(), exited)

    def test_reset_clears_history_output_and_exit_state(self):
        session = self.hello()
        session.continue_()
        reset = session.reset()
        self.assertEqual(reset.state, initial_state(session.program))
        self.assertEqual(reset.stdout, b"")
        self.assertEqual((reset.cursor, reset.history_size), (0, 0))
        self.assertIsNone(reset.last_step)
        self.assertEqual(session.continue_().stdout, b"Hello, World!\n")

    def test_snapshots_and_views_stay_immutable_after_reset(self):
        session = self.hello()
        before = session.continue_()
        session.reset()
        self.assertEqual(before.stdout, b"Hello, World!\n")
        self.assertEqual(before.state.exit_status, 0)
        with self.assertRaises(FrozenInstanceError):
            before.stdout = b"changed"
        with self.assertRaises(TypeError):
            before.state.registers["rax"] = 1
        with self.assertRaises(AttributeError):
            session.program = session.program

    def test_session_uses_independent_assembly_lookups(self):
        session = self.session()
        session.program.program.statements.clear()
        session.program.assembly.items.clear()
        session.program.assembly.symbols.clear()
        self.assertEqual(session.continue_().state.exit_status, 0)

    def test_continue_limits_requests_and_replays_cache(self):
        session = self.session("_start: jmp _start")
        self.assertEqual(session.continue_().state.step_count, 100)
        self.assertEqual(session.continue_(3).state.step_count, 103)
        for _ in range(3):
            session.back()
        self.assertEqual(session.continue_(2).state.step_count, 102)
        self.assertEqual(session.view().history_size, 103)
        for count in (0, -1, 101, True, 1.5):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    session.continue_(count)

    def test_lower_continue_limit_is_enforced(self):
        session = self.session("_start: jmp _start", max_continue_steps=2)
        self.assertEqual(session.continue_(2).state.step_count, 2)
        with self.assertRaises(ValueError):
            session.continue_(3)

    def test_step_limit_fault_is_cached_reversible_and_bounded(self):
        session = self.session("_start: jmp _start", max_steps=3)
        stopped = session.continue_()
        self.assertEqual(stopped.state.halt_reason, "step_limit")
        self.assertEqual(stopped.state.step_count, 3)
        self.assertEqual(stopped.history_size, 4)
        self.assertEqual(session.back().state.status, TraceStatus.RUNNING)
        session.next()
        self.assertEqual(session.view(), stopped)
        session.continue_()
        self.assertEqual(session.view(), stopped)

    def test_history_limit_stops_without_dropping_previous_states(self):
        session = self.session("_start: jmp _start", max_history=2)
        stopped = session.continue_()
        self.assertEqual(stopped.state.halt_reason, "history_limit")
        self.assertEqual(stopped.state.step_count, 2)
        self.assertEqual(stopped.history_size, 3)
        session.next()
        self.assertEqual(session.view().history_size, 3)
        self.assertEqual(session.back().state.status, TraceStatus.RUNNING)

    def test_fault_and_preexisting_stdout_are_restored_on_back(self):
        session = self.hello(max_output_bytes=1)
        faulted = session.continue_()
        self.assertEqual(faulted.state.halt_reason, "output_limit")
        self.assertEqual(faulted.stdout, b"")
        self.assertEqual(session.back().state.status, TraceStatus.RUNNING)
        session.next()
        self.assertEqual(session.view(), faulted)


class TraceAdditionalBoundsTests(unittest.TestCase):
    def test_new_limits_reject_unbounded_values(self):
        for name, maximum in (("max_output_bytes", 65536), ("max_history", 10000),
                              ("max_continue_steps", 100), ("max_memory_bytes", 256)):
            for value in (0, -1, True, 1.5, maximum + 1):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        TraceLimits(**{name: value})

    def test_stdout_size_and_exit_status_validation(self):
        for value in (-1, 1 << 64, True, 1.5):
            with self.subTest(size=value):
                with self.assertRaises(ValueError):
                    CPUState(rip=0, stdout_size=value)
        for value in (-1, 256, True, 1.5):
            with self.subTest(status=value):
                with self.assertRaises(ValueError):
                    CPUState(rip=0, exit_status=value)

    def test_memory_inspection_exact_bounds_and_bytes(self):
        program = prepare_trace('_start: syscall\nmsg: db 0, 65, 255', "memory.asm", TraceLimits())
        self.assertEqual(read_memory(program, program.data_base, 3), b"\0A\xff")
        self.assertEqual(read_memory(program, program.data_base + 1, 2), b"A\xff")
        self.assertEqual(read_memory(program, program.data_base + 3, 0), b"")
        for address, size in ((program.entry, 1), (program.data_base - 1, 1),
                              (program.data_base, 4), (program.data_base + 3, 1),
                              (-1, 1), (MASK64 + 1, 0), (True, 0),
                              (program.data_base, -1), (program.data_base, 257),
                              (program.data_base, True)):
            with self.subTest(address=address, size=size):
                with self.assertRaises(ValueError):
                    read_memory(program, address, size)

    def test_empty_data_memory_inspection(self):
        program = prepare_trace('_start: syscall\nmsg: db ""', "empty.asm", TraceLimits())
        self.assertEqual(read_memory(program, program.data_base, 0), b"")
        with self.assertRaises(ValueError):
            read_memory(program, program.data_base, 1)


if __name__ == "__main__":
    unittest.main()
