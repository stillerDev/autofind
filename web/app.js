const $ = (id) => document.getElementById(id);
const state = { query: "", kind: "", folder: "", since: "", timer: null, jobTimer: null };
const KIND_LABEL = { pdf: "pdf", docx: "docx", code: "код", text: "txt", image: "img" };

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
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

async function runSearch() {
  const query = $("q").value.trim();
  state.query = query;
  if (!query) {
    renderIdle();
    $("meta").textContent = "";
    return;
  }
  const params = new URLSearchParams({ q: query, limit: "12" });
  if (state.kind) params.set("kind", state.kind);
  if (state.folder) params.set("folder", state.folder);
  if (state.since) params.set("since", state.since);

  try {
    const data = await api(`/api/search?${params}`);
    renderResults(data);
    $("meta").textContent = `${data.hits.length} результатов · ${data.ms} мс`;
  } catch (error) {
    toast(`Поиск не удался: ${error.message}`);
  }
}

function renderIdle() {
  $("content").innerHTML = `
    <div class="empty">
      <div>
        <h2>Ищите по смыслу, а не по имени файла</h2>
        <p>Строка находит содержимое документов, текст на скриншотах и фрагменты кода.
           Всё считается на этом компьютере, наружу ничего не уходит.</p>
      </div>
    </div>`;
}

function card(hit, query) {
  const label = KIND_LABEL[hit.kind] || hit.kind;
  const icon = hit.is_image && hit.thumb
    ? `<div class="icon k-image"><img src="${hit.thumb}" alt="" loading="lazy"></div>`
    : `<div class="icon k-${hit.kind}">${esc(label)}</div>`;
  const badges = hit.channels
    .map((channel) => `<span class="badge b-${channel}">${channel}</span>`)
    .join("");
  const similar = hit.is_image
    ? `<button class="act" data-similar="${hit.file_id}">похожие</button>` : "";
  return `
    <div class="card">
      ${icon}
      <div>
        <div class="title-row">
          <a class="name" data-open="${hit.file_id}">${esc(hit.name)}</a>
          <span class="badge">${esc(hit.kind)}</span>
          ${badges}
        </div>
        <div class="path" title="${esc(hit.path)}">${esc(hit.path)}</div>
        <div class="snippet">${highlight(hit.snippet, query)}</div>
        <div class="actions">
          <button class="act" data-open="${hit.file_id}">открыть</button>
          <button class="act" data-reveal="${hit.file_id}">папка</button>
          ${similar}
        </div>
      </div>
    </div>`;
}

function renderResults(data) {
  const parts = [];
  if (data.hits.length) {
    parts.push(`<div class="results">${data.hits.map((hit) => card(hit, data.query)).join("")}</div>`);
  }
  if (data.images && data.images.length) {
    parts.push(`<div class="section-title">похожие изображения</div>
      <div class="grid">${data.images.map(shot).join("")}</div>`);
  }
  if (!parts.length) {
    parts.push(`<div class="empty"><div>
        <h2>Ничего не найдено</h2>
        <p>Попробуйте другие слова, снимите фильтр по типу или проиндексируйте папку справа.</p>
      </div></div>`);
  }
  $("content").innerHTML = parts.join("");
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
      <div>текст: <span>${esc(status.text_embedder)}</span></div>
      <div>картинки: <span>${esc(status.visual_embedder)}</span>${status.visual_supports_text ? "" : ' <span class="hint" title="без CLIP поиск по описанию недоступен">без CLIP</span>'}</div>
      <div>OCR: <span>${engines.length ? esc(engines.join(", ")) : "не найден"}</span></div>`;
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
  const target = event.target.closest("[data-open],[data-reveal],[data-similar]");
  if (!target) return;
  if (target.dataset.similar) { openSimilar(target.dataset.similar); return; }
  const id = target.dataset.open || target.dataset.reveal;
  const route = target.dataset.open ? "open" : "reveal";
  try {
    await api(`/api/${route}?id=${id}`);
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

document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement !== $("q")) {
    event.preventDefault();
    $("q").focus();
    $("q").select();
  }
});

renderIdle();
loadStats();
api("/api/progress").then((job) => {
  if (job.running) { $("index-btn").disabled = true; pollJob(); }
});

const initial = new URLSearchParams(location.search).get("q");
if (initial) {
  $("q").value = initial;
  runSearch();
}
