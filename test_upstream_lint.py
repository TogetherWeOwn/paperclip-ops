#!/usr/bin/env python3
"""Offline regression suite for upstream_lint.py and upstream_prepush_hook.sh (case_214e61d3).

Design: the BASELINE is a PR that passes every check; each case is one named
mutation of that baseline toward a measured failure, and asserts the ONE item
that must go red (plus exit codes). A linter that always failed would pass every
red case, so the green baseline is what makes them mean anything.

Assertions are on exit status and item names/status, never on message prose.
No network (the one live-looking case runs against a loopback stub), no token.
"""
import base64
import copy
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
# The PATH git on agent hosts is a credential-injecting shim (~2 s per call).
if os.path.exists("/usr/bin/git") and "GIT_BIN" not in os.environ:
    os.environ["GIT_BIN"] = "/usr/bin/git"
sys.path.insert(0, HERE)
import upstream_lint as ul  # noqa: E402

REPO = "acme/widget"
SHA = "f5bca33a44fb20d90972a0d15db880e0e3129440"

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


def good_commits():
    return [{"sha": SHA, "parents": ["p1"], "commit": {
        "message": "fix(scheduler): keep task sessions across project row updates",
        "author": {"name": "A", "email": "a@example.com"}}}]


def good_files():
    return [
        {"filename": "server/src/services/session.ts", "status": "modified", "additions": 5,
         "deletions": 1, "changes": 6, "patch": PATCH_SRC},
        {"filename": "server/src/__tests__/session.test.ts", "status": "added", "additions": 3,
         "deletions": 0, "changes": 3, "patch": PATCH_TEST},
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mdir = self.tmp.name
        self.mem = copy.deepcopy(MEMORY)
        self.write_mem()
        self.title = "fix(scheduler): keep task sessions across project row updates"
        self.body = BODY
        self.branch = "fix/session-fingerprint-churn"
        self.commits = good_commits()
        self.files = good_files()

    def write_mem(self):
        ul.save_memory(REPO, self.mem, self.mdir)

    def run_lint(self, stage="first-push", nobody=False):
        return ul.lint(REPO, self.title, self.body, self.branch, self.commits, self.files,
                       stage, self.mdir, nobody)

    def status(self, rep, item):
        return [s for s, i, _ in rep.items if i == item]

    def assertRed(self, rep, item):
        self.assertIn("FAIL", self.status(rep, item), "%s should FAIL: %r" % (item, rep.items))
        self.assertTrue(rep.failed)

    def assertClean(self, rep):
        bad = [(s, i, d) for s, i, d in rep.items if s in ("FAIL", "REFUSED")]
        self.assertEqual(bad, [], "baseline must be clean")


class Baseline(Base):
    def test_baseline_passes_every_check(self):
        rep = self.run_lint()
        self.assertClean(rep)
        names = {i for _, i, _ in rep.items}
        for want in ("memory", "title", "commits", "branch", "signoff", "template", "internal",
                     "tests", "budget", "claims", "rules"):
            self.assertIn(want, names)

    def test_ready_stage_baseline_needs_every_box_ticked(self):
        rep = self.run_lint("ready")
        self.assertRed(rep, "template")  # two boxes pending in the baseline body
        self.body = self.body.replace("- [ ]", "- [x]")
        self.assertClean(self.run_lint("ready"))


class Memory(Base):
    def test_missing_memory_refuses_never_passes(self):
        os.remove(ul.memory_path(REPO, self.mdir))
        rep = self.run_lint()
        self.assertTrue(rep.refused)
        self.assertFalse(any(s == "PASS" for s, i, _ in rep.items if i == "title"))

    def test_memory_for_another_repo_refuses(self):
        self.mem["repo"] = "other/repo"
        self.write_mem()
        self.assertTrue(self.run_lint().refused)

    def test_wrong_schema_version_refuses(self):
        self.mem["schema_version"] = 99
        self.write_mem()
        self.assertTrue(self.run_lint().refused)

    def test_unparseable_memory_refuses(self):
        with open(ul.memory_path(REPO, self.mdir), "w") as fh:
            fh.write("{not json")
        self.assertTrue(self.run_lint().refused)

    def test_unsafe_repo_name_refuses(self):
        with self.assertRaises(ul.Refused):
            ul.memory_path("../../etc/passwd", self.mdir)


class TitleAndCommits(Base):
    def test_title_not_conventional(self):
        self.title = "Keep sessions stable"
        self.assertRed(self.run_lint(), "title")

    def test_title_too_long(self):
        self.title = "fix(scheduler): " + "x" * 100
        self.assertRed(self.run_lint(), "title")

    def test_title_trailing_period(self):
        self.title = "fix(scheduler): keep sessions stable."
        self.assertRed(self.run_lint(), "title")

    def test_title_with_internal_id(self):
        self.title = "fix(scheduler): case_1cab5f7f keep sessions"
        self.assertRed(self.run_lint(), "title")

    def test_scope_less_title_and_commit_pass(self):
        # Conventional Commits allows scope-less headers; the scope rule was ours, not theirs.
        self.title = "fix: keep sessions stable"
        self.commits[0]["commit"]["message"] = "fix: keep sessions stable"
        self.assertClean(self.run_lint())

    def test_commit_subject_not_conventional(self):
        self.commits[0]["commit"]["message"] = "keep sessions stable"
        self.assertRed(self.run_lint(), "commits")

    def test_fixup_commit(self):
        self.commits.append({"sha": "b" * 40, "parents": ["p"], "commit": {"message": "fixup! fix(a): b"}})
        self.assertRed(self.run_lint(), "commits")

    def test_merge_commit(self):
        self.commits[0]["parents"] = ["p1", "p2"]
        self.assertRed(self.run_lint(), "commits")

    def test_no_commits(self):
        self.commits = []
        self.assertRed(self.run_lint(), "commits")


class Signoff(Base):
    def test_dco_missing_signoff(self):
        self.mem["signoff"]["dco"] = True
        self.write_mem()
        self.assertRed(self.run_lint(), "signoff")

    def test_dco_signoff_must_match_author(self):
        self.mem["signoff"]["dco"] = True
        self.write_mem()
        self.commits[0]["commit"]["message"] += "\n\nSigned-off-by: B <b@example.com>"
        self.assertRed(self.run_lint(), "signoff")

    def test_dco_ok(self):
        self.mem["signoff"]["dco"] = True
        self.write_mem()
        self.commits[0]["commit"]["message"] += "\n\nSigned-off-by: A <a@example.com>"
        self.assertClean(self.run_lint())

    def test_cla_without_evidence_fails(self):
        self.mem["signoff"]["cla"] = True
        self.write_mem()
        self.assertRed(self.run_lint(), "signoff")

    def test_dco_does_not_skip_independent_cla_requirement(self):
        self.mem["signoff"].update(dco=True, cla=True)
        self.write_mem()
        self.commits[0]["commit"]["message"] += "\n\nSigned-off-by: A <a@example.com>"
        self.assertRed(self.run_lint(), "signoff")

    def test_cla_with_evidence_passes(self):
        self.mem["signoff"].update(cla=True, cla_evidence="https://example.com/cla/rick")
        self.write_mem()
        self.assertClean(self.run_lint())


class Branch(Base):
    def test_uppercase_branch(self):
        self.branch = "Fix/Session"
        self.assertRed(self.run_lint(), "branch")

    def test_internal_id_branch(self):
        self.branch = "fix/case_1cab5f7f-sessions"
        self.assertRed(self.run_lint(), "branch")  # case-insensitive on purpose
        self.branch = "case_1cab5f7f-sessions"
        rep = self.run_lint()
        self.assertRed(rep, "branch")
        self.assertRed(rep, "internal")

    def test_empty_branch_refuses(self):
        self.branch = ""
        self.assertTrue(self.run_lint().refused)

    def test_long_branch(self):
        self.branch = "fix/" + "a" * 70
        self.assertRed(self.run_lint(), "branch")


class Template(Base):
    def test_missing_section(self):
        self.body = self.body.replace("## Risks\n\n- Low risk: stored sessions reset once after deploy\n\n", "")
        self.assertRed(self.run_lint(), "template")

    def test_placeholder_section(self):
        self.body = self.body.replace("- Low risk: stored sessions reset once after deploy", "-")
        self.assertRed(self.run_lint(), "template")

    def test_template_placeholder_text(self):
        self.body = self.body.replace("- drop the revision timestamp from the session fingerprint", "- TBD")
        self.assertRed(self.run_lint(), "template")

    def test_todo_text_is_a_placeholder(self):
        # Long enough and bulleted: only the placeholder check can catch this one.
        self.body = self.body.replace("- Low risk: stored sessions reset once after deploy",
                                      "- TODO describe the risk of resetting stored sessions")
        self.assertRed(self.run_lint(), "template")

    def test_empty_section_with_blank_line(self):
        self.body = self.body.replace("- ran `pnpm vitest server/src/__tests__/session.test.ts`: 12 passed", "")
        self.assertRed(self.run_lint(), "template")

    def test_verification_must_name_a_command(self):
        self.body = self.body.replace("`pnpm vitest server/src/__tests__/session.test.ts`", "the tests")
        self.assertRed(self.run_lint(), "template")

    def test_deleted_checkbox(self):
        self.body = self.body.replace("- [x] I have run tests locally and they pass\n", "")
        self.assertRed(self.run_lint(), "template")

    def test_unticked_box_not_on_pending_list(self):
        self.body = self.body.replace("- [x] I have run tests locally and they pass",
                                      "- [ ] I have run tests locally and they pass")
        self.assertRed(self.run_lint(), "template")

    def test_model_used_without_id(self):
        self.body = self.body.replace("Anthropic Claude Opus 5.5 (claude-opus-5-5), extended thinking", "Anthropic Claude")
        self.assertRed(self.run_lint(), "template")

    def test_model_used_provider_mismatch(self):
        self.body = self.body.replace("Anthropic Claude Opus 5.5 (claude-opus-5-5), extended thinking",
                                      "Anthropic muse-spark-1.3")
        self.assertRed(self.run_lint(), "template")

    def test_model_used_internal_alias(self):
        self.body = self.body.replace("Anthropic Claude Opus 5.5 (claude-opus-5-5), extended thinking",
                                      "Meta muse-canary 2")
        self.assertRed(self.run_lint(), "template")

    def test_human_authored_is_fine(self):
        self.body = self.body.replace("Anthropic Claude Opus 5.5 (claude-opus-5-5), extended thinking",
                                      "None — human-authored")
        self.assertClean(self.run_lint())

    def test_template_without_sections_in_memory_refuses(self):
        self.mem["template"]["sections"] = []
        self.write_mem()
        self.assertTrue(self.run_lint().refused)


class Internal(Base):
    def assertLeak(self, rep):
        self.assertRed(rep, "internal")

    def test_ticket_id_in_body(self):
        self.body += "\nRefs: case_214e61d3\n"
        self.assertLeak(self.run_lint())

    def test_ticket_id_in_commit(self):
        self.commits[0]["commit"]["message"] += "\n\nCard case_0dd2d987"
        self.assertLeak(self.run_lint())

    def test_pap_id_in_added_line(self):
        self.files[0]["patch"] += "+// see case_cdc74e44\n"
        self.assertLeak(self.run_lint())

    def test_added_source_line_starting_with_two_plus_signs_is_scanned(self):
        self.files[0]["patch"] = "@@ -1 +1,2 @@\n+++// see case_1cab5f7f\n"
        self.assertLeak(self.run_lint())

    def test_internal_path_in_added_line(self):
        self.files[0]["patch"] += "+const p = '/paperclip/instances/default';\n"
        self.assertLeak(self.run_lint())

    def test_tailnet_host_in_body(self):
        self.body += "\nSee host-408af3e7.example.invalid for the repro.\n"
        self.assertLeak(self.run_lint())

    def test_deploymentexample_in_body(self):
        self.body += "\nhttps://host-48c35084.example.invalid/TOG/issues/case_8c5fe4cc\n"
        self.assertLeak(self.run_lint())

    def test_case_insensitive_internal_id_in_pr_text(self):
        self.body += "\nRefs: case_214e61d3\n"
        self.assertLeak(self.run_lint())

    def test_case_insensitive_internal_host_in_added_line(self):
        self.files[0]["patch"] += "+const endpoint = 'https://host-caa6fee6.example.invalid';\n"
        self.assertLeak(self.run_lint())

    def test_localhost_in_body_is_leak_but_not_in_code(self):
        self.files[0]["patch"] += "+const url = 'http://localhost:3100';\n"
        self.assertClean(self.run_lint())  # legitimate in upstream code
        self.body += "\nRepro at http://localhost:3100\n"
        self.assertLeak(self.run_lint())

    def test_localhost_in_branch_and_commit_is_not_a_leak(self):
        # Prose is title+body only: a local address in the branch or a commit
        # message is as legitimate as one in code (tests, local dev docs).
        self.branch = "fix-localhost-probe"
        self.commits[0]["commit"]["message"] = "fix(scheduler): handle http://localhost:3100 in tests"
        self.assertClean(self.run_lint())

    def test_real_shaped_token_in_added_line(self):
        self.files[0]["patch"] += "+const t = 'ghp_" + "a" * 36 + "';\n"
        self.assertLeak(self.run_lint())

    def test_fake_test_fixture_secret_is_not_a_leak(self):
        self.files[0]["patch"] += "+const fake = 'sk-live-super-secret-value-123456';\n"
        self.assertClean(self.run_lint())

    def test_private_key_header(self):
        # Fragmented so secret-scan.sh (BEGIN ... PRIVATE KEY) stays clean;
        # the runtime string is still the full header (cf. test_host_ops_broker.py).
        self.files[0]["patch"] += "+-----BEGIN " + "PRIVATE KEY-----\n"
        self.assertLeak(self.run_lint())

    def test_unscannable_text_file_refuses(self):
        self.files.append({"filename": "src/big.ts", "status": "modified", "additions": 900,
                           "deletions": 0, "changes": 900})  # GitHub drops the patch on huge diffs
        self.assertTrue(self.run_lint().refused)

    def test_lockfile_and_binary_without_patch_are_fine(self):
        self.files.append({"filename": "pnpm-lock.yaml", "status": "modified", "additions": 900,
                           "deletions": 0, "changes": 900})
        self.files.append({"filename": "docs/logo.png", "status": "added", "additions": 0,
                           "deletions": 0, "changes": 0})
        self.assertClean(self.run_lint())

    def test_path_with_internal_id(self):
        self.files.append({"filename": "docs/case_214e61d3-notes.md", "status": "added", "additions": 1,
                           "deletions": 0, "changes": 1, "patch": "@@ -0,0 +1 @@\n+hello\n"})
        self.assertLeak(self.run_lint())

    def test_historical_path_with_internal_id_is_scanned_after_deletion(self):
        # Content is clean and the file is gone from the net diff, yet the path
        # stays in the published commit history.
        self.commits[0]["added_files"] = [
            {"filename": "docs/case_1cab5f7f.txt", "patch": "@@ -0,0 +1 @@\n+hello\n", "changes": 1},
            {"filename": "docs/case_1cab5f7f.txt", "patch": "@@ -1 +0,0 @@\n-hello\n", "changes": 1}]
        self.assertLeak(self.run_lint())

    def test_historical_ordinary_path_is_clean(self):
        self.commits[0]["added_files"] = [
            {"filename": "docs/notes.txt", "patch": "@@ -0,0 +1 @@\n+hello\n", "changes": 1},
            {"filename": "docs/notes.txt", "patch": "@@ -1 +0,0 @@\n-hello\n", "changes": 1}]
        self.assertClean(self.run_lint())

    def test_patchless_zero_count_added_or_modified_file_refuses(self):
        # GitHub reports an added or modified binary as 0/0 with no patch.
        for status in ("added", "modified"):
            with self.subTest(status=status):
                files = self.files + [{"filename": "server/src/services/opaque.data", "status": status,
                                       "additions": 0, "deletions": 0, "changes": 0}]
                rep = ul.lint(REPO, self.title, self.body, self.branch, self.commits, files,
                              "first-push", self.mdir)
                self.assertIn("REFUSED", self.status(rep, "internal"), rep.items)

    def test_patchless_empty_added_file_is_fine(self):
        # `__init__.py` / `.gitkeep`: GitHub sends 0/0 and no patch, but the blob is the empty one.
        self.files.append({"filename": "server/src/services/__init__.py", "status": "added", "additions": 0,
                           "deletions": 0, "changes": 0, "sha": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"})
        rep = self.run_lint()
        self.assertClean(rep)
        self.assertNotIn("REFUSED", self.status(rep, "internal"))

    def test_patchless_zero_count_rename_is_fine(self):
        self.files.append({"filename": "server/src/services/moved.ts", "status": "renamed",
                           "additions": 0, "deletions": 0, "changes": 0})
        rep = self.run_lint()
        self.assertClean(rep)
        self.assertNotIn("REFUSED", self.status(rep, "internal"))

    def test_nobody_mode_still_scans_commits_and_diff(self):
        self.files[0]["patch"] += "+// case_f052904a\n"
        rep = self.run_lint(nobody=True)
        self.assertRed(rep, "internal")


class TitleSubject(Base):
    def test_commit_subject_splits_on_lf_only(self):
        self.commits[0]["commit"]["message"] = "fix(scheduler): short\x0b" + "a" * 200
        self.assertRed(self.run_lint(), "commits")


class Tests(Base):
    def test_behavior_change_without_test(self):
        self.files = self.files[:1]
        self.assertRed(self.run_lint(), "tests")

    def test_test_file_without_assertions_does_not_count(self):
        self.files[1]["patch"] = "@@ -0,0 +1 @@\n+// placeholder\n"
        self.assertRed(self.run_lint(), "tests")

    def test_empty_test_declaration_is_not_an_assertion(self):
        self.files[1]["patch"] = "@@ -0,0 +1 @@\n+it('does a thing', () => {});\n"
        self.assertRed(self.run_lint(), "tests")

    def test_assert_identifier_without_assertion_does_not_count(self):
        self.files[1]["patch"] = "@@ -0,0 +1 @@\n+import { assert } from 'node:assert';\n"
        self.assertRed(self.run_lint(), "tests")

    def test_import_only_test_is_not_an_assertion(self):
        for line in ("import assert from 'node:assert';", "import assert, { ok } from 'node:assert';",
                     "const assert = require('node:assert');"):
            with self.subTest(line=line):
                self.files[1]["patch"] = "@@ -0,0 +1 @@\n+%s\n" % line
                self.assertRed(self.run_lint(), "tests")

    def test_multiline_fixture_text_is_not_an_assertion(self):
        fixtures = {
            "server/src/__tests__/session.test.ts":
                "@@ -0,0 +1,3 @@\n+const fixture = `\n+expect(value).toBe(1);\n+`;\n",
            "server/src/__tests__/test_session.py":
                '@@ -0,0 +1,3 @@\n+FIXTURE = """\n+assert value == 1\n+"""\n',
        }
        for name, patch in fixtures.items():
            with self.subTest(name=name):
                self.files[1]["filename"], self.files[1]["patch"] = name, patch
                self.assertRed(self.run_lint(), "tests")

    def test_assertion_after_a_closed_multiline_fixture_counts(self):
        self.files[1]["patch"] = ("@@ -0,0 +1,4 @@\n+const fixture = `\n+raw\n+`;\n"
                                  "+it('x', () => { expect(fixture).toBe(1); });\n")
        self.assertClean(self.run_lint())

    def test_new_test_file_with_a_backtick_regex_keeps_its_assertions(self):
        # A creation hunk starts at a known state: an unbalanced backtick (here in
        # a regex literal) must not discard the file's real assertions.
        self.files[1]["patch"] = ("@@ -0,0 +1,3 @@\n+it('x', () => {\n+  expect(1).toBe(1);\n"
                                  "+  expect(p).not.toMatch(/```/);\n")
        self.assertClean(self.run_lint())

    def test_creation_hunk_that_leaves_a_literal_open_names_the_file(self):
        # A regex literal with a backtick before the assertions masks them; keep
        # the verdict (fail closed) but say why.
        self.files[1]["patch"] = "@@ -0,0 +1,2 @@\n+const FENCE = /```/;\n+it('x', () => { expect(strip(FENCE)).toBe(1); });\n"
        rep = self.run_lint()
        self.assertRed(rep, "tests")
        self.assertIn("session.test.ts", [d for s, i, d in rep.items if i == "tests"][0])

    def test_dropped_hunk_is_named_in_the_failure(self):
        self.files[1]["patch"] = "@@ -7,0 +8,2 @@\n+expect(value).toBe(1);\n+`;\n"
        detail = [d for s, i, d in self.run_lint().items if i == "tests"][0]
        self.assertIn("unclosed multi-line literal", detail)
        self.assertIn("session.test.ts", detail)

    def test_module_extensions_track_template_literals(self):
        for ext in ("cjs", "mts", "cts", "mjs"):
            with self.subTest(ext=ext):
                self.files[1]["filename"] = "server/src/__tests__/session.test." + ext
                self.files[1]["patch"] = "@@ -0,0 +1,3 @@\n+const fixture = `\n+expect(value).toBe(1);\n+`;\n"
                self.assertRed(self.run_lint(), "tests")

    def test_assertions_after_a_masked_string_still_count(self):
        cases = {
            "server/src/__tests__/test_session.py": "@@ -0,0 +2 @@\n+def test_x():\n+    assert 'abc' == compute()\n",
            "server/src/services/SessionTest.java": "@@ -0,0 +1 @@\n+@Test void t() { if (a) {} else assert y; }\n",
            "server/src/__tests__/types.test.ts": "@@ -0,0 +1 @@\n+assert<IsExact<A, B>>(true);\n",
            "server/src/__tests__/macro_test.rs": "@@ -0,0 +1 @@\n+fn t() { assert_eq!(a(), 1); }\n",
            "server/src/__tests__/path_test.go": "@@ -0,0 +1,2 @@\n+p := `C:\\temp\\`\n+t.Errorf(\"%v\", p)\n",
        }
        for name, patch in cases.items():
            with self.subTest(name=name):
                self.files[1]["filename"], self.files[1]["patch"] = name, patch
                self.assertClean(self.run_lint())

    def test_hunk_that_starts_inside_an_unseen_literal_yields_no_assertion(self):
        # The opening backtick is before the hunk: the closing one looks like an
        # opener, the literal is still open at the hunk end, and nothing in the
        # hunk is provably executable.
        self.files[1]["patch"] = "@@ -7,0 +8,2 @@\n+expect(value).toBe(1);\n+`;\n"
        self.assertRed(self.run_lint(), "tests")

    def test_hunk_inside_a_literal_opened_before_it_is_read_as_code(self):
        # Documented limit of scan_test_code: the patch does not show the literal
        # this hunk starts inside, so assertion-shaped text in it counts.
        self.files[1]["patch"] = ("@@ -10,2 +10,3 @@\n fixture line\n+expect(value).toBe(1);\n"
                                  " fixture line\n")
        self.assertClean(self.run_lint())

    def test_each_hunk_is_read_from_outside_any_literal(self):
        # A literal that one hunk leaves open hides only that hunk; the next hunk
        # starts outside any literal, so its assertion counts.
        self.files[1]["patch"] = ("@@ -1,0 +1,2 @@\n+const fixture = `\n+raw\n"
                                  "@@ -40,0 +42,1 @@\n+it('x', () => { expect(fixture).toBe(1); });\n")
        self.assertClean(self.run_lint())

    def test_rust_prefixed_assert_macros_count(self):
        for line in ("debug_assert!(value == 1);", "debug_assert_eq!(value(), 1);",
                     "debug_assert_ne!(value(), 0);", "prop_assert_eq!(value(), 1);"):
            with self.subTest(line=line):
                self.files[1]["filename"] = "server/src/__tests__/macro_test.rs"
                self.files[1]["patch"] = "@@ -0,0 +1 @@\n+fn t() { %s }\n" % line
                self.assertClean(self.run_lint())

    def test_backtick_in_a_comment_does_not_open_a_literal(self):
        self.files[1]["patch"] = ("@@ -0,0 +1,3 @@\n+// the `rotate` helper\n"
                                  "+it('x', () => {\n+  expect(rotate()).toBe(false);\n")
        self.assertClean(self.run_lint())

    def test_python_assert_statement_counts(self):
        self.files[1]["filename"] = "server/src/__tests__/test_session.py"
        self.files[1]["patch"] = (
            "@@ -0,0 +1,2 @@\n+def test_refresh():\n+    assert refresh() is not None\n"
        )
        self.assertClean(self.run_lint())

    def test_inline_java_assertion_counts(self):
        self.files[1]["filename"] = "server/src/services/SessionTest.java"
        self.files[1]["patch"] = "@@ -0,0 +1 @@\n+@Test void works() { assert value == 1; }\n"
        self.assertClean(self.run_lint())

    def test_assertion_after_unicode_line_separator_counts(self):
        self.files[1]["patch"] = (
            "@@ -0,0 +1 @@\n+it('control', () => { " + chr(0x2028)
            + " expect(rotate()).toBe(false); });\n"
        )
        self.assertClean(self.run_lint())

    def test_assertion_like_comments_do_not_count(self):
        self.files[1]["patch"] = (
            "@@ -0,0 +1,5 @@\n"
            "+// assert(fake);\n"
            "+const note = \\\"expect(fake)\\\"; // should(fake);\n"
            "+/*\n"
            "+ assert(fake);\n"
            "+*/\n"
        )
        self.assertRed(self.run_lint(), "tests")

    def test_test_file_without_patch_refuses(self):
        # GitHub drops the patch on huge diffs: assertions cannot be measured,
        # so this refuses like unscanned source files, never a pass. A
        # patch-less "test" must not waive the same-package gate.
        del self.files[1]["patch"]
        self.files[1]["changes"] = 900
        rep = self.run_lint()
        self.assertTrue(rep.refused)
        self.assertIn("REFUSED", self.status(rep, "tests"))

    def test_renamed_test_file_without_patch_is_fine(self):
        # A pure rename sends no patch and 0 changes: like check_internal,
        # only changed files can refuse (Paperclip Review 3/5 at f57ac32).
        # The baseline test still covers the source change; the rename rides
        # along and must neither refuse nor waive the gate.
        self.files.append({"filename": "server/src/__tests__/renamed.test.ts",
                           "status": "renamed", "additions": 0, "deletions": 0,
                           "changes": 0})
        rep = self.run_lint()
        self.assertClean(rep)
        self.assertNotIn("REFUSED", self.status(rep, "tests"))

    def test_global_test_glob_needs_an_added_assertion(self):
        # A deleted, comment-only or assertion-free test under a global glob
        # must not waive the same-package gate for every package.
        self.mem["tests"]["global_test_globs"] = ["e2e/**"]
        self.write_mem()
        self.files = self.files[:1]
        glob_test = {"filename": "e2e/session.test.ts", "status": "added", "additions": 1,
                     "deletions": 0, "changes": 1}
        cases = {
            "comment-only": dict(glob_test, patch="@@ -0,0 +1 @@\n+// expect(rotate()).toBe(false);\n"),
            "deleted": dict(glob_test, status="removed", additions=0, deletions=3, changes=3,
                            patch="@@ -1,3 +0,0 @@\n-it('x', () => {\n-  expect(rotate()).toBe(false);\n-});\n"),
            "no assertion": dict(glob_test, patch="@@ -0,0 +1 @@\n+const unused = 1;\n"),
        }
        for label, case in cases.items():
            with self.subTest(label=label):
                self.files = self.files[:1] + [case]
                self.assertRed(self.run_lint(), "tests")
        self.files = self.files[:1] + [dict(glob_test, patch=PATCH_TEST)]
        self.assertClean(self.run_lint())

    def test_patchless_zero_count_test_file_refuses(self):
        self.files[1].pop("patch")
        self.files[1].update(status="modified", additions=0, deletions=0, changes=0)
        rep = self.run_lint()
        self.assertIn("REFUSED", self.status(rep, "tests"), rep.items)

    def test_test_in_other_package_does_not_count(self):
        self.files[1]["filename"] = "cli/src/__tests__/x.test.ts"
        self.assertRed(self.run_lint(), "tests")

    def test_docs_only_needs_no_test(self):
        self.files = [{"filename": "README.md", "status": "modified", "additions": 2, "deletions": 0,
                       "changes": 2, "patch": "@@ -1 +1,2 @@\n+more docs\n"}]
        rep = self.run_lint()
        self.assertClean(rep)

    def test_pure_deletion_needs_no_test(self):
        self.files = [{"filename": "server/src/old.ts", "status": "removed", "additions": 0,
                       "deletions": 10, "changes": 10}]
        self.assertClean(self.run_lint())

    def test_exempt_glob(self):
        self.files = self.files[:1]
        self.mem["tests"]["exempt_globs"] = ["server/src/services/*"]
        self.write_mem()
        self.assertClean(self.run_lint())

    def test_ci_workflow_change_is_not_behavior(self):
        self.files = [{"filename": ".github/workflows/ci.yml", "status": "modified", "additions": 2,
                       "deletions": 0, "changes": 2, "patch": "@@ -1 +1,2 @@\n+name: x\n"}]
        self.assertClean(self.run_lint())

    def test_package_mapping_uses_package_roots(self):
        self.files = [
            {"filename": "packages/db/src/a.ts", "status": "modified", "additions": 3, "deletions": 0,
             "changes": 3, "patch": PATCH_SRC},
            {"filename": "packages/shared/src/b.ts", "status": "modified", "additions": 3, "deletions": 0,
             "changes": 3, "patch": PATCH_SRC},
            {"filename": "packages/db/src/a.test.ts", "status": "added", "additions": 3, "deletions": 0,
             "changes": 3, "patch": PATCH_TEST},
        ]
        rep = self.run_lint()
        self.assertRed(rep, "tests")  # packages/shared has no test
        self.files.append({"filename": "packages/shared/src/b.test.ts", "status": "added", "additions": 3,
                           "deletions": 0, "changes": 3, "patch": PATCH_TEST})
        self.assertClean(self.run_lint())

    def test_tests_not_required_when_memory_says_so(self):
        self.files = self.files[:1]
        self.mem["tests"]["required"] = False
        self.write_mem()
        self.assertClean(self.run_lint())


class Budget(Base):
    def big(self, n, name="server/src/big%d.ts", lines=20):
        return [{"filename": name % i, "status": "added", "additions": lines, "deletions": 0,
                 "changes": lines, "patch": "@@ -0,0 +1 @@\n+x\n"} for i in range(n)]

    def test_too_many_lines(self):
        self.files += [{"filename": "server/src/x.ts", "status": "added", "additions": 200, "deletions": 0,
                        "changes": 200, "patch": PATCH_SRC}]
        self.assertRed(self.run_lint(), "budget")

    def test_too_many_files(self):
        # One line each: only the FILE count is over budget (7 files, limit 5; 11 lines, limit 100).
        self.files += self.big(5, lines=1)
        rep = self.run_lint()
        self.assertRed(rep, "budget")
        self.assertIn("files", [d for s, i, d in rep.items if i == "budget"][0])
        self.assertNotIn("changed lines", [d for s, i, d in rep.items if i == "budget"][0])

    def test_lock_and_generated_do_not_count(self):
        self.files += [{"filename": "pnpm-lock.yaml", "status": "modified", "additions": 5000,
                        "deletions": 0, "changes": 5000},
                       {"filename": "dist/out.js", "status": "added", "additions": 5000, "deletions": 0,
                        "changes": 5000}]
        self.assertClean(self.run_lint())

    def test_maintainer_split_request_lowers_the_budget(self):
        self.files += [{"filename": "server/src/x.ts", "status": "added", "additions": 60, "deletions": 0,
                        "changes": 60, "patch": PATCH_SRC}]
        self.assertClean(self.run_lint())  # 69 lines under the default budget of 100
        self.mem["feedback"].append({"id": "maintainer-1-1", "kind": "maintainer", "split_request": True,
                                     "status": "recorded", "category": "scope", "paths": []})
        self.write_mem()
        self.assertRed(self.run_lint(), "budget")
        self.mem["feedback"][0]["status"] = "resolved"
        self.write_mem()
        self.assertClean(self.run_lint())


    def test_commenter_split_request_does_not_lower_the_budget(self):
        self.files += [{"filename": "server/src/x.ts", "status": "added", "additions": 60, "deletions": 0,
                        "changes": 60, "patch": PATCH_SRC}]
        self.mem["feedback"].append({"id": "commenter-1-1", "kind": "commenter", "split_request": True,
                                     "status": "recorded", "category": "scope", "paths": []})
        self.write_mem()
        self.assertClean(self.run_lint())


class Claims(Base):
    def with_text(self, text):
        self.body = self.body.replace("- Low risk: stored sessions reset once after deploy", text)

    def test_over_promising_phrases(self):
        for phrase in ("This guarantees stability", "It is 100% safe", "A perfect fix", "a robust solution",
                       "This is production-ready", "It will never fail", "fixes all the bugs",
                       "seamless upgrade", "completely eliminates the race"):
            self.with_text("- " + phrase)
            self.assertRed(self.run_lint(), "claims")
            self.body = BODY

    def test_negated_claim_is_not_a_claim(self):
        self.with_text("- This is not a general secret-removal guarantee")
        self.assertClean(self.run_lint())

    def test_negation_does_not_excuse_will_never(self):
        self.with_text("- This will never fail")
        self.assertRed(self.run_lint(), "claims")
        # A negator close by must not launder an absolute claim either.
        self.body = BODY
        self.with_text("- It does not hurt anyone and it will never fail again")
        self.assertRed(self.run_lint(), "claims")

    def test_exclamation_emoji_and_shouting(self):
        for phrase in ("- Fixed it!", "- Fixed 🎉", "- This is IMPORTANT behaviour"):
            self.with_text(phrase)
            self.assertRed(self.run_lint(), "claims")
            self.body = BODY

    def test_code_spans_are_not_prose(self):
        self.with_text("- the `ALWAYS_WORKS!` flag and `robust` mode are named in code")
        self.assertClean(self.run_lint())

    def test_allow_list(self):
        self.with_text("- a robust fix for the session churn problem")
        self.assertRed(self.run_lint(), "claims")
        self.mem["claims"]["allow"] = ["robust"]
        self.write_mem()
        self.assertClean(self.run_lint())

    def test_long_sentence_warns_only(self):
        self.with_text("- " + " ".join(["word"] * 45) + ".")
        rep = self.run_lint()
        self.assertClean(rep)
        self.assertIn("WARN", self.status(rep, "tone"))

    def test_skipped_without_pr_text(self):
        self.body = "!!! 100% perfect"
        rep = self.run_lint(nobody=True)
        self.assertNotIn("claims", [i for s, i, _ in rep.items if s == "FAIL"])


class Rules(Base):
    def rule(self, **kw):
        base = {"id": "R-9", "scope": "body", "kind": "forbid", "pattern": "x", "message": "m"}
        base.update(kw)
        self.mem["rules"] = [base]
        self.write_mem()

    def test_forbid(self):
        self.rule(kind="forbid", pattern=r"Low risk")
        self.assertRed(self.run_lint(), "rules")

    def test_require(self):
        self.rule(kind="require", pattern=r"never-present")
        self.assertRed(self.run_lint(), "rules")
        self.rule(kind="require", pattern=r"Low risk")
        self.assertClean(self.run_lint())

    def test_require_any_with_counts(self):
        alts = [{"pattern": r"(?i)fixes #\d+", "min_count": 1}, {"pattern": r"(?m)^\*\*\w+\*\*$", "min_count": 3}]
        self.rule(kind="require_any", scope="section:Linked Issues or Issue Description", alternatives=alts)
        self.assertClean(self.run_lint())
        self.body = self.body.replace("Fixes #15142.", "Nothing linked.")
        self.assertRed(self.run_lint(), "rules")
        self.body = self.body.replace("Nothing linked.", "**What**\nx\n**Why**\ny\n**How**\nz")
        self.assertClean(self.run_lint())

    def test_max_len(self):
        self.rule(kind="max_len", scope="title", max=10)
        self.assertRed(self.run_lint(), "rules")

    def test_scope_paths_and_added(self):
        self.rule(kind="forbid", scope="paths", pattern=r"\.env$")
        self.files.append({"filename": "a/.env", "status": "added", "additions": 1, "deletions": 0,
                           "changes": 1, "patch": "@@ -0,0 +1 @@\n+X=1\n"})
        self.assertRed(self.run_lint(), "rules")
        self.rule(kind="forbid", scope="added", pattern=r"console\.log")
        self.files = good_files()
        self.files[0]["patch"] += "+console.log(1)\n"
        self.assertRed(self.run_lint(), "rules")

    def test_applies_when_paths(self):
        self.rule(kind="require", scope="paths", pattern=r"CHANGELOG\.md", applies_when_paths=["cli/*"])
        self.assertClean(self.run_lint())  # no cli/ path changed: rule does not apply
        self.files.append({"filename": "cli/main.ts", "status": "modified", "additions": 1, "deletions": 0,
                           "changes": 1, "patch": "@@ -1 +1,2 @@\n+x\n"})
        self.files.append({"filename": "cli/main.test.ts", "status": "added", "additions": 3, "deletions": 0,
                           "changes": 3, "patch": PATCH_TEST})
        self.assertRed(self.run_lint(), "rules")

    def test_warn_severity_does_not_fail(self):
        self.rule(kind="forbid", pattern=r"Low risk", severity="warn")
        rep = self.run_lint()
        self.assertClean(rep)
        self.assertIn("WARN", self.status(rep, "rules"))

    def test_unknown_kind_and_bad_regex_refuse(self):
        self.rule(kind="explode")
        self.assertTrue(self.run_lint().refused)
        self.rule(kind="forbid", pattern="(")
        self.assertTrue(self.run_lint().refused)
        self.rule(kind="forbid", scope="nowhere")
        self.assertTrue(self.run_lint().refused)

    def test_nobody_mode_skips_text_scoped_rules_only(self):
        self.rule(kind="require", scope="body", pattern=r"never-present")
        self.assertClean(self.run_lint(nobody=True))
        self.rule(kind="forbid", scope="added", pattern=r"fingerprint")
        self.assertRed(self.run_lint(nobody=True), "rules")


class Advisory(Base):
    def test_feedback_on_touched_path_warns_and_never_fails(self):
        self.mem["feedback"] = [
            {"id": "greptile-1-1", "kind": "greptile", "category": "tests", "status": "recorded",
             "paths": ["server/src/services/session.ts"], "excerpt": "needs a test"},
            {"id": "greptile-1-2", "kind": "greptile", "category": "tests", "status": "recorded",
             "paths": ["elsewhere.ts"], "excerpt": "x"}]
        self.write_mem()
        rep = self.run_lint()
        self.assertClean(rep)
        self.assertEqual(self.status(rep, "advisory").count("WARN"), 2)  # path hit + recurring category

    def test_resolved_feedback_is_silent(self):
        self.mem["feedback"] = [{"id": "g1", "kind": "greptile", "category": "tests", "status": "resolved",
                                 "paths": ["server/src/services/session.ts"], "excerpt": "x"}]
        self.write_mem()
        self.assertEqual(self.status(self.run_lint(), "advisory"), [])


class Batch(Base):
    def opens(self, *prs):
        return list(prs)

    def check(self, opens, split=None):
        return ul.batch_check(REPO, self.branch, self.title, self.files, opens, ul.load_memory(REPO, self.mdir), split)

    def test_no_other_prs_passes(self):
        self.assertFalse(self.check([]).failed)

    def test_overlap_with_our_open_pr_fails(self):
        rep = self.check([{"number": 7, "head_ref": "fix/other", "files": ["server/src/services/session.ts"]}])
        self.assertTrue(rep.failed)

    def test_same_branch_is_not_overlap(self):
        rep = self.check([{"number": 7, "head_ref": self.branch, "files": ["server/src/services/session.ts"]}])
        self.assertFalse(rep.failed)

    def test_disjoint_files_pass(self):
        rep = self.check([{"number": 7, "head_ref": "fix/other", "files": ["cli/a.ts"]}])
        self.assertFalse(rep.failed)

    def test_open_pr_cap(self):
        rep = self.check([{"number": n, "head_ref": "b%d" % n, "files": ["z%d" % n]} for n in (1, 2)])
        self.assertTrue(rep.failed)

    def split_comment(self, association="MEMBER", body="Please split this pull request into smaller changes.", issue=7):
        return {"id": 123, "issue_url": "%s/repos/acme/widget/issues/%d" % (ul.api_base(), issue),
                "user": {"login": "maintainer"}, "author_association": association, "body": body}

    def test_verified_split_request_does_not_waive_open_pr_cap(self):
        o = [
            {"number": 7, "head_ref": "fix/other", "files": ["server/src/services/session.ts"]},
            {"number": 8, "head_ref": "fix/another", "files": ["cli/other.ts"]},
        ]
        url = "https://github.com/acme/widget/pull/7#issuecomment-123"
        with mock.patch.object(ul, "api_get", return_value=self.split_comment()) as get:
            self.assertTrue(ul.verified_split_request(REPO, url, {"7"}))
            get.reset_mock()
            rep = self.check(o, url)
        self.assertTrue(rep.failed)
        get.assert_called_once_with("/repos/acme/widget/issues/comments/123")

    def test_verified_maintainer_split_comment_allows_overlap(self):
        o = [{"number": 7, "head_ref": "fix/other", "files": ["server/src/services/session.ts"]}]
        url = "https://github.com/acme/widget/pull/7#issuecomment-123"
        with mock.patch.object(ul, "api_get", return_value=self.split_comment()) as get:
            self.assertFalse(self.check(o, url).failed)
        get.assert_called_once_with("/repos/acme/widget/issues/comments/123")

    def test_untrusted_or_unrelated_comment_cannot_allow_overlap(self):
        o = [{"number": 7, "head_ref": "fix/other", "files": ["server/src/services/session.ts"]}]
        url = "https://github.com/acme/widget/pull/7#issuecomment-123"
        malformed = self.split_comment()
        malformed["user"] = "maintainer"
        invalid = (
            self.split_comment(association="CONTRIBUTOR"),
            self.split_comment(body="Please do not split this PR."),
            self.split_comment(body="Looks good to me."),
            self.split_comment(issue=8),
            malformed,
        )
        for comment in invalid:
            with self.subTest(comment=comment), mock.patch.object(ul, "api_get", return_value=comment):
                self.assertTrue(self.check(o, url).failed)
        with mock.patch.object(ul, "api_get") as get:
            other_repo = "https://github.com/elsewhere/widget/pull/7#issuecomment-123"
            self.assertTrue(self.check(o, other_repo).failed)
            unrelated = "https://github.com/acme/widget/pull/8#issuecomment-123"
            self.assertTrue(self.check(o, unrelated).failed)
            get.assert_not_called()
        with mock.patch.object(ul, "api_get", side_effect=ul.Refused("comment unreadable")):
            with self.assertRaises(ul.Refused):
                self.check(o, url)

    def test_split_link_must_be_a_public_github_link(self):
        o = [{"number": 7, "head_ref": "fix/other", "files": ["server/src/services/session.ts"]}]
        self.assertTrue(self.check(o, "because I said so").failed)
        self.assertTrue(self.check(o, "http://localhost/x").failed)
        # And on its own, with nothing overlapping: a bad justification is still refused.
        disjoint = [{"number": 7, "head_ref": "fix/other", "files": ["cli/a.ts"]}]
        self.assertTrue(self.check(disjoint, "because I said so").failed)
        self.assertFalse(self.check(disjoint).failed)


def git(cwd, *args):
    return subprocess.run([os.environ.get("GIT_BIN", "git"), "-C", cwd, *args], capture_output=True, text=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="A", GIT_AUTHOR_EMAIL="a@example.com",
                                   GIT_COMMITTER_NAME="A", GIT_COMMITTER_EMAIL="a@example.com")).stdout


def make_repo(root):
    git(root, "init", "-q", "-b", "main")
    os.makedirs(os.path.join(root, "server/src/services"))
    with open(os.path.join(root, "server/src/services/session.ts"), "w") as fh:
        fh.write("const a = 1;\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore(init): base")
    git(root, "checkout", "-q", "-b", "fix/session-fingerprint-churn")
    with open(os.path.join(root, "server/src/services/session.ts"), "a") as fh:
        fh.write("const fingerprint = hash(a);\n")
    os.makedirs(os.path.join(root, "server/src/__tests__"))
    with open(os.path.join(root, "server/src/__tests__/session.test.ts"), "w") as fh:
        fh.write("it('keeps the session', () => {\n  expect(rotate()).toBe(false);\n});\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fix(scheduler): keep task sessions across project row updates")


class GitMode(Base):
    def setUp(self):
        super().setUp()
        self.repo_dir = os.path.join(self.tmp.name, "clone")
        os.makedirs(self.repo_dir)
        make_repo(self.repo_dir)

    def test_layout_and_lint_from_git_range(self):
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        self.assertEqual(len(lay["commits"]), 1)
        self.assertEqual({f["filename"] for f in lay["files"]},
                         {"server/src/services/session.ts", "server/src/__tests__/session.test.ts"})
        rep = ul.lint(REPO, self.title, self.body, "fix/session-fingerprint-churn", lay["commits"], lay["files"],
                      "first-push", self.mdir)
        self.assertClean(rep)

    def test_git_numstat_disables_configured_textconv(self):
        with open(os.path.join(self.repo_dir, ".gitattributes"), "w") as fh:
            fh.write("*.ts diff=upstream-lint-test\n")
        git(self.repo_dir, "add", ".gitattributes")
        git(self.repo_dir, "commit", "-q", "-m", "chore(test): configure textconv driver")
        git(self.repo_dir, "config", "diff.upstream-lint-test.textconv", "printf converted")

        path = os.path.join(self.repo_dir, "server/src/services/session.ts")
        with open(path, "a") as fh:
            fh.write("// see case_1cab5f7f\n")
        git(self.repo_dir, "commit", "-q", "-am", "fix(scheduler): scan raw source change")

        real_git = ul.git
        numstat_calls = []

        def check_numstat_flags(repo_dir, *args):
            if "--numstat" in args:
                numstat_calls.append(args)
                self.assertIn("--no-textconv", args)
            return real_git(repo_dir, *args)

        with mock.patch.object(ul, "git", side_effect=check_numstat_flags):
            lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)

        self.assertEqual(len(numstat_calls), 2)
        source = next(f for f in lay["files"] if f["filename"] == "server/src/services/session.ts")
        self.assertIn("+// see case_1cab5f7f", source["patch"])
        commit_patches = [f["patch"] for c in lay["commits"] for f in c["added_files"]
                          if f["filename"] == "server/src/services/session.ts"]
        self.assertTrue(any("+// see case_1cab5f7f" in patch for patch in commit_patches))
        rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"],
                      lay["files"], "first-push", self.mdir)
        self.assertRed(rep, "internal")

    def test_git_mode_ignores_configured_diff_prefixes_and_scans(self):
        git(self.repo_dir, "config", "diff.noprefix", "true")
        git(self.repo_dir, "config", "diff.srcPrefix", "custom-source/")
        git(self.repo_dir, "config", "diff.dstPrefix", "custom-destination/")
        path = os.path.join(self.repo_dir, "server/src/services/session.ts")
        with open(path, "a") as fh:
            fh.write("// see case_1cab5f7f\n")
        git(self.repo_dir, "commit", "-q", "-am", "fix(scheduler): add internal reference")

        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertRed(rep, "internal")

    def test_git_mode_preserves_quoted_paths_with_newlines(self):
        name = "server/src/services/quoted\nname.ts"
        path = os.path.join(self.repo_dir, name)
        with open(path, "w", newline="") as fh:
            fh.write("const value = 1;\n// see case_1cab5f7f\n")
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): add quoted path")

        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        self.assertIn(name, {f["filename"] for f in lay["files"]})
        self.assertIn(name, {f["filename"] for c in lay["commits"] for f in c["added_files"]})
        rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertRed(rep, "internal")

    def test_git_mode_scans_control_separators_in_added_lines(self):
        # One separator per commit: each alone must reveal the id, so a regression
        # in any single one (lone CR included) cannot hide behind the others.
        path = os.path.join(self.repo_dir, "server/src/services/session.ts")
        for separator in ("\v", "\f", "\x1c", "\x85", chr(0x2028), "\r"):
            with self.subTest(separator=repr(separator)):
                with open(path, "a", newline="") as fh:
                    fh.write("// control%sTOG-1800\n" % separator)
                git(self.repo_dir, "commit", "-q", "-am", "fix(scheduler): scan control separators")
                lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
                rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"], lay["files"],
                              "first-push", self.mdir)
                self.assertRed(rep, "internal")
                git(self.repo_dir, "reset", "-q", "--hard", "HEAD~1")

    def test_git_mode_reads_paths_with_spaces_and_tabs(self):
        # Git quotes a tab but not a space: the diff header cannot be split at a space.
        for name in ("docs/My Notes.md", "my docs/x.md", "a b/c d.md", "docs/tab\tname.md"):
            with self.subTest(name=name):
                path = os.path.join(self.repo_dir, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    fh.write("clean text\n")
                git(self.repo_dir, "add", "-A")
                git(self.repo_dir, "commit", "-q", "-m", "docs(notes): add a note")
                lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
                self.assertIn(name, {f["filename"] for f in lay["files"]})
                rep = ul.lint(REPO, self.title, self.body, "fix/notes", lay["commits"], lay["files"], "first-push", self.mdir)
                self.assertClean(rep)
                with open(path, "a") as fh:
                    fh.write("// see case_1cab5f7f\n")
                git(self.repo_dir, "commit", "-q", "-am", "docs(notes): extend the note")
                lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
                self.assertRed(ul.lint(REPO, self.title, self.body, "fix/notes", lay["commits"], lay["files"],
                                       "first-push", self.mdir), "internal")
                git(self.repo_dir, "reset", "-q", "--hard", "HEAD~2")

    def test_git_mode_reads_a_file_replaced_by_a_symlink(self):
        # A type change is two `diff --git` sections for one numstat entry.
        path = os.path.join(self.repo_dir, "server/src/services/session.ts")
        os.remove(path)
        os.symlink("../other.ts", path)
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): link the session module")
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        self.assertIn("server/src/services/session.ts", {f["filename"] for f in lay["files"]})

    def test_git_mode_marks_deleted_files_removed(self):
        git(self.repo_dir, "rm", "-q", "server/src/__tests__/session.test.ts")
        git(self.repo_dir, "commit", "-q", "-m", "test(scheduler): drop the session test")
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        self.assertEqual([f["status"] for f in lay["files"] if f["filename"].endswith("session.test.ts")], [])
        self.assertEqual(ul.parse_name_status_z("D\0a.ts\0A\0b.ts\0M\0c.ts\0"),
                         {"a.ts": "removed", "b.ts": "added", "c.ts": "modified"})

    def test_git_mode_does_not_let_control_separator_hide_test_assertion(self):
        path = os.path.join(self.repo_dir, "server/src/__tests__/session.test.ts")
        with open(path, "a", newline="") as fh:
            fh.write("it('control', () => { " + chr(0x2028) + " expect(rotate()).toBe(false); });\n")
        git(self.repo_dir, "commit", "-q", "-am", "fix(scheduler): preserve test assertions")

        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertClean(rep)

    def test_git_mode_catches_an_internal_id_in_an_added_line(self):
        with open(os.path.join(self.repo_dir, "server/src/services/session.ts"), "a") as fh:
            fh.write("// see case_1cab5f7f\n")
        git(self.repo_dir, "commit", "-q", "-am", "fix(scheduler): more")
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertRed(rep, "internal")

    def test_git_mode_scans_added_lines_from_every_commit_with_commit_delimiter_text(self):
        path = os.path.join(self.repo_dir, "server/src/services/session.ts")
        with open(path, "a") as fh:
            fh.write("// commit:case_1cab5f7f\n")
        git(self.repo_dir, "commit", "-q", "-am", "fix(scheduler): add temporary note")
        with open(path) as fh:
            contents = fh.read()
        with open(path, "w") as fh:
            fh.write(contents.replace("// commit:case_1cab5f7f\n", ""))
        git(self.repo_dir, "commit", "-q", "-am", "fix(scheduler): remove temporary note")
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertRed(rep, "internal")

    def test_git_mode_scans_a_path_added_then_deleted_before_head(self):
        path = os.path.join(self.repo_dir, "docs", "case_1cab5f7f.txt")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("hello\n")
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "docs(notes): add a note")
        os.remove(path)
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "docs(notes): drop the note")
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        self.assertNotIn("docs/case_1cab5f7f.txt", {f["filename"] for f in lay["files"]})
        rep = ul.lint(REPO, self.title, self.body, "fix/note", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertRed(rep, "internal")

    def test_split_diff_refuses_what_it_cannot_attribute(self):
        sect = "diff --git a/x.ts b/x.ts\nindex 1..2 100644\n--- a/x.ts\n+++ b/x.ts\n@@ -0,0 +1 @@\n+hello\n"
        self.assertEqual(ul.split_diff(sect, ["x.ts"]), {"x.ts": "@@ -0,0 +1 @@\n+hello\n"})
        for label, text, paths in (
                ("more files than numstat", sect + sect.replace("x.ts", "y.ts"), ["x.ts"]),
                ("fewer files than numstat", sect, ["x.ts", "y.ts"]),
                ("header without a/ b/ prefixes", sect.replace("a/x.ts b/x.ts", "x.ts x.ts"), ["x.ts"]),
                ("header for another path", sect, ["z.ts"]),
                ("a and b name different paths", sect.replace("b/x.ts\nindex", "b/y.ts\nindex", 1).replace(
                    "diff --git a/x.ts b/x.ts", "diff --git a/x.ts b/y.ts"), ["x.ts"])):
            with self.subTest(label=label), self.assertRaises(ul.Refused):
                ul.split_diff(text, paths)

    def test_layout_refuses_a_changed_file_whose_patch_was_not_recovered(self):
        real = ul.split_diff
        with mock.patch.object(ul, "split_diff", side_effect=lambda full, paths: {p: "" for p in paths}):
            with self.assertRaisesRegex(ul.Refused, "no parsed hunk"):
                ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        self.assertIs(real, ul.split_diff)

    def test_unquote_c_decodes_git_quoted_paths(self):
        self.assertEqual(ul.unquote_c(r"a/tab\there\\x\"q"), 'a/tab\there\\x"q')
        self.assertEqual(ul.unquote_c(r"a/caf\303\251"), "a/caf\u00e9")
        with self.assertRaises(ul.Refused):
            ul.unquote_c("a/bad\\")

    def test_patch_lines_strip_one_trailing_cr_and_split_on_lf_only(self):
        self.assertEqual(ul.patch_lines("a\r\nb\x0bc\u2028d\ne"), ["a", "b\x0bc\u2028d", "e"])

    def test_git_mode_refuses_unknown_binary_source(self):
        path = os.path.join(self.repo_dir, "server/src/services/opaque.data")
        with open(path, "wb") as fh:
            fh.write(b"binary\x00payload")
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): add opaque source")
        with self.assertRaisesRegex(ul.Refused, "binary or unknown"):
            ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)

    def test_git_mode_refuses_unknown_binary_removed_before_head(self):
        path = os.path.join(self.repo_dir, "server/src/services/opaque.data")
        with open(path, "wb") as fh:
            fh.write(b"binary\x00payload")
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): add temporary binary")
        os.remove(path)
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): remove temporary binary")
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        rep = ul.lint(REPO, self.title, self.body, "fix/control-separator", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertTrue(rep.refused, rep.items)

    def test_git_mode_allows_known_binary_assets(self):
        path = os.path.join(self.repo_dir, "assets/logo.png")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\x00data")
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): add logo asset")
        lay = ul.layout_from_git(self.repo_dir, "main", "HEAD", self.title, self.body)
        rep = ul.lint(REPO, self.title, self.body, "fix/logo-asset", lay["commits"], lay["files"], "first-push", self.mdir)
        self.assertClean(rep)

    def test_cli_exit_codes(self):
        bodyf = os.path.join(self.tmp.name, "body.md")
        with open(bodyf, "w") as fh:
            fh.write(self.body)
        env = dict(os.environ, UPSTREAM_MEMORY_DIR=self.mdir)
        base = [sys.executable, os.path.join(HERE, "upstream_lint.py"), "lint", "--repo", REPO,
                "--repo-dir", self.repo_dir, "--git-range", "main..HEAD", "--title", self.title,
                "--body-file", bodyf, "--branch", "fix/session-fingerprint-churn"]
        self.assertEqual(subprocess.run(base, env=env, capture_output=True).returncode, 0)
        bad = subprocess.run(base[:-1] + ["Bad Branch"], env=env, capture_output=True)
        self.assertEqual(bad.returncode, 1)
        nomem = subprocess.run(base, env=dict(env, UPSTREAM_MEMORY_DIR=self.tmp.name + "/none"), capture_output=True)
        self.assertEqual(nomem.returncode, 2)
        usage = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"), "lint"], env=env, capture_output=True)
        self.assertEqual(usage.returncode, 4)
        nocmd = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py")], env=env, capture_output=True)
        self.assertEqual(nocmd.returncode, 2)

    def test_gate_line_formats(self):
        bodyf = os.path.join(self.tmp.name, "body.md")
        with open(bodyf, "w") as fh:
            fh.write(self.body)
        env = dict(os.environ, UPSTREAM_MEMORY_DIR=self.mdir)
        out = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"), "lint", "--repo", REPO,
                              "--repo-dir", self.repo_dir, "--git-range", "main..HEAD", "--title", self.title,
                              "--body-file", bodyf, "--gate-line"], env=env, capture_output=True, text=True)
        self.assertTrue(out.stdout.startswith("PASS\t"), out.stdout)
        self.assertEqual(len(out.stdout.strip().splitlines()), 1)


