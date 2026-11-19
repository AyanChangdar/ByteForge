// Presentation only: all instructions, addresses and execution effects come from Python.
const byId = (id) => document.getElementById(id);
const registerNames = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp"];
const offsetHex = (value) => `0x${value.toString(16)}`;
const addressAt = (base, offset) => `0x${(BigInt(base) + BigInt(offset)).toString(16).padStart(16, "0")}`;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function table(headers, rows) {
  const result = element("table");
  const head = element("thead");
  const heading = element("tr");
  for (const text of headers) {
    const cell = element("th", text);
    cell.scope = "col";
    heading.append(cell);
  }
  head.append(heading);
  const body = element("tbody");
  for (const row of rows) body.append(row);
  result.append(head, body);
  return result;
}

function empty(id, message) {
  byId(id).replaceChildren(element("p", message, "empty-state"));
}

function instructionAt(assembly, address) {
  return assembly?.instructions.find((item) => item.address === address) ?? null;
}

function instructionText(assembly, address) {
  const item = instructionAt(assembly, address);
  return item ? item.location.text.trim() : "No instruction at this address.";
}

export function renderCPU(view, previousCPU, assembly) {
  const cpu = view?.cpu;
  const cards = registerNames.map((name) => {
    const changed = Boolean(cpu && previousCPU && cpu.registers[name] !== previousCPU.registers[name]);
    const card = element("div", undefined, `register-card${changed ? " changed" : ""}`);
    card.append(element("span", name.toUpperCase()), element("code", cpu?.registers[name] ?? "—"));
    if (changed) card.title = `${name.toUpperCase()} changed from ${previousCPU.registers[name]}`;
    return card;
  });
  byId("registers").replaceChildren(...cards);
  byId("rip").textContent = cpu?.rip ?? "—";
  byId("zf").textContent = cpu ? (cpu.zf ? "1" : "0") : "—";
  byId("zf").classList.toggle("changed", Boolean(cpu && previousCPU && cpu.zf !== previousCPU.zf));
  const next = cpu?.status === "running" ? instructionAt(assembly, cpu.rip) : null;
  byId("next-instruction").textContent = next
    ? `L${next.location.line} · ${next.location.text.trim()}`
    : cpu ? "Execution stopped." : "Assemble to inspect CPU state.";
  const last = view?.last_step;
  byId("last-label").textContent = last && !last.executed ? "LAST ATTEMPT" : "LAST EXECUTED";
  byId("last-instruction").textContent = last?.location
    ? `L${last.location.line} · ${last.location.text.trim()}` : "No instructions executed yet.";
  const branch = byId("branch-info");
  branch.hidden = last?.branch_taken == null;
  branch.classList.toggle("not-taken", last?.branch_taken === false);
  branch.textContent = last?.branch_taken == null ? ""
    : `${last.branch_taken ? "TAKEN" : "NOT TAKEN"} → ${last.branch_target}`;
  byId("step-count").textContent = `${cpu ? BigInt(cpu.step_count).toString() : "0"} executed steps`;
}

export function renderInstruction(assembly, view, selectedAddress) {
  const cpu = view?.cpu;
  const next = cpu?.status === "running" ? instructionAt(assembly, cpu.rip) : null;
  const last = view?.last_step;
  const item = instructionAt(assembly, selectedAddress) ?? next ?? instructionAt(assembly, last?.address);
  byId("selected-source").textContent = item?.location.text.trim() ?? "Source → bytes → CPU state";
  byId("selected-mnemonic").textContent = item?.mnemonic.toUpperCase() ?? "—";
  byId("selected-bytes").textContent = item?.bytes_hex ?? "Awaiting machine code";
  byId("selected-meta").textContent = item
    ? `${item.address} · ${item.size} bytes · file ${offsetHex(item.file_offset)} · line ${item.location.line}, column ${item.location.column}`
    : "Assemble a program, then select an instruction or start stepping.";
  let context = "Exact bytes come from the handwritten Python encoder.";
  if (item) {
    if (selectedAddress) context = "Selected instruction. Selection does not change execution state.";
    else if (next) context = "Next instruction to execute. The source highlight follows the current RIP.";
    else context = last?.executed ? "Last executed instruction. The trace is stopped." : "Last attempted instruction. The trace is stopped.";
    if (last?.address === item.address && last.branch_taken != null) {
      context += ` Last branch: ${last.branch_taken ? "taken" : "not taken"}; target ${last.branch_target}.`;
    }
  }
  byId("selected-context").textContent = context;
  return item;
}

