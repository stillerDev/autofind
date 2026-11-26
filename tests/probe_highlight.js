const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(path.resolve("web/app.js"), "utf8");
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
}
const ctx = { RegExp, Set, String };
vm.createContext(ctx);
const fns = vm.runInContext(
  `${extract("esc")}\n${extract("highlight")}\n({esc,highlight})`, ctx
);
const text = process.argv[2] || "find freetileposition FindfreetilePosition";
const query = process.argv[3] || "find";
console.log(fns.highlight(text, query));
