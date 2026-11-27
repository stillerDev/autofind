/* AutoFind — glass aurora front-end.

   The server (core/api.py) owns the data; this file owns the window. Sections:
   helpers → hotkeys → theme/look → search → cards → idle → drawer panes →
   duplicates → modals → onboarding → boot. Every user-visible string comes
   from i18n.js via t(); nothing is written inline. */

"use strict";

/* ------------------------------------------------------------------ helpers */

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch (e) { /* keep */ }
    throw new Error(detail);
  }
  return response.json();
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 3200);
}

function downloadUrl(url, name) {
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function basename(path) {
  return String(path).split("\\").pop().split("/").pop();
}

function shortPath(path) {
  const parts = String(path).split(/[\\/]/);
  return parts.length > 3 ? `${parts[0]}\\…\\${parts.slice(-2).join("\\")}` : path;
}

function fmtSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1073741824) return `${(value / 1073741824).toFixed(1)} ${t("unit.gb")}`;
  if (value >= 1048576) return `${(value / 1048576).toFixed(1)} ${t("unit.mb")}`;
  if (value >= 1024) return `${Math.round(value / 1024)} ${t("unit.kb")}`;
  return `${value} ${t("unit.b")}`;
}

/* The index size in the stat grid: two tiers are all a person reads — GB when
   the index is heavy, MB when it is not. */
function sizeLabel(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1073741824) return `${(value / 1073741824).toFixed(1)} ${t("unit.gb")}`;
  return `${(value / 1048576).toFixed(1)} ${t("unit.mb")}`;
}

function fmtDate(timestamp) {
  if (!timestamp) return "";
  return new Date(Number(timestamp) * 1000).toLocaleDateString(undefined, {
    day: "numeric", month: "short", year: "numeric",
  });
}

const KIND_LABEL = {
  docs: "DOC", docx: "DOC", pdf: "PDF", image: "IMG",
  code: "PY", email: "EML", archive: "ZIP",
};

function ui(key, vars) {
  return t(key, vars);
}

/* The snippet the server returns is already anchored on the first matched
   term; this trims it to about ten words around that word so the card shows
   the match in context instead of a paragraph. */
function snippetWindow(text, query) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  if (words.length <= 11) return words.join(" ");
  const terms = String(query || "").toLowerCase().split(/\s+/)
    .filter((word) => word.length >= 3);
  let index = -1;
  for (let i = 0; i < words.length; i += 1) {
    const word = words[i].toLowerCase();
    if (terms.some((term) => word.includes(term))) { index = i; break; }
  }
  if (index === -1) index = 0;
  let start = Math.max(0, index - 4);
  const end = Math.min(words.length, start + 11);
  start = Math.max(0, end - 11);
  const prefix = start > 0 ? "… " : "";
  const suffix = end < words.length ? " …" : "";
  return prefix + words.slice(start, end).join(" ") + suffix;
}

function highlight(text, query, minLength = 3) {
  // Escape here, not in the caller: every call site feeds raw indexed file
  // content into this function, and its output goes straight into innerHTML.
  const safe = esc(text);
  const terms = [...new Set(String(query || "").toLowerCase().split(/\s+/).filter(Boolean))]
    .filter((word) => word.length >= minLength);
  if (!terms.length) return safe;
  const pattern = new RegExp(
    `(${terms.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`,
    "gi",
  );
  // Highlight only outside the entities esc() just created. A one-letter
  // query must not turn `&amp;` into `&<mark>a</mark>mp;`.
  return safe.split(/(&(?:amp|lt|gt|quot|nbsp|#39);)/).map((part) =>
    /^&(?:amp|lt|gt|quot|nbsp|#39);$/.test(part)
      ? part
      : part.replace(pattern, "<mark>$1</mark>")
  ).join("");
}

/* ------------------------------------------------- convenience: pure helpers */

function fmtAge(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  if (value < 86400) return `${Math.round(value / 3600)}h`;
  return `${Math.round(value / 86400)}d`;
}

function storageGet(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (error) {
    return fallback;
  }
}

function storageSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* session only */ }
}

function escapeRe(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const FILTER_TOKEN_RE = /(?:^|\s)((?:type|ext|in|path|name|content|modified|size|re|tag|collection|sym|from|subject|lang):)("[^"]*"|'[^']*'|\S+)/g;

/* Every filter token in the query, with its exact span so a chip can remove
   or select just that token without touching the free words around it. */
function filterTokens(raw) {
  const text = String(raw || "");
  const tokens = [];
  FILTER_TOKEN_RE.lastIndex = 0;
  let match;
  while ((match = FILTER_TOKEN_RE.exec(text))) {
    const lead = /^\s/.test(match[0]) ? 1 : 0;
    const start = match.index + lead;
    let value = match[2];
    if (/^["'].*["']$/.test(value)) value = value.slice(1, -1);
    tokens.push({
      kind: match[1].slice(0, -1),
      value,
      raw: match[1] + match[2],
      start,
      end: start + match[1].length + match[2].length,
    });
  }
  return tokens;
}

/* Token autocomplete values. Folder/tag/collection values come from live
   state so the suggestions match this machine, not a canned example. */
const TOKEN_VALUES = {
  type: ["docs", "code", "pdf", "image", "email", "archive"],
  ext: ["py", "txt", "pdf", "docx", "png", "jpg", "md", "rs"],
  name: ["report", "invoice", "photo", "notes"],
  content: ["meeting", "budget", "TODO", "error"],
  modified: ["<7d", "<30d", "7d..30d", ">30d"],
  size: [">10mb", "<1mb", "1mb..10mb"],
  re: ["error\\d+", "^TODO", "[A-Z]{2,}"],
  sym: ["parse_query", "main", "index"],
  from: ["@", "@company.com"],
  subject: ["report", "invoice"],
  lang: ["ru", "en", "zh"],
};

const TOKEN_KINDS = Object.keys(TOKEN_VALUES).concat([
  "in", "path", "tag", "collection",
]);

function tokenValues(kind) {
  if (kind === "in" || kind === "path") {
    const folders = [state.folder, ...recentQueries().flatMap((item) =>
      filterTokens(item).filter((token) => token.kind === kind).map((token) => token.value))]
      .filter(Boolean);
    return [...new Set(folders)].slice(0, 6);
  }
  if (kind === "tag") return (state.metaTags || []).slice(0, 8);
  if (kind === "collection") return (state.metaCollections || []).map((item) => item.name || item.id).slice(0, 8);
  return TOKEN_VALUES[kind] || [];
}

/* Given the whole input value, return completion rows for the word under the
   caret. Both `type:` (values) and a half-typed `ty` (key names) are covered. */
function tokenCompletions(value) {
  const text = String(value || "");
  const found = /(?:^|\s)(\S*)$/.exec(text);
  if (!found) return [];
  const word = found[1];
  if (!word) return [];
  const start = found.index + (/^\s/.test(found[0]) ? 1 : 0);
  const end = start + word.length;
  const keyMatch = word.match(/^([a-z]+):([\s\S]*)$/i);
  const wrap = (insert) => ({ value: insert, label: insert, kind: ui("token.hint"), start, end });
  if (keyMatch) {
    const kind = keyMatch[1].toLowerCase();
    if (!TOKEN_KINDS.includes(kind)) return [];
    const tail = keyMatch[2].toLowerCase();
    return tokenValues(kind)
      .filter((item) => String(item).toLowerCase().startsWith(tail))
      .map((item) => wrap(`${kind}:${item}`));
  }
  return TOKEN_KINDS
    .filter((kind) => kind.startsWith(word.toLowerCase()))
    .map((kind) => wrap(`${kind}:`));
}

const MONTH_NUMBERS = {
  january: 1, jan: 1, february: 2, feb: 2, march: 3, mar: 3, april: 4, apr: 4,
  may: 5, june: 6, jun: 6, july: 7, jul: 7, august: 8, aug: 8,
  september: 9, sep: 9, sept: 9, october: 10, oct: 10, november: 11, nov: 11,
  december: 12, dec: 12,
  \u044f\u043d\u0432\u0430\u0440\u044f: 1, \u0444\u0435\u0432\u0440\u0430\u043b\u044f: 2, \u043c\u0430\u0440\u0442\u0430: 3, \u0430\u043f\u0440\u0435\u043b\u044f: 4, \u043c\u0430\u044f: 5, \u0438\u044e\u043d\u044f: 6, \u0438\u044e\u043b\u044f: 7,
  \u0430\u0432\u0433\u0443\u0441\u0442\u0430: 8, \u0441\u0435\u043d\u0442\u044f\u0431\u0440\u044f: 9, \u043e\u043a\u0442\u044f\u0431\u0440\u044f: 10, \u043d\u043e\u044f\u0431\u0440\u044f: 11, \u0434\u0435\u043a\u0430\u0431\u0440\u044f: 12,
};
const MONTH_PATTERN = Object.keys(MONTH_NUMBERS).join("|");

/* Turn plain phrases into the duration tokens the index understands. The
   server already compiles relative phrases, but doing it here makes the
   conversion visible in the query and covers absolute dates the grammar has
   no rule for ("after 1 march", "\u043f\u043e\u0441\u043b\u0435 1 \u043c\u0430\u0440\u0442\u0430", "\u0434\u043e 2024-01-01"). */
function naturalDateCompile(raw) {
  const text = String(raw || "");
  if (/(?:^|\s)(?:modified|in|path):/.test(text)) return { query: text, changes: [] };
  const changes = [];
  const apply = (start, end, phrase, token) => {
    // A phrase like «\u043f\u043e\u0437\u0430\u0432\u0447\u0435\u0440\u0430» also contains «\u0432\u0447\u0435\u0440\u0430»: keep the first, longest
    // match rather than writing two overlapping tokens into the query.
    if (changes.some((item) => start < item.end && end > item.start)) return;
    changes.push({ start, end, phrase, token });
  };
  // Day + month [+ year], optionally preceded by a direction word.
  const absRe = new RegExp(
    `(?:(after|before|since|\u043f\u043e\u0441\u043b\u0435|\u0434\u043e|\u0441)\\s+)?(\\d{1,2})\\s+(${MONTH_PATTERN})(?:\\s+(\\d{4}))?`,
    "gi",
  );
  let match;
  while ((match = absRe.exec(text))) {
    const day = Number(match[2]);
    const month = MONTH_NUMBERS[match[3].toLowerCase()];
    const year = match[4] ? Number(match[4]) : new Date().getFullYear();
    const target = new Date(year, month - 1, day).getTime();
    if (!Number.isFinite(target)) continue;
    const days = Math.max(0, Math.round((Date.now() - target) / 86400000));
    const direction = (match[1] || "").toLowerCase();
    const token = direction === "before" || direction === "\u0434\u043e"
      ? `modified:<${days + 1}d`
      : direction ? `modified:>${days}d`
        : `modified:${days}d..${days + 1}d`;
    apply(match.index, match.index + match[0].length, match[0], token);
  }
  // ISO date with or without a direction word.
  const isoRe = /(?:(after|before|since|\u043f\u043e\u0441\u043b\u0435|\u0434\u043e|\u0441)\s+)?(\d{4}-\d{2}-\d{2})/gi;
  while ((match = isoRe.exec(text))) {
    const target = Date.parse(`${match[2]}T00:00:00`);
    if (!Number.isFinite(target)) continue;
    const days = Math.max(0, Math.round((Date.now() - target) / 86400000));
    const direction = (match[1] || "").toLowerCase();
    const token = direction === "before" || direction === "\u0434\u043e"
      ? `modified:<${days + 1}d`
      : direction ? `modified:>${days}d`
        : `modified:${days}d..${days + 1}d`;
    apply(match.index, match.index + match[0].length, match[0], token);
  }
  // Relative phrases: explicit tokens, so the query itself explains the filter.
  // JS \w is ASCII-only, so Russian suffixes use an explicit Cyrillic class.
  const RU = "[\u0430-\u044f\u0451\u0410-\u042f\u0401]";
  const RELATIVE = [
    [new RegExp(`(?:\u043f\u043e\u0437\u0430\u0432\u0447\u0435\u0440\u0430|day before yesterday)`, "gi"), "modified:2d..3d"],
    [new RegExp(`(?:\u0432\u0447\u0435\u0440\u0430\u0448\u043d${RU}*|\u0432\u0447\u0435\u0440\u0430|yesterday)`, "gi"), "modified:1d..2d"],
    [new RegExp(`(?:\u0441\u0435\u0433\u043e\u0434\u043d\u044f\u0448\u043d${RU}*|\u0441\u0435\u0433\u043e\u0434\u043d\u044f|today)`, "gi"), "modified:<1d"],
    [new RegExp(`(?:\u0437\u0430|\u043d\u0430|\u0432)\\s+(?:\u043f\u043e\u0441\u043b\u0435\u0434\u043d${RU}*|\u043f\u0440\u043e\u0448\u043b${RU}*|\u044d\u0442${RU}*)?\\s*\u043d\u0435\u0434\u0435\u043b${RU}*|(?:last|this|past)\\s+week`, "gi"), "modified:<7d"],
    [new RegExp(`(?:\u0437\u0430|\u043d\u0430|\u0432)\\s+(?:\u043f\u043e\u0441\u043b\u0435\u0434\u043d${RU}*|\u043f\u0440\u043e\u0448\u043b${RU}*|\u044d\u0442${RU}*)?\\s*\u043c\u0435\u0441\u044f\u0446${RU}*|(?:last|this|past)\\s+month`, "gi"), "modified:<30d"],
    [new RegExp(`(?:\u0437\u0430|\u043d\u0430|\u0432)\\s+(?:\u043f\u043e\u0441\u043b\u0435\u0434\u043d${RU}*|\u043f\u0440\u043e\u0448\u043b${RU}*|\u044d\u0442${RU}*)?\\s*\u0433\u043e\u0434${RU}*|(?:last|this|past)\\s+year`, "gi"), "modified:<365d"],
    [new RegExp(`(\\d{1,3})\\s*(?:\u0434\u043d${RU}*|\u0434\u043d\u0435\u0439|days?)\\s+\u043d\u0430\u0437\u0430\u0434|(\\d{1,3})\\s+days?\\s+ago`, "gi"), null],
  ];
  for (const [pattern, fixed] of RELATIVE) {
    pattern.lastIndex = 0;
    while ((match = pattern.exec(text))) {
      const token = fixed || `modified:${Number(match[1] || match[2])}d..${Number(match[1] || match[2]) + 1}d`;
      apply(match.index, match.index + match[0].length, match[0], token);
    }
  }
  if (!changes.length) return { query: text, changes: [] };
  // Longest phrases first so an overlapping match cannot eat a shorter one.
  const ordered = changes.sort((a, b) => b.start - a.start || b.end - a.end);
  let query = text;
  for (const change of ordered) {
    query = query.slice(0, change.start) + change.token + query.slice(change.end);
  }
  return { query: query.replace(/\s+/g, " ").trim(), changes: changes.reverse() };
}

/* Code tiles and the code preview share one renderer: escape, highlight, then
   pad the numbers into a fixed gutter so the lines stay aligned. */
function numberedCode(text, query, startLine = 1, minLength = 1) {
  const lines = String(text || "").split(/\r?\n/);
  return lines.map((line, index) => `
    <div class="code-line"><span class="ln">${startLine + index}</span><span class="lc">${highlight(line, query, minLength) || "&nbsp;"}</span></div>`
  ).join("");
}

/* ------------------------------------------------------------------ state */

const state = {
  query: "",
  hits: [],
  total: 0,
  nextOffset: null,
  approx: false,
  chips: [],
  facets: {},
  timer: 0,
  searchSeq: 0,
  selected: -1,
  picked: new Set(),
  kind: "",
  folder: "",
  since: "",
  docdate: false,
  sort: "best",
  types: [],
  personal: null,
  metaTags: [],
  metaCollections: [],
  hotkeys: {},
  hotkeyDefaults: {},
  hotkeyConflicts: [],
  dupMode: "images",
  dupCache: {},
  watchTimer: 0,
  watchAdded: -1,
  seal: null,
  // Convenience features (see FEATURE_ROADMAP.md). All local to the window.
  withinText: "",
  viewHits: [],
  filterStack: [],
  filterPos: -1,
  queryLog: [],
  hoverTimer: 0,
  hoverId: 0,
  freshness: null,
  exclusions: [],
  noteText: "",
  previewId: 0,
};

/* ----------------------------------------------------------------- hotkeys */

/* The defaults mirror `core/hotkeys.py`; the file on disk wins once it is
   loaded. A shortcut is a chord in canonical order (`ctrl+shift+d`). */
const FALLBACK_HOTKEYS = {
  focus: ["/"], search: ["ctrl+k"], settings: ["ctrl+,"], panel: ["ctrl+shift+i"],
  duplicates: ["ctrl+shift+d"], preview: ["space"], open: ["enter"],
  reveal: ["shift+enter"], next: ["down"], prev: ["up"], select: ["x"],
  export: ["ctrl+shift+e"],
};
const HOTKEY_ACTIONS = Object.keys(FALLBACK_HOTKEYS);
const MODIFIER_KEYS = new Set(["control", "alt", "shift", "meta"]);
const KEY_NAMES = {
  " ": "space", escape: "escape", esc: "escape", arrowup: "up", arrowdown: "down",
  arrowleft: "left", arrowright: "right", return: "enter", delete: "delete",
  backspace: "backspace", insert: "insert", "+": "plus",
};

function chordOfEvent(event) {
  const raw = String(event.key || "").toLowerCase();
  if (!raw || MODIFIER_KEYS.has(raw)) return "";
  const key = KEY_NAMES[raw] || raw;
  const mods = [];
  if (event.ctrlKey) mods.push("ctrl");
  if (event.altKey) mods.push("alt");
  if (event.shiftKey) mods.push("shift");
  if (event.metaKey) mods.push("meta");
  mods.push(key);
  return mods.join("+");
}

function chordLabel(chord) {
  return String(chord || "")
    .split("+")
    .map((part) => (part.length === 1 ? part.toUpperCase() : part))
    .join("+");
}

async function loadHotkeys() {
  try {
    const data = await api("/api/hotkeys");
    state.hotkeys = data.hotkeys || {};
    state.hotkeyDefaults = data.defaults || {};
    state.hotkeyConflicts = data.conflicts || [];
    renderHotkeys();
  } catch (error) {
    state.hotkeys = { ...FALLBACK_HOTKEYS };
    renderHotkeys();
  }
}

function actionForChord(chord) {
  if (!chord) return "";
  for (const action of HOTKEY_ACTIONS) {
    if ((state.hotkeys[action] || []).includes(chord)) return action;
  }
  return "";
}

function runHotkey(action) {
  switch (action) {
    case "focus": $("q").focus(); return true;
    case "search": $("q").focus(); $("q").select(); return true;
    case "settings": setSettings(true); return true;
    case "panel": setPanel(!$("panel").classList.contains("open")); return true;
    case "duplicates": setDups(!$("dups-drawer").classList.contains("open")); return true;
    case "export": openExport(true); return true;
    default: return false;
  }
}

function renderHotkeys() {
  const host = $("hotkeys-list");
  if (!host) return;
  host.innerHTML = HOTKEY_ACTIONS.map((action) => `
    <div class="hotkey-row">
      <span class="hk-name">${esc(t(`hotkeys.action.${action}`))}</span>
      <input data-hotkey="${action}" value="${esc((state.hotkeys[action] || []).join(", "))}"
             spellcheck="false" aria-label="${esc(t(`hotkeys.action.${action}`))}">
    </div>`).join("");
  const status = $("hotkeys-status");
  if (status) {
    status.textContent = state.hotkeyConflicts.length
      ? t("hotkeys.conflict")
      : "";
    status.classList.toggle("conflict", Boolean(state.hotkeyConflicts.length));
  }
}

async function saveHotkeys() {
  const parsed = {};
  for (const input of document.querySelectorAll("[data-hotkey]")) {
    const chords = input.value.split(",").map((c) => c.trim().toLowerCase()).filter(Boolean);
    parsed[input.dataset.hotkey] = chords;
  }
  try {
    const data = await api("/api/hotkeys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hotkeys: parsed }),
    });
    state.hotkeys = data.hotkeys || state.hotkeys;
    state.hotkeyDefaults = data.defaults || state.hotkeyDefaults;
    state.hotkeyConflicts = data.conflicts || [];
    renderHotkeys();
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

/* -------------------------------------------------------------- theme/look */

/* The window is a photo with an interface on top of it, so the interface cannot
   have one fixed palette: everything is derived from a single hue, `--hue`.
   The choice lives in `localStorage`, the picture itself on the server. */
const WALL_KEY = "autofind.wall";
const WALL_MAX_MB = 12;

const WALLS = [
  { id: "forest", wall: 'url("/fon.jpg")', hue: 205, sat: 8 },
  { id: "dusk", wall: "linear-gradient(155deg,#3b2450 0%,#1a1124 48%,#0b0a10 100%)", hue: 272, sat: 10 },
  { id: "slate", wall: "linear-gradient(155deg,#28313a 0%,#141a1f 52%,#0a0d10 100%)", hue: 208, sat: 8 },
  { id: "sand", wall: "linear-gradient(155deg,#3c3024 0%,#1e1813 52%,#0c0b09 100%)", hue: 34, sat: 11 },
  { id: "ocean", wall: "linear-gradient(155deg,#0f3a44 0%,#0a1b20 52%,#050b0d 100%)", hue: 191, sat: 12 },
];

const WALL_ZOOM_MIN = 1;
const WALL_ZOOM_MAX = 2.5;

const clamp01 = (value) => Math.min(1, Math.max(0, Number(value) || 0));

function clampScale(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 1;
  return Math.min(WALL_ZOOM_MAX, Math.max(WALL_ZOOM_MIN, Math.round(number * 100) / 100));
}

let theme = WALLS[0];

/* The second half of Appearance: everything the wallpaper does not decide.
   Tint override, blur, translucency, dimming, corner radius — all CSS
   variables, all persisted together. */
const LOOK_KEY = "autofind.look";

const DEFAULT_LOOK = {
  tintAuto: true,   // hue/sat follow the wallpaper's palette
  hue: 205,         // custom tint hue, degrees
  sat: 9,           // custom tint strength, %
  dim: 0,           // wallpaper darkening, 0..0.7
  blur: 11,         // glass blur, px (0 = sharp)
  panelAlpha: 0.72, // drawer panels' opacity
  cardAlpha: 0.82,  // result cards' opacity
  radius: 12,       // corner radius, px
};

let look = { ...DEFAULT_LOOK };

function clamp(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(max, Math.max(min, number));
}

function loadLook() {
  try {
    const saved = JSON.parse(localStorage.getItem(LOOK_KEY) || "null");
    if (!saved || typeof saved !== "object") return;
    look = {
      tintAuto: !!saved.tintAuto,
      hue: clamp(saved.hue, 0, 360, DEFAULT_LOOK.hue),
      sat: clamp(saved.sat, 0, 20, DEFAULT_LOOK.sat),
      dim: clamp(saved.dim, 0, 0.7, DEFAULT_LOOK.dim),
      blur: clamp(saved.blur, 0, 24, DEFAULT_LOOK.blur),
      panelAlpha: clamp(saved.panelAlpha, 0.3, 0.98, DEFAULT_LOOK.panelAlpha),
      cardAlpha: clamp(saved.cardAlpha, 0.4, 1, DEFAULT_LOOK.cardAlpha),
      radius: clamp(saved.radius, 0, 24, DEFAULT_LOOK.radius),
    };
  } catch (error) {
    // A broken save keeps the defaults; nothing here is worth a crash.
  }
}

function saveLook() {
  try {
    localStorage.setItem(LOOK_KEY, JSON.stringify(look));
  } catch (error) {
    // Storage blocked: the look lives for this session only.
  }
}

function applyLook() {
  const root = document.documentElement;
  root.style.setProperty("--hue", look.tintAuto
    ? String(Math.round(theme.hue))
    : String(Math.round(look.hue)));
  root.style.setProperty("--sat", look.tintAuto
    ? `${theme.sat}%`
    : `${look.sat}%`);
  root.style.setProperty("--wall-dim", String(look.dim));
  root.style.setProperty("--blur", `${look.blur}px`);
  root.style.setProperty("--panel-alpha", String(look.panelAlpha));
  root.style.setProperty("--card-alpha", String(look.cardAlpha));
  root.style.setProperty("--radius", `${look.radius}px`);
}

function applyTheme(next) {
  theme = next;
  const fx = clamp01(next.fx ?? 0.5);
  const fy = clamp01(next.fy ?? 0.5);
  const root = document.documentElement;
  root.style.setProperty("--wall", next.wall);
  root.style.setProperty("--hue", String(Math.round(next.hue)));
  root.style.setProperty("--sat", `${next.sat}%`);
  root.style.setProperty("--wall-pos", `${fx * 100}% ${fy * 100}%`);
  root.style.setProperty("--wall-scale", String(clampScale(next.scale)));
  // The look sits on top of the wallpaper: without this call a wallpaper
  // switch would repaint the palette over a custom tint.
  applyLook();
}

function renderLook() {
  const set = (id, value) => {
    const node = $(id);
    if (node) node.value = String(value);
  };
  set("look-dim", Math.round(look.dim * 100));
  set("look-blur", look.blur);
  set("look-panel", Math.round(look.panelAlpha * 100));
  set("look-card", Math.round(look.cardAlpha * 100));
  set("look-radius", look.radius);
  set("tint-hue", look.hue);
  set("tint-sat", look.sat);
  const auto = $("tint-auto");
  if (auto) auto.checked = look.tintAuto;
  const locked = look.tintAuto;
  ["tint-hue", "tint-sat"].forEach((id) => {
    const node = $(id);
    if (node) node.disabled = locked;
  });
  const put = (id, text) => {
    const node = $(id);
    if (node) node.textContent = text;
  };
  put("look-dim-val", `${Math.round(look.dim * 100)}%`);
  put("look-blur-val", `${look.blur}px`);
  put("look-panel-val", `${Math.round(look.panelAlpha * 100)}%`);
  put("look-card-val", `${Math.round(look.cardAlpha * 100)}%`);
  put("look-radius-val", `${look.radius}px`);
  put("tint-hue-val", `${Math.round(look.hue)}°`);
  put("tint-sat-val", `${Math.round(look.sat)}%`);
}

function setLook(patch) {
  look = { ...look, ...patch };
  saveLook();
  applyLook();
  renderLook();
}

function saveTheme(next) {
  try {
    localStorage.setItem(WALL_KEY, JSON.stringify(next));
  } catch (error) {
    // Storage can be full or blocked; the theme still applies for this session.
  }
}

function loadTheme() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(WALL_KEY) || "null");
  } catch (error) {
    saved = null;
  }
  if (saved && typeof saved.wall === "string" && Number.isFinite(saved.hue)) {
    const legacyY = { top: 0, center: 0.5, bottom: 1 }[saved.pos];
    applyTheme({
      id: saved.id || "custom",
      wall: saved.wall,
      hue: saved.hue,
      sat: Number.isFinite(saved.sat) ? saved.sat : 9,
      fx: saved.fx ?? 0.5,
      fy: saved.fy ?? legacyY ?? 0.5,
      scale: clampScale(saved.scale),
    });
    return;
  }
  applyTheme(WALLS[0]);
}

function renderWallEdit() {
  const edit = $("wall-edit");
  // The controls only make sense for a picture the user chose, not a gradient.
  if (edit) edit.hidden = theme.id !== "custom";
}

function renderWalls() {
  const row = $("wall-row");
  if (!row) return;
  row.querySelectorAll("[data-wall]").forEach((tile) => {
    const preset = WALLS.find((item) => item.id === tile.dataset.wall);
    // Only the image, never the `background` shorthand: the shorthand would
    // reset `cover` and the tile would show the top-left corner of the photo.
    if (preset) tile.style.backgroundImage = preset.wall;
    const active = theme.id === tile.dataset.wall;
    tile.classList.toggle("on", active);
    tile.setAttribute("aria-checked", String(active));
  });
  const custom = $("wall-pick");
  if (!custom) return;
  const active = theme.id === "custom";
  custom.classList.toggle("on", active);
  custom.style.backgroundImage = active ? theme.wall : "";
  renderWallEdit();
}

function chooseWall(id) {
  const preset = WALLS.find((item) => item.id === id);
  if (!preset) return;
  applyTheme(preset);
  saveTheme(preset);
  renderWalls();
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("could not load the image"));
    image.src = url;
  });
}

