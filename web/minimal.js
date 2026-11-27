/* Minimal menu — flat dark launcher over the existing AutoFind API.
   No framework, no inline scripts (the server's CSP allows only this file). */

'use strict';

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[ch]);

const state = {
  items: [],
  selected: -1,
  seq: 0,
  timer: 0,
  query: "",
};

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  let body = null;
  try { body = await response.json(); } catch (error) { body = null; }
  if (!response.ok) throw new Error((body && body.error) || `HTTP ${response.status}`);
  return body || {};
}

function extension(name) {
  const text = String(name || "");
  const dot = text.lastIndexOf(".");
  return dot > 0 ? text.slice(dot).toLowerCase().slice(0, 12) : "";
}

function ago(seconds) {
  const age = Math.max(0, Date.now() / 1000 - Number(seconds || 0));
  if (!Number.isFinite(age) || age < 60) return "только что";
  if (age < 3600) return `${Math.floor(age / 60)} мин. назад`;
  if (age < 86400) return `${Math.floor(age / 3600)} ч. назад`;
  if (age < 604800) return `${Math.floor(age / 86400)} дн. назад`;
  return new Date(Number(seconds) * 1000).toLocaleDateString("ru-RU");
}

function size(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} Б`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} КБ`;
  return `${(value / 1048576).toFixed(1)} МБ`;
}

function cardMarkup(item, index) {
  const id = Number(item.file_id || 0);
  const ext = esc(item.ext || extension(item.name));
  const when = ago(item.seen_at || item.mtime);
  // Cards stay flat like the reference: the only colour in the grid is text.
  // Images get their preview in the details panel, not in the tile itself.
  return `<div class="card" role="option" tabindex="0" aria-selected="false"
      data-index="${index}" data-id="${id}" title="${esc(item.path)}">
    <div class="thumb"><span class="ext">${ext}</span></div>
    <div class="file">
      <div class="name">${esc(item.name)}</div>
      <div class="meta">${esc(when)}</div>
    </div>
  </div>`;
}

function render() {
  const grid = $("grid");
  grid.innerHTML = state.items.map(cardMarkup).join("");
  state.selected = -1;
  $("empty").hidden = state.items.length > 0;
  $("count").textContent = state.items.length ? `· ${state.items.length}` : "";
  $("section-title").textContent = state.query ? "Результаты" : "Недавние";
}

function select(index) {
  if (!state.items.length) return;
  const next = Math.max(0, Math.min(index, state.items.length - 1));
  const cards = $("grid").querySelectorAll(".card");
  if (cards[state.selected]) {
    cards[state.selected].setAttribute("aria-selected", "false");
    cards[state.selected].classList.remove("is-selected");
  }
  state.selected = next;
  const card = cards[next];
  if (card) {
    card.setAttribute("aria-selected", "true");
    card.classList.add("is-selected");
    card.scrollIntoView({ block: "nearest" });
  }
}

function current() {
  return state.selected >= 0 ? state.items[state.selected] : null;
}

function normalise(data) {
  return (data.recent || data.hits || []).map((item) => ({
    file_id: item.file_id,
    name: item.name || (item.path || "").split(/[\\/]/).pop(),
    path: item.path || "",
    kind: item.kind || "",
    size: item.size || 0,
    mtime: item.mtime || 0,
    seen_at: item.seen_at || item.mtime || 0,
    thumb: item.thumb || null,
    ext: item.ext || extension(item.name),
  }));
}

async function load() {
  const seq = ++state.seq;
  const query = state.query.trim();
  const path = query
    ? `/api/search?q=${encodeURIComponent(query)}&limit=48&sort=newest`
    : "/api/recent?limit=48";
  try {
    const data = await api(path);
    if (seq !== state.seq) return;
    state.items = normalise(data);
    render();
  } catch (error) {
    if (seq !== state.seq) return;
    state.items = [];
    render();
    $("status").textContent = `Ошибка: ${error.message}`;
  }
}

async function loadStats() {
  try {
    const data = await api("/api/stats");
    $("status").textContent = `Файлов: ${Number(data.files || 0).toLocaleString("ru-RU")}`;
  } catch (error) {
    $("status").textContent = "Индекс недоступен";
  }
}

