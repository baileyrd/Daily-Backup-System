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

// Run statuses → semantic badge/dot tones, kept separate from the accent so
// health always reads unambiguously.
const STATUS_TONE = {
  success: "ok", partial: "warn", failed: "err",
  skipped: "info", interrupted: "warn", running: "info",
};
const tone = (s) => STATUS_TONE[s] || "neutral";
const badge = (s, text) => el("span", { className: "badge " + tone(s), textContent: text ?? s });
const dot = (s) => el("span", { className: "dot " + tone(s) });

const num = (n) => (typeof n === "number" ? n.toLocaleString() : n);

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i > 0 && v < 10 ? 1 : 0)} ${units[i]}`;
}

// "today 06:02" / "yesterday 22:00" / "Jul 3 06:02" — falls back to the raw
// string when the timestamp doesn't parse.
function fmtWhen(iso) {
  if (!iso) return "—";
  const t = new Date(iso);
  if (isNaN(t)) return iso.replace("T", " ").slice(0, 16);
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const day = new Date(t); day.setHours(0, 0, 0, 0);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return `today ${time}`;
  if (diff === 1) return `yesterday ${time}`;
  const d = t.toLocaleDateString([], { month: "short", day: "numeric" });
  return `${d} ${time}`;
}

const fmtStamp = (iso) => (iso || "").replace("T", " ").slice(0, 19);

// --- tabs / navigation -------------------------------------------------------

const TAB_TITLES = {
  dashboard: "Overview", browse: "Library", history: "Activity",
  sources: "Sources", add: "Add source", connectors: "Connectors",
  secrets: "API keys", export: "Export", research: "Research", verify: "Verify",
};

const LOADERS = {};
function switchTab(tab) {
  // "Add source" lives under Sources in the nav.
  const navTab = tab === "add" ? "sources" : tab;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === navTab));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + tab));
  $("#crumb").textContent = TAB_TITLES[tab] || tab;
  if (LOADERS[tab]) LOADERS[tab]();
}
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});
$$("[data-goto]").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.goto));
});

// --- theme -------------------------------------------------------------------

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
applyTheme(localStorage.getItem("dbs-theme") || "");
$("#theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.dataset.theme
    || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  localStorage.setItem("dbs-theme", next);
  applyTheme(next);
});

// --- meta ------------------------------------------------------------------

let META = {};

async function loadMeta() {
  try {
    const m = await api("/api/meta");
    META = m;
    $("#meta-version").textContent = `dbs v${m.tool_version}`;
    $("#meta").textContent = `core API v${m.core_api_version}\n${m.config_path}`
      + (m.setup_enabled ? "\nsetup enabled" : "");
    const fmt = $("#export-format");
    fmt.innerHTML = "";
    m.formats.forEach((f) => fmt.append(el("option", { value: f, textContent: f })));
  } catch (e) { toast(e.message, "err"); }
}

// --- shared status fetch (health chip + nav counts) --------------------------

function updateHealthChip(rows) {
  const chip = $("#health-chip");
  if (!rows.length) { chip.classList.add("hidden"); return; }
  const failed = rows.filter((r) => r.last_run_status === "failed").length;
  const partial = rows.filter((r) => r.last_run_status === "partial").length;
  const d = $("#health-dot");
  const t = $("#health-text");
  if (failed) { d.className = "dot err"; t.textContent = `${failed} source${failed > 1 ? "s" : ""} failing`; }
  else if (partial) { d.className = "dot warn"; t.textContent = `${partial} source${partial > 1 ? "s" : ""} partial`; }
  else { d.className = "dot ok"; t.textContent = "all sources healthy"; }
  chip.classList.remove("hidden");
}

// --- VPN awareness -------------------------------------------------------------
// Sources marked requires_vpn run through the server's VPN wrapper. The UI
// tags them and, when the tunnel is verifiably down, disables their Run
// buttons (the wrapper is fail-closed, so running anyway would just fail).

let VPN = { relevant: false, up: null, detail: "" };

function applyVpnUI() {
  $$(".vpn-pill").forEach((pill) => {
    if (VPN.up === false) {
      pill.className = "badge warn vpn-pill";
      pill.textContent = "VPN down";
      pill.title = VPN.detail || "VPN tunnel is down";
    } else {
      pill.className = "badge info vpn-pill";
      pill.textContent = "VPN";
      pill.title = VPN.up ? `Runs through the VPN tunnel — ${VPN.detail}` : "Runs through the VPN tunnel";
    }
  });
  $$('.run-btn[data-vpn="1"]').forEach((btn) => {
    if (btn.dataset.srcDisabled) return; // disabled source stays disabled
    if (VPN.up === false) {
      btn.disabled = true;
      btn.title = "VPN tunnel is down — start it with: sudo systemctl start vpn-netns";
    } else {
      btn.disabled = false;
      btn.title = "Runs through the VPN tunnel";
    }
  });
}

async function refreshVpn(rows) {
  if (!rows.some((r) => r.requires_vpn)) {
    VPN = { relevant: false, up: null, detail: "" };
    return;
  }
  try { VPN = await api("/api/vpn"); } catch (_) { return; }
  applyVpnUI();
}

// --- dashboard (overview) ----------------------------------------------------

function tile(label, value, deltaText, deltaClass = "flat") {
  return el("div", { className: "card tile" },
    el("div", { className: "label", textContent: label }),
    el("div", { className: "value", textContent: String(value) }),
    el("div", { className: "delta " + deltaClass, textContent: deltaText || "" }));
}

function runCounts(r) {
  const parts = [`+${num(r.items_created ?? 0)}`];
  if (r.items_updated) parts.push(`~${num(r.items_updated)}`);
  if (r.items_deleted) parts.push(`✕${num(r.items_deleted)}`);
  return parts.join(" ");
}

function sourceRow(s, { compact = false } = {}) {
  const row = el("div", { className: "source-row" + (s.enabled ? "" : " disabled") });
  row.append(dot(s.last_run_status || (s.enabled ? "" : "skipped")));
  row.append(el("span", { className: "sname", textContent: s.name }));
  row.append(el("span", { className: "stype", textContent: s.type }));
  if (s.requires_vpn) row.append(el("span", { className: "badge info vpn-pill", textContent: "VPN" }));
  row.append(s.last_run_status ? badge(s.last_run_status) : badge("", s.enabled ? "no runs yet" : "disabled"));
  row.append(el("span", {
    className: "slast",
    textContent: compact
      ? `${fmtWhen(s.last_run_at)}`
      : `${num(s.live_items)} items · ${num(s.run_count)} runs`,
  }));

  const ac = CONNECTOR_BY_TYPE[s.type] && CONNECTOR_BY_TYPE[s.type].auth_capture;
  if (!compact && ac && META.setup_enabled) {
    const login = el("button", { className: "btn small", textContent: ac.label });
    login.title = "Open a browser to capture this source's login session";
    // per_source captures write into the source's own tool dir -> per-source endpoint.
    login.addEventListener("click", () => ac.per_source
      ? sourceCapture(s.name, ac.label, login)
      : captureConnector(s.type, login));
    row.append(login);
  }
  const run = el("button", { className: "btn small run-btn", textContent: "Run", disabled: !s.enabled });
  if (!s.enabled) run.dataset.srcDisabled = "1";
  if (s.requires_vpn) run.dataset.vpn = "1";
  run.addEventListener("click", () => startBackup({ source: s.name }));
  row.append(run);
  return row;
}

function feedItem(r) {
  const what = el("div", { className: "what" });
  what.append(el("strong", { textContent: r.source_name || "?" }));
  what.append(document.createTextNode(` ${r.status} — `));
  what.append(el("span", { className: "counts", textContent: runCounts(r) }));
  if (r.error) what.append(document.createTextNode(` · ${r.error}`));
  return el("div", { className: "feed-item" },
    dot(r.status),
    el("div", {}, what, el("div", { className: "when", textContent: fmtWhen(r.started_at) })));
}

function renderSparkline(runs) {
  const card = $("#dash-spark-card");
  const svg = $("#dash-spark");
  const days = [];
  const today = new Date(); today.setHours(0, 0, 0, 0);
  for (let i = 13; i >= 0; i--) {
    const d = new Date(today); d.setDate(d.getDate() - i);
    days.push({ key: d.toDateString(), date: d, count: 0 });
  }
  const byKey = new Map(days.map((d) => [d.key, d]));
  let any = false;
  runs.forEach((r) => {
    const t = new Date(r.started_at);
    if (isNaN(t)) return;
    const b = byKey.get(new Date(t.getFullYear(), t.getMonth(), t.getDate()).toDateString());
    if (b) { b.count += r.items_created || 0; any = true; }
  });
  if (!any) { card.classList.add("hidden"); return; }

  const counts = days.map((d) => d.count);
  const max = Math.max(1, ...counts);
  const x = (i) => (i * 560) / (days.length - 1);
  const y = (c) => 88 - (c / max) * 72;
  const pts = counts.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`);

  const NS = "http://www.w3.org/2000/svg";
  svg.innerHTML = "";
  [24, 52, 80].forEach((gy) => {
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", "0"); line.setAttribute("x2", "560");
    line.setAttribute("y1", gy); line.setAttribute("y2", gy);
    line.setAttribute("stroke", "var(--border)"); line.setAttribute("stroke-width", "1");
    svg.append(line);
  });
  const area = document.createElementNS(NS, "polygon");
  area.setAttribute("points", pts.join(" ") + " 560,96 0,96");
  area.setAttribute("fill", "var(--accent-soft)");
  svg.append(area);
  const line = document.createElementNS(NS, "polyline");
  line.setAttribute("points", pts.join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--accent)");
  line.setAttribute("stroke-width", "2");
  line.setAttribute("stroke-linejoin", "round");
  svg.append(line);
  const end = document.createElementNS(NS, "circle");
  end.setAttribute("cx", "560"); end.setAttribute("cy", y(counts.at(-1)).toFixed(1));
  end.setAttribute("r", "3.5"); end.setAttribute("fill", "var(--accent)");
  svg.append(end);

  const fmt = (d) => d.toLocaleDateString([], { month: "short", day: "numeric" });
  $("#dash-spark-from").textContent = fmt(days[0].date);
  $("#dash-spark-to").textContent = `today · ${num(counts.at(-1))}`;
  const avg = Math.round(counts.reduce((a, b) => a + b, 0) / days.length);
  $("#dash-spark-aux").textContent = `avg ${num(avg)} / day`;
  card.classList.remove("hidden");
}

