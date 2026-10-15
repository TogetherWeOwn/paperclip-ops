#!/usr/bin/env python3
"""Acceptance checks for landing paperclip-ops PR #3.

PR: https://github.com/TogetherWeOwn/paperclip-ops/pull/3
    perf(stop-guard): bound Stop latency with read budget and card cache.

What this pins (landing criteria):
  Group A -- the PR's own feature. Stop-path reads get a short per-read
  budget (~900 ms against the 1 s Stop budget) and a shared card cache:
  fresh cache skips the network, stale cache decides when the network is
  slow, and a slow control plane fails open with its own timeout signal
  instead of stalling the Stop or spiking `card-unreadable`.
  RED on the base (origin/main: unbounded 6500 ms read, no cache),
  GREEN on the PR head.
  Group B -- the CI red blocking the landing (triage 2026-10-15):
  Change detection FAILURE (scripts/sync-toolkit.sh pin drift:
  v0.2.2 has no verification/case_9e76f76d-recorded-command.sh),
  Secret scan FAILURE (public-owned-paths.txt drifted from its generator),
  pr-lint FAILURE (GARM-proof step rejects the ubuntu-latest runner),
  ci-ok FAILURE (aggregator; green iff the above are green).
  RED now, GREEN after the Land fix.

Run:  python3 -m unittest test_pr3_stop_guard_landing -v
Hermetic: stdlib only; git + node required (both present in CI and locally);
no network, no credentials, no database. ~10 s, dominated by one ~900 ms
timeout measurement (test 5).

Test files only: this file asserts about production files, it never edits them.
The Land card must not touch this file; the fix belongs in
production code and workflow files. Wiring into scripts/verify-fast.sh is left
to the Land card (out of scope here on purpose: test files only).
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
HOOK = REPO_ROOT / "hooks" / "disposition-stop-guard" / "disposition-stop-guard.mjs"
HOOK_URI = HOOK.as_uri()
NODE_TIMEOUT_S = 60


def run_node(snippet):
    """Run a JS ESM snippet via node; return (rc, stdout, stderr)."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".mjs", prefix="pr3-land-", delete=False
    ) as handle:
        handle.write(snippet)
        path = handle.name
    try:
        proc = subprocess.run(
            ["node", path],
            capture_output=True,
            text=True,
            timeout=NODE_TIMEOUT_S,
            cwd=str(REPO_ROOT),
        )
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        os.unlink(path)


