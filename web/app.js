const $ = (id) => document.getElementById(id);
const state = {
  query: "", kind: "", folder: "", since: "", sort: "best",
  timer: null, jobTimer: null,
  hits: [], total: 0, nextOffset: null, approx: false, selected: -1, chips: [],
};
const KIND_LABEL = {
  pdf: "pdf", docx: "docx", code: "код", text: "txt", image: "img",
  pptx: "pptx", xlsx: "xlsx", epub: "epub", odt: "odt", rtf: "rtf",
};

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

// "5 версии" is the kind of detail that makes a UI feel translated. Russian
// needs three forms, and the count is the whole message of the badge.
function plural(count, one, few, many) {
  const tail = count % 100;
  if (tail >= 11 && tail <= 14) return many;
  const last = count % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

function highlight(snippet, query) {
  let html = esc(snippet);
  const terms = (query.toLowerCase().match(/[\p{L}\p{N}_]+/gu) || [])
    .filter((t) => t.length >= 3)
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

/* ------------------------------------------------------------------ search */

function scheduleSearch() {
  clearTimeout(state.timer);
  state.timer = setTimeout(runSearch, 150);
}

async function runSearch(options = {}) {
  const append = Boolean(options.append);
  const query = $("q").value.trim();
  state.query = query;
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
  if (state.sort && state.sort !== "best") params.set("sort", state.sort);

  try {
    const data = await api(`/api/search?${params}`);
    state.hits = append ? state.hits.concat(data.hits) : data.hits;
    state.total = data.total ?? data.hits.length;
    state.nextOffset = data.next_offset ?? null;
    state.approx = Boolean(data.approx);
    state.chips = data.chips || [];
    renderResults(data, append);
    const shown = state.hits.length;
    const sign = state.approx ? "≈" : "";
    $("meta").textContent = shown
      ? `${shown} из ${sign}${state.total} · ${data.ms} мс`
      : `${data.ms} мс`;
  } catch (error) {
    toast(`Поиск не удался: ${error.message}`);
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
  $("content").innerHTML = `
    <div class="empty">
      <div class="empty-body">
        <div class="empty-card">
          <h2>Ищите по смыслу, а не по имени файла</h2>
          <p>Строка находит содержимое документов, текст на скриншотах и фрагменты кода.
             Всё считается на этом компьютере, наружу ничего не уходит.</p>
        </div>
        <div class="recent" id="recent" hidden></div>
      </div>
    </div>`;
  loadRecent();
}

/* The idle screen used to be one small plate on a very large photo, which read
   as a half-empty page. Six recent screenshots give the space a floor: the eye
   lands on something real and the whole column stops looking unfinished. */
let recentLoaded = false;

async function loadRecent() {
  if (recentLoaded) return;
  recentLoaded = true;
  const host = $("recent");
  if (!host) return;
  try {
    const data = await api("/api/images?limit=6");
    const images = (data.images || []).slice(0, 6);
    if (!images.length || !$("recent")) return;
    host.innerHTML = `<h3>недавние скриншоты</h3>
      <div class="grid">${images.map(shot).join("")}</div>`;
    host.hidden = false;
  } catch (error) {
    recentLoaded = false; // a decoration must never break the idle screen
  }
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
    ? `<button class="act" data-similar="${hit.file_id}">похожие</button>` : "";
  // A group is a fact about the result, not decoration: "3 версии" tells you the
  // row you are looking at is the one the ranker picked out of three files.
  const groupLabel = {
    burst: () => plural(hit.group_size, "снимок", "снимка", "снимков"),
    copy: () => plural(hit.group_size, "копия", "копии", "копий"),
    version: () => plural(hit.group_size, "версия", "версии", "версий"),
  };
  const group = hit.group_size > 1
    ? `<button class="badge b-group" data-group="${hit.file_id}"
         title="показать все ${hit.group_size}">${
         hit.group_size} ${(groupLabel[hit.group_reason] || groupLabel.version)()
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
        </div>
        <div class="path" title="${esc(hit.path)}">${esc(hit.path)}</div>
        <div class="snippet">${highlight(hit.snippet, query)}</div>
        <div class="actions">
          <button class="act" data-open="${hit.file_id}">открыть</button>
          <button class="act" data-reveal="${hit.file_id}">папка</button>
          <button class="act" data-preview="${hit.file_id}">просмотр</button>
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
        (row.size / 1024).toFixed(0)} КБ · ${
        new Date(row.mtime * 1000).toLocaleDateString("ru-RU")}</span>
    </div>`).join("");
  card.insertAdjacentHTML("beforeend",
    `<div class="group-list">${items || '<div class="group-item">не удалось загрузить</div>'}</div>`);
}

/* The chips row is the contract of the natural-language compiler: every filter
   it inferred is shown with the words that produced it, and one click takes it
   back. Without this the query language would feel like a guess. */
function chipRow(chips) {
  if (!chips.length) return "";
  const items = chips.map((chip) => `
    <button class="ichip" data-chip="${esc(chip.source)}" title="убрать «${esc(chip.source)}»">
      <span>${esc(chip.label)}</span><b>×</b>
    </button>`).join("");
  return `<div class="ichips">${items}</div>`;
}

function renderResults(data, append) {
  const parts = [];
  if (!append) parts.push(chipRow(data.chips || []));
  if (data.hints && data.hints.length) {
    parts.push(`<div class="hints">${data.hints.map((hint) => `<span>${esc(hint)}</span>`).join("")}</div>`);
  }

  if (state.hits.length) {
    parts.push(`<div class="results">${
      state.hits.map((hit, index) => card(hit, data.query, index)).join("")
    }</div>`);
  }

  if (data.next_offset !== null && data.next_offset !== undefined) {
    parts.push(`<button class="more" id="more-btn">показать ещё</button>`);
  }

  if (!append && data.images && data.images.length) {
    parts.push(`<div class="section-title">похожие изображения</div>
      <div class="grid">${data.images.map(shot).join("")}</div>`);
  }
  if (!parts.length) {
    parts.push(`<div class="empty"><div>
        <h2>Ничего не найдено</h2>
        <p>Попробуйте другие слова, снимите фильтр по типу или проиндексируйте папку справа.</p>
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
      `${(data.size / 1024).toFixed(0)} КБ`,
      new Date(data.mtime * 1000).toLocaleDateString("ru-RU"),
      data.opens ? `открывали ${data.opens} раз` : "ни разу не открывали",
      data.exists ? "" : "файла нет на диске",
    ].filter(Boolean).join(" · ");
    const body = data.image
      ? `<img class="preview-img" src="${data.image.thumb}" alt="">
         ${data.image.ocr ? `<pre class="preview-text">${esc(data.image.ocr)}</pre>` : ""}`
      : `<pre class="preview-text">${esc(data.text || "текст не извлекался")}</pre>`;
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
        <button class="act" data-open="${data.file_id}">открыть</button>
        <button class="act" data-reveal="${data.file_id}">показать в папке</button>
      </div>`;
    $("preview").classList.add("open");
  } catch (error) {
    toast(`Не удалось открыть просмотр: ${error.message}`);
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
    $("s-files").textContent = stats.files ?? 0;
    $("s-chunks").textContent = stats.chunks ?? 0;
    $("s-images").textContent = stats.images ?? 0;
    $("s-ocr").textContent = stats.ocr_images ?? 0;
    $("s-size").textContent = `${((stats.db_bytes || 0) / 1048576).toFixed(1)} МБ`;
    $("status-text").textContent = stats.files ? `${stats.files} файлов в индексе` : "индекс пуст";
    $("status-dot").style.background = stats.files ? "var(--teal)" : "var(--accent)";

    $("p-enabled").textContent = privacy.enabled ? "включён" : "выключен";
    $("p-count").textContent = privacy.count ?? 0;
    $("purge-btn").disabled = !privacy.count;
    renderWatch(watch);

    const engines = status.ocr?.available || [];
    $("engines").innerHTML = `
      <div>текст <span>${esc(status.text_embedder)}</span> · картинки <span>${esc(status.visual_embedder)}</span>${status.visual_supports_text ? "" : ' <span class="hint" title="без CLIP поиск по описанию недоступен">без CLIP</span>'}</div>
      <div>OCR <span>${engines.length ? esc(engines.join(", ")) : "не найден"}</span></div>`;
    if (!$("roots").value.trim() && status.default_roots?.length) {
      $("roots").value = status.default_roots.join("\n");
    }
  } catch (error) {
    $("status-text").textContent = "сервер не отвечает";
    $("status-dot").style.background = "var(--red, #e24b4a)";
  }
}

function renderWatch(watch) {
  const on = Boolean(watch && watch.running);
  $("watch-btn").classList.toggle("primary", on);
  $("watch-label").textContent = on
    ? `слежение: ${watch.added} обновлено · ${watch.removed} удалено · ${watch.last_batch} в последней пачке`
    : (watch && watch.last_error ? `слежение остановлено: ${watch.last_error}` : "слежение выключено");
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
    toast(watch.running ? "Слежу за папками" : "Слежение выключено");
  } catch (error) {
    toast(`Не удалось: ${error.message}`);
  }
}

async function purgePrivate() {
  try {
    const result = await api("/api/privacy/purge", { method: "POST", body: "{}" });
    toast(`Убрано из индекса: ${result.removed}`);
    loadStats();
  } catch (error) {
    toast(`Не удалось: ${error.message}`);
  }
}

function startJob() {
  const roots = $("roots").value.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!roots.length) { toast("Укажите хотя бы одну папку"); return; }
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
      : (job.result ? `готово за ${job.result.seconds} с · ${job.result.indexed} файлов` : (job.error || "готов к работе"));
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
      : `<div class="empty" style="min-height:120px"><p>Похожих скриншотов пока нет.</p></div>`;
    $("modal").classList.add("open");
  } catch (error) {
    toast(error.message);
  }
}