async function loadDashboard() {
  let rows, metrics, runs;
  try {
    [rows, metrics, runs] = await Promise.all([
      api("/api/status"),
      api("/api/metrics"),
      api("/api/history?limit=200"),
    ]);
  } catch (e) { toast(e.message, "err"); return; }

  updateHealthChip(rows);
  $("#nav-source-count").textContent = rows.length || "";

  const totals = metrics.by_source_kind.reduce(
    (acc, r) => ({ total: acc.total + r.total, live: acc.live + r.live, deleted: acc.deleted + r.deleted }),
    { total: 0, live: 0, deleted: 0 },
  );
  $("#nav-item-count").textContent = totals.live ? num(totals.live) : "";

  const todayKey = new Date().toDateString();
  const addedToday = runs
    .filter((r) => { const t = new Date(r.started_at); return !isNaN(t) && t.toDateString() === todayKey; })
    .reduce((a, r) => a + (r.items_created || 0), 0);
  const enabled = rows.filter((r) => r.enabled).length;
  const last = runs[0];

  const tiles = $("#dash-tiles");
  tiles.innerHTML = "";
  tiles.append(
    tile("Items stored", num(totals.live), addedToday ? `+${num(addedToday)} today` : "none added today",
      addedToday ? "up" : "flat"),
    tile("Sources", `${enabled} / ${rows.length}`,
      enabled === rows.length ? "all enabled" : `${rows.length - enabled} disabled`, "flat"),
    tile("Last run", last ? fmtWhen(last.started_at) : "never",
      last ? `${last.status} · ${runCounts(last)}` : "run a backup to get started",
      last && last.status === "success" ? "up" : last ? "warn" : "flat"),
    tile("Media stored", fmtBytes(metrics.media_bytes), `${num(metrics.media_count)} files`, "flat"),
  );

  const list = $("#dash-sources");
  list.innerHTML = "";
  $("#dash-sources-aux").textContent = rows.length ? `${rows.length} configured` : "";
  if (!rows.length) {
    const empty = el("div", { className: "empty-row" });
    empty.append(document.createTextNode("No sources configured yet. "));
    const a = el("a", { href: "#", textContent: "Add a source →" });
    a.addEventListener("click", (e) => { e.preventDefault(); switchTab("add"); });
    empty.append(a);
    list.append(empty);
  } else {
    rows.forEach((s) => list.append(sourceRow(s, { compact: true })));
  }
  refreshVpn(rows);
  applyVpnUI();

  const feed = $("#dash-feed");
  feed.innerHTML = "";
  if (!runs.length) {
    feed.append(el("div", { className: "empty-row", textContent: "No runs yet." }));
  } else {
    runs.slice(0, 8).forEach((r) => feed.append(feedItem(r)));
  }

  renderSparkline(runs);
}
LOADERS.dashboard = loadDashboard;

