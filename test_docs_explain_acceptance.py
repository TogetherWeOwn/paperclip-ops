#!/usr/bin/env python3
"""Acceptance contract: opt-in ``--explain`` for the docs-drift gate.

Builder contract under test (feature ``docs-explain``):

* ``scripts/quality-gates/docs_drift.py`` gains an opt-in ``--explain``
  flag. Without it, stdout stays exactly the current ``docs-drift.v1``
  object: same keys, same values, same exit codes (0 clean /
  ``not_configured``, 1 ``finding``, 2 ``unmeasurable``), no new keys.
* With ``--explain``, a normal report (clean, finding or
  ``not_configured``) gains exactly one extra top-level key,
  ``explanations``. Every other key and value, and the exit code, is
  identical to the default invocation on the same base/head. This
  includes the immutable ``base_sha`` / ``head_sha`` /
  ``merge_base_sha`` / ``policy_sha``, ``changed_paths``,
  ``checked_rules``, ``findings`` and ``status``.
* ``explanations`` holds one record per triggered (checked) rule, in
  base-policy rule order -- the same order ``findings`` already uses.
  Each record has exactly these keys:

  - ``rule``: the existing stable rule ID (same spelling as
    ``findings[].rule``);
  - ``matched_source_paths``: sorted, duplicate-free repository paths
    that changed in the merge-base..head diff and match the rule's
    source patterns (deleted and renamed-away source paths included,
    exactly as the verdict sees them);
  - ``qualifying_doc_paths``: sorted, duplicate-free repository paths
    that qualify for *this* rule under the existing predicate -- added
    or content-modified regular files matching the rule's doc patterns
    and not its source patterns;
  - ``status``: ``"satisfied"`` iff ``qualifying_doc_paths`` is
    non-empty, else ``"missing"``.

  ``len(explanations) == checked_rules``. The ``missing`` records
  correspond one-for-one to ``findings`` (same rule, and
  ``findings[].paths == matched_source_paths``); a ``satisfied`` rule
  never has a finding. Explanations must come from the same evaluated
  path sets and predicate as the verdict, not a second evaluator: the
  cross-checks above are the observable form of that requirement.
* Zero triggered rules yields ``explanations: []`` (never a coverage
  claim); a missing base policy yields ``not_configured`` with
  ``explanations: []``. An unmeasurable refusal (bad or missing SHA,
  malformed base policy, oversized diff, Git failure) keeps exit 2 and
  invents no satisfied evidence: ``explanations`` is either absent or
  an empty list, with no other shared value changed.
* Existing qualification exclusions are preserved in explanations:
  a deleted doc, a chmod-only doc change, a symlink and a submodule
  (gitlink) can never appear in ``qualifying_doc_paths``. Renames use
  the existing no-rename-detection, NUL-safe diff: a doc renamed into
  the mapping qualifies under its new path; a doc renamed out does
  not; unusual paths (spaces, newlines, unicode, option-like) round
  trip exactly through JSON.
* Policy still comes from the supplied base commit, never the head or
  the merge base; validation semantics and all limits (including the
  100000-path diff cap) are unchanged. ``.docs-drift.json`` is not
  edited by this feature. The check stays read-only and idempotent:
  repeated explain runs produce byte-identical stdout and leave the
  repository (HEAD, index and worktree) unchanged. Stdout is one
  parseable JSON object carrying only paths, rule IDs and the existing
  commit identities -- never file bodies or subprocess output.
  Explanations prove accompaniment only, never semantic accuracy or
  generated-file freshness.
* Existing Python entry points stay callable as today
  (``check(repo, base, head)`` and ``main(argv)``). ``--help`` shows a
  concise example invocation using ``--explain`` together with
  ``--base`` and ``--head``, and ``docs/docs-drift.md`` documents the
  ``explanations`` contract above in the same change.
* The Builder leaves this suite wired into an existing test
  invocation so the orphan-suite ratchet holds; which invocation is
  the Builder's choice. No docs-drift reusable-workflow change, no
  network, no installation and no automatic doc edits are in scope.

Fixtures are disposable synthetic Git repositories only. No network,
no credentials, no production or host state.

IMMUTABLE: Builder starts from the pushed test branch/commit and never
edits this file. Corrections belong to the Test Author.

Run: python3 -B -m unittest -v test_docs_drift test_docs_explain_acceptance
Offline, stdlib only.
"""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts/quality-gates/docs_drift.py"
DOCS_DOC = ROOT / "docs/docs-drift.md"
SPEC = importlib.util.spec_from_file_location("docs_drift", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

DEFAULT_KEYS = {
    "schema", "base_sha", "head_sha", "merge_base_sha", "policy_sha",
    "changed_paths", "checked_rules", "findings", "status",
}
ERROR_KEYS = {"schema", "status", "code", "retryable", "next_action"}
RECORD_KEYS = {"rule", "matched_source_paths", "qualifying_doc_paths", "status"}
COMMIT_MESSAGE = "test: fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>"


class DocsExplainFixture:
    """Disposable Git repository with a one-rule base policy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docs-explain-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.policy = {"version": 1, "rules": [
            {"id": "api", "sources": ["src/*"], "docs": ["docs/*"]}]}
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.put("src/api.py", "before\n")
        self.put("docs/api.md", "before\n")
        self.put("outside.md", "outside\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, env=self.env, text=True).strip()

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", COMMIT_MESSAGE)
        return self.git("rev-parse", "HEAD")

    def commit_staged(self):
        self.git("commit", "-q", "-m", COMMIT_MESSAGE)
        return self.git("rev-parse", "HEAD")

    def rebase_with_rules(self, rules, files=None):
        """Commit a new base carrying ``rules`` (plus optional files)."""
        self.put(".docs-drift.json", json.dumps({"version": 1, "rules": rules}))
        for name, content in (files or {}).items():
            self.put(name, content)
        self.base = self.commit()
        return self.base

    def change_source(self, content="after\n"):
        self.put("src/api.py", content)

    def change_doc(self, content="after\n"):
        self.put("docs/api.md", content)

    # -- CLI plumbing ---------------------------------------------------

    def run_cli(self, *, base=None, head=None, explain=False, argv=None):
        if argv is None:
            argv = ["python3", str(SCRIPT), "--repo", str(self.root),
                    "--base=" + (base or self.base),
                    "--head=" + (head or self.base)]
            if explain:
                argv.append("--explain")
        return subprocess.run(argv, capture_output=True, text=True,
                              env=self.env, timeout=15)

    def parse(self, result, label):
        self.assertTrue(
            result.stdout.strip(),
            f"{label} run must emit one JSON object on stdout; "
            f"exit={result.returncode} stderr={result.stderr[:400]!r}")
        self.assertEqual(
            result.stderr, "",
            f"{label} run must not write to stderr: {result.stderr[:400]!r}")
        try:
            report = json.loads(result.stdout)
        except ValueError:
            self.fail(f"{label} stdout is not one parseable JSON object: "
                      f"{result.stdout[:400]!r}")
        self.assertIsInstance(report, dict, f"{label} report must be an object")
        return report

    def snapshot(self):
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=self.root,
            env=self.env, text=True)
        return self.git("rev-parse", "HEAD"), status

    # -- shared pair invariants -----------------------------------------

    def assert_pair(self, expected, *, base=None, head=None):
        """Default vs explain on identical inputs; returns explain report.

        Pins: identical exit codes, identical non-explanation values,
        exact key sets, exact SHAs, explanation/finding correspondence,
        and a repository left byte-for-byte untouched.
        """
        base = base or self.base
        head = head or self.base
        before = self.snapshot()
        default_result = self.run_cli(base=base, head=head)
        explain_result = self.run_cli(base=base, head=head, explain=True)
        self.assertEqual(
            self.snapshot(), before,
            "docs-drift runs (default and explain) must not change the repo")
        self.assertEqual(
            default_result.returncode, expected,
            "default: " + default_result.stdout + default_result.stderr)
        self.assertEqual(
            explain_result.returncode, expected,
            "explain exit must match default: "
            + explain_result.stdout + explain_result.stderr)
        default = self.parse(default_result, "default")
        explain = self.parse(explain_result, "explain")
        self.assertEqual(default["schema"], "docs-drift.v1")
        self.assertEqual(explain["schema"], "docs-drift.v1")

        if default["status"] == "unmeasurable":
            self.assertEqual(set(default), ERROR_KEYS,
                             "default unmeasurable object must be unchanged")
            self.assertLessEqual(set(explain), ERROR_KEYS | {"explanations"})
            for key in set(default) & set(explain):
                self.assertEqual(explain[key], default[key],
                                 f"explain changed unmeasurable field {key!r}")
            self.assertEqual(
                explain.get("explanations", []), [],
                "a refusal must never invent satisfied evidence")
            self.assertNotIn("fatal:", explain_result.stdout.lower(),
                             "refusals must not dump subprocess output")
            self.assertNotIn("traceback", explain_result.stdout.lower())
            return explain

        self.assertEqual(set(default), DEFAULT_KEYS,
                         "default docs-drift.v1 object must be unchanged: "
                         f"got keys {sorted(default)}")
        self.assertNotIn("explanations", default,
                         "explanations are opt-in; default output is frozen")
        self.assertEqual(set(explain), DEFAULT_KEYS | {"explanations"},
                         "explain adds exactly one key, 'explanations'")
        stripped = {k: v for k, v in explain.items() if k != "explanations"}
        self.assertEqual(stripped, default,
                         "explain must not change any pre-existing value")
        self.assertEqual(explain["base_sha"], base)
        self.assertEqual(explain["head_sha"], head)
        self.assertEqual(explain["policy_sha"], base)
        self.assertEqual(explain["merge_base_sha"],
                         self.git("merge-base", base, head))
        self.assert_explanation_invariants(explain)
        return explain

    def assert_explanation_invariants(self, report):
        explanations = report["explanations"]
        self.assertIsInstance(explanations, list)
        self.assertEqual(len(explanations), report["checked_rules"],
                         "one explanation record per triggered rule")
        seen_rules = []
        findings_by_rule = {}
        for finding in report["findings"]:
            self.assertNotIn(finding["rule"], findings_by_rule,
                             "at most one finding per rule")
            findings_by_rule[finding["rule"]] = finding
        for record in explanations:
            self.assertEqual(set(record), RECORD_KEYS,
                             f"explanation record keys must be exactly "
                             f"{sorted(RECORD_KEYS)}, got {sorted(record)}")
            self.assertIsInstance(record["rule"], str)
            self.assertNotIn(record["rule"], seen_rules,
                             "one explanation record per rule")
            seen_rules.append(record["rule"])
            for field in ("matched_source_paths", "qualifying_doc_paths"):
                paths = record[field]
                self.assertIsInstance(paths, list, field)
                self.assertTrue(all(isinstance(p, str) for p in paths), field)
                self.assertEqual(paths, sorted(paths),
                                 f"{field} must be sorted: {paths!r}")
                self.assertEqual(len(paths), len(set(paths)),
                                 f"{field} must be duplicate-free: {paths!r}")
            self.assertTrue(record["matched_source_paths"],
                            "a triggered rule always matched a source path")
            self.assertIn(record["status"], ("satisfied", "missing"))
            self.assertEqual(
                record["status"] == "satisfied",
                bool(record["qualifying_doc_paths"]),
                "status is satisfied iff a qualifying doc changed")
            finding = findings_by_rule.get(record["rule"])
            if record["status"] == "missing":
                self.assertIsNotNone(
                    finding, f"missing rule {record['rule']!r} needs a finding")
                self.assertEqual(finding["code"], "docs_required")
                self.assertEqual(
                    finding["paths"], record["matched_source_paths"],
                    "finding paths and explained sources must be the same "
                    "evaluated set")
            else:
                self.assertIsNone(
                    finding,
                    f"satisfied rule {record['rule']!r} must have no finding")


class DefaultControlsTest(DocsExplainFixture, unittest.TestCase):
    """Current behavior that must already pass before the feature lands."""

    def run_default(self, expected, *, base=None, head=None):
        result = self.run_cli(base=base, head=head)
        self.assertEqual(result.returncode, expected,
                         result.stdout + result.stderr)
        return self.parse(result, "default")

    def test_default_satisfied_is_clean_without_explanations(self):
        self.change_source()
        self.change_doc()
        report = self.run_default(0, head=self.commit())
        self.assertEqual(set(report), DEFAULT_KEYS)
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["checked_rules"], 1)
        self.assertEqual(report["findings"], [])

    def test_default_missing_docs_is_finding_without_explanations(self):
        self.change_source()
        report = self.run_default(1, head=self.commit())
        self.assertEqual(set(report), DEFAULT_KEYS)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["findings"][0]["rule"], "api")
        self.assertEqual(report["findings"][0]["paths"], ["src/api.py"])

    def test_default_unrelated_change_checks_no_rules(self):
        self.put("unrelated.txt", "change\n")
        report = self.run_default(0, head=self.commit())
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["checked_rules"], 0)

    def test_default_not_configured_when_base_has_no_policy(self):
        self.git("rm", "-q", ".docs-drift.json")
        unconfigured = self.commit()
        self.change_source()
        report = self.run_default(0, base=unconfigured, head=self.commit())
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual(report["policy_sha"], unconfigured)

    def test_default_invalid_sha_is_unmeasurable(self):
        report = self.run_default(2, head="--help")
        self.assertEqual(set(report), ERROR_KEYS)
        self.assertEqual(report["status"], "unmeasurable")
        self.assertEqual(report["code"], "invalid_sha")


class ExplainSatisfiedMissingTest(DocsExplainFixture, unittest.TestCase):
    """Core satisfied / missing records and report-shape guarantees."""

    def test_satisfied_rule_names_the_qualifying_doc(self):
        self.change_source()
        self.change_doc()
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["explanations"], [{
            "rule": "api",
            "matched_source_paths": ["src/api.py"],
            "qualifying_doc_paths": ["docs/api.md"],
            "status": "satisfied",
        }])

    def test_missing_rule_has_empty_qualifying_docs(self):
        self.change_source()
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["explanations"], [{
            "rule": "api",
            "matched_source_paths": ["src/api.py"],
            "qualifying_doc_paths": [],
            "status": "missing",
        }])

    def test_added_doc_satisfies_and_is_named(self):
        self.change_source()
        self.put("docs/new.md", "new docs\n")
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["explanations"][0]["qualifying_doc_paths"],
                         ["docs/new.md"])

    def test_multiple_qualifying_docs_are_sorted_and_complete(self):
        self.change_source()
        self.put("docs/z-last.md", "new\n")
        self.change_doc()
        self.put("docs/a-first.md", "new\n")
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(
            report["explanations"][0]["qualifying_doc_paths"],
            ["docs/a-first.md", "docs/api.md", "docs/z-last.md"])

    def test_multiple_matched_sources_are_sorted(self):
        self.put("src/zeta.py", "new\n")
        self.change_source()
        self.put("src/alpha.py", "new\n")
        self.change_doc()
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(
            report["explanations"][0]["matched_source_paths"],
            ["src/alpha.py", "src/api.py", "src/zeta.py"])

    def test_no_triggered_rule_has_empty_explanations(self):
        self.put("unrelated.txt", "change\n")
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["checked_rules"], 0)
        self.assertEqual(report["explanations"], [])

    def test_empty_diff_has_empty_explanations(self):
        report = self.assert_pair(0, head=self.base)
        self.assertEqual(report["changed_paths"], 0)
        self.assertEqual(report["explanations"], [])

    def test_new_policy_at_head_only_explains_as_not_configured(self):
        self.git("rm", "-q", ".docs-drift.json")
        unconfigured = self.commit()
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.change_source()
        report = self.assert_pair(0, base=unconfigured, head=self.commit())
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual(report["checked_rules"], 0)
        self.assertEqual(report["explanations"], [])

    def test_explain_is_deterministic_across_repeated_runs(self):
        self.change_source()
        self.change_doc()
        head = self.commit()
        first = self.run_cli(head=head, explain=True)
        second = self.run_cli(head=head, explain=True)
        self.parse(first, "explain")
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(first.stdout, second.stdout,
                         "explain output must be byte-identical across runs")

    def test_explain_stdout_is_one_json_object_without_file_bodies(self):
        source_sentinel = "SOURCE-BODY-SENTINEL-7c1d"
        doc_sentinel = "DOC-BODY-SENTINEL-9e2f"
        self.change_source(source_sentinel + "\n")
        self.change_doc(doc_sentinel + "\n")
        result = self.run_cli(head=self.commit(), explain=True)
        report = self.parse(result, "explain")
        self.assertEqual(report["status"], "clean")
        self.assertNotIn(source_sentinel, result.stdout,
                         "explain output must carry paths, never file bodies")
        self.assertNotIn(doc_sentinel, result.stdout)

    def test_explain_flag_may_precede_other_arguments(self):
        self.change_source()
        head = self.commit()
        argv = ["python3", str(SCRIPT), "--explain",
                "--head=" + head, "--repo", str(self.root),
                "--base=" + self.base]
        result = self.run_cli(argv=argv)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = self.parse(result, "explain")
        self.assertEqual(report["explanations"][0]["status"], "missing")


class ExplainRuleSeparationTest(DocsExplainFixture, unittest.TestCase):
    """Each record judges only its own rule's sources and docs."""

    def test_overlapping_rules_keep_separate_qualifying_docs(self):
        self.rebase_with_rules([
            {"id": "zeta", "sources": ["src/api.py"], "docs": ["manual/*"]},
            {"id": "api", "sources": ["src/*"], "docs": ["docs/*"]},
        ], files={"manual/guide.md": "before\n"})
        self.change_source()
        self.change_doc()
        report = self.assert_pair(1, head=self.commit())
        # Policy order, not alphabetical: the verdict's own order.
        self.assertEqual(report["explanations"], [
            {"rule": "zeta", "matched_source_paths": ["src/api.py"],
             "qualifying_doc_paths": [], "status": "missing"},
            {"rule": "api", "matched_source_paths": ["src/api.py"],
             "qualifying_doc_paths": ["docs/api.md"], "status": "satisfied"},
        ])

    def test_one_doc_can_satisfy_two_rules_when_it_matches_both(self):
        self.rebase_with_rules([
            {"id": "api", "sources": ["src/*"], "docs": ["docs/*"]},
            {"id": "shared", "sources": ["src/api.py"],
             "docs": ["docs/api.md"]},
        ])
        self.change_source()
        self.change_doc()
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(
            [(r["rule"], r["qualifying_doc_paths"], r["status"])
             for r in report["explanations"]],
            [("api", ["docs/api.md"], "satisfied"),
             ("shared", ["docs/api.md"], "satisfied")])

    def test_untriggered_rule_has_no_explanation_record(self):
        self.rebase_with_rules([
            {"id": "api", "sources": ["src/*"], "docs": ["docs/*"]},
            {"id": "other", "sources": ["lib/*"], "docs": ["manual/*"]},
        ], files={"lib/other.py": "before\n", "manual/guide.md": "before\n"})
        self.change_source()
        self.change_doc()
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["checked_rules"], 1)
        self.assertEqual([r["rule"] for r in report["explanations"]], ["api"],
                         "records exist only for triggered rules")

    def test_doc_only_change_triggers_no_rule(self):
        self.change_doc()
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["checked_rules"], 0)
        self.assertEqual(report["explanations"], [],
                         "a lone doc change is not a satisfied rule")

    def test_source_matching_its_own_docs_pattern_cannot_satisfy_itself(self):
        self.rebase_with_rules([
            {"id": "api", "sources": ["src/*"], "docs": ["src/*"]}])
        self.change_source()
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["explanations"], [{
            "rule": "api", "matched_source_paths": ["src/api.py"],
            "qualifying_doc_paths": [], "status": "missing"}])

    def test_doc_for_one_rule_can_be_a_triggering_source_for_another(self):
        self.rebase_with_rules([
            {"id": "api", "sources": ["src/*"], "docs": ["docs/*"]},
            {"id": "docsrc", "sources": ["docs/*"], "docs": ["manual/*"]},
        ], files={"manual/guide.md": "before\n"})
        self.change_source()
        self.change_doc()
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["explanations"], [
            {"rule": "api", "matched_source_paths": ["src/api.py"],
             "qualifying_doc_paths": ["docs/api.md"], "status": "satisfied"},
            {"rule": "docsrc", "matched_source_paths": ["docs/api.md"],
             "qualifying_doc_paths": [], "status": "missing"},
        ])


