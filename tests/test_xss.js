// Runs `highlight()` in isolation: indexed file content must never reach
// innerHTML as executable markup. No jsdom needed — the two functions are
// pure string helpers.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`${name} has no closing brace`);
}

const highlight = vm.runInNewContext(
  `${extract("esc")}\n${extract("highlight")}\nhighlight`,
  { RegExp, Set, String },
  { timeout: 1000 }
);

const result = highlight('<img src=x onerror=alert(1)> auth', "auth");
// The literal text "onerror=..." is fine once `<` and `>` are entities.
if (result.includes("<img") || result.includes("<script")) {
  throw new Error(`unescaped markup in highlight(): ${result}`);
}
if (!result.includes("&lt;img")) {
  throw new Error(`expected escaped markup, got: ${result}`);
}

const oneLetter = highlight("a &amp; b", "a", 1);
if (!oneLetter.includes("<mark>a</mark>") || !oneLetter.includes("&amp;")) {
  throw new Error(`one-letter highlight broke an entity: ${oneLetter}`);
}
const defaultMin = highlight("abc", "a");
if (defaultMin.includes("<mark>")) {
  throw new Error(`default highlight should ignore one-letter terms: ${defaultMin}`);
}
console.log("highlight() escapes indexed content and survives 1-letter queries: OK");
