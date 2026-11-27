// The minimalist menu is a customization option, reachable from both the
// classic settings and the minimal page's own settings panel.
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
const i18n = fs.readFileSync(path.join(root, "web/i18n.js"), "utf8");
const minimalHtml = fs.readFileSync(path.join(root, "web/minimal.html"), "utf8");
const minimalJs = fs.readFileSync(path.join(root, "web/minimal.js"), "utf8");
const minimalCss = fs.readFileSync(path.join(root, "web/minimal.css"), "utf8");

function check(name, condition) {
  if (!condition) throw new Error(`${name} failed`);
  console.log(`${name}: OK`);
}

check("settings has a customization tab", html.includes('data-tab="custom"'));
check("customization pane exists", html.includes('data-pane="custom"'));
check("customization has the minimal menu switch", html.includes('id="minimal-mode"'));
check("minimal switch is wired to the menu", app.includes('"minimal-mode"'));
check("minimal switch opens the minimal menu", app.includes('location.href = "/minimal.html"'));

for (const lang of ["settings.tab.custom", "settings.minimal.title", "settings.minimal.note"]) {
  check(`i18n has ${lang} in all languages`,
    (i18n.match(new RegExp(`"${lang.replace(/\./g, "\\.")}":`, "g")) || []).length >= 3);
}

check("minimal settings has customization", minimalHtml.includes("Кастомизация"));
for (const id of ["set-minimal-menu", "set-compact", "set-ext"]) {
  check(`minimal customization has #${id}`, minimalHtml.includes(`id="${id}"`));
}
check("customization is persisted", minimalJs.includes("autofind.minimal.custom"));
check("compact cards are applied", minimalCss.includes(".grid.compact"));
check("extension labels can be hidden", minimalJs.includes("set-ext"));
