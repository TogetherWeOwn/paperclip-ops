#!/usr/bin/env python3
"""Immutable RED acceptance for pr_standards.py --json (pr-standards-verdicts.v1).

Frozen contract
===============

``--json`` is opt-in and additive. With it, the gate prints exactly one
``pr-standards-verdicts.v1`` object to stdout (nothing else on stdout,
nothing on stderr) and exits with the same code as prose mode for the
same env (1 when any finding has severity error, else 0). Without the
flag, annotation emission, the summary line and exit codes are
byte-identical to today.

Top-level object, exactly these keys:

- ``schema``: the string ``"pr-standards-verdicts.v1"``
- ``event``: echo of the EVENT env (``"pull_request"`` for PR
  evaluation, ``"push"`` for commit evaluation in these fixtures)
- ``mode``: echo of PR_STANDARDS_MODE normalized to ``"warn"``|``"error"``
  (default ``"warn"``; any non-error becomes ``"warn"``, matching the
  gate's effective-level logic)
- ``totals``: exactly ``{findings, errors, warnings}`` counts, where
  findings == len(findings), errors == severity error count,
  warnings == severity warning count
- ``findings``: list in evaluation order (same order as prose
  annotations) of objects with exactly the keys
  ``{rule, field, severity, code, next_action, message}``

Entry fields:

- ``rule``: one of ``title-format|required-section|model-used|card-ref|
  internal-ref|commit-header``
- ``field``: one of ``title|body|branch|commits``
- ``severity``: ``error|warning``, equal to the prose annotation level
- ``code``: stable machine code (pinned per fixture below)
- ``next_action``: bounded (<=300 chars), non-empty, naming the fix;
  stable across runs
- ``message``: the existing Finding message verbatim (equal to the
  unescaped annotation message; no new disclosure surface)

Pinned codes used by these fixtures:

- bad title -> ``title-not-conventional`` (rule title-format, field title)
- missing required section -> ``missing-section`` (rule required-section,
  field body)
- model placeholder -> ``model-placeholder`` (rule model-used, field body)
- missing card ref -> ``missing-card-ref`` (rule card-ref, field body)
- internal ref on public repo -> ``internal-reference``
  (rule internal-ref, field body/title/branch as placed)
- push-event bad header -> ``commit-not-conventional``
  (rule commit-header, field commits)

Output is deterministic: the same env yields byte-identical stdout
across runs, and parsed fields are exactly equal.

RED today: pr_standards.py ignores argv, so ``--json`` prints
annotations/prose (not JSON). Every test that parses stdout as JSON
fails with a JSON decode error; that is the correct RED reason.
"""
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "pr_standards.py")
SCHEMA = "pr-standards-verdicts.v1"
TIMEOUT = 30

TOP_KEYS = {"schema", "event", "mode", "totals", "findings"}
TOTALS_KEYS = {"findings", "errors", "warnings"}
ENTRY_KEYS = {"rule", "field", "severity", "code", "next_action", "message"}
RULES = {"title-format", "required-section", "model-used", "card-ref",
         "internal-ref", "commit-header"}
FIELDS = {"title", "body", "branch", "commits"}

GOOD_BODY = """
## Thinking Path

> - The service signs every request it forwards
> - A stale signing key makes the forwarded request fail
> - The key was never refreshed after rotation
> - This pull request refreshes the key when a signature check fails

## Linked Issues or Issue Description

Refs: case_1cab5f7f

## What Changed

- Refresh the signing key on a failed signature check

## Verification

- `npm test` passes: 41 passed, 0 failed

## Risks

- Low risk. One extra key fetch on the failure path only.

## Model Used

- Example Model 1.0, 200k context, tool use

## Checklist

- [x] I searched for duplicate or related PRs and linked them above

## Merge readiness

<!-- readiness:start -->
## Merge readiness (bot-maintained — do not edit)

Head: pending — the handler fills this on the first event.

| Check | State |
|---|---|

Paperclip Review: pending
Mergeable: pending
Updated: pending
<!-- readiness:end -->
"""

# LIGHT_TYPE body: Linked section removed, dedup still ticked, card ref kept
# at the tail so docs/* passes clean while fix/* reports only the missing
# Linked section (no duplicate-search finding to map).
LIGHT_BODY = (
    GOOD_BODY.replace(
        "## Linked Issues or Issue Description\n\nRefs: case_1cab5f7f\n\n", "")
    + "\nRefs: case_1cab5f7f\n"
)

ANNOTATION_RE = re.compile(r"^::(error|warning) title=(.*?)::(.*)$")


