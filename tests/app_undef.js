#!/usr/bin/env node
"use strict";
/* Names read in admin/web/app.js must be declared in that file or be a known global.
   No package: this scanner is the whole check. It catches a deleted binding that is
   still read, which is what dropped the Bots header actions in 0.13.37. */
const fs = require("fs");

const GLOBALS = [
  "Array", "Boolean", "Date", "Error", "JSON", "Math", "Number", "Object", "Promise", "RegExp",
  "String", "Symbol", "Map", "Set", "WeakMap", "WeakSet", "Uint8Array", "Uint32Array", "Int8Array",
  "DataView", "TextEncoder", "TextDecoder", "Intl", "ArrayBuffer",
  "parseInt", "parseFloat", "isNaN", "isFinite", "NaN", "Infinity", "undefined",
  "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "atob", "btoa",
  "console", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
  "queueMicrotask", "requestAnimationFrame", "cancelAnimationFrame", "structuredClone",
  "window", "document", "location", "navigator", "history", "fetch", "crypto", "performance",
  "localStorage", "sessionStorage", "Image", "File", "Blob", "FormData", "URL", "URLSearchParams",
  "FileReader", "Headers", "Request", "Response", "WebSocket", "AbortController",
  "Event", "CustomEvent", "KeyboardEvent", "MouseEvent", "PointerEvent", "FocusEvent",
  "MutationObserver", "self", "globalThis", "confirm", "alert", "prompt", "CSS",
  "DEPLOY_PINS", // admin/web/deploy-pins.js, loaded before app.js
  "module"       // the node export at the bottom of app.js
];

const KEYWORDS = new Set([
  "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do",
  "else", "export", "extends", "finally", "for", "function", "if", "import", "in", "instanceof",
  "new", "return", "super", "switch", "this", "throw", "try", "typeof", "var", "void", "while",
  "with", "yield", "let", "static", "await", "of", "async"
]);

function isIdStart(c) { return /[A-Za-z_$]/.test(c); }
function isId(c) { return /[A-Za-z0-9_$]/.test(c); }

function tokenize(src) {
  const tokens = [];
  let i = 0;
  const n = src.length;
  const mode = ["code"];
  const exprDepth = [];
  let last = null;
  const regexBefore = new Set([
    undefined, "(", "[", "{", ",", ";", ":", "?", "=", "!", "&", "|", "^", "~", "+", "-", "*", "/", "%",
    "<", ">", "return", "throw", "case", "do", "else", "typeof", "void", "delete", "in", "of",
    "instanceof", "new", "await", "yield", "&&", "||", "??", "=>"
  ]);
  function push(type, value) {
    tokens.push({type, value});
    last = type === "kw" || type === "punct" ? value : type;
  }
  while (i < n) {
    const c = src[i];
    if (mode[mode.length - 1] === "template") {
      if (c === "\\") { i += 2; continue; }
      if (c === "`") { mode.pop(); i++; push("str", "`"); continue; }
      if (c === "$" && src[i + 1] === "{") { mode.push("code"); exprDepth.push(0); i += 2; push("punct", "${"); continue; }
      i++;
      continue;
    }
    if (c === " " || c === "\n" || c === "\r" || c === "\t") { i++; continue; }
    if (c === "/" && src[i + 1] === "/") { while (i < n && src[i] !== "\n") i++; continue; }
    if (c === "/" && src[i + 1] === "*") {
      i += 2;
      while (i < n && !(src[i] === "*" && src[i + 1] === "/")) i++;
      i += 2;
      continue;
    }
    if (c === "'" || c === '"') {
      const q = c;
      i++;
      while (i < n && src[i] !== q) { if (src[i] === "\\") i++; i++; }
      i++;
      push("str", q);
      continue;
    }
    if (c === "`") { mode.push("template"); i++; push("str", "`"); continue; }
    if (isIdStart(c)) {
      const start = i++;
      while (i < n && isId(src[i])) i++;
      const word = src.slice(start, i);
      push(KEYWORDS.has(word) ? "kw" : "id", word);
      continue;
    }
    if ((c >= "0" && c <= "9") || (c === "." && src[i + 1] >= "0" && src[i + 1] <= "9")) {
      i++;
      while (i < n && /[0-9a-fA-FxX._eE+-]/.test(src[i])) i++;
      push("num", "n");
      continue;
    }
    if (c === "/" && regexBefore.has(last)) {
      i++;
      while (i < n && src[i] !== "/" && src[i] !== "\n") { if (src[i] === "\\") i++; i++; }
      if (src[i] === "/") i++;
      while (i < n && /[gimsuy]/.test(src[i])) i++;
      push("regex", "/");
      continue;
    }
    if (c === "{" && mode[mode.length - 1] === "code" && exprDepth.length && mode.includes("template")) {
      exprDepth[exprDepth.length - 1]++;
    }
    if (c === "}" && mode[mode.length - 1] === "code" && exprDepth.length && mode[mode.length - 2] === "template") {
      if (exprDepth[exprDepth.length - 1] === 0) {
        mode.pop();
        exprDepth.pop();
        i++;
        push("punct", "}");
        continue;
      }
      exprDepth[exprDepth.length - 1]--;
    }
    const two = src.slice(i, i + 2);
    if (["=>", "==", "!=", "<=", ">=", "&&", "||", "??", "++", "--", "+=", "-=", "?.", "..."].includes(two)) {
      i += 2;
      push("punct", two);
      continue;
    }
    i++;
    push("punct", c);
  }
  return tokens;
}

