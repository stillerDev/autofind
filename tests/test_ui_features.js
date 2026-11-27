// UI convenience helpers — no jsdom. The pure-helper region of web/app.js is
// evaluated in a VM with tiny stubs, so the tests run against the real source.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

const START = "/* ------------------------------------------------- convenience: pure helpers */";
const END = "/* ------------------------------------------------------------------ state */";
const start = source.indexOf(START);
const end = source.indexOf(END);
if (start < 0 || end < 0 || end <= start) {
  throw new Error("convenience helper region not found in web/app.js");
}
const region = source.slice(start, end);

const state = {
  folder: "C:\\Docs",
  metaTags: ["work", "home"],
  metaCollections: [{ id: 1, name: "Reading" }],
};
const ctx = {
  state,
  ui: (key) => key,
  t: (key) => key,
  currentLang: () => "en",
  recentQueries: () => ['in:"C:\\Downloads" bug'],
  esc: (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"),
  highlight: (text) => String(text ?? ""),
  console, Date, Math, JSON, Number, String, RegExp, Set, Array, Object,
};
vm.createContext(ctx);
vm.runInContext(`${region}\n;globalThis.H = {
  naturalDateCompile, filterTokens, tokenCompletions, tokenValues,
  numberedCode, fmtAge, escapeRe, ocrWordsNotLoaded: true,
};`, ctx);
const H = ctx.H;

function check(name, condition, detail) {
  if (!condition) throw new Error(`${name} failed${detail ? `: ${detail}` : ""}`);
  console.log(`${name}: OK`);
}

// --- 3: natural-language dates -------------------------------------------
check("relative RU date compiles", H.naturalDateCompile("отчёт за прошлую неделю").query.includes("modified:<7d"),
  H.naturalDateCompile("отчёт за прошлую неделю").query);
check("relative EN date compiles", H.naturalDateCompile("bugs yesterday").query.includes("modified:1d..2d"));
check("absolute date compiles", /modified:>\d+d/.test(H.naturalDateCompile("notes after 1 march").query),
  H.naturalDateCompile("notes after 1 march").query);
check("ISO date direction compiles", /modified:<\d+d/.test(H.naturalDateCompile("до 2024-01-01").query),
  H.naturalDateCompile("до 2024-01-01").query);
check("explicit modified is not double-compiled",
  H.naturalDateCompile("modified:<7d вчера").query === "modified:<7d вчера");
check("date phrase is consumed",
  !/прошлую|yesterday|after 1 march/i.test(H.naturalDateCompile("report за прошлую неделю after 1 march").query));

// --- 2: token parsing ----------------------------------------------------
const tokens = H.filterTokens('report in:"C:\\Docs" ext:py');
check("tokens parsed", tokens.length === 2, JSON.stringify(tokens));
check("quoted value kept", tokens[0].kind === "in" && tokens[0].value === "C:\\Docs", JSON.stringify(tokens[0]));
check("second token parsed", tokens[1].kind === "ext" && tokens[1].value === "py");
check("token span selects exact text", 'report in:"C:\\Docs" ext:py'.slice(tokens[1].start, tokens[1].end) === "ext:py");

// --- 1: token autocomplete -----------------------------------------------
check("key completion", H.tokenCompletions("ex").some((item) => item.value === "ext:"));
check("value completion", H.tokenCompletions("ext:p").some((item) => item.value === "ext:py"));
check("completion replaces only the typed word", H.tokenCompletions("report ext:p")[0].start === 7);
check("tag values come from state", H.tokenValues("tag").join(",") === "work,home");

// --- 6: code line numbers ------------------------------------------------
const numbered = H.numberedCode("alpha\nbeta", "", 5);
check("first number", numbered.includes(">5<"), numbered);
check("second number", numbered.includes(">6<"), numbered);
check("code-line wrapper", numbered.includes('class="code-line"'));
check("age formatting", H.fmtAge(3661) === "1h", H.fmtAge(3661));
check("regex escaping", H.escapeRe("a.b") === "a\\.b", H.escapeRe("a.b"));

// --- static integration guards ------------------------------------------
for (const needle of [
  "renderQueryChips()", "wireFeatures()", "/api/self-test", "/api/log?lines=",
  "/api/ocr-boxes", "/api/note", "/api/audit", "data-pin-best", "within-input",
  "fresh-badge", "tag-dropdown", "ocr-word", "/api/exclusions", "/api/portable",
  "/api/history?limit=", "/api/similar-files",
  "Array.isArray(value) ? value : []",
  "state.searchSeq += 1",
  "new Set((data.hits || []).map((hit) => hit.file_id))",
  "function populateFilterMenus",
  "state.previewSeq", "state.similarSeq",
  "input.dataset.dirty = \"1\"",
  "panel.sealStage\" + rawStage.charAt(0).toUpperCase()",
  "input.dataset.dirty === \"1\" || (input.value && !input.dataset.clean)",
]) {
  check(`source contains ${needle}`, source.includes(needle));
}
check("hover preview is wired at init", /\n\s{2}wireHover\(\);/.test(source));
check("no undefined renderWithinBar call", !source.includes("renderWithinBar()"));
check("card tag-new click is exempt from annotate auto-close",
  source.includes("#annotate, [data-tag-add], [data-tag-new]"));
check("content click keeps annotate ids for tag-new",
  source.includes('!event.target.closest("#annotate, [data-tag-new]")'));
