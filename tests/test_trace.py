"""Tests for TraceLab contracts and the bounded typed-instruction simulator."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import unittest
from unittest.mock import patch

from byteforge.elf import build_executable
from byteforge.model import ByteForgeError, ElfConfig, SourceLocation
from byteforge.parser import parse
from byteforge.resolver import assemble
from byteforge.trace import initial_state, prepare_trace, step
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
        blocked = step(program, state)
        self.assertEqual(blocked.fault.code, "unsupported_syscall")
        self.assertFalse(blocked.executed)
        self.assertEqual(blocked.after.step_count, 12)


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
                         registers={**dict.fromkeys(REGISTER_NAMES, 0), "rax": 60, "rcx": 9})
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
        self.assertFalse(hasattr(result.after, "stdout"))
        self.assertFalse(hasattr(result.after, "exit_status"))

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


if __name__ == "__main__":
    unittest.main()
