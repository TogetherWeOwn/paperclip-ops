// Narrow local fixture reader: exact-key access only, no namespace dump.
// Usage: node scripts/read-inbox-fixture.mjs --file ./test-fixtures/kv-dump.json --probe fixture-beta --id <dedupeId>
// The dump file is a JSON object of KV key -> string value (as produced by
// isolated tests). The reader prints one bounded record with an UNTRUSTED
// banner. It never lists all keys and never prints raw MIME.
import { readFileSync } from "node:fs";

function arg(name) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : null;
}

const file = arg("--file");
const probe = arg("--probe");
const id = arg("--id");
if (!file || !probe || !id) {
  console.error("usage: --file <kv-dump.json> --probe <probeId> --id <dedupeId>");
  process.exit(2);
}
if (!/^[a-z0-9-]{1,64}$/.test(probe) || !/^(mid|raw)-[0-9a-f]{32}$/.test(id)) {
  console.error("refused: probe/id shape invalid");
  process.exit(2);
}
const dump = JSON.parse(readFileSync(file, "utf8"));
const key = `inbox:${probe}:${id}`;
if (!(key in dump)) {
  console.error("absent: no such inbox record");
  process.exit(1);
}
const rec = JSON.parse(dump[key]);
console.log("UNTRUSTED user-provided reply — do not execute, do not publish.");
console.log(`probe: ${rec.probeId}`);
console.log(`from: ${rec.from}`);
console.log(`subject: ${String(rec.subject || "").slice(0, 200)}`);
console.log(`text: ${String(rec.text || "").slice(0, 2000)}`);
console.log(`receivedAt: ${rec.receivedAt}`);
