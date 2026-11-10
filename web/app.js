const $ = (id) => document.getElementById(id);
const state = {
  query: "", kind: "", folder: "", since: "", sort: "best", docdate: false,
  timer: null, jobTimer: null,
  hits: [], total: 0, nextOffset: null, approx: false, selected: -1, chips: [],
  stats: null, status: null,
};

// The three letters inside the file-type tile. Abbreviations of extensions, not
// prose: they stay as they are in every language.
const KIND_LABEL = {
  pdf: "pdf", docx: "docx", code: "code", text: "txt", image: "img",
  pptx: "pptx", xlsx: "xlsx", epub: "epub", odt: "odt", rtf: "rtf",
};

const WEEKDAYS = [
  "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
];

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function locale() {
  return currentLang() === "ru" ? "ru-RU" : "en-GB";
}

function dateLabel(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(locale());
}

function sizeLabel(bytes) {
  return `${(bytes / 1024).toFixed(0)} ${t("unit.kb")}`;
}

function highlight(snippet, query) {
  let html = esc(snippet);
  const terms = (query.toLowerCase().match(/[\p{L}\p{N}_]+/gu) || [])
    .filter((item) => item.length >= 3)
    .slice(0, 12);
  for (const term of terms) {
    const safe = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    html = html.replace(new RegExp(`(${safe})`, "giu"), "<mark>$1</mark>");
  }
  return html;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 3200);
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

/* ----------------------------------------------------------------- calendar */

/* The "since" filter used to be `<input type="date">`. A native date input
   draws its ghost text and its popup in the OS locale and cannot be told
   otherwise from script — an English interface on a Russian Windows still said
   "дд.мм.гггг" and opened a Russian calendar. This one is ours: rounded, grey,
   and it speaks the interface language because its month and weekday names come
   from the same dictionary as everything else. */
const CAL = { open: false, year: 0, month: 0 };

function pad2(number) {
  return String(number).padStart(2, "0");
}