def git(*args, env_extra=None):
    """Run git in REPO_ROOT; return stdout text (raises on failure)."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(REPO_ROOT), env=env
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve_base_ref():
    """PR base tree for the freeze-list recipe: merge-base, else origin/main."""
    for ref in ("origin/main", "HEAD"):
        try:
            out = git("merge-base", "HEAD", ref)
            if out.strip():
                return out.strip()
        except AssertionError:
            continue
    try:
        git("rev-parse", "--verify", "origin/main")
        return "origin/main"
    except AssertionError:
        return "HEAD"


def regen_freeze_list(base):
    """Replicate the secret-scan 'freeze list drifted' recipe (ci.yml)."""
    script = (
        "cut -d'|' -f1 vendor/toolkit-files.txt | sort -u "
        "| grep -v '^plugins/model-selection/' | comm -23 - "
        "<(comm -12 <(git ls-tree -r --name-only \"$BASE\" | sort -u) "
        "<(git ls-files | sort -u))"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "LC_ALL": "C", "BASE": base},
    )
    if proc.returncode != 0:
        raise AssertionError(f"freeze-list regen recipe failed: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line]


def committed_freeze_list():
    """The committed list, filtered exactly like the CI step."""
    lines = (
        (REPO_ROOT / "public-owned-paths.txt").read_text().splitlines()
    )
    return [l for l in lines if l and not l.startswith("#")]


def freeze_check():
    """Return (ok, detail) for the secret-scan freeze-list gate."""
    base = resolve_base_ref()
    regen = regen_freeze_list(base)
    committed = committed_freeze_list()
    if regen == committed:
        return True, "freeze list matches its generator"
    detail = ["public-owned-paths.txt drifted from its generator "
              "(secret-scan CI error)."]
    set_regen, set_comm = set(regen), set(committed)
    for missing in sorted(set_comm - set_regen)[:8]:
        detail.append(f"  stale entry (remove/regenerate): {missing}")
    for extra in sorted(set_regen - set_comm)[:8]:
        detail.append(f"  missing entry (regenerate): {extra}")
    if len(set_regen ^ set_comm) > 16:
        detail.append(f"  ... and {len(set_regen ^ set_comm) - 16} more")
    return False, "\n".join(detail)


def pin_check():
    """Return (ok, detail) for the change-detection toolkit-pin gate."""
    manifest = (REPO_ROOT / "vendor" / "toolkit-files.txt").read_text().splitlines()
    pins_text = (REPO_ROOT / "vendor" / "toolkit-pins.txt").read_text().splitlines()
    pinned_tags = {
        line.split()[0]
        for line in pins_text
        if line.strip() and not line.startswith("#")
    }
    entries = []
    for line in manifest:
        if not line.strip():
            continue
        parts = line.split("|")
        dest = parts[0]
        src = parts[1] if len(parts) > 1 and parts[1] else dest
        tag = parts[2] if len(parts) > 2 and parts[2] else None
        entries.append((dest, src, tag))
    problems = []
    for dest, src, tag in entries:
        if tag and tag not in pinned_tags:
            problems.append(f"manifest entry {dest} pins unknown tag {tag}")
    # Exact CI error on PR #3 (change-detection job log, 2026-10-15):
    #   sync-toolkit: v0.2.2 has no verification/case_9e76f76d-recorded-command.sh (pin drift?)
    known_missing = {
        ("verification/case_9e76f76d-recorded-command.sh", "v0.2.2"),
        ("verification/case_9e76f76d-recorded-command.txt", "v0.2.2"),
    }
    for dest, src, tag in entries:
        if (src, tag) in known_missing:
            problems.append(
                f"sync-toolkit: {tag} has no {src} (pin drift?) "
                f"[manifest line for {dest}]"
            )
    if problems:
        return False, "\n".join(problems)
    return True, "toolkit manifest pins resolve"


def runner_check():
    """Return (ok, detail) for the pr-lint runner/proof gate."""
    body = (REPO_ROOT / ".github" / "workflows" / "pr-lint.yml").read_text()
    proof = "garm-" in body and "RUNNER_NAME" in body
    if not proof:
        return True, "no GARM-only proof step"
    hosted = re.findall(r"runs-on:\s*([^\n#]+)", body)
    bad = [h.strip() for h in hosted if "ubuntu-latest" in h]
    if bad:
        return False, (
            "pr-lint runs on ubuntu-latest (GitHub-hosted) but its first step "
            "requires a GARM runner (RUNNER_NAME=garm-*): "
            "picked up by GitHub Actions, not a GARM runner (pr-lint CI error). "
            "Run it on a GARM pool or drop the proof step."
        )
    return True, "pr-lint runner matches its proof step"


class StopGuardLandingTest(unittest.TestCase):
    # ---- Group A: Stop-latency read budget + card cache (PR feature) ----

    def test_stop_read_budget_bounded(self):
        """Per-read budget defaults to 900 ms (Stop budget is 1 s)."""
        src = HOOK.read_text()
        self.assertRegex(
            src,
            r"export const REQUEST_TIMEOUT_MS_DEFAULT\s*=\s*900\b",
            "expected `export const REQUEST_TIMEOUT_MS_DEFAULT = 900` "
            "(base has the unbounded 6500 ms default)",
        )
        self.assertNotRegex(
            src,
            r"(?<!_)REQUEST_TIMEOUT_MS\s*=\s*6500",
            "legacy unbounded `REQUEST_TIMEOUT_MS = 6500` must be gone",
        )

    def test_stop_read_budget_precedence_and_clamps(self):
        """Budget precedence env > config > default, clamped to [50, 8000]."""
        rc, out, err = run_node(
            f"""import assert from "node:assert/strict";
import {{ requestTimeoutMs, REQUEST_TIMEOUT_MS_DEFAULT,
  REQUEST_TIMEOUT_MS_MIN, REQUEST_TIMEOUT_MS_MAX }} from {HOOK_URI!r};
