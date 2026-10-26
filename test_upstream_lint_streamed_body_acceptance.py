#!/usr/bin/env python3
"""Acceptance: streamed draft bodies (--body-file -) with recoverable input errors.

Immutable acceptance artifact. Tests only; no implementation.

Contract under test (synthetic offline inputs, existing `lint` command only):
  * `--body-file -` reads stdin once as UTF-8 in BOTH `--layout` override and
    `--repo-dir`/`--git-range` modes, without trimming, then runs the same
    template, public-text, claim, maintainer-memory and stage checks.
  * Empty supplied body is evaluated by existing rules, never treated as
    `--no-pr-text` or a clean bypass.
  * Unreadable-file, invalid-UTF-8 and interrupted/failed-input handling is
    controlled: one shared reader, documented bounded stdin byte limit, refuse
    overflow rather than truncate.
  * Under `--json`, input failures produce one parseable existing-style refused
    report with a stable input error identifier/code, `retryable=false` and a
    fixed `next_action`. Gate-line and text keep refusal/usage exit conventions.
  * New errors never echo body bytes, supplied snippets, paths or raw parser
    exceptions. No publishing, receipt-post, memory-write or hook operation.
  * File/stdin semantic parity for the same valid body; stable repo/head/check
    ids; current successful file behavior, no-pr-text/stage semantics and all
    privacy/template checks preserved. Invalid flag combos never consume stdin.
    `--live` stays opt-in; synthetic acceptance uses no live calls. Read-only,
    idempotent, dry-run.

Baseline expectation: PositiveControls pass on the pre-feature tree; every
Streamed* test fails (RED) because `--body-file -` currently crashes with an
internal error, missing/unreadable files crash, invalid UTF-8 crashes, no stdin
limit is documented, and `--help` has no piped example. The paired build turns
them GREEN without editing this file.

Run:
  python3 -B -m unittest -v test_upstream_lint_streamed_body_acceptance
  python3 -B -m unittest -v test_upstream_lint test_upstream_lint_streamed_body_acceptance

No network, no credentials, no production state. Subprocess CLI only.
"""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists("/usr/bin/git") and "GIT_BIN" not in os.environ:
    os.environ["GIT_BIN"] = "/usr/bin/git"
sys.path.insert(0, HERE)
import upstream_lint as ul  # noqa: E402

REPO = "acme/widget"
SHA = "f5bca33a44fb20d90972a0d15db880e0e3129440"

# Baseline valid body, snapshotted from test_upstream_lint.py so this artifact
# stays immutable even if the base suite evolves.
BODY = """<!-- Write all pull request text in Simplified Technical English. -->

## Thinking Path

> - Widget is the open source app people use to manage things
> - the scheduler keeps task sessions across runs
> - project row touches rotate sessions for no reason
> - This pull request drops the timestamp from the fingerprint so sessions stay stable
> - The benefit is fewer lost sessions

## Linked Issues or Issue Description

Fixes #15142. A project-row touch alone must not rotate a reusable session.

## What Changed

- drop the revision timestamp from the session fingerprint

## Verification

- ran `pnpm vitest server/src/__tests__/session.test.ts`: 12 passed

## Risks

- Low risk: stored sessions reset once after deploy

## Model Used

Anthropic Claude Opus 5.5 (claude-opus-5-5), extended thinking

## Checklist

- [x] I have included a thinking path that traces from project context to this change
- [x] I have run tests locally and they pass
- [ ] All CI gates are green (pending; ticked after measuring)
- [ ] Greptile is 5/5 (pending upstream review)
"""

MEMORY = {
    "schema_version": 1, "repo": REPO, "updated": "2026-10-07", "default_base": "main",
    "template": {
        "path": ".github/PULL_REQUEST_TEMPLATE.md", "blob_sha": "abc123",
        "sections": ["Thinking Path", "Linked Issues or Issue Description", "What Changed",
                     "Verification", "Risks", "Model Used", "Checklist"],
        "checkboxes": ["i have included a thinking path that traces from project context to this change",
                       "i have run tests locally and they pass",
                       "all ci gates are green", "greptile is 5/5"],
        "pending_ok_at_first_push": ["greptile", "ci gates are green"],
        "section_rules": [{"name": "Thinking Path", "min_bullets": 4, "min_chars": 60},
                          {"name": "What Changed", "min_bullets": 1},
                          {"name": "Verification", "min_bullets": 0, "require_code": True}],
    },
    "title": {"max_len": 100},
    "signoff": {"dco": False, "cla": False, "cla_evidence": None, "detected_from": []},
    "budget": {"max_changed_lines": 100, "max_files": 5, "split_max_changed_lines": 20,
               "split_max_files": 2, "exclude_globs": []},
    "tests": {"required": True, "test_globs": [], "exempt_globs": []},
    "claims": {"allow": []}, "batch": {"max_open_prs": 2}, "rules": [], "feedback": [],
}

PATCH_SRC = "@@ -1,2 +1,3 @@\n const a = 1;\n+const fingerprint = hash(a);\n"
PATCH_TEST = "@@ -0,0 +1,3 @@\n+it('keeps the session', () => {\n+  expect(rotate()).toBe(false);\n+});\n"

TITLE = "fix(scheduler): keep task sessions across project row updates"
BRANCH = "fix/session-fingerprint-churn"

