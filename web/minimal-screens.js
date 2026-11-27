/* Minimal theme — settings and launcher screens, plus live indexing progress.
   Uses the helpers from minimal.js ($, esc, api, postJSON, setTheme). */

'use strict';

let capturingHotkey = false;
let progressTimer = 0;

function anyScreenOpen() {
  return !$("settings-screen").hidden || !$("launcher-screen").hidden;
}

function hideScreens() {
  $("settings-screen").hidden = true;
  $("launcher-screen").hidden = true;
  remember("autofind.minimal.onboarded", "1");
}

function syncThemeControls() {
  const light = document.documentElement.dataset.theme === "light";
  const wanted = light ? "light" : "dark";
  document.querySelectorAll("[data-theme-choice]").forEach((node) => {
    node.classList.toggle("selected", node.dataset.themeChoice === wanted);
  });
  document.querySelectorAll("[data-theme-set]").forEach((node) => {
    node.classList.toggle("on", node.dataset.themeSet === wanted);
  });
}

function syncMenuStyle() {
  const detailed = Boolean(custom.detailed);
  document.querySelectorAll("[data-menu-style]").forEach((node) => {
    node.classList.toggle("on", (node.dataset.menuStyle === "detailed") === detailed);
  });
}

function showScreen(name) {
  hideScreens();
  $("settings-screen").hidden = name !== "settings";
  $("launcher-screen").hidden = name !== "launcher";
  syncThemeControls();
  syncMenuStyle();
  if (name === "settings") syncScreenSettings();
}

document.querySelectorAll("[data-menu-style]").forEach((node) => {
  node.addEventListener("click", () => {
    custom.detailed = node.dataset.menuStyle === "detailed";
    saveCustom();
    applyCustom();
    syncMenuStyle();
    remember("autofind.minimal.menuStyle", custom.detailed ? "detailed" : "minimal");
  });
});

/* Keyboard shortcuts of the search view must not fire behind these screens. */
document.addEventListener("keydown", (event) => {
  if (capturingHotkey || !anyScreenOpen()) return;
  if (event.key === "Escape") {
    event.stopImmediatePropagation();
    hideScreens();
    return;
  }
  event.stopImmediatePropagation();
}, true);

/* The gear menu is wired in minimal.js; capture these two actions first. */
$("menu").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  if (button.dataset.action === "settings-screen" || button.dataset.action === "launcher-screen") {
    openMenu(false);
    showScreen(button.dataset.action === "settings-screen" ? "settings" : "launcher");
    event.stopImmediatePropagation();
  }
}, true);

/* ------------------------------------------------------------- theme / nav */

document.querySelectorAll("[data-theme-choice]").forEach((node) => {
  node.addEventListener("click", () => {
    setTheme(node.dataset.themeChoice === "light");
    syncThemeControls();
  });
});

document.querySelectorAll("[data-theme-set]").forEach((node) => {
  node.addEventListener("click", () => {
    setTheme(node.dataset.themeSet === "light");
    syncThemeControls();
  });
});

$("launcher-theme").addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme !== "light");
  syncThemeControls();
});

$("settings-screen-close").addEventListener("click", hideScreens);
$("launcher-back").addEventListener("click", hideScreens);

function showSetPage(name, label) {
  let page = document.querySelector(`[data-set-content="${name}"]`);
  if (!page) {
    page = document.createElement("div");
    page.className = "set-page";
    page.dataset.setName = name;
    page.innerHTML = `<h1>${esc(label)}</h1>
      <p class="lead">Этот раздел доступен в классическом окне AutoFind.</p>`;
    document.querySelector(".set-main").appendChild(page);
  }
  document.querySelectorAll(".set-page").forEach((node) => { node.hidden = true; });
  page.hidden = false;
  document.querySelectorAll(".side-item").forEach((node) => {
    node.classList.toggle("active", node.dataset.setPage === name);
  });
}

document.querySelectorAll(".side-item").forEach((node) => {
  node.addEventListener("click", () => showSetPage(node.dataset.setPage, node.textContent.trim()));
});