class ExplainChangeKindsTest(DocsExplainFixture, unittest.TestCase):
    """Deletions, renames and unusual paths under the existing diff logic."""

    def test_chmod_only_source_change_still_triggers_rule(self):
        os.chmod(self.root / "src/api.py", 0o755)
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["explanations"][0]["matched_source_paths"],
                         ["src/api.py"])
        self.assertEqual(report["explanations"][0]["status"], "missing")

    def test_deleted_source_is_matched_and_can_be_satisfied(self):
        (self.root / "src/api.py").unlink()
        self.change_doc()
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["explanations"], [{
            "rule": "api", "matched_source_paths": ["src/api.py"],
            "qualifying_doc_paths": ["docs/api.md"], "status": "satisfied"}])

    def test_deleted_source_without_doc_is_missing(self):
        (self.root / "src/api.py").unlink()
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["explanations"][0]["matched_source_paths"],
                         ["src/api.py"])
        self.assertEqual(report["explanations"][0]["status"], "missing")

    def test_renamed_source_keeps_old_and_new_paths(self):
        self.git("mv", "src/api.py", "src/renamed.py")
        self.change_doc()
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["explanations"][0]["matched_source_paths"],
                         ["src/api.py", "src/renamed.py"])
        self.assertEqual(report["explanations"][0]["status"], "satisfied")

    def test_source_renamed_out_of_mapping_still_matches_old_path(self):
        self.git("mv", "src/api.py", "outside.py")
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["explanations"][0]["matched_source_paths"],
                         ["src/api.py"])
        self.assertEqual(report["explanations"][0]["status"], "missing")

    def test_doc_renamed_into_mapping_qualifies_under_new_path(self):
        self.change_source()
        self.git("mv", "outside.md", "docs/moved.md")
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["explanations"][0]["qualifying_doc_paths"],
                         ["docs/moved.md"])

    def test_doc_renamed_out_of_mapping_does_not_qualify(self):
        self.change_source()
        self.git("mv", "docs/api.md", "moved-out.md")
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["explanations"][0]["qualifying_doc_paths"], [])
        self.assertEqual(report["explanations"][0]["status"], "missing")

    def test_unusual_paths_round_trip_exactly(self):
        paths = ["src/a b.py", "src/a\nb.py", "src/☃.py", "src/--flag.py"]
        for name in paths:
            self.put(name, "new\n")
        self.put("docs/a\nb.md", "new docs\n")
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["explanations"][0]["matched_source_paths"],
                         sorted(paths))
        self.assertEqual(report["explanations"][0]["qualifying_doc_paths"],
                         ["docs/a\nb.md"])


