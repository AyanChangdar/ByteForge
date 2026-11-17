"""Interactive terminal interface for simulated tracing; never executes ELF files."""

import argparse
from pathlib import Path
import shlex

from byteforge.model import ByteForgeError
from byteforge.trace import TraceSession, prepare_trace, read_memory
from byteforge.trace_model import REGISTER_NAMES, TraceLimits, TraceStatus, TraceStep


HELP = """Commands:
  next / n                 Step forward (or replay saved history)
  back                     Restore the previous saved state
  registers                Show all eight registers, RIP and ZF
  memory ADDRESS [LENGTH]  Inspect DB bytes (decimal or 0x values; default 16)
  continue / c [STEPS]     Advance up to 100 steps (default 100)
  reset                    Clear history/output and restore the initial state
  help                     Show commands
  quit / q                 Leave the debugger, not a simulated Linux exit"""


def _show(session: TraceSession, transition: TraceStep | None = None) -> None:
    view = session.view()
    state = view.state
    if transition is not None:
        if transition.location is not None:
            location = transition.location
            print(f"Source {location.filename}:{location.line}:{location.column}: {location.text!r}")
        print(f"Instruction 0x{transition.address:016X}: {transition.data.hex(' ').upper()}")
        print(f"RIP 0x{transition.before.rip:016X} -> 0x{transition.after.rip:016X}; "
              f"ZF {int(transition.before.zf)} -> {int(transition.after.zf)}")
        changes = ", ".join(f"{name.upper()}=0x{transition.after.registers[name]:016X}"
                            for name in transition.changed_registers)
        print(f"Changed registers: {changes or 'none'}")
        if transition.branch_taken is not None:
            decision = "TAKEN" if transition.branch_taken else "NOT TAKEN"
            print(f"Branch {decision}; target 0x{transition.branch_target:016X}")
        if transition.fault is not None:
            print(f"Fault {transition.fault.code}: {transition.fault.message}")
    print(f"State: {state.status.value}; RIP=0x{state.rip:016X}; ZF={int(state.zf)}; "
          f"steps={state.step_count}; history={view.cursor}/{view.history_size}; "
          f"reason={state.halt_reason or 'none'}")
    print(f"Captured stdout: {view.stdout!r}")
    if state.exit_status is not None:
        print(f"Simulated exit status: {state.exit_status} (separate from debugger exit code)")
    current = session.program.instructions.get(state.rip)
    if current is not None and state.status is TraceStatus.RUNNING:
        print(f"Next source line {current.statement.location.line}: {current.statement.location.text!r}")


def _command(session: TraceSession, text: str) -> bool:
    """Run one command; False requests a normal debugger exit."""
    words = shlex.split(text)
    if not words:
        return True
    name, args = words[0].lower(), words[1:]
    if name in ("memory", "continue", "c"):
        if name == "memory":
            if not 1 <= len(args) <= 2:
                raise ValueError("Usage: memory ADDRESS [LENGTH]")
            address = int(args[0], 0)
            length = int(args[1], 0) if len(args) == 2 else 16
            data = read_memory(session.program, address, length)
            print(f"DB memory 0x{address:016X}: {data.hex(' ').upper()}  {data!r}")
        else:
            if len(args) > 1:
                raise ValueError("Usage: continue [STEPS]")
            view = session.continue_(int(args[0], 0) if args else 100)
            _show(session, view.last_step)
        return True
    if args:
        raise ValueError(f"Command {name!r} takes no arguments")
    if name in ("quit", "q"):
        return False
    if name in ("next", "n"):
        _show(session, session.next())
    elif name == "back":
        print("Restored previous saved state.")
        _show(session, session.back().last_step)
    elif name == "reset":
        session.reset()
        print("Reset to the initial simulated state.")
        _show(session)
    elif name == "registers":
        state = session.view().state
        for register in REGISTER_NAMES:
            print(f"{register.upper()}=0x{state.registers[register]:016X}")
        _show(session)
    elif name == "help":
        print(HELP)
    else:
        raise ValueError(f"Unknown command {name!r}; use help")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ByteForge TraceLab simulated terminal debugger")
    parser.add_argument("source", type=Path, help="Assembly source file")
    parser.add_argument("--interactive", action="store_true", required=True,
                        help="Start the simulated debugger; never execute native code")
    args = parser.parse_args(argv)
    try:
        limits = TraceLimits()
        with args.source.open("rb") as source_file:
            raw = source_file.read(limits.max_source_bytes + 1)
        if len(raw) > limits.max_source_bytes:
            raise ValueError(f"Source exceeds {limits.max_source_bytes} UTF-8 bytes")
        session = TraceSession(prepare_trace(raw.decode("utf-8"), str(args.source), limits))
        print("ByteForge TraceLab - SIMULATED CPU STATE; no native ELF execution")
        print(f"Entry: 0x{session.program.entry:016X}")
        print("Type help for commands. Debugger quit/EOF returns 0 independently of simulated exit status.")
        _show(session)
        while True:
            try:
                command = input("tracelab> ")
            except EOFError:
                print("End of input; leaving debugger.")
                return 0
            try:
                if not _command(session, command):
                    return 0
            except ValueError as error:
                print(f"Command error: {error}")
    except KeyboardInterrupt:
        print("\nDebugger interrupted.")
        return 130
    except (ByteForgeError, OSError, UnicodeError, ValueError) as error:
        print(f"TraceLab error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
