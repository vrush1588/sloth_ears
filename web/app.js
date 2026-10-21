"use strict";

const $ = (selector) => document.querySelector(selector);
const MAX_TEXT_CHARS = 200000;
const DEFAULT_MODEL = "gemma3:4b";
const MODE_COPY = {
  media: {
    heading: "Choose your listening nook",
    subheading: "Point Sloth Gist at one recording, or a folder to find recent ones.",
    button: "Summarize while I nap",
  },
  text: {
    heading: "Hand over something to read",
    subheading: "Paste text or open a .txt or .md file. Only the digest is saved.",
    button: "Read it while I nap",
  },
};
const state = {
  digest: [],
  mode: "media",
  loadedFile: null,
  polling: false,
  pollTimer: null,
  toastTimer: null,
  status: null,
  completed: readCompletedActions(),
};

function readCompletedActions() {
  try {
    // Fall back to the key used before the rename from Sloth Ears.
    const raw = localStorage.getItem("sloth-gist-completed")
      ?? localStorage.getItem("sloth-ears-completed");
    const saved = JSON.parse(raw || "{}");
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  } catch {
    return {};
  }
}

function saveCompletedActions() {
  try {
    localStorage.setItem("sloth-gist-completed", JSON.stringify(state.completed));
  } catch {
    showToast("Action checkmarks will last for this visit only.");
  }
}

function escapeKey(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, (character) =>
    `_${character.codePointAt(0).toString(16)}_`,
  );
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const contentType = response.headers.get("Content-Type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message = payload && typeof payload === "object" ? payload.error : payload;
    throw new Error(message || `Request failed (${response.status}).`);
  }
  return payload;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2600);
}

function switchView(view) {
  const digest = view === "digest";
  $("#drop-view").hidden = digest;
  $("#digest-view").hidden = !digest;
  $("#nav-drop").classList.toggle("is-active", !digest);
  $("#nav-digest").classList.toggle("is-active", digest);
  $("#nav-drop").toggleAttribute("aria-current", !digest);
  $("#nav-digest").toggleAttribute("aria-current", digest);
  if (digest) refreshDigest();
}

function setSettingsOpen(open) {
  $("#settings-popover").hidden = !open;
  $("#popover-backdrop").hidden = !open;
  $("#settings-button").setAttribute("aria-expanded", String(open));
  if (open) $("#whisper-model").focus();
}

function updateOfflineBadge() {
  $("#offline-badge").hidden = !$("#offline-toggle").checked;
}

function modelGroup(label, models) {
  const group = document.createElement("optgroup");
  group.label = label;
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model.name;
    option.textContent = model.name;
    group.append(option);
  }
  return group;
}

async function loadModels() {
  const hint = $("#models-hint");
  const select = $("#ai-model");
  try {
    const result = await api("/api/models");
    const models = (Array.isArray(result.models) ? result.models : [])
      .filter((model) => typeof model?.name === "string" && model.name);
    if (!models.length) {
      hint.textContent = "No local Ollama models found yet.";
      return;
    }
    // The server already lists gemma models first and flags them.
    const recommended = models.filter((model) => model.recommended);
    const others = models.filter((model) => !model.recommended);
    const groups = [];
    if (recommended.length) groups.push(modelGroup("Recommended for multilingual", recommended));
    if (others.length) {
      groups.push(modelGroup(recommended.length ? "Other local models" : "Local models", others));
    }
    select.replaceChildren(...groups);
    const names = models.map((model) => model.name);
    select.value = recommended[0]?.name
      ?? (names.includes(DEFAULT_MODEL) ? DEFAULT_MODEL : names[0]);
    hint.textContent = `${models.length} model${models.length === 1 ? "" : "s"} available in local Ollama.`;
  } catch (error) {
    hint.textContent = `Local Ollama models are unavailable: ${error.message}`;
  }
}