class ExplainNonQualifyingDocsTest(DocsExplainFixture, unittest.TestCase):
    """Change kinds that are never documentation evidence."""

    def assert_missing_with_no_qualifying(self, head):
        report = self.assert_pair(1, head=head)
        self.assertEqual(len(report["explanations"]), 1)
        self.assertEqual(report["explanations"][0]["qualifying_doc_paths"], [])
        self.assertEqual(report["explanations"][0]["status"], "missing")

    def test_deleted_doc_cannot_satisfy(self):
        self.change_source()
        (self.root / "docs/api.md").unlink()
        self.assert_missing_with_no_qualifying(self.commit())

    def test_chmod_only_doc_cannot_satisfy(self):
        self.change_source()
        os.chmod(self.root / "docs/api.md", 0o755)
        self.assert_missing_with_no_qualifying(self.commit())

    def test_symlink_doc_cannot_satisfy(self):
        self.change_source()
        os.symlink("api.md", self.root / "docs/link.md")
        self.assert_missing_with_no_qualifying(self.commit())

    def test_submodule_gitlink_doc_cannot_satisfy(self):
        self.change_source()
        self.git("add", "src/api.py")
        self.git("update-index", "--add", "--cacheinfo",
                 f"160000,{self.base},docs/sub")
        self.assert_missing_with_no_qualifying(self.commit_staged())

    def test_executable_doc_with_content_change_still_qualifies(self):
        self.change_source()
        self.change_doc()
        os.chmod(self.root / "docs/api.md", 0o755)
        report = self.assert_pair(0, head=self.commit())
        self.assertEqual(report["explanations"][0]["qualifying_doc_paths"],
                         ["docs/api.md"])
        self.assertEqual(report["explanations"][0]["status"], "satisfied")