// --- sources ---------------------------------------------------------------

let CONNECTOR_BY_TYPE = {};

async function loadSources() {
  const list = $("#sources-list");
  let rows = [];
  let conns = [];
  try {
    [rows, conns] = await Promise.all([api("/api/status"), api("/api/connectors")]);
  } catch (e) { toast(e.message, "err"); return; }
  CONNECTOR_BY_TYPE = Object.fromEntries(conns.map((c) => [c.type, c]));
  updateHealthChip(rows);
  $("#nav-source-count").textContent = rows.length || "";

  list.innerHTML = "";
  if (!rows.length) {
    const empty = el("div", { className: "empty-row" });
    empty.append(document.createTextNode("No sources configured yet. "));
    const a = el("a", { href: "#", textContent: "Add a source →" });
    a.addEventListener("click", (e) => { e.preventDefault(); switchTab("add"); });
    empty.append(a);
    list.append(empty);
  } else {
    rows.forEach((s) => list.append(sourceRow(s)));
  }
  refreshVpn(rows);
  applyVpnUI();

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

// --- history (activity) ------------------------------------------------------

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
        el("td", { className: "mono", textContent: fmtWhen(r.started_at) }),
        el("td", { className: "mono", textContent: r.source_name || "?" }),
        el("td", {}, badge(r.status)),
        el("td", { className: "mono", textContent: r.mode }),
        el("td", { className: "mono num", textContent: num(r.items_created ?? 0) }),
        el("td", { className: "mono num", textContent: num(r.items_updated ?? 0) }),
        el("td", { className: "mono num", textContent: num(r.items_deleted ?? 0) }),
        el("td", { className: "muted", textContent: r.error || "—" }),
      ));
    });
  } catch (e) { toast(e.message, "err"); }
}
LOADERS.history = loadHistory;
$("#refresh-history").addEventListener("click", loadHistory);

