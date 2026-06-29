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
function switchTab(tab) {
  $$("nav#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + tab));
  if (LOADERS[tab]) LOADERS[tab]();
}
$$("nav#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// --- meta ------------------------------------------------------------------

let META = {};

async function loadMeta() {
  try {
    const m = await api("/api/meta");
    META = m;
    $("#meta").textContent = `v${m.tool_version} · core API v${m.core_api_version} · ${m.config_path}`
      + (m.setup_enabled ? " · setup on" : "");
    const fmt = $("#export-format");
    fmt.innerHTML = "";
    m.formats.forEach((f) => fmt.append(el("option", { value: f, textContent: f })));
  } catch (e) { toast(e.message, "err"); }
}

// --- sources ---------------------------------------------------------------

const num = (n) => (typeof n === "number" ? n.toLocaleString() : n);

async function loadSources() {
  const tbody = $("#sources-table tbody");
  tbody.innerHTML = "";
  let rows = [];
  try {
    rows = await api("/api/status");
  } catch (e) { toast(e.message, "err"); return; }
  if (!rows.length) {
    tbody.append(el("tr", {}, el("td", { colSpan: 8, className: "muted", textContent: "No sources configured yet — add one in “Add source”." })));
  } else {
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
        el("td", { textContent: num(s.live_items) }),
        el("td", { textContent: num(s.deleted_items) }),
        el("td", { textContent: num(s.run_count) }),
        el("td", {}, last),
        el("td", {}, btn),
      ));
    });
  }
  // Hint: connectors that are available but have no configured source yet.
  const hint = $("#sources-hint");
  hint.innerHTML = "";
  try {
    const conns = await api("/api/connectors");
    const have = new Set(rows.map((r) => r.type));
    const missing = conns.map((c) => c.type).filter((t) => !have.has(t));
    if (missing.length) {
      hint.append(document.createTextNode(`Available connectors with no source yet: ${missing.join(", ")}. `));
      const a = el("a", { href: "#", textContent: "Add a source →" });
      a.addEventListener("click", (e) => { e.preventDefault(); switchTab("add"); });
      hint.append(a);
    }
  } catch (e) { /* hint is best-effort */ }
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

      const ready = el("span", { className: "pill " + (c.ready ? "st-success" : "st-partial"),
        textContent: c.ready ? "ready" : "needs setup" });

      const actions = el("div", { className: "conn-actions" });
      const addBtn = el("button", { className: "small", textContent: "Add source" });
      addBtn.addEventListener("click", () => startAddSource(c.type));
      actions.append(addBtn);
      if (!c.ready) {
        if (META.setup_enabled) {
          const install = el("button", { className: "primary small", textContent: "Install" });
          install.addEventListener("click", () => installConnector(c.type, install));
          actions.append(install);
        } else if (c.ready_detail) {
          actions.append(el("code", { textContent: c.ready_detail }));
        }
        if (c.needs_playwright_browser && !META.setup_enabled) {
          actions.append(el("span", { className: "tag", textContent: "+ playwright install chromium" }));
        }
      }
      if (c.supports_interactive_login && META.setup_enabled) {
        const login = el("button", { className: "small", textContent: "Log in (browser)" });
        login.disabled = !c.ready;  // need the package before logging in
        login.title = c.ready ? "Opens a browser on the server host" : "Install the connector first";
        login.addEventListener("click", () => loginConnector(c.type, login));
        actions.append(login);
      }
      if (c.secret_keys.length) {
        const jump = el("a", { href: "#", textContent: "set API key →" });
        jump.addEventListener("click", (e) => { e.preventDefault(); switchTab("secrets"); });
        actions.append(jump);
      }
      if (c.docs_url) actions.append(el("a", { href: c.docs_url, target: "_blank", rel: "noopener", textContent: "docs ↗" }));

      const card = el("div", { className: "connector" },
        el("div", { className: "row", style: "gap:0.5rem;align-items:baseline;flex-wrap:wrap;" },
          el("h3", { textContent: `${c.display_name} (${c.type})`, style: "margin:0;" }), ready),
        el("div", { className: "muted", textContent: c.description || "" }),
        el("div", { className: "tag", textContent: `${c.is_builtin ? "built-in" : c.dist_name} · secrets: ${c.secret_keys.join(", ") || "none"} · kinds: ${c.item_kinds.map((k) => k.name).join(", ")}` }),
        caps,
        actions,
      );
      if (c.type === "youtube") {
        card.append(el("div", { className: "tag", style: "margin-top:0.4rem;",
          textContent: "Tip: instead of a cookies file you can set cookies_from_browser (e.g. chrome) in the source config to read your logged-in browser's cookies." }));
      }
      box.append(card);
    });
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.connectors = loadConnectors;
$("#refresh-connectors").addEventListener("click", loadConnectors);
$("#setup-log-hide").addEventListener("click", () => $("#setup-log-card").classList.add("hidden"));

// --- setup actions (install / login) ---------------------------------------

let setupES = null;

async function installConnector(type, btn) {
  if (btn) btn.disabled = true;
  try {
    const job = await api(`/api/connectors/${encodeURIComponent(type)}/install`, { method: "POST" });
    streamSetup(job.id, `Installing ${type}…`);
  } catch (e) { toast(e.message, "err"); if (btn) btn.disabled = false; }
}