/* -------------------------------------------------------------- preferences */

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch (error) { /* session only */ }
}

function selectOpenMode(mode) {
  document.querySelectorAll("[data-mode]").forEach((node) => {
    node.classList.toggle("on", node.dataset.mode === mode);
  });
  document.querySelectorAll("[data-open-mode]").forEach((node) => {
    node.classList.toggle("selected", node.dataset.openMode === mode);
  });
  remember("autofind.minimal.openMode", mode);
}

document.querySelectorAll("[data-mode], [data-open-mode]").forEach((node) => {
  node.addEventListener("click", () => selectOpenMode(node.dataset.mode || node.dataset.openMode));
});

// The choice is remembered for both surfaces that share it: restore the
// stored mode instead of always showing the markup default.
let openMode = "launcher";
try { openMode = localStorage.getItem("autofind.minimal.openMode") || "launcher"; } catch (error) { /* default */ }
selectOpenMode(openMode);

const langSelect = $("screen-lang");
try { langSelect.value = localStorage.getItem("autofind.minimal.lang") || "auto"; } catch (error) { /* default */ }
langSelect.addEventListener("change", () => remember("autofind.minimal.lang", langSelect.value));

$("hotkey-change").addEventListener("click", (event) => {
  event.stopImmediatePropagation();
  capturingHotkey = true;
  const button = event.currentTarget;
  button.textContent = "Нажмите сочетание…";
  const capture = (keyEvent) => {
    keyEvent.preventDefault();
    keyEvent.stopImmediatePropagation();
    const parts = [];
    if (keyEvent.ctrlKey) parts.push("Ctrl");
    if (keyEvent.altKey) parts.push("Alt");
    if (keyEvent.shiftKey) parts.push("Shift");
    const key = String(keyEvent.key || "");
    if (!["Control", "Alt", "Shift"].includes(key)) parts.push(key.toUpperCase());
    if (parts.length) {
      const keys = document.querySelectorAll(".hotkey-row kbd");
      keys.forEach((node, index) => { node.textContent = parts[index] || ""; });
      remember("autofind.minimal.hotkey", parts.join(" + "));
    }
    button.textContent = "Изменить";
    capturingHotkey = false;
    document.removeEventListener("keydown", capture, true);
  };
  document.addEventListener("keydown", capture, true);
});

/* ------------------------------------------------------- settings content */

/* The fullscreen screen mirrors the quick settings panel: service toggles read
   the same APIs and reuse applyWatch/applyPrivacy plus the custom.* state from
   minimal.js, so both surfaces write the same underlying settings. */

const screenState = { rules: {}, defaultRoots: [] };

function syncToggle(id, checked) {
  const box = $(id);
  if (box) box.checked = checked;
}

function screenNote(message) {
  const note = $("scr-note");
  if (!note) return;
  note.textContent = message;
  note.hidden = !message;
}

function renderRoots() {
  const known = Object.keys(screenState.rules);
  const paths = [...known, ...screenState.defaultRoots.filter((path) => !known.includes(path))];
  $("scr-roots").innerHTML = paths.map((path) => {
    const rule = screenState.rules[path] || {};
    const extras = [];
    if (rule.exclusions && rule.exclusions.length) extras.push(`исключения: ${rule.exclusions.length}`);
    if (rule.max_file_mb) extras.push(`до ${rule.max_file_mb} МБ`);
    return `<label class="set-row">
      <span class="root-path">${esc(path)}${extras.length ? `<small>${esc(extras.join(" · "))}</small>` : ""}</span>
      <input type="checkbox" data-root="${esc(path)}"${rule.enabled !== false ? " checked" : ""}>
    </label>`;
  }).join("") || `<p class="settings-note">Папки индекса ещё не заданы.</p>`;
}