class ExplainPolicyAndRefusalTest(DocsExplainFixture, unittest.TestCase):
    """Base-owned policy and refusals that invent no evidence."""

    def test_head_policy_deletion_cannot_change_the_explanation(self):
        self.change_source()
        (self.root / ".docs-drift.json").unlink()
        report = self.assert_pair(1, head=self.commit())
        self.assertEqual(report["policy_sha"], self.base)
        self.assertEqual(report["explanations"][0]["status"], "missing")

    def test_supplied_base_policy_not_merge_base_policy_governs(self):
        self.change_source()
        self.change_doc()
        head = self.commit()
        self.git("checkout", "-q", "--detach", self.base)
        target_policy = {"version": 1, "rules": [
            {"id": "api", "sources": ["src/*"], "docs": ["manual/*"]}]}
        self.put(".docs-drift.json", json.dumps(target_policy))
        target = self.commit()
        report = self.assert_pair(1, base=target, head=head)
        self.assertEqual(report["merge_base_sha"], self.base)
        self.assertEqual(report["findings"][0]["docs"], ["manual/*"])
        self.assertEqual(report["explanations"], [{
            "rule": "api", "matched_source_paths": ["src/api.py"],
            "qualifying_doc_paths": [], "status": "missing"}])

    def test_malformed_base_policy_is_unmeasurable_in_explain_mode(self):
        self.put(".docs-drift.json", json.dumps({"version": 1, "rules": []}))
        bad_base = self.commit()
        report = self.assert_pair(2, base=bad_base, head=self.commit())
        self.assertEqual(report["code"], "invalid_policy")

    def test_invalid_and_missing_shas_invent_no_satisfied_evidence(self):
        for head, code in [("--help", "invalid_sha"),
                           ("0" * 40, "git_failed")]:
            with self.subTest(head=head):
                report = self.assert_pair(2, head=head)
                self.assertEqual(report["code"], code)
                self.assertEqual(report.get("explanations", []), [])

    def test_unknown_argument_remains_an_argparse_error_with_explain(self):
        argv = ["python3", str(SCRIPT), "--repo", str(self.root),
                "--base=" + self.base, "--head=" + self.base,
                "--explain", "--bogus-flag"]
        result = self.run_cli(argv=argv)
        self.assertEqual(result.returncode, 2,
                         "argument syntax errors keep argparse exit 2")
        self.assertEqual(result.stdout, "",
                         "argument syntax errors emit no JSON evidence")
        self.assertIn("usage", result.stderr.lower())