async function loadWhisperModels() {
  const select = $("#whisper-model");
  try {
    const result = await api("/api/whisper-models");
    const models = Array.isArray(result.models) ? result.models : [];
    const downloaded = new Set(models.filter((model) => model.downloaded).map((model) => model.name));
    for (const option of select.options) {
      option.dataset.label ??= option.textContent;
      const known = models.some((model) => model.name === option.value);
      option.textContent = known && !downloaded.has(option.value)
        ? `${option.dataset.label} · not downloaded`
        : option.dataset.label;
    }
    if (downloaded.size && !downloaded.has(select.value)) {
      // Prefer the recommended size, then the next most accurate one.
      select.value = ["small", "base", "medium", "tiny"].find((name) => downloaded.has(name));
    }
  } catch {
    // Keep the static list; a run will report a missing model clearly.
  }
}

function setMode(mode) {
  state.mode = mode === "text" ? "text" : "media";
  const text = state.mode === "text";
  for (const tab of document.querySelectorAll(".mode-tab")) {
    const active = tab.dataset.mode === state.mode;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  $("#media-panel").hidden = text;
  $("#text-panel").hidden = !text;
  $("#time-range-field").hidden = text;
  const copy = MODE_COPY[state.mode];
  $("#run-heading").textContent = copy.heading;
  $("#run-subheading").textContent = copy.subheading;
  $("#run-button-label").textContent = copy.button;
}

function onModeKeydown(event) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = state.mode === "media" ? "text" : "media";
  const target = event.key === "Home" ? "media" : event.key === "End" ? "text" : next;
  setMode(target);
  $(`#tab-${target}`).focus();
}

function updateCharCount() {
  const length = $("#text-input").value.length;
  const counter = $("#char-count");
  counter.textContent = `${length.toLocaleString()} / ${MAX_TEXT_CHARS.toLocaleString()} characters`;
  counter.classList.toggle("is-over", length > MAX_TEXT_CHARS);
  const file = state.loadedFile;
  const fromFile = file && file.text === $("#text-input").value;
  $("#text-file-name").textContent = fromFile ? file.name : "";
}

async function loadTextFile() {
  const input = $("#text-file");
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (!/\.(txt|md)$/i.test(file.name)) {
    showToast("Please choose a .txt or .md file.");
    return;
  }
  try {
    const text = await file.text();
    if (text.length > MAX_TEXT_CHARS) {
      showToast(`That file is longer than ${MAX_TEXT_CHARS.toLocaleString()} characters.`);
      return;
    }
    state.loadedFile = { name: file.name, text };
    $("#text-input").value = text;
    updateCharCount();
  } catch (error) {
    showToast(`Could not read that file: ${error.message}`);
  }
}

function setStatus(status) {
  state.status = status;
  if (status.storage_path) $("#storage-path").textContent = status.storage_path;

  const running = status.state === "running";
  $("#run-form-card").hidden = running;
  $("#progress-card").hidden = !running && status.state !== "done";
  $("#notice").hidden = !status.error;
  if (status.error) $("#notice").textContent = status.error;

  if (running || status.state === "done") {
    const total = Number(status.total) || 0;
    const done = Number(status.done) || 0;
    const inProgress = running && status.stage && status.stage !== "done";
    const visibleCount = Math.min(total, done + (inProgress ? 1 : 0));
    const ratio = total ? visibleCount / total : 0;
    $("#file-count").textContent = `${visibleCount} of ${total} file${total === 1 ? "" : "s"}`;
    $("#progress-percent").textContent = `${Math.round(ratio * 100)}%`;
    $("#progress-fill").style.width = `${ratio * 100}%`;
    $("#sloth-marker").style.left = `${Math.max(0, Math.min(100, ratio * 100))}%`;
    $("#current-file").textContent = status.current_file || "Getting your recordings ready…";
    $("#current-stage").textContent = running
      ? stageLabel(status.stage)
      : "Digest ready";
    $("#nap-status").textContent = running
      ? (status.mode === "text" ? "Reading softly" : "Listening softly")
      : "Nap complete";
    $(".progress-card h2").textContent = running ? "Taking it slow." : "A lovely little rest.";
    const items = status.mode === "text" ? "words" : "recordings";
    $(".progress-subtitle").textContent = running
      ? `Your ${items} are in good paws.`
      : `Your ${items} have been gathered into a digest.`;
    ensureDigestButton();
  } else {
    removeDigestButton();
  }

  if (running) {
    startPolling();
  } else {
    stopPolling();
    if (status.state === "done") refreshDigest();
  }
}