// --- browse / library (paginated item listing + metrics + detail drawer) ----

const BROWSE_LIMIT = 50;
const BROWSE_GROUP_LIMIT = 12; // cards per source section in the grouped view
let browseOffset = 0;
const BROWSE_SOURCES = new Set(); // empty = all sources
let SOURCE_NAMES = [];            // all known sources, for the grouped view
let LIB_VIEW = localStorage.getItem("dbs-lib-view") || "cards";

function statTile(value, label) {
  return el("div", { className: "card tile" },
    el("div", { className: "label", textContent: label }),
    el("div", { className: "value", textContent: String(value) }));
}

async function loadBrowseChips() {
  const box = $("#browse-source-chips");
  let rows;
  try { rows = await api("/api/status"); } catch (_) { return; }
  SOURCE_NAMES = rows.map((r) => r.name);
  box.innerHTML = "";
  BROWSE_SOURCES.forEach((name) => {
    if (!rows.some((r) => r.name === name)) BROWSE_SOURCES.delete(name);
  });
  rows.forEach((s) => {
    const chip = el("button", {
      type: "button",
      className: "chip" + (BROWSE_SOURCES.has(s.name) ? " on" : ""),
      textContent: s.name,
    });
    chip.setAttribute("aria-pressed", BROWSE_SOURCES.has(s.name) ? "true" : "false");
    chip.addEventListener("click", () => {
      if (BROWSE_SOURCES.has(s.name)) BROWSE_SOURCES.delete(s.name);
      else BROWSE_SOURCES.add(s.name);
      chip.classList.toggle("on", BROWSE_SOURCES.has(s.name));
      chip.setAttribute("aria-pressed", BROWSE_SOURCES.has(s.name) ? "true" : "false");
      browseOffset = 0;
      loadBrowse();
    });
    box.append(chip);
  });
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
  $("#nav-item-count").textContent = totals.live ? num(totals.live) : "";
  box.append(el("div", { className: "metrics-strip" },
    statTile(num(totals.live), "live items"),
    statTile(num(totals.deleted), "deleted"),
    statTile(num(m.revision_count), "revisions"),
    statTile(num(m.media_count), "media files"),
    statTile(fmtBytes(m.media_bytes), "media stored"),
  ));
  if (m.by_source_kind.length) {
    const tbody = el("tbody");
    m.by_source_kind.forEach((r) => tbody.append(el("tr", {},
      el("td", { className: "mono", textContent: r.source }),
      el("td", {}, el("span", { className: "badge neutral", textContent: r.kind })),
      el("td", { className: "mono num", textContent: num(r.live) }),
      el("td", { className: "mono num", textContent: num(r.deleted) }),
    )));
    // Collapsed by default so the items themselves stay above the fold.
    box.append(el("details", { className: "metrics-details" },
      el("summary", { textContent: "Breakdown by source & kind" }),
      el("div", { className: "card table-wrap metrics-table-wrap" },
        el("table", {},
          el("thead", {}, el("tr", {},
            el("th", { textContent: "Source" }), el("th", { textContent: "Kind" }),
            el("th", { className: "num", textContent: "Live" }), el("th", { className: "num", textContent: "Deleted" }))),
          tbody,
        ))));
  }
}