function rgbToHsl(red, green, blue) {
  const r = red / 255;
  const g = green / 255;
  const b = blue / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const lightness = (max + min) / 2;
  const delta = max - min;
  if (delta === 0) return [0, 0, lightness];
  const saturation = delta / (1 - Math.abs(2 * lightness - 1));
  let hue;
  if (max === r) hue = ((g - b) / delta) % 6;
  else if (max === g) hue = (b - r) / delta + 2;
  else hue = (r - g) / delta + 4;
  return [(hue * 60 + 360) % 360, saturation, lightness];
}

/* One hue for the whole window, taken from the picture itself: each pixel
   votes weighted by saturation and midness, votes averaged as angles. */
function hueOfImage(image) {
  const canvas = document.createElement("canvas");
  canvas.width = 32;
  canvas.height = 32;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  context.drawImage(image, 0, 0, 32, 32);
  let data;
  try {
    data = context.getImageData(0, 0, 32, 32).data;
  } catch (error) {
    return null; // a tainted canvas; the default palette is fine
  }
  let x = 0;
  let y = 0;
  let weight = 0;
  for (let index = 0; index < data.length; index += 4) {
    const [hue, saturation, lightness] = rgbToHsl(data[index], data[index + 1], data[index + 2]);
    const vote = saturation * (1 - Math.abs(2 * lightness - 1));
    if (vote <= 0.02) continue;
    const angle = (hue * Math.PI) / 180;
    x += Math.cos(angle) * vote;
    y += Math.sin(angle) * vote;
    weight += vote;
  }
  if (weight < 0.6) return null;
  const degrees = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  return Math.round(degrees * 10) / 10;
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

async function useWallFile(file) {
  if (!file) return;
  if (file.size > WALL_MAX_MB * 1048576) {
    toast(t("settings.wallTooBig", { mb: WALL_MAX_MB }));
    return;
  }
  try {
    const data = await readAsDataUrl(file);
    const saved = await api("/api/background", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    });
    let hue = 205;
    try {
      hue = hueOfImage(await loadImage(saved.url)) ?? 205;
    } catch (error) {
      hue = 205; // the picture is fine, only the measuring failed
    }
    const next = { id: "custom", wall: `url("${saved.url}")`, hue, sat: 9, fx: 0.5, fy: 0.5, scale: 1 };
    applyTheme(next);
    saveTheme(next);
    renderWalls();
    toast(t("settings.wallSaved"));
  } catch (error) {
    toast(t("settings.wallFailed", { error: error.message }));
  }
}

/* ------------------------------------------------------------ wall editor */

const editor = {
  open: false,
  image: null,
  url: null,
};

function wallImageUrl() {
  const match = theme.wall.match(/^url\("(.*)"\)$/);
  return match ? match[1] : null;
}

async function openWallEditor() {
  if (theme.id !== "custom") return;
  const url = wallImageUrl();
  if (!url) return;
  editor.open = true;
  $("wall-editor").classList.add("open");
  syncWallZoom();
  if (editor.url === url && editor.image) return;
  editor.url = url;
  editor.image = null;
  try {
    editor.image = await loadImage(url);
  } catch (error) {
    editor.image = null;
  }
}

function closeWallEditor() {
  editor.open = false;
  $("wall-editor").classList.remove("open");
}

function syncWallZoom() {
  $("wall-zoom").value = String(clampScale(theme.scale));
}

function editWall(next) {
  applyTheme({ ...theme, ...next });
  saveTheme(theme);
  renderWalls();
}

function stageCover(stage, image) {
  const factor = Math.max(stage.clientWidth / image.naturalWidth,
                          stage.clientHeight / image.naturalHeight);
  return {
    width: image.naturalWidth * factor,
    height: image.naturalHeight * factor,
  };
}

function setWallFocus(fx, fy) {
  editWall({ fx: clamp01(fx), fy: clamp01(fy) });
}

function resetWallFrame() {
  editWall({ fx: 0.5, fy: 0.5, scale: 1 });
  syncWallZoom();
}

/* ------------------------------------------------------------------ search */

const RECENT_KEY = "af.recent-queries";

function recentQueries() {
  try {
    const value = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(value) ? value : [];
  } catch (error) {
    return [];
  }
}

function rememberQuery(query) {
  const clean = String(query || "").trim();
  if (!clean) return;
  const list = recentQueries().filter((item) => item !== clean);
  list.unshift(clean);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 8)));
  } catch (error) {
    /* private mode loses the memory, nothing else breaks */
  }
}

function scheduleSearch() {
  clearTimeout(state.timer);
  state.timer = setTimeout(runSearch, 150);
}

function searchParams(offset) {
  const params = new URLSearchParams({ q: state.query, limit: "12", offset: String(offset) });
  if (state.kind) params.set("kind", state.kind);
  if (state.folder) params.set("folder", state.folder);
  if (state.since) params.set("since", state.since);
  if (state.docdate) params.set("docdate", "1");
  if (state.sort && state.sort !== "best") params.set("sort", state.sort);
  return params;
}

async function runSearch(options = {}) {
  const append = Boolean(options.append);
  const rawQuery = $("q").value.trim();
  state.query = rawQuery;
  // Plain-language dates become the duration tokens the index understands;
  // the query itself is rewritten so the filter is visible and removable.
  const compiled = naturalDateCompile(state.query);
  state.query = compiled.query;
  if (compiled.changes.length && !append) {
    $("q").value = state.query;
    noteNaturalDate(compiled.changes);
  }
  $("save-btn").classList.toggle("show", Boolean(state.query));
  if (!state.query) {
    // Invalidate a response already in flight for the previous query.
    state.searchSeq += 1;
    state.hits = [];
    state.total = 0;
    state.nextOffset = null;
    clearPicks();
    state.withinText = "";
    renderIdle();
    const chips = $("query-chips");
    if (chips) chips.hidden = true;
    $("meta").textContent = "";
    return;
  }
  const offset = append ? (state.nextOffset ?? 0) : 0;
  const moreBtn = append ? $("more-btn") : null;
  if (moreBtn) moreBtn.remove();
  // A slow response must never overwrite a newer one: every search gets a
  // sequence number and only the latest may paint.
  document.querySelectorAll(".suggestion-inline").forEach((el) => el.remove());
  const seq = ++state.searchSeq;
  try {
    const data = await api(`/api/search?${searchParams(offset)}`);
    if (seq !== state.searchSeq) return;
    state.hits = append ? state.hits.concat(data.hits) : data.hits;
    state.total = data.total ?? data.hits.length;
    state.nextOffset = data.next_offset ?? null;
    state.approx = Boolean(data.approx);
    state.chips = data.chips || [];
    state.facets = data.facets || {};
    if (!append) {
      rememberQuery(rawQuery || state.query);
      clearPicks();
      state.withinText = "";
      if (!options.skipHistory) pushFilterHistory(rawQuery || state.query);
    }
    renderResults(data, append);
    recordHistory(rawQuery || state.query, state.total);
    const shown = visibleHits().length;
    $("meta").textContent = shown
      ? t("search.meta", {
        shown, total: state.total, ms: data.ms, sign: state.approx ? "≈" : "",
      })
      : `${data.ms} ${t("unit.ms")}`;
    if (data.suggestion && !append) {
      const hint = document.createElement("button");
      hint.type = "button";
      hint.className = "suggestion-inline";
      hint.textContent = t("hint.did_you_mean", { value: data.suggestion });
      hint.addEventListener("click", () => {
        $("q").value = data.suggestion;
        scheduleSearch();
        hint.remove();
      });
      $("meta").after(hint);
    }
  } catch (error) {
    if (moreBtn) $("content").appendChild(moreBtn);
    toast(t("search.failed", { error: error.message }));
  }
}

function clearChip(source) {
  const input = $("q");
  if (!source) return;
  input.value = input.value.replace(source, " ").replace(/\s+/g, " ").trim();
  clearTimeout(state.timer);
  runSearch();
  input.focus();
}

/* -------------------------------------------------------------- result card */

function thumbZone(hit) {
  if (hit.is_image && hit.thumb) {
    return `<div class="gthumb">
      <img src="${hit.thumb}" alt="" loading="lazy">
      <div class="ocr-boxes" data-q="${esc(state.query)}"></div>
      ${kindBadges(hit)}
    </div>`;
  }
  // Text-bearing kinds get a live page: the first lines of the real extracted
  // text, hydrated below from the same preview endpoint Quick Look uses.
  const inner = {
    pdf: `<div class="sheet page" data-text-id="${hit.file_id}" style="--sheet-accent:#c94747"></div>`,
    docs: `<div class="sheet page" data-text-id="${hit.file_id}" style="--sheet-accent:#3f6fc4"></div>`,
    docx: `<div class="sheet page" data-text-id="${hit.file_id}" style="--sheet-accent:#3f6fc4"></div>`,
    text: `<div class="sheet page" data-text-id="${hit.file_id}"></div>`,
    code: `<div class="term page is-code" data-text-id="${hit.file_id}"></div>`,
    email: `<div class="sheet page" data-text-id="${hit.file_id}" style="--sheet-accent:#8a63b8"></div>`,
    archive: `<div class="pack"></div>`,
  }[hit.kind] || `<div class="sheet"></div>`;
  return `<div class="gthumb">
    <div class="kind-tile" data-snippet="${esc(hit.snippet || "")}">${inner}</div>
    ${kindBadges(hit)}
  </div>`;
}

/* Fill the document tiles with their real text. One request per file, cached
   for the session: a re-render (tag added, language switch) must not refetch. */
const TEXT_CACHE = new Map();

async function hydrateDocThumbs() {
  const hosts = document.querySelectorAll("[data-text-id]:not([data-hydrated])");
  if (!hosts.length) return;
  for (const host of hosts) {
    const id = Number(host.dataset.textId);
    if (!id) continue;
    host.dataset.hydrated = "1";
    try {
      let text = TEXT_CACHE.get(id);
      if (text === undefined) {
        // 520 characters hid matches that live further down a file. Read a
        // real window so the tile can show and mark the matching line.
        const data = await api(`/api/preview?id=${id}&chars=4000`);
        text = String(data.text || "");
        TEXT_CACHE.set(id, text);
      }
      if (!text) continue;
      const terms = String(state.query || "").toLowerCase().split(/\s+/)
        .filter((word) => word.length >= 2);
      const matches = (value) => terms.some((word) => value.toLowerCase().includes(word));
      let all = text.split(/\s*\n\s*/).map((line) => line.trim()).filter(Boolean);
      if (!all.some(matches) && host.parentElement?.dataset.snippet) {
        // The server snippet is anchored on the match: use it when a long
        // file's matching line is beyond the preview window.
        all = String(host.parentElement.dataset.snippet).split(/\s*\n\s*/)
          .map((line) => line.trim()).filter(Boolean);
      }
      const first = Math.max(0, all.findIndex(matches));
      const lines = all.slice(first, first + 12)
        .map((line) => (line.length > 60 ? `${line.slice(0, 60)}…` : line));
      // The code tile is a tiny preview of the real file. Number the lines
      // and mark the query there too — the user is looking at the tile.
      host.innerHTML = host.classList.contains("is-code")
        ? numberedCode(lines.join("\n"), state.query, 1, 1)
        : highlight(lines.join("\n"), state.query, 1);
    } catch (error) {
      /* a failed hydration keeps the static tile */
    }
  }
}

function kindBadges(hit) {
  const badges = (hit.channels || [])
    .filter((channel) => channel === "ocr")
    .map((channel) => `<span class="gbadge b-${channel}">${channel}</span>`)
    .join("");
  return badges;
}

