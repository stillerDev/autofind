// Two screens inside the minimal theme: the settings layout from screenshot 1
// and the launcher/onboarding layout from screenshot 2, plus a real indexing
// progress readout (percent, file count, moving bar, ETA under the percent).
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/minimal.html"), "utf8");
const css = fs.readFileSync(path.join(root, "web/minimal-screens.css"), "utf8");
const js = fs.readFileSync(path.join(root, "web/minimal-screens.js"), "utf8");

function check(name, condition) {
  if (!condition) throw new Error(`${name} failed`);
  console.log(`${name}: OK`);
}

for (const file of ["minimal-screens.css", "minimal-screens.js"]) {
  check(`${file} is loaded`, html.includes(`/${file}`));
}

for (const id of ["settings-screen", "launcher-screen", "idx-percent", "idx-count",
                  "idx-bar", "idx-eta", "idx-status", "settings-save-reindex",
                  "launcher-start", "launcher-back", "hotkey-change"]) {
  check(`screen has #${id}`, html.includes(`id="${id}"`));
}

for (const label of ["Как должен открываться AutoFind?", "Сочетание открытия",
                     "Начать индексацию", "Сохранить и переиндексировать"]) {
  check(`screen copy: ${label}`, html.includes(label));
}
check("no copied launcher branding", !["03 / 03", "検索", "чтобы искать.", "Wayland",
  "Всё остаётся на этом компьютере."].some((label) => html.includes(label)));

for (const item of ["Общие", "Папки", "Исключения", "Поиск", "Семантический поиск",
                    "Агенты", "Состояние", "О программе"]) {
  check(`sidebar has ${item}`, html.includes(item));
}

check("bottom light/dark control in the status bar", html.includes('data-theme-set="light"') &&
  html.includes('data-theme-set="dark"'));
check("launcher offers minimal vs detailed menu", html.includes('data-menu-style="minimal"') &&
  html.includes('data-menu-style="detailed"'));
check("menu style is wired", js.includes("data-menu-style") && js.includes("custom.detailed"));
check("menu styles are visual previews", html.includes('class="preview minimal-preview"') &&
  html.includes('class="preview detailed-preview"'));
check("window card selection is applied",
  js.includes('classList.add("selected")') || js.includes('toggle("selected"'));
check("first run shows the overlay", js.includes("autofind.minimal.onboarded") &&
  js.includes('showScreen("launcher")'));
check("detailed cards are styled", css.includes(".grid.detailed") ||
  fs.readFileSync(path.join(root, "web/minimal.css"), "utf8").includes(".grid.detailed"));
check("theme preview cards", html.includes('data-theme-choice="light"') &&
  html.includes('data-theme-choice="dark"'));

check("progress reads the real job", js.includes("/api/progress") &&
  js.includes("position") && js.includes("total"));
check("percent and count are rendered", js.includes("idx-percent") && js.includes("idx-count"));
check("bar width follows percent", js.includes("idx-bar"));
check("eta is rendered below the percent", js.includes("idx-eta"));
check("indexing can be started", js.includes("/api/index") || js.includes("/api/reindex"));
check("screens are styled", css.includes("#settings-screen") && css.includes("#launcher-screen"));
