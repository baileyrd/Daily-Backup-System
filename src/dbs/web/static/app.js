"use strict";

// --- tiny helpers ----------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const el = (tag, props = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    // `dataset` is a getter-only DOMStringMap — assign its keys, don't replace it.
    if (k === "dataset") Object.assign(n.dataset, v);
    else n[k] = v;
  }
  for (const k of kids) n.append(k);
  return n;
};

// --- auth token (dbs serve --token) -----------------------------------------
// Picked up once from ?token=... (then scrubbed from the URL), stored locally,
// attached to every api() call; URL-based consumers (EventSource, downloads)
// carry it as a query parameter via withToken() since they can't set headers.

const TOKEN_KEY = "dbs-token";
(function pickupToken() {
  const params = new URLSearchParams(location.search);
  const t = params.get("token");
  if (t) {
    localStorage.setItem(TOKEN_KEY, t);
    params.delete("token");
    const qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  }
})();

function withToken(path) {
  const t = localStorage.getItem(TOKEN_KEY);
  if (!t) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t);
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const t = localStorage.getItem(TOKEN_KEY);
  if (t) headers["Authorization"] = "Bearer " + t;
  const res = await fetch(path, {
    ...opts,
    headers,
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
  let conns = [];
  try {
    [rows, conns] = await Promise.all([api("/api/status"), api("/api/connectors")]);
  } catch (e) { toast(e.message, "err"); return; }
  const byType = Object.fromEntries(conns.map((c) => [c.type, c]));

  if (!rows.length) {
    tbody.append(el("tr", {}, el("td", { colSpan: 8, className: "muted", textContent: "No sources configured yet — add one in “Add source”." })));
  } else {
    rows.forEach((s) => {
      const last = s.last_run_status
        ? el("span", { className: statusClass(s.last_run_status), textContent: s.last_run_status })
        : el("span", { className: "muted", textContent: "—" });
      const actions = el("div", { className: "row", style: "gap:0.4rem;justify-content:flex-end;" });
      const ac = byType[s.type] && byType[s.type].auth_capture;
      if (ac && META.setup_enabled) {
        const login = el("button", { className: "small", textContent: ac.label });
        login.title = "Open a browser to capture this source's login session";
        // per_source captures write into the source's own tool dir -> per-source endpoint.
        login.addEventListener("click", () => ac.per_source
          ? sourceCapture(s.name, ac.label, login)
          : captureConnector(s.type, login));
        actions.append(login);
      }
      const btn = el("button", { className: "small", textContent: "Back up", disabled: !s.enabled });
      btn.addEventListener("click", () => startBackup({ source: s.name }));
      actions.append(btn);
      tbody.append(el("tr", {},
        el("td", { textContent: s.name }),
        el("td", { className: "tag", textContent: s.type }),
        el("td", { textContent: s.enabled ? "yes" : "no" }),
        el("td", { textContent: num(s.live_items) }),
        el("td", { textContent: num(s.deleted_items) }),
        el("td", { textContent: num(s.run_count) }),
        el("td", {}, last),
        el("td", {}, actions),
      ));
    });
  }
  // Hint: connectors that are available but have no configured source yet.
  const hint = $("#sources-hint");
  hint.innerHTML = "";
  const have = new Set(rows.map((r) => r.type));
  const missing = conns.map((c) => c.type).filter((t) => !have.has(t));
  if (missing.length) {
    hint.append(document.createTextNode(`Available connectors with no source yet: ${missing.join(", ")}. `));
    const a = el("a", { href: "#", textContent: "Add a source →" });
    a.addEventListener("click", (e) => { e.preventDefault(); switchTab("add"); });
    hint.append(a);
  }
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
        el("td", { className: "muted",
                   textContent: [r.error, ...(r.warnings || [])].filter(Boolean).join(" — ") }),
      ));
    });
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.history = loadHistory;
$("#refresh-history").addEventListener("click", loadHistory);

// --- browse (paginated item listing + metrics + detail drawer) -------------