# Distinctive non-triggering marker for leak checks (no internal pattern).
LEAK_MARK = "BODYBYTES-SENTINEL-7f3a9c1e"
# Internal-id sentinel, split so this source file never holds the literal.
INTERNAL_SENTINEL = "TOG-" "999001"

FORBIDDEN_EXCEPTION_TOKENS = (
    "Traceback", "FileNotFoundError", "UnicodeDecodeError", "IsADirectoryError",
    "PermissionError", "internal error",
)


def good_commits():
    return [{"sha": SHA, "parents": ["p1"], "commit": {
        "message": TITLE, "author": {"name": "A", "email": "a@example.com"}}}]


def good_files():
    return [
        {"filename": "server/src/services/session.ts", "status": "modified", "additions": 5,
         "deletions": 1, "changes": 6, "patch": PATCH_SRC},
        {"filename": "server/src/__tests__/session.test.ts", "status": "added", "additions": 3,
         "deletions": 0, "changes": 3, "patch": PATCH_TEST},
    ]


def git(cwd, *args):
    return subprocess.run([os.environ.get("GIT_BIN", "git"), "-C", cwd,
                           "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
                          capture_output=True, text=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="A", GIT_AUTHOR_EMAIL="a@example.com",
                                   GIT_COMMITTER_NAME="A", GIT_COMMITTER_EMAIL="a@example.com")).stdout


def make_repo(root):
    git(root, "init", "-q", "-b", "main")
    os.makedirs(os.path.join(root, "server/src/services"))
    with open(os.path.join(root, "server/src/services/session.ts"), "w") as fh:
        fh.write("const a = 1;\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore(init): base")
    git(root, "checkout", "-q", "-b", BRANCH)
    with open(os.path.join(root, "server/src/services/session.ts"), "a") as fh:
        fh.write("const fingerprint = hash(a);\n")
    os.makedirs(os.path.join(root, "server/src/__tests__"))
    with open(os.path.join(root, "server/src/__tests__/session.test.ts"), "w") as fh:
        fh.write("it('keeps the session', () => {\n  expect(rotate()).toBe(false);\n});\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", TITLE)


def cli_env(mdir, extra=None):
    env = dict(os.environ, UPSTREAM_MEMORY_DIR=mdir)
    # Synthetic acceptance never uses live calls; point the API seam at a
    # closed loopback port so any accidental network use fails fast offline.
    env.setdefault("GH_API_URL", "http://127.0.0.1:9/")
    env.pop("GH_TOKEN", None)
    env.pop("UPSTREAM_AUTHOR_LOGIN", None)
    if extra:
        env.update(extra)
    return env


def run_cli(argv, mdir, input_bytes=None, timeout=60):
    """Run upstream_lint.py via subprocess. input_bytes=None means no stdin."""
    full = [sys.executable, os.path.join(HERE, "upstream_lint.py")] + argv
    try:
        proc = subprocess.run(full, input=input_bytes, capture_output=True, timeout=timeout,
                              env=cli_env(mdir))
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("CLI hung (did it block on stdin?): %r" % (argv,)) from exc
    return proc


def stdout_text(proc):
    return proc.stdout.decode("utf-8", "replace")


def stderr_text(proc):
    return proc.stderr.decode("utf-8", "replace")


def combined_text(proc):
    return stdout_text(proc) + "\n" + stderr_text(proc)


def parse_json_stdout(test, proc, context):
    out = stdout_text(proc).strip()
    try:
        return json.loads(out)
    except ValueError:
        test.fail(
            "%s: expected one parseable JSON report on stdout, got rc=%d stdout=%r stderr=%r"
            % (context, proc.returncode, out[:800], stderr_text(proc)[:800]))


def extract_input_code(out):
    """Stable input-error identifier from a refused JSON report, or None."""
    for key in ("code", "error_code", "error", "input_code", "input_error"):
        val = out.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int,)) and not isinstance(val, bool):
            return str(val)
    items = out.get("items") or []
    if items and isinstance(items[0], dict):
        item = items[0].get("item")
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def assert_no_exception_leak(test, proc, context):
    blob = combined_text(proc)
    for token in FORBIDDEN_EXCEPTION_TOKENS:
        test.assertNotIn(token, blob, "%s: raw exception leaked %r" % (context, token))


def assert_refused_json(test, proc, context):
    """Existing-style refused report + recoverable-input envelope. Returns (out, code)."""
    test.assertEqual(
        proc.returncode, ul.EXIT_REFUSED,
        "%s: input failure must exit REFUSED(2), got %d out=%r err=%r"
        % (context, proc.returncode, stdout_text(proc)[:500], stderr_text(proc)[:500]))
    out = parse_json_stdout(test, proc, context)
    test.assertIsInstance(out, dict, "%s: JSON report must be an object" % context)
    test.assertEqual(out.get("repo"), REPO, "%s: repo id must stay stable" % context)
    test.assertFalse(out.get("pass"), "%s: refused report must have pass=false" % context)
    test.assertTrue(out.get("refused"), "%s: refused report must have refused=true" % context)
    items = out.get("items")
    test.assertIsInstance(items, list, "%s: items must be a list" % context)
    test.assertTrue(any(i.get("status") == "REFUSED" for i in items if isinstance(i, dict)),
                    "%s: at least one REFUSED item required: %r" % (context, items))
    test.assertIs(out.get("retryable"), False,
                  "%s: input failure must carry retryable=false: %r" % (context, out))
    nxt = out.get("next_action")
    test.assertIsInstance(nxt, str, "%s: next_action must be a string: %r" % (context, out))
    test.assertTrue(nxt.strip(), "%s: next_action must be fixed non-empty: %r" % (context, out))
    code = extract_input_code(out)
    test.assertTrue(code, "%s: stable input error identifier/code required: %r" % (context, out))
    # Input errors need their own identifier, not the generic lint/memory bucket.
    if code in ("lint", "memory", "batch"):
        # Allow only when an explicit top-level code field carries the input id.
        explicit = out.get("code") or out.get("error_code") or out.get("error")
        test.assertTrue(explicit and str(explicit).strip() not in ("lint", "memory", "batch"),
                        "%s: input error needs an input-specific code, got generic %r in %r"
                        % (context, code, out))
    assert_no_exception_leak(test, proc, context)
    return out, code