function stageLabel(stage) {
  if (stage === "reading") return "Reading";
  if (stage === "listening") return "Listening";
  if (stage === "summarizing") return "Writing a thoughtful summary";
  if (stage === "done") return "Finished";
  return "Getting things ready";
}

function ensureDigestButton() {
  let button = $("#progress-digest-button");
  if (button) return;
  button = document.createElement("button");
  button.className = "secondary-button";
  button.id = "progress-digest-button";
  button.type = "button";
  button.textContent = "View your digest";
  button.addEventListener("click", () => switchView("digest"));
  button.style.marginTop = "14px";
  $(".progress-card").append(button);
}

function removeDigestButton() {
  $("#progress-digest-button")?.remove();
}

function startPolling() {
  if (state.polling) return;
  state.polling = true;
  state.pollTimer = setInterval(pollStatus, 1000);
}

function stopPolling() {
  state.polling = false;
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function pollStatus() {
  try {
    setStatus(await api("/api/status"));
  } catch (error) {
    $("#notice").hidden = false;
    $("#notice").textContent = `Could not check progress: ${error.message}`;
  }
}

function formatDuration(seconds) {
  const duration = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(duration / 3600);
  const minutes = Math.floor((duration % 3600) / 60);
  const remainder = duration % 60;
  if (hours) return `${hours} hr ${minutes} min`;
  if (minutes) return `${minutes} min`;
  return `${remainder} sec`;
}

function fileName(path) {
  return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "Recording";
}

function createIcon(markup, className = "") {
  const span = document.createElement("span");
  span.className = className;
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = markup;
  return span;
}

function actionKey(entry, cardIndex, actionIndex, action) {
  if (entry.id) return `${entry.id}|${actionIndex}`;
  return `${entry.file || ""}|${cardIndex}|${actionIndex}|${action}`;
}

function entryWhen(entry) {
  const time = entry.time || "Time unavailable";
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(entry.date || "");
  if (!match) return time;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (sameDay(date, today)) return `Today · ${time}`;
  if (sameDay(date, yesterday)) return `Yesterday · ${time}`;
  const options = { day: "numeric", month: "short" };
  if (date.getFullYear() !== today.getFullYear()) options.year = "numeric";
  return `${date.toLocaleDateString(undefined, options)} · ${time}`;
}

function entryLength(entry) {
  if (entry.kind !== "text") return formatDuration(entry.duration_seconds);
  const words = Math.max(0, Number(entry.words) || 0);
  return `${words.toLocaleString()} word${words === 1 ? "" : "s"}`;
}

function emptySummary(entry) {
  return entry.kind === "text" ? "No text found." : "No speech detected.";
}

function makeDigestCard(entry, cardIndex) {
  const card = document.createElement("article");
  card.className = "digest-card";

  const heading = document.createElement("div");
  heading.className = "card-heading";
  const fileInfo = document.createElement("div");
  fileInfo.className = "file-title";
  fileInfo.append(createIcon(
    "<svg viewBox='0 0 24 24'><path d='M6 3.5h8l4 4V20a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 20V5a1.5 1.5 0 0 1 2-1.5Z'/><path d='M14 3.5V8h4M8 13h8m-8 4h8'/></svg>",
    "file-icon",
  ));
  const nameBlock = document.createElement("div");
  nameBlock.style.minWidth = "0";
  const title = document.createElement("h2");
  title.textContent = fileName(entry.file);
  title.title = fileName(entry.file);
  const metadata = document.createElement("p");
  metadata.className = "file-meta";
  metadata.textContent = `${entryWhen(entry)} · ${entryLength(entry)}`;
  nameBlock.append(title, metadata);
  fileInfo.append(nameBlock);
  heading.append(fileInfo);
  card.append(heading);

  const points = Array.isArray(entry.points) ? entry.points : [];
  if (entry.summary || !points.length) {
    const summary = document.createElement("p");
    summary.className = "summary-text";
    summary.textContent = entry.summary || emptySummary(entry);
    card.append(summary);
  }
  if (points.length) {
    const label = document.createElement("p");
    label.className = "points-label";
    label.textContent = "Key points";
    const list = document.createElement("ul");
    list.className = "point-list";
    for (const point of points) {
      const item = document.createElement("li");
      item.textContent = String(point);
      list.append(item);
    }
    card.append(label, list);
  }

  const actions = Array.isArray(entry.actions) ? entry.actions : [];
  if (actions.length) {
    const list = document.createElement("ul");
    list.className = "todo-list";
    actions.forEach((action, actionIndex) => {
      const item = document.createElement("li");
      item.className = "todo-item";
      const checkbox = document.createElement("input");
      const key = actionKey(entry, cardIndex, actionIndex, action);
      checkbox.type = "checkbox";
      checkbox.id = `todo-${escapeKey(key)}`;
      checkbox.checked = Boolean(state.completed[key]);
      const label = document.createElement("label");
      label.htmlFor = checkbox.id;
      label.textContent = String(action);
      item.classList.toggle("is-complete", checkbox.checked);
      checkbox.addEventListener("change", () => {
        state.completed[key] = checkbox.checked;
        item.classList.toggle("is-complete", checkbox.checked);
        saveCompletedActions();
        updateActionCount();
      });
      item.append(checkbox, label);
      list.append(item);
    });
    card.append(list);
  }
  return card;
}

function updateActionCount() {
  const boxes = [...document.querySelectorAll(".todo-item input[type=checkbox]")];
  const completed = boxes.filter((box) => box.checked).length;
  const total = boxes.length;
  $("#action-count").textContent = `${completed} of ${total} action item${total === 1 ? "" : "s"} completed`;
  $("#tracker-fill").style.width = `${total ? (completed / total) * 100 : 0}%`;
}

async function refreshDigest() {
  try {
    const result = await api("/api/digest");
    state.digest = Array.isArray(result) ? result : [];
    renderDigest();
  } catch (error) {
    if (error.message.includes("No saved digest")) {
      state.digest = [];
      renderDigest();
      return;
    }
    $("#notice").hidden = false;
    $("#notice").textContent = `Could not load the digest: ${error.message}`;
  }
}

function renderDigest() {
  const list = $("#digest-list");
  list.replaceChildren();
  const hasDigest = state.digest.length > 0;
  $("#empty-state").hidden = hasDigest;
  $("#action-tracker").hidden = !hasDigest;
  $("#copy-button").disabled = !hasDigest;
  $("#export-button").disabled = !hasDigest;
  $("#wipe-button").disabled = !hasDigest;
  if (!hasDigest) {
    $("#digest-meta").textContent = "";
    updateActionCount();
    return;
  }
  // Newest first; cards keep their saved index so older checkmarks still match.
  for (const [index, entry] of [...state.digest.entries()].reverse()) {
    if (entry && typeof entry === "object") list.append(makeDigestCard(entry, index));
  }
  const texts = state.digest.filter((entry) => entry?.kind === "text").length;
  const recordings = state.digest.length - texts;
  const parts = [];
  if (recordings) parts.push(`${recordings} recording${recordings === 1 ? "" : "s"}`);
  if (texts) parts.push(`${texts} text${texts === 1 ? "" : "s"}`);
  $("#digest-meta").textContent = `${parts.join(" · ")} · Stored locally`;
  updateActionCount();
}

function createMarkdown() {
  const lines = ["# Your Sloth Gist digest", ""];
  for (const entry of state.digest) {
    lines.push(`## ${fileName(entry.file)} (${entryWhen(entry)} · ${entryLength(entry)})`);
    const points = Array.isArray(entry.points) ? entry.points : [];
    if (entry.summary || !points.length) lines.push("", entry.summary || emptySummary(entry));
    if (points.length) lines.push("", ...points.map((point) => `- ${point}`), "");
    for (const [actionIndex, action] of (entry.actions || []).entries()) {
      const key = actionKey(entry, state.digest.indexOf(entry), actionIndex, action);
      lines.push(`- [${state.completed[key] ? "x" : " "}] ${action}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

async function copyDigest() {
  if (!state.digest.length) return;
  const text = createMarkdown();
  try {
    await navigator.clipboard.writeText(text);
    showToast("Digest copied to your clipboard.");
  } catch {
    const temporary = document.createElement("textarea");
    temporary.value = text;
    temporary.setAttribute("readonly", "");
    temporary.style.position = "fixed";
    temporary.style.opacity = "0";
    document.body.append(temporary);
    temporary.select();
    const copied = document.execCommand("copy");
    temporary.remove();
    showToast(copied ? "Digest copied to your clipboard." : "Could not copy the digest.");
  }
}

function exportDigest() {
  if (!state.digest.length) return;
  const file = new Blob([createMarkdown()], { type: "text/markdown;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(file);
  link.download = "sloth-gist-digest.md";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
  showToast("Your Markdown digest is ready.");
}

async function wipeDigest() {
  if (!state.digest.length || !window.confirm("Wipe the saved digest from this device?")) return;
  try {
    await api("/api/digest", { method: "DELETE" });
    state.digest = [];
    state.completed = {};
    saveCompletedActions();
    renderDigest();
    showToast("Digest wiped from this device.");
  } catch (error) {
    showToast(`Could not wipe the digest: ${error.message}`);
  }
}

function buildRunPayload() {
  const shared = {
    mode: state.mode,
    style: $("#output-style").value,
    language: $("#language").value || null,
    ai_model: $("#ai-model").value || DEFAULT_MODEL,
  };
  if (state.mode === "text") {
    const text = $("#text-input").value;
    if (!text.trim()) {
      $("#text-input").focus();
      return null;
    }
    if (text.length > MAX_TEXT_CHARS) {
      showToast(`Please keep text under ${MAX_TEXT_CHARS.toLocaleString()} characters.`);
      $("#text-input").focus();
      return null;
    }
    const file = state.loadedFile;
    const name = file && file.text === text ? file.name : "Pasted text";
    return { ...shared, text, name };
  }
  // Windows "Copy as path" wraps the path in quotes.
  const folder = $("#folder-path").value.trim().replace(/^(["'])(.*)\1$/, "$2").trim();
  if (!folder) {
    $("#folder-path").focus();
    return null;
  }
  const timeValue = $("#time-range").value;
  return {
    ...shared,
    folder,
    since_hours: timeValue === "all" ? 1000000000 : Number(timeValue),
    whisper_model: $("#whisper-model").value,
    offline: $("#offline-toggle").checked,
  };
}

async function startRun(event) {
  event.preventDefault();
  const payload = buildRunPayload();
  if (!payload) return;
  $("#notice").hidden = true;
  $("#run-button").disabled = true;
  try {
    await api("/api/run", { method: "POST", body: JSON.stringify(payload) });
    switchView("drop");
    setStatus(await api("/api/status"));
  } catch (error) {
    $("#notice").hidden = false;
    $("#notice").textContent = error.message;
  } finally {
    $("#run-button").disabled = false;
  }
}

$("#nav-drop").addEventListener("click", () => switchView("drop"));
$("#nav-digest").addEventListener("click", () => switchView("digest"));
$("#empty-start-button").addEventListener("click", () => switchView("drop"));
$("#run-form").addEventListener("submit", startRun);
for (const tab of document.querySelectorAll(".mode-tab")) {
  tab.addEventListener("click", () => setMode(tab.dataset.mode));
  tab.addEventListener("keydown", onModeKeydown);
}
$("#text-input").addEventListener("input", updateCharCount);
$("#text-file-button").addEventListener("click", () => $("#text-file").click());
$("#text-file").addEventListener("change", loadTextFile);
$("#settings-button").addEventListener("click", () => setSettingsOpen($("#settings-popover").hidden));
$("#settings-close").addEventListener("click", () => setSettingsOpen(false));
$("#popover-backdrop").addEventListener("click", () => setSettingsOpen(false));
$("#offline-toggle").addEventListener("change", updateOfflineBadge);
$("#copy-button").addEventListener("click", copyDigest);
$("#export-button").addEventListener("click", exportDigest);
$("#wipe-button").addEventListener("click", wipeDigest);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setSettingsOpen(false);
});

async function initialize() {
  updateOfflineBadge();
  await Promise.all([loadModels(), loadWhisperModels(), refreshDigest()]);
  try {
    setStatus(await api("/api/status"));
  } catch (error) {
    $("#notice").hidden = false;
    $("#notice").textContent = `The local server is unavailable: ${error.message}`;
  }
}

initialize();
