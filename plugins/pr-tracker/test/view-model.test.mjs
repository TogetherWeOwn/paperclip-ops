import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PR_STATUSES,
  badgeFor,
  labelFor,
  isNeedsUs,
  defaultFilter,
  applyFilters,
  widgetCounts,
  TABLE_COLUMNS,
  toRow,
} from "../src/ui/view-model.js";
import { manifest } from "../src/manifest.js";

const NOW = 1_700_000_000_000;

function rec(over = {}) {
  return {
    repo: "org/repo",
    number: 42,
    title: "Fix the thing",
    prUrl: "https://example.invalid/org/repo/pull/42",
    kind: "org",
    status: "ours-to-do",
    greptile: 5,
    ciSummary: "9/9 green",
    openThreads: 1,
    uncheckedBoxes: 0,
    lastActivityMs: NOW - 3_600_000,
    taskId: "task-1",
    taskUrl: "https://example.invalid/tasks/1",
    owner: "agent-a",
    openedAtMs: NOW - 86_400_000,
    ...over,
  };
}

test("seven statuses with labels and badges", () => {
  assert.equal(PR_STATUSES.length, 7);
  assert.equal(labelFor("ci-red"), "CI red");
  assert.equal(badgeFor("ci-red"), "error");
  assert.equal(badgeFor("conflict"), "error");
  assert.equal(badgeFor("merged"), "ok");
  assert.equal(badgeFor("bogus"), "info");
});

test("needs-us covers the four action statuses", () => {
  assert.equal(isNeedsUs("ours-to-do"), true);
  assert.equal(isNeedsUs("ci-red"), true);
  assert.equal(isNeedsUs("changes-requested"), true);
  assert.equal(isNeedsUs("conflict"), true);
  assert.equal(isNeedsUs("waiting-maintainer"), false);
  assert.equal(isNeedsUs("merged"), false);
  assert.equal(isNeedsUs("closed"), false);
});

test("default filter is needs-us", () => {
  assert.deepEqual(defaultFilter(), { needsUs: true });
});

test("filters: status, repo, kind, owner, text", () => {
  const rows = [
    rec({ status: "ci-red", repo: "org/a", kind: "org", owner: "x", title: "Alpha fix" }),
    rec({ status: "waiting-maintainer", repo: "up/b", kind: "upstream", owner: "y", title: "Beta feat" }),
    rec({ status: "merged", repo: "org/a", kind: "org", owner: "x", title: "Gamma docs" }),
  ];
  assert.equal(applyFilters(rows, { needsUs: true }).length, 1);
  assert.equal(applyFilters(rows, { status: "merged" }).length, 1);
  assert.equal(applyFilters(rows, { repo: "org/a" }).length, 2);
  assert.equal(applyFilters(rows, { kind: "upstream" }).length, 1);
  assert.equal(applyFilters(rows, { owner: "y" }).length, 1);
  assert.equal(applyFilters(rows, { text: "alpha" }).length, 1);
  assert.equal(applyFilters(rows, { text: "ORG/A" }).length, 2);
  assert.equal(applyFilters(rows, {}).length, 3);
  assert.equal(
    applyFilters(rows, { needsUs: true, repo: "org/a" }).length,
    1,
  );
});

test("widget counts", () => {
  const rows = [
    rec({ status: "ci-red" }),
    rec({ status: "conflict" }),
    rec({ status: "waiting-maintainer" }),
    rec({ status: "merged" }),
  ];
  assert.deepEqual(widgetCounts(rows), { needsUs: 2, waiting: 1, redCi: 1 });
});

test("table has the twelve spec columns in order", () => {
  assert.deepEqual([...TABLE_COLUMNS], [
    "repo", "title", "kind", "status", "greptile", "ciSummary",
    "openThreads", "uncheckedBoxes", "lastActivity", "linkedTask",
    "owner", "age",
  ]);
});

test("toRow projects badge, links, and age", () => {
  const row = toRow(rec({ status: "ci-red" }), NOW);
  assert.equal(row.badge, "error");
  assert.equal(row.prUrl, "https://example.invalid/org/repo/pull/42");
  assert.equal(row.linkedTask, "task-1");
  assert.equal(row.age, 86_400_000);
});

test("manifest declares ui entrypoint plus three slots", () => {
  assert.equal(manifest.entrypoints.ui, "./dist/ui.js");
  assert.equal(manifest.ui.slots.length, 3);
  const types = manifest.ui.slots.map((s) => s.type).sort();
  assert.deepEqual(types, ["dashboardWidget", "detailTab", "sidebar"]);
  const tab = manifest.ui.slots.find((s) => s.type === "detailTab");
  assert.deepEqual(tab.entityTypes, ["issue"]);
  const side = manifest.ui.slots.find((s) => s.type === "sidebar");
  assert.equal(side.routePath, "pull-requests");
  assert.ok(!side.routePath.includes("/") && side.routePath === side.routePath.toLowerCase());
  for (const s of manifest.ui.slots) {
    assert.ok(s.id && s.displayName && s.exportName);
  }
  assert.ok(manifest.capabilities.includes("ui.sidebar/register"));
  assert.ok(manifest.jobs.some((j) => j.jobKey === "pollPrs" && j.schedule === "*/2 * * * *"));
});
