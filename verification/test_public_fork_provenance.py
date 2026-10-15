#!/usr/bin/env python3
"""Offline synthetic evidence; positive snapshots are NOT live approvals."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import public_fork_provenance as gate

ROOT = Path(__file__).resolve().parent
AUTHOR = "00000000-0000-4000-8000-000000000019"
REVIEWER = "00000000-0000-4000-8000-000000000206"
LINK = "/TOG/issues/case_8c5fe4cc#comment-synthetic-offline-fixture"
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
BEFORE = "2026-10-04T10:00:00Z"
AFTER = "2026-10-04T11:00:00Z"
CAPTURE = "2026-10-04T12:00:00Z"


def sha(n):
    return f"{n:040x}"


def review(head, base, merge):
    return {"agentId": REVIEWER, "headSha": head, "baseSha": base,
            "mergeSha": merge, "verdict": "APPROVE", "observedAt": BEFORE,
            "record": LINK, "superseded": False}


def transition(parent, merge, head, ref="refs/heads/master", number=1):
    pubs = [{"sha": s, "addedLinesDigest": "a" * 64, "messageDigest": "b" * 64,
             "firstPublicAt": AFTER} for s in dict.fromkeys([head, merge])]
    return {"kind": "squash", "destinationRef": ref,
            "pr": {"number": number, "headRepo": gate.REPO, "headRef": "refs/heads/fix/synthetic",
                   "headSha": head, "baseRepo": gate.REPO, "baseRef": ref,
                   "baseSha": parent, "authorAgentId": AUTHOR, "mergedAt": AFTER},
            "mergeSha": merge, "mergeParents": [parent], "publications": pubs,
            "authorScan": {"agentId": AUTHOR, "observedAt": BEFORE, "record": LINK,
                           "publications": [{k: p[k] for k in ("sha", "addedLinesDigest", "messageDigest")} for p in pubs]},
            "codeReview": review(head, parent, merge),
            "disclosureReview": review(head, parent, merge), "integrationReview": None,
            "weakness": {"applicable": "no", "agentId": REVIEWER, "observedAt": BEFORE,
                         "record": LINK, "alternative": None}, "holds": [], "findings": []}


def fixture(timing="POSTHOC"):
    r = {"repository": gate.REPO, "ref": "refs/heads/master", "tipSha": sha(3),
         "capturedAt": CAPTURE,
         "checkpoint": {"sha": sha(1), "timingClass": timing, "decisionAt": BEFORE,
                        "agentId": REVIEWER, "record": LINK, "firstPublicAt": "unknown"},
         "history": {"state": "complete", "checkpointSha": sha(1), "tipSha": sha(3),
                     "commits": [{"sha": sha(3), "parents": [sha(1)]}]},
         "transitions": [transition(sha(1), sha(3), sha(2))]}
    pin = {"sha": sha(1), "timingClass": timing, "decisionAt": BEFORE,
           "card": "case_8c5fe4cc", "firstPublicAt": "unknown"}
    return r, pin


def assess(r, pin):
    gate.validate_record(r)
    return gate.evaluate([r], r["repository"], r["ref"], r["tipSha"], pin, NOW)


class Consistency(unittest.TestCase):
    def test_reviewed_successor_preserves_posthoc(self):
        r, pin = fixture()
        out = assess(r, pin)
        self.assertEqual(out["status"], "consistent-snapshot")
        self.assertEqual(out["candidateTimingClass"], "POSTHOC")
        self.assertIs(out["acceptanceEnabled"], False)

    def test_declared_checkpoint_and_all_prepush_closures(self):
        r, pin = fixture("DECLARED")
        out = assess(r, pin)
        self.assertEqual(out["status"], "consistent-snapshot")
        self.assertEqual(out["candidateTimingClass"], "DECLARED")

    def test_unknown_or_postpush_never_declared(self):
        for first in ("unknown", BEFORE):
            with self.subTest(first=first):
                r, pin = fixture("DECLARED")
                r["transitions"][0]["publications"][0]["firstPublicAt"] = first
                out = assess(r, pin)
                self.assertEqual(out["status"], "consistent-snapshot")
                self.assertEqual(out["candidateTimingClass"], "POSTHOC")

    def test_earlier_leaf_cannot_be_hidden_by_final_approval(self):
        r, pin = fixture()
        r["tipSha"] = r["history"]["tipSha"] = sha(5)
        r["history"]["commits"].append({"sha": sha(5), "parents": [sha(3)]})
        r["transitions"].append(transition(sha(3), sha(5), sha(4), number=2))
        self.assertEqual(assess(r, pin)["status"], "consistent-snapshot")
        for surface in ("message", "added-lines"):
            r["transitions"][0]["findings"] = [{"sha": sha(3), "surface": surface, "code": "operational-disclosure"}]
            self.assertEqual(assess(r, pin)["status"], "finding")
            self.assertIn("range-disclosure-finding", assess(r, pin)["codes"])

    def test_evaluated_missing_evidence_and_moved_head(self):
        r, pin = fixture()
        self.assertEqual(gate.evaluate([], gate.REPO, r["ref"], r["tipSha"], pin, NOW)["status"], "finding")
        self.assertIn("moved-head", gate.evaluate([r], gate.REPO, r["ref"], sha(9), pin, NOW)["codes"])
        self.assertIn("missing-ref-evidence", gate.evaluate([r], "Elsewhere/paperclip", r["ref"], r["tipSha"], pin, NOW)["codes"])

    def test_fail_closed_negative_controls(self):
        # Each mutation starts from the same valid control: no unrelated conjunct can kill it.
        mutations = [
            ("moved-checkpoint", lambda r: r["checkpoint"].update(sha=sha(8)), "unaccepted-or-moved-checkpoint"),
            ("backdated-decision", lambda r: r["checkpoint"].update(decisionAt=AFTER), "unaccepted-or-moved-checkpoint"),
            ("relabel-checkpoint", lambda r: r["checkpoint"].update(firstPublicAt=AFTER), "checkpoint-timing-relabelled"),
            ("direct", lambda r: r["transitions"][0].update(kind="direct"), "unsupported-transition-exact-pin-only"),
            ("cherry-pick", lambda r: r["transitions"][0].update(kind="cherry-pick"), "unsupported-transition-exact-pin-only"),
            ("rewrite", lambda r: r["history"]["commits"][0].update(parents=[sha(8)]), "nonlinear-or-rewritten-history-exact-pin-only"),
            ("extra-parent", lambda r: r["history"]["commits"][0]["parents"].append(sha(8)), "nonlinear-or-rewritten-history-exact-pin-only"),
            ("no-pr", lambda r: r["transitions"][0].update(pr=None), "missing-pr-provenance-exact-pin-only"),
            ("no-code", lambda r: r["transitions"][0].update(codeReview=None), "missing-codeReview"),
            ("no-disclosure", lambda r: r["transitions"][0].update(disclosureReview=None), "missing-disclosureReview"),
            ("adverse", lambda r: r["transitions"][0]["codeReview"].update(verdict="CHANGES"), "adverse-or-superseded-codeReview"),
            ("superseded", lambda r: r["transitions"][0]["codeReview"].update(superseded=True), "adverse-or-superseded-codeReview"),
            ("shared-bot", lambda r: r["transitions"][0]["codeReview"].update(agentId=AUTHOR), "nonindependent-codeReview"),
            ("wrong-head-review", lambda r: r["transitions"][0]["disclosureReview"].update(headSha=sha(8)), "wrong-tuple-disclosureReview"),
            ("private-candidate-pass", lambda r: r["transitions"][0]["disclosureReview"].update(headSha=sha(8)), "wrong-tuple-disclosureReview"),
            ("public-pass-held", lambda r: r["transitions"][0]["holds"].append(LINK), "publication-hold"),
            ("wrong-head-repo", lambda r: r["transitions"][0]["pr"].update(headRepo="Elsewhere/paperclip"), "wrong-repo-or-pr-target"),
            ("upstream-base", lambda r: r["transitions"][0]["pr"].update(baseRepo="paperclipai/paperclip"), "wrong-repo-or-pr-target"),
            ("cut-target", lambda r: r["transitions"][0]["pr"].update(baseRef="refs/heads/cut/build"), "wrong-repo-or-pr-target"),
            ("wrong-ref", lambda r: r["transitions"][0].update(destinationRef="refs/heads/tog/other"), "wrong-destination-ref"),
            ("wrong-merge", lambda r: r["transitions"][0].update(mergeSha=sha(8)), "merge-object-binding"),
            ("wrong-base-without-carry", lambda r: r["transitions"][0]["pr"].update(baseSha=sha(8)), "missing-integrationReview"),
            ("no-message-scope", lambda r: r["transitions"][0]["authorScan"]["publications"].pop(), "incomplete-added-lines-or-message-scan"),
            ("wrong-digest", lambda r: r["transitions"][0]["authorScan"]["publications"][0].update(messageDigest="c" * 64), "incomplete-added-lines-or-message-scan"),
            ("unknown-weakness", lambda r: r["transitions"][0]["weakness"].update(applicable="unknown"), "unknown-weakness-applicability"),
            ("missing-notification", lambda r: r["transitions"][0]["weakness"].update(applicable="yes"), "missing-or-unbound-private-alternative"),
            ("future-scan", lambda r: r["transitions"][0]["authorScan"].update(observedAt="2026-10-05T00:00:00Z"), "future-scan"),
        ]
        for name, mutate, code in mutations:
            with self.subTest(name=name):
                r, pin = fixture()
                self.assertEqual(assess(r, pin)["status"], "consistent-snapshot")
                mutate(r)
                out = assess(r, pin)
                self.assertEqual(out["status"], "finding")
                self.assertIn(code, out["codes"])
                self.assertIs(out["acceptanceEnabled"], False)

    def test_recorded_base_may_differ_with_explicit_integration(self):
        r, pin = fixture()
        t = r["transitions"][0]
        t["pr"]["baseSha"] = sha(8)
        for key in ("codeReview", "disclosureReview"):
            t[key]["baseSha"] = sha(8)
        self.assertIn("missing-integrationReview", assess(r, pin)["codes"])
        t["integrationReview"] = review(sha(2), sha(8), sha(3))
        self.assertEqual(assess(r, pin)["status"], "consistent-snapshot")

    def test_pr_head_and_renamed_alias(self):
        r, pin = fixture()
        ref = "refs/heads/fix/synthetic"
        r["ref"] = ref
        r["tipSha"] = r["history"]["tipSha"] = sha(2)
        r["history"]["commits"][0]["sha"] = sha(2)
        t = transition(sha(1), sha(2), sha(2))
        t.update(kind="pr-head", destinationRef=ref)
        t["pr"]["mergedAt"] = "unknown"
        r["transitions"] = [t]
        self.assertEqual(assess(r, pin)["status"], "consistent-snapshot")
        t["pr"]["headRef"] = "refs/heads/fix/old-name"
        self.assertIn("renamed-or-unbound-pr-head", assess(r, pin)["codes"])

    def test_out_of_class(self):
        for repo, ref in (("TogetherWeOwn/CLIProxyAPI", "refs/heads/master"),
                          (gate.REPO, "refs/heads/cut/build")):
            r, pin = fixture()
            r.update(repository=repo, ref=ref)
            self.assertEqual(assess(r, pin)["codes"], ["out-of-class-exact-pin-only"])

    def test_private_notification_and_private_build_controls(self):
        r, pin = fixture()
        w = r["transitions"][0]["weakness"]
        w.update(applicable="yes", alternative={"kind": "private-notification", "candidateSha": sha(2),
                 "sourceVisibility": "private", "observedAt": BEFORE, "record": LINK, "verified": True,
                 "recipient": LINK, "channel": "private-security-contact", "deliveryRecord": LINK})
        self.assertEqual(assess(r, pin)["status"], "consistent-snapshot")
        for field, value in (("verified", False), ("sourceVisibility", "public"), ("candidateSha", sha(9))):
            bad = deepcopy(r)
            bad["transitions"][0]["weakness"]["alternative"][field] = value
            self.assertIn("missing-or-unbound-private-alternative", assess(bad, pin)["codes"])
        w["alternative"] = {"kind": "private-build", "candidateSha": sha(2), "sourceVisibility": "private",
                            "observedAt": BEFORE, "record": LINK, "verified": True,
                            "sourceRepo": "private-source", "sourceSha": sha(2)}
        self.assertEqual(assess(r, pin)["status"], "consistent-snapshot")
        w["alternative"]["sourceSha"] = sha(9)
        self.assertIn("private-build-not-exact-private-source", assess(r, pin)["codes"])

    def test_cannot_evaluate_incomplete_unavailable_stale(self):
        for mutate in (lambda r: r["history"].update(state="unavailable"),
                       lambda r: r["history"].update(state="truncated"),
                       lambda r: r["history"]["commits"].clear(),
                       lambda r: r.update(capturedAt="2026-10-01T12:00:00Z")):
            r, pin = fixture()
            mutate(r)
            self.assertEqual(assess(r, pin)["status"], "cannot-evaluate")

    def test_captured_ciso_ranges_stay_negative(self):
        # Real tuples; ALL review/scan objects generated below are synthetic.
        ranges = json.loads((ROOT.parent / "test/fixtures/disclosure/release_ranges.json").read_text())
        for captured in ranges:
            with self.subTest(ref=captured["ref"]):
                r, pin = fixture()
                r["ref"] = captured["ref"]
                r["checkpoint"]["sha"] = pin["sha"] = captured["checkpoint"]
                r["history"]["checkpointSha"] = captured["checkpoint"]
                r["transitions"] = []
                r["history"]["commits"] = []
                parent = captured["checkpoint"]
                for row in captured["transitions"]:
                    t = transition(parent, row["merge"], row["head"], captured["ref"], row["pr"])
                    t["pr"]["baseSha"] = row["base"]
                    if row["base"] != parent:
                        for key in ("codeReview", "disclosureReview"):
                            t[key]["baseSha"] = row["base"]
                        t["integrationReview"] = review(row["head"], row["base"], row["merge"])
                    if row.get("findingSurface"):
                        t["findings"] = [{"sha": row["merge"], "surface": row["findingSurface"], "code": "operational-disclosure"}]
                    r["transitions"].append(t)
                    r["history"]["commits"].append({"sha": row["merge"], "parents": [parent]})
                    parent = row["merge"]
                r["tipSha"] = r["history"]["tipSha"] = parent
                out = assess(r, pin)
                self.assertEqual(out["status"], "finding")
                self.assertIn("range-disclosure-finding", out["codes"])
                for t in r["transitions"]:
                    t["findings"] = []
                self.assertEqual(assess(r, pin)["status"], "consistent-snapshot")


class Boundary(unittest.TestCase):
    def test_parser_rejects_malformed_unknown_duplicate_and_oversized(self):
        r, _ = fixture()
        data = {"version": 1, "records": [r]}
        bad = deepcopy(data)
        bad["records"][0]["transitions"][0]["codeReview"]["agentId"] = "togetherweown[bot]"
        cases = ["{", '{"version":1,"version":1,"records":[]}', json.dumps(bad),
                 json.dumps({**data, "execute": "do not execute this"}),
                 json.dumps({"version": 1, "records": [r, r]}), " " * (gate.MAX_BYTES + 1)]
        with tempfile.TemporaryDirectory(prefix="provenance-test-") as d:
            path = Path(d) / "evidence.json"
            path.write_text(json.dumps(data))
            self.assertEqual(len(gate.load(path)), 1)
            for raw in cases:
                with self.subTest(size=len(raw)):
                    path.write_text(raw)
                    with self.assertRaises(gate.CannotEvaluate):
                        gate.load(path)
            with self.assertRaises(gate.CannotEvaluate):
                gate.load(Path(d) / "missing.json")

    def test_nonregular_input_is_refused_without_waiting(self):
        with tempfile.TemporaryDirectory(prefix="provenance-fifo-") as d:
            path = Path(d) / "pipe"
            os.mkfifo(path)
            with self.assertRaisesRegex(gate.CannotEvaluate, "nonregular-evidence"):
                gate.load(path)
            with self.assertRaises(gate.CannotEvaluate):
                gate.load(d)

    def test_pin_precedence(self):
        raw = (f"POSTHOC|paperclip|refs/heads/master|{sha(1)}|case_8c5fe4cc|unknown|{BEFORE}|f\n"
               f"DECLARED|paperclip|refs/heads/master|{sha(2)}|case_d02d0252|{BEFORE}|f\n")
        pin = gate.read_pin(raw, gate.REPO, "refs/heads/master")
        self.assertEqual(pin["timingClass"], "POSTHOC")
        self.assertEqual(pin["sha"], sha(1))
        self.assertIsNone(gate.read_pin(raw, gate.REPO, "refs/heads/other"))

    def test_cli_exit_codes_and_live_pin_fallback(self):
        # Query through the shell integration. Real POSTHOC is the immutable
        # anchor; all successor tuples below remain synthetic fixture claims.
        r, _ = fixture()
        r["capturedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        r["checkpoint"].update(sha="2ff804e873462c44b45532e85e694b9b198e55d0",
                               decisionAt="2026-10-02T20:53:55Z", record="/TOG/issues/case_fcdcc723")
        r["history"]["checkpointSha"] = r["checkpoint"]["sha"]
        r["history"]["commits"][0]["parents"] = [r["checkpoint"]["sha"]]
        t = r["transitions"][0]
        t["mergeParents"] = [r["checkpoint"]["sha"]]
        t["pr"]["baseSha"] = r["checkpoint"]["sha"]
        for key in ("codeReview", "disclosureReview"):
            t[key]["baseSha"] = r["checkpoint"]["sha"]
        with tempfile.TemporaryDirectory(prefix="provenance-cli-") as d:
            path = Path(d) / "evidence.json"
            command = ["bash", str(ROOT / "case_092cd51d-public-fork-disclosure-audit.sh"),
                       "--provenance-diagnostics", str(path), "paperclip", r["ref"], r["tipSha"]]
            env = {"PATH": "/usr/bin:/bin", "HOME": d, "TMPDIR": d}
            path.write_text(json.dumps({"version": 1, "records": [r]}))
            out = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIs(json.loads(out.stdout)["acceptanceEnabled"], False)
            r["transitions"][0]["codeReview"]["verdict"] = "CHANGES"
            path.write_text(json.dumps({"version": 1, "records": [r]}))
            self.assertEqual(subprocess.run(command, env=env, capture_output=True, timeout=30).returncode, 1)
            path.write_text("{")
            self.assertEqual(subprocess.run(command, env=env, capture_output=True, timeout=30).returncode, 2)

    def test_live_audit_never_accepts_a_consistent_snapshot(self):
        r, _ = fixture()
        r["capturedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        checkpoint = "2ff804e873462c44b45532e85e694b9b198e55d0"
        r["checkpoint"].update(sha=checkpoint, decisionAt="2026-10-02T20:53:55Z", record="/TOG/issues/case_fcdcc723")
        r["history"]["checkpointSha"] = checkpoint
        r["history"]["commits"][0]["parents"] = [checkpoint]
        t = r["transitions"][0]
        t["mergeParents"] = [checkpoint]
        t["pr"]["baseSha"] = checkpoint
        for key in ("codeReview", "disclosureReview"):
            t[key]["baseSha"] = checkpoint
        with tempfile.TemporaryDirectory(prefix="provenance-live-stub-") as d:
            directory = Path(d)
            evidence = directory / "evidence.json"
            evidence.write_text(json.dumps({"version": 1, "records": [r]}))
            curl = directory / "curl"
            curl.write_text('#!/bin/sh\nprintf \'%s\\n\' \'[{"name":"paperclip","fork":true,"visibility":"public","parent":{"full_name":"paperclipai/paperclip"}}]\'\n')
            git = directory / "git"
            git.write_text('''#!/bin/sh
case "$*" in
  *paperclip-ops.git*) exit 128;;
  *ls-remote*TogetherWeOwn/paperclip.git*) printf '%s\\t%s\\n' "$FIXTURE_TIP" "$FIXTURE_REF";;
  *ls-remote*paperclipai/paperclip.git*) printf '%s\\t%s\\n' 'ffffffffffffffffffffffffffffffffffffffff' 'refs/heads/master';;
  *log*%ae*) printf '%s\\n' "$FIXTURE_AUTHOR";;
  *log*%s*) printf '%s\\n' 'synthetic offline subject';;
  *init*|*fetch*) exit 0;;
  *) exit 99;;
esac
''')
            curl.chmod(0o755)
            git.chmod(0o755)
            env = {"PATH": d + ":/usr/bin:/bin", "HOME": d, "TMPDIR": d,
                   "DISCLOSURE_AUDIT_CACHE": str(directory / "cache"),
                   "DISCLOSURE_AUDIT_PROVENANCE_EVIDENCE": str(evidence),
                   "FIXTURE_TIP": r["tipSha"], "FIXTURE_REF": r["ref"],
                   "FIXTURE_AUTHOR": "togetherweown[bot]@users.noreply.github.com"}
            command = ["bash", str(ROOT / "case_092cd51d-public-fork-disclosure-audit.sh")]
            out = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 1, out.stderr + out.stdout)
            self.assertIn('"status": "consistent-snapshot"', out.stdout)
            self.assertIn('"acceptanceEnabled": false', out.stdout)
            self.assertIn("0 declared/clean, 0 post-hoc adjudicated, 1 undeclared publication(s), 0 unresolved", out.stdout)
            evidence.write_text("{")
            out = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 2, out.stderr + out.stdout)
            self.assertIn("1 undeclared publication(s)", out.stdout)
            # Unresolved authorship must short-circuit BEFORE any evidence lookup.
            env.update(FIXTURE_REF="refs/heads/case_c89b5362-synthetic", FIXTURE_AUTHOR="external@example.invalid")
            out = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 1, out.stderr + out.stdout)
            self.assertIn("0 undeclared publication(s), 1 unresolved", out.stdout)
            self.assertNotIn('"mode": "diagnostic-only"', out.stdout)

    def test_assertion_killed_mutants(self):
        mutations = [
            ('if t["findings"]:', 'if False:', "Consistency.test_captured_ciso_ranges_stay_negative"),
            ('if t["holds"]:', 'if False:', "Consistency.test_fail_closed_negative_controls"),
            ('if v["agentId"] == p["authorAgentId"]:', 'if False:', "Consistency.test_fail_closed_negative_controls"),
            ('if v["verdict"] != "APPROVE" or v["superseded"]:', 'if False:', "Consistency.test_fail_closed_negative_controls"),
            ('timing = "POSTHOC"', 'timing = "DECLARED"', "Consistency.test_unknown_or_postpush_never_declared"),
            ('codes, timing, previous = [], c["timingClass"], c["sha"]',
             'codes, timing, previous = [], "DECLARED", c["sha"]', "Consistency.test_reviewed_successor_preserves_posthoc"),
            ('"acceptanceEnabled": False', '"acceptanceEnabled": True', "Consistency.test_reviewed_successor_preserves_posthoc"),
        ]
        source = (ROOT / "public_fork_provenance.py").read_text()
        with tempfile.TemporaryDirectory(prefix="provenance-mutants-") as d:
            directory = Path(d)
            verification = directory / "verification"
            verification.mkdir()
            fixture_dir = directory / "test/fixtures/disclosure"
            fixture_dir.mkdir(parents=True)
            shutil.copy(ROOT.parent / "test/fixtures/disclosure/release_ranges.json", fixture_dir)
            shutil.copy(Path(__file__), verification)
            module = verification / "public_fork_provenance.py"
            for anchor, replacement, target in mutations:
                with self.subTest(anchor=anchor):
                    self.assertEqual(source.count(anchor), 1)
                    module.write_text(source)
                    command = [sys.executable, "-B", "-m", "unittest", "test_public_fork_provenance." + target]
                    clean = subprocess.run(command, cwd=verification, capture_output=True, text=True, timeout=30)
                    self.assertEqual(clean.returncode, 0, clean.stderr)
                    module.write_text(source.replace(anchor, replacement))
                    killed = subprocess.run(command, cwd=verification, capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(killed.returncode, 0)
                    self.assertIn("FAIL:", killed.stderr)
                    self.assertNotIn("ERROR:", killed.stderr)
                    self.assertIn("AssertionError", killed.stderr)


if __name__ == "__main__":
    unittest.main()
