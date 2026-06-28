"use strict";

// --- tiny helpers ----------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids) n.append(k);
  return n;
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}

const statusClass = (s) => "st-" + (s || "");

// --- tabs ------------------------------------------------------------------

const LOADERS = {};
$$("nav#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("nav#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    const tab = btn.dataset.tab;
    $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + tab));
    if (LOADERS[tab]) LOADERS[tab]();
  });
});

// --- meta ------------------------------------------------------------------

async function loadMeta() {
  try {
    const m = await api("/api/meta");
    $("#meta").textContent = `v${m.tool_version} · core API v${m.core_api_version} · ${m.config_path}`;
    const fmt = $("#export-format");
    fmt.innerHTML = "";
    m.formats.forEach((f) => fmt.append(el("option", { value: f, textContent: f })));
  } catch (e) { toast(e.message, "err"); }
}

// --- sources ---------------------------------------------------------------

async function loadSources() {
  const tbody = $("#sources-table tbody");
  tbody.innerHTML = "";
  try {
    const rows = await api("/api/status");
    if (!rows.length) {
      tbody.append(el("tr", {}, el("td", { colSpan: 8, className: "muted", textContent: "No sources configured. Add one in the “Add source” tab." })));
      return;
    }
    rows.forEach((s) => {
      const last = s.last_run_status
        ? el("span", { className: statusClass(s.last_run_status), textContent: s.last_run_status })
        : el("span", { className: "muted", textContent: "—" });
      const btn = el("button", { className: "small", textContent: "Back up", disabled: !s.enabled });
      btn.addEventListener("click", () => startBackup({ source: s.name }));
      tbody.append(el("tr", {},
        el("td", { textContent: s.name }),
        el("td", { className: "tag", textContent: s.type }),
        el("td", { textContent: s.enabled ? "yes" : "no" }),
        el("td", { textContent: s.live_items }),
        el("td", { textContent: s.deleted_items }),
        el("td", { textContent: s.run_count }),
        el("td", {}, last),
        el("td", {}, btn),
      ));
    });
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.sources = loadSources;
$("#refresh-sources").addEventListener("click", loadSources);
$("#backup-all").addEventListener("click", () => startBackup({ all: true }));

// --- history ---------------------------------------------------------------

async function loadHistory() {
  const tbody = $("#history-table tbody");
  tbody.innerHTML = "";
  const source = $("#history-source").value.trim();
  const limit = $("#history-limit").value || 25;
  const qs = new URLSearchParams({ limit });
  if (source) qs.set("source", source);
  try {
    const runs = await api("/api/history?" + qs);
    if (!runs.length) {
      tbody.append(el("tr", {}, el("td", { colSpan: 8, className: "muted", textContent: "No runs yet." })));
      return;
    }
    runs.forEach((r) => {
      tbody.append(el("tr", {},
        el("td", { className: "mono", textContent: (r.started_at || "").replace("T", " ").slice(0, 19) }),
        el("td", { textContent: r.source_name || "?" }),
        el("td", { className: statusClass(r.status), textContent: r.status }),
        el("td", { className: "tag", textContent: r.mode }),
        el("td", { textContent: r.items_created ?? 0 }),
        el("td", { textContent: r.items_updated ?? 0 }),
        el("td", { textContent: r.items_deleted ?? 0 }),
        el("td", { className: "muted", textContent: r.error || "" }),
      ));
    });
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.history = loadHistory;
$("#refresh-history").addEventListener("click", loadHistory);

// --- connectors ------------------------------------------------------------

async function loadConnectors() {
  const box = $("#connectors-list");
  box.innerHTML = "";
  try {
    const items = await api("/api/connectors");
    items.forEach((c) => {
      const caps = el("div", { className: "caps" });
      Object.entries(c.capabilities).forEach(([k, v]) => {
        if (v === true) caps.append(el("span", { className: "pill", textContent: k.replace(/^supports_/, "") }));
      });
      const card = el("div", { className: "connector" },
        el("h3", { textContent: `${c.display_name} (${c.type})` }),
        el("div", { className: "muted", textContent: c.description || "" }),
        el("div", { className: "tag", textContent: `${c.is_builtin ? "built-in" : c.dist_name} · secrets: ${c.secret_keys.join(", ") || "none"} · kinds: ${c.item_kinds.map((k) => k.name).join(", ")}` }),
        caps,
      );
      box.append(card);
    });
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.connectors = loadConnectors;

// --- add source ------------------------------------------------------------

let CONNECTOR_SCHEMAS = {};

async function loadAddForm() {
  const sel = $("#add-type");
  try {
    const items = await api("/api/connectors");
    CONNECTOR_SCHEMAS = {};
    sel.innerHTML = "";
    items.forEach((c) => {
      CONNECTOR_SCHEMAS[c.type] = c.config_schema || {};
      sel.append(el("option", { value: c.type, textContent: `${c.type} — ${c.display_name}` }));
    });
    renderSchemaFields(sel.value);
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.add = loadAddForm;
$("#add-type").addEventListener("change", (e) => renderSchemaFields(e.target.value));

function renderSchemaFields(type) {
  const box = $("#add-schema");
  box.innerHTML = "";
  const schema = CONNECTOR_SCHEMAS[type] || {};
  const props = schema.properties || {};
  const required = new Set(schema.required || []);
  Object.entries(props).forEach(([key, spec]) => {
    const type0 = Array.isArray(spec.type) ? spec.type[0] : spec.type;
    const labelTxt = `${key}${required.has(key) ? " *" : ""}`;
    let input;
    if (type0 === "boolean") {
      input = el("input", { type: "checkbox", dataset: { key, kind: "boolean" } });
      if (spec.default === true) input.checked = true;
      const lab = el("label", { className: "check" }, input, document.createTextNode(" " + labelTxt));
      box.append(lab);
      return;
    } else if (type0 === "integer" || type0 === "number") {
      input = el("input", { type: "number", dataset: { key, kind: type0 } });
      if (spec.default != null) input.value = spec.default;
    } else if (type0 === "array") {
      input = el("input", { type: "text", placeholder: "comma,separated", dataset: { key, kind: "array" } });
    } else {
      input = el("input", { type: "text", dataset: { key, kind: "string" } });
      if (spec.default != null) input.value = spec.default;
    }
    if (required.has(key)) input.required = true;
    box.append(el("label", {}, document.createTextNode(labelTxt), input,
      spec.description ? el("span", { className: "tag", textContent: spec.description }) : document.createTextNode("")));
  });
}

function collectOptions() {
  const options = {};
  $$("#add-schema [data-key]").forEach((input) => {
    const { key, kind } = input.dataset;
    if (kind === "boolean") { options[key] = input.checked; return; }
    const v = input.value.trim();
    if (v === "") return; // let the connector default apply
    if (kind === "integer") options[key] = parseInt(v, 10);
    else if (kind === "number") options[key] = parseFloat(v);
    else if (kind === "array") options[key] = v.split(",").map((s) => s.trim()).filter(Boolean);
    else options[key] = v;
  });
  const advanced = $("#add-options").value.trim();
  if (advanced) {
    let parsed;
    try { parsed = JSON.parse(advanced); }
    catch (e) { throw new Error("Advanced options is not valid JSON: " + e.message); }
    Object.assign(options, parsed); // advanced JSON wins
  }
  return options;
}

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = $("#add-result");
  result.textContent = "";
  try {
    const body = JSON.stringify({
      name: $("#add-name").value.trim(),
      type: $("#add-type").value,
      options: collectOptions(),
    });
    const sc = await api("/api/sources", { method: "POST", body });
    result.textContent = `Added ${sc.name} (${sc.type}).`;
    result.className = "result st-success";
    toast(`Source “${sc.name}” added.`, "ok");
    $("#add-name").value = "";
  } catch (err) {
    result.textContent = err.message;
    result.className = "result st-failed";
  }
});

// --- export ----------------------------------------------------------------

$("#export-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const qs = new URLSearchParams();
  qs.set("format", $("#export-format").value);
  const csv = (id, key) => $(id).value.split(",").map((s) => s.trim()).filter(Boolean).forEach((v) => qs.append(key, v));
  csv("#export-source", "source");
  csv("#export-type", "type");
  if ($("#export-since").value.trim()) qs.set("since", $("#export-since").value.trim());
  if ($("#export-until").value.trim()) qs.set("until", $("#export-until").value.trim());
  if ($("#export-deleted").checked) qs.set("include_deleted", "true");
  if ($("#export-revisions").checked) qs.set("include_revisions", "true");
  if ($("#export-noraw").checked) qs.set("no_raw", "true");
  window.location.assign("/api/export?" + qs.toString());
});

// --- verify ----------------------------------------------------------------

async function runVerify() {
  const box = $("#verify-result");
  box.innerHTML = "Running…";
  try {
    const r = await api("/api/verify");
    if (r.ok) {
      box.innerHTML = "";
      box.append(el("div", { className: "st-success", textContent: "OK — no issues found." }));
      return;
    }
    box.innerHTML = "";
    box.append(el("div", { className: "st-failed", textContent: `${r.issues.length} issue(s):` }));
    r.issues.forEach((i) => box.append(el("div", { className: "mono", textContent: `[${i.kind}] ${i.source}: ${i.detail}` })));
  } catch (e) { toast(e.message, "err"); }
}
$("#run-verify").addEventListener("click", runVerify);

// --- backup progress (SSE) -------------------------------------------------

let activeES = null;

function setBackupButtons(disabled) {
  // While a backup runs, lock the triggers. On finish, loadSources() re-renders
  // the per-row buttons with their correct enabled/disabled state anyway.
  $("#backup-all").disabled = disabled;
  if (disabled) $$("#sources-table button.small").forEach((b) => { b.disabled = true; });
}

async function startBackup(spec) {
  if (activeES) { toast("A backup is already running.", "err"); return; }
  try {
    const job = await api("/api/backup", { method: "POST", body: JSON.stringify(spec) });
    openProgress(job);
  } catch (e) { toast(e.message, "err"); }
}

function openProgress(job) {
  const panel = $("#progress");
  panel.classList.remove("hidden");
  $("#progress-title").textContent = job.spec.all ? "Backing up all sources" : `Backing up ${job.spec.source}`;
  $("#progress-sub").textContent = "";
  $("#progress-results").innerHTML = "";
  const bar = $("#progress-bar");
  bar.style.width = "0%";
  setBackupButtons(true);

  let doneCount = 0;
  let total = job.spec.all ? null : 1;

  activeES = new EventSource(`/api/backup/${job.id}/stream`);
  activeES.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.source_total) total = ev.source_total;
    $("#progress-sub").textContent = ev.source_total ? `[${ev.source_index}/${ev.source_total}] ${ev.source}` : ev.source;
    const stats = `+${ev.created} ~${ev.updated} =${ev.unchanged}` + (ev.deleted ? ` x${ev.deleted}` : "");
    $("#progress-line").textContent = `${ev.source} [${ev.mode}] ${ev.fetched.toLocaleString()} fetched (${stats})`;
    if (ev.phase === "source_done" && ev.result) {
      doneCount++;
      addResult(ev.result);
    }
    if (total) {
      bar.classList.remove("indeterminate");
      bar.style.width = Math.round((doneCount / total) * 100) + "%";
    } else {
      bar.classList.add("indeterminate");
    }
  };
  activeES.addEventListener("end", (m) => finishProgress(JSON.parse(m.data)));
  activeES.onerror = () => { /* server closed; the 'end' event handles teardown */ };
}

function addResult(r) {
  const line = `${r.source}: ${r.status} [${r.mode}] +${r.created} ~${r.updated} =${r.unchanged} x${r.deleted} (fetched ${r.fetched})`;
  $("#progress-results").append(el("div", { className: "mono " + statusClass(r.status), textContent: line + (r.error ? `  — ${r.error}` : "") }));
}

function finishProgress(snap) {
  if (activeES) { activeES.close(); activeES = null; }
  const bar = $("#progress-bar");
  bar.classList.remove("indeterminate");
  bar.style.width = "100%";
  $("#progress-title").textContent = snap.status === "error" ? "Backup failed" : "Backup complete";
  if (snap.status === "error") {
    $("#progress-results").append(el("div", { className: "st-failed mono", textContent: snap.error || "error" }));
  } else if (snap.results && $("#progress-results").childElementCount === 0) {
    snap.results.forEach(addResult); // fallback if we missed live events
  }
  setBackupButtons(false);
  loadSources();
  toast(snap.status === "error" ? "Backup failed." : "Backup complete.", snap.status === "error" ? "err" : "ok");
}

// On load, if a backup is already running (e.g. page refresh), reattach.
async function resumeIfRunning() {
  try {
    const cur = await api("/api/backup/current");
    if (cur && cur.status === "running") openProgress(cur);
  } catch (_) {}
}

// --- boot ------------------------------------------------------------------

loadMeta();
loadSources();
resumeIfRunning();
