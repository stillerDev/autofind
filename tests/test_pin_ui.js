// Verifies that unpinning from the idle screen repaints the pinned list.
// No jsdom: togglePin() is extracted and run with a tiny fake DOM.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

function extract(name) {
  const start = source.indexOf(`async function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`${name} has no closing brace`);
}

(async () => {
  const calls = [];
  const state = {
    hits: [{ file_id: 7, pinned: true }],
    personal: { pins: [{ file_id: 7, name: "old" }] },
  };
  const ctx = {
    state,
    api: async () => ({ pinned: false }),
    $: (id) => (id === "personal" ? {} : null),
    document: { querySelectorAll: () => [] },
    loadPersonal: async (force) => {
      calls.push(`load:${force}`);
      state.personal = { pins: [] };
      return state.personal;
    },
    paintPersonal: () => calls.push("paint"),
    t: (value) => value,
    toast: () => {},
    JSON,
    Number,
  };
  vm.createContext(ctx);
  const togglePin = vm.runInContext(`${extract("togglePin")}\ntogglePin`, ctx);
  await togglePin(7, false);

  if (!calls.includes("load:true") || !calls.includes("paint")) {
    throw new Error(`idle pinned list was not refreshed: ${calls.join(",")}`);
  }
  if (state.personal.pins.length !== 0) {
    throw new Error("pinned list still contains the unpinned file");
  }
  if (state.hits[0].pinned !== false) {
    throw new Error("search-result star was not updated");
  }
  console.log("togglePin() refreshes the pinned list: OK");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