function card(hit, index, order = 0) {
  const delay = Math.min(order, 12) * 38;
  const picked = state.picked.has(hit.file_id);
  const snippetText = snippetWindow(hit.snippet, state.query);
  const snippet = snippetText
    ? `<div class="gsnippet">${highlight(snippetText, state.query)}</div>` : "";
  const tags = (hit.tags || []).map((tag) => `
    <button class="tag-chip" type="button" data-tag-run="${esc(tag)}">#${esc(tag)}</button>`
  ).join("");
  const similar = `<button class="gact" data-similar="${hit.file_id}">${esc(t("card.similar"))}</button>`;
  const group = hit.group_size > 1
    ? `<button class="badge b-group" data-group="${hit.file_id}"
         title="${esc(t("card.showAll", { n: hit.group_size }))}">${hit.group_size}×</button>`
    : "";
  const channels = (hit.channels || [])
    .filter((channel) => channel !== "ocr")
    .map((channel) => `<span class="badge b-${esc(channel)}">${esc(channel)}</span>`)
    .join("");
  const freq = Number(hit.opens) > 0
    ? `<span class="freq-badge" title="${esc(t("preview.opened", { n: hit.opens }))}">${esc(ui("freq", { n: hit.opens }))}</span>`
    : "";
  const pinBest = index === 0
    ? `<button class="gact pin-best" type="button" data-pin-best="${hit.file_id}"
            title="${esc(ui("pinBest"))}">★ ${esc(ui("pinBest"))}</button>` : "";
  return `
    <div class="gcard${picked ? " picked" : ""}" data-index="${index}" data-file="${hit.file_id}"
         draggable="true" data-drag-file="${hit.file_id}"
         style="--enter-delay:${delay}ms">
      <label class="card-pick-wrap" title="${esc(t("card.select"))}">
        <input type="checkbox" class="card-pick" data-pick="${hit.file_id}"
               aria-label="${esc(t("card.select"))}"${picked ? " checked" : ""}>
      </label>
      ${thumbZone(hit)}
      <div class="gbody">
        <div class="gtitle">
          <a class="gname" data-open="${hit.file_id}" title="${esc(hit.path)}">${esc(hit.name)}</a>
          <span class="badge">${esc(hit.kind)}</span>
          ${channels}
          ${group}
          ${hit.symbol ? `<span class="badge b-def" title="${esc(t("card.symbol"))}">${esc(hit.symbol)}</span>` : ""}
          <button class="pin-star${hit.pinned ? " on" : ""}" type="button"
                  data-pin="${hit.file_id}" aria-pressed="${hit.pinned ? "true" : "false"}"
                  title="${esc(t(hit.pinned ? "card.unpin" : "card.pin"))}">★</button>
        </div>
        <div class="gpath" title="${esc(hit.path)}">${esc(shortPath(hit.path))} · ${fmtSize(hit.size)} · ${fmtDate(hit.mtime)}${freq}</div>
        ${tags ? `<div class="gtags">${tags}</div>` : ""}
        ${snippet}
        <div class="gactions">
          <button class="gact" data-open="${hit.file_id}">${esc(t("card.open"))}</button>
          <button class="gact" data-reveal="${hit.file_id}">${esc(t("card.folder"))}</button>
          <button class="gact" data-preview="${hit.file_id}">${esc(t("card.preview"))}</button>
          <button class="gact" data-tag-add="${hit.file_id}">${esc(t("card.tag"))}</button>
          <button class="gact" data-tag-menu="${hit.file_id}" aria-expanded="false">${esc(t("card.tag"))} ▾</button>
          <button class="gact" data-collection-add="${hit.file_id}">${esc(t("card.collection"))}</button>
          <button class="gact" data-why="${hit.file_id}">${esc(t("card.why"))}</button>
          ${pinBest}
          ${similar}
        </div>
      </div>
    </div>`;
}

function renderResults(data, append) {
  const host = $("content");
  const grid = document.createElement("div");
  grid.className = "results-grid";
  const view = visibleHits();
  // On append, render ONLY the newly fetched hits. Building all hits again and
  // then moving them into the old grid duplicates every existing card.
  const fresh = append ? new Set((data.hits || []).map((hit) => hit.file_id)) : null;
  const rows = append ? view.filter((hit) => fresh.has(hit.file_id)) : view;
  grid.innerHTML = rows.map((hit, index) => {
    const absolute = view.indexOf(hit);
    return card(hit, absolute, append ? index : absolute);
  }).join("");
  if (append) {
    const old = host.querySelector(".results-grid");
    if (old) {
      while (grid.firstChild) old.appendChild(grid.firstChild);
      grid.remove();
    } else {
      host.innerHTML = "";
      host.appendChild(grid);
    }
  } else {
    host.innerHTML = "";
    host.appendChild(grid);
  }
  if (state.nextOffset !== null) {
    const more = document.createElement("button");
    more.type = "button";
    more.id = "more-btn";
    more.className = "more-btn";
    more.textContent = t("results.more");
    more.addEventListener("click", () => runSearch({ append: true }));
    host.appendChild(more);
  }
  paintFacets();
  paintOcrBoxes();
  hydrateDocThumbs();
  renderQueryChips();
}

/* The "search within results" filter is client-side: the server page stays
   untouched, only the cards on screen are narrowed. Index lookup helpers use
   this same view so keyboard actions stay aligned with what is painted. */
function visibleHits() {
  const term = state.withinText.trim().toLowerCase();
  if (!term) return state.hits;
  return state.hits.filter((hit) => [hit.name, hit.path, hit.snippet, ...(hit.tags || [])]
    .some((value) => String(value || "").toLowerCase().includes(term)));
}

function paintFacets() {
  const facets = state.facets || {};
  document.querySelectorAll(".chip[data-kind]").forEach((chip) => {
    const kind = chip.dataset.kind;
    const count = kind === "" ? (facets.all ?? (state.total || null)) : facets[kind];
    // Rebuild the base from the i18n key: a cached textContent from the
    // previous language would survive a RU→EN switch.
    const key = chip.dataset.i18n;
    const base = (key && t(key)) || chip.dataset.base || chip.textContent;
    chip.dataset.base = base;
    chip.textContent = count != null ? `${base} · ${count}` : base;
  });
}

function clearPicks() {
  state.picked.clear();
  updateBulkBar();
}

function updateBulkBar() {
  const bar = $("bulk-bar");
  const count = state.picked.size;
  bar.hidden = count === 0;
  if (count) $("bulk-count").textContent = t("bulk.count", { n: count });
}

/* -------------------------------------------------------- ocr boxes on cards */

async function paintOcrBoxes() {
  const holders = document.querySelectorAll(".ocr-boxes[data-q]");
  if (!holders.length) return;
  for (const holder of holders) {
    const imageId = Number(holder.closest("[data-file]")?.dataset.file || 0);
    const query = holder.dataset.q;
    if (!imageId || !query) continue;
    try {
      const data = await api(`/api/ocr-boxes?id=${imageId}&q=${encodeURIComponent(query)}`);
      if (!data.boxes?.length || !data.width || !data.height) continue;
      holder.innerHTML = data.boxes.map((box) => {
        const left = (box.x / data.width) * 100;
        const top = (box.y / data.height) * 100;
        const width = (box.w / data.width) * 100;
        const height = (box.h / data.height) * 100;
        return `<span class="ocr-box" style="left:${left}%;top:${top}%;width:${width}%;height:${height}%"></span>`;
      }).join("");
    } catch (error) {
      /* a failed overlay must never break the card */
    }
  }
}

/* ------------------------------------------------------------------- idle */

function renderIdle() {
  paintFacets();
  $("content").innerHTML = `
    <div class="empty">
      <div class="empty-body">
        <div class="empty-card">
          <h2>${esc(t("idle.title"))}</h2>
          <p>${esc(t("idle.body"))}</p>
          <p class="empty-tip">${esc(t("idle.tip"))}</p>
        </div>
        <div class="personal" id="personal" hidden></div>
        <div class="recent" id="recent" hidden></div>
      </div>
    </div>`;
  Promise.all([loadPersonal(), loadMeta()]).then(paintPersonal);
  loadRecent();
}

let recentImages = null;

async function loadRecent() {
  const host = $("recent");
  if (!host) return;
  if (recentImages) { paintRecent(host); return; }
  try {
    const data = await api("/api/images?limit=6");
    recentImages = (data.images || []).slice(0, 6);
    if ($("recent")) paintRecent($("recent"));
  } catch (error) {
    recentImages = null;
  }
}

function shot(item) {
  return `<div class="shot" data-preview="${item.file_id}" title="${esc(item.path)}">
    <img src="${item.thumb}" alt="" loading="lazy">
    <div class="ocr-boxes" data-q="${esc(state.query)}"></div>
  </div>`;
}

function paintRecent(host) {
  if (!recentImages || !recentImages.length) return;
  host.innerHTML = `<h3>${esc(t("idle.recent"))}</h3>
    <div class="grid">${recentImages.map(shot).join("")}</div>`;
  host.hidden = false;
}

async function loadPersonal(force) {
  if (state.personal && !force) return state.personal;
  try {
    state.personal = await api("/api/personal");
  } catch (error) {
    state.personal = null;
  }
  return state.personal;
}

async function loadMeta(force) {
  try {
    const [tags, collections] = await Promise.all([
      force ? api("/api/tags") : Promise.resolve(state.metaTags.length ? null : api("/api/tags")),
      force ? api("/api/collections") : Promise.resolve(state.metaCollections.length ? null : api("/api/collections")),
    ]);
    if (tags) state.metaTags = tags.tags || [];
    if (collections) state.metaCollections = collections.collections || [];
  } catch (error) {
    /* filters keep whatever they had */
  }
}

function paintPersonal() {
  const host = $("personal");
  if (!host || !state.personal) return;
  const pins = state.personal.pins || [];
  const saved = state.personal.saved || [];
  const aliases = state.personal.aliases || [];
  const sections = [];
  if (pins.length) {
    sections.push(`<section><h3>${esc(t("idle.pinned"))}</h3>${pins.map((pin) => `
      <div class="pin-row" data-open="${pin.file_id}" title="${esc(pin.path)}">
        <span class="p-name">${esc(pin.name)}</span>
        <span class="p-meta">${fmtSize(pin.size)}</span>
        <button class="row-x" data-unpin="${pin.file_id}" aria-label="${esc(t("card.unpin"))}">×</button>
      </div>`).join("")}</section>`);
  }
  if (saved.length) {
    sections.push(`<section><h3>${esc(t("idle.saved"))}</h3>${saved.map((item) => `
      <div class="saved-row" data-saved-run="${esc(item.query)}">
        <span class="s-q">${esc(item.label)}</span>
        <span class="s-used">${item.used || 0}</span>
        <button class="row-x" data-saved-drop="${item.id}" aria-label="${esc(t("saved.forget"))}">×</button>
      </div>`).join("")}</section>`);
  }
  if (aliases.length) {
    sections.push(`<section><h3>${esc(t("alias.title"))}</h3>${aliases.map((item) => `
      <div class="alias-row"><span>${esc(item.alias)}</span><span>→ ${esc(item.expand)}</span></div>`).join("")}</section>`);
  }
  if (!sections.length) { host.hidden = true; renderSmartCollections(); return; }
  host.innerHTML = sections.join("");
  host.hidden = false;
  renderSmartCollections();
}

/* ---------------------------------------------------------------- drawer */

const DRAWER_TITLES = {
  index: "rail.indexShort", privacy: "rail.privacyShort", encryption: "rail.encryptionShort",
  search: "rail.searchShort", keys: "rail.keysShort", appearance: "rail.appearanceShort",
};

function setPanel(open, tab) {
  const drawer = $("panel");
  drawer.classList.toggle("open", Boolean(open));
  drawer.setAttribute("aria-hidden", String(!open));
  $("scrim").classList.toggle("show", Boolean(open) && !$("dups-drawer").classList.contains("open"));
  $("panel-btn").classList.toggle("on", Boolean(open));
  if (open && tab) setDrawerTab(tab);
  updateActionButtons();
}

function setDrawerTab(tab) {
  state.drawerTab = tab;
  document.querySelectorAll(".drawer-tab").forEach((node) => {
    node.classList.toggle("active", node.dataset.drawerTab === tab);
  });
  document.querySelectorAll(".drawer-pane").forEach((node) => {
    node.classList.toggle("active", node.dataset.drawerPane === tab);
  });
  $("drawer-title").textContent = t(DRAWER_TITLES[tab] || "rail.indexShort");
  if (tab === "search") {
    loadServerHistory();
    renderSmartCollections();
    renderCollectionTargets();
    loadPersonal().then(renderAliases);
  } else if (tab === "privacy") {
    refreshPrivacy();
  } else if (tab === "seal") {
    api("/api/seal").then(renderSeal).catch(() => {});
  }
  paintDynamicLabels();
}

// A few elements carry state, not static text: applyStaticText() must not
// clobber them on a language switch or on drawer navigation.
function paintDynamicLabels() {
  const sort = $("sort-value");
  if (sort) sort.textContent = t(`sort.${state.sort}`);
  const title = $("drawer-title");
  if (title) title.textContent = t(DRAWER_TITLES[state.drawerTab] || "rail.indexShort");
}

function updateActionButtons() {
  $("dups-btn").classList.toggle("on", $("dups-drawer").classList.contains("open"));
  $("panel-btn").classList.toggle("on", $("panel").classList.contains("open"));
  const trash = $("dups-delete");
  if (trash) trash.disabled = selectedDups().length === 0;
}

/* ------------------------------------------------------------------ stats */

function setStatusPill(stats) {
  const dot = $("status-dot");
  const text = $("status-text");
  if (stats && stats.files) {
    text.textContent = plural(stats.files, "status.files");
    dot.style.background = "var(--teal)";
    dot.classList.add("live");
  } else {
    text.textContent = t("status.empty");
    dot.style.background = "var(--acc)";
    dot.classList.remove("live");
  }
}

async function loadStats() {
  try {
    const [stats, status] = await Promise.all([api("/api/stats"), api("/api/status")]);
    setStatusPill(stats);
    const put = (id, value) => { const node = $(id); if (node) node.textContent = value; };
    put("s-files", stats.files ?? 0);
    put("s-chunks", stats.chunks ?? 0);
    put("s-images", stats.images ?? 0);
    put("s-ocr", stats.ocr_images ?? 0);
    put("s-size", sizeLabel(stats.db_bytes));
    put("s-symbols", stats.symbols ?? 0);
    put("s-symbols2", stats.symbols ?? 0);
    put("s-texts", stats.texts ?? 0);
    put("s-sealed", stats.texts_sealed ?? 0);
    return { stats, status };
  } catch (error) {
    $("status-text").textContent = t("status.down");
    return null;
  }
}

/* --------------------------------------------------------- index + watch */

function pollJob() {
  api("/api/progress").then((job) => {
    paintJob(job);
    // A failed run paints the bar red, never the success color.
    if (job.error) $("bar").style.background = "var(--red)";
    if (job.running) setTimeout(pollJob, 450);
    else {
      $("index-btn").disabled = false;
      loadStats();
    }
  }).catch(() => {});
}

function paintJob(job) {
  const done = job.total ? Math.round((job.position / job.total) * 100) : 0;
  const bar = $("bar");
  if (job.error) {
    bar.style.width = "0";
    bar.style.background = "var(--red)";
  } else {
    bar.style.background = "";
    bar.style.width = `${job.running ? done : 100}%`;
  }
  $("progress-label").textContent = job.running
    ? `${job.position}/${job.total} · ${shortPath(job.path)}`
    : (job.result
      ? t("panel.ready")
      : (job.error || t("panel.ready")));
  $("job-stage").textContent = job.running
    ? `${job.status || ""} ${job.position}/${job.total}`
    : (job.error ? job.error : t("panel.jobIdle"));
  const jobBar = $("job-bar");
  if (jobBar) jobBar.style.width = `${job.running ? done : (job.result ? 100 : 0)}%`;
}

function indexNow() {
  const roots = $("roots").value.split("\n").map((line) => line.trim()).filter(Boolean);
  $("index-btn").disabled = true;
  api("/api/index", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ roots, prune: $("prune").checked }),
  }).then(() => {
    pollJob();
  }).catch((error) => {
    toast(error.message);
    $("index-btn").disabled = false;
    api("/api/progress").then((job) => { if (job.running) pollJob(); });
  });
}

async function toggleWatch() {
  const on = Boolean(state.watchRunning);
  try {
    const watch = await api("/api/watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !on, roots: $("roots").value.split("\n").map((l) => l.trim()).filter(Boolean) }),
    });
    renderWatch(watch);
    toast(watch.running ? t("toast.watchOn") : t("toast.watchOff"));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

function renderWatch(watch) {
  state.watchRunning = Boolean(watch && watch.running);
  const on = state.watchRunning;
  $("watch-btn").classList.toggle("primary", on);
  $("watch-label").textContent = on
    ? t("panel.watchOn", {
      added: watch.added, removed: watch.removed, batch: watch.last_batch,
    })
    : (watch && watch.last_error
      ? t("panel.watchError", { error: watch.last_error })
      : t("panel.watchingOff"));
}

function startWatchPoll() {
  clearInterval(state.watchTimer);
  state.watchTimer = setInterval(async () => {
    try {
      const watch = await api("/api/watch");
      renderWatch(watch);
      if (!watch.running) { state.watchAdded = -1; return; }
      if (state.watchAdded >= 0 && watch.added > state.watchAdded) {
        toast(t("toast.newFiles", { n: watch.added - state.watchAdded }));
      }
      state.watchAdded = watch.added;
    } catch (error) {
      /* a dropped poll is not a notification */
    }
  }, 6000);
}

/* --------------------------------------------------------------- privacy */

const PRIVACY_KEY = "privacy.";

function renderPrivacy(privacy) {
  const catalog = privacy.categories || [];
  const host = $("privacy-filters-list");
  const signature = catalog.map((item) => item.id).join(",");
  if (host.dataset.signature !== signature) {
    host.innerHTML = catalog.map((item) => `
      <label class="stat-row">
        <span>${esc(t(PRIVACY_KEY + item.id))}</span>
        <input type="checkbox" data-privacy="${esc(item.id)}">
      </label>`).join("");
    host.dataset.signature = signature;
  }
  for (const box of host.querySelectorAll("[data-privacy]")) {
    const item = catalog.find((entry) => entry.id === box.dataset.privacy);
    box.checked = Boolean(item && item.enabled);
    box.disabled = !privacy.enabled;
  }
  $("privacy-enabled").checked = Boolean(privacy.enabled);
  $("p-enabled").textContent = privacy.enabled ? t("panel.on") : t("panel.off");
  $("p-count").textContent = privacy.count ?? 0;
  $("purge-btn").disabled = !privacy.count;
  const chosen = catalog.filter((item) => item.enabled).length;
  $("privacy-summary").textContent = !privacy.enabled
    ? t("panel.privacyOff")
    : chosen === catalog.length
      ? t("panel.privacyAll")
      : t("panel.privacySome", { n: chosen, total: catalog.length });
}

async function refreshPrivacy() {
  const privacy = await api("/api/privacy").catch(() => null);
  if (privacy) renderPrivacy(privacy);
  return privacy;
}

async function savePrivacy(patch) {
  try {
    const privacy = await api("/api/privacy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    renderPrivacy(privacy);
    toast(t("toast.privacySaved"));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

/* ------------------------------------------------------------------ seal */

function renderSeal(seal) {
  state.seal = seal;
  $("seal-state").textContent = seal.sealed ? t("panel.sealed") : t("panel.notSealed");
  const button = $("seal-btn");
  button.textContent = seal.sealed ? t("panel.unsealBtn") : t("panel.sealBtn");
  button.disabled = Boolean(seal.working);
  const progress = $("seal-progress");
  if (seal.working) {
    progress.hidden = false;
    const total = seal.total || 1;
    const percent = Math.min(99, Math.round(((seal.done || 0) / total) * 100));
    $("seal-progress-fill").style.width = `${percent}%`;
    const rawStage = String(seal.stage || "");
    // The server sends lowercase "seal"/"unseal"; only the first letter needs
    // capitalising to reach panel.sealStageSeal / panel.sealStageUnseal.
    const stageKey = "panel.sealStage" + rawStage.charAt(0).toUpperCase() + rawStage.slice(1);
    const stage = t(stageKey) !== stageKey ? t(stageKey) : seal.stage || "";
    $("seal-progress-label").textContent = `${stage} · ${percent}%`;
    $("seal-warn").hidden = true;
  } else {
    progress.hidden = true;
    $("seal-progress-fill").style.width = "0";
    if (seal.error) {
      $("seal-warn").hidden = false;
      $("seal-warn").textContent = seal.error;
    } else {
      $("seal-warn").hidden = !seal.sealed;
      if (seal.sealed) $("seal-warn").textContent = t("panel.sealWarn");
    }
  }
}

async function pollSeal() {
  for (;;) {
    try {
      const seal = await api("/api/seal");
      renderSeal(seal);
      $("seal-btn").disabled = false;
      if (!seal.working) break;
    } catch (error) {
      $("seal-btn").disabled = false;
      return;
    }
    await sleep(400);
  }
  loadStats();
}

/* ------------------------------------------------------- aliases + types */

function renderAliases() {
  const host = $("alias-list");
  if (!host) return;
  const aliases = (state.personal && state.personal.aliases) || [];
  host.innerHTML = aliases.length
    ? aliases.map((item) => `
      <div class="alias-row">
        <span>${esc(item.alias)}</span><span class="alias-arrow">→</span>
        <span>${esc(item.expand)}</span>
        <button class="row-x a-x" data-alias-drop="${esc(item.alias)}"
                aria-label="${esc(t("alias.removed", { alias: item.alias }))}">×</button>
      </div>`).join("")
    : `<p class="settings-note" id="alias-empty">${esc(t("alias.empty"))}</p>`;
}

function renderTypes() {
  const host = $("types-list");
  if (!host) return;
  host.innerHTML = state.types.length
    ? state.types.map((type) => `
      <div class="type-card" data-type="${esc(type.id || "")}">
        <input class="type-name" data-type-name value="${esc(type.name || "")}"
               placeholder="${esc(t("types.name"))}" spellcheck="false">
        <textarea class="type-masks" data-type-masks rows="2"
                  placeholder="${esc(t("types.mask"))}" spellcheck="false">${esc((type.masks || []).join("\n"))}</textarea>
        <div style="display:flex;justify-content:flex-end">
          <button class="row-x" data-type-remove aria-label="${esc(t("types.remove"))}">×</button>
        </div>
      </div>`).join("")
    : `<p class="settings-note" id="types-empty">${esc(t("types.empty"))}</p>`;
}

async function loadTypes() {
  try {
    const data = await api("/api/types");
    state.types = data.types || [];
  } catch (error) {
    state.types = [];
  }
  renderTypes();
}

async function saveTypes() {
  const rows = [];
  for (const cardNode of document.querySelectorAll("#types-list .type-card")) {
    const name = cardNode.querySelector("[data-type-name]")?.value.trim();
    const masks = (cardNode.querySelector("[data-type-masks]")?.value || "")
      .split("\n").map((line) => line.trim()).filter(Boolean);
    const id = cardNode.dataset.type;
    if (name && masks.length) rows.push({ id: id || undefined, name, masks });
  }
  try {
    const data = await api("/api/types", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ types: rows }),
    });
    state.types = data.types || rows;
    renderTypes();
    paintCustomChips();
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

function paintCustomChips() {
  const menu = $(".menu");
  if (!menu) return;
  menu.querySelectorAll("[data-kind^='custom:']").forEach((node) => node.remove());
  for (const type of state.types) {
    if (!type.id || !type.name) continue;
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.dataset.kind = `custom:${type.id}`;
    chip.textContent = type.name;
    chip.addEventListener("click", () => {
      state.kind = chip.dataset.kind;
      paintKindChips();
      runSearch();
    });
    menu.appendChild(chip);
  }
}

/* ------------------------------------------------------------ dups drawer */

function setDups(open) {
  $("dups-drawer").classList.toggle("open", Boolean(open));
  $("dups-drawer").setAttribute("aria-hidden", String(!open));
  $("scrim").classList.toggle("show", Boolean(open) && !$("panel").classList.contains("open"));
  updateActionButtons();
  if (!open && state.dupStream) {
    state.dupStream.abort();
    state.dupStream = null;
    $("dups-progress").hidden = true;
  }
  if (open) loadDuplicates(state.dupMode);
}

async function loadDuplicates(mode, force) {
  // A mode switch must stop the old stream: it otherwise keeps writing the
  // shared progress bar and can render stale groups over the new mode.
  if (state.dupStream) {
    state.dupStream.abort();
    state.dupStream = null;
  }
  if (!force && state.dupCache[mode]) {
    renderDuplicates(state.dupCache[mode]);
    return;
  }
  const fill = $("dups-progress-fill");
  const label = $("dups-progress-label");
  $("dups-progress").hidden = false;
  fill.style.width = "0";
  label.textContent = t("dups.scanning", { percent: 0 });
  const controller = new AbortController();
  state.dupStream = controller;
  try {
    const response = await fetch(`/api/duplicates/stream?mode=${mode}`, { signal: controller.signal });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "progress" && event.total) {
          fill.style.width = `${Math.round((event.done / event.total) * 100)}%`;
          label.textContent = `${event.done}/${event.total}`;
        } else if (event.type === "done") {
          state.dupCache[event.mode || mode] = event.groups || [];
          renderDuplicates(event.groups || []);
          $("dups-progress").hidden = true;
        } else if (event.type === "error") {
          label.textContent = event.error;
        }
      }
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      label.textContent = error.message;
    }
  } finally {
    if (state.dupStream === controller) state.dupStream = null;
  }
}

