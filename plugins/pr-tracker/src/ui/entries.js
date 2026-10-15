// PR Tracker UI bundle entries (staging scaffold).
//
// Pure model builders behind the manifest `exportName`s (`PrSidebarPage`,
// `PrDetailTab`, `PrCountsWidget`). The host prebuilds this directory to
// `./dist/ui.js`, dynamically imports it, and mounts each slot's export with
// the SDK components (`DataTable`, `StatusBadge`); data arrives via the
// `usePluginData` → worker `getData` bridge, actions via `usePluginAction` →
// `performAction`. Nothing here touches the network or the board.

import {
  TABLE_COLUMNS,
  applyFilters,
  defaultFilter,
  labelFor,
  toRow,
  widgetCounts,
} from "./view-model.js";

/** Sidebar "Pull Requests" page model: columns, filtered rows, counts. */
export function PrSidebarPage({ records, filter = defaultFilter(), nowMs = Date.now() } = {}) {
  const rows = applyFilters(records, filter).map((r) => toRow(r, nowMs));
  return {
    columns: [...TABLE_COLUMNS],
    rows,
    counts: widgetCounts(records),
    totalCount: rows.length,
    activeFilter: { ...filter },
  };
}

/**
 * Compliance checklist for one snapshot (decision-core shape): each item is
 * `{ key, label, ok }`. Pure read of the latest collector snapshot.
 */
export function checklistFor(snapshot = {}) {
  const openThreads = Array.isArray(snapshot.threads) ? snapshot.threads.length : 0;
  return [
    { key: "open", label: "PR is open", ok: snapshot.state === "open" && snapshot.merged !== true },
    { key: "ci", label: "CI is green", ok: snapshot.ci?.rollup === "green" },
    { key: "mergeable", label: "Mergeable (no conflicts)", ok: snapshot.mergeable === "mergeable" },
    { key: "review", label: "Review approved", ok: snapshot.reviewDecision === "approved" },
    { key: "threads", label: "No unresolved threads", ok: openThreads === 0 },
  ];
}

/** Task detail tab model: this task's PRs plus each PR's checklist. */
export function PrDetailTab({ records, snapshots = {}, taskId, nowMs = Date.now() } = {}) {
  const mine = records.filter((r) => r.taskId === taskId);
  return mine.map((r) => ({
    row: toRow(r, nowMs),
    statusLabel: labelFor(r.status),
    checklist: checklistFor(snapshots[`${r.repo}#${r.number}`]),
  }));
}

/** Dashboard widget model: needs-us / waiting / red-CI counts. */
export function PrCountsWidget({ records } = {}) {
  return { ...widgetCounts(records) };
}
