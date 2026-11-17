import {
  activateTab, renderAssembly, renderCPU, renderExamples, renderInstruction,
  renderMemory, renderOutput, renderSourceLines, renderTimeline,
} from "/panels.js";

const $ = (id) => document.getElementById(id);
const state = {
  connected: false, token: null, limits: null, examples: [], sessionId: null, revision: 0,
  boundSessionId: null, assembly: null, view: null, stale: true, edited: false,
  version: 0, busy: false, playing: false, playGeneration: 0, timer: null,
  filename: "loop.asm", selectedAddress: null, diagnostic: null,
  previousCPU: null,
  memory: null, memoryOffset: 0, timeline: null, timelineOffset: 0,
};
const pageNames = ["home", "playground", "instructions", "cli-guide"];

class APIError extends Error {
  constructor(response, status) {
    super(response?.error?.message ?? `Server returned HTTP ${status}`);
    this.status = status;
    this.detail = response?.error;
  }
}

async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 6500);
  try {
    const options = {signal: controller.signal, cache: "no-store", credentials: "same-origin"};
    if (body !== undefined) {
      options.method = "POST";
      options.headers = {"Content-Type": "application/json", "X-ByteForge-Token": state.token};
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok || !result.ok) throw new APIError(result, response.status);
    if (result.schema_version !== 1) throw new Error("Unsupported API schema. Reload after checking the server version.");
    return result;
  } finally {
    clearTimeout(timeout);
  }
}

function notice(message, type = "") {
  $("notice").textContent = message;
  $("notice").className = `notice${type ? ` ${type}` : ""}`;
}

function connected(value) {
  state.connected = value;
  $("connection").className = `connection ${value ? "connected" : "disconnected"}`;
  $("connection-text").textContent = value ? "Local engine connected" : "Engine unavailable";
}

function storeContext(result) {
  state.sessionId = result.session_id;
  state.revision = result.revision;
}

function validTrace() {
  return !state.stale && state.assembly && state.view && state.boundSessionId === state.sessionId;
}

function running() {
  return validTrace() && state.view.cpu.status === "running";
}

function pause() {
  state.playing = false;
  state.playGeneration += 1;
  clearTimeout(state.timer);
  state.timer = null;
  updateControls();
}

function updateControls() {
  const valid = Boolean(validTrace());
  $("assemble").disabled = state.busy || !state.connected;
  $("back").disabled = state.busy || !valid || state.view.cursor === 0 || state.playing;
  $("step").disabled = state.busy || !running() || state.playing;
  $("reset").disabled = state.busy || !valid || state.playing;
  $("play").disabled = !state.playing && (state.busy || !running());
  $("play").textContent = state.playing ? "Ⅱ Pause" : "▶ Play";
  $("play").classList.toggle("active", state.playing);
  const labels = {
    ready: ["Ready", "ready"], running: [state.playing ? "Playing" : "Paused", "running"],
    halted_successfully: [`Exited ${state.view?.cpu?.exit_status ?? ""}`, "halted"],
    unsupported_operation: ["Unsupported", "error"], faulted: ["Faulted", "error"],
    step_limit_reached: ["Step limit", "warning"],
  };
  let [text, style] = valid ? labels[state.view.execution_status] ?? ["Stopped", "warning"]
    : state.edited ? ["Source changed", "warning"] : ["Not assembled", ""];
  if (state.busy) [text, style] = ["Updating…", ""];
  else if (!valid && state.diagnostic) [text, style] = ["Assembly error", "error"];
  $("execution-status").textContent = text;
  $("execution-status").className = `status-badge ${style}`;
  renderMemory(state.memory, state.assembly, state.memoryOffset, state.busy);
  renderTimeline(state.timeline, state.assembly, state.busy);
}

function syncEditorScroll() {
  $("line-numbers").parentElement.scrollTop = $("source").scrollTop;
  $("highlight-viewport").scrollTop = $("source").scrollTop;
}

function sourcePosition() {
  const prefix = $("source").value.slice(0, $("source").selectionStart);
  const lines = prefix.split("\n");
  $("source-position").textContent = `Ln ${lines.length}, Col ${lines.at(-1).length + 1}`;
}