def base_env(**overrides):
    env = {
        "EVENT": "pull_request",
        "TITLE": "fix(auth): refresh the signing key",
        "BODY": GOOD_BODY,
        "AUTHOR": "agent-one",
        "HEAD_REF": "fix/refresh-signing-key",
        "REPO_PRIVATE": "true",
        "REQUIRE_CARD_REF": "true",
        "PR_STANDARDS_MODE": "error",
    }
    env.update(overrides)
    return env


def run_gate(args, fixture):
    """Run pr_standards.py via subprocess with fixture env. Returns CompletedProcess."""
    full = dict(os.environ)
    full.update(fixture)
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        env=full, capture_output=True, text=True, timeout=TIMEOUT,
    )


def run_prose(fixture):
    return run_gate([], fixture)


def run_json(fixture):
    return run_gate(["--json"], fixture)


def unescape(text):
    # Reverse of pr_standards._escape: %0D/%0A first, %25 last.
    return text.replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")


def parse_prose(stdout):
    """Return (annotations, summary_or_None). Annotations are (level,title,message)."""
    annotations = []
    summary = None
    for line in stdout.splitlines():
        m = ANNOTATION_RE.match(line)
        if m:
            annotations.append((m.group(1), unescape(m.group(2)), unescape(m.group(3))))
        elif line in ("PR conventions OK", "PR conventions OK (warnings above)"):
            summary = line
    return annotations, summary


def parse_json_stdout(proc, fixture_label):
    """Assert single-object JSON stdout; return parsed object. Fails RED today."""
    assert proc.stderr == "", f"{fixture_label}: stderr not empty: {proc.stderr!r}"
    assert "::" not in proc.stdout, (
        f"{fixture_label}: --json stdout holds annotations (argv ignored): "
        f"{proc.stdout[:400]!r}")
    assert "PR conventions OK" not in proc.stdout, (
        f"{fixture_label}: --json stdout holds prose summary: {proc.stdout[:400]!r}")
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{fixture_label}: --json stdout is not one JSON object "
            f"(RED: argv ignored, output is annotations/prose): {exc}; "
            f"stdout head: {proc.stdout[:400]!r}") from exc
    assert isinstance(obj, dict), f"{fixture_label}: top level is not an object: {type(obj)}"
    assert set(obj.keys()) == TOP_KEYS, (
        f"{fixture_label}: top keys {sorted(obj.keys())} != {sorted(TOP_KEYS)}")
    assert obj["schema"] == SCHEMA, f"{fixture_label}: schema {obj['schema']!r}"
    return obj


def assert_totals(test, obj, label):
    totals = obj["totals"]
    test.assertEqual(set(totals.keys()), TOTALS_KEYS, label)
    findings = obj["findings"]
    test.assertIsInstance(findings, list, label)
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    test.assertEqual(totals["findings"], len(findings), label)
    test.assertEqual(totals["errors"], errors, label)
    test.assertEqual(totals["warnings"], warnings, label)
    for entry in findings:
        test.assertEqual(set(entry.keys()), ENTRY_KEYS, label)
        test.assertIn(entry["rule"], RULES, label)
        test.assertIn(entry["field"], FIELDS, label)
        test.assertIn(entry["severity"], ("error", "warning"), label)
        test.assertIsInstance(entry["code"], str, label)
        test.assertTrue(entry["code"], label)
        test.assertIsInstance(entry["next_action"], str, label)
        test.assertTrue(entry["next_action"].strip(), label)
        test.assertLessEqual(len(entry["next_action"]), 300, label)
        test.assertIsInstance(entry["message"], str, label)
        test.assertTrue(entry["message"].strip(), label)


def assert_echo(test, obj, fixture, label):
    test.assertEqual(obj["event"], fixture.get("EVENT", ""), label)
    raw_mode = (fixture.get("PR_STANDARDS_MODE", "warn") or "warn").strip().lower()
    expected_mode = "error" if raw_mode == "error" else "warn"
    test.assertEqual(obj["mode"], expected_mode, label)


def assert_message_parity(test, prose_proc, obj, label):
    annotations, _ = parse_prose(prose_proc.stdout)
    json_messages = [f["message"] for f in obj["findings"]]
    prose_messages = [m for _, _, m in annotations]
    test.assertEqual(json_messages, prose_messages, label)
    json_levels = [f["severity"] for f in obj["findings"]]
    prose_levels = [lvl for lvl, _, _ in annotations]
    test.assertEqual(json_levels, prose_levels, label)