function browseFilterValues() {
  return {
    sources: [...BROWSE_SOURCES],
    types: $("#browse-type").value.split(",").map((s) => s.trim()).filter(Boolean),
    q: $("#browse-q").value.trim(),
    since: $("#browse-since").value.trim(),
    until: $("#browse-until").value.trim(),
    deleted: $("#browse-deleted").classList.contains("on"),
  };
}

function browseParams({ source = null, limit = BROWSE_LIMIT, offset = browseOffset } = {}) {
  const f = browseFilterValues();
  const qs = new URLSearchParams();
  if (source) qs.append("source", source);
  else f.sources.forEach((v) => qs.append("source", v));
  f.types.forEach((v) => qs.append("type", v));
  if (f.q) qs.set("q", f.q);
  if (f.since) qs.set("since", f.since);
  if (f.until) qs.set("until", f.until);
  if (f.deleted) qs.set("include_deleted", "true");
  qs.set("limit", limit);
  qs.set("offset", offset);
  return qs;
}

// The first youtube tag is the list the video came from (watch-later, liked,
// playlist:<name>); other connectors' first tag is similarly the most useful.
function listTag(it) {
  const t = (it.tags || [])[0];
  return t && t !== it.item_kind ? t : null;
}

// YouTube stores no image media, but thumbnails are derivable from the video id.
function thumbUrl(it) {
  if (it.thumbnail) return it.thumbnail;
  if (it.type === "youtube" && it.url) {
    const m = it.url.match(/[?&]v=([\w-]{11})/);
    if (m) return `https://i.ytimg.com/vi/${m[1]}/mqdefault.jpg`;
  }
  return null;
}

function itemCard(it) {
  const card = el("div", { className: "item-card" + (it.deleted ? " deleted" : "") });
  const thumb = el("div", { className: "item-thumb" });
  const src = thumbUrl(it);
  if (src) {
    const img = el("img", { src, alt: "", loading: "lazy" });
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => { img.remove(); thumb.classList.add("empty"); });
    thumb.append(img);
  } else {
    thumb.classList.add("empty");
  }
  thumb.append(el("span", { className: "thumb-kind", textContent: it.item_kind }));
  card.append(thumb);
  const body = el("div", { className: "item-card-body" });
  body.append(el("div", { className: "item-title", textContent: it.title || "(untitled)" }));
  const meta = el("div", { className: "item-meta" });
  meta.append(el("span", { className: "mono", textContent: it.source }));
  const tag = listTag(it);
  if (tag) meta.append(el("span", { className: "badge neutral", textContent: tag }));
  if (it.created_at) meta.append(el("span", { className: "mono", textContent: fmtStamp(it.created_at).slice(0, 10) }));
  body.append(meta);
  card.append(body);
  card.addEventListener("click", () => openItemDrawer(it.id));
  return card;
}

function cardGrid(items) {
  const grid = el("div", { className: "item-grid" });
  items.forEach((it) => grid.append(itemCard(it)));
  return grid;
}

function setLibView(view) {
  LIB_VIEW = view;
  localStorage.setItem("dbs-lib-view", view);
  $$("#browse-view-toggle button").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === view));
  loadBrowse();
}

async function loadBrowse() {
  $$("#browse-view-toggle button").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === LIB_VIEW));
  const grouped = LIB_VIEW === "cards" && BROWSE_SOURCES.size === 0;
  $("#browse-cards").classList.toggle("hidden", LIB_VIEW !== "cards");
  $("#browse-table-wrap").classList.toggle("hidden", LIB_VIEW !== "table");
  $("#browse-pager").classList.toggle("hidden", grouped);
  if (grouped) return loadBrowseGrouped();
  if (LIB_VIEW === "cards") return loadBrowseCardsFlat();
  return loadBrowseTable();
}

