// Validate Mermaid diagrams with the real parser (used by test_outputs.py).
// usage: node mermaid_parse.mjs <node_modules parent dir> < diagrams.json
// prints a JSON array of null (ok) or an error message per diagram.
const base = process.argv[2];
const { JSDOM } = await import(base + "/node_modules/jsdom/lib/api.js");
const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const mermaid = (await import(base + "/node_modules/mermaid/dist/mermaid.core.mjs")).default;
let input = "";
for await (const chunk of process.stdin) input += chunk;
const results = [];
for (const text of JSON.parse(input)) {
  try { await mermaid.parse(text); results.push(null); }
  catch (e) { results.push((e.message || String(e)).split("\n").slice(0, 3).join(" ")); }
}
console.log(JSON.stringify(results));