function renderDuplicates(groups) {
  $("dups-body").innerHTML = groups.length
    ? groups.map((group) => {
      const keep = Number(group.keep) || 0;
      const oldest = Math.min(...group.items.map((item) => Number(item.mtime) || 0));
      const largest = Math.max(...group.items.map((item) => Number(item.size) || 0));
      return `<div class="dup-group" data-keep="${keep}">
          <b class="dup-count">${t("panel.copies", { n: group.items.length })}${
            group.kind === "doc" ? ` <span class="dup-kind">${esc(t("dups.documents"))}</span>` : ""
          }</b>
          ${group.items.map((item) => {
            const isKeep = item.file_id === keep;
            // Hints are advisory only: the server's `keep` still decides what
            // "Keep the best" selects, and nothing is deleted without confirm.
            const hints = [];
            if ((Number(item.mtime) || 0) === oldest) hints.push(ui("dup.older"));
            if ((Number(item.size) || 0) === largest) hints.push(ui("dup.larger"));
            if (isKeep) hints.push(ui("dup.keep"));
            return `<div class="dup-item${isKeep ? " keep" : ""}">
              <button type="button" class="dup-reveal" data-reveal="${item.file_id}"
                      title="${esc(item.path)}">
                <span class="dup-name">${esc(basename(item.path))}${
                  isKeep ? ` <span class="dup-keep">${esc(t("dups.keep"))}</span>` : ""
                }</span>
                <span class="dup-hints">${hints.map((hint) => `<span class="dup-hint">${esc(hint)}</span>`).join("")}</span>
                <span class="dup-full">${esc(item.path)}</span>
              </button>
              <input type="checkbox" class="dup-pick" value="${item.file_id}"
                     aria-label="${esc(t("dups.select", { name: basename(item.path) }))}">
            </div>`;
          }).join("")}
        </div>`;
    }).join("")
    : `<p class="panel-note">${esc(t("panel.noDuplicates"))}</p>`;
  document.querySelectorAll(".dup-mode").forEach((button) => {
    button.classList.toggle("active", button.dataset.dupMode === state.dupMode);
  });
}

function selectedDups() {
  return [...document.querySelectorAll("#dups-body .dup-pick:checked")]
    .map((box) => Number(box.value));
}

/* ------------------------------------------------------------ open + delete */

async function openFile(id, reveal) {
  try {
    await api(`/api/${reveal ? "reveal" : "open"}?id=${id}`);
    if (!reveal) {
      // The most recent query log entry gains the name of what it opened.
      const hit = state.hits.find((item) => item.file_id === id);
      if (hit) openedInHistory(hit.name);
    }
  } catch (error) {
    toast(error.message);
  }
}

function openConfirm(text, onYes) {
  $("confirm-text").textContent = text;
  $("confirm").classList.add("open");
  $("confirm-yes").onclick = async () => {
    $("confirm").classList.remove("open");
    await onYes();
  };
  $("confirm-no").onclick = () => $("confirm").classList.remove("open");
}

async function deleteIds(ids) {
  if (!ids.length) return;
  openConfirm(t("confirm.deleteTitle"), async () => {
    try {
      const result = await api("/api/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      });
      toast(t("toast.deleted", { n: result.deleted }));
      state.dupCache = {};
      for (const id of ids) state.picked.delete(id);
      updateBulkBar();
      await loadDuplicates(state.dupMode, true);
      if (state.query) runSearch({ append: false });
    } catch (error) {
      toast(t("toast.failed", { error: error.message }));
    }
  });
}

/* ------------------------------------------------------------- pin + saved */

async function togglePin(id, wanted) {
  try {
    const data = await api("/api/pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(wanted === undefined ? { id } : { id, pinned: wanted }),
    });
    const hit = state.hits.find((item) => item.file_id === id);
    if (hit) hit.pinned = data.pinned;
    document.querySelectorAll(`[data-pin="${id}"]`).forEach((star) => {
      star.classList.toggle("on", data.pinned);
      star.setAttribute("aria-pressed", String(data.pinned));
      // The hop-and-flash: a click on the star must never read as nothing.
      star.classList.remove("pop");
      void star.offsetWidth;
      star.classList.add("pop");
    });
    // The idle screen caches the pinned rows. Dropping the cache alone leaves
    // the old row on screen until a full reload, so repaint it from the server.
    if ($("personal")) {
      await loadPersonal(true);
      paintPersonal();
    } else {
      state.personal = null;
    }
    toast(data.pinned ? t("pin.added") : t("pin.removed"));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

async function saveSearch(query) {
  try {
    await api("/api/saved", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    await loadPersonal(true);
    paintPersonal();
    // The bookmark hop: save must be felt, not only toasted.
    const btn = $("save-btn");
    btn.classList.remove("pop");
    void btn.offsetWidth;
    btn.classList.add("pop");
    setTimeout(() => btn.classList.remove("pop"), 550);
    toast(t("saved.done", { query }));
  } catch (error) {
    toast(t("saved.failed", { error: error.message }));
  }
}

async function dropSaved(id) {
  try {
    await api("/api/saved/drop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: Number(id) }),
    });
    await loadPersonal(true);
    paintPersonal();
    toast(t("saved.forgotten"));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

async function addAlias(alias, expand) {
  try {
    await api("/api/alias", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias, expand }),
    });
    $("alias-word").value = "";
    $("alias-means").value = "";
    await loadPersonal(true);
    renderAliases();
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

async function dropAlias(alias) {
  try {
    await api("/api/alias/drop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias }),
    });
    await loadPersonal(true);
    renderAliases();
    toast(t("alias.removed", { alias }));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

/* ------------------------------------------------------- annotate popover */

let annotateIds = [];

function openAnnotate(mode, ids) {
  annotateIds = ids;
  const isTag = mode === "tag";
  $("annotate-title").textContent = isTag ? t("bulk.tag") : t("bulk.collection");
  const chips = isTag
    ? state.metaTags.map((tag) => `<button class="tag-chip" data-annotate-run="${esc(tag)}">#${esc(tag)}</button>`).join("")
    : state.metaCollections.map((collection) => `
      <button class="tag-chip" data-annotate-collection="${collection.id}">${esc(collection.name || collection.id)}</button>`).join("");
  $("annotate-body").innerHTML = `
    <input class="annotate-input" id="annotate-value" spellcheck="false"
           placeholder="${esc(isTag ? t("card.tag") : t("bulk.collection"))}">
    <div class="annotate-chips">${chips || `<p class="settings-note">${esc(t("annotate.empty"))}</p>`}</div>
    <p class="annotate-note">${esc(t(isTag ? "annotate.tagNote" : "annotate.collectionNote"))}</p>`;
  const input = $("annotate-value");
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submitAnnotate(mode, input.value);
  });
  const popover = $("annotate");
  popover.hidden = false;
  const rect = document.querySelector("[data-tag-add], [data-collection-add], #bulk-tag")?.getBoundingClientRect();
  if (rect) {
    popover.style.left = `${Math.min(rect.left, window.innerWidth - 320)}px`;
    popover.style.top = `${Math.min(rect.bottom + 8, window.innerHeight - 260)}px`;
  }
  input.focus();
}

async function submitAnnotate(mode, raw) {
  const clean = String(raw || "").trim().replace(/"/g, "");
  if (!clean || !annotateIds.length) return;
  try {
    if (mode === "tag") {
      await api("/api/tag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: clean, ids: annotateIds, on: true }),
      });
      for (const id of annotateIds) {
        const hit = state.hits.find((item) => item.file_id === id);
        if (hit) hit.tags = [...new Set([...(hit.tags || []), clean])].sort();
      }
      await loadMeta(true);
      toast(t("annotate.tagged", { tag: clean, n: annotateIds.length }));
    } else {
      let id = Number(clean);
      if (!Number.isFinite(id) || !state.metaCollections.some((c) => Number(c.id) === id)) {
        const created = await api("/api/collections", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "create", name: clean }),
        });
        id = Number(created.id) || 0;
      }
      if (!id) return;
      await api("/api/collections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", id, ids: annotateIds }),
      });
      await loadMeta(true);
      toast(t("annotate.added", { n: annotateIds.length }));
    }
    closeAnnotate();
    if (state.query) renderResults({ hits: state.hits }, false);
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

function closeAnnotate() {
  $("annotate").hidden = true;
  annotateIds = [];
}

/* ---------------------------------------------------------------- preview */

function paintBoxesInto(holder, data, hotWord) {
  if (!holder) return;
  if (!data.boxes?.length || !data.width || !data.height) { holder.innerHTML = ""; return; }
  holder.innerHTML = data.boxes.map((box) => {
    const left = (box.x / data.width) * 100;
    const top = (box.y / data.height) * 100;
    const width = (box.w / data.width) * 100;
    const height = (box.h / data.height) * 100;
    const hot = hotWord && String(box.text || "").toLowerCase().includes(String(hotWord).toLowerCase());
    return `<span class="ocr-box${hot ? " hot" : ""}" title="${esc(box.text || "")}"
      style="left:${left}%;top:${top}%;width:${width}%;height:${height}%"></span>`;
  }).join("");
}

async function openPreview(fileId) {
  const seq = state.previewSeq = (state.previewSeq || 0) + 1;
  state.previewId = Number(fileId) || 0;
  state.noteText = "";
  $("preview").classList.add("open");
  const body = $("preview-body");
  body.innerHTML = `<p class="settings-note">${esc(t("status.checking"))}</p>`;
  try {
    const data = await api(`/api/preview?id=${fileId}&chars=1600`);
    if (seq !== state.previewSeq) return;  // a newer preview already won
    const meta = `<p class="pv-meta"><b>${esc(data.name)}</b><br>${esc(data.path)}<br>
      ${fmtSize(data.size)} · ${fmtDate(data.mtime)} · ${esc(t("preview.opened", { n: data.opens }))}</p>`;
    const missing = data.exists ? "" : `<p class="pv-missing">${esc(t("preview.missing"))}</p>`;
    if (data.image) {
      const ocrText = String(data.image.ocr || "");
      body.innerHTML = `${meta}
        <div class="pv-image">
          <img src="${data.image.thumb}" alt="">
          <div class="ocr-boxes" data-pv-id="${fileId}"></div>
        </div>
        ${ocrText ? `<div class="pv-text ocr-text">${ocrWords(ocrText, state.query)}</div>` : ""}
        ${missing}
        <div class="pv-actions">
          <button class="btn primary" data-open="${fileId}">${esc(t("card.open"))}</button>
          <button class="btn" data-reveal="${fileId}">${esc(t("card.folder"))}</button>
          <button class="btn" data-similar="${fileId}">${esc(t("card.similar"))}</button>
          <button class="btn" data-copy-path="${esc(data.path)}">${esc(t("ctx.copy"))}</button>
        </div>
        ${notesEditor(fileId)}`;
      api(`/api/ocr-boxes?id=${fileId}&q=${encodeURIComponent(state.query || "")}`)
        .then((boxes) => {
          if (seq === state.previewSeq) paintBoxesInto(body.querySelector(".ocr-boxes"), boxes);
        })
        .catch(() => {});
    } else {
      const isCode = data.kind === "code";
      body.innerHTML = `${meta}
        <div class="pv-text${isCode ? " is-code" : ""}">${isCode
          ? numberedCode(data.text || "", state.query, 1, 1)
          : highlight(data.text || "", state.query, 1)}</div>
        ${missing}
        <div class="pv-actions">
          <button class="btn primary" data-open="${fileId}">${esc(t("card.open"))}</button>
          <button class="btn" data-reveal="${fileId}">${esc(t("card.folder"))}</button>
          <button class="btn" data-pin="${fileId}">${esc(t("card.pin"))}</button>
          <button class="btn" data-copy-path="${esc(data.path)}">${esc(t("ctx.copy"))}</button>
        </div>
        ${notesEditor(fileId)}`;
    }
    if (data.truncated) {
      body.querySelector(".pv-actions").insertAdjacentHTML("beforebegin",
        `<p class="settings-note">…</p>`);
    }
    loadNoteInto(fileId);
  } catch (error) {
    if (seq === state.previewSeq) {
      body.innerHTML = `<p class="pv-missing">${esc(t("preview.failed", { error: error.message }))}</p>`;
    }
  }
}

function closePreview() {
  $("preview").classList.remove("open");
}

/* ---------------------------------------------------------------- similar */

async function openSimilar(fileId) {
  const seq = state.similarSeq = (state.similarSeq || 0) + 1;
  try {
    // Document neighbours come from file vectors (/api/similar-files);
    // screenshots use the visual route (/api/similar). Both are read-only.
    let files = [];
    try {
      const data = await api(`/api/similar-files?id=${fileId}&limit=12`);
      files = data.files || [];
    } catch (error) {
      /* older servers only have the visual route */
    }
    if (seq !== state.similarSeq) return;
    if (files.length) {
      $("modal-grid").innerHTML = files.map((item) => `
        <div class="similar-row" data-open="${item.file_id}" title="${esc(item.path)}">
          <span class="s-name">${esc(item.name || basename(item.path))}</span>
          <span class="s-score">${Number(item.score || 0).toFixed(3)}</span>
        </div>`).join("");
    } else {
      const data = await api(`/api/similar?id=${fileId}`);
      if (seq !== state.similarSeq) return;
      $("modal-grid").innerHTML = (data.images || []).length
        ? data.images.map(shot).join("")
        : `<div class="empty" style="min-height:120px"><p>${esc(t("results.nothing"))}</p></div>`;
    }
    $("modal").classList.add("open");
  } catch (error) {
    if (seq === state.similarSeq) toast(error.message);
  }
}

/* ---------------------------------------------------------------- why box */

function openWhy(id) {
  const hit = state.hits.find((item) => item.file_id === id);
  if (!hit) return;
  $("annotate-title").textContent = `${hit.name} — ${ui("why.title")}`;
  // Every channel gets a row. Translated ones use the interface wording; the
  // rest fall back to a readable name from the local table, never a raw key.
  const extraNames = {
    bm25: "BM25", fuzzy: "fuzzy", vector: "vector", ocr: "OCR",
    semantic: "semantic", text: "text", name: "name", image: "image",
  };
  const chips = (hit.channels || []).map((channel) => {
    const translated = t(`why.${channel}`);
    const label = translated !== `why.${channel}`
      ? translated
      : (extraNames[channel] || channel);
    return `<button class="tag-chip" style="cursor:default">${esc(label)}</button>`;
  }).join("");
  const terms = String(state.query || "").split(/\s+/)
    .filter((word) => word && !word.includes(":")).slice(0, 8);
  $("annotate-body").innerHTML = `
    <div class="why-head">${esc(ui("why.channels"))}</div>
    <div class="annotate-chips">${chips || `<p class="settings-note">${esc(t("annotate.empty"))}</p>`}</div>
    <div class="why-meta">
      <span>${esc(ui("why.score"))}: ${Number(hit.score || 0).toFixed(4)}</span>
      ${terms.length ? `<span>${esc(ui("why.terms"))}: ${terms.map((word) => esc(word)).join(", ")}</span>` : ""}
    </div>`;
  const popover = $("annotate");
  popover.hidden = false;
  const rect = document.querySelector(`[data-why="${id}"]`)?.getBoundingClientRect();
  if (rect) {
    popover.style.left = `${Math.min(rect.left, window.innerWidth - 320)}px`;
    popover.style.top = `${Math.min(rect.bottom + 8, window.innerHeight - 200)}px`;
  }
}

/* -------------------------------------------------------------- filter UI */

const SIZE_UNITS = { b: 1, kb: 1024, mb: 1048576, gb: 1073741824 };

function builtFilterTokens() {
  const tokens = [];
  const type = $("f-type")?.value || "";
  if (type && !type.startsWith("custom:")) tokens.push(`type:${type}`);
  const folder = $("f-folder")?.value.trim();
  if (folder) tokens.push(`in:"${folder.replace(/"/g, "")}"`);
  const min = Number($("f-size-min")?.value);
  const max = Number($("f-size-max")?.value);
  const minUnit = $("f-size-min-unit")?.value || "mb";
  const maxUnit = $("f-size-max-unit")?.value || "mb";
  if (min > 0) tokens.push(`size:>=${min}${minUnit}`);
  if (max > 0) tokens.push(`size:<${max}${maxUnit}`);
  const period = $("f-period")?.value;
  if (period) tokens.push(`modified:${period}`);
  const tag = $("f-tag")?.value.trim();
  if (tag) tokens.push(`tag:"${tag.replace(/"/g, "")}"`);
  const collection = $("f-collection")?.value;
  if (collection) tokens.push(`collection:"${collection.replace(/"/g, "")}"`);
  return tokens;
}