def documented_stdin_limit():
    """Parse the bounded stdin byte limit from --help. None when undocumented."""
    proc = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"),
                           "lint", "--help"], capture_output=True, timeout=30)
    text = proc.stdout.decode("utf-8", "replace")
    candidates = []
    for line in text.splitlines():
        low = line.lower()
        if not any(k in low for k in ("stdin", "body", "limit", "max")):
            continue
        for m in re.finditer(r"(\d[\d_,]*)\s*(bytes|byte|KiB|KB|MiB|MB|\bB\b)", line, re.I):
            num = int(m.group(1).replace(",", "").replace("_", ""))
            unit = m.group(2).lower()
            mult = 1
            if unit in ("kib", "kb"):
                mult = 1024
            elif unit in ("mib", "mb"):
                mult = 1024 * 1024
            candidates.append(num * mult)
    if candidates:
        return min(candidates)
    return None


def sha_of(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class StreamedBodyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mdir = os.path.join(self.tmp.name, "mem")
        os.makedirs(self.mdir)
        ul.save_memory(REPO, copy.deepcopy(MEMORY), self.mdir)
        self.mem_path = ul.memory_path(REPO, self.mdir)
        self.mem_before = sha_of(self.mem_path)
        self.title = TITLE
        self.branch = BRANCH
        self.body = BODY
        # Layout dir for --layout mode.
        self.layout = os.path.join(self.tmp.name, "lay")
        os.makedirs(self.layout)
        self.write_layout(self.layout, self.title, "placeholder-layout-body", self.branch)
        # Git repo for --repo-dir/--git-range mode.
        self.repo_dir = os.path.join(self.tmp.name, "clone")
        os.makedirs(self.repo_dir)
        make_repo(self.repo_dir)

    def write_layout(self, d, title, body, branch):
        with open(os.path.join(d, "pr.json"), "w", encoding="utf-8") as fh:
            json.dump({"title": title, "body": body,
                       "head": {"sha": SHA, "ref": branch}}, fh)
        with open(os.path.join(d, "commits.json"), "w", encoding="utf-8") as fh:
            json.dump(good_commits(), fh)
        with open(os.path.join(d, "files.json"), "w", encoding="utf-8") as fh:
            json.dump(good_files(), fh)

    def write_body_file(self, name, data_bytes):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(data_bytes)
        return path

    def layout_args(self, body_file, extra=None):
        argv = ["lint", "--repo", REPO, "--layout", self.layout,
               "--body-file", body_file]
        if extra:
            argv += extra
        return argv

    def git_args(self, body_file, extra=None):
        argv = ["lint", "--repo", REPO, "--repo-dir", self.repo_dir,
               "--git-range", "main..HEAD", "--title", self.title,
               "--body-file", body_file, "--branch", self.branch]
        if extra:
            argv += extra
        return argv

    def assertMemUnchanged(self, context):
        self.assertEqual(sha_of(self.mem_path), self.mem_before,
                         "%s: lint must not write maintainer memory" % context)

    def assertRepoClean(self, context):
        status = git(self.repo_dir, "status", "--porcelain")
        self.assertEqual(status, "", "%s: lint must leave the repo untouched: %r" % (context, status))

    def assertItemsEqual(self, a_items, b_items, context):
        norm = lambda items: [(i.get("status"), i.get("item")) for i in items]
        self.assertEqual(norm(a_items), norm(b_items),
                         "%s: file/stdin check ids and statuses must match" % context)


class PositiveControls(StreamedBodyBase):
    """Baseline behavior that already works; proves the harness is valid."""

    def test_positive_file_layout_json_passes(self):
        bodyf = self.write_body_file("body.md", self.body.encode("utf-8"))
        proc = run_cli(self.layout_args(bodyf, ["--json"]), self.mdir)
        self.assertEqual(proc.returncode, 0, combined_text(proc))
        out = parse_json_stdout(self, proc, "file layout json")
        self.assertTrue(out["pass"] and not out["refused"])
        self.assertEqual(out["repo"], REPO)
        self.assertEqual(out["head"], SHA)
        self.assertMemUnchanged("file layout")
        self.assertRepoClean("file layout")

    def test_positive_file_git_range_text_passes(self):
        bodyf = self.write_body_file("body.md", self.body.encode("utf-8"))
        proc = run_cli(self.git_args(bodyf), self.mdir)
        self.assertEqual(proc.returncode, 0, combined_text(proc))
        self.assertIn("LINT: PASS", stdout_text(proc))
        self.assertMemUnchanged("file git-range")

    def test_positive_file_empty_body_is_evaluated_not_skipped(self):
        bodyf = self.write_body_file("empty.md", b"")
        proc = run_cli(self.git_args(bodyf, ["--json"]), self.mdir)
        out = parse_json_stdout(self, proc, "empty file body")
        by_item = {i["item"]: i for i in out["items"]}
        self.assertIn("template", by_item)
        self.assertEqual(by_item["template"]["status"], "FAIL",
                         "empty file body must FAIL template, not skip: %r" % (out,))
        self.assertNotIn("skipped (no PR text yet", json.dumps(out))

    def test_positive_no_pr_text_skips_template(self):
        proc = run_cli(["lint", "--repo", REPO, "--repo-dir", self.repo_dir,
                        "--git-range", "main..HEAD", "--no-pr-text",
                        "--branch", self.branch, "--json"], self.mdir)
        self.assertEqual(proc.returncode, 0, combined_text(proc))
        out = parse_json_stdout(self, proc, "no-pr-text")
        by_item = {i["item"]: i for i in out["items"]}
        self.assertIn("template", by_item)
        self.assertIn("skipped (no PR text yet", by_item["template"]["detail"])

    def test_positive_stage_ready_needs_boxes_ticked(self):
        bodyf = self.write_body_file("body.md", self.body.encode("utf-8"))
        proc = run_cli(self.git_args(bodyf, ["--json", "--stage", "ready"]), self.mdir)
        out = parse_json_stdout(self, proc, "ready stage unticked")
        by_item = {i["item"]: i for i in out["items"]}
        self.assertEqual(by_item["template"]["status"], "FAIL")
        ticked = self.body.replace("- [ ]", "- [x]")
        bodyf2 = self.write_body_file("ticked.md", ticked.encode("utf-8"))
        proc2 = run_cli(self.git_args(bodyf2, ["--json", "--stage", "ready"]), self.mdir)
        self.assertEqual(proc2.returncode, 0, combined_text(proc2))

    def test_positive_gate_line_single_line_pass(self):
        bodyf = self.write_body_file("body.md", self.body.encode("utf-8"))
        proc = run_cli(self.git_args(bodyf, ["--gate-line"]), self.mdir)
        self.assertEqual(proc.returncode, 0, combined_text(proc))
        self.assertTrue(stdout_text(proc).startswith("PASS\t"), stdout_text(proc))
        self.assertEqual(len(stdout_text(proc).strip().splitlines()), 1)

    def test_positive_help_mentions_body_file_and_live(self):
        proc = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"),
                               "lint", "--help"], capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        text = proc.stdout.decode("utf-8", "replace")
        self.assertIn("--body-file", text)
        self.assertIn("--live", text)