class SingleObject(unittest.TestCase):
    def test_json_clean_is_one_object_no_annotations(self):
        fixture = base_env()
        proc = run_json(fixture)
        obj = parse_json_stdout(proc, "clean")
        self.assertEqual(proc.returncode, 0, proc.stdout[:400])
        assert_totals(self, obj, "clean")
        self.assertEqual(obj["findings"], [])
        self.assertEqual(obj["totals"], {"findings": 0, "errors": 0, "warnings": 0})
        assert_echo(self, obj, fixture, "clean")

    def test_json_findings_is_one_object_no_annotations(self):
        fixture = base_env(TITLE="fix auth")
        proc = run_json(fixture)
        obj = parse_json_stdout(proc, "bad-title")
        self.assertEqual(proc.returncode, 1, proc.stdout[:400])
        assert_totals(self, obj, "bad-title")
        self.assertEqual(len(obj["findings"]), 1)

    def test_prose_is_not_json(self):
        # Documents the RED gap: prose mode prints annotations/summary, never JSON.
        proc = run_prose(base_env(TITLE="fix auth"))
        self.assertIn("::error title=PR title::", proc.stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc.stdout)


class EightCaseMatrix(unittest.TestCase):
    def check_case(self, label, fixture, expected):
        prose = run_prose(fixture)
        js = run_json(fixture)
        # Exit parity prose vs JSON for this env.
        self.assertEqual(js.returncode, prose.returncode,
                         f"{label}: exit {js.returncode} != prose {prose.returncode}")
        obj = parse_json_stdout(js, label)
        assert_totals(self, obj, label)
        assert_echo(self, obj, fixture, label)
        assert_message_parity(self, prose, obj, label)
        findings = obj["findings"]
        self.assertEqual(len(findings), len(expected), f"{label}: {findings}")
        for entry, (rule, field, severity, code, needle) in zip(findings, expected):
            self.assertEqual(entry["rule"], rule, label)
            self.assertEqual(entry["field"], field, label)
            self.assertEqual(entry["severity"], severity, label)
            self.assertEqual(entry["code"], code, label)
            self.assertIn(needle.lower(), entry["next_action"].lower(), label)
            # Message already pinned by parity; also pin a stable substring.
            self.assertTrue(entry["message"].strip(), label)

    def test_clean_pr(self):
        fixture = base_env()
        prose = run_prose(fixture)
        js = run_json(fixture)
        self.assertEqual(prose.returncode, 0, prose.stdout)
        self.assertEqual(js.returncode, 0, js.stdout[:400])
        obj = parse_json_stdout(js, "clean")
        assert_totals(self, obj, "clean")
        self.assertEqual(obj["findings"], [])
        assert_echo(self, obj, fixture, "clean")
        annotations, summary = parse_prose(prose.stdout)
        self.assertEqual(annotations, [])
        self.assertEqual(summary, "PR conventions OK")

    def test_bad_title(self):
        fixture = base_env(TITLE="fix auth")
        prose = run_prose(fixture)
        js = run_json(fixture)
        self.assertEqual(prose.returncode, 1, prose.stdout)
        self.check_case("bad-title", fixture, [
            ("title-format", "title", "error", "title-not-conventional", "Conventional"),
        ])
        obj = parse_json_stdout(js, "bad-title-substring")
        self.assertIn("Conventional Commits", obj["findings"][0]["message"])

    def test_missing_required_section(self):
        body = GOOD_BODY.replace("## Risks", "## Notes")
        fixture = base_env(BODY=body)
        self.check_case("missing-section", fixture, [
            ("required-section", "body", "error", "missing-section", "Risks"),
        ])
        obj = parse_json_stdout(run_json(fixture), "missing-section-substring")
        self.assertIn("Missing section", obj["findings"][0]["message"])
        self.assertIn("Risks", obj["findings"][0]["message"])

    def test_model_placeholder(self):
        body = GOOD_BODY.replace("- Example Model 1.0, 200k context, tool use", "- <model>")
        fixture = base_env(BODY=body)
        self.check_case("model-placeholder", fixture, [
            ("model-used", "body", "error", "model-placeholder", "model"),
        ])
        obj = parse_json_stdout(run_json(fixture), "model-placeholder-substring")
        self.assertIn("Model Used", obj["findings"][0]["message"])

    def test_missing_card_ref(self):
        body = GOOD_BODY.replace("Refs: case_1cab5f7f", "Fixes #12")
        fixture = base_env(BODY=body)
        self.check_case("missing-card-ref", fixture, [
            ("card-ref", "body", "error", "missing-card-ref", "card"),
        ])
        obj = parse_json_stdout(run_json(fixture), "missing-card-ref-substring")
        self.assertIn("card reference", obj["findings"][0]["message"])

    def test_internal_ref_on_public_repo(self):
        # GOOD_BODY carries a case_ ref: clean for private, internal for public.
        fixture = base_env(REPO_PRIVATE="false")
        self.check_case("internal-ref-public", fixture, [
            ("internal-ref", "body", "error", "internal-reference", "internal"),
        ])
        obj = parse_json_stdout(run_json(fixture), "internal-ref-substring")
        self.assertIn("internal references", obj["findings"][0]["message"])

    def test_light_type_skips(self):
        # docs/* skips Linked + dedup: clean. Same body as fix/* reports Linked.
        docs_fixture = base_env(TITLE="docs(readme): explain setup", BODY=LIGHT_BODY)
        prose_docs = run_prose(docs_fixture)
        js_docs = run_json(docs_fixture)
        self.assertEqual(prose_docs.returncode, 0, prose_docs.stdout)
        self.assertEqual(js_docs.returncode, 0, js_docs.stdout[:400])
        obj_docs = parse_json_stdout(js_docs, "light-docs")
        assert_totals(self, obj_docs, "light-docs")
        self.assertEqual(obj_docs["findings"], [], obj_docs["findings"])
        assert_echo(self, obj_docs, docs_fixture, "light-docs")

        fix_fixture = base_env(TITLE="fix(auth): refresh the key", BODY=LIGHT_BODY)
        self.check_case("light-fix-reports-linked", fix_fixture, [
            ("required-section", "body", "error", "missing-section",
             "Linked Issues or Issue Description"),
        ])

    def test_push_event_commit_headers(self):
        commits = json.dumps([{"id": "a" * 40, "message": "Update stuff"}])
        fixture = base_env(EVENT="push", COMMITS=commits)
        self.check_case("push-commit-header", fixture, [
            ("commit-header", "commits", "error", "commit-not-conventional", "squash"),
        ])
        obj = parse_json_stdout(run_json(fixture), "push-substring")
        self.assertIn("Conventional", obj["findings"][0]["message"])