function paintFilterPreview() {
  const preview = $("filter-preview");
  if (preview) preview.textContent = builtFilterTokens().join(" ");
}

// The picker menus are empty spans in the markup: build their options from
// the hidden selects once, otherwise type/period/collection cannot be chosen.
function populateFilterMenus() {
  const builder = $("filter-builder");
  if (!builder) return;
  builder.querySelectorAll("[data-picker]").forEach((field) => {
    const select = field.querySelector("select");
    const menu = field.querySelector(".filter-menu");
    if (!select || !menu || menu.children.length) return;
    menu.innerHTML = [...select.options].map((option) =>
      `<button type="button" role="option" data-value="${esc(option.value)}">${esc(option.textContent)}</button>`
    ).join("");
  });
}

function applyFilters() {
  const tokens = builtFilterTokens().join(" ");
  const input = $("q");
  // Strip any tokens the builder wrote earlier, then append the fresh ones:
  // the builder owns its own words in the query and nothing else's.
  input.value = `${input.value.replace(/(?:type:\S+|in:"[^"]*"|size:>=?\S+|size:<\S+|modified:\S+|tag:"[^"]*"|collection:"[^"]*")/g, " ").replace(/\s+/g, " ").trim()} ${tokens}`.trim();
  openFilters(false);
  runSearch();
}

function openFilters(open) {
  const box = $("filter-builder");
  if (!box) return;
  box.hidden = !open;
  $("filters-btn").setAttribute("aria-expanded", String(open));
  if (open) paintFilterPreview();
}

/* --------------------------------------------------------------- calendar */

const CAL = { month: new Date().getMonth(), year: new Date().getFullYear() };