function paint(previousCPU) {
  if (previousCPU !== undefined) state.previousCPU = previousCPU;
  const valid = Boolean(validTrace());
  const assembly = valid ? state.assembly : null;
  const view = valid ? state.view : null;
  renderCPU(view, state.previousCPU, assembly);
  renderAssembly(assembly, view, state.selectedAddress);
  const selected = renderInstruction(assembly, view, state.selectedAddress);
  renderOutput(view);
  const next = view?.cpu?.status === "running"
    ? assembly.instructions.find((item) => item.address === view.cpu.rip) : null;
  const last = view?.last_step;
  renderSourceLines($("source").value, next?.location.line, last?.executed ? last.location?.line : null,
    state.selectedAddress ? selected?.location.line : null,
    state.diagnostic?.location?.line ?? (last && !last.executed ? last.location?.line : null));
  syncEditorScroll();
  updateControls();
}

function invalidate(message, edited = true) {
  pause();
  state.version += 1;
  state.stale = true;
  state.edited = edited;
  state.assembly = null;
  state.view = null;
  state.boundSessionId = null;
  state.selectedAddress = null;
  state.previousCPU = null;
  state.diagnostic = null;
  state.memory = null;
  state.timeline = null;
  state.memoryOffset = state.timelineOffset = 0;
  $("diagnostic").hidden = true;
  notice(message, edited ? "warning" : "");
  paint();
}

function loadExample(name) {
  const example = state.examples.find((item) => item.name === name);
  if (!example) return;
  $("source").value = example.source;
  $("source").setSelectionRange(0, 0);
  $("source").scrollTop = $("source").scrollLeft = 0;
  state.filename = example.filename;
  $("source-filename").textContent = example.filename;
  $("example-select").value = name;
  invalidate(`Loaded ${example.filename}. Assemble to inspect its real bytes and begin simulated tracing.`, false);
  sourcePosition();
}