async function loginConnector(type, btn) {
  if (btn) btn.disabled = true;
  try {
    const job = await api(`/api/connectors/${encodeURIComponent(type)}/login`, { method: "POST" });
    streamSetup(job.id, `${type}: browser login (check the server host for a window)`);
  } catch (e) { toast(e.message, "err"); if (btn) btn.disabled = false; }
}

function streamSetup(jobId, title) {
  if (setupES) { setupES.close(); setupES = null; }
  const card = $("#setup-log-card");
  const log = $("#setup-log");
  $("#setup-log-title").textContent = title;
  log.textContent = "";
  card.classList.remove("hidden");
  setupES = new EventSource(`/api/setup/${jobId}/stream`);
  setupES.onmessage = (m) => {
    const { line } = JSON.parse(m.data);
    log.textContent += line + "\n";
    log.scrollTop = log.scrollHeight;
  };
  setupES.addEventListener("end", (m) => {
    if (setupES) { setupES.close(); setupES = null; }
    const snap = JSON.parse(m.data);
    const ok = snap.status === "done";
    toast(ok ? "Setup finished." : `Setup failed: ${snap.error || "error"}`, ok ? "ok" : "err");
    loadConnectors();
    loadSecrets();
  });
  setupES.onerror = () => { /* 'end' handles teardown */ };
}

// --- secrets / API keys ----------------------------------------------------

async function saveSecret(name, value, statusEl) {
  if (!value) { toast("Enter a value first.", "err"); return; }
  try {
    const r = await api("/api/secrets", { method: "POST", body: JSON.stringify({ name, value }) });
    toast(`Saved ${name}.`, "ok");
    if (r.shadowed_by_process_env) toast(`Note: ${name} is also set in the process environment, which overrides .env.`, "");
    loadSecrets();
  } catch (e) {
    if (statusEl) { statusEl.textContent = e.message; statusEl.className = "result st-failed"; }
    else toast(e.message, "err");
  }
}

async function clearSecret(name) {
  try {
    await api(`/api/secrets/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(`Cleared ${name}.`, "ok");
    loadSecrets();
  } catch (e) { toast(e.message, "err"); }
}

async function loadSecrets() {
  const box = $("#secrets-list");
  box.innerHTML = "";
  try {
    const data = await api("/api/secrets");
    $("#secrets-envpath").textContent = data.env_file;

    if (!data.secrets.length) {
      box.append(el("div", { className: "muted", textContent: "None of your configured sources require an API key." }));
    }
    data.secrets.forEach((s) => {
      const status = el("span", { className: "pill " + (s.set ? "st-success" : "st-failed"),
        textContent: s.set ? (s.in_env_file ? "set" : "set (process env)") : "not set" });
      const input = el("input", { type: "password", placeholder: s.set ? "replace…" : "value", autocomplete: "new-password" });
      const result = el("span", { className: "result" });
      const save = el("button", { className: "primary small", textContent: "Save" });
      save.addEventListener("click", () => saveSecret(s.name, input.value, result));
      const row = el("div", { className: "row", style: "gap:0.5rem;flex-wrap:wrap;" },
        el("strong", { className: "mono", textContent: s.name }), status, input, save);
      if (s.in_env_file) {
        const clear = el("button", { className: "small", textContent: "Clear" });
        clear.addEventListener("click", () => clearSecret(s.name));
        row.append(clear);
      }
      const used = el("div", { className: "tag", textContent: "used by: " + (s.sources.join(", ") || "—") });
      const warn = s.in_process_env
        ? el("div", { className: "tag st-partial", textContent: "also set in the process environment (overrides .env at runtime)" })
        : document.createTextNode("");
      box.append(el("div", { className: "connector" }, row, used, warn, result));
    });

    // "Set another key": allowed names not already listed above.
    const listed = new Set(data.secrets.map((s) => s.name));
    const sel = $("#secret-other-name");
    sel.innerHTML = "";
    const others = data.allowed.filter((n) => !listed.has(n));
    if (!others.length) {
      sel.append(el("option", { value: "", textContent: "(no other keys)" }));
      $("#secret-other-form").querySelector("button").disabled = true;
    } else {
      $("#secret-other-form").querySelector("button").disabled = false;
      others.forEach((n) => sel.append(el("option", { value: n, textContent: n })));
    }
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.secrets = loadSecrets;
$("#refresh-secrets").addEventListener("click", loadSecrets);
$("#secret-other-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("#secret-other-name").value;
  if (!name) return;
  saveSecret(name, $("#secret-other-value").value);
  $("#secret-other-value").value = "";
});

// --- add source ------------------------------------------------------------

let CONNECTOR_SCHEMAS = {};
let pendingAddType = null;  // a connector type to preselect on next form load

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
    if (pendingAddType && CONNECTOR_SCHEMAS[pendingAddType]) {
      sel.value = pendingAddType;
    }
    pendingAddType = null;
    renderSchemaFields(sel.value);
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.add = loadAddForm;
$("#add-type").addEventListener("change", (e) => renderSchemaFields(e.target.value));

// Jump to the Add-source tab with a connector type preselected.
function startAddSource(type) {
  pendingAddType = type;
  switchTab("add");  // triggers loadAddForm(), which honors pendingAddType
  $("#add-name").focus();
}

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
      store_media: $("#add-store-media").checked,
      max_media_mb: parseInt($("#add-max-media").value || "0", 10),
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