function isoDate(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function paintSince() {
  const label = $("since-label");
  if (label) label.textContent = state.since || t("tools.any");
}

function paintCalendar() {
  // i18n keeps both lists as comma-separated strings, not arrays.
  const months = String(t("cal.months")).split(",");
  const weekdays = String(t("cal.weekdays")).split(",");
  $("cal-title").textContent = t("cal.monthYear", {
    month: months[CAL.month], year: CAL.year,
  });
  $("cal-week").innerHTML = weekdays
    .map((name) => `<span>${esc(name)}</span>`).join("");
  // Monday first, which is what the rest of the interface assumes.
  const start = (new Date(CAL.year, CAL.month, 1).getDay() + 6) % 7;
  const days = new Date(CAL.year, CAL.month + 1, 0).getDate();
  const today = isoDate(new Date());
  const cells = [];
  for (let blank = 0; blank < start; blank += 1) cells.push("<span></span>");
  for (let day = 1; day <= days; day += 1) {
    const value = isoDate(new Date(CAL.year, CAL.month, day));
    const classes = ["cal-day"];
    if (value === state.since) classes.push("sel");
    if (value === today) classes.push("today");
    cells.push(`<button type="button" class="${classes.join(" ")}" data-day="${value}">${day}</button>`);
  }
  $("cal-grid").innerHTML = cells.join("");
}

function openCalendar(open) {
  const box = $("calendar");
  box.hidden = !open;
  $("since-btn").setAttribute("aria-expanded", String(open));
  if (open) paintCalendar();
}

function setSince(value) {
  state.since = value;
  paintSince();
  openCalendar(false);
  $("since-btn").focus();
  if (state.query) runSearch();
}

/* ---------------------------------------------------------------- suggest */

let suggestIndex = -1;

function suggestItems(text) {
  // Token completions win over saved queries: a half-typed `ext:` is an
  // unfinished filter, not the beginning of a remembered search.
  const tokens = tokenCompletions(text);
  if (tokens.length) return tokens.slice(0, 8);
  const needle = text.trim().toLowerCase();
  const saved = ((state.personal && state.personal.saved) || [])
    .map((item) => ({ value: item.query, kind: t("suggest.saved") }));
  const recent = recentQueries()
    .map((value) => ({ value, kind: t("suggest.recent") }));
  const pool = [...saved, ...recent];
  const seen = new Set();
  return pool.filter((item) => {
    if (!needle || !item.value.toLowerCase().includes(needle)) return false;
    if (seen.has(item.value)) return false;
    seen.add(item.value);
    return true;
  }).slice(0, 7);
}

/* Applying a suggestion replaces only the word under the caret for tokens;
   saved and recent queries still replace the whole input. */
function applySuggestion(item) {
  if (!item) return;
  const input = $("q");
  if (item.start != null && item.end != null) {
    const value = input.value;
    input.value = value.slice(0, item.start) + item.value + value.slice(item.end);
  } else {
    input.value = item.value;
  }
  closeSuggest();
  runSearch();
  input.focus();
}

function paintSuggest() {
  const box = $("suggest");
  const items = suggestItems($("q").value);
  box._items = items;
  if (!items.length) { box.hidden = true; suggestIndex = -1; return; }
  box.innerHTML = items.map((item, index) => `
    <button type="button" class="suggestion${index === suggestIndex ? " sel" : ""}" data-suggest="${index}">
      <span>${esc(item.value)}</span><span class="s-kind">${esc(item.kind)}</span>
    </button>`).join("");
  box.hidden = false;
}

function moveSuggest(delta) {
  const items = suggestItems($("q").value);
  if (!items.length) return;
  suggestIndex = (suggestIndex + delta + items.length) % items.length;
  paintSuggest();
}

function closeSuggest() {
  $("suggest").hidden = true;
  suggestIndex = -1;
}

/* ----------------------------------------------------------------- export */

function openExport(open) {
  const menu = $("export-menu");
  menu.hidden = !open;
  $("export-btn").setAttribute("aria-expanded", String(open));
}

function exportCurrent(format) {
  if (!state.query) { toast(t("export.empty")); return; }
  const url = `/api/export?${new URLSearchParams({
    format, ...Object.fromEntries(searchParams(0).entries()), limit: "5000",
  })}`;
  downloadUrl(url, `autofind-export.${format}`);
  toast(t("export.started"));
}

function exportSelectionCsv() {
  const ids = [...state.picked];
  const rows = state.hits.filter((hit) => ids.includes(hit.file_id));
  if (!rows.length) { toast(t("bulk.nothing")); return; }
  const header = ["name", "path", "kind", "size", "modified", "score", "tags"];
  // A file name is user-typed data; a cell that opens with = + - @ would
  // run as a formula the moment Excel opens the export.
  const safe = (cell) => {
    let text = String(cell ?? "").replace(/"/g, '""');
    if (/^[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text}"`;
  };
  const body = [header, ...rows.map((hit) => [
    hit.name, hit.path, hit.kind, hit.size,
    new Date(hit.mtime * 1000).toISOString(),
    Number(hit.score || 0).toFixed(6),
    (hit.tags || []).join("; "),
  ])].map((row) => row.map(safe).join(",")).join("\r\n");
  const blob = new Blob(["\uFEFF", body], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  downloadUrl(url, "autofind-selection.csv");
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/* --------------------------------------------------------------- settings */

function setSettings(open) {
  $("settings").classList.toggle("open", Boolean(open));
  if (open) {
    loadAbout();
    renderAuditBreakdown();
    renderTelemetryBadge();
    renderPortableBadge();
    loadExclusions();
  }
}

async function loadAbout() {
  try {
    const { stats, status } = (await loadStats()) || {};
    const rows = [
      ["app.title", "AutoFind"],
      ["settings.about", status ? `${status.text_embedder || ""} · ${status.visual_embedder || ""}` : ""],
    ];
    const audit = await api("/api/audit");
    const dl = $("about");
    const label = (key) => t(key);
    dl.innerHTML = `
      <dt>${esc(label("audit.path"))}</dt><dd>${esc(audit.db)}</dd>
      <dt>${esc(label("audit.size"))}</dt><dd>${fmtSize(audit.total_bytes)}</dd>
      <dt>${esc(label("audit.files"))}</dt><dd>${audit.files}</dd>
      <dt>${esc(label("audit.chunks"))}</dt><dd>${audit.chunks}</dd>
      <dt>${esc(label("audit.images"))}</dt><dd>${audit.images}</dd>
      <dt>${esc(label("audit.vectors"))}</dt><dd>${audit.vectors}</dd>
      <dt>${esc(label("audit.pins"))}</dt><dd>${audit.personal.pins}</dd>
      <dt>${esc(label("audit.saved"))}</dt><dd>${audit.personal.saved}</dd>
      <dt>${esc(label("audit.aliases"))}</dt><dd>${audit.personal.aliases}</dd>
      <dt>${esc(label("audit.tags"))}</dt><dd>${audit.personal.tags}</dd>
      <dt>${esc(label("audit.collections"))}</dt><dd>${audit.personal.collections}</dd>
      <dt>${esc(label("audit.privacy"))}</dt><dd>${audit.sensitive.enabled
        ? t("audit.privacyOn", { n: audit.sensitive.count })
        : t("panel.off")}</dd>
      <dt>${esc(label("audit.encrypted"))}</dt><dd>${audit.encrypted ? t("audit.yes") : t("audit.no")}</dd>`;
  } catch (error) {
    /* the about box stays with whatever it had */
  }
}

function paintLang() {
  document.querySelectorAll("[data-lang]").forEach((button) => {
    button.classList.toggle("on", button.dataset.lang === LANG);
  });
}

/* ----------------------------------------------------------- window chrome */

function wireWindowControls() {
  const markApp = () => document.body.classList.add("in-app");
  window.addEventListener("pywebviewready", markApp);
  document.addEventListener("pywebviewready", markApp);
  if (window.pywebview && window.pywebview.api) markApp();

  const wireDataWin = (box) => {
    if (!box) return;
    box.addEventListener("click", (event) => {
      const button = event.target.closest("[data-win]");
      if (!button) return;
      const bridge = window.pywebview && window.pywebview.api;
      if (!bridge) return;
      event.preventDefault();
      if (button.dataset.win === "quit") bridge.quit();
      else bridge.hide();
    });
  };
  const topButton = $("ontop-btn");
  if (topButton) {
    topButton.addEventListener("click", () => {
      const bridge = window.pywebview && window.pywebview.api;
      if (bridge && bridge.always_on_top) {
        bridge.always_on_top();
        topButton.classList.toggle("on");
      }
    });
  }
  wireDataWin($("win-controls"));
  wireDataWin($("onboard-controls"));
}

function greetingBridge(name) {
  const bridge = window.pywebview && window.pywebview.api;
  return bridge && typeof bridge[name] === "function" ? bridge[name] : null;
}

/* ------------------------------------------------------------ boot screen */

const BOOT_MIN_MS = 900;
const BOOT_POLL_MS = 120;
const BOOT_MAX_POLLS = 70;

const BOOT_GREETINGS = [
  { hello: "Hello", name: "English" },
  { hello: "\u041f\u0440\u0438\u0432\u0435\u0442", name: "\u0420\u0443\u0441\u0441\u043a\u0438\u0439" },
  { hello: "Hola", name: "Español" },
  { hello: "Bonjour", name: "Français" },
  { hello: "Hallo", name: "Deutsch" },
  { hello: "こんにちは", name: "日本語" },
  { hello: "你好", name: "中文" },
  { hello: "Ciao", name: "Italiano" },
  { hello: "Olá", name: "Português" },
  { hello: "Merhaba", name: "Türkçe" },
];

function skipBoot() {
  const box = $("boot");
  if (!box || box.classList.contains("done")) return;
  box.classList.add("done");
  setTimeout(() => box.remove(), 620);
}

/* /api/ocr answers with {tag, name, hello} objects; the static table above
   only covers the first frame, so every entry is normalized to one shape. */
function greetingPool(languages) {
  return (languages || []).map((item) => {
    if (item && typeof item === "object") {
      const name = String(item.name || item.tag || "?");
      return { hello: String(item.hello || name), name };
    }
    return { hello: String(item), name: String(item) };
  });
}

async function boot() {
  const box = $("boot");
  if (!box) return;
  const hello = $("boot-hello");
  const lang = $("boot-lang");
  const bar = $("boot-bar");
  const stage = $("boot-stage");
  const started = performance.now();
  let pool = BOOT_GREETINGS;
  let index = Math.floor(Math.random() * pool.length);

  const show = () => {
    const item = pool[index % pool.length];
    index += 1;
    hello.classList.add("fade");
    lang.classList.add("fade");
    setTimeout(() => {
      hello.textContent = item.hello;
      lang.textContent = item.name;
      hello.classList.remove("fade");
      lang.classList.remove("fade");
    }, 110);
  };
  show();
  const timer = setInterval(show, 700);

  // The server knows exactly which languages this machine can read.
  api("/api/ocr")
    .then((data) => {
      const list = (data.coverage && data.coverage.languages) || [];
      if (!list.length) return;
      pool = greetingPool(list);
      index = Math.floor(Math.random() * pool.length);
    })
    .catch(() => {});

  const onboard = await fetchOnboarding();
  if (onboard && onboard.first_run) {
    const enough = 3 * 700 + 250;
    const wait = enough - (performance.now() - started);
    if (wait > 0) await sleep(wait);
    clearInterval(timer);
    openOnboarding(onboard, { hello: hello.textContent, name: lang.textContent });
    box.classList.add("done");
    setTimeout(() => box.remove(), 460);
    return;
  }

  for (let attempt = 0; attempt < BOOT_MAX_POLLS; attempt += 1) {
    try {
      const ready = await api("/api/ready");
      if (ready.ready) {
        bar.style.width = "100%";
        stage.textContent = t("boot.ready", {
          ms: Math.round(ready.warm_ms ?? ready.uptime_ms),
        });
        break;
      }
      const warm = ready.stage === "vectors";
      bar.style.width = warm ? "74%" : "26%";
      stage.textContent = warm ? t("boot.warm") : t("boot.index");
    } catch (error) {
      stage.textContent = t("boot.wait");
    }
    await sleep(BOOT_POLL_MS);
  }

  const spent = performance.now() - started;
  if (spent < BOOT_MIN_MS) await sleep(BOOT_MIN_MS - spent);
  clearInterval(timer);
  box.classList.add("done");
  setTimeout(() => box.remove(), 620);
  $("q")?.focus();
}

/* -------------------------------------------------------------- first run */

const ONBOARD = {
  active: false,
  data: null,
  timer: null,
  index: 0,
  pool: BOOT_GREETINGS,
  scan: null,
  imported: null,
  importBusy: false,
  importError: null,
  autostartError: null,
  job: null,
};

async function fetchOnboarding() {
  try {
    // The welcome must never hold the window hostage: if the server is slow to
    // answer, the ready loop below takes over from here.
    return await Promise.race([
      api("/api/onboarding"),
      sleep(5000).then(() => null),
    ]);
  } catch (error) {
    return null;
  }
}

function openOnboarding(data, greeting) {
  ONBOARD.active = true;
  ONBOARD.data = data;
  const box = $("onboard");
  box.hidden = false;
  box.classList.remove("done");
  box.classList.add("pywebview-drag-region");
  const hello = $("onboard-hello");
  const helloLang = $("onboard-hello-lang");
  if (greeting) { hello.textContent = greeting.hello; helloLang.textContent = greeting.name; }
  const rotate = () => {
    const item = ONBOARD.pool[ONBOARD.index % ONBOARD.pool.length];
    ONBOARD.index += 1;
    hello.classList.add("fade");
    setTimeout(() => {
      hello.textContent = item.hello;
      helloLang.textContent = item.name;
      hello.classList.remove("fade");
    }, 110);
  };
  clearInterval(ONBOARD.timer);
  ONBOARD.timer = setInterval(rotate, 900);
  paintOnboarding();
  api("/api/ocr").then((ocr) => {
    const list = (ocr.coverage && ocr.coverage.languages) || [];
    if (list.length) {
      ONBOARD.pool = greetingPool(list);
    }
  }).catch(() => {});
}

function paintOnboarding() {
  const data = ONBOARD.data;
  if (!data) return;
  document.querySelectorAll("#onboard-lang [data-lang]").forEach((button) => {
    button.classList.toggle("on", button.dataset.lang === LANG);
  });
  const toggle = $("ob-autostart");
  const auto = data.autostart || {};
  if (toggle) {
    toggle.checked = Boolean(auto.enabled);
    toggle.disabled = !auto.supported;
  }
  const autoState = $("ob-autostart-state");
  if (autoState) {
    if (ONBOARD.autostartError) {
      autoState.textContent = ONBOARD.autostartError;
      autoState.className = "ob-state bad";
    } else {
      autoState.textContent = auto.enabled ? t("panel.on") : "";
      autoState.className = "ob-state";
    }
  }
  paintImportState();
  const roots = $("ob-roots");
  if (roots) {
    roots.innerHTML = (data.roots || []).map((root) => `<li>${esc(root)}</li>`).join("");
  }
  paintIndexState();
}

function paintImportState() {
  const badge = $("ob-import-badge");
  const state2 = $("ob-import-state");
  if (!state2) return;
  if (ONBOARD.importBusy) {
    state2.textContent = t("dups.scanning", { percent: 0 });
    state2.className = "ob-state";
  } else if (ONBOARD.importError) {
    state2.textContent = ONBOARD.importError;
    state2.className = "ob-state bad";
  } else if (ONBOARD.imported) {
    const found = ONBOARD.imported.found;
    badge.hidden = !found;
    if (found) badge.textContent = `+${ONBOARD.imported.pins || 0} / ${ONBOARD.imported.saved || 0} / ${ONBOARD.imported.aliases || 0}`;
    state2.textContent = found
      ? t("onboard.importDone", {
        pins: ONBOARD.imported.pins || 0,
        saved: ONBOARD.imported.saved || 0,
        aliases: ONBOARD.imported.aliases || 0,
      })
      : t("onboard.importMissing", { n: ONBOARD.imported.pins || 0 });
    state2.className = "ob-state good";
  } else if (ONBOARD.scan) {
    badge.hidden = !ONBOARD.scan.found;
    if (ONBOARD.scan.found) {
      badge.textContent = `+${ONBOARD.scan.pins} / ${ONBOARD.scan.saved} / ${ONBOARD.scan.aliases}`;
    }
    state2.textContent = ONBOARD.scan.found
      ? t("onboard.importFound", {
        pins: ONBOARD.scan.pins, saved: ONBOARD.scan.saved, aliases: ONBOARD.scan.aliases,
      })
      : t("onboard.importMissing", { n: ONBOARD.scan.pins || 0 });
    state2.className = "ob-state";
  } else {
    state2.textContent = "";
  }
}

async function scanKensaku() {
  if (ONBOARD.importBusy || ONBOARD.imported) return;
  ONBOARD.importBusy = true;
  ONBOARD.importError = null;
  paintImportState();
  try {
    ONBOARD.scan = await api("/api/import/kensaku");
  } catch (error) {
    ONBOARD.importError = error.message;
  } finally {
    ONBOARD.importBusy = false;
    paintImportState();
  }
}

async function importKensaku() {
  if (ONBOARD.importBusy) return;
  ONBOARD.importBusy = true;
  ONBOARD.importError = null;
  paintImportState();
  try {
    ONBOARD.imported = await api("/api/import/kensaku", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!ONBOARD.imported.found) ONBOARD.scan = { found: false, pins: 0, saved: 0, aliases: 0 };
    loadStats();
    if (state.query) runSearch(); else renderIdle();
  } catch (error) {
    ONBOARD.importError = error.message;
  } finally {
    ONBOARD.importBusy = false;
    paintImportState();
  }
}

async function toggleAutostart() {
  const toggle = $("ob-autostart");
  if (!toggle || !ONBOARD.data) return;
  const wanted = toggle.checked;
  toggle.disabled = true;
  ONBOARD.autostartError = null;
  try {
    ONBOARD.data.autostart = await api("/api/autostart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: wanted }),
    });
  } catch (error) {
    toggle.checked = !wanted;
    ONBOARD.autostartError = error.message;
  } finally {
    toggle.disabled = !ONBOARD.data.autostart.supported;
    paintOnboarding();
  }
}

function paintIndexState() {
  const state2 = $("ob-index-state");
  const bar = $("ob-bar");
  if (!state2 || !bar) return;
  const job = ONBOARD.job;
  let percent = 0;
  state2.className = "ob-state";
  if (job && job.running) {
    percent = job.total ? Math.round((job.position / job.total) * 100) : 4;
    state2.textContent = `${job.position}/${job.total} · ${shortPath(job.path || "")}`;
  } else if (job && job.errorKind === "empty") {
    state2.textContent = t("onboard.indexEmpty");
    state2.classList.add("bad");
  } else if (job && job.error) {
    state2.textContent = t("onboard.indexFailed", { error: job.error });
    state2.classList.add("bad");
  } else if (job && job.result) {
    percent = 100;
    state2.textContent = t("onboard.indexDone", {
      files: plural(job.result.indexed || 0, "unit.files"),
      seconds: job.result.seconds || 0,
    });
    state2.classList.add("good");
  } else {
    state2.textContent = "";
  }
  if (bar) bar.style.width = `${percent}%`;
}

async function startOnboardingIndex() {
  const roots = (ONBOARD.data && ONBOARD.data.roots) || [];
  if (!roots.length) {
    ONBOARD.job = { running: false, errorKind: "empty" };
    paintIndexState();
    return;
  }
  const button = $("ob-index");
  if (button) button.disabled = true;
  ONBOARD.job = { running: true, position: 0, total: 0 };
  paintIndexState();
  try {
    await api("/api/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roots, prune: false }),
    });
    pollOnboardingJob();
  } catch (error) {
    ONBOARD.job = { running: false, error: error.message };
    paintIndexState();
    if (button) button.disabled = false;
  }
}

function pollOnboardingJob() {
  api("/api/progress").then((job) => {
    ONBOARD.job = job;
    paintIndexState();
    if (job.running) {
      setTimeout(pollOnboardingJob, 450);
    } else {
      const button = $("ob-index");
      if (button) button.disabled = false;
      loadStats();
    }
  }).catch(() => {
    const button = $("ob-index");
    if (button) button.disabled = false;
  });
}

async function finishOnboarding() {
  if (!ONBOARD.active) return;
  ONBOARD.active = false;
  clearInterval(ONBOARD.timer);
  api("/api/onboarding/done", { method: "POST", body: "{}" }).catch(() => {});
  const grow = greetingBridge("onboarding_finish");
  if (grow) Promise.resolve(grow()).catch(() => {});

  const box = $("onboard");
  if (box) {
    box.classList.add("done");
    setTimeout(() => box.remove(), 300);
  }
  setTimeout(() => {
    $("q")?.focus();
    loadStats();
    if (!state.query) renderIdle();
  }, 320);
  toast(t("onboard.ready"));
}

async function replayOnboarding() {
  setSettings(false);
  try {
    await api("/api/onboarding/reset", { method: "POST", body: "{}" });
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
    return;
  }
  const reopen = greetingBridge("onboarding_reopen");
  if (reopen) {
    try { await reopen(); } catch (error) { /* reload still replays it */ }
  }
  location.reload();
}

/* -------------------------------------------------------------- selection */

function togglePick(id, checked) {
  if (checked) state.picked.add(Number(id));
  else state.picked.delete(Number(id));
  document.querySelectorAll(`.gcard[data-file="${id}"]`).forEach((node) => {
    node.classList.toggle("picked", state.picked.has(Number(id)));
  });
  updateBulkBar();
}

function copyText(text) {
  navigator.clipboard?.writeText(text).then(
    () => toast(t("copied")),
    () => toast(t("toast.failed", { error: "clipboard" })),
  );
}

/* ---------------------------------------------------------- event wiring */

function wire() {
  // The Rust shell saves Ctrl+Alt+S screenshots and dispatches this event.
  document.addEventListener("autofind:screenshot", async (event) => {
    const info = event.detail || window.__autofindLastScreenshot || {};
    const path = typeof info === "string" ? info : info.path;
    if (!path) return;
    toast(t("screenshot.saved", { name: String(path).split(/[\\/]/).pop() }));
    if (typeof info === "object" && info.indexed) return;
    try {
      await api("/api/screenshot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      toast(t("screenshot.indexed"));
    } catch (error) {
      /* the file is still saved and visible in the screenshots folder */
    }
  });

  // Ctrl+Shift+1..9 — saved searches, in the order shown in the drawer.
  document.addEventListener("keydown", async (event) => {
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "c") {
      const hit = selectedHit && selectedHit();
      if (hit && navigator.clipboard) {
        event.preventDefault();
        try { await navigator.clipboard.writeText(hit.path); } catch (error) {}
      }
      return;
    }
    if (!(event.ctrlKey && event.shiftKey && /^[1-9]$/.test(event.key))) return;
    event.preventDefault();
    try {
      const data = await api("/api/saved");
      const item = (data.saved || [])[Number(event.key) - 1];
      if (!item) return;
      $("q").value = item.query;
      runSearch();
    } catch (error) {
      /* a missing saved search is not an error worth a toast */
    }
  });

  // search box
  const q = $("q");
  q.addEventListener("input", () => { paintSuggest(); scheduleSearch(); });
  q.addEventListener("focus", () => { if (q.value) paintSuggest(); });
  q.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") { event.preventDefault(); moveSuggest(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); moveSuggest(-1); }
    else if (event.key === "Enter") {
      const items = suggestItems(q.value);
      if (suggestIndex >= 0 && items[suggestIndex]) {
        event.preventDefault();
        applySuggestion(items[suggestIndex]);
        return;
      }
      closeSuggest();
      runSearch();
    } else if (event.key === "Escape") {
      closeSuggest();
    }
  });
  q.addEventListener("blur", () => setTimeout(closeSuggest, 160));
  $("suggest").addEventListener("mousedown", (event) => {
    const button = event.target.closest("[data-suggest]");
    if (!button) return;
    event.preventDefault();
    const items = $("suggest")._items || [];
    applySuggestion(items[Number(button.dataset.suggest)]);
  });
  $("clear").addEventListener("click", () => {
    q.value = "";
    state.kind = "";
    state.folder = "";
    state.since = "";
    paintSince();
    paintKindChips();
    runSearch();
    q.focus();
  });
  $("save-btn").addEventListener("click", () => saveSearch(q.value.trim()));

  // kind chips
  document.querySelectorAll(".chip[data-kind]").forEach((chip) => {
    chip.addEventListener("click", () => {
      state.kind = chip.dataset.kind;
      paintKindChips();
      runSearch();
    });
  });

  // tools
  $("folder").addEventListener("input", () => {
    state.folder = $("folder").value.trim();
    scheduleSearch();
  });
  $("since-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    openCalendar($("calendar").hidden);
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".date-field")) openCalendar(false);
    if (!event.target.closest(".sort-field") && !$("sort-menu").hidden) openSort(false);
    if (!event.target.closest("#filters-btn") && !event.target.closest("#filter-builder")) openFilters(false);
    if (!event.target.closest(".menu-anchor") && !$("export-menu").hidden) openExport(false);
    // The click that OPENS the popover also reaches document: without the
    // opener guard it closes the popover in the same event, so "+ \u0442\u0435\u0433" and
    // "+ \u043a\u043e\u043b\u043b\u0435\u043a\u0446\u0438\u044f" looked dead.
    // [data-tag-new] fires from the per-card tag dropdown, outside #annotate.
    // Without the exemption closeAnnotate() clears annotateIds during the
    // submitAnnotate() await and the success toast reports zero files.
    if (!event.target.closest("#annotate, [data-tag-add], [data-tag-new], [data-collection-add], #bulk-tag")
        && !$("annotate").hidden) closeAnnotate();
  });
  $("calendar").addEventListener("click", (event) => {
    const day = event.target.closest("[data-day]");
    if (day) { setSince(day.dataset.day); return; }
    const action = event.target.closest("[data-cal]");
    if (!action) return;
    const what = action.dataset.cal;
    if (what === "prev") { CAL.month -= 1; if (CAL.month < 0) { CAL.month = 11; CAL.year -= 1; } paintCalendar(); }
    else if (what === "next") { CAL.month += 1; if (CAL.month > 11) { CAL.month = 0; CAL.year += 1; } paintCalendar(); }
    else if (what === "clear") setSince("");
    else if (what === "today") setSince(isoDate(new Date()));
    else if (what === "docdate") {
      state.docdate = !state.docdate;
      action.classList.toggle("on", state.docdate);
      if (state.query) runSearch();
    }
  });
  $("sort-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    // hidden=true means the menu is closed, so closed → open. The inverted
    // form here is why every click read as "close" and the menu never opened.
    openSort($("sort-menu").hidden);
  });
  $("sort-menu").addEventListener("click", (event) => {
    const option = event.target.closest("[data-sort]");
    if (!option) return;
    state.sort = option.dataset.sort;
    $("sort-value").textContent = t(`sort.${state.sort}`);
    openSort(false);
    runSearch();
  });
  $("filters-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    openFilters($("filter-builder").hidden);
  });
  populateFilterMenus();
  $("filter-builder").addEventListener("input", paintFilterPreview);
  $("filter-builder").addEventListener("click", (event) => {
    const option = event.target.closest(".filter-menu button, .unit-menu button");
    if (!option) return;
    const menu = option.parentElement;
    const field = menu.closest("[data-picker], .unit-picker");
    menu.hidden = true;
    if (option.dataset.unit) {
      const holder = menu.parentElement;
      holder.querySelector("input[type=hidden]").value = option.dataset.unit;
      holder.querySelector(".unit-value").textContent = option.textContent;
    } else if (field) {
      const select = field.querySelector("select");
      select.value = option.dataset.value ?? "";
      field.querySelector(".filter-picker-value").textContent = option.textContent;
    }
    paintFilterPreview();
  });
  $("filter-builder").addEventListener("click", (event) => {
    const trigger = event.target.closest(".filter-picker-btn, .unit-trigger");
    if (trigger) {
      const menu = trigger.parentElement.querySelector(".filter-menu, .unit-menu");
      if (menu) menu.hidden = !menu.hidden;
    }
  });
  $("filter-apply").addEventListener("click", applyFilters);
  $("filter-clear").addEventListener("click", () => {
    $("filter-builder").querySelectorAll("input").forEach((input) => {
      if (input.type === "hidden") input.value = "mb";
      else input.value = "";
    });
    $("filter-builder").querySelectorAll("select").forEach((select) => { select.value = ""; });
    $("filter-builder").querySelectorAll(".filter-picker-value").forEach((node) => {
      node.textContent = t("filters.any");
    });
    $("filter-builder").querySelectorAll(".unit-value").forEach((node) => {
      node.textContent = t("unit.mb");
    });
    paintFilterPreview();
  });
  $("export-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    openExport($("export-menu").hidden);
  });
  $("export-menu").addEventListener("click", (event) => {
    const option = event.target.closest("[data-export]");
    if (!option) return;
    openExport(false);
    exportCurrent(option.dataset.export);
  });

  // header actions
  $("panel-btn").addEventListener("click", () => setPanel(!$("panel").classList.contains("open")));
  $("dups-btn").addEventListener("click", () => setDups(!$("dups-drawer").classList.contains("open")));
  $("panel-close").addEventListener("click", () => setPanel(false));
  $("dups-close").addEventListener("click", () => setDups(false));
  $("scrim").addEventListener("click", () => { setPanel(false); setDups(false); });

  // drawer tabs
  $("drawer-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-drawer-tab]");
    if (tab) setDrawerTab(tab.dataset.drawerTab);
  });

  // index pane
  $("index-btn").addEventListener("click", indexNow);
  $("watch-btn").addEventListener("click", toggleWatch);
  $("dups-open").addEventListener("click", () => setDups(true));

  // privacy pane
  $("privacy-enabled").addEventListener("change", () => {
    savePrivacy({ enabled: $("privacy-enabled").checked });
  });
  $("privacy-toggle").addEventListener("click", () => {
    const box = $("privacy-filters");
    const open = !box.classList.contains("open");
    box.classList.toggle("open", open);
    $("privacy-filters").setAttribute("aria-hidden", String(!open));
    $("privacy-toggle").setAttribute("aria-expanded", String(open));
  });
  $("privacy-filters-list").addEventListener("change", (event) => {
    const box = event.target.closest("[data-privacy]");
    if (!box) return;
    const checked = [...document.querySelectorAll("[data-privacy]:checked")]
      .map((node) => node.dataset.privacy);
    savePrivacy({ categories: checked });
  });
  $("purge-btn").addEventListener("click", async () => {
    try {
      const purged = await api("/api/privacy/purge", { method: "POST", body: "{}" });
      toast(t("toast.purged", { n: purged?.count ?? 0 }));
      refreshPrivacy();
    } catch (error) {
      toast(t("toast.failed", { error: error.message }));
    }
  });

  // seal pane
  $("seal-btn").addEventListener("click", async () => {
    const button = $("seal-btn");
    if (button.disabled) return;
    button.disabled = true;
    const action = state.seal && state.seal.sealed ? "off" : "on";
    try {
      await api("/api/seal", { method: "POST", body: JSON.stringify({ action }) });
    } catch (error) {
      button.disabled = false;
      toast(t("toast.failed", { error: error.message }));
      return;
    }
    pollSeal();
  });

  // search pane (aliases + types)
  $("alias-add").addEventListener("click", () => {
    const alias = $("alias-word").value.trim();
    const expand = $("alias-means").value.trim();
    if (!alias || !expand) { toast(t("alias.need")); return; }
    addAlias(alias, expand);
  });
  $("alias-list").addEventListener("click", (event) => {
    const drop = event.target.closest("[data-alias-drop]");
    if (drop) dropAlias(drop.dataset.aliasDrop);
  });
  $("types-add").addEventListener("click", () => {
    state.types.push({ name: "", masks: [""] });
    renderTypes();
  });
  $("types-list").addEventListener("click", (event) => {
    const remove = event.target.closest("[data-type-remove]");
    if (!remove) return;
    const cardNode = remove.closest(".type-card");
    const index = [...document.querySelectorAll("#types-list .type-card")].indexOf(cardNode);
    if (index >= 0) {
      state.types.splice(index, 1);
      renderTypes();
    }
  });
  $("types-list").addEventListener("change", () => {/* edits commit on blur */});
  $("types-list").addEventListener("focusout", saveTypes);

  // keys pane
  $("hotkeys-list").addEventListener("keydown", (event) => {
    const input = event.target.closest("[data-hotkey]");
    if (!input) return;
    if (event.key === "Backspace") {
      // Documented behaviour: Backspace clears the hotkey, not types a word.
      event.preventDefault();
      input.value = "";
      input.dataset.dirty = "1";
      return;
    }
    const chord = chordOfEvent(event);
    if (chord && !MODIFIER_KEYS.has(chord)) {
      event.preventDefault();
      input.value = chordLabel(chord);
      input.dataset.dirty = "1";
    }
  });
  $("hotkeys-list").addEventListener("focusout", () => {
    const dirty = [...document.querySelectorAll("[data-hotkey]")]
      .some((input) => input.dataset.dirty === "1" || (input.value && !input.dataset.clean));
    if (dirty) saveHotkeys();
  });
  $("hotkeys-reset").addEventListener("click", async () => {
    try {
      const data = await api("/api/hotkeys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hotkeys: FALLBACK_HOTKEYS }),
      });
      state.hotkeys = data.hotkeys || FALLBACK_HOTKEYS;
      state.hotkeyConflicts = data.conflicts || [];
      renderHotkeys();
    } catch (error) {
      toast(t("toast.failed", { error: error.message }));
    }
  });

  // appearance pane
  $("wall-row").addEventListener("click", (event) => {
    const tile = event.target.closest("[data-wall]");
    if (tile) chooseWall(tile.dataset.wall);
  });
  $("wall-pick").addEventListener("click", () => $("wall-file").click());
  $("wall-file").addEventListener("change", () => {
    useWallFile($("wall-file").files[0]);
    $("wall-file").value = "";
  });
  $("wall-edit").addEventListener("click", openWallEditor);
  const bindLook = (id, patch) => {
    $(id).addEventListener("input", () => setLook(patch($(id).value)));
  };
  bindLook("look-dim", (v) => ({ dim: clamp(Number(v) / 100, 0, 0.7, 0) }));
  bindLook("look-blur", (v) => ({ blur: clamp(Number(v), 0, 24, 11) }));
  bindLook("look-panel", (v) => ({ panelAlpha: clamp(Number(v) / 100, 0.3, 0.98, 0.72) }));
  bindLook("look-card", (v) => ({ cardAlpha: clamp(Number(v) / 100, 0.4, 1, 0.82) }));
  bindLook("look-radius", (v) => ({ radius: clamp(Number(v), 0, 24, 12) }));
  bindLook("tint-hue", (v) => ({ hue: clamp(Number(v), 0, 360, 205), tintAuto: false }));
  bindLook("tint-sat", (v) => ({ sat: clamp(Number(v), 0, 20, 9), tintAuto: false }));
  $("tint-auto").addEventListener("change", () => {
    setLook({ tintAuto: $("tint-auto").checked });
  });
  $("look-reset").addEventListener("click", () => setLook({ ...DEFAULT_LOOK }));

  // wall editor
  $("wall-editor-close").addEventListener("click", closeWallEditor);
  $("wall-editor-reset").addEventListener("click", resetWallFrame);
  $("wall-zoom").addEventListener("input", () => editWall({ scale: clampScale($("wall-zoom").value) }));
  const stage = $("wall-stage");
  stage.addEventListener("pointerdown", (event) => {
    if (!editor.image) return;
    stage.setPointerCapture(event.pointerId);
    const move = (moveEvent) => {
      const rect = stage.getBoundingClientRect();
      const cover = stageCover(stage, editor.image);
      const slackX = Math.max(cover.width - rect.width, 0);
      const slackY = Math.max(cover.height - rect.height, 0);
      const dx = -((moveEvent.clientX - rect.left) / rect.width - clamp01(theme.fx)) * slackX / (cover.width || 1);
      const dy = -((moveEvent.clientY - rect.top) / rect.height - clamp01(theme.fy)) * slackY / (cover.height || 1);
      setWallFocus(clamp01(theme.fx + dx), clamp01(theme.fy + dy));
    };
    const up = () => {
      stage.removeEventListener("pointermove", move);
      stage.removeEventListener("pointerup", up);
    };
    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerup", up);
  });

  // dups drawer
  $("dups-mode").addEventListener("click", (event) => {
    const mode = event.target.closest("[data-dup-mode]");
    if (!mode) return;
    state.dupMode = mode.dataset.dupMode;
    loadDuplicates(state.dupMode);
  });
  $("dups-smart").addEventListener("click", () => {
    document.querySelectorAll("#dups-body .dup-group").forEach((group) => {
      const keep = Number(group.dataset.keep) || 0;
      group.querySelectorAll(".dup-pick").forEach((box) => {
        box.checked = Number(box.value) !== keep;
      });
    });
    updateActionButtons();
    toast(t("dups.smartDone", { n: selectedDups().length }));
  });
  $("dups-select-all").addEventListener("click", () => {
    document.querySelectorAll("#dups-body .dup-pick").forEach((box) => { box.checked = true; });
    updateActionButtons();
    toast(t("bulk.count", { n: selectedDups().length }));
  });
  $("dups-clear").addEventListener("click", () => {
    document.querySelectorAll("#dups-body .dup-pick").forEach((box) => { box.checked = false; });
    updateActionButtons();
    toast(t("bulk.nothing"));
  });
  $("dups-delete").addEventListener("click", () => deleteIds(selectedDups()));
  $("dups-body").addEventListener("click", (event) => {
    const reveal = event.target.closest("[data-reveal]");
    if (reveal) openFile(reveal.dataset.reveal, true);
  });

  // results interactions (delegated)
  $("content").addEventListener("click", (event) => {
    const pick = event.target.closest("[data-pick]");
    if (pick) { togglePick(pick.dataset.pick, pick.checked); return; }
    // The drop buttons live INSIDE clickable rows: they must be matched before
    // the row's own data-open / data-saved-run, or the × runs the search
    // instead of removing the entry.
    const unpin = event.target.closest("[data-unpin]");
    if (unpin) { togglePin(Number(unpin.dataset.unpin), false); return; }
    const savedDrop = event.target.closest("[data-saved-drop]");
    if (savedDrop) { dropSaved(savedDrop.dataset.savedDrop); return; }
    const open = event.target.closest("[data-open]");
    if (open) { openFile(open.dataset.open, false); return; }
    const reveal = event.target.closest("[data-reveal]");
    if (reveal) { openFile(reveal.dataset.reveal, true); return; }
    const preview = event.target.closest("[data-preview]");
    if (preview) { openPreview(Number(preview.dataset.preview)); return; }
    const similar = event.target.closest("[data-similar]");
    if (similar) { openSimilar(Number(similar.dataset.similar)); return; }
    const pin = event.target.closest("[data-pin]");
    if (pin) { togglePin(Number(pin.dataset.pin)); return; }
    const tagAdd = event.target.closest("[data-tag-add]");
    if (tagAdd) { openAnnotate("tag", [Number(tagAdd.dataset.tagAdd)]); return; }
    const collAdd = event.target.closest("[data-collection-add]");
    if (collAdd) { openAnnotate("collection", [Number(collAdd.dataset.collectionAdd)]); return; }
    const why = event.target.closest("[data-why]");
    if (why) { openWhy(Number(why.dataset.why)); return; }
    const group = event.target.closest("[data-group]");
    if (group) { toggleGroup(group); return; }
    const tagRun = event.target.closest("[data-tag-run]");
    if (tagRun) { runToken(`tag:"${tagRun.dataset.tagRun}"`); return; }
    const savedRun = event.target.closest("[data-saved-run]");
    if (savedRun) {
      $("q").value = savedRun.dataset.savedRun;
      runSearch();
      return;
    }
    const annotateRun = event.target.closest("[data-annotate-run]");
    if (annotateRun) { submitAnnotate("tag", annotateRun.dataset.annotateRun); return; }
    const annotateColl = event.target.closest("[data-annotate-collection]");
    if (annotateColl) { submitAnnotate("collection", annotateColl.dataset.annotateCollection); return; }
    const card2 = event.target.closest(".gcard");
    if (card2) selectCard(Number(card2.dataset.index));
    if (!event.target.closest("#annotate, [data-tag-new]")) closeAnnotate();
  });

  // preview modal
  $("preview-close").addEventListener("click", closePreview);
  $("preview").addEventListener("click", (event) => {
    if (event.target === $("preview")) closePreview();
  });
  $("preview-body").addEventListener("click", (event) => {
    const open = event.target.closest("[data-open]");
    if (open) { openFile(open.dataset.open, false); return; }
    const reveal = event.target.closest("[data-reveal]");
    if (reveal) { openFile(reveal.dataset.reveal, true); return; }
    const similar = event.target.closest("[data-similar]");
    if (similar) { openSimilar(Number(similar.dataset.similar)); return; }
    const copy = event.target.closest("[data-copy-path]");
    if (copy) { copyText(copy.dataset.copyPath); return; }
    const pin = event.target.closest("[data-pin]");
    if (pin) togglePin(Number(pin.dataset.pin));
  });

  // similar modal
  $("modal-close").addEventListener("click", () => $("modal").classList.remove("open"));
  $("modal").addEventListener("click", (event) => {
    if (event.target === $("modal")) $("modal").classList.remove("open");
    const similarOpen = event.target.closest(".similar-row[data-open]");
    if (similarOpen) {
      $("modal").classList.remove("open");
      openFile(similarOpen.dataset.open, false);
      return;
    }
    const shotEl = event.target.closest("[data-preview]");
    if (shotEl) {
      $("modal").classList.remove("open");
      openPreview(Number(shotEl.dataset.preview));
    }
  });

  // settings modal: the gear opens About/language; the sliders icon opens the drawer
  $("settings-gear").addEventListener("click", () => setSettings(true));
  $("settings-close").addEventListener("click", () => setSettings(false));
  $("settings").addEventListener("click", (event) => {
    if (event.target === $("settings")) setSettings(false);
  });
  $("settings-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]");
    if (!tab) return;
    document.querySelectorAll(".settings-tab").forEach((node) => {
      node.classList.toggle("active", node.dataset.tab === tab.dataset.tab);
    });
    document.querySelectorAll(".settings-pane").forEach((node) => {
      node.classList.toggle("active", node.dataset.pane === tab.dataset.tab);
    });
  });
  document.body.addEventListener("click", (event) => {
    // The annotate popover lives outside #content, so its chips need a
    // document-level listener — the one on #content never sees them.
    const tagRun = event.target.closest("[data-annotate-run]");
    if (tagRun) { submitAnnotate("tag", tagRun.dataset.annotateRun); return; }
    const collRun = event.target.closest("[data-annotate-collection]");
    if (collRun) { submitAnnotate("collection", collRun.dataset.annotateCollection); return; }
    const lang = event.target.closest("[data-lang]");
    if (!lang) return;
    if (setLang(lang.dataset.lang)) {
      paintLang();
      paintSince();
      paintDynamicLabels();
      if (state.query) renderResults({ hits: state.hits }, false);
      else renderIdle();
    }
  });
  $("replay-onboarding").addEventListener("click", replayOnboarding);
  // Customization → minimal menu. The choice lives in this browser so the
  // minimal page can offer the way back without a server setting.
  const minimalMode = $("minimal-mode");
  if (minimalMode) {
    try { minimalMode.checked = localStorage.getItem("autofind.minimal.enabled") === "1"; }
    catch (error) { /* storage blocked: the button below still works */ }
    minimalMode.addEventListener("change", () => {
      try { localStorage.setItem("autofind.minimal.enabled", minimalMode.checked ? "1" : "0"); }
      catch (error) { /* session only */ }
      if (minimalMode.checked) location.href = "/minimal.html";
    });
  }
  $("minimal-open")?.addEventListener("click", () => { location.href = "/minimal.html"; });

  // bulk bar
  $("bulk-pin").addEventListener("click", async () => {
    const ids = [...state.picked];
    if (!ids.length) return;
    try {
      await api("/api/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, pinned: true }),
      });
      for (const id of ids) {
        const hit = state.hits.find((item) => item.file_id === id);
        if (hit) hit.pinned = true;
      }
      document.querySelectorAll("[data-pin]").forEach((star) => {
        if (state.picked.has(Number(star.dataset.pin))) star.classList.add("on");
      });
      toast(t("bulk.pinned", { n: ids.length }));
      state.picked.clear();
      updateBulkBar();
      if (state.query) renderResults({ hits: state.hits }, false);
    } catch (error) {
      toast(t("toast.failed", { error: error.message }));
    }
  });
  $("bulk-tag").addEventListener("click", () => openAnnotate("tag", [...state.picked]));
  $("bulk-collection").addEventListener("click", () => openAnnotate("collection", [...state.picked]));
  $("bulk-copy").addEventListener("click", () => {
    const rows = state.hits.filter((hit) => state.picked.has(hit.file_id));
    if (!rows.length) { toast(t("bulk.nothing")); return; }
    copyText(rows.map((hit) => hit.path).join("\n"));
  });
  $("bulk-export").addEventListener("click", exportSelectionCsv);
  $("bulk-clear").addEventListener("click", () => {
    state.picked.clear();
    document.querySelectorAll(".card-pick").forEach((box) => { box.checked = false; });
    document.querySelectorAll(".gcard.picked").forEach((node) => node.classList.remove("picked"));
    updateBulkBar();
  });

  // annotate popover: it lives outside #content, so it wires its own clicks
  $("annotate-close").addEventListener("click", closeAnnotate);
  $("annotate-body").addEventListener("click", (event) => {
    const run = event.target.closest("[data-annotate-run]");
    if (run) { submitAnnotate("tag", run.dataset.annotateRun); return; }
    const collection = event.target.closest("[data-annotate-collection]");
    if (collection) submitAnnotate("collection", collection.dataset.annotateCollection);
  });

  // welcome screen (the boot screen hands off to it on a first run)
  $("onboard-skip").addEventListener("click", finishOnboarding);
  $("onboard-done").addEventListener("click", finishOnboarding);
  $("ob-import").addEventListener("click", importKensaku);
  $("ob-import-scan").addEventListener("click", scanKensaku);
  $("ob-autostart").addEventListener("change", toggleAutostart);
  $("ob-index").addEventListener("click", startOnboardingIndex);

  // help overlay
  document.addEventListener("keydown", (event) => {
    if (event.key === "?" && !event.target.closest("input, textarea")) {
      const help = $("help-overlay");
      help.hidden = !help.hidden;
    }
  });
  document.querySelector(".help-close").addEventListener("click", () => {
    $("help-overlay").hidden = true;
  });
}

