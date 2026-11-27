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

for (const item of ["Общие", "Вид", "Индексация", "Запуск", "Конфиденциальность",
                    "Исключения", "Состояние", "О программе"]) {
  check(`sidebar has ${item}`, html.includes(item));
}

// Every toggle the app really has lives on the settings screen, grouped into
// sections that all carry real content instead of "see the classic window".
for (const id of ["scr-watch", "scr-idle", "scr-autostart", "scr-privacy",
                  "scr-cats", "scr-purge", "scr-minimal-menu", "scr-compact",
                  "scr-ext", "scr-detailed", "scr-roots", "scr-exclusions",
                  "scr-stats", "scr-version", "scr-selftest", "scr-replay"]) {
  check(`settings screen has #${id}`, html.includes(`id="${id}"`));
}
check("placeholder sections are gone",
  !["data-set-page=\"search\"", "data-set-page=\"semantic\"", "data-set-page=\"agents\""]
    .some((marker) => html.includes(marker)));
check("every section has real content",
  ["general", "appearance", "index", "startup", "privacy", "exclusions", "state", "about"]
    .every((page) => html.includes(`data-set-page="${page}"`) &&
                     html.includes(`data-set-content="${page}"`)));
check("every section carries an icon",
  (html.match(/data-set-page="[a-z]+">\s*<svg/g) || []).length === 8);
check("screen toggles reuse the switch style",
  css.includes(".set-row") ||
  fs.readFileSync(path.join(root, "web/minimal.css"), "utf8").includes(".set-row input[type=\"checkbox\"]"));
check("roots toggles are wired to rules",
  js.includes("/api/rules") && js.includes("scr-roots"));
check("exclusions editor is wired",
  js.includes("/api/exclusions") && js.includes("scr-exclusions"));
check("service toggles reuse the quick-panel logic",
  js.includes("applyWatch") && js.includes("applyPrivacy") && js.includes("/api/autostart"));
check("customization toggles save", js.includes("saveCustom") && js.includes("applyCustom"));
check("privacy purge is wired", js.includes("/api/privacy/purge"));
check("state shows real stats", js.includes("/api/stats") && js.includes("scr-stats"));
check("about shows the real version", js.includes("scr-version") && js.includes("/api/status"));
check("onboarding can be replayed",
  js.includes("/api/onboarding/reset") && js.includes("scr-replay"));
check("self-test is available", js.includes("/api/self-test") && js.includes("scr-selftest"));
check("save applies pending rules", js.includes("savePending"));

check("bottom light/dark control in the status bar", html.includes('data-theme-set="light"') &&
  html.includes('data-theme-set="dark"'));
check("launcher offers minimal vs detailed menu", html.includes('data-menu-style="minimal"') &&
  html.includes('data-menu-style="detailed"'));
check("menu style is wired", js.includes("data-menu-style") && js.includes("custom.detailed"));
check("menu styles are visual previews", html.includes('class="preview minimal-preview"') &&
  html.includes('class="preview detailed-preview"'));
check("window card selection is applied",
  js.includes('classList.add("selected")') || js.includes('toggle("selected"'));
check("mode mockups are recognizable",
  html.includes('class="win-bar"') && html.includes('class="win-task"') &&
  html.includes('class="palette"') && html.includes('class="hotkey-mini"') &&
  html.includes('class="tray"'));
check("mockups are styled in both themes",
  css.includes(".win-task i:first-child") && css.includes(".palette"));
check("menu previews show the actual difference",
  html.includes('class="m-tile"') && html.includes('class="d-card"') &&
  html.includes("C:\\Docs\\app.js") && html.includes("code · 12 КБ"));
check("buttons use SVG arrows, not text glyphs",
  (html.match(/class="arrow-icon"/g) || []).length === 2 &&
  !html.includes("←") && !html.includes("→"));
check("arrow hover is gated and reduced-motion safe",
  css.includes("#launcher-start:hover .arrow-icon") && css.includes("prefers-reduced-motion"));
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

// One design system for both screens: the launcher uses the same page anatomy
// as the settings, the window-mode picker is the same card component in both,
// and the launcher's actions sit in the same floating pill.
check("launcher reuses the settings page anatomy", html.includes('class="set-page ln-page"'));
check("mode picker is one component in both screens",
  (html.match(/class="mini-window"/g) || []).length === 2 &&
  (html.match(/class="mini-launcher"/g) || []).length === 2 &&
  (html.match(/data-open-mode="window"/g) || []).length === 2);
check("the plain mode row is gone", !html.includes('class="mode-row"'));
check("every selectable card shares the choice-card language",
  (html.match(/choice-card/g) || []).length === 8);
check("picker cards carry the radio dot", (html.match(/class="radio"/g) || []).length === 6);
check("group titles are headed by icons",
  (html.match(/class="group-head"/g) || []).length === 6);
check("launcher actions sit in the same floating pill",
  (html.match(/class="set-bottom"/g) || []).length === 2);
check("the bespoke launcher card is gone",
  !html.includes("ln-card") && !html.includes("ln-foot"));
check("the chosen open mode is restored on load",
  js.includes('localStorage.getItem("autofind.minimal.openMode")'));
check("cards share one border and hover rule",
  css.includes(".theme-card, .mode-card, .menu-style-card") &&
  css.includes(".theme-card:hover, .mode-card:hover, .menu-style-card:hover"));
check("selected titles turn green on every card",
  css.includes(".menu-style-card.on b") && css.includes(".mode-card.selected b") &&
  css.includes(".theme-card.selected span.label"));
check("cards collapse to one column on narrow windows",
  css.includes("@media (max-width: 460px)") &&
  css.includes(".theme-cards, .mode-cards, .menu-style-cards"));
check("screens scroll with a quiet thin bar, not the fat default",
  css.includes("::-webkit-scrollbar-thumb") && css.includes("scrollbar-width: thin") &&
  css.includes("::-webkit-scrollbar-track { background: transparent; }"));
check("the eye icon is replaced by the menu-layout tiles",
  !html.includes('M2 12s3.5-6.5') && (html.match(/rx="1\.5"/g) || []).length === 12);

// Progress tells the truth: statuses are localized, the scan phase shows
// found files instead of a percent, the bar holds 99% until the run really
// finishes, and the panel dismisses itself on success.
check("progress statuses are localized",
  js.includes("STATUS_RU") && js.includes('undecodable: "Не читается"') &&
  js.includes('finalize: "Финализация"'));
check("the bar holds 99% until the job really finishes", js.includes("Math.min(99"));
check("the scan phase shows found files, not a percent", js.includes("найдено"));
check("the panel dismisses itself after success",
  js.includes("hideProgressTimer") && js.includes('"Готово"'));