class LayoutStdin(StreamedBodyBase):
    """--body-file - in --layout override mode (RED on baseline)."""

    def test_layout_stdin_valid_body_matches_file(self):
        body_bytes = self.body.encode("utf-8")
        bodyf = self.write_body_file("body.md", body_bytes)
        file_proc = run_cli(self.layout_args(bodyf, ["--json"]), self.mdir)
        self.assertEqual(file_proc.returncode, 0, combined_text(file_proc))
        file_out = parse_json_stdout(self, file_proc, "layout file")
        stdin_proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=body_bytes)
        self.assertEqual(stdin_proc.returncode, 0,
                         "layout stdin valid body must pass like the file: %r" % combined_text(stdin_proc))
        stdin_out = parse_json_stdout(self, stdin_proc, "layout stdin")
        self.assertEqual(stdin_out["repo"], file_out["repo"])
        self.assertEqual(stdin_out["head"], file_out["head"])
        self.assertEqual(stdin_out["pass"], file_out["pass"])
        self.assertEqual(stdin_out["refused"], file_out["refused"])
        self.assertItemsEqual(file_out["items"], stdin_out["items"], "layout file/stdin")
        self.assertMemUnchanged("layout stdin")
        self.assertRepoClean("layout stdin")

    def test_layout_stdin_unicode_newlines_preserved(self):
        varied = (
            "\n\n" + self.body.replace("drops the timestamp", "drops café ☃ — naïve ✓")
            + "\n\ntrailing-spaces   \nno-final-newline-marker"
        )
        # Mix CRLF and LF to prove bytes are not normalized away differentially.
        varied = varied.replace("\n## Verification", "\r\n## Verification")
        data = varied.encode("utf-8")
        bodyf = self.write_body_file("unicode.md", data)
        file_proc = run_cli(self.layout_args(bodyf, ["--json"]), self.mdir)
        file_out = parse_json_stdout(self, file_proc, "layout unicode file")
        stdin_proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=data)
        self.assertEqual(stdin_proc.returncode, file_proc.returncode,
                         "unicode/newline stdin must match file rc: file=%d stdin=%d %r"
                         % (file_proc.returncode, stdin_proc.returncode, combined_text(stdin_proc)))
        stdin_out = parse_json_stdout(self, stdin_proc, "layout unicode stdin")
        self.assertItemsEqual(file_out["items"], stdin_out["items"], "layout unicode parity")

    def test_layout_stdin_empty_body_still_evaluated(self):
        proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=b"")
        out = parse_json_stdout(self, proc, "layout empty stdin")
        by_item = {i["item"]: i for i in out["items"]}
        self.assertIn("template", by_item, "empty stdin must still evaluate template: %r" % (out,))
        self.assertEqual(by_item["template"]["status"], "FAIL",
                         "empty stdin body must FAIL template, never skip or pass: %r" % (out,))
        self.assertFalse(out["pass"])
        self.assertNotIn("skipped (no PR text yet", json.dumps(out))

    def test_layout_stdin_bad_template_rejected_like_file(self):
        bad = "hello world\n\nno template sections here\n"
        bodyf = self.write_body_file("bad.md", bad.encode("utf-8"))
        file_proc = run_cli(self.layout_args(bodyf, ["--json"]), self.mdir)
        file_out = parse_json_stdout(self, file_proc, "layout bad file")
        stdin_proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=bad.encode("utf-8"))
        stdin_out = parse_json_stdout(self, stdin_proc, "layout bad stdin")
        for out, label in ((file_out, "file"), (stdin_out, "stdin")):
            by_item = {i["item"]: i for i in out["items"]}
            self.assertEqual(by_item["template"]["status"], "FAIL",
                             "bad template %s must FAIL template: %r" % (label, out))
        self.assertItemsEqual(file_out["items"], stdin_out["items"], "layout bad-template parity")

    def test_layout_stdin_private_sentinel_rejected(self):
        poisoned = self.body + "\nSee instance %s for details.\n" % INTERNAL_SENTINEL
        data = poisoned.encode("utf-8")
        bodyf = self.write_body_file("poison.md", data)
        file_proc = run_cli(self.layout_args(bodyf, ["--json"]), self.mdir)
        file_out = parse_json_stdout(self, file_proc, "layout sentinel file")
        stdin_proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=data)
        stdin_out = parse_json_stdout(self, stdin_proc, "layout sentinel stdin")
        for out, label in ((file_out, "file"), (stdin_out, "stdin")):
            by_item = {i["item"]: i for i in out["items"]}
            self.assertIn("internal", by_item)
            self.assertEqual(by_item["internal"]["status"], "FAIL",
                             "private sentinel %s must FAIL internal: %r" % (label, out))
        self.assertItemsEqual(file_out["items"], stdin_out["items"], "layout sentinel parity")

    def test_layout_stdin_localhost_prose_rejected(self):
        poisoned = self.body + "\nTested against http://localhost:8080/widget.\n"
        data = poisoned.encode("utf-8")
        stdin_proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=data)
        stdin_out = parse_json_stdout(self, stdin_proc, "layout localhost stdin")
        by_item = {i["item"]: i for i in stdin_out["items"]}
        self.assertEqual(by_item["internal"]["status"], "FAIL",
                         "localhost in stdin prose must FAIL internal: %r" % (stdin_out,))

    def test_layout_stdin_invalid_utf8_refused_json(self):
        payload = ("prefix %s " % LEAK_MARK).encode("utf-8") + b"\xff\xfe\xfd\x80" + b" tail"
        proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=payload)
        out, _ = assert_refused_json(self, proc, "layout invalid-utf8 stdin")
        blob = combined_text(proc)
        self.assertNotIn(LEAK_MARK, blob, "input error must not echo body bytes")
        self.assertNotIn("\xff", blob)
        self.assertMemUnchanged("layout invalid-utf8")
        self.assertRepoClean("layout invalid-utf8")

    def test_layout_stdin_truncated_utf8_refused_json(self):
        payload = "truncated euro sign \xe2\x82".encode("latin1")
        proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=payload)
        assert_refused_json(self, proc, "layout truncated-utf8 stdin")

    def test_layout_stdin_over_limit_refused_not_truncated(self):
        limit = documented_stdin_limit()
        self.assertIsNotNone(limit, "stdin byte limit must be documented in --help")
        self.assertGreater(limit, 1024, "documented stdin limit implausibly small: %r" % (limit,))
        self.assertLess(limit, 100 * 1024 * 1024, "documented stdin limit implausibly large: %r" % (limit,))
        payload = ("OVERLIMIT-%s-" % LEAK_MARK).encode("utf-8") + b"x" * (limit + 1)
        proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=payload)
        out, _ = assert_refused_json(self, proc, "layout over-limit stdin")
        self.assertNotIn(LEAK_MARK, combined_text(proc), "overflow error must not echo input")
        self.assertFalse(out["pass"])

    def test_layout_missing_body_file_refused_json(self):
        missing = os.path.join(self.tmp.name, "missing-body-7f3a9c.md")
        proc = run_cli(self.layout_args(missing, ["--json"]), self.mdir)
        out, _ = assert_refused_json(self, proc, "layout missing file")
        blob = combined_text(proc)
        self.assertNotIn("missing-body-7f3a9c.md", blob, "input error must not echo the path")
        self.assertNotIn(missing, blob)

    def test_layout_unreadable_body_file_refused_json(self):
        # A directory is unreadable as a file on every platform and user.
        proc = run_cli(self.layout_args(self.layout, ["--json"]), self.mdir)
        out, _ = assert_refused_json(self, proc, "layout unreadable file")
        self.assertNotIn(self.layout, combined_text(proc), "input error must not echo the path")

    def test_layout_invalid_utf8_file_refused_json(self):
        path = self.write_body_file("badbytes.md", b"ok " + b"\xff\xfe" + b" tail")
        proc = run_cli(self.layout_args(path, ["--json"]), self.mdir)
        assert_refused_json(self, proc, "layout invalid-utf8 file")

    def test_layout_stdin_text_mode_refused(self):
        payload = b"\xff\xfe invalid stdin bytes"
        proc = run_cli(self.layout_args("-"), self.mdir, input_bytes=payload)
        self.assertEqual(proc.returncode, ul.EXIT_REFUSED, combined_text(proc))
        text = stdout_text(proc)
        self.assertIn("REFUSED", text)
        self.assertIn("LINT: REFUSED", text)
        assert_no_exception_leak(self, proc, "layout stdin text refused")

    def test_layout_stdin_gate_line_refused(self):
        payload = b"\xff\xfe invalid stdin bytes"
        proc = run_cli(self.layout_args("-", ["--gate-line"]), self.mdir, input_bytes=payload)
        self.assertEqual(proc.returncode, ul.EXIT_REFUSED, combined_text(proc))
        lines = stdout_text(proc).strip().splitlines()
        self.assertEqual(len(lines), 1, "gate-line must stay one line: %r" % (lines,))
        self.assertTrue(lines[0].startswith("REFUSED\t"), lines[0])
        assert_no_exception_leak(self, proc, "layout stdin gate-line refused")