assert.equal(REQUEST_TIMEOUT_MS_DEFAULT, 900);
assert.equal(REQUEST_TIMEOUT_MS_MIN, 50);
assert.equal(REQUEST_TIMEOUT_MS_MAX, 8000);
assert.equal(requestTimeoutMs({{}}, {{}}), 900);
assert.equal(requestTimeoutMs({{DISPOSITION_STOP_GUARD_REQUEST_TIMEOUT_MS:"100"}}, {{}}), 100);
assert.equal(requestTimeoutMs({{}}, {{requestTimeoutMs: 200}}), 200);
assert.equal(requestTimeoutMs({{DISPOSITION_STOP_GUARD_REQUEST_TIMEOUT_MS:"1"}}, {{}}), 50);
assert.equal(requestTimeoutMs({{DISPOSITION_STOP_GUARD_REQUEST_TIMEOUT_MS:"99999"}}, {{}}), 8000);
assert.equal(requestTimeoutMs({{DISPOSITION_STOP_GUARD_REQUEST_TIMEOUT_MS:"abc"}}, {{}}), 900);
console.log("budget-ok");"""
        )
        self.assertEqual(rc, 0, f"budget contract failed: {err.strip()}")
        self.assertIn("budget-ok", out)

    def test_stop_card_cache_api(self):
        """Shared card cache exists: 30 s fresh, 5 min stale, tmpdir-backed."""
        rc, out, err = run_node(
            f"""import assert from "node:assert/strict";
import {{ CACHE_FRESH_MS_DEFAULT, CACHE_STALE_MS_DEFAULT,
  cacheFileFor, readCardCache, writeCardCache }} from {HOOK_URI!r};
assert.equal(CACHE_FRESH_MS_DEFAULT, 30 * 1000);
assert.equal(CACHE_STALE_MS_DEFAULT, 5 * 60 * 1000);
for (const fn of [cacheFileFor, readCardCache, writeCardCache])
  assert.equal(typeof fn, "function");
const {{ dir }} = cacheFileFor({{}}, "task-1");
assert.ok(dir.includes("disposition-stop-guard"), dir);
// Deliberately NOT the run scratch dir (deleted with the run): the tail to
// bound is first-Stops of unrelated runs on the same box.
console.log("cache-api-ok " + dir);"""
        )
        self.assertEqual(rc, 0, f"cache API missing: {err.strip()}")
        self.assertIn("cache-api-ok", out)

    def test_stop_card_cache_roundtrip(self):
        """Cache round-trips; foreign/missing/corrupt entries read as null."""
        rc, out, err = run_node(
            f"""import assert from "node:assert/strict";
import {{ mkdtempSync, writeFileSync }} from "node:fs";
import {{ tmpdir }} from "node:os";
import {{ join }} from "node:path";
import {{ cacheFileFor, readCardCache, writeCardCache }} from {HOOK_URI!r};
const dir = mkdtempSync(join(tmpdir(), "pr3-land-cache-"));
const env = {{ DISPOSITION_STOP_GUARD_CACHE_DIR: dir }};
assert.equal(readCardCache(env, "T"), null);
assert.equal(writeCardCache(env, "T", {{ card: {{ id: "T", status: "in_progress" }},
  pendingInteractions: 0 }}), true);
const entry = readCardCache(env, "T");
assert.ok(entry && entry.card.status === "in_progress" && Number.isInteger(entry.ts));
assert.equal(entry.pendingInteractions, 0);
assert.equal(readCardCache(env, "OTHER"), null);
const {{ file }} = cacheFileFor(env, "OTHER");
writeFileSync(file, "not json{{{{");
assert.equal(readCardCache(env, "OTHER"), null);
console.log("cache-roundtrip-ok");"""
        )
        self.assertEqual(rc, 0, f"cache round-trip failed: {err.strip()}")
        self.assertIn("cache-roundtrip-ok", out)

    def test_stop_slow_control_plane_fails_open_fast(self):
        """A hung API reports timeout (own fail-open class) inside ~budget."""
        rc, out, err = run_node(
            f"""import assert from "node:assert/strict";
import {{ createServer }} from "node:http";
import {{ requestTimeoutMs, defaultFetchJson }} from {HOOK_URI!r};
const budget = requestTimeoutMs({{}}, {{}});
assert.ok(budget <= 1000, "default budget " + budget + "ms exceeds the 1 s Stop budget");
const server = createServer((req, res) => {{ /* never respond: hold the socket */ }});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const env = {{ PAPERCLIP_API_URL: `http://127.0.0.1:${{server.address().port}}/api`,
  PAPERCLIP_API_KEY: "test-key" }};