async function openFile(id, reveal) {
  if (!id) return;
  try {
    await api(`/api/${reveal ? "reveal" : "open"}?id=${id}`);
  } catch (error) {
    $("status").textContent = `Ошибка: ${error.message}`;
  }
}

function closeDetails() {
  $("details").hidden = true;
  $("details-body").innerHTML = "";
}

async function showDetails() {
  const item = current();
  if (!item) return;
  const panel = $("details");
  panel.hidden = false;
  const body = $("details-body");
  body.innerHTML = `<h2>${esc(item.name)}</h2><p class="path">${esc(item.path)}</p>
    <p>Загрузка…</p>`;
  try {
    const data = await api(`/api/preview?id=${item.file_id}&chars=1400`);
    body.innerHTML = `<h2>${esc(data.name || item.name)}</h2>
      <p class="path">${esc(data.path || item.path)}</p>
      <dl>
        <dt>Тип</dt><dd>${esc(data.kind || item.kind || "—")}</dd>
        <dt>Размер</dt><dd>${esc(size(data.size ?? item.size))}</dd>
        <dt>Изменён</dt><dd>${esc(new Date(Number(data.mtime || item.mtime) * 1000).toLocaleString("ru-RU"))}</dd>
        <dt>Открытий</dt><dd>${esc(data.opens ?? 0)}</dd>
      </dl>
      <pre>${esc(String(data.text || data.image?.ocr || "").slice(0, 1400) || "Текст не извлечён")}</pre>`;
  } catch (error) {
    body.innerHTML = `<h2>${esc(item.name)}</h2><p class="path">${esc(item.path)}</p>
      <p>Предпросмотр недоступен: ${esc(error.message)}</p>`;
  }
}

function openMenu(open) {
  $("menu").hidden = !open;
  $("gear").setAttribute("aria-expanded", String(open));
}

/* ------------------------------------------------------------------ wiring */

$("q").addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  $("clear").hidden = !state.query;
  clearTimeout(state.timer);
  state.timer = setTimeout(load, 140);
});

$("q").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (state.query) {
      $("q").value = "";
      state.query = "";
      $("clear").hidden = true;
      load();
    }
    event.preventDefault();
  }
});

$("clear").addEventListener("click", () => {
  $("q").value = "";
  state.query = "";
  $("clear").hidden = true;
  load();
  $("q").focus();
});

$("grid").addEventListener("click", (event) => {
  const card = event.target.closest(".card");
  if (!card) return;
  select(Number(card.dataset.index));
  openFile(Number(card.dataset.id), false);
});

$("grid").addEventListener("dblclick", (event) => {
  const card = event.target.closest(".card");
  if (!card) return;
  openFile(Number(card.dataset.id), true);
});

$("refresh").addEventListener("click", () => { load(); loadStats(); });
$("details-close").addEventListener("click", closeDetails);

$("gear").addEventListener("click", (event) => {
  event.stopPropagation();
  openMenu($("menu").hidden);
});

$("menu").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  openMenu(false);
  if (action === "refresh") { load(); loadStats(); }
  else if (action === "folder") {
    $("status").textContent = "Папка индекса: ~/.autofind";
  } else if (action === "settings") {
    location.href = "/";
  } else if (action === "classic") {
    location.href = "/";
  }
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".menu-anchor")) openMenu(false);
});

document.addEventListener("keydown", (event) => {
  const inField = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement;
  if (event.key === "Escape") {
    if (!$("details").hidden) { closeDetails(); return; }
    if (!$("menu").hidden) { openMenu(false); return; }
    return;
  }
  if (inField) return;
  const item = current();
  const id = item ? Number(item.file_id) : 0;
  switch (event.key) {
    case "ArrowRight":
    case "ArrowDown": select(state.selected + 1); break;
    case "ArrowLeft":
    case "ArrowUp": select(state.selected - 1); break;
    case "Home": select(0); break;
    case "End": select(state.items.length - 1); break;
    case "Enter":
    case " ": openFile(id, false); break;
    case "p": case "P": showDetails(); break;
    case "f": case "F": openFile(id, true); break;
    case "i": case "I": showDetails(); break;
    default: return;
  }
  event.preventDefault();
});

load();
loadStats();