class GitRangeStdin(StreamedBodyBase):
    """--body-file - in --repo-dir/--git-range mode (RED on baseline)."""

    def test_gitrange_stdin_valid_body_matches_file(self):
        body_bytes = self.body.encode("utf-8")
        bodyf = self.write_body_file("body.md", body_bytes)
        file_proc = run_cli(self.git_args(bodyf, ["--json"]), self.mdir)
        self.assertEqual(file_proc.returncode, 0, combined_text(file_proc))
        file_out = parse_json_stdout(self, file_proc, "gitrange file")
        stdin_proc = run_cli(self.git_args("-", ["--json"]), self.mdir, input_bytes=body_bytes)
        self.assertEqual(stdin_proc.returncode, 0,
                         "git-range stdin valid body must pass like the file: %r" % combined_text(stdin_proc))
        stdin_out = parse_json_stdout(self, stdin_proc, "gitrange stdin")
        self.assertEqual(stdin_out["repo"], file_out["repo"])
        self.assertEqual(stdin_out["head"], file_out["head"])
        self.assertTrue(stdin_out["head"], "head SHA must stay stable for stdin")
        self.assertEqual(stdin_out["pass"], file_out["pass"])
        self.assertEqual(stdin_out["refused"], file_out["refused"])
        self.assertItemsEqual(file_out["items"], stdin_out["items"], "gitrange file/stdin")
        self.assertMemUnchanged("gitrange stdin")
        self.assertRepoClean("gitrange stdin")

    def test_gitrange_stdin_unicode_newlines_preserved(self):
        varied = self.body.replace("Low risk", "Löw rïsk ☃") + "\n\nfin café"
        varied = varied.replace("\n## Risks", "\r\n## Risks")
        data = varied.encode("utf-8")
        bodyf = self.write_body_file("unicode.md", data)
        file_proc = run_cli(self.git_args(bodyf, ["--json"]), self.mdir)
        file_out = parse_json_stdout(self, file_proc, "gitrange unicode file")
        stdin_proc = run_cli(self.git_args("-", ["--json"]), self.mdir, input_bytes=data)
        self.assertEqual(stdin_proc.returncode, file_proc.returncode,
                         "unicode stdin must match file rc: %r" % combined_text(stdin_proc))
        stdin_out = parse_json_stdout(self, stdin_proc, "gitrange unicode stdin")
        self.assertItemsEqual(file_out["items"], stdin_out["items"], "gitrange unicode parity")

    def test_gitrange_stdin_empty_body_still_evaluated(self):
        proc = run_cli(self.git_args("-", ["--json"]), self.mdir, input_bytes=b"")
        out = parse_json_stdout(self, proc, "gitrange empty stdin")
        by_item = {i["item"]: i for i in out["items"]}
        self.assertEqual(by_item["template"]["status"], "FAIL",
                         "empty stdin body must FAIL template: %r" % (out,))
        self.assertFalse(out["pass"])
        self.assertNotIn("skipped (no PR text yet", json.dumps(out))

    def test_gitrange_stdin_bad_template_rejected_like_file(self):
        bad = "just a line, no sections\n"
        bodyf = self.write_body_file("bad.md", bad.encode("utf-8"))
        file_out = parse_json_stdout(self, run_cli(self.git_args(bodyf, ["--json"]), self.mdir), "gitrange bad file")
        stdin_out = parse_json_stdout(
            self, run_cli(self.git_args("-", ["--json"]), self.mdir, input_bytes=bad.encode("utf-8")),
            "gitrange bad stdin")
        for out in (file_out, stdin_out):
            by_item = {i["item"]: i for i in out["items"]}
            self.assertEqual(by_item["template"]["status"], "FAIL")

    def test_gitrange_stdin_private_sentinel_rejected(self):
        poisoned = self.body + "\nRef %s noted.\n" % INTERNAL_SENTINEL
        data = poisoned.encode("utf-8")
        stdin_out = parse_json_stdout(
            self, run_cli(self.git_args("-", ["--json"]), self.mdir, input_bytes=data), "gitrange sentinel stdin")
        by_item = {i["item"]: i for i in stdin_out["items"]}
        self.assertEqual(by_item["internal"]["status"], "FAIL",
                         "private sentinel via stdin must FAIL internal: %r" % (stdin_out,))

    def test_gitrange_stdin_invalid_utf8_refused_json(self):
        payload = ("prefix %s " % LEAK_MARK).encode("utf-8") + b"\x80\x81\xfe\xff"
        proc = run_cli(self.git_args("-", ["--json"]), self.mdir, input_bytes=payload)
        out, _ = assert_refused_json(self, proc, "gitrange invalid-utf8 stdin")
        self.assertNotIn(LEAK_MARK, combined_text(proc))
        self.assertMemUnchanged("gitrange invalid-utf8")
        self.assertRepoClean("gitrange invalid-utf8")

    def test_gitrange_stdin_over_limit_refused_not_truncated(self):
        limit = documented_stdin_limit()
        self.assertIsNotNone(limit, "stdin byte limit must be documented in --help")
        payload = ("OVERLIMIT-%s-" % LEAK_MARK).encode("utf-8") + b"y" * (limit + 1)
        proc = run_cli(self.git_args("-", ["--json"]), self.mdir, input_bytes=payload)
        out, _ = assert_refused_json(self, proc, "gitrange over-limit stdin")
        self.assertNotIn(LEAK_MARK, combined_text(proc))
        self.assertFalse(out["pass"])

    def test_gitrange_missing_body_file_refused_json(self):
        missing = os.path.join(self.tmp.name, "absent-7f3a9c.md")
        proc = run_cli(self.git_args(missing, ["--json"]), self.mdir)
        out, _ = assert_refused_json(self, proc, "gitrange missing file")
        self.assertNotIn("absent-7f3a9c.md", combined_text(proc))
        self.assertNotIn(missing, combined_text(proc))

    def test_gitrange_unreadable_body_file_refused_json(self):
        proc = run_cli(self.git_args(self.layout, ["--json"]), self.mdir)
        assert_refused_json(self, proc, "gitrange unreadable file")

    def test_gitrange_stdin_text_mode_refused(self):
        proc = run_cli(self.git_args("-"), self.mdir, input_bytes=b"\xff bad")
        self.assertEqual(proc.returncode, ul.EXIT_REFUSED, combined_text(proc))
        self.assertIn("LINT: REFUSED", stdout_text(proc))
        assert_no_exception_leak(self, proc, "gitrange stdin text refused")

    def test_gitrange_stdin_gate_line_refused(self):
        proc = run_cli(self.git_args("-", ["--gate-line"]), self.mdir, input_bytes=b"\xff bad")
        self.assertEqual(proc.returncode, ul.EXIT_REFUSED, combined_text(proc))
        lines = stdout_text(proc).strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("REFUSED\t"), lines[0])