export function renderAssembly(assembly, view, selectedAddress) {
  if (!assembly) {
    empty("machine-bytes", "Assemble your source to inspect its machine code.");
    empty("symbols", "No assembled symbol table.");
    byId("assembly-summary").textContent = "Actual instruction bytes and virtual addresses.";
    return;
  }
  byId("assembly-summary").textContent = `${assembly.instructions.length} instructions · ${assembly.code_size} code bytes · ${assembly.data_size} data bytes · ${assembly.elf.file_size} ELF bytes`;
  const nextAddress = view?.cpu?.status === "running" ? view.cpu.rip : null;
  const lastAddress = view?.last_step?.executed ? view.last_step.address : null;
  const rows = assembly.instructions.map((item) => {
    const row = element("tr");
    row.classList.toggle("is-next", item.address === nextAddress);
    row.classList.toggle("is-last", item.address === lastAddress);
    row.classList.toggle("is-selected", item.address === selectedAddress);
    const sourceCell = element("td");
    const select = element("button", item.location.text.trim(), "table-button");
    select.dataset.address = item.address;
    select.title = `Select line ${item.location.line}: ${item.location.text}`;
    sourceCell.append(select);
    const line = element("td", String(item.location.line));
    if (item.address === nextAddress) line.append(element("span", "Next", "row-state"));
    else if (item.address === lastAddress) line.append(element("span", "Last", "row-state last"));
    row.append(line, element("td", item.address), sourceCell,
      element("td", item.bytes_hex, "byte-cell"), element("td", String(item.size)));
    return row;
  });
  byId("machine-bytes").replaceChildren(table(["LINE", "VIRTUAL ADDRESS", "SOURCE", "MACHINE BYTES", "SIZE"], rows));
  const symbols = assembly.symbols.map((item) => {
    const row = element("tr");
    const name = element("td");
    const button = element("button", item.name, "table-button");
    button.dataset.symbolAddress = item.address;
    button.title = `Inspect symbol ${item.name}`;
    name.append(button);
    row.append(name, element("td", item.address), element("td", offsetHex(item.file_offset)));
    return row;
  });
  byId("symbols").replaceChildren(table(["SYMBOL", "VIRTUAL ADDRESS", "FILE OFFSET"], symbols));
}

export function renderMemory(memory, assembly, pageOffset, busy) {
  const total = assembly?.data_size ?? 0;
  byId("memory-prev").disabled = busy || !total || pageOffset === 0;
  byId("memory-next").disabled = busy || !total || !memory || pageOffset + memory.data.length >= total;
  byId("memory-summary").textContent = total
    ? `Read-only DB data · bytes ${pageOffset + 1}–${pageOffset + (memory?.data.length ?? 0)} of ${total}`
    : "Read-only bytes defined by DB directives.";
  if (!memory || !total) {
    empty("memory", assembly ? "This program has no DB data." : "Assemble a program with DB data to inspect memory.");
    return;
  }
  const bytes = memory.data.hex ? memory.data.hex.split(" ") : [];
  const rows = [];
  for (let offset = 0; offset < bytes.length; offset += 16) {
    const chunk = bytes.slice(offset, offset + 16);
    const ascii = chunk.map((value) => {
      const byte = parseInt(value, 16);
      return byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : ".";
    }).join("");
    const row = element("tr");
    row.append(element("td", addressAt(memory.address, offset)),
      element("td", offsetHex(memory.file_offset + offset)), element("td", chunk.join(" "), "byte-cell"), element("td", ascii));
    rows.push(row);
  }
  byId("memory").replaceChildren(table(["VIRTUAL ADDRESS", "FILE OFFSET", "BYTES", "ASCII"], rows));
}