/* keyboard navigation over the results */

function selectCard(index) {
  state.selected = index;
  document.querySelectorAll(".gcard.selected").forEach((node) => node.classList.remove("selected"));
  const node = document.querySelector(`.gcard[data-index="${index}"]`);
  if (node) {
    node.classList.add("selected");
    node.scrollIntoView({ block: "nearest" });
  }
}

function selectedHit() {
  return visibleHits()[state.selected] || null;
}

function paintKindChips() {
  document.querySelectorAll(".chip[data-kind]").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.kind === state.kind);
  });
}

function openSort(open) {
  $("sort-menu").hidden = !open;
  $("sort-btn").setAttribute("aria-expanded", String(open));
}

function runToken(token) {
  const input = $("q");
  input.value = `${input.value} ${token}`.trim();
  runSearch();
  input.focus();
}

async function toggleGroup(button) {
  const cardNode = button.closest(".gcard");
  const existing = cardNode.parentElement.querySelector(`.group-list[data-for="${button.dataset.group}"]`);
  if (existing) { existing.remove(); button.classList.remove("open"); return; }
  const hit = visibleHits()[Number(cardNode.dataset.index)];
  if (!hit || !hit.group_members || !hit.group_members.length) return;
  button.classList.add("open");
  const list = document.createElement("div");
  list.className = "group-list";
  list.dataset.for = button.dataset.group;
  list.innerHTML = `<p class="settings-note">${esc(t("status.checking"))}</p>`;
  cardNode.parentElement.appendChild(list);
  const rows = await Promise.all(
    hit.group_members.slice(0, 24).map((id) =>
      api(`/api/preview?id=${id}&chars=0`).catch(() => null)),
  );
  list.innerHTML = rows.filter(Boolean).map((row) => `
    <div class="group-row">
      <button class="g-name" data-open="${row.file_id}">${esc(row.name)}</button>
      <span class="g-meta">${fmtSize(row.size)} · ${fmtDate(row.mtime)}</span>
      <button class="gact" data-reveal="${row.file_id}">${esc(t("card.folder"))}</button>
    </div>`).join("");
}

document.addEventListener("keydown", (event) => {
  const inField = Boolean(event.target.closest("input, textarea, select"));
  // Escape closes the topmost layer: boot, onboard, modals, drawers, popovers.
  if (event.key === "Escape") {
    if ($("boot")) { skipBoot(); return; }
    if (!$("annotate").hidden) { closeAnnotate(); return; }
    if ($("preview").classList.contains("open")) { closePreview(); return; }
    if ($("modal").classList.contains("open")) { $("modal").classList.remove("open"); return; }
    if ($("settings").classList.contains("open")) { setSettings(false); return; }
    if ($("wall-editor").classList.contains("open")) { closeWallEditor(); return; }
    if ($("dups-drawer").classList.contains("open")) { setDups(false); return; }
    if ($("panel").classList.contains("open")) { setPanel(false); return; }
    if (!$("help-overlay").hidden) { $("help-overlay").hidden = true; return; }
    return;
  }
  if (inField) return;
  const chord = chordOfEvent(event);
  const action = actionForChord(chord);
  if (["next", "prev", "open", "reveal", "preview", "select"].includes(action)) {
    const delta = action === "next" ? 1 : action === "prev" ? -1 : 0;
    if (delta) selectCard(state.selected < 0 ? 0 : Math.min(state.selected + delta, state.hits.length - 1));
    if (action === "open" && selectedHit()) openFile(selectedHit().file_id, false);
    if (action === "reveal" && selectedHit()) openFile(selectedHit().file_id, true);
    if (action === "preview" && selectedHit()) openPreview(selectedHit().file_id);
    if (action === "select" && selectedHit()) togglePick(selectedHit().file_id, !state.picked.has(selectedHit().file_id));
    event.preventDefault();
    return;
  }
  if (runHotkey(action)) event.preventDefault();
});

/* ============================================================ convenience UI
   Everything below rides on endpoints that already exist in core/api.py:
   /api/search, /api/preview, /api/ocr-boxes, /api/note, /api/notes,
   /api/similar, /api/audit, /api/self-test, /api/log, /api/health,
   /api/watch, /api/index, /api/collections. No new route is required. */

function noteNaturalDate(changes) {
  const host = $("meta");
  if (!host || !changes.length) return;
  host.querySelector(".natdate-note")?.remove();
  const note = document.createElement("span");
  note.className = "natdate-note";
  note.textContent = ui("natdate.note", { phrase: changes[0].phrase, token: changes[0].token });
  host.after(note);
  setTimeout(() => note.remove(), 6000);
}

/* ------------------------------------------------- 1/2: tokens and chips */

function folderFromQuery() {
  const token = filterTokens(state.query).find((item) => item.kind === "in" || item.kind === "path");
  return token && token.value ? token.value : "";
}

function renderQueryChips() {
  const host = $("query-chips");
  if (!host) return;
  const chips = [...(state.chips || [])];
  const seen = new Set(chips.map((chip) => chip.raw));
  for (const token of filterTokens(state.query)) {
    if (!seen.has(token.raw)) {
      chips.push({
        kind: token.kind, value: token.value, label: token.raw,
        raw: token.raw, source: token.raw,
      });
    }
  }
  host._chips = chips;
  host.innerHTML = `
    <div class="query-tools">
      <button class="mini-tool" id="undo-btn" type="button" title="${esc(ui("undo"))}" aria-label="${esc(ui("undo"))}">↶</button>
      <button class="mini-tool" id="redo-btn" type="button" title="${esc(ui("redo"))}" aria-label="${esc(ui("redo"))}">↷</button>
      <input class="within-input" id="within-input" type="search" spellcheck="false"
             placeholder="${esc(ui("within.placeholder"))}" value="${esc(state.withinText)}">
      <span class="within-count" id="within-count"></span>
      <button class="mini-tool reindex-folder" id="reindex-folder-btn" type="button"
              title="${esc(ui("reindexFolder"))}" hidden>↻ ${esc(ui("reindexFolder"))}</button>
    </div>
    ${chips.length ? `<div class="chip-row">${chips.map((chip, index) => `
      <span class="filter-chip" data-chip="${index}" title="${esc(ui("chip.title"))}">
        <span class="chip-label" data-chip-edit="${index}">${esc(chip.label || chip.raw || `${chip.kind}:${chip.value}`)}</span>
        <button class="chip-x" type="button" data-chip-remove="${index}" aria-label="remove">×</button>
      </span>`).join("")}</div>` : ""}`;
  host.hidden = false;
  updateUndoButtons();
  updateWithinCount();
  const reindex = $("reindex-folder-btn");
  if (reindex) reindex.hidden = !folderFromQuery();
}

function applyWithin(value) {
  state.withinText = String(value || "");
  // Re-render only the cards. Rebuilding the toolbar would drop focus from the
  // input on every keystroke, which makes "search within results" unusable.
  const grid = $("content")?.querySelector(".results-grid");
  if (grid) {
    const view = visibleHits();
    grid.innerHTML = view.map((hit, index) => card(hit, index, index)).join("");
    paintOcrBoxes();
    hydrateDocThumbs();
  }
  updateWithinCount();
}

function updateWithinCount() {
  const el = $("within-count");
  if (el) el.textContent = ui("within.count", { n: visibleHits().length, total: state.hits.length });
}

/* ------------------------------------------------ 19: undo / redo filters */

function pushFilterHistory(query) {
  const stack = state.filterStack;
  if (stack[state.filterPos] === query) return;
  stack.splice(state.filterPos + 1);
  stack.push(query);
  if (stack.length > 60) stack.shift();
  state.filterPos = stack.length - 1;
  updateUndoButtons();
}

function updateUndoButtons() {
  const undo = $("undo-btn");
  const redo = $("redo-btn");
  if (undo) undo.disabled = state.filterPos <= 0;
  if (redo) redo.disabled = state.filterPos >= state.filterStack.length - 1;
}

function applyFilterAt(position) {
  const query = state.filterStack[position] || "";
  // Move the cursor before searching. Without this, pushFilterHistory sees the
  // query already on the stack, returns early, and undo keeps appending the
  // same entry instead of walking back through the stack.
  state.filterPos = position;
  $("q").value = query;
  updateUndoButtons();
  runSearch({ skipHistory: true });
}

function undoFilter() {
  if (state.filterPos > 0) applyFilterAt(state.filterPos - 1);
}

function redoFilter() {
  if (state.filterPos < state.filterStack.length - 1) applyFilterAt(state.filterPos + 1);
}

/* ---------------------------------------------- 4: history with context */

const HISTORY_KEY = "af.query-history";

function loadHistory() {
  if (!state.queryLog.length) state.queryLog = storageGet(HISTORY_KEY, []) || [];
  return state.queryLog;
}

function recordHistory(query, count) {
  if (!query) return;
  const list = loadHistory();
  const last = list[0];
  if (last && last.query === query) {
    last.time = Date.now();
    last.count = count || 0;
  } else {
    list.unshift({ query, time: Date.now(), count: count || 0, opened: "" });
  }
  state.queryLog = list.slice(0, 40);
  storageSet(HISTORY_KEY, state.queryLog);
  renderHistory();
}

function openedInHistory(name) {
  const list = loadHistory();
  if (!list.length || !name) return;
  if (list[0].opened === name) return;
  list[0].opened = name;
  storageSet(HISTORY_KEY, list);
  renderHistory();
}

function renderHistory() {
  const host = $("history-list");
  if (!host) return;
  const list = loadHistory();
  host.innerHTML = list.length
    ? list.map((item, index) => `
      <div class="history-row" data-history-run="${esc(item.query)}">
        <span class="h-query">${esc(item.query)}</span>
        <span class="h-meta">${esc(new Date(item.time).toLocaleString())} · ${esc(ui("history.count", { n: item.count }))}${
          item.uses ? ` · ×${item.uses}` : ""
        }${
          item.opened ? ` · ${esc(ui("history.opened", { name: item.opened }))}` : ""
        }</span>
        <button class="row-x" type="button" data-history-drop="${index}" aria-label="remove">×</button>
      </div>`).join("")
    : `<p class="settings-note">${esc(ui("history.empty"))}</p>`;
}

function clearHistory() {
  state.queryLog = [];
  storageSet(HISTORY_KEY, []);
  renderHistory();
}

/* Context the server cannot know (result count, what was opened) lives in
   localStorage; the query list itself comes from /api/history. */
async function loadServerHistory() {
  try {
    const data = await api("/api/history?limit=20");
    const local = loadHistory();
    const byQuery = new Map(local.map((item) => [item.query, item]));
    const merged = (data.history || []).map((item) => {
      const own = byQuery.get(item.query) || {};
      return {
        query: item.query,
        time: (item.at || 0) * 1000 || own.time || Date.now(),
        count: own.count || 0,
        opened: own.opened || "",
        uses: item.uses || 0,
      };
    });
    for (const item of local) {
      if (!merged.some((entry) => entry.query === item.query)) merged.push(item);
    }
    state.queryLog = merged.slice(0, 40);
    renderHistory();
  } catch (error) {
    /* the local log is still a complete history */
  }
}

/* ------------------------------------------- 14: reindex current folder */

async function reindexFolder() {
  const folder = folderFromQuery();
  if (!folder) { toast(ui("reindex.noFolder")); return; }
  try {
    await api("/api/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roots: [folder], prune: false }),
    });
    toast(ui("reindex.started"));
    pollJob();
  } catch (error) {
    toast(error.message);
  }
}

/* ------------------------------------------------- 5: hover preview */

function wireHover() {
  const host = $("hover-preview");
  const content = $("content");
  if (!host || !content) return;
  const hide = () => { clearTimeout(state.hoverTimer); host.hidden = true; };
  content.addEventListener("mouseover", (event) => {
    const cardNode = event.target.closest(".gcard");
    if (!cardNode || cardNode.contains(event.relatedTarget)) return;
    const id = Number(cardNode.dataset.file || 0);
    if (!id) return;
    clearTimeout(state.hoverTimer);
    state.hoverId = id;
    state.hoverTimer = setTimeout(() => showHover(id, cardNode), 380);
  });
  content.addEventListener("mouseout", (event) => {
    const cardNode = event.target.closest(".gcard");
    if (!cardNode || cardNode.contains(event.relatedTarget)) return;
    hide();
  });
  host.addEventListener("mouseenter", () => clearTimeout(state.hoverTimer));
  host.addEventListener("mouseleave", hide);
}