class SharedReaderAndParity(StreamedBodyBase):
    """One shared body-input reader across modes; stable envelope (RED on baseline)."""

    def collect_envelope(self, argv, input_bytes):
        proc = run_cli(argv, self.mdir, input_bytes=input_bytes)
        out, code = assert_refused_json(self, proc, "shared reader %r" % (argv[:4],))
        return out, code, out["next_action"]

    def test_shared_reader_same_envelope_across_modes_and_causes(self):
        bad = b"\xff\xfe shared"
        lay_out, lay_code, lay_next = self.collect_envelope(
            self.layout_args("-", ["--json"]), bad)
        git_out, git_code, git_next = self.collect_envelope(
            self.git_args("-", ["--json"]), bad)
        self.assertEqual(lay_code, git_code,
                         "layout and git-range must share one body reader code: %r vs %r"
                         % (lay_out, git_out))
        self.assertEqual(lay_next, git_next, "next_action must be fixed across modes")
        missing = os.path.join(self.tmp.name, "gone-7f3a9c.md")
        miss_out, miss_code, miss_next = self.collect_envelope(
            self.layout_args(missing, ["--json"]), None)
        self.assertEqual(miss_code, lay_code,
                         "missing-file and invalid-utf8 must share the stable input code")
        self.assertEqual(miss_next, lay_next, "next_action must be fixed across input causes")
        limit = documented_stdin_limit()
        self.assertIsNotNone(limit, "stdin byte limit must be documented in --help")
        over_out, over_code, over_next = self.collect_envelope(
            self.layout_args("-", ["--json"]), b"z" * (limit + 1))
        self.assertEqual(over_code, lay_code, "overflow must share the stable input code")
        self.assertEqual(over_next, lay_next, "overflow next_action must match")

    def test_file_stdin_semantic_parity_stable_ids(self):
        data = self.body.encode("utf-8")
        for mkargs in (self.layout_args, self.git_args):
            with self.subTest(mode=mkargs.__name__):
                bodyf = self.write_body_file("parity-%s.md" % mkargs.__name__, data)
                file_out = parse_json_stdout(
                    self, run_cli(mkargs(bodyf, ["--json"]), self.mdir), "parity file")
                stdin_out = parse_json_stdout(
                    self, run_cli(mkargs("-", ["--json"]), self.mdir, input_bytes=data), "parity stdin")
                self.assertEqual(file_out["repo"], stdin_out["repo"])
                self.assertEqual(file_out["head"], stdin_out["head"])
                self.assertEqual([i["item"] for i in file_out["items"]],
                                 [i["item"] for i in stdin_out["items"]],
                                 "check ids must stay stable file-vs-stdin")

    def test_stdin_lint_is_idempotent(self):
        data = self.body.encode("utf-8")
        first = parse_json_stdout(
            self, run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=data), "idempotent first")
        second = parse_json_stdout(
            self, run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=data), "idempotent second")
        self.assertEqual(first, second, "same stdin bytes must lint identically twice")
        self.assertMemUnchanged("idempotent")
        self.assertRepoClean("idempotent")