function check(src) {
  const tokens = tokenize(src);
  const names = new Set(GLOBALS);
  const braces = [];
  let mode = null;
  let depth = 0;
  for (let t = 0; t < tokens.length; t++) {
    const tok = tokens[t], prev = tokens[t - 1], next = tokens[t + 1];
    if (tok.type === "punct" && tok.value === "{") {
      const p = prev ? prev.value : "";
      braces.push(["=", "(", "[", ",", "return", ":", "?", "&&", "||", "??", "throw"].includes(p) ? "object" : "block");
    } else if (tok.type === "punct" && tok.value === "}") braces.pop();
    if ((mode === "decl" || mode === "array") && tok.type === "punct" && tok.value === "(") depth++;
    if ((mode === "decl" || mode === "array") && tok.type === "punct" && tok.value === ")" && depth > 0) depth--;
    if ((mode === "decl" || mode === "array") && depth === 0 && tok.type === "punct" && tok.value === ";") { mode = null; continue; }
    if ((mode === "decl" || mode === "array") && depth === 0 && tok.type === "kw" && tok.value !== "of" && tok.value !== "in") mode = null;
    if (tok.type === "kw" && (tok.value === "const" || tok.value === "let" || tok.value === "var")) { mode = "decl"; depth = 0; continue; }
    if (tok.type === "kw" && tok.value === "function") { mode = next && next.type === "id" ? "fname" : "param"; depth = 0; continue; }
    if (tok.type === "kw" && tok.value === "catch") { mode = "param"; depth = 0; continue; }
    if (mode === "param" && tok.type === "punct" && tok.value === "(") { depth++; continue; }
    if (mode === "param" && tok.type === "punct" && tok.value === ")") { depth--; if (depth <= 0) mode = null; continue; }
    if (mode === "decl" && tok.type === "punct" && tok.value === "[") { mode = "array"; continue; }
    if (mode === "array" && tok.type === "punct" && tok.value === "]") { mode = "decl"; continue; }
    if (tok.type === "id" && (mode === "fname" || mode === "decl" || mode === "array" || mode === "param")) names.add(tok.value);
    if (mode === "fname") { mode = "param"; depth = 0; }
    if (tok.type === "punct" && tok.value === "=>" && prev && prev.type === "id") names.add(prev.value);
    if (tok.type === "punct" && tok.value === "=>" && prev && prev.value === ")") {
      let d = 0;
      for (let j = t - 1; j >= 0; j--) {
        if (tokens[j].value === ")") d++;
        else if (tokens[j].value === "(") { d--; if (d === 0) break; }
        else if (d === 1 && tokens[j].type === "id" && !(tokens[j + 1] && tokens[j + 1].value === "(")) names.add(tokens[j].value);
      }
    }
  }
  const missing = [];
  braces.length = 0;
  for (let t = 0; t < tokens.length; t++) {
    const tok = tokens[t], prev = tokens[t - 1], next = tokens[t + 1];
    if (tok.type === "punct" && tok.value === "{") {
      const p = prev ? prev.value : "";
      braces.push(["=", "(", "[", ",", "return", ":", "?", "&&", "||", "??", "throw"].includes(p) ? "object" : "block");
      continue;
    }
    if (tok.type === "punct" && tok.value === "}") { braces.pop(); continue; }
    if (tok.type !== "id") continue;
    if (prev && prev.type === "punct" && (prev.value === "." || prev.value === "?.")) continue;
    if (prev && prev.type === "kw" && (prev.value === "function" || prev.value === "const" || prev.value === "let" || prev.value === "var" || prev.value === "catch")) continue;
    const brace = braces[braces.length - 1];
    if (brace === "object" && next && (next.value === ":" || next.value === "(") && prev && prev.type === "punct" && (prev.value === "{" || prev.value === ",")) continue;
    if (next && next.value === "=>") continue;
    if (!names.has(tok.value)) missing.push(tok.value);
  }
  return missing;
}

function main(argv) {
  const path = argv[2];
  if (!path) { process.stderr.write("usage: app_undef.js <file>\n"); process.exit(2); }
  const seen = new Set();
  const names = [];
  for (const name of check(fs.readFileSync(path, "utf8"))) {
    if (!seen.has(name)) { seen.add(name); names.push(name); }
  }
  if (names.length) { process.stdout.write(names.join("\n") + "\n"); process.exit(1); }
}

if (require.main === module) main(process.argv);
module.exports = {check};