function renderStats(stats) {
  if (!stats) { $("scr-stats").innerHTML = ""; return; }
  const fmt = (value) => Number(value || 0).toLocaleString("ru-RU");
  const rows = [
    ["Файлы", fmt(stats.files)], ["Фрагменты", fmt(stats.chunks)],
    ["Изображения", fmt(stats.images)], ["С OCR-текстом", fmt(stats.ocr_images)],
    ["Вектора", fmt(stats.vectors)], ["Размер базы", size(stats.db_bytes)],
  ];
  $("scr-stats").innerHTML = rows.map(([label, value]) =>
    `<div class="stat"><span>${esc(label)}</span><b>${esc(value)}</b></div>`).join("");
}

async function syncScreenSettings() {
  const [watch, autostart, privacy] = await Promise.all([
    api("/api/watch").catch(() => null),
    api("/api/autostart").catch(() => null),
    api("/api/privacy").catch(() => null),
  ]);
  // Keep the shared prefs from minimal.js current, so applyWatch and
  // applyPrivacy post what this screen actually shows.
  prefs.watch = Boolean(watch && watch.running);
  prefs.idle = !watch || watch.idle_only !== false;
  if (watch && watch.roots && watch.roots.length) prefs.roots = watch.roots;
  prefs.privacy = Boolean(privacy && privacy.enabled);
  syncToggle("scr-watch", prefs.watch);
  syncToggle("scr-idle", prefs.idle);
  syncToggle("set-watch", prefs.watch);
  syncToggle("set-idle", prefs.idle);
  const startBox = $("scr-autostart");
  startBox.checked = Boolean(autostart && autostart.enabled);
  startBox.disabled = !autostart || !autostart.supported;
  startBox.title = !autostart || !autostart.supported
    ? "Автозапуск не поддерживается этой системой" : "";
  syncToggle("scr-privacy", prefs.privacy);
  syncToggle("set-privacy", prefs.privacy);
  $("scr-cats").innerHTML = ((privacy && privacy.categories) || []).map((item) =>
    `<label><input type="checkbox" data-cat="${esc(item.id)}"${item.enabled ? " checked" : ""}> ${esc(item.id)}</label>`
  ).join("") || `<span class="empty">Категорий нет</span>`;
  syncToggle("scr-minimal-menu", true);
  syncToggle("scr-compact", custom.compact);
  syncToggle("scr-ext", custom.showExt);
  syncToggle("scr-detailed", custom.detailed);
  syncToggle("set-compact", custom.compact);
  syncToggle("set-ext", custom.showExt);
  syncToggle("set-detailed", custom.detailed);
  try {
    const saved = localStorage.getItem("autofind.minimal.hotkey");
    if (saved) {
      $("scr-hotkey").innerHTML = saved.split(" + ")
        .map((part) => `<kbd>${esc(part)}</kbd>`).join("");
    }
  } catch (error) { /* the default combination stays */ }
  try {
    const [rules, status, stats] = await Promise.all([
      api("/api/rules"), api("/api/status"), api("/api/stats"),
    ]);
    screenState.rules = (rules && rules.roots) || {};
    screenState.defaultRoots = (status && status.default_roots) || [];
    renderRoots();
    $("scr-version").textContent = (status && status.version) || "—";
    renderStats(stats);
    screenNote("");
  } catch (error) {
    screenNote(`Состояние недоступно: ${error.message}`);
  }
  try {
    const data = await api("/api/exclusions");
    $("scr-exclusions").value = (data.exclusions || []).join("\n");
  } catch (error) { /* the editor stays empty */ }
}

async function applyScreenPrivacy() {
  try {
    const chosen = [...$("scr-cats").querySelectorAll("input:checked")]
      .map((box) => box.dataset.cat);
    await postJSON("/api/privacy/categories", {
      enabled: prefs.privacy,
      categories: chosen,
    });
  } catch (error) {
    screenNote(`Ошибка: ${error.message}`);
  }
}

/* Roots and exclusions wait for the floating Save buttons; everything else
   applies immediately, like on the quick panel. */