// One card section per source, fetched concurrently.
async function loadBrowseGrouped() {
  const box = $("#browse-cards");
  box.innerHTML = "";
  if (!SOURCE_NAMES.length) await loadBrowseChips();
  const sections = await Promise.all(SOURCE_NAMES.map(async (name) => {
    try {
      const data = await api("/api/items?" + browseParams({ source: name, limit: BROWSE_GROUP_LIMIT, offset: 0 }));
      return { name, data };
    } catch (_) { return { name, data: null }; }
  }));
  let any = false;
  sections.forEach(({ name, data }) => {
    if (!data || !data.items.length) return;
    any = true;
    const head = el("div", { className: "lib-section-head" });
    head.append(el("h3", { textContent: name }));
    head.append(el("span", { className: "aux mono", textContent: `${num(data.total)} items` }));
    if (data.total > data.items.length) {
      const all = el("button", { className: "btn ghost small", textContent: "View all →" });
      all.addEventListener("click", () => {
        BROWSE_SOURCES.clear();
        BROWSE_SOURCES.add(name);
        browseOffset = 0;
        loadBrowseChips();
        loadBrowse();
      });
      head.append(all);
    }
    box.append(el("section", { className: "lib-section" }, head, cardGrid(data.items)));
  });
  if (!any) box.append(el("div", { className: "empty-row", textContent: "No items match these filters." }));
}

// Flat card grid (a source filter is active) with the normal pager.
async function loadBrowseCardsFlat() {
  const box = $("#browse-cards");
  box.innerHTML = "";
  let data;
  try { data = await api("/api/items?" + browseParams()); }
  catch (e) { toast(e.message, "err"); return; }
  if (!data.items.length) {
    box.append(el("div", { className: "empty-row", textContent: "No items match these filters." }));
  } else {
    box.append(cardGrid(data.items));
  }
  updateBrowsePager(data);
}

async function loadBrowseTable() {
  const tbody = $("#browse-table tbody");
  tbody.innerHTML = "";
  let data;
  try { data = await api("/api/items?" + browseParams()); }
  catch (e) { toast(e.message, "err"); return; }
  if (!data.items.length) {
    tbody.append(el("tr", {}, el("td", { colSpan: 7, className: "muted", textContent: "No items match these filters." })));
  } else {
    data.items.forEach((it) => {
      const row = el("tr", { className: "rowlink" + (it.deleted ? " deleted" : "") },
        el("td", {}, el("span", { className: "title-cell", textContent: it.title || "(untitled)" })),
        el("td", { className: "mono", textContent: it.source }),
        el("td", {}, el("span", { className: "badge neutral", textContent: listTag(it) || it.item_kind })),
        el("td", { className: "mono", textContent: fmtStamp(it.created_at) }),
        el("td", { className: "mono", textContent: fmtStamp(it.updated_at) }),
        el("td", { className: "mono num", textContent: it.revision }),
        el("td", { className: "mono num", textContent: it.media_count || "" }),
      );
      row.addEventListener("click", () => openItemDrawer(it.id));
      tbody.append(row);
    });
  }
  updateBrowsePager(data);
}

function updateBrowsePager(data) {
  $("#browse-count").textContent = data.total
    ? `${data.offset + 1}–${Math.min(data.offset + data.items.length, data.total)} of ${num(data.total)}`
    : "0 results";
  $("#browse-prev").disabled = data.offset <= 0;
  $("#browse-next").disabled = data.offset + data.items.length >= data.total;
}
LOADERS.browse = async () => {
  await loadBrowseChips(); // grouped view needs the source list first
  loadBrowseMetrics();
  loadBrowse();
};
$$("#browse-view-toggle button").forEach((b) =>
  b.addEventListener("click", () => setLibView(b.dataset.view)));
$("#refresh-browse").addEventListener("click", () => { loadBrowseChips(); loadBrowseMetrics(); loadBrowse(); });
$("#browse-filters").addEventListener("submit", (e) => { e.preventDefault(); browseOffset = 0; loadBrowse(); });
$("#browse-deleted").addEventListener("click", (e) => {
  const chip = e.currentTarget;
  chip.classList.toggle("on");
  chip.setAttribute("aria-pressed", chip.classList.contains("on") ? "true" : "false");
  browseOffset = 0;
  loadBrowse();
});
$("#browse-prev").addEventListener("click", () => { browseOffset = Math.max(0, browseOffset - BROWSE_LIMIT); loadBrowse(); });
$("#browse-next").addEventListener("click", () => { browseOffset += BROWSE_LIMIT; loadBrowse(); });

// "Export this view" carries the Library filters into the Export form, so a
// filter only ever has to be defined once.
$("#browse-export").addEventListener("click", () => {
  const f = browseFilterValues();
  $("#export-source").value = f.sources.join(", ");
  $("#export-type").value = f.types.join(", ");
  $("#export-since").value = f.since;
  $("#export-until").value = f.until;
  $("#export-deleted").checked = f.deleted;
  switchTab("export");
});