const BROWSE_LIMIT = 50;
let browseOffset = 0;

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i > 0 && v < 10 ? 1 : 0)} ${units[i]}`;
}

function statTile(value, label) {
  return el("div", { className: "stat" },
    el("div", { className: "stat-num", textContent: String(value) }),
    el("div", { className: "stat-label", textContent: label }));
}

async function loadBrowseMetrics() {
  const box = $("#browse-metrics");
  box.innerHTML = "";
  let m;
  try { m = await api("/api/metrics"); } catch (e) { toast(e.message, "err"); return; }
  const totals = m.by_source_kind.reduce(
    (acc, r) => ({ total: acc.total + r.total, live: acc.live + r.live, deleted: acc.deleted + r.deleted }),
    { total: 0, live: 0, deleted: 0 },
  );
  box.append(el("div", { className: "metrics-strip row" },
    statTile(num(totals.total), "items"),
    statTile(num(totals.live), "live"),
    statTile(num(totals.deleted), "deleted"),
    statTile(num(m.revision_count), "revisions"),
    statTile(num(m.media_count), "media files"),
    statTile(fmtBytes(m.media_bytes), "media stored"),
  ));
  if (m.by_source_kind.length) {
    const tbody = el("tbody");
    m.by_source_kind.forEach((r) => tbody.append(el("tr", {},
      el("td", { textContent: r.source }),
      el("td", { className: "tag", textContent: r.kind }),
      el("td", { textContent: num(r.live) }),
      el("td", { textContent: num(r.deleted) }),
    )));
    box.append(el("table", { className: "metrics-table" },
      el("thead", {}, el("tr", {},
        el("th", { textContent: "Source" }), el("th", { textContent: "Kind" }),
        el("th", { textContent: "Live" }), el("th", { textContent: "Deleted" }))),
      tbody,
    ));
  }
}

function browseParams() {
  const qs = new URLSearchParams();
  const csv = (id, key) => $(id).value.split(",").map((s) => s.trim()).filter(Boolean).forEach((v) => qs.append(key, v));
  csv("#browse-source", "source");
  csv("#browse-type", "type");
  if ($("#browse-q").value.trim()) qs.set("q", $("#browse-q").value.trim());
  if ($("#browse-since").value.trim()) qs.set("since", $("#browse-since").value.trim());
  if ($("#browse-until").value.trim()) qs.set("until", $("#browse-until").value.trim());
  if ($("#browse-deleted").checked) qs.set("include_deleted", "true");
  qs.set("limit", BROWSE_LIMIT);
  qs.set("offset", browseOffset);
  return qs;
}

async function loadBrowse() {
  const tbody = $("#browse-table tbody");
  tbody.innerHTML = "";
  let data;
  try { data = await api("/api/items?" + browseParams()); }
  catch (e) { toast(e.message, "err"); return; }
  if (!data.items.length) {
    tbody.append(el("tr", {}, el("td", { colSpan: 7, className: "muted", textContent: "No items match these filters." })));
  } else {
    data.items.forEach((it) => {
      const row = el("tr", { className: it.deleted ? "muted" : "" },
        el("td", { textContent: it.source }),
        el("td", { className: "tag", textContent: it.item_kind }),
        el("td", { textContent: it.title || "(untitled)" }),
        el("td", { className: "mono", textContent: (it.created_at || "").replace("T", " ").slice(0, 19) }),
        el("td", { className: "mono", textContent: (it.updated_at || "").replace("T", " ").slice(0, 19) }),
        el("td", { textContent: it.revision }),
        el("td", { textContent: it.media_count || "" }),
      );
      row.addEventListener("click", () => openItemDrawer(it.id));
      tbody.append(row);
    });
  }
  $("#browse-count").textContent = data.total
    ? `${data.offset + 1}–${Math.min(data.offset + data.items.length, data.total)} of ${num(data.total)}`
    : "0 results";
  $("#browse-prev").disabled = data.offset <= 0;
  $("#browse-next").disabled = data.offset + data.items.length >= data.total;
}
LOADERS.browse = () => { loadBrowseMetrics(); loadBrowse(); };
$("#refresh-browse").addEventListener("click", () => { loadBrowseMetrics(); loadBrowse(); });
$("#browse-filters").addEventListener("submit", (e) => { e.preventDefault(); browseOffset = 0; loadBrowse(); });
$("#browse-prev").addEventListener("click", () => { browseOffset = Math.max(0, browseOffset - BROWSE_LIMIT); loadBrowse(); });
$("#browse-next").addEventListener("click", () => { browseOffset += BROWSE_LIMIT; loadBrowse(); });

async function openItemDrawer(id) {
  const drawer = $("#item-drawer");
  const body = $("#item-drawer-body");
  body.innerHTML = "Loading…";
  drawer.classList.remove("hidden");
  let item;
  try { item = await api(`/api/items/${id}`); }
  catch (e) { body.textContent = e.message; return; }
  $("#item-drawer-title").textContent = item.title || item.external_id;
  body.innerHTML = "";
  body.append(el("div", { className: "tag" },
    document.createTextNode(`${item.source} · ${item.item_kind} · rev ${item.revision}` + (item.deleted ? " · deleted" : ""))));
  if (item.url) {
    body.append(el("div", { style: "margin-top:0.4rem;" },
      el("a", { href: item.url, target: "_blank", rel: "noopener", textContent: item.url })));
  }
  if (item.media && item.media.length) {
    const mediaBox = el("div", { className: "media-list" });
    item.media.forEach((m) => {
      const isImage = m.has_data && (m.mime || "").startsWith("image/");
      if (isImage) {
        mediaBox.append(el("img", { src: withToken(`/api/media/${m.id}`), alt: m.filename || "", className: "media-thumb" }));
      } else {
        const label = `${m.filename || m.kind} (${m.byte_size != null ? fmtBytes(m.byte_size) : "not stored"})`;
        mediaBox.append(m.has_data
          ? el("a", { href: withToken(`/api/media/${m.id}`), textContent: label, className: "small" })
          : el("span", { className: "tag", textContent: label }));
      }
    });
    body.append(mediaBox);
  }
  body.append(el("pre", { className: "log", style: "max-height:24rem;", textContent: JSON.stringify(item.raw, null, 2) }));
}
$("#item-drawer-close").addEventListener("click", () => $("#item-drawer").classList.add("hidden"));

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
      // per_source captures need a configured source -> use the Sources-row
      // button, not this connector-level one.
      if (c.auth_capture && !c.auth_capture.per_source && META.setup_enabled) {
        const cap = el("button", { className: "small", textContent: c.auth_capture.label });
        cap.title = "Opens a browser on the server host so you can log in; the session is captured into .env";
        cap.addEventListener("click", () => captureConnector(c.type, cap));
        actions.append(cap);
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
        c.setup_hint ? el("div", { className: "hint", textContent: c.setup_hint }) : document.createTextNode(""),
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

async function captureConnector(type, btn) {
  if (btn) btn.disabled = true;
  try {
    const job = await api(`/api/connectors/${encodeURIComponent(type)}/capture`, { method: "POST" });
    streamSetup(job.id, `${type}: login capture — check the server host for a browser window`);
  } catch (e) { toast(e.message, "err"); if (btn) btn.disabled = false; }
}

async function sourceCapture(name, label, btn) {
  if (btn) btn.disabled = true;
  try {
    const job = await api(`/api/sources/${encodeURIComponent(name)}/capture`, { method: "POST" });
    streamSetup(job.id, `${label} — check the server host for a browser window`);
  } catch (e) { toast(e.message, "err"); if (btn) btn.disabled = false; }
}

function streamSetup(jobId, title) {
  if (setupES) { setupES.close(); setupES = null; }
  const card = $("#setup-log-card");
  const log = $("#setup-log");
  $("#setup-log-title").textContent = title;
  log.textContent = "";
  card.classList.remove("hidden");
  setupES = new EventSource(withToken(`/api/setup/${jobId}/stream`));
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
let CONNECTORS = {};         // full connector objects by type
let pendingAddType = null;  // a connector type to preselect on next form load

async function loadAddForm() {
  const sel = $("#add-type");
  try {
    const items = await api("/api/connectors");
    CONNECTOR_SCHEMAS = {};
    CONNECTORS = {};
    sel.innerHTML = "";
    items.forEach((c) => {
      CONNECTOR_SCHEMAS[c.type] = c.config_schema || {};
      CONNECTORS[c.type] = c;
      sel.append(el("option", { value: c.type, textContent: `${c.type} — ${c.display_name}` }));
    });
    if (pendingAddType && CONNECTOR_SCHEMAS[pendingAddType]) {
      sel.value = pendingAddType;
    }
    pendingAddType = null;
    renderTypeUI(sel.value);
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.add = loadAddForm;
$("#add-type").addEventListener("change", (e) => renderTypeUI(e.target.value));

function renderTypeUI(type) {
  renderSchemaFields(type);
  renderCaptureArea(type);
}

// Per-connector setup guidance + a "Log in (capture session)" action, right
// where you configure the source.
function renderCaptureArea(type) {
  const box = $("#add-capture");
  box.innerHTML = "";
  const c = CONNECTORS[type];
  if (!c) return;
  if (c.setup_hint) box.append(el("div", { className: "hint", textContent: c.setup_hint }));
  if (!c.auth_capture) return;
  const ac = c.auth_capture;
  const wrap = el("div", { className: "capture-box" });
  if (ac.per_source) {
    // The capture target depends on this source's config, so it runs after the
    // source is added — from the Sources tab.
    wrap.append(el("div", { className: "muted",
      textContent: `Add the source (with its folder configured), then use the “${ac.label}” button on the Sources tab to capture the login.` }));
    box.append(wrap);
    return;
  }
  wrap.append(el("div", { className: "muted",
    textContent: `This source needs a login. Capture it once — a browser opens on this machine, you log in, and ${ac.secret_key} is saved to your .env.` }));
  if (META.setup_enabled) {
    const btn = el("button", { type: "button", className: "primary small", textContent: `${ac.label} (open browser)` });
    btn.addEventListener("click", () => captureConnector(type, btn));
    wrap.append(btn);
    if (c.capture_ready === false) {
      wrap.append(el("div", { className: "tag", textContent: "First run installs Playwright + a browser, then opens the login window (watch the log)." }));
    }
  } else {
    wrap.append(el("div", { className: "tag st-partial",
      textContent: "Login capture is disabled — restart the server with:  dbs serve --allow-setup" }));
  }
  box.append(wrap);
}

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
  window.location.assign(withToken("/api/export?" + qs.toString()));
});

// --- research (YouTube -> NotebookLM -> report) ------------------------------

let researchES = null;
let RESEARCH_META = null;

async function loadResearch() {
  const setup = $("#research-setup");
  setup.innerHTML = "";
  setup.classList.add("hidden");
  try {
    RESEARCH_META = await api("/api/research/meta");
  } catch (e) { toast(e.message, "err"); return; }
  const m = RESEARCH_META;

  // Missing deps → install strip.
  if (!m.ready) {
    setup.classList.remove("hidden");
    setup.append(el("div", { className: "muted",
      textContent: `The research pipeline needs: ${m.pip_requirements.join(", ")} (missing: ${m.missing.join(", ")}).` }));
    if (META.setup_enabled) {
      const btn = el("button", { className: "primary small", textContent: "Install research deps" });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          const job = await api("/api/research/install", { method: "POST" });
          streamSetup(job.id, "Installing research dependencies…");
        } catch (e) { toast(e.message, "err"); btn.disabled = false; }
      });
      setup.append(btn);
    } else {
      setup.append(el("code", { textContent: "pip install 'daily-backup-system[research]'" }));
    }
  }

  // Auth status + login capture. Same Google account the YouTube connector
  // uses; the capture writes the storageState file notebooklm-py reads.
  const note = $("#research-auth-note");
  if (m.auth.configured) {
    note.textContent = "NotebookLM: logged in";
    note.className = "tag st-success";
  } else {
    note.textContent = "NotebookLM: not logged in";
    note.className = "tag st-partial";
    setup.classList.remove("hidden");
    setup.append(el("div", { className: "muted",
      textContent: "NotebookLM needs a Google login (the same account as YouTube). Capture it once — the session is saved server-side and reused." }));
    if (META.setup_enabled) {
      const btn = el("button", { className: "primary small", textContent: "NotebookLM login (open browser)" });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          const job = await api("/api/research/login", { method: "POST" });
          streamSetup(job.id, "NotebookLM login — check the server host for a browser window");
        } catch (e) { toast(e.message, "err"); btn.disabled = false; }
      });
      setup.append(btn);
      setup.append(el("div", { className: "tag",
        textContent: "If Google blocks sign-in in the automated browser, run `notebooklm login` on the host instead — it produces the same file." }));
    } else {
      setup.append(el("div", { className: "tag st-partial",
        textContent: "Login capture is disabled — restart with: dbs serve --allow-setup, or run `notebooklm login` on the host." }));
    }
  }

  // Backup-mode source picker.
  const sel = $("#research-source");
  sel.innerHTML = "";
  sel.append(el("option", { value: "", textContent: "(all youtube sources)" }));
  m.youtube_sources.forEach((s) => sel.append(el("option", { value: s, textContent: s })));
  $("#research-questions").placeholder = m.default_questions.join("\n");
}
LOADERS.research = loadResearch;
$("#refresh-research").addEventListener("click", loadResearch);

$("#research-mode").addEventListener("change", (e) => {
  const backup = e.target.value === "backup";
  $("#research-search-fields").classList.toggle("hidden", backup);
  $("#research-backup-fields").classList.toggle("hidden", !backup);
});

const lines = (v) => v.split("\n").map((s) => s.trim()).filter(Boolean);
const csvList = (v) => v.split(",").map((s) => s.trim()).filter(Boolean);

$("#research-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (researchES) { toast("A research run is already in progress.", "err"); return; }
  const mode = $("#research-mode").value;
  const body = {
    mode,
    topic: $("#research-topic").value.trim(),
    queries: mode === "search" ? lines($("#research-queries").value) : [],
    sources: mode === "backup" && $("#research-source").value ? [$("#research-source").value] : [],
    lists: mode === "backup" ? csvList($("#research-lists").value) : [],
    questions: lines($("#research-questions").value),
    count: parseInt($("#research-count").value || "10", 10),
    per_query_count: parseInt($("#research-per-query").value || "10", 10),
    months: parseInt($("#research-months").value || "6", 10),
    infographic: $("#research-infographic").checked,
    notebook_name: $("#research-notebook").value.trim(),
  };
  try {
    const job = await api("/api/research", { method: "POST", body: JSON.stringify(body) });
    openResearchProgress(job);
  } catch (err) { toast(err.message, "err"); }
});

function openResearchProgress(job) {
  $("#research-run").disabled = true;
  $("#research-result").classList.add("hidden");
  const panel = $("#research-progress");
  const log = $("#research-log");
  $("#research-progress-title").textContent = `Researching: ${job.connector}`;
  log.textContent = "";
  panel.classList.remove("hidden");

  researchES = new EventSource(withToken(`/api/research/${job.id}/stream`));
  researchES.onmessage = (m) => {
    const { line } = JSON.parse(m.data);
    log.textContent += line + "\n";
    log.scrollTop = log.scrollHeight;
  };
  researchES.addEventListener("end", (m) => {
    if (researchES) { researchES.close(); researchES = null; }
    $("#research-run").disabled = false;
    const snap = JSON.parse(m.data);
    if (snap.status === "done" && snap.result) {
      $("#research-report").textContent = snap.result.report;
      $("#research-download").href = withToken(`/api/research/${snap.id}/report`);
      $("#research-result").classList.remove("hidden");
      toast(`Research complete — ${snap.result.indexed}/${snap.result.total} videos indexed.`, "ok");
    } else {
      toast(`Research failed: ${snap.error || "error"}`, "err");
    }
  });
  researchES.onerror = () => { /* 'end' handles teardown */ };
}
$("#research-log-hide").addEventListener("click", () => $("#research-progress").classList.add("hidden"));

// On load, if a research run is already going (e.g. page refresh), reattach.
async function resumeResearchIfRunning() {
  try {
    const cur = await api("/api/research/current");
    if (cur && cur.status === "running") { switchTab("research"); openResearchProgress(cur); }
  } catch (_) {}
}

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

  activeES = new EventSource(withToken(`/api/backup/${job.id}/stream`));
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
  const notes = [r.error, ...(r.warnings || [])].filter(Boolean);
  $("#progress-results").append(el("div", { className: "mono " + statusClass(r.status), textContent: line + (notes.length ? `  — ${notes.join(" — ")}` : "") }));
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
resumeResearchIfRunning();