async function savePending() {
  const boxes = [...$("scr-roots").querySelectorAll("input[data-root]")];
  if (boxes.length) {
    const roots = {};
    for (const box of boxes) {
      const rule = screenState.rules[box.dataset.root] || {};
      roots[box.dataset.root] = {
        enabled: box.checked,
        exclusions: rule.exclusions || [],
        excluded_dirs: rule.excluded_dirs || [],
        max_file_mb: rule.max_file_mb || 0,
      };
    }
    await postJSON("/api/rules", { roots });
  }
  const patterns = $("scr-exclusions").value.split("\n")
    .map((line) => line.trim()).filter(Boolean);
  await postJSON("/api/exclusions", { exclusions: patterns });
}

$("scr-watch").addEventListener("change", (event) => {
  prefs.watch = event.target.checked;
  syncToggle("set-watch", event.target.checked);
  applyWatch();
});

$("scr-idle").addEventListener("change", (event) => {
  prefs.idle = event.target.checked;
  syncToggle("set-idle", event.target.checked);
  if (prefs.watch) applyWatch();
});

$("scr-autostart").addEventListener("change", async (event) => {
  const wanted = event.target.checked;
  try {
    await postJSON("/api/autostart", { enabled: wanted });
    syncToggle("set-autostart", wanted);
  } catch (error) {
    event.target.checked = !wanted;
    screenNote(`Ошибка: ${error.message}`);
  }
});

$("scr-privacy").addEventListener("change", (event) => {
  prefs.privacy = event.target.checked;
  syncToggle("set-privacy", event.target.checked);
  applyScreenPrivacy();
});

$("scr-cats").addEventListener("change", applyScreenPrivacy);

$("scr-minimal-menu").addEventListener("change", (event) => {
  if (!event.target.checked) location.href = "/";
});

$("scr-compact").addEventListener("change", (event) => {
  custom.compact = event.target.checked;
  syncToggle("set-compact", event.target.checked);
  applyCustom();
  saveCustom();
});

$("scr-ext").addEventListener("change", (event) => {
  custom.showExt = event.target.checked;
  syncToggle("set-ext", event.target.checked);
  applyCustom();
  saveCustom();
});

$("scr-detailed").addEventListener("change", (event) => {
  custom.detailed = event.target.checked;
  syncToggle("set-detailed", event.target.checked);
  applyCustom();
  saveCustom();
  syncMenuStyle();
});

$("scr-purge").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!button.classList.contains("confirm")) {
    button.textContent = "Точно удалить?";
    button.classList.add("confirm");
    setTimeout(() => {
      button.textContent = "Удалить приватное из индекса";
      button.classList.remove("confirm");
    }, 4000);
    return;
  }
  button.disabled = true;
  try {
    await postJSON("/api/privacy/purge", {});
    button.textContent = "Готово";
    load();
  } catch (error) {
    screenNote(`Ошибка: ${error.message}`);
  } finally {
    button.disabled = false;
    button.classList.remove("confirm");
    setTimeout(() => { button.textContent = "Удалить приватное из индекса"; }, 2000);
  }
});

