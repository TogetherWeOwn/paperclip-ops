#!/usr/bin/env python3
"""Immutable, tests-only acceptance for ambiguous docs-policy refusal.

Run: python3 -B -m unittest -v test_docs_drift test_docs_policy_ambiguity_acceptance

The paired implementation must not edit this file. Expectations come from the
CLI/policy contract, not checker helpers. All policies and histories are synthetic;
the real repository policy is never read or changed. No packages or network needed.

Breaks caught: first/last-value-wins, shallow/lexical-only duplicate detection,
head-policy authority, leaked parser errors, swallowed recursion errors, relaxed
schema/size limits, path splitting, Git environment injection and false clean.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
CHECKER = ROOT / "scripts/quality-gates/docs_drift.py"
PRIVATE = "SYNTHETIC_PRIVATE_POLICY_73921"
RULE = '{"id":"api","sources":["src/*"],"docs":["docs/*"]}'
POLICY = '{"version":1,"rules":[' + RULE + ']}'
REPAIR = "Use the documented version-1 policy schema; fix the base policy in a reviewed PR."

# These are deliberate lossy-parser mutants, not substitute checkers. The CLI,
# Git reads, schema validation and reporting remain real. Override the JSON
# pairs hook only, to reproduce the two forbidden ambiguity resolutions.
LOSSY_PARSER = r'''
import json, runpy, sys
mode, script, *args = sys.argv[1:]
original_loads = json.loads

def pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if mode == "last" or key not in result:
            result[key] = value
    return result

def lossy_loads(*args, **kwargs):
    kwargs["object_pairs_hook"] = pairs_hook
    return original_loads(*args, **kwargs)

json.loads = lossy_loads
sys.argv = [script, *args]
runpy.run_path(script, run_name="__main__")
'''


def duplicate_policy(field, first, second, *, escaped=False, reverse_keys=False):
    """Emit literal JSON pairs without collapsing the duplicate in test setup."""
    key = json.dumps(field)
    other_key = ('"\\u%04x%s"' % (ord(field[0]), field[1:])) if escaped else key
    if reverse_keys:
        key, other_key = other_key, key
    pair = key + ":" + first + "," + other_key + ":" + second
    if field == "version":
        return "{" + pair + ',"rules":[' + RULE + "]}"
    if field == "rules":
        return '{"version":1,' + pair + "}"
    fields = {"id": '"api"', "sources": '["src/*"]', "docs": '["docs/*"]'}
    body = [pair if name == field else json.dumps(name) + ":" + value
            for name, value in fields.items()]
    return '{"version":1,"rules":[{' + ",".join(body) + "}]}"


DUPLICATE_VALUES = {
    "version": ("1", "2"),
    "rules": ("[" + RULE + "]", '[{"id":"other","sources":["unused/*"],"docs":["manual/*"]}]'),
    "id": ('"api"', '"other"'),
    "sources": ('["src/*"]', '["unused/*"]'),
    "docs": ('["docs/*"]', '["manual/*"]'),
}


class DocsPolicyAmbiguityAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="docs-policy-acceptance-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        # Exclude credentials, managed Git wrappers and inherited Git overrides.
        self.env = {"PATH": os.defpath, "HOME": self.temp.name,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.put(".docs-drift.json", POLICY)
        self.put("src/api.py", "before\n")
        self.put("docs/api.md", "before\n")
        self.initial = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo,
                                       env=self.env, text=True).strip()

    def put(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m",
                 "test: synthetic policy fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        return self.git("rev-parse", "HEAD")

    def history(self, raw, *, head_policy="unchanged", docs=False):
        self.put(".docs-drift.json", raw)
        base = self.commit()
        self.put("src/api.py", "after " + base + "\n")
        if head_policy is None:
            (self.repo / ".docs-drift.json").unlink()
        elif head_policy != "unchanged":
            self.put(".docs-drift.json", head_policy)
        if docs:
            self.put("docs/api.md", "after " + base + "\n")
        return base, self.commit()

    def invoke(self, base, head, *, mutation=None, env=None):
        command = [sys.executable, "-B", str(CHECKER)]
        if mutation is not None:
            command = [sys.executable, "-B", "-c", LOSSY_PARSER, mutation, str(CHECKER)]
        return subprocess.run(command + ["--repo", str(self.repo), "--base", base, "--head", head],
                              cwd=self.repo, env=env or self.env, capture_output=True,
                              text=True, encoding="utf-8", timeout=10)

    def report(self, result):
        self.assertEqual(result.stderr, "", "CLI leaked diagnostics: " + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        report = json.loads(result.stdout)
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("schema"), "docs-drift.v1")
        return report

    def assert_invalid(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = self.report(result)
        self.assertEqual(report.get("status"), "unmeasurable", report)
        self.assertEqual(report.get("code"), "invalid_policy", report)
        self.assertIs(report.get("check_complete"), False, report)
        self.assertIs(report.get("retryable"), False, report)
        self.assertEqual(report.get("next_action"), REPAIR)
        self.assertEqual(set(report), {"schema", "status", "code", "check_complete",
                                       "retryable", "next_action"}, report)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def assert_measured(self, result, status, exit_code, *, checked=1):
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        report = self.report(result)
        self.assertEqual(report.get("status"), status, report)
        self.assertEqual(report.get("checked_rules"), checked, report)
        return report

    def test_identical_duplicates_in_each_schema_field_are_refused(self):
        for field, (value, _) in DUPLICATE_VALUES.items():
            with self.subTest(field=field):
                base, head = self.history(duplicate_policy(field, value, value))
                self.assert_invalid(self.invoke(base, head))

    def test_conflicting_duplicates_are_refused_in_both_value_orders(self):
        for field, (first, second) in DUPLICATE_VALUES.items():
            for values in [(first, second), (second, first)]:
                with self.subTest(field=field, values=values):
                    base, head = self.history(duplicate_policy(field, *values))
                    self.assert_invalid(self.invoke(base, head))

    def test_escaped_duplicate_keys_are_refused_in_both_key_orders(self):
        for field, (value, other) in DUPLICATE_VALUES.items():
            for reverse in [False, True]:
                for second in [value, other]:
                    with self.subTest(field=field, reverse=reverse, identical=second == value):
                        base, head = self.history(duplicate_policy(
                            field, value, second, escaped=True, reverse_keys=reverse))
                        self.assert_invalid(self.invoke(base, head))

    def test_duplicates_in_later_rules_and_multiple_objects_are_refused(self):
        later = '{"id":"second","sources":["lib/*"],"docs":["guide/*"],"docs":["guide/*"]}'
        for raw in ['{"version":1,"rules":[' + RULE + "," + later + "]}",
                    '{"version":1,"version":1,"rules":[' + later + "]}"]:
            with self.subTest(raw=raw):
                base, head = self.history(raw)
                self.assert_invalid(self.invoke(base, head))

    def test_duplicate_base_is_not_repaired_or_deleted_by_head(self):
        raw = duplicate_policy("rules", "[" + RULE + "]", "[" + RULE + "]")
        for head_policy in [POLICY, None, '{"version":1,"rules":[]}']:
            with self.subTest(head_policy=head_policy):
                base, head = self.history(raw, head_policy=head_policy, docs=True)
                self.assert_invalid(self.invoke(base, head))

    def test_target_duplicate_is_authoritative_even_when_merge_base_is_valid(self):
        self.put("src/api.py", "head change\n")
        head = self.commit()
        self.git("checkout", "--detach", self.initial)
        self.put(".docs-drift.json", duplicate_policy("id", '"api"', '"api"'))
        target = self.commit()
        self.assertEqual(self.git("merge-base", "--all", target, head), self.initial)
        self.assert_invalid(self.invoke(target, head))

    def test_unique_policy_still_distinguishes_clean_and_missing_docs(self):
        base, head = self.history(POLICY)
        report = self.assert_measured(self.invoke(base, head), "finding", 1)
        self.assertEqual(report["findings"][0]["code"], "docs_required")
        self.assertEqual(report["findings"][0]["rule"], "api")
        self.assertEqual(report["findings"][0]["paths"], ["src/api.py"])
        self.put("docs/api.md", "accompany source\n")
        head = self.commit()
        report = self.assert_measured(self.invoke(base, head), "clean", 0)
        self.assertEqual(report["policy_sha"], base)
        self.assertEqual(report["merge_base_sha"], base)

    def test_head_only_ambiguity_cannot_override_unique_base_policy(self):
        raw = duplicate_policy("sources", '["src/*"]', '["unused/*"]')
        base, head = self.history(POLICY, head_policy=raw)
        self.assert_measured(self.invoke(base, head), "finding", 1)
        self.put("docs/api.md", "accompany source\n")
        self.assert_measured(self.invoke(base, self.commit()), "clean", 0)

    def test_head_only_policy_does_not_activate_onboarding(self):
        (self.repo / ".docs-drift.json").unlink()
        base = self.commit()
        self.put(".docs-drift.json", duplicate_policy("version", "1", "1"))
        self.put("src/api.py", "after\n")
        self.assert_measured(self.invoke(base, self.commit()), "not_configured", 0, checked=0)

    def test_unique_escaped_keys_unicode_and_nul_safe_paths_remain_valid(self):
        raw = r'{"version":1,"rules":[{"id":"api","sources":["src/*"],"docs":["docs/雪/*"]}]}'
        raw = raw.replace("version", chr(92) + "u0076ersion")
        raw = raw.replace("sources", chr(92) + "u0073ources")
        self.put(".docs-drift.json", raw)
        base = self.commit()
        paths = ["src/a b.py", "src/a\nb.py", "src/雪.py", "src/--flag.py",
                 "src/é.py", "src/é.py"]
        for path in paths:
            self.put(path, "source\n")
        report = self.assert_measured(self.invoke(base, self.commit()), "finding", 1)
        self.assertEqual(report["findings"][0]["paths"], sorted(paths))
        self.put("docs/雪/new\npage.md", "accompany source\n")
        self.assert_measured(self.invoke(base, self.commit()), "clean", 0)

    def test_git_overrides_cannot_redirect_policy_and_check_is_read_only(self):
        base, head = self.history(POLICY)
        before = (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain=v1"),
                  (self.repo / ".docs-drift.json").read_bytes())
        env = dict(self.env, GIT_DIR=str(self.repo / "absent.git"), GIT_WORK_TREE="/nonexistent",
                   GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.bare", GIT_CONFIG_VALUE_0="true")
        first = self.invoke(base, head, env=env)
        self.assert_measured(first, "finding", 1)
        second = self.invoke(base, head, env=env)
        self.assertEqual((second.returncode, second.stdout, second.stderr),
                         (first.returncode, first.stdout, first.stderr))
        after = (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain=v1"),
                 (self.repo / ".docs-drift.json").read_bytes())
        self.assertEqual(after, before)

    def test_invalid_schema_and_private_inputs_have_one_safe_error(self):
        invalid = ["{", "[]", '{"version":true,"rules":[' + RULE + "]}",
                   '{"version":1,"rules":[]}',
                   '{"version":1,"rules":[' + RULE + '],"' + PRIVATE + '":"' + PRIVATE + '"}',
                   '{"version":1,"rules":[{"id":"api","sources":["../' + PRIVATE + '"],"docs":["docs/*"]}]}',
                   '{"version":1,"rules":[{"id":"api","sources":["src/*"],"docs":["docs/*"],"'
                   + PRIVATE + '":{"' + PRIVATE + '":1,"' + PRIVATE + '":2}}]}']
        for index, raw in enumerate(invalid):
            with self.subTest(case=index):
                base, head = self.history(raw)
                self.assert_invalid(self.invoke(base, head))

    def test_deep_input_has_controlled_invalid_policy_not_parser_traceback(self):
        raw = '{"version":1,"rules":' + "[" * 2000 + '"' + PRIVATE + '"' + "]" * 2000 + "}"
        base, head = self.history(raw)
        self.assert_invalid(self.invoke(base, head))

    def test_policy_byte_limit_is_preserved_including_multibyte_content(self):
        exact = POLICY + " " * (65536 - len(POLICY.encode("utf-8")))
        base, head = self.history(exact, docs=True)
        self.assert_measured(self.invoke(base, head), "clean", 0)
        multibyte = POLICY.replace("docs/*", "docs/雪/*")
        multibyte += " " * (65536 - len(multibyte))
        self.assertGreater(len(multibyte.encode("utf-8")), 65536)
        for raw in [exact + " ", multibyte]:
            with self.subTest(bytes=len(raw.encode("utf-8"))):
                base, head = self.history(raw)
                self.assert_invalid(self.invoke(base, head))

    def test_exact_rule_pattern_and_id_limits_remain_accepted(self):
        source = "src/" + "s" * 252
        doc = "docs/" + "d" * 251
        rules = [{"id": "a" * 64,
                  "sources": ["src/*", source] + ["unused/" + str(i) + "/*" for i in range(48)],
                  "docs": [doc] + ["guide/" + str(i) + "/*" for i in range(49)]}]
        rules += [{"id": "r" + str(i), "sources": ["unused/*"], "docs": ["guide/*"]}
                  for i in range(99)]
        self.put(".docs-drift.json", json.dumps({"version": 1, "rules": rules}))
        base = self.commit()
        self.put(source, "source\n")
        self.assert_measured(self.invoke(base, self.commit()), "finding", 1)
        self.put(doc, "accompany source\n")
        self.assert_measured(self.invoke(base, self.commit()), "clean", 0)

    def test_rule_pattern_and_id_limits_are_not_relaxed(self):
        rules = [{"id": "r" + str(index), "sources": ["unused/*"], "docs": ["docs/*"]}
                 for index in range(101)]
        invalid = [json.dumps({"version": 1, "rules": rules}),
                   json.dumps({"version": 1, "rules": [json.loads(RULE), json.loads(RULE)]})]
        for field, value in [("sources", ["src/*"] * 51), ("docs", ["docs/*"] * 51),
                             ("sources", ["s" * 257]), ("docs", ["d" * 257]),
                             ("id", "a" * 65), ("sources", ["src/\x00private"]),
                             ("docs", ["/absolute/*"])]:
            rule = json.loads(RULE)
            rule[field] = value
            invalid.append(json.dumps({"version": 1, "rules": [rule]}))
        for index, raw in enumerate(invalid):
            with self.subTest(case=index):
                base, head = self.history(raw)
                self.assert_invalid(self.invoke(base, head))

    def test_multiple_merge_bases_are_still_unmeasurable(self):
        tree = self.git("rev-parse", self.initial + "^{tree}")

        def node(label, *parents):
            args = ["commit-tree", tree, "-m", label + "\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>"]
            for parent in parents:
                args.extend(["-p", parent])
            return self.git(*args)

        left, right = node("left", self.initial), node("right", self.initial)
        base, head = node("base", left, right), node("head", right, left)
        self.assertCountEqual(self.git("merge-base", "--all", base, head).splitlines(), [left, right])
        result = self.invoke(base, head)
        self.assertEqual(result.returncode, 2)
        report = self.report(result)
        self.assertEqual(report.get("status"), "unmeasurable")
        self.assertEqual(report.get("code"), "ambiguous_base")
        self.assertIs(report.get("retryable"), False)
        self.assertTrue(report.get("next_action", "").strip())

    def test_negative_controls_first_and_last_value_wins_cannot_satisfy_refusal(self):
        for mode in ["first", "last"]:
            for field, (value, _) in DUPLICATE_VALUES.items():
                with self.subTest(mode=mode, field=field):
                    base, head = self.history(duplicate_policy(field, value, value), docs=True)
                    result = self.invoke(base, head, mutation=mode)
                    # Prove the mutant really bypassed ambiguity detection, rather
                    # than counting a launch error or unrelated schema error as a kill.
                    self.assert_measured(result, "clean", 0)
                    with self.assertRaises(AssertionError):
                        self.assert_invalid(result)


if __name__ == "__main__":
    unittest.main()
