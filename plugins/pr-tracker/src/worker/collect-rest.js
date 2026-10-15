// PR Tracker REST collector (staging scaffold).
//
// Maps GitHub REST payloads to decision-core snapshots. Pure mappers plus a
// thin `fetchImpl`-injected transport, so the offline suite drives every path
// with fakes: no network, no credentials.
//
// Design rules (from the core contract + plan):
// - REST only, with ETag conditional requests. GraphQL POST has no ETag.
// - Failed/rate-limited/partial reads resolve to `unknown`, never `silent`.
// - Time still passes on quiet ticks: PRs whose head and `updated_at` are
//   unchanged skip the 4 detail reads but still refresh check-runs (CI
//   completion leaves head SHA and `updated_at` untouched), and a 304 list
//   still refreshes every known PR — so `decideUpstreamWatch` runs each tick
//   and the `nowMs`-driven 4h/4h/7d SLA transitions can fire.
// - A list ETag commits only when every PR read off that list resolved; a
//   failed item holds the list ETag back so the next tick refetches it.
// - Snapshots carry SHAs, fixed-vocabulary states, opaque ids and content
//   hashes only — never upstream text (core safety rule).
// - Review comments map to *threads* (each comment starts/updates a thread;
//   a deleted comment reads as resolved). Issue comments map to *comments*.
//   Resolution state is invisible over REST, so thread resolution converges
//   only on edit/delete; full resolved-state needs an enriched source.
// - Unticked task-list boxes, stale body text and Greptile scores need
//   snapshot fields the core does not have yet — documented, not invented.
// - 403 with an exhausted rate limit is normalized to 429 (retry), so the
//   status classifier never mistakes a quota pause for a credential stop.

import { createHash } from "node:crypto";
import { normalizeSnapshot } from "../decision-core/upstream-watcher.js";

export const API_BASE = "https://api.github.com";
export const USER_AGENT = "paperclip-pr-tracker/0.4.0";

export function sha256Hex(text) {
  return createHash("sha256").update(String(text)).digest("hex");
}

export function toMs(iso) {
  if (iso === null || iso === undefined) return null;
  const ms = Date.parse(iso);
  return Number.isInteger(ms) ? ms : null;
}

export function authorKindOf(user) {
  const login = user?.login ?? "";
  const type = user?.type ?? "";
  if (type === "Bot" || login.endsWith("[bot]")) return "bot";
  return "human";
}

/** next-page URL from a GitHub `Link` header, or null. */
export function parseLinkNext(link) {
  if (!link) return null;
  for (const part of String(link).split(",")) {
    const m = part.match(/<([^>]+)>;\s*rel="next"/);
    if (m) return m[1];
  }
  return null;
}

function rateRemaining(headers) {
  const v = headers?.get?.("x-ratelimit-remaining");
  return v === null || v === undefined ? null : Number(v);
}

/**
 * One GET with optional ETag. Returns `{ status, headers, body }`, where a
 * 403 with an exhausted rate limit is normalized to 429. Throws only on
 * transport failure (caller maps to `unknown`).
 */