async function showHover(id, cardNode) {
  const host = $("hover-preview");
  if (!host || id !== state.hoverId || !cardNode.isConnected) return;
  try {
    const data = await api(`/api/preview?id=${id}&chars=500`);
    if (id !== state.hoverId) return;
    const body = data.image
      ? `${data.image.thumb ? `<img class="hv-thumb" src="${data.image.thumb}" alt="">` : ""}
         ${data.image.ocr ? `<div class="hv-text">${highlight(String(data.image.ocr).slice(0, 300), state.query, 2)}</div>` : ""}`
      : `<div class="hv-text">${highlight(String(data.text || "").slice(0, 600), state.query, 2)}</div>`;
    host.innerHTML = `<div class="hv-head">${esc(data.name || "")}</div>${body}`;
    const rect = cardNode.getBoundingClientRect();
    host.hidden = false;
    const left = Math.min(rect.right + 12, window.innerWidth - host.offsetWidth - 12);
    const top = Math.min(Math.max(rect.top, 12), window.innerHeight - host.offsetHeight - 12);
    host.style.left = `${Math.max(12, left)}px`;
    host.style.top = `${Math.max(12, top)}px`;
  } catch (error) {
    /* A hover preview is best-effort: a failed request must never flash. */
  }
}

/* ------------------------------------- 7/16: OCR words and notes in preview */

function ocrWords(text, query) {
  return String(text || "").split(/(\s+)/).map((part) => {
    if (/^\s+$/.test(part)) return esc(part);
    const word = part.replace(/[^\w\u0430-\u044f\u0451\u0410-\u042f\u04010-9_-]/g, "");
    return `<button class="ocr-word" type="button" data-ocr-word="${esc(word)}">${highlight(part, query, 2)}</button>`;
  }).join("");
}

function notesEditor(fileId) {
  return `<div class="note-editor" data-note-id="${fileId}">
    <div class="note-head"><b>${esc(ui("notes.label"))}</b>
      <button class="btn mini primary" type="button" data-note-save="${fileId}">${esc(ui("notes.save"))}</button></div>
    <textarea class="note-input" id="note-input" rows="3" spellcheck="false"
              placeholder="${esc(ui("notes.placeholder"))}">${esc(state.noteText || "")}</textarea>
  </div>`;
}

async function loadNoteInto(fileId) {
  try {
    const data = await api(`/api/notes?ids=${fileId}`);
    const map = data.notes || {};
    const raw = map[String(fileId)] ?? map[fileId] ?? "";
    state.noteText = typeof raw === "string" ? raw : (raw.text || "");
    const area = $("note-input");
    const editor = $("preview-body")?.querySelector(`[data-note-id="${fileId}"]`);
    if (area && editor) area.value = state.noteText;
  } catch (error) {
    state.noteText = "";
  }
}

async function saveNote(fileId) {
  const area = $("note-input");
  const text = area ? area.value : "";
  try {
    await api("/api/note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: fileId, text }),
    });
    state.noteText = text;
    toast(ui("notes.saved"));
  } catch (error) {
    toast(error.message);
  }
}

/* ------------------------------------------------ 11: tag chip dropdown */

function closeTagMenus() {
  document.querySelectorAll(".tag-dropdown").forEach((node) => node.remove());
  document.querySelectorAll("[data-tag-menu][aria-expanded='true']")
    .forEach((node) => node.setAttribute("aria-expanded", "false"));
}

function toggleTagMenu(cardNode, id) {
  const existing = cardNode.querySelector(".tag-dropdown");
  if (existing) { closeTagMenus(); return; }
  closeTagMenus();
  const menu = document.createElement("div");
  menu.className = "tag-dropdown";
  menu.innerHTML = `
    <div class="tag-dd-head">${esc(ui("tag.recent"))}</div>
    <div class="tag-dd-chips">${(state.metaTags || []).slice(0, 12).map((tag) =>
      `<button class="tag-chip" type="button" data-tag-run="${esc(tag)}">#${esc(tag)}</button>`).join("")
      || `<p class="settings-note">—</p>`}</div>
    <div class="tag-dd-add"><input class="tag-dd-input" spellcheck="false"
      placeholder="${esc(ui("tag.new"))}" aria-label="${esc(ui("tag.new"))}">
      <button class="btn mini primary" type="button" data-tag-new="${id}">+</button></div>`;
  cardNode.appendChild(menu);
  const trigger = cardNode.querySelector(`[data-tag-menu="${id}"]`);
  if (trigger) trigger.setAttribute("aria-expanded", "true");
  menu.querySelector("input")?.focus();
}

/* --------------------------------------- 21: regex builder + live preview */

let regexTimer = 0;

function regexFlags() {
  return [...document.querySelectorAll("#regex-flags input:checked")].map((box) => box.value).join("");
}

function liveRegexSearch(pattern) {
  const host = $("regex-live");
  if (!host) return;
  clearTimeout(regexTimer);
  if (!pattern) { host.textContent = ""; return; }
  regexTimer = setTimeout(async () => {
    host.textContent = ui("logs.running");
    try {
      const q = `re:"${String(pattern).replace(/"/g, "")}"`;
      const data = await api(`/api/search?q=${encodeURIComponent(q)}&limit=5`);
      const count = Number(data.total || 0);
      host.innerHTML = `<b>${esc(ui("regex.matchCount", { n: count }))}</b>` +
        (data.hits || []).slice(0, 5).map((hit) =>
          `<div class="regex-hit" data-open="${hit.file_id}">${esc(hit.name)}</div>`).join("");
    } catch (error) {
      host.textContent = ui("regex.noMatches");
    }
  }, 320);
}

function paintRegexSample() {
  const pattern = $("regex-pattern")?.value || "";
  const sample = $("regex-sample")?.value || "";
  const view = $("regex-sample-view");
  if (!view) return;
  if (!pattern) { view.textContent = ""; liveRegexSearch(""); return; }
  let re;
  try { re = new RegExp(pattern, `${regexFlags()}g`); }
  catch (error) {
    view.innerHTML = `<span class="regex-error">${esc(error.message)}</span>`;
    liveRegexSearch("");
    return;
  }
  let html = "";
  let last = 0;
  let count = 0;
  let match;
  while ((match = re.exec(sample))) {
    if (match[0] === "") { re.lastIndex += 1; continue; }
    html += esc(sample.slice(last, match.index)) + `<mark>${esc(match[0])}</mark>`;
    last = match.index + match[0].length;
    if (++count > 200) break;
  }
  html += esc(sample.slice(last));
  view.innerHTML = html || "<span class='regex-dim'>—</span>";
  liveRegexSearch(pattern);
}

function openRegexBuilder(open) {
  const box = $("regex-builder");
  if (!box) return;
  box.hidden = !open;
  $("regex-open").setAttribute("aria-expanded", String(open));
  if (open) paintRegexSample();
}

/* --------------------------------- 13: watcher status / index freshness */

async function refreshFreshness() {
  const badge = $("fresh-badge");
  if (!badge) return;
  try {
    const [watch, health] = await Promise.all([api("/api/watch"), api("/api/health")]);
    const watching = watch && watch.running ? ui("fresh.watching") : ui("fresh.off");
    const age = health && health.last_index_age_s != null
      ? ui("fresh.age", { age: fmtAge(health.last_index_age_s) })
      : ui("fresh.unknown");
    badge.textContent = `${watching} · ${age}`;
    badge.classList.toggle("on", Boolean(watch && watch.running));
    badge.hidden = false;
  } catch (error) {
    badge.hidden = true;
  }
}

/* -------------------------- 22/23: smart collections and drop targets */

function renderSmartCollections() {
  const host = $("smart-list");
  if (!host) return;
  const saved = (state.personal && state.personal.saved) || [];
  host.innerHTML = saved.length
    ? saved.map((item) => `
      <div class="smart-row" data-saved-run="${esc(item.query)}" title="${esc(item.query)}">
        <span class="s-mark">★</span>
        <span class="s-q">${esc(item.label || item.query)}</span>
        <span class="s-used">${item.used || 0}</span>
      </div>`).join("")
    : `<p class="settings-note">${esc(ui("history.empty"))}</p>`;
}

function renderCollectionTargets() {
  const host = $("collection-targets");
  if (!host) return;
  host.innerHTML = (state.metaCollections || []).map((item) => `
    <div class="collection-target" data-drop-collection="${item.id}">
      <span>${esc(item.name || item.id)}</span><small>${item.count ?? ""}</small>
    </div>`).join("") || `<p class="settings-note">${esc(t("annotate.empty"))}</p>`;
}

function wireDnD() {
  document.addEventListener("dragstart", (event) => {
    const cardNode = event.target.closest(".gcard[data-drag-file]");
    if (!cardNode || !event.dataTransfer) return;
    event.dataTransfer.setData("text/plain", cardNode.dataset.dragFile);
    event.dataTransfer.effectAllowed = "copy";
    cardNode.classList.add("dragging");
  });
  document.addEventListener("dragend", () => {
    document.querySelectorAll(".gcard.dragging").forEach((node) => node.classList.remove("dragging"));
    document.querySelectorAll(".collection-target.over").forEach((node) => node.classList.remove("over"));
  });
  const targets = $("collection-targets");
  if (!targets) return;
  targets.addEventListener("dragover", (event) => {
    const target = event.target.closest("[data-drop-collection]");
    if (!target) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    target.classList.add("over");
  });
  targets.addEventListener("dragleave", (event) => {
    event.target.closest("[data-drop-collection]")?.classList.remove("over");
  });
  targets.addEventListener("drop", async (event) => {
    const target = event.target.closest("[data-drop-collection]");
    if (!target) return;
    event.preventDefault();
    target.classList.remove("over");
    const fileId = Number(event.dataTransfer.getData("text/plain"));
    if (!fileId) return;
    try {
      await api("/api/collections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", id: Number(target.dataset.dropCollection), ids: [fileId] }),
      });
      await loadMeta(true);
      renderCollectionTargets();
      toast(t("annotate.added", { n: 1 }));
    } catch (error) {
      toast(error.message);
    }
  });
}

/* ---------------- 12/18/24/25: exclusions, audit, logs, telemetry badge */

/* The server owns index exclusions: GET/POST /api/exclusions are already in
   core/api.py, so the editor writes real rules instead of a local imitation. */
async function loadExclusions() {
  try {
    const data = await api("/api/exclusions");
    state.exclusions = Array.isArray(data.exclusions) ? data.exclusions : [];
  } catch (error) {
    state.exclusions = [];
  }
  const area = $("exclusions-editor");
  if (area) area.value = state.exclusions.join("\n");
}

async function saveExclusions() {
  const patterns = ($("exclusions-editor")?.value || "")
    .split("\n").map((line) => line.trim()).filter(Boolean);
  try {
    const data = await api("/api/exclusions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exclusions: patterns }),
    });
    state.exclusions = Array.isArray(data.exclusions) ? data.exclusions : patterns;
    const area = $("exclusions-editor");
    if (area) area.value = state.exclusions.join("\n");
    toast(ui("exclusions.saved"));
  } catch (error) {
    toast(error.message);
  }
}

/* 26: portable-mode indicator, from /api/portable. */
async function renderPortableBadge() {
  const badge = $("portable-badge");
  if (!badge) return;
  try {
    const data = await api("/api/portable");
    badge.textContent = ui(data.portable ? "portable.on" : "portable.off");
    badge.classList.toggle("on", Boolean(data.portable));
    badge.hidden = false;
  } catch (error) {
    badge.hidden = true;
  }
}

async function renderAuditBreakdown() {
  const host = $("audit-breakdown");
  if (!host) return;
  try {
    const data = await api("/api/audit");
    const rows = [
      [t("audit.files"), data.files, data.total_bytes],
      [t("audit.chunks"), data.chunks, 0],
      [t("audit.images"), data.images, 0],
      [t("audit.vectors"), data.vectors, 0],
      [t("audit.pins"), data.personal?.pins || 0, 0],
    ];
    const max = Math.max(...rows.map((row) => Number(row[1]) || 0), 1);
    host.innerHTML = rows.map(([label, value, bytes]) => `
      <div class="breakdown-row">
        <span>${esc(label)}</span>
        <span class="breakdown-bar"><i style="width:${Math.max(2, Math.round((Number(value) || 0) / max * 100))}%"></i></span>
        <b>${Number(value) || 0}${bytes ? ` · ${fmtSize(bytes)}` : ""}</b>
      </div>`).join("");
  } catch (error) {
    host.innerHTML = `<p class="settings-note">${esc(error.message)}</p>`;
  }
}

async function showSelfTest() {
  const out = $("diag-output");
  if (!out) return;
  out.hidden = false;
  out.textContent = ui("logs.running");
  try {
    const data = await api("/api/self-test");
    out.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    out.textContent = error.message;
  }
}

async function showLog() {
  const out = $("diag-output");
  if (!out) return;
  out.hidden = false;
  out.textContent = ui("logs.running");
  try {
    const data = await api("/api/log?lines=200");
    out.textContent = (data.lines || []).join("\n") || "(empty)";
  } catch (error) {
    out.textContent = error.message;
  }
}

function renderTelemetryBadge() {
  const badge = $("telemetry-badge");
  if (badge) badge.textContent = ui("telemetry");
}

/* ------------------------------------------------------- event wiring */

function wireFeatures() {
  // Static labels for elements we cannot translate through i18n.js.
  const labels = {
    "regex-open": "regex.open", "regex-title": "regex.title",
    "regex-pattern-label": "regex.pattern", "regex-sample-label": "regex.sample",
    "history-title": "history.title", "history-clear": "history.clear",
    "smart-title": "smart.title", "collections-title": "collections.title",
    "exclusions-title": "exclusions.title", "exclusions-note": "exclusions.note",
    "exclusions-editor": "exclusions.placeholder",
    "audit-breakdown-title": "audit.title", "diagnostics-title": "logs.title",
    "selftest-btn": "logs.selfTest", "logs-btn": "logs.viewLog",
    "telemetry-badge": "telemetry",
  };
  for (const [id, key] of Object.entries(labels)) {
    const node = $(id);
    if (node) node.textContent = ui(key);
  }

  // Regex builder.
  $("regex-open")?.addEventListener("click", (event) => {
    event.stopPropagation();
    openRegexBuilder($("regex-builder").hidden);
  });
  $("regex-close")?.addEventListener("click", () => openRegexBuilder(false));
  $("regex-insert")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-insert]");
    if (!button) return;
    const input = $("regex-pattern");
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? start;
    const text = button.dataset.insert.replace("…", "");
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    input.focus();
    input.setSelectionRange(start + text.length, start + text.length);
    paintRegexSample();
  });
  $("regex-pattern")?.addEventListener("input", paintRegexSample);
  $("regex-sample")?.addEventListener("input", paintRegexSample);
  $("regex-flags")?.addEventListener("change", paintRegexSample);
  $("regex-live")?.addEventListener("click", (event) => {
    const hit = event.target.closest("[data-open]");
    if (hit) openFile(hit.dataset.open, false);
  });

  // Query toolbar: chips, undo/redo, within-results, folder reindex.
  const bar = $("query-chips");
  bar?.addEventListener("click", (event) => {
    const chips = bar._chips || [];
    const remove = event.target.closest("[data-chip-remove]");
    if (remove) {
      const chip = chips[Number(remove.dataset.chipRemove)];
      if (chip) clearChip(chip.source || chip.raw);
      return;
    }
    const edit = event.target.closest("[data-chip-edit]");
    if (edit) {
      const chip = chips[Number(edit.dataset.chipEdit)];
      if (chip) {
        const input = $("q");
        input.focus();
        const at = input.value.indexOf(chip.source || chip.raw);
        if (at >= 0) input.setSelectionRange(at, at + (chip.source || chip.raw).length);
      }
      return;
    }
    if (event.target.closest("#undo-btn")) { undoFilter(); return; }
    if (event.target.closest("#redo-btn")) { redoFilter(); return; }
    if (event.target.closest("#reindex-folder-btn")) { reindexFolder(); return; }
  });
  bar?.addEventListener("input", (event) => {
    if (event.target.id === "within-input") applyWithin(event.target.value);
  });

  // First-card pin and per-card tag dropdown.
  $("content")?.addEventListener("click", (event) => {
    const best = event.target.closest("[data-pin-best]");
    if (best) { togglePin(Number(best.dataset.pinBest), true); return; }
    const menu = event.target.closest("[data-tag-menu]");
    if (menu) {
      toggleTagMenu(menu.closest(".gcard"), Number(menu.dataset.tagMenu));
      return;
    }
    const input = event.target.closest(".tag-dd-input");
    if (input) return;
    const newTag = event.target.closest("[data-tag-new]");
    if (newTag) {
      const value = newTag.parentElement.querySelector(".tag-dd-input")?.value.trim();
      if (value) {
        // submitAnnotate shares the bulk popover's target list. Without this
        // the per-card "+" silently returns because annotateIds is empty.
        annotateIds = [Number(newTag.dataset.tagNew)];
        submitAnnotate("tag", value);
      }
    }
  });

  // OCR word click, note save.
  $("preview-body")?.addEventListener("click", async (event) => {
    const word = event.target.closest("[data-ocr-word]");
    if (word) {
      const term = word.dataset.ocrWord;
      if (!term) return;
      const holder = $("preview-body").querySelector(".ocr-boxes");
      try {
        const data = await api(`/api/ocr-boxes?id=${state.previewId || currentPreviewId()}&q=${encodeURIComponent(term)}`);
        paintBoxesInto(holder, data, term);
      } catch (error) { /* keep the existing overlay */ }
      return;
    }
    const save = event.target.closest("[data-note-save]");
    if (save) saveNote(Number(save.dataset.noteSave));
  });

  // History rows in the drawer.
  $("history-list")?.addEventListener("click", (event) => {
    const drop = event.target.closest("[data-history-drop]");
    if (drop) {
      const list = loadHistory();
      list.splice(Number(drop.dataset.historyDrop), 1);
      state.queryLog = list;
      storageSet(HISTORY_KEY, list);
      renderHistory();
      return;
    }
    const run = event.target.closest("[data-history-run]");
    if (run) {
      $("q").value = run.dataset.historyRun;
      runSearch();
    }
  });
  $("history-clear")?.addEventListener("click", clearHistory);

  // Settings: exclusions, diagnostics.
  $("exclusions-save")?.addEventListener("click", saveExclusions);
  $("selftest-btn")?.addEventListener("click", showSelfTest);
  $("logs-btn")?.addEventListener("click", showLog);

  // Dismiss layers on an outside click.
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".tag-dropdown, [data-tag-menu]")) closeTagMenus();
    if (!event.target.closest("#regex-builder, #regex-open")) openRegexBuilder(false);
  });

  wireHover();
  wireDnD();
}

/* The preview modal does not currently store its id, so fall back to the
   clicked file id carried by its action buttons. */
function currentPreviewId() {
  const node = $("preview-body")?.querySelector("[data-open], [data-pin], [data-note-save]");
  return Number(node?.dataset.open || node?.dataset.pin || node?.dataset.noteSave || 0);
}

/* --------------------------------------------------------------- init */

applyStaticText();
paintDynamicLabels();
loadLook();
loadTheme();
renderWalls();
renderLook();
paintLang();
paintSince();
renderIdle();
wireWindowControls();
loadStats();
loadTypes().then(() => { renderTypes(); paintCustomChips(); });
loadMeta().then(() => {
  const datalist = $("filter-tag-list");
  if (datalist) {
    datalist.innerHTML = state.metaTags.map((tag) => `<option value="${esc(tag)}"></option>`).join("");
  }
  const collectionSelect = $("f-collection");
  if (collectionSelect) {
    collectionSelect.innerHTML = `<option value="">${esc(t("filters.any"))}</option>` +
      state.metaCollections.map((collection) =>
        `<option value="${esc(collection.name || collection.id)}">${esc(collection.name || collection.id)}</option>`).join("");
  }
  renderCollectionTargets();
  populateFilterMenus();
});
loadHotkeys();
startWatchPoll();
loadExclusions();
renderHistory();
loadServerHistory();
renderSmartCollections();
renderCollectionTargets();
refreshFreshness();
setInterval(refreshFreshness, 30000);
wireFeatures();
api("/api/progress").then((job) => {
  // A run that died on this boot must not greet the user with a green bar.
  if (job.error) $("bar").style.background = "var(--red)";
  if (job.running) { $("index-btn").disabled = true; pollJob(); }
});

wire();

const params = new URLSearchParams(location.search);
const initial = params.get("q");
if (initial) {
  $("q").value = initial;
  runSearch();
}
// Deep link to a file: `?preview=<id>` opens Quick Look straight away, which is
// what a link pasted into a chat should do.
const initialPreview = params.get("preview");
if (Number.isInteger(Number(initialPreview)) && Number(initialPreview) > 0) openPreview(Number(initialPreview));

if (initial || initialPreview) skipBoot();
else boot();