class InvalidArgsAndSafety(StreamedBodyBase):
    """Usage errors never consume stdin; lint stays read-only (RED on baseline where noted)."""

    def test_invalid_combo_missing_source_does_not_consume_stdin(self):
        # No --layout and no --repo-dir/--git-range: usage, even with a poison
        # stdin payload that would refuse if it were read.
        proc = run_cli(["lint", "--repo", REPO, "--body-file", "-", "--json"],
                       self.mdir, input_bytes=b"\xff\xfe poison " + b"p" * 4096)
        self.assertEqual(proc.returncode, ul.EXIT_USAGE,
                         "invalid flags must exit USAGE(4), not read stdin: %r" % combined_text(proc))
        self.assertNotIn("Traceback", combined_text(proc))

    def test_invalid_combo_bad_git_range_does_not_consume_stdin(self):
        proc = run_cli(["lint", "--repo", REPO, "--repo-dir", self.repo_dir,
                        "--git-range", "mainHEAD", "--title", self.title,
                        "--body-file", "-", "--json"],
                       self.mdir, input_bytes=b"\xff\xfe poison")
        self.assertEqual(proc.returncode, ul.EXIT_USAGE, combined_text(proc))

    def test_invalid_combo_git_mode_needs_title(self):
        proc = run_cli(["lint", "--repo", REPO, "--repo-dir", self.repo_dir,
                        "--git-range", "main..HEAD", "--body-file", "-", "--json"],
                       self.mdir, input_bytes=self.body.encode("utf-8"))
        self.assertEqual(proc.returncode, ul.EXIT_USAGE,
                         "git mode without --title must stay a usage error: %r" % combined_text(proc))

    def test_no_side_effects_files_memory_repo_hooks(self):
        before_files = set(os.listdir(self.tmp.name))
        before_hook = os.path.join(self.repo_dir, ".git", "hooks", "pre-push")
        hook_before = os.path.exists(before_hook)
        data = self.body.encode("utf-8")
        proc = run_cli(self.layout_args("-", ["--json"]), self.mdir, input_bytes=data)
        self.assertEqual(proc.returncode, 0, combined_text(proc))
        self.assertMemUnchanged("side effects")
        self.assertRepoClean("side effects")
        self.assertEqual(set(os.listdir(self.tmp.name)), before_files,
                         "stdin lint must not generate files")
        self.assertEqual(os.path.exists(before_hook), hook_before, "lint must not install hooks")
        lay_files = set(os.listdir(self.layout))
        self.assertEqual(lay_files, {"pr.json", "commits.json", "files.json"},
                         "stdin override must not rewrite the layout dir: %r" % (lay_files,))

    def test_devnull_stdin_is_controlled_empty_not_crash(self):
        # Closed stdin reads as empty: evaluated (template FAIL), never a crash.
        full = [sys.executable, os.path.join(HERE, "upstream_lint.py")] + self.layout_args("-", ["--json"])
        with open(os.devnull, "rb") as dn:
            proc = subprocess.run(full, stdin=dn, capture_output=True, timeout=60, env=cli_env(self.mdir))
        out = parse_json_stdout(self, proc, "devnull stdin")
        by_item = {i["item"]: i for i in out["items"]}
        self.assertEqual(by_item["template"]["status"], "FAIL",
                         "closed stdin must evaluate as empty body: %r" % (out,))
        assert_no_exception_leak(self, proc, "devnull stdin")