function route() {
  const requested = location.hash.slice(1) || "home";
  if (!pageNames.includes(requested)) return;
  if (requested !== "playground") pause();
  for (const page of pageNames) $("page-" + page).hidden = page !== requested;
  for (const link of document.querySelectorAll("[data-page]")) {
    if (link.dataset.page === requested) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `ByteForge · ${requested === "playground" ? "TraceLab" : requested === "home" ? "TraceLab Explorer" : requested === "instructions" ? "Instruction Set" : "CLI Guide"}`;
}

function contextQuery(extra = {}) {
  return new URLSearchParams({session_id: state.sessionId, revision: String(state.revision), ...extra});
}

function sameRequest(version, sessionId, revision) {
  return version === state.version && sessionId === state.sessionId && revision === state.revision && !state.stale;
}

async function refreshInspectors(version, followCursor = false) {
  const sessionId = state.sessionId;
  const revision = state.revision;
  if (followCursor) state.timelineOffset = Math.floor(Math.max(0, state.view.cursor - 1) / 50) * 50;
  const timelineURL = `/api/timeline?${contextQuery({offset: state.timelineOffset, limit: 50})}`;
  const size = state.assembly.data_size;
  const memoryURL = size ? `/api/memory?${contextQuery({
    address: `0x${(BigInt(state.assembly.data_base) + BigInt(state.memoryOffset)).toString(16).padStart(16, "0")}`,
    length: Math.min(state.limits.max_memory_bytes, size - state.memoryOffset),
  })}` : null;
  const [timeline, memory] = await Promise.all([api(timelineURL), memoryURL ? api(memoryURL) : null]);
  if (!sameRequest(version, sessionId, revision)) return;
  state.timeline = timeline;
  state.memory = memory?.memory ?? null;
}

function showDiagnostic(detail) {
  state.diagnostic = detail;
  $("diagnostic").hidden = false;
  $("diagnostic-title").textContent = detail.message;
  const loc = detail.location;
  $("diagnostic-text").textContent = loc
    ? `${loc.filename}:${loc.line}:${loc.column}\n${loc.text}\n${" ".repeat(Math.max(0, Math.min(loc.column - 1, loc.text.length)))}^`
    : `${detail.phase ?? "request"} / ${detail.code ?? "error"}`;
  $("diagnostic-jump").hidden = !loc;
}

async function recover(error) {
  pause();
  invalidate(error.status === 409
    ? "The server workspace changed. Refreshing its revision; reassemble this source before tracing."
    : "The request could not be completed. Reassemble before tracing; refreshing the server context.");
  if (error.detail && error.status !== 409) showDiagnostic(error.detail);
  const version = state.version;
  try {
    const [bootstrap, current] = await Promise.all([api("/api/bootstrap"), api("/api/state")]);
    state.token = bootstrap.mutation_token;
    storeContext(current);
    connected(true);
  } catch {
    connected(false);
  }
  if (version !== state.version) return;
  notice(error.status === 409
    ? "Workspace changed in another tab. Server context refreshed; reassemble to continue."
    : `${error.message || "Connection interrupted"}. Check the Python server, then reassemble.`, "error");
  paint();
}

async function mutate(operation, playback = false) {
  if (state.busy || !state.connected) return;
  if (operation !== "assemble" && !validTrace()) return;
  if (!playback) pause();
  const version = state.version;
  const source = $("source").value;
  const filename = state.filename;
  const previousCPU = state.view?.cpu ?? null;
  state.busy = true;
  state.diagnostic = null;
  $("diagnostic").hidden = true;
  updateControls();
  try {
    const common = {session_id: state.sessionId, revision: state.revision};
    const result = operation === "assemble"
      ? await api("/api/assemble", {...common, source, filename})
      : await api("/api/trace", {...common, operation});
    // Even a discarded response may have mutated Python state. Keep its context
    // for the next assembly, but never apply its trace to subsequently edited source.
    storeContext(result);
    if (version !== state.version) return;
    if (operation === "assemble") {
      state.assembly = result.assembly;
      state.boundSessionId = result.session_id;
      state.stale = false;
      state.edited = false;
      state.memoryOffset = state.timelineOffset = 0;
    }
    state.view = result.view;
    state.selectedAddress = null;
    if (operation === "reset") state.memoryOffset = state.timelineOffset = 0;
    await refreshInspectors(version, true);
    if (version !== state.version) return;
    paint(operation === "assemble" ? null : previousCPU);
    const nextInstruction = state.view.cpu.status === "running"
      ? state.assembly.instructions.find((item) => item.address === state.view.cpu.rip) : null;
    keepLineVisible(nextInstruction?.location.line ?? state.view.last_step?.location?.line);
    const fault = state.view.last_step?.fault;
    if (state.view.cpu.status !== "running") {
      pause();
      notice(state.view.cpu.halt_reason === "exit"
        ? `Simulated execution complete · exit status ${state.view.cpu.exit_status}. No native binary was run.`
        : fault?.message ?? `Trace stopped: ${state.view.cpu.halt_reason}.`, fault ? "error" : "success");
    } else if (operation === "assemble") {
      notice(`Assembled ${state.assembly.code_size} code bytes. Ready to step through simulated execution.`, "success");
    } else {
      notice(operation === "reset" ? "Reset to the initial simulated state. History and output cleared."
        : operation === "back" ? "Restored the previous saved state, including output and flags."
        : "Trace state, source position and inspectors synchronized.");
    }
  } catch (error) {
    if (version !== state.version) {
      // Update context only; do not attach an obsolete diagnostic to new source.
      try { storeContext(await api("/api/state")); } catch { connected(false); }
    } else if (operation === "assemble" && error.detail?.phase === "assembly") {
      invalidate("Assembly failed. Correct the source and assemble again.");
      showDiagnostic(error.detail);
      paint();
    } else {
      await recover(error);
    }
  } finally {
    state.busy = false;
    updateControls();
  }
}

async function playbackTick(generation) {
  if (!state.playing || generation !== state.playGeneration || !running()) return;
  await mutate("next", true);
  if (state.playing && generation === state.playGeneration && running()) {
    state.timer = setTimeout(() => playbackTick(generation), Number($("speed").value));
  }
}

function togglePlay() {
  if (state.playing) { pause(); return; }
  if (state.busy || !running()) return;
  state.playing = true;
  const generation = ++state.playGeneration;
  updateControls();
  playbackTick(generation);
}

async function paginate(kind, direction) {
  if (state.busy || !validTrace()) return;
  pause();
  const version = state.version;
  state.busy = true;
  if (kind === "memory") state.memoryOffset = Math.max(0, state.memoryOffset + direction * state.limits.max_memory_bytes);
  else state.timelineOffset = direction < 0 ? Math.max(0, state.timelineOffset - 50) : state.timeline?.next_offset ?? state.timelineOffset;
  updateControls();
  try {
    await refreshInspectors(version);
  } catch (error) {
    if (version === state.version) await recover(error);
  } finally {
    state.busy = false;
    updateControls();
  }
}

function selectInstruction(address) {
  if (!validTrace()) return;
  state.selectedAddress = address;
  paint();
  keepLineVisible(state.assembly.instructions.find((item) => item.address === address)?.location.line);
}

function keepLineVisible(line) {
  if (!line) return;
  const editor = $("source");
  const top = 18 + (line - 1) * 24;
  if (top < editor.scrollTop + 18 || top + 24 > editor.scrollTop + editor.clientHeight - 18) {
    editor.scrollTop = Math.max(0, top - editor.clientHeight / 3);
    syncEditorScroll();
  }
}

function jumpToLocation(location) {
  if (!location) return;
  const lines = $("source").value.split("\n");
  const offset = lines.slice(0, location.line - 1).reduce((sum, line) => sum + line.length + 1, 0) + Math.max(0, location.column - 1);
  $("source").focus();
  $("source").setSelectionRange(offset, offset);
  $("source").scrollTop = Math.max(0, (location.line - 4) * 24);
  syncEditorScroll();
  sourcePosition();
}

$("source").addEventListener("input", () => {
  invalidate("Source changed. The previous trace is invalidated; assemble again before stepping.");
  sourcePosition();
});
$("source").addEventListener("scroll", syncEditorScroll);
$("source").addEventListener("click", sourcePosition);
$("source").addEventListener("keyup", sourcePosition);
$("example-select").addEventListener("change", (event) => loadExample(event.target.value));
$("assemble").addEventListener("click", () => mutate("assemble"));
$("step").addEventListener("click", () => mutate("next"));
$("back").addEventListener("click", () => mutate("back"));
$("reset").addEventListener("click", () => mutate("reset"));
$("play").addEventListener("click", togglePlay);
$("diagnostic-jump").addEventListener("click", () => jumpToLocation(state.diagnostic?.location));
$("memory-prev").addEventListener("click", () => paginate("memory", -1));
$("memory-next").addEventListener("click", () => paginate("memory", 1));
$("timeline-prev").addEventListener("click", () => paginate("timeline", -1));
$("timeline-next").addEventListener("click", () => paginate("timeline", 1));
$("machine-bytes").addEventListener("click", (event) => {
  const button = event.target.closest("[data-address]");
  if (button) selectInstruction(button.dataset.address);
});
$("symbols").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-symbol-address]");
  if (!button || !validTrace() || state.busy) return;
  const address = button.dataset.symbolAddress;
  if (state.assembly.instructions.some((item) => item.address === address)) {
    selectInstruction(address);
    activateTab("bytes");
  } else {
    const offset = BigInt(address) - BigInt(state.assembly.data_base);
    if (offset < 0n || offset >= BigInt(state.assembly.data_size)) return;
    state.memoryOffset = Math.floor(Number(offset) / state.limits.max_memory_bytes) * state.limits.max_memory_bytes;
    activateTab("memory");
    await paginate("memory", 0);
  }
});
for (const tab of document.querySelectorAll("[data-tab]")) {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    const tabs = [...document.querySelectorAll("[data-tab]")];
    const index = tabs.indexOf(tab);
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    activateTab(tabs[next].dataset.tab, true);
  });
}
for (const button of document.querySelectorAll("[data-example]")) {
  button.addEventListener("click", () => {
    loadExample(button.dataset.example);
    location.hash = "playground";
  });
}
window.addEventListener("hashchange", route);
window.addEventListener("keydown", (event) => {
  if (location.hash !== "#playground") return;
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault(); if (!$("assemble").disabled) mutate("assemble");
  } else if (event.key === "F9") {
    event.preventDefault(); togglePlay();
  } else if (event.key === "F10") {
    event.preventDefault();
    const button = event.shiftKey ? $("back") : $("step");
    if (!button.disabled) button.click();
  }
});

