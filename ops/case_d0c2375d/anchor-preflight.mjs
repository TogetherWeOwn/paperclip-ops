// Preflight only: load the mutation gate's mutant table and check that every
// `from` anchor still matches EXACTLY once after the merge. Runs no tests.
// A merge that re-wraps or duplicates a guarded region shows up here as 0 or 2.
import { readFile } from "node:fs/promises";
import { join, dirname } from "node:path";

const root = process.argv[2];
const gatePath = join(root, "scripts/mutation-gate.mjs");
const src = await readFile(gatePath, "utf8");

const start = src.indexOf("const mutants = [");
const endMarker = "\n];\n";
const end = src.indexOf(endMarker, start);
if (start < 0 || end < 0) throw new Error("could not locate mutants array");
const arrayLiteral = src.slice(start + "const mutants = ".length, end + 2);

const mutants = (0, eval)(`(${arrayLiteral})`);

let bad = 0;
const cache = new Map();
for (const m of mutants) {
  const p = join(root, m.file);
  if (!cache.has(p)) cache.set(p, await readFile(p, "utf8"));
  const n = cache.get(p).split(m.from).length - 1;
  if (n !== 1) {
    console.error(`ANCHOR BROKEN: ${m.name} matched ${n}x in ${m.file}`);
    bad += 1;
  }
}
console.log(`anchors: ${mutants.length - bad}/${mutants.length} match exactly once`);
process.exit(bad > 0 ? 1 : 0);