class HelpDocumentsStreamedInput(unittest.TestCase):
    """--help carries the synthetic piped example and recovery (RED on baseline)."""

    def help_text(self):
        proc = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"),
                               "lint", "--help"], capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        return proc.stdout.decode("utf-8", "replace")

    def test_help_documents_body_file_dash_stdin(self):
        text = self.help_text()
        self.assertIn("--body-file", text)
        low = text.lower()
        self.assertIn("stdin", low, "--help must document stdin intake:\n%s" % text)
        self.assertTrue("-" in text and ("'-'" in text or '"-"' in text or "--body-file -" in text),
                        "--help must name --body-file - explicitly:\n%s" % text)

    def test_help_shows_piped_example(self):
        text = self.help_text()
        self.assertIn("|", text, "--help must show a synthetic piped example:\n%s" % text)
        low = text.lower()
        self.assertTrue("printf" in low or "cat" in low or "echo" in low or "gh pr create" in low,
                        "--help piped example must use a synthetic producer:\n%s" % text)
        self.assertIn("--body-file -", text, "--help example must pipe into --body-file -:\n%s" % text)

    def test_help_documents_byte_limit(self):
        text = self.help_text()
        limit = documented_stdin_limit()
        self.assertIsNotNone(limit, "bounded stdin byte limit must be documented in --help:\n%s" % text)

    def test_help_mentions_nonzero_error_recovery(self):
        text = self.help_text()
        low = text.lower()
        hits = [k for k in ("retry", "next_action", "refused", "recovery", "exit 2") if k in low]
        self.assertTrue(hits, "--help must mention nonzero-error recovery:\n%s" % text)


if __name__ == "__main__":
    unittest.main()
