// Static contract for the minimal menu: the reference palette, the flat card
// grid, no inline scripts (the server CSP rejects them) and real API wiring.
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/minimal.html"), "utf8");
const css = fs.readFileSync(path.join(root, "web/minimal.css"), "utf8");
const js = fs.readFileSync(path.join(root, "web/minimal.js"), "utf8");

function check(name, condition) {
  if (!condition) throw new Error(`${name} failed`);
  console.log(`${name}: OK`);
}

// Palette taken from the reference screenshot.
check("canvas is near-black", css.includes("--bg: #0e0e10"));
check("card surface is dark gray", css.includes("--surface: #1b1b1e"));
check("status green matches the app accent", css.includes("--green: #3ecf8e"));
check("flat cards, no thumbnail layer", !js.includes("<img src=") && !css.includes(".thumb img"));

// Markup and CSP.
for (const id of ["q", "gear", "menu", "grid", "status", "refresh", "details"]) {
  check(`html has #${id}`, html.includes(`id="${id}"`));
}
check("no inline script tag", !/<script(?![^>]*\bsrc=)/.test(html));
check("script is same-origin", html.includes('<script src="/minimal.js">'));

// Behaviour.
check("recent feed", js.includes('"/api/recent?limit=48"'));
check("search feed", js.includes("/api/search?q="));
check("open and reveal", js.includes("/api/${reveal ? \"reveal\" : \"open\"}"));
check("details from preview", js.includes("/api/preview?id="));
check("keyboard navigation", ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"]
  .every((key) => js.includes(`case "${key}"`)));
check("html is escaped before innerHTML", js.includes("const esc = (value)"));
check("reduced motion respected", css.includes("prefers-reduced-motion: reduce"));
check("responsive grid", css.includes("grid-template-columns: repeat(auto-fill"));

// Light theme toggle sits to the left of the gear and persists its choice.
check("theme button exists", html.includes('id="theme-btn"'));
check("theme button is labelled", html.includes('aria-label="Светлая тема"'));
check("theme button keeps the sun and can hide its rays",
  html.includes('class="icon-sun"') && html.includes('class="sun-rays"'));
check("theme button carries a crescent for light mode", html.includes('class="moon-core"'));
check("theme button precedes gear", html.indexOf("theme-btn") < html.indexOf('id="gear"'));
check("light theme tokens", css.includes('[data-theme="light"]'));
check("theme choice is persisted", js.includes("autofind.minimal.theme"));
check("gear is a thin hollow outline cog",
  html.includes("M19.40 12.00") && html.includes('stroke-width="1.3"'));

// Self-contained settings: every switch stays on this page and reaches an API.
for (const id of ["settings-panel", "set-watch", "set-idle", "set-autostart",
                  "set-privacy", "set-theme", "set-categories", "set-purge"]) {
  check(`settings has #${id}`, html.includes(`id="${id}"`));
}
check("settings opens in place", js.includes("openSettings(true)"));
for (const route of ["/api/watch", "/api/autostart", "/api/privacy/categories",
                     "/api/privacy/purge"]) {
  check(`settings calls ${route}`, js.includes(route));
}
check("switches are styled", css.includes('.set-row input[type="checkbox"]'));
check("dark theme drops the sun", css.includes(".sun-core { display: none"));
check("dark theme shows the crescent", css.includes(".moon-core { display: block"));
check("theme knob moves right in dark mode", css.includes("translateX(18px)"));
check("search pauses while settings are open", js.includes("searchSuspended()"));
check("settings swallow the wheel", js.includes('"wheel"'));