// --- item detail drawer ------------------------------------------------------

function closeDrawer() {
  const drawer = $("#item-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
}

async function openItemDrawer(id) {
  const drawer = $("#item-drawer");
  const body = $("#item-drawer-body");
  body.innerHTML = "Loading…";
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  let item;
  try { item = await api(`/api/items/${id}`); }
  catch (e) { body.textContent = e.message; return; }
  $("#item-drawer-title").textContent = item.title || item.external_id;
  body.innerHTML = "";

  body.append(el("div", { className: "row wrap" },
    el("span", { className: "badge neutral", textContent: item.item_kind }),
    item.deleted ? badge("failed", "deleted") : badge("success", "live"),
  ));

  const kv = el("dl", { className: "kv" });
  const pair = (k, v) => { if (v != null && v !== "") kv.append(el("dt", { textContent: k }), el("dd", { textContent: v })); };
  pair("Source", item.source);
  pair("External ID", item.external_id);
  pair("Created", fmtStamp(item.created_at));
  pair("Updated", fmtStamp(item.updated_at));
  pair("Revision", item.revision);
  body.append(kv);

  if (item.url) {
    body.append(el("div", {}, el("a", { href: item.url, target: "_blank", rel: "noopener", textContent: item.url })));
  }
  if (item.media && item.media.length) {
    const mediaBox = el("div", { className: "media-list" });
    item.media.forEach((m) => {
      const isImage = m.has_data && (m.mime || "").startsWith("image/");
      if (isImage) {
        mediaBox.append(el("img", { src: `/api/media/${m.id}`, alt: m.filename || "", className: "media-thumb" }));
      } else {
        const label = `${m.filename || m.kind} (${m.byte_size != null ? fmtBytes(m.byte_size) : "not stored"})`;
        mediaBox.append(m.has_data
          ? el("a", { href: `/api/media/${m.id}`, textContent: label })
          : el("span", { className: "tag", textContent: label }));
      }
    });
    body.append(mediaBox);
  }
  body.append(el("pre", { className: "log tall", textContent: JSON.stringify(item.raw, null, 2) }));
}
$("#item-drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

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
      const addBtn = el("button", { className: "btn small", textContent: "Add source" });
      addBtn.addEventListener("click", () => startAddSource(c.type));
      actions.append(addBtn);
      if (!c.ready) {
        if (META.setup_enabled) {
          const install = el("button", { className: "btn primary small", textContent: "Install" });
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
        const cap = el("button", { className: "btn small", textContent: c.auth_capture.label });
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
        el("div", { className: "row wrap", style: "gap:0.5rem;align-items:baseline;" },
          el("h3", { textContent: `${c.display_name} (${c.type})` }), ready),
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
      box.append(el("div", { className: "muted hint-line", textContent: "None of your configured sources require an API key." }));
    }
    data.secrets.forEach((s) => {
      const status = s.set
        ? badge("success", s.in_env_file ? "set" : "set (process env)")
        : badge("failed", "not set");
      const input = el("input", { type: "password", placeholder: s.set ? "replace…" : "value", autocomplete: "new-password" });
      const result = el("span", { className: "result" });
      const save = el("button", { className: "btn primary small", textContent: "Save" });
      save.addEventListener("click", () => saveSecret(s.name, input.value, result));
      const row = el("div", { className: "row wrap", style: "gap:0.5rem;" },
        el("strong", { className: "mono", textContent: s.name }), status, input, save);
      if (s.in_env_file) {
        const clear = el("button", { className: "btn small", textContent: "Clear" });
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
    const btn = el("button", { type: "button", className: "btn primary small", textContent: `${ac.label} (open browser)` });
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

// Jump to the Add-source view with a connector type preselected.
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
      requires_vpn: $("#add-requires-vpn").checked,
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
      const btn = el("button", { className: "btn primary small", textContent: "Install research deps" });
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
      const btn = el("button", { className: "btn primary small", textContent: "NotebookLM login (open browser)" });
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

  researchES = new EventSource(`/api/research/${job.id}/stream`);
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
      $("#research-download").href = `/api/research/${snap.id}/report`;
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
      box.append(el("div", {}, badge("success", "OK — no issues found")));
      return;
    }
    box.innerHTML = "";
    box.append(el("div", {}, badge("failed", `${r.issues.length} issue(s)`)));
    r.issues.forEach((i) => box.append(el("div", { className: "mono", textContent: `[${i.kind}] ${i.source}: ${i.detail}` })));
  } catch (e) { toast(e.message, "err"); }
}
$("#run-verify").addEventListener("click", runVerify);

// --- backup progress (SSE) -------------------------------------------------

let activeES = null;
let progressHadFailure = false;

function setBackupButtons(disabled) {
  // While a backup runs, lock the triggers. On finish, the source lists are
  // re-rendered with each button's correct enabled/disabled state anyway.
  $("#backup-all").disabled = disabled;
  if (disabled) $$(".run-btn").forEach((b) => { b.disabled = true; });
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
  $("#progress-pct").textContent = "";
  $("#progress-results").innerHTML = "";
  $("#progress-line").textContent = "";
  progressHadFailure = false;
  const bar = $("#progress-bar");
  bar.style.width = "0%";
  setBackupButtons(true);

  let doneCount = 0;
  let total = job.spec.all ? null : 1;

  activeES = new EventSource(`/api/backup/${job.id}/stream`);
  activeES.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.source_total) total = ev.source_total;
    const pos = ev.source_total ? `[${ev.source_index}/${ev.source_total}] ${ev.source}` : ev.source;
    if (ev.phase === "log") {
      // VPN-wrapped subprocess run: raw output lines instead of engine stats.
      $("#progress-sub").textContent = `${pos} · via VPN`;
      if (ev.note) $("#progress-line").textContent = ev.note;
    } else {
      $("#progress-sub").textContent = pos;
      const stats = `+${ev.created} ~${ev.updated} =${ev.unchanged}` + (ev.deleted ? ` ✕${ev.deleted}` : "");
      $("#progress-line").textContent = `${ev.source} [${ev.mode}] ${(ev.fetched ?? 0).toLocaleString()} fetched (${stats})`;
    }
    if (ev.phase === "source_done" && ev.result) {
      doneCount++;
      addResult(ev.result);
    }
    if (total) {
      bar.classList.remove("indeterminate");
      const pct = Math.round((doneCount / total) * 100);
      bar.style.width = pct + "%";
      $("#progress-pct").textContent = pct + "%";
    } else {
      bar.classList.add("indeterminate");
      $("#progress-pct").textContent = "";
    }
  };
  activeES.addEventListener("end", (m) => finishProgress(JSON.parse(m.data)));
  activeES.onerror = () => { /* server closed; the 'end' event handles teardown */ };
}

function addResult(r) {
  if (r.status === "failed") progressHadFailure = true;
  const line = `${r.source}: ${r.status} [${r.mode}] +${r.created} ~${r.updated} =${r.unchanged} ✕${r.deleted} (fetched ${r.fetched})`;
  $("#progress-results").append(el("div", { className: "mono " + statusClass(r.status), textContent: line + (r.error ? `  — ${r.error}` : "") }));
}

let progressHideTimer = null;

function finishProgress(snap) {
  if (activeES) { activeES.close(); activeES = null; }
  const bar = $("#progress-bar");
  bar.classList.remove("indeterminate");
  bar.style.width = "100%";
  $("#progress-pct").textContent = "100%";
  if (snap.status === "error") {
    $("#progress-results").append(el("div", { className: "st-failed mono", textContent: snap.error || "error" }));
  } else if (snap.results && $("#progress-results").childElementCount === 0) {
    snap.results.forEach(addResult); // fallback if we missed live events
  }
  // The job "succeeding" only means it ran to completion — surface per-source
  // failures in the headline rather than calling a failed run "complete".
  const failed = snap.status === "error" || progressHadFailure;
  $("#progress-title").textContent = snap.status === "error" ? "Backup failed"
    : progressHadFailure ? "Backup finished with errors" : "Backup complete";
  setBackupButtons(false);
  loadSources();
  loadDashboard();
  toast(failed ? "Backup finished with errors." : "Backup complete.", failed ? "err" : "ok");
  // Clean runs dismiss themselves; anything with a failure stays until dismissed.
  clearTimeout(progressHideTimer);
  if (!failed) {
    progressHideTimer = setTimeout(() => $("#progress").classList.add("hidden"), 10000);
  }
}
$("#progress-hide").addEventListener("click", () => {
  clearTimeout(progressHideTimer);
  $("#progress").classList.add("hidden");
});

// On load, if a backup is already running (e.g. page refresh), reattach.
async function resumeIfRunning() {
  try {
    const cur = await api("/api/backup/current");
    if (cur && cur.status === "running") openProgress(cur);
  } catch (_) {}
}

// --- boot ------------------------------------------------------------------

loadMeta();
loadDashboard();
resumeIfRunning();
resumeResearchIfRunning();
