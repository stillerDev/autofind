// The per-card tag dropdown's "+" must tag that card. It calls the same
// submitAnnotate() as the bulk popover, which needs annotateIds to be set first.
// The real branch is extracted from app.js and run with a stubbed event.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");
const match = source.match(
  /const newTag = event\.target\.closest\("\[data-tag-new\]"\);[\s\S]*?\r?\n    \}\r?\n/
);
if (!match) throw new Error("data-tag-new branch not found in app.js");

const submitted = [];
const ctx = {
  event: {
    target: {
      closest: (selector) => selector === "[data-tag-new]"
        ? { dataset: { tagNew: "42" }, parentElement: { querySelector: () => ({ value: "audit-tag" }) } }
        : null,
    },
  },
  annotateIds: [],
  submitAnnotate: (mode, value) => submitted.push({ mode, value, ids: ctx.annotateIds.slice() }),
  Number, String,
};
vm.createContext(ctx);
vm.runInContext(match[0], ctx);

if (submitted.length !== 1) throw new Error(`submitAnnotate was not called: ${JSON.stringify(submitted)}`);
if (submitted[0].mode !== "tag" || submitted[0].value !== "audit-tag") {
  throw new Error(`wrong submit arguments: ${JSON.stringify(submitted[0])}`);
}
if (submitted[0].ids.length !== 1 || submitted[0].ids[0] !== 42) {
  throw new Error(`the clicked file id was not tagged: ${JSON.stringify(submitted[0])}`);
}
console.log("card tag dropdown tags the clicked file: OK");
