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
check("theme button has sun and moon icons",
  html.includes('class="icon-sun"') && html.includes('class="icon-moon"'));
check("theme button precedes gear", html.indexOf("theme-btn") < html.indexOf('id="gear"'));
check("light theme tokens", css.includes('[data-theme="light"]'));
check("theme choice is persisted", js.includes("autofind.minimal.theme"));
check("gear is a standard filled cog", html.includes("M19.14 12.94c"));
