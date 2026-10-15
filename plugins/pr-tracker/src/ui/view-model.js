// PR Tracker sidebar view-model (pure, dependency-free).
//
// Maps tracked-PR records to `DataTable` rows and `StatusBadge` props, and
// applies the sidebar filters. The host SDK owns rendering; this module owns
// the mapping so it stays offline-testable with fixtures.
//
// A tracked-PR record: `{ repo, number, title, prUrl, kind, status,
// greptile, ciSummary, openThreads, uncheckedBoxes, lastActivityMs,
// taskId, taskUrl, owner, openedAtMs }`, where `kind` is
// `"upstream" | "org"` and `status` is one of PR_STATUSES.

/** Sidebar status vocabulary (7 values, matching the spec). */
export const PR_STATUSES = Object.freeze([
  "ours-to-do",
  "ci-red",
  "changes-requested",
  "conflict",
  "waiting-maintainer",
  "merged",
  "closed",
]);

/** Human labels for the status column + tab headers. */
export const STATUS_LABELS = Object.freeze({
  "ours-to-do": "Ours to do",
  "ci-red": "CI red",
  "changes-requested": "Changes requested",
  conflict: "Conflict",
  "waiting-maintainer": "Waiting on maintainer",
  merged: "Merged",
  closed: "Closed",
});

/**
 * `StatusBadge` mapping. SDK vocabulary is
 * `ok | warning | error | info | pending`; unknown input falls back to
 * `info` (visible, never silent).
 */
export const STATUS_BADGE = Object.freeze({
  "ours-to-do": "warning",
  "ci-red": "error",
  "changes-requested": "warning",
  conflict: "error",
  "waiting-maintainer": "info",
  merged: "ok",
  closed: "info",
});

export function badgeFor(status) {
  return STATUS_BADGE[status] ?? "info";
}

export function labelFor(status) {
  return STATUS_LABELS[status] ?? status;
}

/** Statuses where the ball is in our court. */
export const NEEDS_US = Object.freeze([
  "ours-to-do",
  "ci-red",
  "changes-requested",
  "conflict",
]);

export function isNeedsUs(status) {
  return NEEDS_US.includes(status);
}

/** Saved default filter: "Needs us". */
export function defaultFilter() {
  return { needsUs: true };
}

/**
 * Apply sidebar filters. All clauses are ANDed; absent clauses pass.
 * `text` matches repo, title, and number (case-insensitive).
 */
export function applyFilters(records, filter = {}) {
  const text = (filter.text ?? "").trim().toLowerCase();
  return records.filter((r) => {
    if (filter.needsUs === true && !isNeedsUs(r.status)) return false;
    if (filter.status !== undefined && r.status !== filter.status) return false;
    if (filter.repo !== undefined && r.repo !== filter.repo) return false;
    if (filter.kind !== undefined && r.kind !== filter.kind) return false;
    if (filter.owner !== undefined && r.owner !== filter.owner) return false;
    if (text !== "") {
      const hay = `${r.repo} ${r.title} ${r.number}`.toLowerCase();
      if (!hay.includes(text)) return false;
    }
    return true;
  });
}

/** Dashboard widget counts: needs-us / waiting / red-CI. */
export function widgetCounts(records) {
  let needsUs = 0;
  let waiting = 0;
  let redCi = 0;
  for (const r of records) {
    if (isNeedsUs(r.status)) needsUs += 1;
    if (r.status === "waiting-maintainer") waiting += 1;
    if (r.status === "ci-red") redCi += 1;
  }
  return { needsUs, waiting, redCi };
}

/** `DataTable` column keys for the sidebar page, in display order. */
export const TABLE_COLUMNS = Object.freeze([
  "repo",
  "title",
  "kind",
  "status",
  "greptile",
  "ciSummary",
  "openThreads",
  "uncheckedBoxes",
  "lastActivity",
  "linkedTask",
  "owner",
  "age",
]);

/** Project one record to a table row (derives `age` from `nowMs`). */
export function toRow(record, nowMs = Date.now()) {
  return {
    repo: record.repo,
    title: record.title,
    prUrl: record.prUrl,
    kind: record.kind,
    status: record.status,
    badge: badgeFor(record.status),
    greptile: record.greptile ?? null,
    ciSummary: record.ciSummary ?? "",
    openThreads: record.openThreads ?? 0,
    uncheckedBoxes: record.uncheckedBoxes ?? 0,
    lastActivity: record.lastActivityMs ?? null,
    linkedTask: record.taskId ?? null,
    taskUrl: record.taskUrl ?? null,
    owner: record.owner ?? null,
    age: Math.max(0, nowMs - (record.openedAtMs ?? nowMs)),
  };
}