export function renderOutput(view) {
  const output = view?.stdout;
  let text = "No simulated output yet.";
  let format = "UTF-8 preview · raw bytes available below";
  if (output?.length) {
    const bytes = Uint8Array.from(output.hex.split(" "), (value) => parseInt(value, 16));
    try {
      text = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
      if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)) throw new Error("Binary control bytes");
    } catch {
      text = output.display;
      format = "Escaped binary output · exact raw bytes available below";
    }
  }
  byId("output").textContent = text;
  byId("output").classList.toggle("empty-state", !output?.length);
  byId("output-count").textContent = `${output?.length ?? 0} B`;
  byId("output-summary").textContent = output?.length ? format : "Captured simulated stdout · no native process";
  byId("output-raw").textContent = output?.length ? `${output.hex}\n\nEscaped: ${output.display}` : "No output bytes.";
  byId("exit-status").textContent = view?.cpu?.exit_status == null ? "Not exited" : `Simulated exit status ${view.cpu.exit_status}`;
}

export function renderTimeline(page, assembly, busy) {
  byId("timeline-prev").disabled = busy || !page || page.offset === 0;
  byId("timeline-next").disabled = busy || page?.next_offset == null;
  byId("timeline-summary").textContent = page?.total
    ? `Events ${page.offset + 1}–${page.offset + page.events.length} of ${page.total} · history cursor ${page.cursor}`
    : "Recorded transitions from the shared trace engine.";
  if (!page?.events.length) {
    empty("timeline", "Step through a program to build its timeline.");
    return;
  }
  const rows = page.events.map((event) => {
    const result = event.step;
    const row = element("tr");
    row.classList.toggle("timeline-future", !event.visible);
    row.classList.toggle("timeline-current", event.index === page.cursor - 1);
    const state = !event.visible ? "Saved future" : event.index === page.cursor - 1 ? "Current" : "Recorded";
    const effect = result.fault?.message ?? (result.branch_taken == null
      ? result.changed_registers.map((name) => name.toUpperCase()).join(", ") || "No register changes"
      : `${result.branch_taken ? "TAKEN" : "NOT TAKEN"} → ${result.branch_target}`);
    row.append(element("td", String(event.index + 1)), element("td", state),
      element("td", result.address), element("td", instructionText(assembly, result.address)), element("td", effect));
    return row;
  });
  byId("timeline").replaceChildren(table(["EVENT", "HISTORY", "ADDRESS", "INSTRUCTION", "EFFECT"], rows));
}

export function renderSourceLines(source, nextLine, lastLine, selectedLine, errorLine) {
  const count = source.split("\n").length;
  const numbers = byId("line-numbers");
  const rows = byId("highlight-rows");
  if (numbers.childElementCount !== count) {
    numbers.replaceChildren(...Array.from({length: count}, (_, index) => element("span", String(index + 1))));
    rows.replaceChildren(...Array.from({length: count}, () => element("div", undefined, "source-row")));
  }
  for (let index = 0; index < count; index += 1) {
    for (const node of [numbers.children[index], rows.children[index]]) {
      node.classList.toggle("is-next", index + 1 === nextLine);
      node.classList.toggle("is-last", index + 1 === lastLine);
      node.classList.toggle("is-selected", index + 1 === selectedLine);
      node.classList.toggle("is-error", index + 1 === errorLine);
    }
  }
}

export function activateTab(name, focus = false) {
  for (const tab of document.querySelectorAll("[data-tab]")) {
    const active = tab.dataset.tab === name;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    byId(`panel-${tab.dataset.tab}`).hidden = !active;
    if (active && focus) tab.focus();
  }
}

export function renderExamples(examples, onChoose) {
  const descriptions = {
    hello: ["Hello, World!", "Follow a string from DB memory to a simulated Linux write."],
    exit: ["Minimal exit", "Three instructions. One native executable. Exit status 7."],
    arithmetic: ["Arithmetic", "Watch registers and ZF change with XOR, ADD, SUB and CMP."],
    loop: ["Counting loop", "Follow RCX from three to zero through a backward branch."],
    conditional: ["Conditional branches", "Compare a taken JE with a non-taken JNE."],
  };
  const ordered = ["hello", "arithmetic", "loop", "conditional", "exit"];
  const cards = ordered.map((name, index) => {
    const example = examples.find((item) => item.name === name);
    if (!example) return null;
    const [title, description] = descriptions[name];
    const button = element("button", undefined, "example-card");
    button.append(element("span", `0${index + 1} / ${example.filename}`, "card-number"),
      element("strong", title), element("p", description), element("span", "Open example →", "card-link"));
    button.addEventListener("click", () => onChoose(name));
    return button;
  }).filter(Boolean);
  byId("example-cards").replaceChildren(...cards);
}