class ExitParityWarnAndError(unittest.TestCase):
    # Representative fixtures covering always-error, mode-dependent, and clean.
    def fixtures(self):
        bad_title = base_env(TITLE="fix auth")
        missing = base_env(BODY=GOOD_BODY.replace("## Risks", "## Notes"))
        public_internal = base_env(REPO_PRIVATE="false")
        push_bad = base_env(
            EVENT="push",
            COMMITS=json.dumps([{"id": "b" * 40, "message": "Update stuff"}]))
        clean = base_env()
        return [
            ("bad-title", bad_title, {"warn": 1, "error": 1}),
            ("missing-section", missing, {"warn": 0, "error": 1}),
            ("internal-public", public_internal, {"warn": 0, "error": 1}),
            ("push-header", push_bad, {"warn": 1, "error": 1}),
            ("clean", clean, {"warn": 0, "error": 0}),
        ]

    def test_exit_parity_prose_vs_json_in_both_modes(self):
        for label, template, expected in self.fixtures():
            for mode, want in expected.items():
                fixture = dict(template)
                fixture["PR_STANDARDS_MODE"] = mode
                prose = run_prose(fixture)
                js = run_json(fixture)
                self.assertEqual(prose.returncode, want, f"{label}/{mode} prose")
                self.assertEqual(js.returncode, prose.returncode, f"{label}/{mode} parity")
                # JSON shape still holds in both modes (RED today: not JSON).
                obj = parse_json_stdout(js, f"{label}/{mode}")
                assert_echo(self, obj, fixture, f"{label}/{mode}")
                if label == "missing-section" and mode == "warn":
                    self.assertEqual(obj["findings"][0]["severity"], "warning")
                if label == "missing-section" and mode == "error":
                    self.assertEqual(obj["findings"][0]["severity"], "error")


class Stability(unittest.TestCase):
    def test_exact_field_stability_across_two_runs(self):
        cases = [
            ("clean", base_env()),
            ("bad-title", base_env(TITLE="fix auth")),
            ("missing-section", base_env(BODY=GOOD_BODY.replace("## Risks", "## Notes"))),
            ("push", base_env(
                EVENT="push",
                COMMITS=json.dumps([{"id": "c" * 40, "message": "Update stuff"}]))),
        ]
        for label, fixture in cases:
            first = run_json(fixture)
            second = run_json(fixture)
            self.assertEqual(first.returncode, second.returncode, label)
            # Byte-identical stdout (deterministic rendering).
            self.assertEqual(first.stdout, second.stdout, label)
            self.assertEqual(first.stderr, "", label)
            self.assertEqual(second.stderr, "", label)
            # Parsed fields exactly equal (RED today: stdout is not JSON).
            obj1 = parse_json_stdout(first, f"{label}/run1")
            obj2 = parse_json_stdout(second, f"{label}/run2")
            self.assertEqual(obj1, obj2, label)


class Help(unittest.TestCase):
    def test_help_names_json(self):
        full = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--help"],
            env=full, capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(proc.returncode, 0, proc.stdout[:400] + proc.stderr[:200])
        self.assertIn("--json", proc.stdout, proc.stdout[:600])


if __name__ == "__main__":
    unittest.main()