export async function fetchJson(fetchImpl, url, { token, etag } = {}) {
  const headers = {
    Accept: "application/vnd.github+json",
    "User-Agent": USER_AGENT,
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (etag) headers["If-None-Match"] = etag;
  const res = await fetchImpl(url, { headers });
  let status = res.status;
  if (status === 403 && rateRemaining(res.headers) === 0) status = 429;
  let body = null;
  if (status !== 304) {
    try {
      body = await res.json();
    } catch {
      body = null;
    }
  }
  return { status, headers: res.headers, body };
}

/** Follow pagination; returns `{ items, pages, etag, ok, rateLimited }`. */
export async function fetchAllPages(fetchImpl, url, { token, etag } = {}) {
  const items = [];
  let next = url;
  let first = true;
  let fetched = 0;
  let lastEtag = null;
  while (next) {
    const res = await fetchJson(fetchImpl, next, {
      token,
      etag: first ? etag : undefined,
    });
    first = false;
    if (res.status === 304 && next === url) {
      return { items: null, pages: null, etag, ok: true, notModified: true };
    }
    if (res.status === 429) return { items, pages: null, etag: null, ok: false, rateLimited: true };
    if (res.status !== 200 || !Array.isArray(res.body)) {
      return { items, pages: null, etag: null, ok: false, status: res.status };
    }
    fetched += 1;
    items.push(...res.body);
    lastEtag = res.headers?.get?.("etag") ?? lastEtag;
    next = parseLinkNext(res.headers?.get?.("link"));
  }
  return {
    items,
    pages: { fetched, total: null },
    etag: lastEtag,
    ok: true,
  };
}

const RED_CONCLUSIONS = new Set(["failure", "timed_out", "action_required", "stale"]);
const GREEN_CONCLUSIONS = new Set(["success", "skipped", "neutral"]);

/** Roll up check runs: any red wins, then pending, then green, else unknown. */
export function ciRollupFromChecks(runs = []) {
  let pending = false;
  let green = false;
  let redSince = null;
  for (const run of runs) {
    const conclusion = run?.conclusion ?? null;
    const status = run?.status ?? "";
    if (conclusion && RED_CONCLUSIONS.has(conclusion)) {
      const started = toMs(run.started_at);
      if (started !== null && (redSince === null || started < redSince)) redSince = started;
      return { rollup: "red", sinceMs: redSince };
    }
    if (conclusion === null || ["queued", "in_progress", "waiting", "pending", "requested"].includes(status)) {
      pending = true;
    } else if (GREEN_CONCLUSIONS.has(conclusion)) {
      green = true;
    } else if (conclusion === "cancelled") {
      pending = true;
    }
  }
  if (pending) return { rollup: "pending", sinceMs: null };
  if (green) return { rollup: "green", sinceMs: null };
  return { rollup: "unknown", sinceMs: null };
}

/** Map the REST PR mergeable fields to the core vocabulary. */
export function mergeableFromPr(pr = {}) {
  if (pr.mergeable === true) return "mergeable";
  if (pr.mergeable === false) return "conflicting";
  if (pr.mergeable_state === "behind") return "behind";
  return "unknown";
}

/** Latest non-dismissed review state wins: changes-requested > approved. */
export function reviewDecisionFromReviews(reviews = []) {
  const live = reviews.filter((r) => r?.state && r.state !== "DISMISSED");
  if (live.some((r) => r.state === "CHANGES_REQUESTED")) return "changes_requested";
  if (live.some((r) => r.state === "APPROVED")) return "approved";
  return "review_required";
}

export function mapThreads(reviewComments = []) {
  return reviewComments.map((c) => ({
    id: `review-${c.id}`,
    createdAtMs: toMs(c.created_at) ?? 0,
    updatedAtMs: toMs(c.updated_at) ?? toMs(c.created_at) ?? 0,
    bodyHash: sha256Hex(c.body ?? ""),
    resolved: false,
  }));
}

export function mapComments(issueComments = []) {
  return issueComments.map((c) => ({
    id: `issue-${c.id}`,
    authorKind: authorKindOf(c.user),
    createdAtMs: toMs(c.created_at) ?? 0,
    updatedAtMs: toMs(c.updated_at) ?? toMs(c.created_at) ?? 0,
    bodyHash: sha256Hex(c.body ?? ""),
  }));
}

/** Split comment/thread times into ours vs maintainer by login set. */
export function responseTimes(issueComments = [], reviewComments = [], ourLogins = []) {
  const ours = new Set(ourLogins.map((l) => String(l).toLowerCase()));
  let lastOur = null;
  let lastMaintainer = null;
  for (const c of [...issueComments, ...reviewComments]) {
    const at = toMs(c.updated_at ?? c.created_at);
    if (at === null) continue;
    if (ours.has(String(c.user?.login ?? "").toLowerCase())) {
      if (lastOur === null || at > lastOur) lastOur = at;
    } else if (lastMaintainer === null || at > lastMaintainer) {
      lastMaintainer = at;
    }
  }
  return { lastOurResponseAtMs: lastOur, lastMaintainerAtMs: lastMaintainer };
}

/** Build one core snapshot from REST payloads (validated by the core). */
export function snapshotFromRest({
  repository,
  pr,
  issueComments = [],
  reviewComments = [],
  reviews = [],
  checkRuns = [],
  ourLogins = [],
  nowMs = Date.now(),
  readOk = true,
  rateLimited = false,
  partial = false,
  pages = null,
}) {
  const ci = ciRollupFromChecks(checkRuns);
  const { lastOurResponseAtMs, lastMaintainerAtMs } = responseTimes(
    issueComments,
    reviewComments,
    ourLogins,
  );
  return {
    repository,
    number: pr.number,
    state: pr.state === "closed" ? "closed" : "open",
    merged: pr.merged_at !== null && pr.merged_at !== undefined ? true : pr.merged === true,
    headSha: pr.head?.sha,
    ci,
    mergeable: mergeableFromPr(pr),
    reviewDecision: reviewDecisionFromReviews(reviews),
    threads: mapThreads(reviewComments),
    comments: mapComments(issueComments),
    lastOurResponseAtMs,
    lastMaintainerAtMs,
    fetchedAtMs: nowMs,
    readOk,
    ...(rateLimited ? { rateLimited: true } : {}),
    ...(partial ? { partial: true } : {}),
    ...(pages ? { pages } : {}),
  };
}

function prKey(repository, number) {
  return `${repository}#${number}`;
}

/**
 * Create the `collect` callback for `setup()`.
 * - `fetchImpl`: `(url, { headers }) => Response` (host HTTP / tests).
 * - `ourLogins`: logins counted as "us" for response-time tracking.
 * - `upstreamAuthor`: login for the upstream search intake; upstream repos
 *   are skipped loudly (diagnostic `unknown`) without it.
 * - `resolveTracking(repository, number, prItem)`: tracking entry or null.
 */
export function createRestCollector({
  fetchImpl,
  ourLogins = [],
  upstreamAuthor = null,
  resolveTracking = () => null,
  apiBase = API_BASE,
} = {}) {
  if (typeof fetchImpl !== "function") throw new Error("createRestCollector: fetchImpl is required");
  const bodies = new Map(); // process-local PR-object cache for 304 reuse

  async function get(url, etag, token) {
    return fetchJson(fetchImpl, url, { token, etag });
  }

  function safeResolveTracking(repo, number) {
    try {
      return resolveTracking(repo, number, null);
    } catch {
      return null;
    }
  }

  function checksUrlFor(repo, headSha) {
    return `${apiBase}/repos/${repo}/commits/${headSha}/check-runs?per_page=100`;
  }

  /**
   * Refresh a known PR without the 4 detail reads: re-fetch check-runs only
   * and re-run decide on a snapshot stamped with this tick's `nowMs`, so the
   * time-driven SLA transitions (4h response, 4h red-CI, 7d ping) fire on
   * quiet PRs. Threads/comments/review state ride the previous snapshot —
   * any change to them bumps `updated_at` and takes the full path instead.
   */
  async function refreshOnePr(repo, number, kind, etags, token, prevAck, nowMs) {
    const key = prKey(repo, number);
    const prevSnap = prevAck?.snapshot;
    const headSha = prevSnap?.headSha;
    if (!prevSnap || typeof headSha !== "string" || headSha.length === 0) {
      return { key, status: 422 };
    }
    const checksUrl = checksUrlFor(repo, headSha);
    const checksRes = await get(checksUrl, etags[checksUrl], token);
    let ci = prevSnap.ci;
    if (checksRes.status === 304) {
      // Check-runs unchanged: keep the previous rollup.
    } else if (checksRes.status === 200) {
      ci = ciRollupFromChecks(checksRes.body?.check_runs ?? []);
      const etag = checksRes.headers?.get?.("etag");
      if (etag) etags[checksUrl] = etag;
    } else if (checksRes.status === 404) {
      ci = { rollup: "unknown", sinceMs: null };
    } else if (checksRes.status === 429) {
      return { key, status: 429 };
    } else {
      return { key, status: checksRes.status ?? 500 };
    }
    // Rebuilt in the collector's field shape (not a spread): stored ack
    // snapshots are normalized (`pages: null`, frozen), and the core
    // validator only accepts `pages` as undefined-or-object.
    const next = {
      repository: prevSnap.repository,
      number: prevSnap.number,
      state: prevSnap.state,
      merged: prevSnap.merged,
      headSha: prevSnap.headSha,
      ci,
      mergeable: prevSnap.mergeable,
      reviewDecision: prevSnap.reviewDecision,
      threads: prevSnap.threads,
      comments: prevSnap.comments,
      lastOurResponseAtMs: prevSnap.lastOurResponseAtMs,
      lastMaintainerAtMs: prevSnap.lastMaintainerAtMs,
      fetchedAtMs: nowMs,
      readOk: prevSnap.readOk,
    };
    try {
      normalizeSnapshot(next);
    } catch {
      return { key, status: 422 };
    }
    // `refresh: true` tells setup() this read saw no PR body: the resolver
    // output below is unenriched, so setup keeps the stored tracking entry
    // (title/prUrl/kind/owner/openedAtMs) instead of overwriting it.
    return {
      key,
      status: 200,
      refresh: true,
      input: { prev: null, next, tracking: safeResolveTracking(repo, number), nowMs },
    };
  }

  /**
   * Confirm a known PR that vanished from the open list: one PR-detail read.
   * Closed PRs resolve to a closed snapshot (the core digests close/merge);
   * still-open PRs (list race) fall back to a check-runs refresh.
   */
  async function closeCheckOnePr(repo, number, kind, etags, token, prevAck, nowMs) {
    const key = prKey(repo, number);
    const prUrl = `${apiBase}/repos/${repo}/pulls/${number}`;
    const cached = bodies.get(prUrl);
    const prRes = await get(prUrl, cached?.etag, token);
    let prBody = null;
    if (prRes.status === 304 && cached) {
      prBody = cached.body;
    } else if (prRes.status === 200 && prRes.body) {
      prBody = prRes.body;
      const etag = prRes.headers?.get?.("etag");
      if (etag) bodies.set(prUrl, { etag, body: prBody });
    } else if (prRes.status === 429) {
      return { key, status: 429 };
    } else {
      return { key, status: prRes.status ?? 500 };
    }
    if (prBody.state !== "closed" && prBody.merged_at == null && prBody.merged !== true) {
      return refreshOnePr(repo, number, kind, etags, token, prevAck, nowMs);
    }
    const prevSnap = prevAck?.snapshot;
    if (!prevSnap) return { key, status: 422 };
    // Same collector-shape rebuild as the refresh path (see above).
    const next = {
      repository: prevSnap.repository,
      number: prevSnap.number,
      state: "closed",
      merged: prBody.merged_at != null || prBody.merged === true,
      headSha: prBody.head?.sha ?? prevSnap.headSha,
      ci: prevSnap.ci,
      mergeable: prevSnap.mergeable,
      reviewDecision: prevSnap.reviewDecision,
      threads: prevSnap.threads,
      comments: prevSnap.comments,
      lastOurResponseAtMs: prevSnap.lastOurResponseAtMs,
      lastMaintainerAtMs: prevSnap.lastMaintainerAtMs,
      fetchedAtMs: nowMs,
      readOk: prevSnap.readOk,
    };
    try {
      normalizeSnapshot(next);
    } catch {
      return { key, status: 422 };
    }
    // A PR body was read, so enrich tracking exactly like `collectOnePr`
    // (no `refresh` marker: this entry is safe to persist over the stored one).
    const resolved = safeResolveTracking(repo, number);
    const tracking = resolved
      ? {
          ...resolved,
          title: resolved.title ?? prBody.title,
          prUrl: resolved.prUrl ?? prBody.html_url,
          kind: resolved.kind ?? kind,
          owner: resolved.owner ?? prBody.user?.login,
          openedAtMs: resolved.openedAtMs ?? toMs(prBody.created_at) ?? nowMs,
        }
      : resolved;
    return {
      key,
      status: 200,
      input: { prev: null, next, tracking, nowMs },
    };
  }

  /**
   * Known-PR numbers for one repo, in stable order, excluding retired PRs:
   * a closed PR whose ack already latched the close (`closedSeen`) needs no
   * further polls — re-reading it every 2 minutes is pure request overhead.
   * Closed-but-unconverged PRs stay in: their close digest still has to
   * deliver (or retry after a failed delivery).
   */
  function knownNumbersFor(repo, prevAcks) {
    const out = [];
    for (const key of Object.keys(prevAcks ?? {})) {
      if (!key.startsWith(`${repo}#`)) continue;
      const number = Number(key.slice(repo.length + 1));
      if (!Number.isInteger(number)) continue;
      const prev = prevAcks[key];
      if (prev?.closedSeen === true && prev?.snapshot?.state === "closed") continue;
      out.push(number);
    }
    return out.sort((a, b) => a - b);
  }

  async function collectOrgRepo(repo, etags, token, prevAcks, reads) {
    const listUrl = `${apiBase}/repos/${repo}/pulls?state=open&per_page=100`;
    const list = await fetchAllPages(fetchImpl, listUrl, { token, etag: etags[listUrl] });
    const nowMs = Date.now();
    if (list.notModified) {
      // List unchanged, but wall-clock moved: refresh every known PR so
      // SLA clocks and red-CI transitions still evaluate this tick.
      for (const number of knownNumbersFor(repo, prevAcks)) {
        reads.push(
          await refreshOnePr(repo, number, "org", etags, token, prevAcks[prKey(repo, number)], nowMs),
        );
      }
      return;
    }
    if (!list.ok) {
      reads.push({ key: `scope:${repo}`, status: list.rateLimited ? 429 : (list.status ?? 500) });
      return;
    }
    let allResolved = true;
    const listed = new Set();
    const note = (read) => {
      reads.push(read);
      if (read.status !== 200 || !read.input) allResolved = false;
    };
    for (const item of list.items ?? []) {
      const key = prKey(repo, item.number);
      listed.add(item.number);
      const prev = prevAcks?.[key];
      const headSha = item.head?.sha;
      const updatedAt = toMs(item.updated_at);
      if (
        prev?.snapshot
        && prev.snapshot.headSha === headSha
        && updatedAt !== null
        && updatedAt <= prev.snapshot.fetchedAtMs
      ) {
        note(await refreshOnePr(repo, item.number, "org", etags, token, prev, nowMs));
        continue;
      }
      note(await collectOnePr(repo, item.number, "org", etags, token, prevAcks));
    }
    for (const number of knownNumbersFor(repo, prevAcks)) {
      if (listed.has(number)) continue;
      note(
        await closeCheckOnePr(repo, number, "org", etags, token, prevAcks[prKey(repo, number)], nowMs),
      );
    }
    // Commit the list ETag only when every PR read off this list resolved —
    // a failed item holds the ETag back so the next tick refetches it.
    if (allResolved && list.etag) etags[listUrl] = list.etag;
  }

  async function collectOnePr(repo, number, kind, etags, token) {
    const key = prKey(repo, number);
    const urls = {
      pr: `${apiBase}/repos/${repo}/pulls/${number}`,
      comments: `${apiBase}/repos/${repo}/issues/${number}/comments?per_page=100`,
      reviewComments: `${apiBase}/repos/${repo}/pulls/${number}/comments?per_page=100`,
      reviews: `${apiBase}/repos/${repo}/pulls/${number}/reviews?per_page=100`,
    };
    const cached = bodies.get(urls.pr);
    const prRes = await get(urls.pr, cached?.etag, token);
    let prBody = null;
    if (prRes.status === 304 && cached) {
      prBody = cached.body;
    } else if (prRes.status === 200 && prRes.body) {
      prBody = prRes.body;
      const etag = prRes.headers?.get?.("etag");
      if (etag) bodies.set(urls.pr, { etag, body: prBody });
    } else {
      return { key, status: prRes.status === 403 && prRes.rateLimited ? 429 : prRes.status };
    }
    const headSha = prBody.head?.sha;
    // Array endpoints paginate; the check-runs and search endpoints answer
    // with envelope objects, so they go through single-page fetchJson here
    // (first 100; wider histories ride the next slice with Link support).
    const checksUrl = `${apiBase}/repos/${repo}/commits/${headSha}/check-runs?per_page=100`;
    const [commentsRes, reviewRes, reviewsRes, checksRes] = await Promise.all([
      fetchAllPages(fetchImpl, urls.comments, { token }),
      fetchAllPages(fetchImpl, urls.reviewComments, { token }),
      fetchAllPages(fetchImpl, urls.reviews, { token }),
      get(checksUrl, undefined, token),
    ]);
    const checksEtag = checksRes.headers?.get?.("etag");
    if (checksRes.status === 200 && checksEtag) etags[checksUrl] = checksEtag;
    const parts = [commentsRes, reviewRes, reviewsRes];
    if (parts.some((p) => !p.ok) || (checksRes.status !== 200 && checksRes.status !== 404)) {
      const rateLimited = parts.some((p) => p.rateLimited) || checksRes.status === 429;
      return { key, status: rateLimited ? 429 : (parts.find((p) => p.status)?.status ?? 500) };
    }
    const nowMs = Date.now();
    const snapshot = snapshotFromRest({
      repository: repo,
      pr: prBody,
      issueComments: commentsRes.items,
      reviewComments: reviewRes.items,
      reviews: reviewsRes.items,
      checkRuns: checksRes.body?.check_runs ?? [],
      ourLogins,
      nowMs,
    });
    try {
      normalizeSnapshot(snapshot);
    } catch {
      return { key, status: 422 };
    }
    const tracking = resolveTracking(repo, number, prBody);
    return {
      key,
      status: 200,
      input: {
        prev: null,
        next: snapshot,
        tracking: tracking
          ? {
              ...tracking,
              title: tracking.title ?? prBody.title,
              prUrl: tracking.prUrl ?? prBody.html_url,
              kind: tracking.kind ?? kind,
              owner: tracking.owner ?? prBody.user?.login,
              openedAtMs: tracking.openedAtMs ?? toMs(prBody.created_at) ?? nowMs,
            }
          : tracking,
        nowMs,
      },
    };
  }

  async function collectUpstreamRepo(repo, etags, token, prevAcks, reads) {
    if (!upstreamAuthor) {
      reads.push({ key: `scope:${repo}`, status: 0 });
      return;
    }
    const q = encodeURIComponent(`repo:${repo} author:${upstreamAuthor} is:pr is:open`);
    const url = `${apiBase}/search/issues?q=${q}&per_page=100`;
    // Search answers with an envelope object: single first page here.
    const res = await get(url, etags[url], token);
    const nowMs = Date.now();
    if (res.status === 304) {
      // Search unchanged, but wall-clock moved: refresh known PRs so SLA
      // clocks and red-CI transitions still evaluate this tick.
      for (const number of knownNumbersFor(repo, prevAcks)) {
        reads.push(
          await refreshOnePr(repo, number, "upstream", etags, token, prevAcks[prKey(repo, number)], nowMs),
        );
      }
      return;
    }
    if (res.status === 429) {
      reads.push({ key: `scope:${repo}`, status: 429 });
      return;
    }
    if (res.status !== 200 || !Array.isArray(res.body?.items)) {
      reads.push({ key: `scope:${repo}`, status: res.status });
      return;
    }
    const etag = res.headers?.get?.("etag");
    let allResolved = true;
    const listed = new Set();
    for (const item of res.body.items) {
      listed.add(item.number);
      const read = await collectOnePr(repo, item.number, "upstream", etags, token, prevAcks);
      reads.push(read);
      if (read.status !== 200 || !read.input) allResolved = false;
    }
    for (const number of knownNumbersFor(repo, prevAcks)) {
      if (listed.has(number)) continue;
      const read = await closeCheckOnePr(repo, number, "upstream", etags, token, prevAcks[prKey(repo, number)], nowMs);
      reads.push(read);
      if (read.status !== 200 || !read.input) allResolved = false;
    }
    if (allResolved && etag) etags[url] = etag;
  }

  return async function collect({ scope, etags = {}, token, prevAcks = {} } = {}) {
    const reads = [];
    for (const repo of scope?.org ?? []) {
      await collectOrgRepo(repo, etags, token, prevAcks, reads);
    }
    for (const repo of scope?.upstream ?? []) {
      await collectUpstreamRepo(repo, etags, token, prevAcks, reads);
    }
    return reads;
  };
}