class ExplainLimitsAndCompatTest(DocsExplainFixture, unittest.TestCase):
    """Diff cap and pre-existing Python entry points survive explain."""

    def synthetic_diff_main(self, count):
        # Mock only the large Git diff output, as the existing suite does;
        # argument parsing, policy, main and JSON stay real.
        raw = b"".join(f"M\0unmapped/file-{i}\0".encode() for i in range(count))
        real_run = subprocess.run
        diff_calls = []

        def run(args, *positional, **kwargs):
            if (args[0] == "git" and "diff" in args
                    and "--name-status" in args and "-z" in args):
                diff_calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout=raw,
                                                   stderr=b"")
            return real_run(args, *positional, **kwargs)

        output = io.StringIO()
        argv = ["--repo", str(self.root), "--base", self.base,
                "--head", self.base, "--explain"]
        with patch.object(GATE.subprocess, "run", side_effect=run):
            with redirect_stdout(output):
                try:
                    code = GATE.main(argv)
                except SystemExit as exc:
                    self.fail("main() must accept --explain and return an "
                              f"exit code, not raise SystemExit({exc.code})")
        self.assertEqual(
            len(diff_calls), 1,
            "explain must reuse the verdict's single diff evaluation, "
            f"not evaluate the diff again ({len(diff_calls)} diff calls)")
        return code, json.loads(output.getvalue())

    def test_diff_above_path_cap_still_refuses_in_explain_mode(self):
        code, report = self.synthetic_diff_main(100001)
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "unmeasurable")
        self.assertEqual(report["code"], "invalid_diff")
        self.assertEqual(report.get("explanations", []), [])

    def test_diff_at_exact_path_cap_explains_as_clean(self):
        code, report = self.synthetic_diff_main(100000)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["changed_paths"], 100000)
        self.assertEqual(report["checked_rules"], 0)
        self.assertEqual(report["explanations"], [])

    def test_existing_python_entrypoints_remain_callable(self):
        self.change_source()
        head = self.commit()
        report = GATE.check(self.root, self.base, head)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["findings"][0]["rule"], "api")
        output = io.StringIO()
        with redirect_stdout(output):
            code = GATE.main(["--repo", str(self.root),
                              "--base", self.base, "--head", head])
        self.assertEqual(code, 1)
        cli_report = json.loads(output.getvalue())
        self.assertEqual(set(cli_report), DEFAULT_KEYS,
                         "main() without --explain keeps the frozen object")

    def test_checker_source_has_no_network_path(self):
        source = SCRIPT.read_text()
        for token in ("urllib", "http.client", "socket", "requests",
                      "urlopen", "curl", "wget"):
            with self.subTest(token=token):
                self.assertNotIn(token, source,
                                 "the checker must stay offline and read-only")


