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

/* ---------------------------------------------------------------- indexing */

function fmtEta(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 1) return "~ 1 с";
  if (seconds < 60) return `~ ${Math.ceil(seconds)} с`;
  if (seconds < 3600) return `~ ${Math.ceil(seconds / 60)} мин`;
  return `~ ${(seconds / 3600).toFixed(1)} ч`;
}

function setProgress(position, total, elapsed, status) {
  const done = Number(position) || 0;
  const all = Number(total) || 0;
  const pct = all > 0 ? Math.min(100, Math.round((done / all) * 100)) : 0;
  $("idx-percent").textContent = `${pct}%`;
  $("idx-count").textContent =
    `${done.toLocaleString("ru-RU")} / ${all.toLocaleString("ru-RU")} файлов`;
  $("idx-bar").style.width = `${pct}%`;
  const rate = done > 0 ? (Number(elapsed) || 0) / done : 0;
  $("idx-eta").textContent = fmtEta(all > done ? rate * (all - done) : 0);
  $("idx-status").textContent = status || "";
}

async function startIndex() {
  remember("autofind.minimal.onboarded", "1");
  $("idx-progress").hidden = false;
  setProgress(0, 0, 0, "запуск индексации");
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
      setProgress(job.position || 0, job.total || 0, job.elapsed || 0, job.status || "");
      if (!job.running) {
        clearInterval(progressTimer);
        $("idx-status").textContent = job.error ? `Ошибка: ${job.error}` : "Готово";
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

$("settings-save-reindex").addEventListener("click", () => {
  hideScreens();
  startIndex();
});

$("settings-save").addEventListener("click", hideScreens);

syncThemeControls();
syncMenuStyle();

// On the very first launch the setup overlay sits on top of the search view:
// window vs launcher, the open hotkey, and the minimal/detailed menu choice.
let onboarded = false;
try { onboarded = localStorage.getItem("autofind.minimal.onboarded") === "1"; } catch (error) { /* show it */ }
if (!onboarded) showScreen("launcher");