async function initialize() {
  paint();
  route();
  try {
    const [bootstrap, examples, current] = await Promise.all([
      api("/api/bootstrap"), api("/api/examples"), api("/api/state"),
    ]);
    state.token = bootstrap.mutation_token;
    state.limits = bootstrap.limits;
    state.examples = examples.examples;
    storeContext(current);
    connected(true);
    const options = state.examples.map((example) => {
      const option = document.createElement("option");
      option.value = example.name;
      option.textContent = example.filename;
      return option;
    });
    $("example-select").replaceChildren(...options);
    $("example-select").disabled = false;
    $("source").disabled = false;
    renderExamples(state.examples, (name) => { loadExample(name); location.hash = "playground"; });
    $("home-source").textContent = state.examples.find((example) => example.name === "loop")?.source ?? "Select an available example.";
    loadExample(state.examples.some((example) => example.name === "loop") ? "loop" : state.examples[0]?.name);
  } catch (error) {
    connected(false);
    notice(`Cannot reach the local engine: ${error.message}. Start the Python server and reload this page.`, "error");
    $("home-source").textContent = "Local engine unavailable. Start python -m byteforge.trace_server and reload.";
    $("example-cards").textContent = "Examples are unavailable until the local Python engine is connected.";
    updateControls();
  }
}

initialize();