class HelpAndDocsContractTest(unittest.TestCase):
    """The flag is discoverable and the canonical doc carries the contract."""

    def test_help_documents_explain_with_a_runnable_example(self):
        result = subprocess.run(["python3", str(SCRIPT), "--help"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = result.stdout
        self.assertIn("--explain", help_text,
                      "--help must list the --explain flag")
        self.assertIn("example", help_text.lower(),
                      "--help must carry a concise example")
        # Argparse may wrap the example across lines; judge the flattened
        # text after the word "example", not physical line breaks.
        flattened = " ".join(
            help_text.lower().split("example", 1)[1].split())
        for flag in ("--explain", "--base", "--head"):
            self.assertIn(
                flag, flattened,
                f"--help example must show {flag}; got:\n{help_text[:800]}")

    def test_canonical_docs_document_the_explain_contract(self):
        doc = DOCS_DOC.read_text()
        for token in ("--explain", "explanations", "matched_source_paths",
                      "qualifying_doc_paths", "satisfied", "missing",
                      "not_configured", "docs-drift.v1"):
            with self.subTest(token=token):
                self.assertIn(token, doc,
                              "docs/docs-drift.md must document the explain "
                              f"contract ({token!r} missing)")
        self.assertIn("accompaniment", doc.lower(),
                      "docs must keep the accompaniment-only limit explicit")


if __name__ == "__main__":
    unittest.main()
