# ByteForge screenshot index

All ten PNGs below are genuine, manually captured application or terminal
states. They are linked from the main README beside the relevant workflows.
Their image contents are unchanged; filenames were normalized and SHA-256
hashes verified. Numbers identify captures, not their order in the README.

## Prepare

- From the repository root, run `python -m byteforge.trace_server --port 8765`
  and open `http://127.0.0.1:8765` in your browser.
- Use the existing examples without editing them. Assemble before tracing;
  use Reset when a fresh initial state is required. Keep Play paused for
  exact instruction counts and wait for each Step request to finish.
- Use a consistent desktop viewport and readable zoom. Keep the relevant
  labels, addresses and execution status visible; scroll or use a full-page
  capture when necessary. Hide unrelated windows and private information.
- Capture the real UI. Do not mock CPU values, alter the DOM, generate
  placeholder images or show planned Stage 6C features as implemented.

## Completed captures

| Filename | Verified visible content |
|---|---|
| `01-dashboard.png` | Home branding, navigation, Open Playground, source preview and the beginning of the example cards; local engine connected. |
| `02-playground.png` | Assembled `loop.asm` during playback, editor, toolbar and eight simulated registers. Four executed steps, RCX=2 and ZF=0; this is not the initial state. |
| `03-loop-taken.png` | Four executed instructions: RCX=2, ZF=0, next RIP=`0x40100A`, last `jne loop` TAKEN; next is `sub rcx, 1`. |
| `04-loop-termination.png` | Ten executed instructions: RCX=0, ZF=1, next RIP=`0x40101E`, last `jne loop` NOT TAKEN; exit setup is next, not yet executed. |
| `05-machine-bytes.png` | All six `arithmetic.asm` instructions with actual addresses, bytes and sizes: 36 code bytes and 4,132 ELF bytes. No opcode-field microscope is implied. |
| `06-symbol-table.png` | Hello World symbols `_start=0x401000` and `msg=0x401040`, at file offsets `0x1000` and `0x1040`. |
| `07-db-memory.png` | Hello World read-only DB row at `0x401040` / file offset `0x1040`, all 14 bytes and ASCII preview. |
| `08-hello-output.png` | Hello World after eight simulated instructions: 14 output bytes, visible greeting and simulated exit status 0. |
| `09-elf-native.png` | Ubuntu/WSL `file` and `readelf` output: ELF64 little-endian x86-64 ET_EXEC, entry `0x401000`, RX LOAD segment; native greeting and exit status 0. |
| `10-cli-trace.png` | Interactive terminal `next` commands, real encoded-byte metadata, taken JNE and `registers` output after four simulated instructions. |

Labels, blank lines and comments do not count as executed instructions. Stop
at the specified loop counts before the exit syscall: modeled SYSCALL changes
RCX. Keep next-to-execute and last-executed highlights distinguishable.

Expected Hello World DB bytes:

```text
48 65 6C 6C 6F 2C 20 57 6F 72 6C 64 21 0A
```

The final `0A` is a newline; the memory ASCII column represents it with a dot.
Browser and debugger captures show simulated CPU state, not hardware
registers. Only `09-elf-native.png` records native Linux execution evidence.

## Capture review checklist

- For replacements, repeat the preparation above and use the same exact names.
- Check PNG validity, readability, visible state and the matching README caption.
- Review browser chrome and terminal paths for private information before sharing.
- Keep simulation distinct from native execution. Do not imply Stage 6C exists.
- Verify relative image links in the main README and run `git diff --check`.