$("scr-selftest").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  const out = $("scr-selftest-out");
  try {
    const report = await api("/api/self-test");
    out.hidden = false;
    out.textContent = report.ok
      ? `Ок: файлы ${report.files}, фрагменты ${report.chunks}, тексты ${report.texts}, изображения ${report.images}` +
        (report.issues && report.issues.length ? `; замечания: ${report.issues.join("; ")}` : "")
      : `Проблемы: ${(report.issues || []).join("; ") || "неизвестно"}`;
  } catch (error) {
    out.hidden = false;
    out.textContent = `Ошибка: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

$("scr-replay").addEventListener("click", async () => {
  try { await postJSON("/api/onboarding/reset", {}); } catch (error) { /* show it anyway */ }
  showScreen("launcher");
});

/* ---------------------------------------------------------------- indexing */

function fmtEta(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 1) return "~ 1 с";
  if (seconds < 60) return `~ ${Math.ceil(seconds)} с`;
  if (seconds < 3600) return `~ ${Math.ceil(seconds / 60)} мин`;
  return `~ ${(seconds / 3600).toFixed(1)} ч`;
}

const STATUS_RU = {
  scan: "Сканирование папок",
  unchanged: "Без изменений",
  duplicate: "Дубликат",
  "same-content": "Дубликат",
  "seen-image": "Дубликат",
  "name-only": "Только имя",
  "no-text": "Без текста",
  undecodable: "Не читается",
  error: "Ошибка чтения",
  finalize: "Финализация",
};

let hideProgressTimer = 0;

function hideProgressSoon() {
  clearTimeout(hideProgressTimer);
  hideProgressTimer = setTimeout(() => { $("idx-progress").hidden = true; }, 3000);
}

function setProgress(position, total, elapsed, status, running) {
  const done = Number(position) || 0;
  const all = Number(total) || 0;
  const label = STATUS_RU[status] || status || "";
  if (!all) {
    // The filesystem walk runs before any percent can be honest: show found
    // files instead of a bar racing to 100%.
    $("idx-percent").textContent = "…";
    $("idx-bar").style.width = "24%";
    $("idx-count").textContent = `${done.toLocaleString("ru-RU")} найдено`;
    $("idx-eta").textContent = "~ —";
    $("idx-status").textContent = label;
    return;
  }
  // 100% means the job is really done; the finalize tail (commit, backfills,
  // sealed sidecar) holds the bar at 99% instead of claiming completion.
  const pct = running ? Math.min(99, Math.round((done / all) * 100)) : 100;
  $("idx-percent").textContent = `${pct}%`;
  $("idx-count").textContent =
    `${done.toLocaleString("ru-RU")} / ${all.toLocaleString("ru-RU")} файлов`;
  $("idx-bar").style.width = `${pct}%`;
  const rate = done > 0 ? (Number(elapsed) || 0) / done : 0;
  $("idx-eta").textContent = fmtEta(all > done ? rate * (all - done) : 0);
  $("idx-status").textContent = label;
}

async function startIndex() {
  remember("autofind.minimal.onboarded", "1");
  clearTimeout(hideProgressTimer);
  $("idx-progress").hidden = false;
  setProgress(0, 0, 0, "scan", true);
  try {
    const status = await api("/api/status");
    await postJSON("/api/index", { roots: (status && status.default_roots) || [], prune: false });
  } catch (error) {
    $("idx-status").textContent = error.message;
    return;
  }
  clearInterval(progressTimer);
  progressTimer = setInterval(async () => {
    try {
      const job = await api("/api/progress");
      setProgress(job.position || 0, job.total || 0, job.elapsed || 0, job.status || "", job.running);
      if (!job.running) {
        clearInterval(progressTimer);
        $("idx-bar").style.width = "100%";
        $("idx-percent").textContent = "100%";
        if (job.error) {
          $("idx-status").textContent = `Ошибка: ${job.error}`;
        } else {
          $("idx-status").textContent = "Готово";
          hideProgressSoon();
        }
      }
    } catch (error) {
      clearInterval(progressTimer);
      $("idx-status").textContent = error.message;
    }
  }, 400);
}

$("launcher-start").addEventListener("click", () => {
  $("launcher-start").disabled = true;
  startIndex().finally(() => { $("launcher-start").disabled = false; });
});

$("settings-save-reindex").addEventListener("click", async () => {
  try {
    await savePending();
  } catch (error) {
    screenNote(`Не сохранено: ${error.message}`);
    return;
  }
  hideScreens();
  startIndex();
});

$("settings-save").addEventListener("click", async () => {
  try {
    await savePending();
  } catch (error) {
    screenNote(`Не сохранено: ${error.message}`);
    return;
  }
  hideScreens();
});

syncThemeControls();
syncMenuStyle();

// On the very first launch the setup overlay sits on top of the search view:
// window vs launcher, the open hotkey, and the minimal/detailed menu choice.
let onboarded = false;
try { onboarded = localStorage.getItem("autofind.minimal.onboarded") === "1"; } catch (error) { /* show it */ }
if (!onboarded) showScreen("launcher");