function isoDate(date) {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function parseIso(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  return match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : null;
}

function calWords(key) {
  return t(key).split(",").map((word) => word.trim());
}

function paintSince() {
  const label = $("since-label");
  if (!label) return;
  const date = parseIso(state.since);
  label.textContent = date
    ? date.toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" })
    : t("tools.any");
  $("since-btn").classList.toggle("set", Boolean(date));
}

function openCalendar(open) {
  const box = $("calendar");
  if (!box) return;
  CAL.open = open;
  box.hidden = !open;
  $("since-btn").setAttribute("aria-expanded", String(open));
  if (!open) return;
  const anchor = parseIso(state.since) || new Date();
  CAL.year = anchor.getFullYear();
  CAL.month = anchor.getMonth();
  renderCalendar();
}

function renderCalendar() {
  const months = calWords("cal.months");
  $("cal-title").textContent = t("cal.monthYear", {
    month: months[CAL.month], year: CAL.year,
  });
  $("cal-week").innerHTML = calWords("cal.weekdays")
    .map((name) => `<span>${esc(name)}</span>`).join("");

  // Monday first, which is what the rest of the interface assumes: the week
  // starts on Monday in the locale this was written for, and a Sunday-first
  // grid next to a Monday-first hint row is a bug waiting to be reported.
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

function setSince(value) {
  state.since = value;
  paintSince();
  openCalendar(false);
  // The day button that was just clicked is gone with the popup; focus goes
  // back to the trigger so the keyboard is not stranded on a hidden element.
  $("since-btn").focus();
  if (state.query) runSearch();
}

/* ------------------------------------------------------------------ search */

function scheduleSearch() {
  clearTimeout(state.timer);
  state.timer = setTimeout(runSearch, 150);
}

async function runSearch(options = {}) {
  const append = Boolean(options.append);
  const query = $("q").value.trim();
  state.query = query;
  // The bookmark only exists while there is something to save.
  $("save-btn").classList.toggle("show", Boolean(query));
  if (!query) {
    state.hits = [];
    state.total = 0;
    state.nextOffset = null;
    renderIdle();
    $("meta").textContent = "";
    return;
  }
  const offset = append ? (state.nextOffset ?? 0) : 0;
  const params = new URLSearchParams({ q: query, limit: "12", offset: String(offset) });
  if (state.kind) params.set("kind", state.kind);
  if (state.folder) params.set("folder", state.folder);
  if (state.since) params.set("since", state.since);
  if (state.docdate) params.set("docdate", "1");
  if (state.sort && state.sort !== "best") params.set("sort", state.sort);

  try {
    const data = await api(`/api/search?${params}`);
    state.hits = append ? state.hits.concat(data.hits) : data.hits;
    state.total = data.total ?? data.hits.length;
    state.nextOffset = data.next_offset ?? null;
    state.approx = Boolean(data.approx);
    state.chips = data.chips || [];
    if (!append) rememberQuery(query);
    renderResults(data, append);
    const shown = state.hits.length;
    $("meta").textContent = shown
      ? t("search.meta", {
        shown, total: state.total, ms: data.ms, sign: state.approx ? "≈" : "",
      })
      : `${data.ms} ${t("unit.ms")}`;
  } catch (error) {
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

function renderIdle() {
  // The kind tabs live outside #content, so replacing the page does not touch
  // them: without this the last search's facet numbers stay glued to the tabs
  // after the query is cleared.
  paintFacets({});
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
  loadPersonal().then(paintPersonal);
  loadRecent();
}

/* ------------------------------------------------------------------ theme */

/* The window is a photo with an interface on top of it, so the interface cannot
   have one fixed palette: a blue-grey panel on a warm sand photo reads as a
   mistake. Everything is derived from a single hue, `--hue`, and a background
   brings its own: the presets carry theirs, and a picture chosen by hand is
   measured with a canvas — the circular mean of its saturated pixels, which is
   the colour the eye remembers rather than the average RGB (which is almost
   always grey).

   The choice lives in `localStorage`, the picture itself on the server: a 4 MB
   photo does not belong in a 5 MB storage quota, and `~/.autofind` travels with
   a backup while a cache clear would take the wallpaper with it. */
const WALL_KEY = "autofind.wall";
const WALL_MAX_MB = 12;

const WALLS = [
  { id: "forest", wall: 'url("/fon.jpg")', hue: 205, sat: 8 },
  { id: "dusk", wall: "linear-gradient(155deg,#3b2450 0%,#1a1124 48%,#0b0a10 100%)", hue: 272, sat: 10 },
  { id: "slate", wall: "linear-gradient(155deg,#28313a 0%,#141a1f 52%,#0a0d10 100%)", hue: 208, sat: 8 },
  { id: "sand", wall: "linear-gradient(155deg,#3c3024 0%,#1e1813 52%,#0c0b09 100%)", hue: 34, sat: 11 },
  { id: "ocean", wall: "linear-gradient(155deg,#0f3a44 0%,#0a1b20 52%,#050b0d 100%)", hue: 191, sat: 12 },
];

// A chosen picture is framed after upload in its own editor: drag puts the
// focus point where the eye should land, the slider (or the wheel) pushes in.
// The focus is a fraction of the picture (fx, fy) that always sits at the same
// fraction of the screen, so zooming never drags the subject off its spot. The
// presets are gradients, so the editor means nothing for them and its button is
// hidden. Scale starts at 1: anything below lets the layer shrink away from the
// screen edges and show the void behind it.
const WALL_ZOOM_MIN = 1;
const WALL_ZOOM_MAX = 2.5;

const clamp01 = (value) => Math.min(1, Math.max(0, Number(value) || 0));

function clampScale(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 1;
  return Math.min(WALL_ZOOM_MAX, Math.max(WALL_ZOOM_MIN, Math.round(number * 100) / 100));
}

let theme = WALLS[0];

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
    // Saves from the three-button era carry `pos` as a word; it maps onto the
    // vertical focus and keeps the picture where the user last put it.
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

function renderWalls() {
  const row = $("wall-row");
  if (!row) return;
  row.querySelectorAll("[data-wall]").forEach((tile) => {
    const preset = WALLS.find((item) => item.id === tile.dataset.wall);
    // Only the image, never the `background` shorthand: the shorthand would
    // reset `cover` and the tile would show the top-left corner of the photo
    // instead of the picture as it will actually be laid out.
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

function renderWallEdit() {
  const edit = $("wall-edit");
  // The controls only make sense for a picture the user chose, not a gradient.
  if (edit) edit.hidden = theme.id !== "custom";
}

/* ------------------------------------------------------------ wall editor */

/* The editor preview is painted by the same four variables as the real layer
   (`--wall`, `--wall-pos`, `--wall-scale`), so there is no second way to draw a
   wallpaper that could drift from the first. Math for the drag: the composite
   of `background-position: fx fy` and a `scale(s)` about the same point is
   identical to a picture drawn at `cover * s` with the focus fraction (fx, fy)
   of the image hidden off the left/top by fx/fy of the slack — so dragging the
   picture by dx pixels simply moves fx by dx over that slack. */
const editor = {
  open: false,
  image: null, // natural size of the current picture, for the slack math
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
    editor.image = null; // the drag needs the natural size; without it, no drag
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
  // The drawn size of the picture before the zoom slider: `cover` on the stage.
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

function setWallZoom(scale) {
  editWall({ scale: clampScale(scale) });
}

function resetWallFrame() {
  editWall({ fx: 0.5, fy: 0.5, scale: 1 });
  syncWallZoom();
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

/* One hue for the whole window, taken from the picture itself.

   A plain average of the pixels is always grey — a photo has as much red as
   blue and the mean lands in the middle. So each pixel votes weighted by how
   saturated and how mid-toned it is, and the votes are averaged as angles,
   which is the only correct way to average a hue (350° and 10° are 20° apart,
   not 340°). A grey picture returns null and the default blue-grey stays. */
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
  // One decimal is all a palette needs, and `20.000000000000284` in storage
  // reads like a bug the next time somebody opens the dev tools.
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

/* --------------------------------------------------------------- personal */

/* Pins, saved searches and the dictionary are the three things a person brings
   with them from another search app, so they get the prime screen: the empty
   screen is where you decide whether an app is worth keeping. They live in the
   same database as the index, which is what makes `autofind import kensaku` a
   copy rather than a retyping.

   The list is fetched once and cached. Every mutation refetches it, because a
   star that stays lit while the idle screen still shows the old list is the
   kind of half-truth that makes people distrust the whole app. */
let personal = null;

async function loadPersonal(force = false) {
  if (personal && !force) return personal;
  try {
    personal = await api("/api/personal");
  } catch (error) {
    personal = null; // the personal layer must never break the screen
  }
  return personal;
}

function paintPersonal() {
  const host = $("personal");
  if (!host || !personal) return;
  const pins = personal.pins || [];
  const saved = personal.saved || [];
  // An empty block is not hidden: the empty screen is the one place where a
  // feature nobody has used yet can explain itself in a line, and "nothing
  // pinned yet" is how a star on a result becomes a thing people know about.
  const blocks = [
    `<div class="personal-block">
        <h3>${esc(t("idle.pinned"))}</h3>
        ${pins.length
          ? `<div class="pin-list">${pins.map(pinRow).join("")}</div>`
          : `<p class="personal-empty">${esc(t("idle.pinsEmpty"))}</p>`}
      </div>`,
    `<div class="personal-block">
        <h3>${esc(t("idle.saved"))}</h3>
        ${saved.length
          ? `<div class="saved-list">${saved.map(savedChip).join("")}</div>`
          : `<p class="personal-empty">${esc(t("idle.savedEmpty"))}</p>`}
      </div>`,
  ];
  host.innerHTML = blocks.join("");
  host.hidden = false;
}

function pinRow(item) {
  const icon = item.is_image && item.thumb
    ? `<img class="pin-thumb" src="${item.thumb}" alt="" loading="lazy">`
    : `<span class="pin-kind k-${item.kind}">${esc(KIND_LABEL[item.kind] || item.kind)}</span>`;
  // Titles on markup that is built after `applyStaticText` ran are written from
  // `t()` directly: a `data-i18n-title` here would never be read, and the button
  // would end up with no tooltip at all.
  return `<div class="pin-row">
      <button class="pin-star on" type="button" data-pin="${item.file_id}"
              aria-pressed="true" title="${esc(t("card.unpin"))}">★</button>
      ${icon}
      <button class="pin-name" type="button" data-open="${item.file_id}">${esc(item.name)}</button>
      <span class="pin-path" title="${esc(item.path)}">${esc(item.path)}</span>
    </div>`;
}

function savedChip(item) {
  return `<span class="saved-chip">
      <button class="saved-run" type="button" data-run="${esc(item.query)}">${esc(item.label)}</button>
      <button class="saved-x" type="button" data-forget="${item.id}"
              title="${esc(t("saved.forget"))}" aria-label="${esc(t("saved.forget"))}">×</button>
    </span>`;
}

/* One star, three places it can appear: a result card, a pinned row on the
   empty screen, and a second window. They all go through here so they cannot
   disagree. */
async function togglePin(fileId, wanted) {
  const id = Number(fileId);
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
    });
    await loadPersonal(true);
    paintPersonal();
    toast(t(data.pinned ? "pin.added" : "pin.removed"));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

async function saveSearch() {
  const query = $("q").value.trim();
  if (!query) return;
  try {
    await api("/api/saved", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    await loadPersonal(true);
    paintPersonal();
    toast(t("saved.done", { query }));
  } catch (error) {
    toast(t("saved.failed", { error: error.message }));
  }
}

async function forgetSearch(savedId) {
  try {
    await api("/api/saved/drop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: Number(savedId) }),
    });
    await loadPersonal(true);
    paintPersonal();
    toast(t("saved.forgotten"));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

function renderAliases() {
  const host = $("alias-list");
  if (!host) return;
  const rows = (personal && personal.aliases) || [];
  host.innerHTML = rows.length
    ? rows.map((item) => `<div class="alias-row">
        <b>${esc(item.alias)}</b>
        <span class="alias-arrow">→</span>
        <span class="alias-means">${esc(item.expand)}</span>
        <button class="alias-x" type="button" data-unalias="${esc(item.alias)}"
                title="${esc(t("saved.forget"))}" aria-label="${esc(t("saved.forget"))}">×</button>
      </div>`).join("")
    : `<p class="settings-note" id="alias-empty">${esc(t("alias.empty"))}</p>`;
}

async function addAlias() {
  const alias = $("alias-word").value.trim();
  const expand = $("alias-means").value.trim();
  if (!alias || !expand) { toast(t("alias.need")); return; }
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
    toast(t("alias.added", { alias, expand }));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

async function removeAlias(alias) {
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

/* The idle screen used to be one small plate on a very large photo, which read
   as a half-empty page. Six recent screenshots give the space a floor: the eye
   lands on something real and the whole column stops looking unfinished.

   The list is cached, not the rendering: switching language redraws the idle
   screen, and refetching six thumbnails to change one word would be silly. */
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
    recentImages = null; // a decoration must never break the idle screen
  }
}

function paintRecent(host) {
  if (!recentImages || !recentImages.length) return;
  host.innerHTML = `<h3>${esc(t("idle.recent"))}</h3>
    <div class="grid">${recentImages.map(shot).join("")}</div>`;
  host.hidden = false;
}

function card(hit, query, index) {
  const label = KIND_LABEL[hit.kind] || hit.kind;
  const icon = hit.is_image && hit.thumb
    ? `<div class="icon k-image"><img src="${hit.thumb}" alt="" loading="lazy"></div>`
    : `<div class="icon k-${hit.kind}">${esc(label)}</div>`;
  const badges = hit.channels
    .map((channel) => `<span class="badge b-${channel}">${channel}</span>`)
    .join("");
  const similar = hit.is_image
    ? `<button class="act" data-similar="${hit.file_id}">${esc(t("card.similar"))}</button>` : "";
  // A group is a fact about the result, not decoration: "3 versions" tells you
  // the row you are looking at is the one the ranker picked out of three files.
  const reason = hit.group_reason === "burst" ? "snapshot"
    : hit.group_reason === "copy" ? "copy" : "version";
  const group = hit.group_size > 1
    ? `<button class="badge b-group" data-group="${hit.file_id}"
         title="${esc(t("card.showAll", { n: hit.group_size }))}">${
         hit.group_size} ${esc(plural(hit.group_size, `group.${reason}`))
       }</button>`
    : "";
  return `
    <div class="card" data-index="${index}">
      ${icon}
      <div>
        <div class="title-row">
          <a class="name" data-open="${hit.file_id}">${esc(hit.name)}</a>
          <span class="badge">${esc(hit.kind)}</span>
          ${badges}
          ${group}
          ${hit.symbol ? `<span class="badge b-def" title="${esc(t("card.symbol"))}">${esc(hit.symbol)}</span>` : ""}
          <!-- A pin is the cheapest promise an app can make: "this will still be
               here tomorrow". It sits with the name, not in the action row, so
               it reads as a property of the file rather than a command. -->
          <button class="pin-star${hit.pinned ? " on" : ""}" type="button"
                  data-pin="${hit.file_id}" aria-pressed="${hit.pinned ? "true" : "false"}"
                  title="${esc(t(hit.pinned ? "card.unpin" : "card.pin"))}">★</button>
        </div>
        <div class="path" title="${esc(hit.path)}">${esc(hit.path)}</div>
        <div class="snippet">${highlight(hit.snippet, query)}</div>
        <div class="actions">
          <button class="act" data-open="${hit.file_id}">${esc(t("card.open"))}</button>
          <button class="act" data-reveal="${hit.file_id}">${esc(t("card.folder"))}</button>
          <button class="act" data-preview="${hit.file_id}">${esc(t("card.preview"))}</button>
          ${similar}
        </div>
      </div>
    </div>`;
}

/* Expanding a group is a lazy fetch, not a server-side expansion: the collapsed
   row already carries the member ids, and asking for one preview each keeps the
   search response small on a query that groups twenty screenshots. */
async function toggleGroup(button) {
  const card = button.closest(".card");
  const existing = card.querySelector(".group-list");
  if (existing) { existing.remove(); button.classList.remove("open"); return; }

  const hit = state.hits[Number(card.dataset.index)];
  if (!hit || !hit.group_members || !hit.group_members.length) return;
  button.classList.add("open");

  const rows = await Promise.all(
    hit.group_members.slice(0, 12).map((id) =>
      api(`/api/preview?id=${id}`).catch(() => null)
    )
  );
  // For a copy group the folder is the only thing that tells the rows apart,
  // so it belongs in the row; for versions the folder is identical by
  // definition and printing it four times is noise.
  const loaded = rows.filter(Boolean);
  const showWhere = hit.group_reason === "copy";
  const folders = showWhere
    ? relativeFolders([hit.path, ...loaded.map((row) => row.path)]) : [];
  const items = loaded.map((row, index) => `
    <div class="group-item">
      <a data-open="${row.file_id}">${esc(row.name)}</a>
      <span class="group-meta">${
        showWhere && folders[index + 1] ? `${esc(folders[index + 1])} · ` : ""}${
        sizeLabel(row.size)} · ${dateLabel(row.mtime)}</span>
    </div>`).join("");
  card.insertAdjacentHTML("beforeend",
    `<div class="group-list">${items || `<div class="group-item">${esc(t("group.failed"))}</div>`}</div>`);
}

/* A chip arrives from the server as a kind plus a value, because the words
   around it are a property of the interface and not of the search engine. */
function chipLabel(chip) {
  let value = chip.value ?? "";
  if (chip.kind === "weekday") {
    const name = WEEKDAYS[Number(value)];
    if (name) value = t(`weekday.${name}`);
  } else if (chip.kind === "sort") {
    value = t(`sort.${value}`);
  } else if (chip.kind === "type") {
    const key = `typeVal.${value}`;
    const named = t(key);
    if (named !== key) value = named;
  }
  const key = `chip.${chip.kind}`;
  const rendered = t(key, { value });
  // An unknown kind must still say something true rather than print a key.
  return rendered === key ? (chip.label || value) : rendered;
}

/* The chips row is the contract of the natural-language compiler: every filter
   it inferred is shown with the words that produced it, and one click takes it
   back. Without this the query language would feel like a guess. */
function chipRow(chips) {
  if (!chips.length) return "";
  const items = chips.map((chip) => `
    <button class="ichip" data-chip="${esc(chip.source)}"
            title="${esc(t("chip.remove", { source: chip.source }))}">
      <span>${esc(chipLabel(chip))}</span><b>×</b>
    </button>`).join("");
  return `<div class="ichips">${items}</div>`;
}

/* Facet counts on the kind tabs: numbers over every candidate that survived
   the filters, not just the twelve on the page. A tab without matches fades —
   the same honesty the results list has. */
function paintFacets(facets) {
  const counts = facets || {};
  // Mirrors KIND_MAP.docs on the server: which internal kinds a tab covers.
  const DOCS_KINDS = ["text", "code", "pdf", "docx", "pptx", "xlsx", "epub", "odt", "rtf"];
  document.querySelectorAll(".menu .chip").forEach((chip) => {
    const kind = chip.dataset.kind;
    let count = 0;
    if (kind === "") {
      count = Object.values(counts).reduce((a, b) => a + b, 0);
    } else if (kind === "docs") {
      count = DOCS_KINDS.reduce((a, k) => a + (counts[k] || 0), 0);
    } else {
      count = counts[kind] || 0;
    }
    let badge = chip.querySelector(".count");
    if (!count) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement("b");
      badge.className = "count";
      chip.appendChild(badge);
    }
    badge.textContent = count > 999 ? "999+" : String(count);
  });
}

function renderResults(data, append) {
  const parts = [];
  if (!append) parts.push(chipRow(data.chips || []));
  if (data.hints && data.hints.length) {
    parts.push(`<div class="hints">${data.hints.map((hint) => {
      const key = `hint.${hint.kind}`;
      const text = t(key, { value: hint.value ?? "" });
      const shown = esc(text === key ? (hint.text || hint.kind) : text);
      // A spelling suggestion is an action, not a remark: click it and the
      // search reruns on the corrected word.
      const clickable = hint.kind === "did_you_mean" && hint.value
        ? `<button class="hint-link" data-suggest="${esc(hint.value)}">${shown}</button>`
        : `<span>${shown}</span>`;
      return clickable;
    }).join("")}</div>`);
  }
  paintFacets(data.facets);

  if (state.hits.length) {
    parts.push(`<div class="results">${
      state.hits.map((hit, index) => card(hit, data.query, index)).join("")
    }</div>`);
  }

  if (data.next_offset !== null && data.next_offset !== undefined) {
    parts.push(`<button class="more" id="more-btn">${esc(t("results.more"))}</button>`);
  }

  if (!append && data.images && data.images.length) {
    parts.push(`<div class="section-title">${esc(t("results.similarImages"))}</div>
      <div class="grid">${data.images.map(shot).join("")}</div>`);
  }
  if (!parts.length) {
    parts.push(`<div class="empty"><div>
        <h2>${esc(t("results.nothing"))}</h2>
        <p>${esc(t("results.nothingHint"))}</p>
      </div></div>`);
  }

  if (append) {
    // Appending must not rebuild the list: the browser would drop the scroll
    // position and re-request every thumbnail already on screen.
    $("content").insertAdjacentHTML("beforeend", parts.join(""));
  } else {
    $("content").innerHTML = parts.join("");
  }
  if (!append) state.selected = -1;
  paintSelection();
}

function paintSelection() {
  document.querySelectorAll(".card.sel").forEach((node) => node.classList.remove("sel"));
  const node = document.querySelector(`.card[data-index="${state.selected}"]`);
  if (node) {
    node.classList.add("sel");
    node.scrollIntoView({ block: "nearest" });
  }
}

function moveSelection(delta) {
  if (!state.hits.length) return;
  const next = state.selected + delta;
  state.selected = Math.max(0, Math.min(state.hits.length - 1, next));
  paintSelection();
}

/* Quick Look: images get the thumbnail, everything else the indexed text, so a
   PDF previews without being parsed again. */
async function openPreview(fileId) {
  try {
    const data = await api(`/api/preview?id=${fileId}`);
    const meta = [
      sizeLabel(data.size),
      dateLabel(data.mtime),
      data.opens ? t("preview.opened", { n: data.opens }) : t("preview.never"),
      data.exists ? "" : t("preview.missing"),
    ].filter(Boolean).join(" · ");
    const body = data.image
      ? `<img class="preview-img" src="${data.image.thumb}" alt="">
         ${data.image.ocr ? `<pre class="preview-text">${esc(data.image.ocr)}</pre>` : ""}`
      : `<pre class="preview-text">${esc(data.text || t("preview.noText"))}</pre>`;
    $("preview-body").innerHTML = `
      <div class="preview-head">
        <div>
          <div class="preview-name">${esc(data.name)}</div>
          <div class="preview-path">${esc(data.path)}</div>
          <div class="preview-meta">${esc(meta)}</div>
        </div>
      </div>
      ${body}
      <div class="preview-foot">
        <button class="act" data-open="${data.file_id}">${esc(t("card.open"))}</button>
        <button class="act" data-reveal="${data.file_id}">${esc(t("card.folder"))}</button>
      </div>`;
    $("preview").classList.add("open");
  } catch (error) {
    toast(t("preview.failed", { error: error.message }));
  }
}

function shot(image) {
  return `<div class="shot" data-similar="${image.file_id}" title="${esc(image.path)}">
      <img src="${image.thumb}" alt="" loading="lazy">
      <div class="cap">${esc(image.name)}</div>
    </div>`;
}

/* -------------------------------------------------------------- side panel */

async function loadStats() {
  try {
    const [stats, status, privacy, watch] = await Promise.all([
      api("/api/stats"), api("/api/status"), api("/api/privacy"), api("/api/watch"),
    ]);
    state.stats = stats;
    state.status = status;
    $("s-files").textContent = stats.files ?? 0;
    $("s-chunks").textContent = stats.chunks ?? 0;
    $("s-images").textContent = stats.images ?? 0;
    $("s-ocr").textContent = stats.ocr_images ?? 0;
    $("s-size").textContent = `${((stats.db_bytes || 0) / 1048576).toFixed(1)} ${t("unit.mb")}`;
    // The definition count is the only place the app says out loud that it
    // indexes symbols, so it is worth a stat of its own.
    loadPersonal().then((data) => {
      if (data && $("s-symbols")) $("s-symbols").textContent = data.symbols ?? 0;
    });
    $("status-text").textContent = stats.files
      ? plural(stats.files, "status.files")
      : t("status.empty");
    $("status-dot").style.background = stats.files ? "var(--teal)" : "var(--accent)";

    renderPrivacy(privacy);
    renderWatch(watch);

    const engines = status.ocr?.available || [];
    const clip = status.visual_supports_text
      ? "" : ` <span class="hint" title="${esc(t("panel.noClipHint"))}">${esc(t("panel.noClip"))}</span>`;
    $("engines").innerHTML = `
      <div>${esc(t("panel.text"))} <span>${esc(status.text_embedder)}</span> · ${
        esc(t("panel.images"))} <span>${esc(status.visual_embedder)}</span>${clip}</div>
      <div>OCR <span>${engines.length ? esc(engines.join(", ")) : esc(t("panel.notFound"))}</span></div>`;
    if (!$("roots").value.trim() && status.default_roots?.length) {
      $("roots").value = status.default_roots.join("\n");
    }
    if ($("settings").classList.contains("open")) renderAbout();
  } catch (error) {
    $("status-text").textContent = t("status.down");
    $("status-dot").style.background = "var(--red, #e24b4a)";
  }
}

/* The privacy guard is a set of switches, not one bit: SSH keys and browser
   sessions are different risks, and the panel lets each group be turned off
   on its own. The list comes from the server so a new group needs no HTML. */
const PRIVACY_KEY = "privacy.";

function privacyLabel(id) {
  return t(PRIVACY_KEY + id);
}

function renderPrivacy(privacy) {
  const catalog = privacy.categories || [];
  const host = $("privacy-filters-list");
  const signature = catalog.map((item) => item.id).join(",");
  if (host.dataset.signature !== signature) {
    host.innerHTML = catalog.map((item, index) => `
      <label class="privacy-filter" style="--i:${index}">
        <input type="checkbox" data-privacy="${esc(item.id)}">
        <span>${esc(privacyLabel(item.id))}</span>
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

function renderWatch(watch) {
  const on = Boolean(watch && watch.running);
  $("watch-btn").classList.toggle("primary", on);
  $("watch-label").textContent = on
    ? t("panel.watchOn", {
      added: watch.added, removed: watch.removed, batch: watch.last_batch,
    })
    : (watch && watch.last_error
      ? t("panel.watchError", { error: watch.last_error })
      : t("panel.watchingOff"));
}

async function toggleWatch() {
  const on = $("watch-btn").classList.contains("primary");
  const roots = $("roots").value.split("\n").map((line) => line.trim()).filter(Boolean);
  try {
    const watch = await api("/api/watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !on, roots }),
    });
    renderWatch(watch);
    toast(watch.running ? t("toast.watchOn") : t("toast.watchOff"));
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

async function purgePrivate() {
  try {
    const result = await api("/api/privacy/purge", { method: "POST", body: "{}" });
    toast(t("toast.purged", { n: result.removed }));
    loadStats();
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
}

function startJob() {
  const roots = $("roots").value.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!roots.length) { toast(t("toast.needRoots")); return; }
  api("/api/index", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ roots, prune: $("prune").checked }),
  }).then(() => {
    $("index-btn").disabled = true;
    pollJob();
  }).catch((error) => toast(error.message));
}

function pollJob() {
  clearTimeout(state.jobTimer);
  api("/api/progress").then((job) => {
    const done = job.total ? Math.round((job.position / job.total) * 100) : 0;
    $("bar").style.width = `${job.running ? done : 100}%`;
    $("progress-label").textContent = job.running
      ? `${job.position}/${job.total} · ${shortPath(job.path)}`
      : (job.result
        ? t("toast.jobDone", {
          seconds: job.result.seconds,
          files: plural(job.result.indexed, "unit.files"),
        })
        : (job.error || t("panel.ready")));
    if (job.running) {
      state.jobTimer = setTimeout(pollJob, 400);
    } else {
      $("index-btn").disabled = false;
      loadStats();
      if (job.result && job.result.indexed) runSearch();
    }
  }).catch(() => { $("index-btn").disabled = false; });
}

function shortPath(path) {
  if (!path) return "";
  const parts = path.split(/[\\/]/);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

/* Two copies of one file differ at the *front* of the path, which is exactly
   what shortPath throws away. Strip what they share and show what is left:
   "openWallpaper\bin\steamcmd\public" against "zz\bin\steamcmd\public". */
function relativeFolders(paths) {
  const split = paths.map((path) => String(path).split(/[\\/]/));
  let depth = 0;
  while (split.every((parts) => parts.length > depth + 1)) {
    const segment = split[0][depth].toLowerCase();
    if (!split.every((parts) => parts[depth].toLowerCase() === segment)) break;
    depth += 1;
  }
  return split.map((parts) => parts.slice(depth, -1).join("\\"));
}

/* ------------------------------------------------------------------ modal */

async function openSimilar(fileId) {
  try {
    const data = await api(`/api/similar?id=${fileId}`);
    $("modal-grid").innerHTML = data.images.length
      ? data.images.map(shot).join("")
      : `<div class="empty" style="min-height:120px"><p>${esc(t("results.nothing"))}</p></div>`;
    $("modal").classList.add("open");
  } catch (error) {
    toast(error.message);
  }
}

/* --------------------------------------------------------- drawer + settings */

/* The index panel is a drawer, not a column. Two consequences worth naming:
   it slides over the results instead of reflowing them, and the scrim behind it
   is the close button for people who do not aim at small targets. */
function setPanel(open) {
  const drawer = $("panel");
  $("panel").classList.toggle("open", open);
  $("scrim").classList.toggle("show", open);
  drawer.setAttribute("aria-hidden", String(!open));
  $("panel-btn").setAttribute("aria-expanded", String(open));
}

function setSettings(open) {
  $("settings").classList.toggle("open", open);
  if (open) {
    renderAbout();
    paintLang();
    loadPersonal().then(renderAliases);
    renderWalls();
    // Focus the card, not the close button: a ring on "close" is the first
    // thing the eye lands on, and it is not what the dialog is about.
    $("settings").querySelector(".settings-card").focus();
  }
}

function paintLang() {
  document.querySelectorAll("#lang-switch button").forEach((button) => {
    button.setAttribute("aria-checked", String(button.dataset.lang === currentLang()));
  });
}

function renderAbout() {
  const host = $("about");
  if (!host) return;
  const stats = state.stats || {};
  const status = state.status || {};
  const ocr = status.ocr || {};
  const rows = [
    ["settings.aboutFiles",
      `${plural(stats.files || 0, "unit.files")} · ${plural(stats.chunks || 0, "unit.chunks")}`],
    ["settings.aboutSize",
      `${((stats.db_bytes || 0) / 1048576).toFixed(1)} ${t("unit.mb")}`],
    ["settings.aboutLanguages", String(ocr.supported_languages ?? "—")],
    ["settings.aboutEmbedder", status.text_embedder || "—"],
    ["settings.aboutImages", status.visual_embedder || "—"],
    ["settings.aboutOcr", (ocr.available || []).join(", ") || t("panel.notFound")],
    ["settings.aboutVersion", status.version || "—"],
  ];
  host.innerHTML = rows
    .map(([key, value]) => `<dt>${esc(t(key))}</dt><dd>${esc(value)}</dd>`)
    .join("");
}

/* ----------------------------------------------------------------- events */

document.addEventListener("click", async (event) => {
  const chip = event.target.closest("[data-chip]");
  if (chip) { clearChip(chip.dataset.chip); return; }

  if (event.target.closest("#more-btn")) { runSearch({ append: true }); return; }

  const star = event.target.closest("[data-pin]");
  if (star) {
    event.preventDefault();
    event.stopPropagation();
    // A star knows its own state, so the click says what it wants instead of
    // asking the server to guess: two windows must not flip each other twice.
    togglePin(star.dataset.pin, !star.classList.contains("on"));
    return;
  }

  const run = event.target.closest("[data-run]");
  if (run) {
    $("q").value = run.dataset.run;
    clearTimeout(state.timer);
    runSearch();
    $("q").focus();
    return;
  }

  const forget = event.target.closest("[data-forget]");
  if (forget) { forgetSearch(forget.dataset.forget); return; }

  const unalias = event.target.closest("[data-unalias]");
  if (unalias) { removeAlias(unalias.dataset.unalias); return; }

  const suggest = event.target.closest("[data-suggest]");
  if (suggest) {
    $("q").value = suggest.dataset.suggest;
    runSearch();
    $("q").focus();
    return;
  }

  const group = event.target.closest("[data-group]");
  if (group) { toggleGroup(group); return; }

  const preview = event.target.closest("[data-preview]");
  if (preview) { openPreview(Number(preview.dataset.preview)); return; }

  const target = event.target.closest("[data-open],[data-reveal],[data-similar]");
  if (!target) {
    // Clicking anywhere on a card selects it, so the keyboard continues from
    // the row the user just pointed at.
    const card = event.target.closest(".card");
    if (card) { state.selected = Number(card.dataset.index); paintSelection(); }
    return;
  }
  if (target.dataset.similar) { openSimilar(target.dataset.similar); return; }
  const id = target.dataset.open || target.dataset.reveal;
  const route = target.dataset.open ? "open" : "reveal";
  try {
    await api(`/api/${route}?id=${id}`);
    $("preview").classList.remove("open");
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  }
});

document.querySelectorAll(".menu .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".menu .chip").forEach((item) => item.classList.remove("active"));
    chip.classList.add("active");
    state.kind = chip.dataset.kind;
    if (state.query) runSearch();
  });
});

/* ------------------------------------------------------------ suggestions */

/* Recent queries live in localStorage, saved searches in the personal layer;
   both are closer than retyping. The dropdown opens under the box while you
   type, follows the typed text, and ↑↓ + Enter run a row without the mouse. */
const RECENT_KEY = "af.recent-queries";
const SUGGEST = { items: [], index: -1 };

function recentQueries() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(list) ? list.filter((item) => typeof item === "string") : [];
  } catch {
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
  } catch {
    /* private mode loses the memory, nothing else breaks */
  }
}

function suggestItems(text) {
  const needle = text.trim().toLowerCase();
  const saved = ((personal && personal.saved) || [])
    .map((item) => ({ label: item.query, kind: "saved" }));
  const recent = recentQueries().map((label) => ({ label, kind: "recent" }));
  const seen = new Set();
  const rows = [];
  for (const item of [...saved, ...recent]) {
    if (!item.label || seen.has(item.label)) continue;
    if (needle && !item.label.toLowerCase().includes(needle)) continue;
    seen.add(item.label);
    rows.push(item);
  }
  return rows.slice(0, 8);
}

function paintSuggest() {
  $("suggest").innerHTML = SUGGEST.items.map((item, index) => `
    <button type="button" class="suggest-item${index === SUGGEST.index ? " active" : ""}"
            data-suggest="${index}">
      <span class="suggest-kind">${esc(t(item.kind === "saved" ? "suggest.saved" : "suggest.recent"))}</span>
      <span class="suggest-label">${esc(item.label)}</span>
    </button>`).join("");
}

function openSuggest() {
  SUGGEST.items = suggestItems($("q").value);
  SUGGEST.index = -1;
  if (!SUGGEST.items.length) { closeSuggest(); return; }
  paintSuggest();
  $("suggest").hidden = false;
}

function closeSuggest() {
  SUGGEST.items = [];
  SUGGEST.index = -1;
  $("suggest").hidden = true;
}

function moveSuggest(delta) {
  if (!SUGGEST.items.length) return;
  const next = SUGGEST.index + delta;
  SUGGEST.index = next >= SUGGEST.items.length ? 0
    : next < 0 ? SUGGEST.items.length - 1 : next;
  paintSuggest();
}

function acceptSuggest() {
  const item = SUGGEST.items[SUGGEST.index];
  if (!item) return false;
  $("q").value = item.label;
  closeSuggest();
  clearTimeout(state.timer);
  runSearch();
  return true;
}

$("q").addEventListener("input", () => {
  scheduleSearch();
  if (personal) openSuggest();
  else loadPersonal().then(() => { if (!$("suggest").hidden || $("q").value) openSuggest(); });
});

$("q").addEventListener("keydown", (event) => {
  const suggestOpen = !$("suggest").hidden;
  if (event.key === "Enter") {
    clearTimeout(state.timer);
    if (!(suggestOpen && SUGGEST.index >= 0 && acceptSuggest())) runSearch();
    closeSuggest();
  } else if (event.key === "Escape") {
    if (suggestOpen) closeSuggest();
    else { $("q").value = ""; runSearch(); }
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    if (!suggestOpen) openSuggest();
    moveSuggest(1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    moveSuggest(-1);
  }
});

$("suggest").addEventListener("mousedown", (event) => {
  event.preventDefault(); // keep the input focused; click runs the query
  const button = event.target.closest("[data-suggest]");
  if (!button) return;
  SUGGEST.index = Number(button.dataset.suggest);
  acceptSuggest();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".find")) closeSuggest();
});
$("sort").addEventListener("change", () => {
  state.sort = $("sort").value;
  if (state.query) runSearch();
});
$("folder").addEventListener("change", () => { state.folder = $("folder").value.trim(); if (state.query) runSearch(); });
$("since-btn").addEventListener("click", (event) => {
  event.stopPropagation();
  openCalendar(!CAL.open);
});
$("calendar").addEventListener("click", (event) => {
  event.stopPropagation();
  const action = event.target.closest("[data-cal]");
  if (action) {
    const name = action.dataset.cal;
    if (name === "clear") { setSince(""); return; }
    if (name === "docdate") {
      state.docdate = !state.docdate;
      $("doc-date-toggle").classList.toggle("set", state.docdate);
      if (state.query) runSearch();
      return;
    }
    if (name === "today") { setSince(isoDate(new Date())); return; }
    const step = name === "prev" ? -1 : 1;
    const next = new Date(CAL.year, CAL.month + step, 1);
    CAL.year = next.getFullYear();
    CAL.month = next.getMonth();
    renderCalendar();
    return;
  }
  const day = event.target.closest("[data-day]");
  if (day) setSince(day.dataset.day);
});
/* Right-click on a result: open, show in folder, copy path — the three
   things kensaku's context menu does, drawn as our own card so it follows the
   theme instead of the browser's. */
let ctxMenu = null;

document.addEventListener("contextmenu", (event) => {
  const card = event.target.closest(".card");
  if (!card || !state.hits[Number(card.dataset.index)]) return;
  event.preventDefault();
  closeCtxMenu();
  const hit = state.hits[Number(card.dataset.index)];
  ctxMenu = document.createElement("div");
  ctxMenu.className = "ctx-menu";
  ctxMenu.innerHTML = `
    <button data-act="open">${esc(t("ctx.open"))}</button>
    <button data-act="reveal">${esc(t("ctx.reveal"))}</button>
    <button data-act="copy">${esc(t("ctx.copy"))}</button>`;
  ctxMenu.style.left = `${Math.min(event.clientX, window.innerWidth - 190)}px`;
  ctxMenu.style.top = `${Math.min(event.clientY, window.innerHeight - 120)}px`;
  document.body.appendChild(ctxMenu);
  ctxMenu.addEventListener("click", async (menuEvent) => {
    const button = menuEvent.target.closest("[data-act]");
    if (!button) return;
    closeCtxMenu();
    if (button.dataset.act === "copy") {
      try { await navigator.clipboard.writeText(hit.path); toast(t("ctx.copied")); }
      catch { toast(t("search.failed", { error: "clipboard" })); }
      return;
    }
    api(`/api/${button.dataset.act === "open" ? "open" : "reveal"}?id=${hit.file_id}`)
      .catch((error) => toast(t("toast.failed", { error: error.message })));
  });
});

function closeCtxMenu() {
  if (ctxMenu) { ctxMenu.remove(); ctxMenu = null; }
}

document.addEventListener("click", (event) => {
  if (ctxMenu && !event.target.closest(".ctx-menu")) closeCtxMenu();
});
document.addEventListener("scroll", closeCtxMenu, true);

// A click anywhere else closes the popup, the way a menu behaves.
document.addEventListener("click", (event) => {
  if (!CAL.open) return;
  if (event.target.closest("#calendar") || event.target.closest("#since-btn")) return;
  openCalendar(false);
});
$("clear").addEventListener("click", () => {
  $("q").value = "";
  clearTimeout(state.timer);
  runSearch();
  $("q").focus();
});
$("save-btn").addEventListener("click", saveSearch);
$("alias-add").addEventListener("click", addAlias);
$("wall-row").addEventListener("click", (event) => {
  const tile = event.target.closest("[data-wall]");
  if (tile) { chooseWall(tile.dataset.wall); return; }
  if (event.target.closest("#wall-pick")) $("wall-file").click();
});
$("wall-file").addEventListener("change", () => {
  const file = $("wall-file").files && $("wall-file").files[0];
  // Reset first: choosing the same file twice must upload it twice, and a file
  // input that keeps its value fires no change event the second time.
  $("wall-file").value = "";
  useWallFile(file);
});
$("wall-edit").addEventListener("click", openWallEditor);
$("wall-editor-close").addEventListener("click", closeWallEditor);
$("wall-editor").addEventListener("click", (event) => {
  if (event.target.id === "wall-editor") closeWallEditor();
});
$("wall-editor-reset").addEventListener("click", resetWallFrame);
$("wall-zoom").addEventListener("input", () => setWallZoom(Number($("wall-zoom").value)));

/* Drag-to-pan: the picture follows the pointer, clamped so it always covers the
   stage. Pointer capture keeps the drag alive when the pointer leaves the
   preview, which is where a mouse-based crop usually falls apart. */
(() => {
  const stage = $("wall-stage");
  let drag = null;

  stage.addEventListener("pointerdown", (event) => {
    if (!editor.open || !editor.image || event.button !== 0) return;
    const cover = stageCover(stage, editor.image);
    const slackX = cover.width * clampScale(theme.scale) - stage.clientWidth;
    const slackY = cover.height * clampScale(theme.scale) - stage.clientHeight;
    drag = { x: event.clientX, y: event.clientY, fx: clamp01(theme.fx ?? 0.5), fy: clamp01(theme.fy ?? 0.5), slackX, slackY };
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener("pointermove", (event) => {
    if (!drag) return;
    setWallFocus(
      drag.slackX > 0 ? drag.fx - (event.clientX - drag.x) / drag.slackX : drag.fx,
      drag.slackY > 0 ? drag.fy - (event.clientY - drag.y) / drag.slackY : drag.fy,
    );
  });
  const stop = (event) => {
    if (!drag) return;
    drag = null;
    if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
  };
  stage.addEventListener("pointerup", stop);
  stage.addEventListener("pointercancel", stop);

  // The wheel zooms around the focus point, which by construction never moves
  // on screen — the subject stays put while the world grows around it.
  stage.addEventListener("wheel", (event) => {
    if (!editor.open || !editor.image) return;
    event.preventDefault();
    const current = clampScale(theme.scale);
    const next = event.deltaY < 0 ? current * 1.08 : current / 1.08;
    setWallZoom(next);
    syncWallZoom();
  }, { passive: false });
})();
// Enter in either dictionary field adds the entry: a two-field form with no
// keyboard path is a form people fill in with the mouse or not at all.
["alias-word", "alias-means"].forEach((id) => {
  $(id).addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addAlias(); }
  });
});
$("index-btn").addEventListener("click", startJob);
$("watch-btn").addEventListener("click", toggleWatch);

/* Duplicate screenshots: the report opens in its own right-side drawer with
   every copy listed by name and full path. Each row reuses the same reveal
   handler the result cards have — click shows the file in Explorer. A checkbox
   on every row marks a copy for deletion; the bin in the header deletes the
   checked files after one confirmation. */
function setDups(open) {
  $("dups-drawer").classList.toggle("open", open);
  $("dups-drawer").setAttribute("aria-hidden", String(!open));
  $("scrim").classList.toggle("show", open);
}

function basename(path) {
  return path.split("\\").pop().split("/").pop();
}

function renderDuplicates(groups) {
  $("dups-body").innerHTML = groups.length
    ? groups.map((group) => `
        <div class="dup-group">
          <b class="dup-count">${t("panel.copies", { n: group.items.length })}</b>
          ${group.items.map((item) => `
            <div class="dup-item">
              <button type="button" class="dup-reveal" data-reveal="${item.file_id}"
                      title="${esc(item.path)}">
                <span class="dup-name">${esc(basename(item.path))}</span>
                <span class="dup-full">${esc(item.path)}</span>
              </button>
              <input type="checkbox" class="dup-pick" value="${item.file_id}"
                     aria-label="${esc(t("dups.select", { name: basename(item.path) }))}">
            </div>`).join("")}
        </div>`).join("")
    : `<p class="panel-note">${esc(t("panel.noDuplicates"))}</p>`;
}

async function loadDuplicates() {
  const data = await api("/api/duplicates");
  renderDuplicates(data.groups || []);
  setDups(true);
}

function selectedDupIds() {
  return Array.from($("dups-body").querySelectorAll(".dup-pick:checked"))
    .map((node) => Number(node.value));
}

/* Ctrl + drag paints selection over the rows. The first row decides the mode:
   start on an unchecked row and the swiped rows become checked, start on a
   checked one and they are cleared. A checkbox clicked on its own still behaves
   like a normal checkbox. Near the top or bottom edge the report scrolls on its
   own, so a selection can continue past the visible rows. */
let dupPaint = null;
let dupPaintSwallowClick = false;

const DUP_SCROLL_EDGE = 46;
const DUP_SCROLL_MAX = 20;

function dupScroller() {
  const body = $("dups-body");
  const drawer = $("dups-drawer");
  if (body.scrollHeight > body.clientHeight + 1) return body;
  return drawer.scrollHeight > drawer.clientHeight + 1 ? drawer : body;
}

function dupPaintRowAt(x, y) {
  const node = document.elementFromPoint(x, y);
  const row = node && node.closest && node.closest(".dup-item");
  if (!row || !$("dups-body").contains(row)) return;
  const pick = row.querySelector(".dup-pick");
  if (pick) pick.checked = dupPaint.value;
}

function dupAutoScroll() {
  if (!dupPaint) return;
  const scroller = dupScroller();
  const rect = scroller.getBoundingClientRect();
  let delta = 0;
  if (dupPaint.y > rect.bottom - DUP_SCROLL_EDGE) {
    const depth = dupPaint.y - (rect.bottom - DUP_SCROLL_EDGE);
    delta = Math.min(DUP_SCROLL_MAX, Math.round(depth / 2) + 4);
  } else if (dupPaint.y < rect.top + DUP_SCROLL_EDGE) {
    const depth = (rect.top + DUP_SCROLL_EDGE) - dupPaint.y;
    delta = -Math.min(DUP_SCROLL_MAX, Math.round(depth / 2) + 4);
  }
  if (delta) {
    const before = scroller.scrollTop;
    scroller.scrollTop += delta;
    if (scroller.scrollTop !== before) {
      const x = Math.min(Math.max(dupPaint.x, rect.left + 2), rect.right - 2);
      const y = Math.min(Math.max(dupPaint.y, rect.top + 2), rect.bottom - 2);
      dupPaintRowAt(x, y);
    }
  }
  dupPaint.raf = requestAnimationFrame(dupAutoScroll);
}

$("dups-body").addEventListener("pointerdown", (event) => {
  if (!event.ctrlKey || event.button !== 0) return;
  if (event.target.closest(".dup-pick")) return;
  const row = event.target.closest(".dup-item");
  if (!row) return;
  const pick = row.querySelector(".dup-pick");
  if (!pick) return;
  dupPaint = { value: !pick.checked, x: event.clientX, y: event.clientY, raf: 0 };
  pick.checked = dupPaint.value;
  dupPaintSwallowClick = true;
  event.preventDefault();
  dupPaint.raf = requestAnimationFrame(dupAutoScroll);
});

window.addEventListener("pointermove", (event) => {
  if (!dupPaint) return;
  dupPaint.x = event.clientX;
  dupPaint.y = event.clientY;
  dupPaintRowAt(event.clientX, event.clientY);
});

function stopDupPaint() {
  if (dupPaint) cancelAnimationFrame(dupPaint.raf);
  dupPaint = null;
  if (dupPaintSwallowClick) {
    setTimeout(() => { dupPaintSwallowClick = false; }, 0);
  }
}

window.addEventListener("pointerup", stopDupPaint);
window.addEventListener("pointercancel", stopDupPaint);

/* The list is the only scroller in the drawer; the wheel works over the header
   and the empty space too, not just over a row. */
$("dups-drawer").addEventListener("wheel", (event) => {
  const body = $("dups-body");
  if (body.scrollHeight <= body.clientHeight + 1) return;
  const step = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
  body.scrollTop += step;
  event.preventDefault();
}, { passive: false });

$("dups-body").addEventListener("click", (event) => {
  if (!dupPaintSwallowClick) return;
  event.preventDefault();
  event.stopPropagation();
}, true);

let pendingDelete = [];

function setConfirm(open) {
  pendingDelete = open ? selectedDupIds() : [];
  $("confirm").classList.toggle("open", open);
  $("confirm").setAttribute("aria-hidden", String(!open));
}

function closeConfirm() {
  setConfirm(false);
  $("confirm-yes").disabled = false;
}

$("dups-btn").addEventListener("click", async () => {
  const button = $("dups-btn");
  button.disabled = true;
  try {
    await loadDuplicates();
  } catch (error) {
    toast(t("toast.failed", { error: error.message }));
  } finally {
    button.disabled = false;
  }
});

$("dups-delete").addEventListener("click", () => {
  const ids = selectedDupIds();
  if (!ids.length) { toast(t("dups.nothingSelected")); return; }
  $("confirm-text").textContent = t("confirm.deleteText", { n: ids.length });
  setConfirm(true);
});

$("confirm-no").addEventListener("click", closeConfirm);
$("confirm").addEventListener("click", (event) => {
  if (event.target.id === "confirm") closeConfirm();
});

$("confirm-yes").addEventListener("click", async () => {
  const ids = pendingDelete.slice();
  if (!ids.length) { closeConfirm(); return; }
  const button = $("confirm-yes");
  button.disabled = true;
  try {
    const result = await api("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    closeConfirm();
    toast(t("toast.deleted", { n: result.deleted }));
    await loadDuplicates();
  } catch (error) {
    button.disabled = false;
    toast(t("toast.failed", { error: error.message }));
  }
});

$("dups-close").addEventListener("click", () => setDups(false));
$("purge-btn").addEventListener("click", purgePrivate);
$("privacy-toggle").addEventListener("click", () => {
  const open = $("privacy-filters").classList.toggle("open");
  $("privacy-filters").setAttribute("aria-hidden", String(!open));
  $("privacy-toggle").classList.toggle("open", open);
  $("privacy-toggle").setAttribute("aria-expanded", String(open));
});
$("privacy-enabled").addEventListener("change", () => {
  savePrivacy({ enabled: $("privacy-enabled").checked });
});
$("privacy-filters-list").addEventListener("change", () => {
  const categories = [...$("privacy-filters-list").querySelectorAll("[data-privacy]:checked")]
    .map((box) => box.dataset.privacy);
  savePrivacy({ categories });
});
$("modal-close").addEventListener("click", () => $("modal").classList.remove("open"));
$("modal").addEventListener("click", (event) => { if (event.target.id === "modal") $("modal").classList.remove("open"); });
$("preview-close").addEventListener("click", () => $("preview").classList.remove("open"));
$("preview").addEventListener("click", (event) => {
  if (event.target.id === "preview") $("preview").classList.remove("open");
});

$("panel-btn").addEventListener("click", () => {
  setPanel(!$("panel").classList.contains("open"));
});
$("panel-close").addEventListener("click", () => setPanel(false));
$("scrim").addEventListener("click", () => { setPanel(false); setDups(false); });

$("settings-btn").addEventListener("click", () => setSettings(true));
$("settings-close").addEventListener("click", () => setSettings(false));
$("settings").addEventListener("click", (event) => {
  if (event.target.id === "settings") setSettings(false);
});

/* The welcome screen is the only place a new user learns that the index and
   the language switch exist, so its controls have to work before the main app
   has anything to show. */
$("onboard-lang").addEventListener("click", (event) => {
  const button = event.target.closest("[data-lang]");
  if (!button) return;
  if (!setLang(button.dataset.lang)) { paintOnboarding(); return; }
  paintLang();
  paintOnboarding();
  paintSince();
  loadStats();
  if (state.query) runSearch(); else renderIdle();
});
$("ob-autostart").addEventListener("change", toggleAutostart);
$("ob-import").addEventListener("click", importKensaku);
$("ob-import-scan").addEventListener("click", scanKensaku);
$("ob-index").addEventListener("click", startOnboardingIndex);
$("onboard-done").addEventListener("click", finishOnboarding);
$("onboard-skip").addEventListener("click", finishOnboarding);
$("replay-onboarding").addEventListener("click", replayOnboarding);

/* The welcome card doubles as the title bar of its little frameless window:
   grab it anywhere calm with the left button and drag. pywebview moves the
   window for anything inside `.pywebview-drag-region`; the capture guard
   keeps a press on a real control a click instead of a drag. */
const onboardBox = $("onboard");
onboardBox.classList.add("pywebview-drag-region");
onboardBox.addEventListener(
  "mousedown",
  (event) => {
    if (event.button !== 0) return;
    if (event.target.closest("button, input, select, label, a, [role='switch'], [role='button']")) {
      event.stopPropagation();
    }
  },
  true,
);

/* Switching language redraws the screen from the same data: nothing is
   refetched except the strings, and the choice is remembered across launches. */
$("lang-switch").addEventListener("click", (event) => {
  const button = event.target.closest("[data-lang]");
  if (!button) return;
  if (!setLang(button.dataset.lang)) { paintLang(); return; }
  paintLang();
  renderAbout();
  renderAliases();
  paintSince();
  if (CAL.open) renderCalendar();
  loadStats();
  if (state.query) runSearch(); else renderIdle();
});

function selectedHit() {
  return state.selected >= 0 ? state.hits[state.selected] : null;
}

document.addEventListener("keydown", (event) => {
  const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);

  if (event.key === "/" && document.activeElement !== $("q")) {
    event.preventDefault();
    $("q").focus();
    $("q").select();
    return;
  }
  if (event.key === "Escape") {
    if ($("confirm").classList.contains("open")) { closeConfirm(); return; }
    if (CAL.open) { openCalendar(false); return; }
    if (editor.open) { closeWallEditor(); return; }
    if ($("preview").classList.contains("open")) { $("preview").classList.remove("open"); return; }
    if ($("settings").classList.contains("open")) { setSettings(false); return; }
    if ($("modal").classList.contains("open")) { $("modal").classList.remove("open"); return; }
    if ($("panel").classList.contains("open")) { setPanel(false); return; }
    if ($("dups-drawer").classList.contains("open")) { setDups(false); return; }
  }
  if (inField && document.activeElement === $("q")) return;

  const hit = selectedHit();
  if (!hit) return;

  if (event.key === "ArrowDown") { event.preventDefault(); moveSelection(1); }
  else if (event.key === "ArrowUp") { event.preventDefault(); moveSelection(-1); }
  else if (event.key === "Enter") {
    event.preventDefault();
    api(`/api/${event.shiftKey ? "reveal" : "open"}?id=${hit.file_id}`)
      .catch((error) => toast(t("toast.failed", { error: error.message })));
  } else if (event.key === " " || (event.key === "Spacebar")) {
    // Space is a space while the search box has focus; everywhere else it is
    // Quick Look, the same reflex as in Finder.
    event.preventDefault();
    openPreview(hit.file_id);
  }
});

/* The window has no title bar, so the header carries the only close button
   there is. pywebview injects its bridge after load; until then, and in a
   plain browser, the button stays hidden — there is nothing to hide. */
function wireWindowControls() {
  const markApp = () => document.body.classList.add("in-app");
  // pywebview dispatches this on window, after its bridge is injected.
  window.addEventListener("pywebviewready", markApp);
  document.addEventListener("pywebviewready", markApp);
  if (window.pywebview && window.pywebview.api) markApp();

  const wireDataWin = (box) => {
    if (!box) return;
    box.addEventListener("click", (event) => {
      const button = event.target.closest("[data-win]");
      if (!button) return;
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      event.preventDefault();
      if (button.dataset.win === "quit") api.quit();
      else api.hide();
    });
  };
  wireDataWin($("win-controls"));
  // The welcome card carries the same pair in its own corner: the grey bar
  // hides the little window, the grey cross leaves the app entirely.
  wireDataWin($("onboard-controls"));
}

/* ------------------------------------------------------------ boot screen */

/* The window is up in ~200 ms; the vector pack is not. Instead of showing an
   idle screen that cannot search yet, the app opens on a greeting: one "hello"
   from the languages this machine's OCR can actually read, changing every
   700 ms while the pack warms. It is a loading screen that states a fact about
   the product — the languages on screen are exactly the ones the index can
   find text in — and it is bounded at both ends, so it can neither flash nor
   hang. */
const BOOT_MIN_MS = 900;
const BOOT_POLL_MS = 120;
const BOOT_MAX_POLLS = 70;

// Shown for the first frame, before `/api/ocr` answers with the real list of
// languages this machine can read. All of these are readable on any machine
// that has the bundled model or a CJK language pack.
const BOOT_GREETINGS = [
  { hello: "Hello", name: "English" },
  { hello: "Привет", name: "Русский" },
  { hello: "Hola", name: "Español" },
  { hello: "Bonjour", name: "Français" },
  { hello: "Hallo", name: "Deutsch" },
  { hello: "こんにちは", name: "日本語" },
  { hello: "你好", name: "中文" },
  { hello: "Ciao", name: "Italiano" },
  { hello: "Olá", name: "Português" },
  { hello: "Merhaba", name: "Türkçe" },
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ------------------------------------------------------------ first run */

/* The setup screen is the boot greeting with somewhere to put the answers.
   It reuses the same greeting pool and the same words; the host window grows
   around its centre and unfolds the right column (see `onboarding_split` in
   app.py). */
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

function greetingBridge(name) {
  const bridge = window.pywebview && window.pywebview.api;
  return bridge && typeof bridge[name] === "function" ? bridge[name] : null;
}

async function fetchOnboarding() {
  try {
    return await api("/api/onboarding");
  } catch (error) {
    // The app can still be used without the setup screen; never block boot on it.
    return null;
  }
}

function showNextOnboardingGreeting() {
  const hello = $("onboard-hello");
  const lang = $("onboard-hello-lang");
  if (!hello || !ONBOARD.pool.length) return;
  const item = ONBOARD.pool[ONBOARD.index % ONBOARD.pool.length];
  ONBOARD.index += 1;
  hello.classList.add("fade");
  setTimeout(() => {
    hello.textContent = item.hello;
    if (lang) lang.textContent = item.name;
    hello.classList.remove("fade");
  }, 140);
}

function startOnboardingCycle() {
  clearInterval(ONBOARD.timer);
  ONBOARD.timer = setInterval(showNextOnboardingGreeting, 2200);
}

function openOnboarding(data, current) {
  const box = $("onboard");
  if (!box) return;
  ONBOARD.active = true;
  ONBOARD.data = data;
  ONBOARD.pool = BOOT_GREETINGS;
  ONBOARD.index = 0;

  const hello = $("onboard-hello");
  const lang = $("onboard-hello-lang");
  if (hello && current && current.hello) hello.textContent = current.hello;
  if (lang && current && current.name) lang.textContent = current.name;

  // Continue from the word already on screen instead of starting over.
  const at = BOOT_GREETINGS.findIndex((item) => item.hello === (current && current.hello));
  ONBOARD.index = at >= 0 ? at + 1 : 0;

  box.hidden = false;
  paintOnboarding();
  requestAnimationFrame(() => box.classList.add("open"));

  // A beat at the centre, then the window moves aside and the settings column
  // unfolds. The delay is what makes the movement read as a transition rather
  // than as a different window having opened behind the first one.
  setTimeout(() => {
    box.classList.add("split");
    const split = greetingBridge("onboarding_split");
    if (split) Promise.resolve(split()).catch(() => {});
  }, 480);

  startOnboardingCycle();
  // Kensaku detection is cheap, but it touches foreign config files; let the
  // layout settle first so a slow disk does not make the greeting stutter.
  setTimeout(scanKensaku, 900);
}

function paintOnboarding() {
  const data = ONBOARD.data;
  if (!data) return;

  document.querySelectorAll("#onboard-lang button").forEach((button) => {
    button.setAttribute("aria-checked", String(button.dataset.lang === currentLang()));
  });

  const auto = data.autostart || {};
  const toggle = $("ob-autostart");
  if (toggle) {
    toggle.checked = Boolean(auto.enabled);
    toggle.disabled = !auto.supported;
  }
  const autoState = $("ob-autostart-state");
  if (autoState) {
    autoState.classList.remove("good", "bad");
    if (ONBOARD.autostartError) {
      autoState.textContent = t("onboard.autostartFailed", { error: ONBOARD.autostartError });
      autoState.classList.add("bad");
    } else if (!auto.supported) {
      autoState.textContent = t("onboard.autostartUnsupported");
    } else {
      autoState.textContent = auto.enabled
        ? t("onboard.autostartOn") : t("onboard.autostartOff");
      autoState.classList.toggle("good", Boolean(auto.enabled));
    }
  }

  const roots = data.roots || [];
  const list = $("ob-roots");
  if (list) {
    list.innerHTML = roots.length
      ? roots.map((root) => `<li title="${esc(root)}">${esc(root)}</li>`).join("")
      : `<li class="ob-none">${esc(t("onboard.indexEmpty"))}</li>`;
  }

  paintImportState();
  paintIndexState();
}

function paintImportState() {
  const state = $("ob-import-state");
  if (!state) return;
  state.classList.remove("good", "bad");
  const badge = $("ob-import-badge");
  const importBtn = $("ob-import");
  const scanBtn = $("ob-import-scan");
  if (scanBtn) scanBtn.disabled = ONBOARD.importBusy;
  if (importBtn) {
    const blocked = ONBOARD.importBusy
      || Boolean(ONBOARD.scan && !ONBOARD.scan.found);
    importBtn.disabled = blocked;
    // A filled primary button that does nothing is worse than a quiet one:
    // without a Kensaku install there is nothing to import, so stop shouting.
    importBtn.classList.toggle("primary", !blocked);
    importBtn.classList.toggle("ghost", blocked);
  }

  if (ONBOARD.importBusy) {
    state.textContent = t("onboard.importScanning");
  } else if (ONBOARD.importError) {
    state.textContent = t("onboard.importFailed", { error: ONBOARD.importError });
    state.classList.add("bad");
  } else if (ONBOARD.imported) {
    const report = ONBOARD.imported;
    state.textContent = t("onboard.importDone", {
      pins: report.pins, saved: report.saved, aliases: report.aliases,
    });
    state.classList.add("good");
    if (report.missing_count) {
      state.textContent += ` ${t("onboard.importMissing", { n: report.missing_count })}`;
    }
  } else if (ONBOARD.scan) {
    if (ONBOARD.scan.found) {
      state.textContent = t("onboard.importFound", {
        pins: ONBOARD.scan.pins,
        saved: ONBOARD.scan.saved,
        aliases: ONBOARD.scan.aliases,
      });
      state.classList.add("good");
    } else {
      state.textContent = t("onboard.importNothing");
    }
  } else {
    state.textContent = "";
  }

  if (badge) {
    const found = Boolean(ONBOARD.scan && ONBOARD.scan.found);
    badge.hidden = !found;
    if (found) {
      badge.textContent = String(
        ONBOARD.scan.pins + ONBOARD.scan.saved + ONBOARD.scan.aliases,
      );
    }
  }
}

function paintIndexState() {
  const state = $("ob-index-state");
  const bar = $("ob-bar");
  if (!state) return;
  state.classList.remove("good", "bad");
  const job = ONBOARD.job;
  let percent = 0;
  if (job && job.running) {
    percent = job.total ? Math.round((job.position / job.total) * 100) : 4;
    state.textContent = t("onboard.indexRunning", { done: percent });
  } else if (job && job.errorKind === "empty") {
    state.textContent = t("onboard.indexEmpty");
    state.classList.add("bad");
  } else if (job && job.error) {
    state.textContent = t("onboard.indexFailed", { error: job.error });
    state.classList.add("bad");
  } else if (job && job.result) {
    percent = 100;
    state.textContent = t("onboard.indexDone", {
      files: plural(job.result.indexed || 0, "unit.files"),
      seconds: job.result.seconds || 0,
    });
    state.classList.add("good");
  } else {
    state.textContent = "";
  }
  if (bar) bar.style.width = `${percent}%`;
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
  // Persist first: the window grows back whether or not the bridge exists.
  api("/api/onboarding/done", { method: "POST", body: "{}" }).catch(() => {});
  const grow = greetingBridge("onboarding_finish");
  if (grow) Promise.resolve(grow()).catch(() => {});

  const box = $("onboard");
  if (box) {
    box.classList.add("done");
    setTimeout(() => box.remove(), 640);
  }
  setTimeout(() => {
    $("q")?.focus();
    loadStats();
    if (!state.query) renderIdle();
  }, 700);
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
    setTimeout(() => {
      hello.textContent = item.hello;
      lang.textContent = item.name;
      hello.classList.remove("fade");
    }, 110);
  };
  show();
  const timer = setInterval(show, 700);

  // The server knows exactly which languages this machine can read; the
  // built-in list above is only there so the first frame does not wait for it.
  api("/api/ocr")
    .then((data) => {
      const list = (data.coverage && data.coverage.languages) || [];
      if (!list.length) return;
      pool = list;
      index = Math.floor(Math.random() * pool.length);
    })
    .catch(() => {
      // A splash must never be the reason the app does not start.
    });

  // First launch is not a splash screen, it is a welcome: show the greeting
  // three times while the window sits in the middle, then turn the same words
  // into the setup screen. Waiting for the vector pack here would make setup
  // feel like a hang; it warms up behind the conversation.
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
        // `warm_ms` is how long the index took to be ready; `uptime_ms` is how
        // long the server has been up. A second window opened an hour later
        // must not claim the app took an hour to start.
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
  hello.classList.remove("fade");
  box.classList.add("done");
  setTimeout(() => box.remove(), 620);
  // The cursor belongs in the search box: this is a launcher, not a dashboard.
  $("q")?.focus();
}

function skipBoot() {
  const box = $("boot");
  if (!box || box.classList.contains("done")) return;
  box.classList.add("done");
  setTimeout(() => box.remove(), 620);
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") skipBoot();
}, true);
document.addEventListener("click", (event) => {
  if (event.target.closest("#boot")) skipBoot();
}, true);

applyStaticText();
loadTheme();
renderWalls();
paintLang();
paintSince();
renderIdle();
wireWindowControls();
loadStats();
api("/api/progress").then((job) => {
  if (job.running) { $("index-btn").disabled = true; pollJob(); }
});

const params = new URLSearchParams(location.search);
const initial = params.get("q");
if (initial) {
  $("q").value = initial;
  runSearch();
}
// Deep link to a file: `?preview=<id>` opens Quick Look straight away, which is
// what a link pasted into a chat should do.
const initialPreview = params.get("preview");
if (initialPreview) openPreview(Number(initialPreview));

// A deep link is a request for a specific file, not a request for a greeting:
// the splash would be in the way of the thing the link points at.
if (initial || initialPreview) skipBoot();
else boot();