/* ----------------------------------------------------------------- events */

document.addEventListener("click", async (event) => {
  const chip = event.target.closest("[data-chip]");
  if (chip) { clearChip(chip.dataset.chip); return; }

  if (event.target.closest("#more-btn")) { runSearch({ append: true }); return; }

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
    toast(`Не удалось: ${error.message}`);
  }
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach((item) => item.classList.remove("active"));
    chip.classList.add("active");
    state.kind = chip.dataset.kind;
    if (state.query) runSearch();
  });
});

$("q").addEventListener("input", scheduleSearch);
$("q").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { clearTimeout(state.timer); runSearch(); }
  if (event.key === "Escape") { $("q").value = ""; runSearch(); }
  if (event.key === "ArrowDown") { event.preventDefault(); moveSelection(1); }
  if (event.key === "ArrowUp") { event.preventDefault(); moveSelection(-1); }
});
$("sort").addEventListener("change", () => {
  state.sort = $("sort").value;
  if (state.query) runSearch();
});
$("folder").addEventListener("change", () => { state.folder = $("folder").value.trim(); if (state.query) runSearch(); });
$("since").addEventListener("change", () => { state.since = $("since").value; if (state.query) runSearch(); });
$("clear").addEventListener("click", () => {
  $("q").value = "";
  clearTimeout(state.timer);
  runSearch();
  $("q").focus();
});
$("index-btn").addEventListener("click", startJob);
$("watch-btn").addEventListener("click", toggleWatch);
$("purge-btn").addEventListener("click", purgePrivate);
$("modal-close").addEventListener("click", () => $("modal").classList.remove("open"));
$("modal").addEventListener("click", (event) => { if (event.target.id === "modal") $("modal").classList.remove("open"); });
$("preview-close").addEventListener("click", () => $("preview").classList.remove("open"));
$("preview").addEventListener("click", (event) => {
  if (event.target.id === "preview") $("preview").classList.remove("open");
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
    if ($("preview").classList.contains("open")) { $("preview").classList.remove("open"); return; }
    if ($("modal").classList.contains("open")) { $("modal").classList.remove("open"); return; }
  }
  if (inField && document.activeElement === $("q")) return;

  const hit = selectedHit();
  if (!hit) return;

  if (event.key === "ArrowDown") { event.preventDefault(); moveSelection(1); }
  else if (event.key === "ArrowUp") { event.preventDefault(); moveSelection(-1); }
  else if (event.key === "Enter") {
    event.preventDefault();
    api(`/api/${event.shiftKey ? "reveal" : "open"}?id=${hit.file_id}`)
      .catch((error) => toast(`Не удалось: ${error.message}`));
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

  const box = $("win-controls");
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
}

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