const t0 = Date.now();
const res = await defaultFetchJson(env, "/api/issues/T", budget);
const elapsed = Date.now() - t0;
server.close();
assert.equal(res.ok, false);
assert.equal(res.timeout, true, "slow read must signal timeout, not a plain failure");
assert.ok(elapsed < budget + 1500, `elapsed ${{elapsed}}ms exceeds budget ${{budget}}ms + slack`);
console.log(`stop-budget-ok elapsed=${{elapsed}}ms budget=${{budget}}ms`);"""
        )
        self.assertEqual(rc, 0, f"slow-plane probe failed: {err.strip()}")
        self.assertIn("stop-budget-ok", out)
        print(f"  [measure] {out.strip()}")

    def test_stop_fresh_cache_skips_network(self):
        """decide() on a fresh cache entry never touches the network."""
        rc, out, err = run_node(
            f"""import assert from "node:assert/strict";
import {{ mkdtempSync }} from "node:fs";
import {{ tmpdir }} from "node:os";
import {{ join }} from "node:path";
import {{ decide, writeCardCache }} from {HOOK_URI!r};
const dir = mkdtempSync(join(tmpdir(), "pr3-land-decide-"));
const env = {{ PAPERCLIP_TASK_ID: "T", PAPERCLIP_AGENT_ID: "A", PAPERCLIP_RUN_ID: "R",
  DISPOSITION_STOP_GUARD_STATE_DIR: join(dir, "state"),
  DISPOSITION_STOP_GUARD_CACHE_DIR: join(dir, "cache"),
  DISPOSITION_STOP_GUARD_LOG: join(dir, "d.jsonl"),
  DISPOSITION_STOP_GUARD_MODE: "enforce" }};
const card = {{ id: "T", identifier: "OPS-1", status: "in_progress",
  assigneeAgentId: "A", executionRunId: "R", conversationAgentId: null,
  originKind: null, externalChannelBinding: null,
  monitorNextCheckAt: null, executionState: null, blockedBy: [],
  blockerAttention: {{ unresolvedBlockerCount: 0 }},
  activeRecoveryAction: null, scheduledRetry: null, successfulRunHandoff: null }};
assert.equal(writeCardCache(env, "T", {{ card, pendingInteractions: 0 }}), true);
let calls = 0;
const fetchJson = async (path) => {{ calls++; throw new Error("network used: " + path); }};
const verdict = await decide({{ hook_event_name: "Stop", session_id: "s1" }}, env,
  {{ fetchJson, config: {{}}, killSwitchPresent: () => false }});
assert.equal(calls, 0, "fresh cache must skip the network entirely");
assert.equal(verdict.cached, true);
assert.equal(verdict.action, "block");
console.log("fresh-cache-ok");"""
        )
        self.assertEqual(rc, 0, f"fresh-cache decision failed: {err.strip()}")
        self.assertIn("fresh-cache-ok", out)

    # ---- Group B: CI red blockers (Land fix target) ----

    def test_ci_secret_scan_freeze_list_current(self):
        """public-owned-paths.txt matches its generator (secret-scan gate)."""
        ok, detail = freeze_check()
        self.assertTrue(ok, detail)

    def test_ci_change_detection_toolkit_pin_resolves(self):
        """Every toolkit manifest pin resolves; no known-missing (pin drift)."""
        ok, detail = pin_check()
        self.assertTrue(ok, detail)

    def test_ci_pr_lint_runner_matches_proof(self):
        """pr-lint runs where its GARM proof step can pass."""
        ok, detail = runner_check()
        self.assertTrue(ok, detail)

    def test_ci_ok_aggregates_without_red_inputs(self):
        """ci-ok goes green iff its needed jobs are green (offline mirror)."""
        ci_yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
        m = re.search(r"\bci-ok:\s*\n(?:.*\n)*?\s*needs:\s*\n((?:\s*-\s*\S+\n)+)", ci_yml)
        self.assertIsNotNone(m, "ci-ok job with a needs: list not found in ci.yml")
        needs = [line.split("-", 1)[1].strip() for line in m.group(1).strip().splitlines()]
        for required in ("changes", "secret-scan"):
            self.assertIn(
                required,
                needs,
                f"ci-ok must aggregate the {required} gate (needs: {needs})",
            )
        red = []
        ok, detail = pin_check()
        if not ok:
            red.append("changes ended failure\n" + detail)
        ok, detail = freeze_check()
        if not ok:
            red.append("secret-scan ended failure\n" + detail)
        ok, detail = runner_check()
        if not ok:
            red.append("pr-lint ended failure (required check)\n" + detail)
        self.assertFalse(
            red,
            "ci-ok: needed job(s) ended failure:\n\n" + "\n\n".join(red),
        )


if __name__ == "__main__":
    unittest.main()
