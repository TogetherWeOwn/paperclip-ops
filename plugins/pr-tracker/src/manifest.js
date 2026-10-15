// PR Tracker plugin manifest (staging scaffold).
//
// Validated against `pluginManifestV1Schema` at export time in the public
// repo. This staging copy mirrors the shape so the export is mechanical:
// fill in, validate, ship. Rollback for everything under `plugins/pr-tracker/`
// is delete-the-directory: no core patch, no live wiring.
//
// PHASE GATE (cleared 2026-10-04): UI slot shapes verified against the live
// plugin SDK (`PLUGIN_UI_SLOT_TYPES`, `pluginUiSlotDeclarationSchema`):
// `sidebar` / `detailTab` / `dashboardWidget` are all valid slot types;
// `detailTab` requires `entityTypes` (here `["issue"]`); `routePath` must be
// a lowercase single-segment slug; `entrypoints.ui` is required whenever
// `ui.slots` are declared. Only the worker (`pollPrs`) was declared before.

export const PLUGIN_ID = "togetherweown.pr-tracker";
export const PLUGIN_VERSION = "0.4.0";
export const PLUGIN_API_VERSION = "v1";

export const JOB_KEYS = Object.freeze({
  pollPrs: "pollPrs",
});

/**
 * Worker capabilities. Read-only GitHub access via the existing org App
 * secret refs; no new credentials. `issues.create` is bounded to
 * tracking-card reuse (one card per PR, never duplicates).
 */
export const WORKER_CAPABILITIES = Object.freeze([
  "issues.read",
  "issues.create",
  "issues.wakeup",
  "issue.relations.read",
  "plugin.state.read",
  "plugin.state.write",
  "secrets.read-ref",
  "http.outbound",
  "jobs.schedule",
  "database.namespace.read",
  "activity.log.write",
  "metrics.write",
]);

/**
 * UI registration capabilities. The host mounts each declared slot's
 * `exportName` from the prebuilt UI bundle (`entrypoints.ui`).
 */
export const UI_CAPABILITIES = Object.freeze([
  "ui.sidebar/register",
  "ui.detailTab/register",
  "ui.dashboardWidget/register",
]);

/**
 * UI slots, per `pluginUiSlotDeclarationSchema`:
 * `{ type, id, displayName, exportName, entityTypes?, routePath?, order? }`.
 * The sidebar page mirrors the Tasks page (`DataTable` + `StatusBadge`);
 * the detail tab shows a task's PRs plus the compliance checklist; the
 * widget shows needs-us / waiting / red-CI counts.
 */
export const UI_SLOTS = Object.freeze([
  {
    type: "sidebar",
    id: "pr-list",
    displayName: "Pull Requests",
    exportName: "PrSidebarPage",
    routePath: "pull-requests",
  },
  {
    type: "detailTab",
    id: "pr-detail",
    displayName: "Pull Requests",
    exportName: "PrDetailTab",
    entityTypes: ["issue"],
  },
  {
    type: "dashboardWidget",
    id: "pr-counts",
    displayName: "PR Tracker",
    exportName: "PrCountsWidget",
  },
]);

export const manifest = Object.freeze({
  id: PLUGIN_ID,
  apiVersion: PLUGIN_API_VERSION,
  version: PLUGIN_VERSION,
  displayName: "PR Tracker",
  description:
    "Watches open pull requests every 2 minutes and wakes the owning task with a precise change digest, plus repeating compliance checks so nothing is forgotten.",
  author: "TogetherWeOwn",
  categories: ["automation"],
  capabilities: [...WORKER_CAPABILITIES, ...UI_CAPABILITIES],
  entrypoints: { worker: "./dist/worker.js", ui: "./dist/ui.js" },
  ui: { slots: [...UI_SLOTS] },
  jobs: [
    {
      jobKey: JOB_KEYS.pollPrs,
      displayName: "Poll pull requests",
      description:
        "Read-only REST collection with ETag conditional requests (304s are free) over owner-authored upstream PRs plus all open org PRs, then feed the decision core. Every 2 minutes so event-to-wake stays within 5 minutes.",
      schedule: "*/2 * * * *",
    },
  ],
});
