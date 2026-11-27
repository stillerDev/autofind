// Undo/redo must move the stack cursor, not append the old query as a new entry.
// The four small history functions are extracted from the real app.js source
// and run with stubs; runSearch is stubbed to mirror its pushFilterHistory call.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

function extract(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`${name} has no closing brace`);
}

const input = { value: "" };
const state = { filterStack: ["alpha", "beta", "gamma"], filterPos: 2 };
const calls = [];
const ctx = {
  state,
  $: () => input,
  document: { getElementById: (id) => ({ disabled: false, id }) },
  updateUndoButtons: () => calls.push("buttons"),
  // Mirrors runSearch: it pushes the query unless the caller opts out.
  runSearch: (options = {}) => {
    calls.push(options.skipHistory ? "search:skip" : "search:push");
    if (!options.skipHistory) ctx.pushFilterHistory(input.value);
  },
  console,
};
vm.createContext(ctx);
for (const name of ["pushFilterHistory", "applyFilterAt", "undoFilter", "redoFilter"]) {
  vm.runInContext(`${extract(name)}\n;globalThis.${name} = ${name};`, ctx);
}

ctx.undoFilter();
if (state.filterPos !== 1 || input.value !== "beta") {
  throw new Error(`undo did not move to the previous query: pos=${state.filterPos} q=${input.value}`);
}
if (state.filterStack.length !== 3) {
  throw new Error(`undo rewrote the stack: ${JSON.stringify(state.filterStack)}`);
}
if (!calls.includes("search:skip")) {
  throw new Error("undo ran a search that pushed a new history entry");
}

ctx.redoFilter();
if (state.filterPos !== 2 || input.value !== "gamma") {
  throw new Error(`redo did not return to the newer query: pos=${state.filterPos} q=${input.value}`);
}
if (state.filterStack.length !== 3) {
  throw new Error(`redo rewrote the stack: ${JSON.stringify(state.filterStack)}`);
}
console.log("undo/redo move the filter cursor without rewriting the stack: OK");