class LayoutDir(Base):
    def write_layout(self, d):
        with open(os.path.join(d, "pr.json"), "w") as fh:
            json.dump({"title": self.title, "body": self.body, "head": {"sha": SHA, "ref": self.branch}}, fh)
        with open(os.path.join(d, "commits.json"), "w") as fh:
            json.dump(self.commits, fh)
        with open(os.path.join(d, "files.json"), "w") as fh:
            json.dump(self.files, fh)

    def test_layout_dir_roundtrip_and_missing_files_refuses(self):
        d = os.path.join(self.tmp.name, "lay")
        os.makedirs(d)
        self.write_layout(d)
        lay = ul.layout_from_dir(d)
        self.assertEqual(lay["head"], SHA)
        os.remove(os.path.join(d, "files.json"))
        with self.assertRaises(ul.Refused):
            ul.layout_from_dir(d)


class MemoryTools(Base):
    def test_parse_template_extracts_sections_and_boxes(self):
        t = ("<!-- c -->\n## A\n\n<!-- x -->\n-\n\n## Checklist\n\n- [ ] I did `X` (note)\n- [x] Two\n")
        p = ul.parse_template(t)
        self.assertEqual(p["sections"], ["A", "Checklist"])
        self.assertEqual(p["checkboxes"], ["i did x", "two"])

    def test_detect_signoff(self):
        self.assertTrue(ul.detect_signoff("All commits need a Signed-off-by line", [])["dco"])
        self.assertTrue(ul.detect_signoff("Sign the Contributor License Agreement", [])["cla"])
        self.assertTrue(ul.detect_signoff("", [".github/workflows/dco.yml"])["dco"])
        none = ul.detect_signoff("Be kind.", [".github/workflows/ci.yml"])
        self.assertFalse(none["dco"] or none["cla"])

    def test_signoff_policy_fetch_failure_does_not_overwrite_memory(self):
        self.mem["signoff"].update(dco=True, detected_from=["prior DCO evidence"])
        self.write_mem()

        def api_get(path):
            if path == "/repos/%s" % REPO:
                return {"default_branch": "main"}
            if path.endswith("/contents/.github") or path.endswith("/contents/.github/workflows"):
                return []
            if path.endswith("/contents"):
                return []
            raise AssertionError(path)

        def fetch_file(repo, path, ref):
            if path == ".github/PULL_REQUEST_TEMPLATE.md":
                return ("## Thinking Path\\n", "template-sha")
            if path == "CONTRIBUTING.md":
                raise ul.Refused("GET /repos/acme/widget/contents/CONTRIBUTING.md returned HTTP 503")
            raise AssertionError(path)

        with mock.patch.object(ul, "api_get", side_effect=api_get), mock.patch.object(ul, "fetch_file", side_effect=fetch_file):
            with self.assertRaisesRegex(ul.Refused, "memory unchanged"):
                ul.memory_init(REPO, self.mdir, refresh=True)
        self.assertTrue(ul.load_memory(REPO, self.mdir)["signoff"]["dco"])

    def test_signoff_policy_listing_failure_does_not_overwrite_memory(self):
        self.mem["signoff"].update(dco=True, detected_from=["prior DCO evidence"])
        self.write_mem()
        failed_paths = [
            "/repos/%s/contents/.github" % REPO,
            "/repos/%s/contents/.github/workflows" % REPO,
            "/repos/%s/contents" % REPO,
        ]
        for failed_path in failed_paths:
            with self.subTest(path=failed_path):
                def api_get(path):
                    if path == "/repos/%s" % REPO:
                        return {"default_branch": "main"}
                    if path == failed_path:
                        raise ul.Refused("GET %s returned HTTP 503" % path)
                    if path.endswith("/contents/.github") or path.endswith("/contents/.github/workflows"):
                        return []
                    if path.endswith("/contents"):
                        return []
                    raise AssertionError(path)

                def fetch_file(repo, path, ref):
                    if path == ".github/PULL_REQUEST_TEMPLATE.md":
                        return ("## Thinking Path\\n", "template-sha")
                    if path == "CONTRIBUTING.md":
                        return ("No signoff policy here.", "contributing-sha")
                    raise AssertionError(path)

                with mock.patch.object(ul, "api_get", side_effect=api_get), mock.patch.object(ul, "fetch_file", side_effect=fetch_file):
                    with self.assertRaisesRegex(ul.Refused, "memory unchanged"):
                        ul.memory_init(REPO, self.mdir, refresh=True)
                self.assertTrue(ul.load_memory(REPO, self.mdir)["signoff"]["dco"])

    def refresh_with_template(self, readable):
        """memory_init refresh with the stored template path unreadable (503) or absent (404)."""
        def api_get(path):
            if path == "/repos/%s" % REPO:
                return {"default_branch": "main"}
            if "/contents" in path:
                return []
            raise AssertionError(path)

        def fetch_file(repo, path, ref):
            if path == ".github/PULL_REQUEST_TEMPLATE.md":
                if readable == "directory":
                    raise ul.Refused("contents/%s on %s is not a file" % (path, repo))
                raise ul.Refused("GET /repos/acme/widget/contents/%s returned HTTP %s" % (path, "404" if readable else "503"))
            if path == ".github/pull_request_template.md":
                return ("## Only Section\n", "fallback-sha")
            if path == "CONTRIBUTING.md":
                raise ul.Refused("GET /repos/acme/widget/contents/CONTRIBUTING.md returned HTTP 404")
            raise ul.Refused("GET /repos/acme/widget/contents/%s returned HTTP 404" % path)

        with mock.patch.object(ul, "api_get", side_effect=api_get), mock.patch.object(ul, "fetch_file", side_effect=fetch_file):
            return ul.memory_init(REPO, self.mdir, refresh=True)

    def test_unreadable_primary_template_does_not_select_a_weaker_fallback(self):
        before = ul.load_memory(REPO, self.mdir)["template"]
        with self.assertRaisesRegex(ul.Refused, "memory unchanged"):
            self.refresh_with_template(readable=False)
        self.assertEqual(ul.load_memory(REPO, self.mdir)["template"], before)

    def test_primary_template_that_is_not_a_file_falls_back(self):
        self.refresh_with_template(readable="directory")
        self.assertEqual(ul.load_memory(REPO, self.mdir)["template"]["path"], ".github/pull_request_template.md")

    def test_verified_absent_primary_template_falls_back(self):
        self.refresh_with_template(readable=True)
        tpl = ul.load_memory(REPO, self.mdir)["template"]
        self.assertEqual((tpl["path"], tpl["sections"]), (".github/pull_request_template.md", ["Only Section"]))

    def ingest_fixture(self):
        d = os.path.join(self.tmp.name, "ing")
        os.makedirs(d)
        files = {
            "pr.json": {"user": {"login": "rick"}},
            "review_comments.json": [
                {"id": 1, "user": {"login": "greptile-apps[bot]"}, "path": "a.ts", "created_at": "2026-10-01T00:00:00Z",
                 "html_url": "https://github.com/acme/widget/pull/9#r1",
                 "body": '<a href="#"><img alt="P1" src="x/p1.svg"></a> **Missing guard** The loop never ends.'},
                {"id": 2, "user": {"login": "greptile-apps[bot]"}, "in_reply_to_id": 1, "body": "Acknowledged."},
                {"id": 3, "user": {"login": "maint"}, "author_association": "MEMBER", "path": "b.ts",
                 "body": "Please split this into two PRs, it is too large."},
                {"id": 4, "user": {"login": "rick"}, "body": "my own reply"},
                {"id": 5, "user": {"login": "dependabot[bot]"}, "body": "bump"},
            ],
            "issue_comments.json": [
                {"id": 10, "user": {"login": "greptile-apps[bot]"}, "body": "<!-- greptile_confidence_score:4 -->"},
                {"id": 11, "user": {"login": "maint"}, "author_association": "MEMBER",
                 "body": "Can you add a test for the retry?"},
            ],
            "reviews.json": [{"id": 20, "user": {"login": "maint"}, "author_association": "OWNER", "body": "Needs work",
                              "submitted_at": "2026-10-02T00:00:00Z"},
                             {"id": 21, "user": {"login": "maint"}, "author_association": "OWNER", "body": ""}],
        }
        for name, val in files.items():
            with open(os.path.join(d, name), "w") as fh:
                json.dump(val, fh)
        return d

    def test_ingest_records_maintainer_and_greptile_feedback_once(self):
        d = self.ingest_fixture()
        added, seen = ul.ingest_feedback(REPO, 9, self.mdir, "rick", d)
        self.assertEqual(added, 4)  # greptile P1, maintainer inline, maintainer issue comment, review body
        mem = ul.load_memory(REPO, self.mdir)
        by = {f["id"]: f for f in mem["feedback"]}
        g = by["greptile-9-1"]
        self.assertEqual(g["severity"], "P1")
        self.assertNotIn("<", g["excerpt"])
        self.assertEqual(g["paths"], ["a.ts"])
        self.assertTrue(by["maintainer-9-3"]["split_request"])
        self.assertEqual(by["maintainer-9-3"]["category"], "scope")
        self.assertNotIn("greptile-9-2", by)       # Greptile reply to our own reply
        self.assertNotIn("maintainer-9-4", by)     # our own comment
        self.assertNotIn("maintainer-9-5", by)     # other bots
        again, _ = ul.ingest_feedback(REPO, 9, self.mdir, "rick", d)
        self.assertEqual(again, 0)                  # idempotent: the watcher can re-run it

    def test_ingest_skips_someone_elses_pr_unless_asked(self):
        d = self.ingest_fixture()
        with open(os.path.join(d, "pr.json"), "w") as fh:
            json.dump({"user": {"login": "stranger"}}, fh)
        self.assertEqual(ul.ingest_feedback(REPO, 9, self.mdir, "rick", d), (0, 0))
        added, _ = ul.ingest_feedback(REPO, 9, self.mdir, "rick", d, any_author=True)
        self.assertGreater(added, 0)

    def test_unrelated_commenter_cannot_set_maintainer_policy(self):
        d = self.ingest_fixture()
        with open(os.path.join(d, "issue_comments.json"), "w") as fh:
            json.dump([{"id": 12, "user": {"login": "visitor"}, "author_association": "NONE",
                        "body": "Please split this into two PRs, it is too large."},
                       {"id": 13, "user": {"login": "nofield"},
                        "body": "Please split this into smaller PRs."}], fh)
        ul.ingest_feedback(REPO, 9, self.mdir, "rick", d)
        by = {f["id"]: f for f in ul.load_memory(REPO, self.mdir)["feedback"]}
        for fid in ("commenter-9-12", "commenter-9-13"):
            self.assertEqual(by[fid]["kind"], "commenter")
            self.assertFalse(by[fid]["split_request"])
        self.assertNotIn("maintainer-9-12", by)
        self.assertEqual(by["commenter-9-12"]["association"], "NONE")
        # the feedback is kept, but only the verified maintainer's request counts
        self.assertTrue(by["maintainer-9-3"]["split_request"])
        self.mem = ul.load_memory(REPO, self.mdir)
        self.mem["feedback"] = [f for f in self.mem["feedback"] if f["id"] != "maintainer-9-3"]
        self.write_mem()
        self.files += [{"filename": "server/src/x.ts", "status": "added", "additions": 60, "deletions": 0,
                        "changes": 60, "patch": PATCH_SRC}]
        self.assertClean(self.run_lint())

    def test_legacy_commenter_entry_marked_maintainer_does_not_reappear(self):
        d = self.ingest_fixture()
        with open(os.path.join(d, "issue_comments.json"), "w") as fh:
            json.dump([{"id": 12, "user": {"login": "visitor"}, "author_association": "NONE", "body": "Looks odd."}], fh)
        self.mem["feedback"].append({"id": "maintainer-9-12", "kind": "maintainer", "split_request": False,
                                     "status": "recorded", "category": "other", "paths": []})
        self.write_mem()
        ul.ingest_feedback(REPO, 9, self.mdir, "rick", d)
        ids = {f["id"] for f in ul.load_memory(REPO, self.mdir)["feedback"]}
        self.assertNotIn("commenter-9-12", ids)

    def test_split_request_ingested_from_a_maintainer_lowers_the_lint_budget(self):
        d = self.ingest_fixture()
        ul.ingest_feedback(REPO, 9, self.mdir, "rick", d)
        self.files += [{"filename": "server/src/x.ts", "status": "added", "additions": 60, "deletions": 0,
                        "changes": 60, "patch": PATCH_SRC}]
        self.assertRed(self.run_lint(), "budget")


class Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/contents/.github/PULL_REQUEST_TEMPLATE.md"):
            blob = self.server.blob
            body = json.dumps({"content": base64.b64encode(b"## A\n").decode(), "sha": blob}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ReceiptStub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.server.posted.append((self.path, json.loads(self.rfile.read(n).decode()), self.headers.get("Authorization")))
        body = b"{}"
        self.send_response(self.server.code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Receipts(Base):
    def setUp(self):
        super().setUp()
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), ReceiptStub)
        self.srv.posted, self.srv.code = [], 201
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self._env = {k: os.environ.get(k) for k in ("GH_TOKEN", "GH_API_URL")}
        os.environ["GH_TOKEN"] = "tok"
        os.environ["GH_API_URL"] = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_marker_cannot_be_closed_early_and_round_trips(self):
        desc = "clean --> text=0123456789abcdef -- author=a reviewer=b/c"
        body = ul.receipt_body("upstream-gate/lint", "success", SHA, desc)
        marker = body.splitlines()[0]
        self.assertEqual(marker.count("-->"), 1)              # only the real terminator
        self.assertEqual(marker.count("--"), marker.count("<!--") + marker.count("-->"))
        got = ul.receipts_from_comments([{"id": 1, "user": {"login": "Me"}, "created_at": "t", "body": body}], "me")
        self.assertEqual(got[0]["description"], desc)
        self.assertEqual(got[0]["context"], "upstream-gate/lint")

    def test_only_our_login_is_trusted_and_order_is_by_time(self):
        mk = lambda cid, who, at, state: {"id": cid, "user": {"login": who}, "created_at": at,
                                          "body": ul.receipt_body("c", state, SHA, "d")}
        rows = [mk(2, "me", "2026-10-07T02:00:00Z", "failure"), mk(1, "me", "2026-10-07T01:00:00Z", "success"),
                mk(3, "stranger", "2026-10-07T03:00:00Z", "success")]
        got = ul.receipts_from_comments(rows, "me")
        self.assertEqual([r["state"] for r in got], ["success", "failure"])

    def test_post_receipt_comments_on_the_mirror_pr(self):
        ul.post_receipt("Fork/widget", 52, SHA, "upstream-gate/lint", "success", "pass first-push")
        path, payload, auth = self.srv.posted[0]
        self.assertEqual(path, "/repos/Fork/widget/issues/52/comments")
        self.assertEqual(auth, "Bearer tok")
        got = ul.receipts_from_comments([{"id": 1, "user": {"login": "x"}, "body": payload["body"]}], "x")
        self.assertEqual((got[0]["context"], got[0]["state"], got[0]["head"]), ("upstream-gate/lint", "success", SHA))

    def test_post_receipt_refuses_bad_input_without_calling_github(self):
        for args in (("Fork/widget", 52, "abc", "c", "success", "d"), ("Fork/widget", 52, SHA, "c", "great", "d"),
                     ("Fork/widget", 0, SHA, "c", "success", "d")):
            with self.assertRaises(ul.Refused):
                ul.post_receipt(*args)
        self.assertEqual(self.srv.posted, [])

    def test_post_receipt_refuses_when_github_says_403(self):
        self.srv.code = 403
        with self.assertRaises(ul.Refused):
            ul.post_receipt("Fork/widget", 52, SHA, "c", "success", "d")

    def test_cli_receipt_post_exit_codes(self):
        env = dict(os.environ)
        base = [sys.executable, os.path.join(HERE, "upstream_lint.py"), "receipt-post", "--repo", "Fork/widget",
                "--pr", "52", "--head", SHA, "--context", "upstream-gate/lint", "--state", "success",
                "--description", "pass"]
        self.assertEqual(subprocess.run(base, env=env, capture_output=True).returncode, 0)
        self.srv.code = 403
        self.assertEqual(subprocess.run(base, env=env, capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run(base[:-4], env=env, capture_output=True).returncode, 4)


class Hook(Base):
    def setUp(self):
        super().setUp()
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        self.srv.blob = "abc123"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.repo_dir = os.path.join(self.tmp.name, "clone")
        os.makedirs(self.repo_dir)
        make_repo(self.repo_dir)
        git(self.repo_dir, "update-ref", "refs/remotes/up/main", "main")
        self.env = dict(os.environ, UPSTREAM_MEMORY_DIR=self.mdir, GH_TOKEN="dummy",
                        GH_API_URL="http://127.0.0.1:%d" % self.srv.server_address[1])
        self.hook = os.path.join(HERE, "upstream_prepush_hook.sh")
        self.head = git(self.repo_dir, "rev-parse", "HEAD").strip()

    def push(self, url, stdin=None, remote="up"):
        line = stdin if stdin is not None else "refs/heads/local %s refs/heads/fix/session-fingerprint-churn %s\n" % (self.head, "0" * 40)
        return subprocess.run(["bash", self.hook, remote, url], input=line, text=True, cwd=self.repo_dir,
                              env=self.env, capture_output=True)

    def write_text(self, title=None, body=None, branch="fix/session-fingerprint-churn"):
        d = os.path.join(self.repo_dir, ".git", "upstream-pr", branch)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "title.txt"), "w") as fh:
            fh.write(title or self.title)
        with open(os.path.join(d, "body.md"), "w") as fh:
            fh.write(body or self.body)

    def test_own_org_passes_through_without_any_text(self):
        for url in ("https://github.com/TogetherWeOwn/paperclip.git", "person-dd677eb8@example.invalid:togetherweown/x.git"):
            self.assertEqual(self.push(url).returncode, 0, url)

    def test_non_github_remote_is_blocked(self):
        self.assertEqual(self.push("https://gitlab.com/acme/widget.git").returncode, 1)

    def test_non_github_url_userinfo_is_not_echoed(self):
        url = "https://user:person-6325cb2e@example.invalid/acme/widget.git"
        r = self.push(url)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(url, r.stderr)
        self.assertNotIn("user:dummy-marker@", r.stderr)

    def test_url_used_as_the_remote_name_does_not_echo_userinfo(self):
        # `git push <url>` passes the URL as both hook arguments.
        for url in ("https://user:person-6325cb2e@example.invalid/acme/widget.git",
                    "https://user:person-09ae8172@example.invalid/acme/widget.git"):
            with self.subTest(url=url):
                self.write_text()
                r = self.push(url, remote=url)
                self.assertEqual(r.returncode, 1)
                self.assertNotIn("dummy-marker", r.stderr + r.stdout)

    def test_missing_pr_text_blocks(self):
        self.assertEqual(self.push("https://github.com/acme/widget.git").returncode, 1)

    def test_clean_push_passes(self):
        self.write_text()
        r = self.push("https://github.com/acme/widget.git")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_ssh_url_form_is_parsed(self):
        self.write_text()
        r = self.push("person-dd677eb8@example.invalid:acme/widget.git")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_failing_lint_blocks_the_push(self):
        self.write_text(title="not conventional")
        self.assertEqual(self.push("https://github.com/acme/widget.git").returncode, 1)

    def test_internal_branch_name_on_the_remote_blocks(self):
        self.write_text(branch="case_1cab5f7f-fix")
        line = "refs/heads/local %s refs/heads/case_1cab5f7f-fix %s\n" % (self.head, "0" * 40)
        self.assertEqual(self.push("https://github.com/acme/widget.git", line).returncode, 1)

    def test_unknown_memory_blocks(self):
        self.write_text()
        self.assertEqual(self.push("https://github.com/stranger/other.git").returncode, 1)

    def test_changed_upstream_template_blocks(self):
        self.write_text()
        self.srv.blob = "def456"
        self.assertEqual(self.push("https://github.com/acme/widget.git").returncode, 1)

    def test_branch_delete_and_tags_are_ignored(self):
        line = "(delete) %s refs/heads/old %s\nrefs/tags/v1 %s refs/tags/v1 %s\n" % ("0" * 40, self.head, self.head, "0" * 40)
        self.assertEqual(self.push("https://github.com/acme/widget.git", line).returncode, 0)

    def test_unresolvable_base_blocks(self):
        self.write_text()
        git(self.repo_dir, "update-ref", "-d", "refs/remotes/up/main")
        self.assertEqual(self.push("https://github.com/acme/widget.git").returncode, 1)

    def test_unresolved_base_does_not_fall_back_to_remote_tip(self):
        self.write_text()
        path = os.path.join(self.repo_dir, "server/src/__tests__/session.test.ts")
        with open(path, "a") as fh:
            fh.write("// another asserted test change\n")
        git(self.repo_dir, "commit", "-q", "-am", "test(scheduler): add follow-up assertion")
        head = git(self.repo_dir, "rev-parse", "HEAD").strip()
        git(self.repo_dir, "update-ref", "-d", "refs/remotes/up/main")
        line = "refs/heads/local %s refs/heads/fix/session-fingerprint-churn %s\n" % (head, self.head)
        r = self.push("https://github.com/acme/widget.git", line)
        self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
        self.assertIn("full PR base", r.stderr)

    def test_update_lints_the_full_pr_range_not_just_the_increment(self):
        # Split-push evasion: the leak rides in the first PR commit; the update
        # only adds an asserted test, so the increment passes every other gate.
        # The full range must fail on the leak, not missing test coverage.
        self.write_text()
        git(self.repo_dir, "reset", "--soft", "main")
        with open(os.path.join(self.repo_dir, "server/src/services/session.ts"), "a") as fh:
            fh.write("// see case_1cab5f7f\n")
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): keep task sessions across project row updates")
        first = git(self.repo_dir, "rev-parse", "HEAD").strip()
        with open(os.path.join(self.repo_dir, "server/src/__tests__/session.test.ts"), "a") as fh:
            fh.write("it('keeps the session', () => { expect(session()).toBe(1); });\n")
        git(self.repo_dir, "add", "-A")
        git(self.repo_dir, "commit", "-q", "-m", "fix(scheduler): follow up")
        second = git(self.repo_dir, "rev-parse", "HEAD").strip()
        line = "refs/heads/local %s refs/heads/fix/session-fingerprint-churn %s\n" % (second, first)
        self.assertEqual(self.push("https://github.com/acme/widget.git", line).returncode, 1)

    def test_install_hook_writes_a_wrapper_and_refuses_to_clobber(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"), "install-hook",
                              "--repo-dir", self.repo_dir], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        hook = os.path.join(self.repo_dir, ".git", "hooks", "pre-push")
        self.assertTrue(os.access(hook, os.X_OK))
        again = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"), "install-hook",
                                "--repo-dir", self.repo_dir], capture_output=True, text=True)
        self.assertEqual(again.returncode, 0)  # our own wrapper may be refreshed
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\necho mine\n")
        clobber = subprocess.run([sys.executable, os.path.join(HERE, "upstream_lint.py"), "install-hook",
                                  "--repo-dir", self.repo_dir], capture_output=True, text=True)
        self.assertEqual(clobber.returncode, 2)

    def test_install_hook_quotes_helper_path(self):
        sentinel = os.path.join(self.tmp.name, "shell-injection-ran")
        helper_dir = os.path.join(self.tmp.name, "tool dir; touch %s; #" % sentinel)
        os.makedirs(helper_dir)
        for name in ("upstream_lint.py", "upstream_prepush_hook.sh"):
            with open(os.path.join(HERE, name), "rb") as source:
                with open(os.path.join(helper_dir, name), "wb") as target:
                    target.write(source.read())
        os.chmod(os.path.join(helper_dir, "upstream_prepush_hook.sh"), 0o755)
        linter = os.path.join(helper_dir, "upstream_lint.py")
        out = subprocess.run([sys.executable, linter, "install-hook", "--repo-dir", self.repo_dir],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        hook = os.path.join(self.repo_dir, ".git", "hooks", "pre-push")
        run = subprocess.run(["bash", hook, "origin", "https://github.com/TogetherWeOwn/paperclip.git"],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertFalse(os.path.exists(sentinel))


if __name__ == "__main__":
    unittest.main(verbosity=1)
