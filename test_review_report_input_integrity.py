"""Immutable offline acceptance contract for review-report input integrity.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_review_report_input_integrity
Only synthetic JSONL is read. Each invocation runs the REAL public CLI, with a
five-second deadline, no inherited credentials, and an audit guard rejecting
network, subprocess/host commands and filesystem mutations. No router is run.
The existing router suite is a separate positive-control command.

This suite specifies the required SUBSET of the new --json v1 interface; extra
fields are allowed. The Builder documents this same contract in canonical help:
  schema_version: 1; status: ok | partial | unknown | invalid
  counts_state: measured | partial | unknown (of supplied rows, NOT a live week)
  counts: routed_prs, expensive_reviews, matched_findings, unmatched_findings,
          invalid_decisions, invalid_findings, excluded_rows (nonnegative ints)
  metrics: expensive_review_share, findings_per_expensive_review,
           audit_sample_defects (unavailable ratios are null, never false zero)
  source: kind='supplied', ledger/findings paths, window_verified=false
  window: now, cutoff, promotion_days (promotion evidence window only)
  prs: [{id: 'owner/repo#N', repo, pr, expensive}]
  buckets / promotion_candidates: [{id: '<bucket>', count: N}]
  reason_counts: {actionable_error_code: excluded_row_count}
  errors: [{code: '<actionable_error_code>', ...}]
  retryable: bool; next_action: nonempty text
Exit 0 means a usable, syntactically valid supplied-data report, not verified
weekly completeness or savings. Bad supplied evidence exits nonzero and retains
valid-row metrics explicitly partial; an empty decision ledger is unknown.
CLI usage errors exit 2. Output wording beyond existing metric labels is free.

Mutation targets: accepting a bad route/identity before dedup; hash/sort before
bucket validation; truthy audit flags; joining by PR alone; last-decision-wins;
missing temporal upper bound; overflow before argument validation; fabricated
zero denominators; unqualified source claims; and a side effect during replay.
Intentional RED assertions on the old implementation are the deliverable.
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "review_routing_weekly.py"
NOW = "2026-10-09T00:00:00Z"
CUTOFF = "2026-07-11T00:00:00Z"

# This is a containment boundary, not a substitute report implementation. Audit
# hooks run BEFORE runpy loads the real entrypoint (and its local dependencies).
OFFLINE_BOOTSTRAP = r'''
import os, runpy, sys
root, entrypoint, *args = sys.argv[1:]
sys.path.insert(0, root)
def guard(event, args):
    forbidden = (
        event.startswith(('socket.', 'subprocess.', 'ctypes.', 'winreg.'))
        or event in {'os.system', 'os.posix_spawn', 'os.spawn', 'os.exec',
                     'os.fork', 'os.forkpty', 'os.startfile', 'os.kill',
                     'os.killpg', 'os.putenv', 'os.unsetenv', 'os.chdir',
                     'os.remove', 'os.rmdir', 'os.mkdir', 'os.rename',
                     'os.link', 'os.symlink', 'os.truncate', 'os.chmod',
                     'os.chown', 'os.utime', 'os.setxattr', 'os.removexattr',
                     'shutil.copyfile', 'shutil.copymode', 'shutil.copystat',
                     'shutil.rmtree'}
    )
    if event == 'open':
        _, mode, flags = args
        forbidden = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    if forbidden:
        sys.stderr.write('OFFLINE_GUARD: ' + event + '\n')
        raise PermissionError('offline acceptance forbids ' + event)
sys.addaudithook(guard)
sys.argv = [entrypoint, *args]
runpy.run_path(entrypoint, run_name='__main__')
'''


def decision(repo="a/x", pr=1, route="expensive", **fields):
    row = {"repo": repo, "pr": pr, "route": route, "ts": NOW}
    row.update(fields)
    return row


def finding(repo="a/x", pr=1, bucket="tier0-lint", **fields):
    row = {"repo": repo, "pr": pr, "bucket": bucket, "audit_only": False}
    row.update(fields)
    return row


class ReviewReportInputIntegrity(unittest.TestCase):
    maxDiff = 5000

    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
            "PAPERCLIP_SCRATCH_DIR") or os.environ.get("TMPDIR")
        self.tmp = tempfile.TemporaryDirectory(prefix="review-report-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.ledger = self.work / "decisions.jsonl"
        self.findings = self.work / "findings.jsonl"
        self.no_commands = self.work / "no-commands"
        self.no_commands.mkdir()
        self.env = {
            "PATH": str(self.no_commands), "HOME": str(self.work),
            "TMPDIR": str(self.work), "LC_ALL": "C", "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def write_rows(self, path, rows):
        path.write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in rows))

    def invoke(self, decisions=None, findings=None, *, options=(), raw_ledger=None,
               raw_findings=None, machine=False):
        self.write_rows(self.ledger, [decision()] if decisions is None else decisions)
        self.write_rows(self.findings, [] if findings is None else findings)
        if raw_ledger is not None:
            self.ledger.write_text(raw_ledger)
        if raw_findings is not None:
            self.findings.write_text(raw_findings)
        before = {p.name: p.read_bytes() for p in self.work.iterdir() if p.is_file()}
        args = ["--ledger", str(self.ledger), "--findings", str(self.findings),
                "--now", NOW, *options]
        if machine:
            args.append("--json")
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", OFFLINE_BOOTSTRAP,
                 str(ROOT), str(ENTRYPOINT), *args],
                cwd=self.work, env=self.env, text=True, capture_output=True,
                timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("report exceeded the bounded five-second offline deadline")
        self.assertNotIn("OFFLINE_GUARD:", result.stderr,
                         "report attempted a command, network access or mutation")
        after = {p.name: p.read_bytes() for p in self.work.iterdir() if p.is_file()}
        self.assertEqual(before, after, "offline report changed fixture files")
        self.assertNotIn("Traceback (most recent call last)", result.stderr,
                         "bad evidence/arguments must be controlled results, not tracebacks")
        return result

    def metric(self, result, name):
        matches = [line.split(":", 1)[1].strip() for line in result.stdout.splitlines()
                   if line.startswith(name + ":")]
        self.assertEqual(len(matches), 1, (name, result.stdout, result.stderr))
        return matches[0]

    def text_counts(self, result, routed, expensive):
        self.assertEqual(self.metric(result, "routed_prs"), str(routed))
        self.assertEqual(self.metric(result, "expensive_reviews"), str(expensive))

    def candidates(self, result):
        lines = result.stdout.splitlines()
        self.assertTrue(any(s.startswith("promotion_candidates") for s in lines),
                        result.stdout)
        active = False
        found = {}
        for line in lines:
            if line.startswith("promotion_candidates"):
                active = True
                continue
            if active and line.startswith("  "):
                name, count = line.strip().rsplit(":", 1)
                found[name] = int(count)
            elif active and line and not line.startswith(" "):
                active = False
        return found

    def machine(self, *args, **kwargs):
        result = self.invoke(*args, machine=True, **kwargs)
        try:
            data = json.loads(result.stdout, parse_constant=lambda v: self.fail(
                "non-finite JSON value: " + v))
        except json.JSONDecodeError:
            self.fail("--json must return one versioned JSON object, not text/usage: "
                      + repr((result.returncode, result.stdout, result.stderr)))
        self.assertIsInstance(data, dict)
        self.assertEqual(type(data.get("schema_version")), int)
        self.assertEqual(data["schema_version"], 1)
        self.assertIn(data.get("status"), {"ok", "partial", "unknown", "invalid"})
        self.assertIn(data.get("counts_state"), {"measured", "partial", "unknown"})
        self.assertIs(type(data.get("retryable")), bool)
        self.assertIsInstance(data.get("next_action"), str)
        self.assertTrue(data["next_action"].strip())
        for name in ("routed_prs", "expensive_reviews", "matched_findings",
                     "unmatched_findings", "invalid_decisions", "invalid_findings",
                     "excluded_rows"):
            self.assertIn(name, data.get("counts", {}))
            self.assertIs(type(data["counts"][name]), int, name)
            self.assertGreaterEqual(data["counts"][name], 0, name)
        self.assertIsInstance(data.get("errors"), list)
        self.assertIsInstance(data.get("reason_counts"), dict)
        for error in data["errors"]:
            self.assertIsInstance(error.get("code"), str)
            self.assertTrue(error["code"].strip())
        for code, count in data["reason_counts"].items():
            self.assertTrue(code)
            self.assertIs(type(count), int)
            self.assertGreaterEqual(count, 0)
        return result, data

    def assert_partial(self, result, data, code, invalid_decisions=0, invalid_findings=0):
        self.assertNotEqual(result.returncode, 0, "malformed evidence cannot exit clean")
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["counts_state"], "partial")
        self.assertEqual(data["counts"]["invalid_decisions"], invalid_decisions)
        self.assertEqual(data["counts"]["invalid_findings"], invalid_findings)
        self.assertEqual(data["counts"]["excluded_rows"],
                         invalid_decisions + invalid_findings)
        self.assertEqual(data["reason_counts"].get(code),
                         invalid_decisions + invalid_findings)
        self.assertIn(code, [error["code"] for error in data["errors"]])
        self.assertFalse(data["retryable"], "unchanged malformed input is not retryable")

    # Positive controls: these must remain green on the current implementation.
    def test_valid_emitted_route_vocabulary_preserves_metrics(self):
        result = self.invoke([decision(route="standard", reasons=[], audit_sample="no"),
                              decision(pr=2, reasons="audit-sample", audit_sample="yes")],
                             [finding(pr=2, audit_only=True)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.text_counts(result, 2, 1)
        self.assertEqual(self.metric(result, "expensive_review_share"), "0.500")
        self.assertEqual(self.metric(result, "findings_per_expensive_review"), "1.00")
        self.assertEqual(self.metric(result, "audit_sample_defects"), "1")

    def test_repo_qualified_dedup_and_any_expensive_decision_wins(self):
        rows = [decision(), decision(route="standard"), decision(),
                decision(repo="b/y", route="standard"), decision(pr=2)]
        for ordered in (rows, list(reversed(rows))):
            with self.subTest(order=ordered):
                result = self.invoke(ordered)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.text_counts(result, 3, 2)
                self.assertEqual(self.metric(result, "expensive_review_share"), "0.667")

    def test_original_repo_and_expensive_review_joins_are_preserved(self):
        result = self.invoke([decision(), decision(repo="b/y", route="standard")],
                             [finding(repo="b/y", audit_only=True)] * 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metric(result, "findings_per_expensive_review"), "0.00")
        self.assertEqual(self.metric(result, "audit_sample_defects"), "0")
        # Histogram/promotion remains all valid findings, not a new join policy.
        self.assertEqual(self.candidates(result), {"tier0-lint": 3})

    def test_missing_bucket_intentionally_falls_back_to_other(self):
        row = finding()
        del row["bucket"]
        result = self.invoke(findings=[row])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  other: 1", result.stdout)
        self.assertEqual(self.candidates(result), {})

    def test_undated_findings_fall_back_to_decision_date(self):
        for ts, expected in ((NOW, {"tier0-lint": 3}),
                             ("2026-06-01T00:00:00Z", {})):
            with self.subTest(ts=ts):
                result = self.invoke([decision(ts=ts)], [finding()] * 3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.candidates(result), expected)

    def test_truly_undated_findings_retain_conservative_promotion_fallback(self):
        row = decision()
        del row["ts"]
        result = self.invoke([row], [finding()] * 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.candidates(result), {"tier0-lint": 3})

    def test_cutoff_now_and_offset_equivalent_boundaries_are_inclusive(self):
        for ts in (CUTOFF, NOW, "2026-10-09T02:00:00+02:00"):
            with self.subTest(ts=ts):
                result = self.invoke(findings=[finding(ts=ts)] * 3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.candidates(result), {"tier0-lint": 3})

    def test_before_cutoff_findings_do_not_promote_but_wider_window_still_does(self):
        rows = [finding(ts="2026-07-10T23:59:59Z")] * 3
        result = self.invoke(findings=rows)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.candidates(result), {})
        widened = self.invoke(findings=rows, options=("--promotion-days", "200"))
        self.assertEqual(widened.returncode, 0, widened.stderr)
        self.assertEqual(self.candidates(widened), {"tier0-lint": 3})

    def test_one_recent_plus_two_old_findings_do_not_meet_threshold(self):
        result = self.invoke(findings=[finding(ts=NOW),
                                      finding(ts="2026-01-01T00:00:00Z"),
                                      finding(ts="2026-01-02T00:00:00Z")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.candidates(result), {})

    def test_nonpromotable_bucket_never_becomes_a_rule_candidate(self):
        result = self.invoke(findings=[finding(bucket="security-boundary")] * 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.candidates(result), {})

    # Text-level defects are checked independently of the missing JSON feature.
    def test_missing_or_unknown_routes_are_excluded_not_silently_standard(self):
        for route in ("nonsense", "standrad", "", None, [], {}, 1, True, "MISSING"):
            with self.subTest(route=route):
                bad = decision(pr=2, route=route)
                if route == "MISSING":
                    del bad["route"]
                result = self.invoke([decision(), bad])
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.text_counts(result, 1, 1)
                self.assertRegex(result.stdout + result.stderr, r"(?i)partial|invalid")

    def test_nonpositive_boolean_and_wrong_type_prs_are_invalid_evidence(self):
        for pr in (0, -1, True, False, 1.0, "1", None, [], {}):
            with self.subTest(pr=pr):
                result = self.invoke([decision(), decision(repo="b/y", pr=pr)])
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.text_counts(result, 1, 1)

    def test_repo_must_be_a_positive_repo_qualified_identity(self):
        for repo in ("", "a", "a/", "/x", "a/x/y", " a/x", "a/x\n", None, [], True):
            with self.subTest(repo=repo):
                result = self.invoke([decision(), decision(repo=repo, pr=2)])
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.text_counts(result, 1, 1)

    def test_bucket_types_cannot_crash_or_contaminate_statistics(self):
        for bucket in ([], {}, None, 1, True, ["tier0-lint"], ""):
            with self.subTest(bucket=bucket):
                result = self.invoke(findings=[finding(), finding(bucket=bucket)])
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.metric(result, "findings_per_expensive_review"), "1.00")
                self.assertIn("  tier0-lint: 1", result.stdout)
                self.assertEqual(self.candidates(result), {})

    def test_invalid_audit_types_are_excluded_not_treated_as_false_or_truthy(self):
        for audit in ("true", "false", 0, 1, [], {}, None):
            with self.subTest(audit=audit):
                result = self.invoke(findings=[finding(audit_only=True),
                                              finding(audit_only=audit)])
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.metric(result, "findings_per_expensive_review"), "1.00")
                self.assertEqual(self.metric(result, "audit_sample_defects"), "1")

    def test_explicit_bad_dates_do_not_masquerade_as_undated_fallback(self):
        for ts in ("not-a-date", "", None, 7, [], {}):
            with self.subTest(ts=ts):
                result = self.invoke(findings=[finding(ts=ts)] * 3)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.candidates(result), {})

    def test_future_findings_never_trigger_last_n_days_candidates(self):
        for ts in ("2099-01-01T00:00:00Z", "2026-10-09T00:00:01Z",
                   "2026-10-09T02:00:01+02:00"):
            with self.subTest(ts=ts):
                result = self.invoke(findings=[finding(ts=ts)] * 3)
                self.assertEqual(self.candidates(result), {})

    def test_future_decision_fallback_cannot_promote_undated_findings(self):
        result = self.invoke([decision(ts="2099-01-01T00:00:00Z")], [finding()] * 3)
        self.assertEqual(self.candidates(result), {})

    def test_two_current_plus_one_future_finding_do_not_reach_threshold(self):
        result = self.invoke(findings=[finding(ts=NOW), finding(ts=CUTOFF),
                                      finding(ts="2099-01-01T00:00:00Z")])
        self.assertEqual(self.candidates(result), {})

    def test_bad_promotion_days_are_prompt_usage_errors_without_overflow(self):
        for days in ("0", "-1", "no", "1.5", "1000000000000", "9" * 5000):
            with self.subTest(days=days[:30]):
                result = self.invoke(options=("--promotion-days", days))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn("promotion", result.stderr.lower())

    def test_bad_now_and_unrepresentable_cutoff_are_controlled_usage_errors(self):
        for now in ("not-a-date", "", "99999-01-01T00:00:00Z", "0001-01-01T00:00:00Z"):
            with self.subTest(now=now):
                result = self.invoke(options=("--now", now))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))

    def test_unparsable_and_non_object_rows_make_text_report_explicitly_partial(self):
        for tail in ("{broken\n", "[]\n", "null\n", "42\n"):
            with self.subTest(tail=tail):
                result = self.invoke(raw_ledger=json.dumps(decision()) + "\n" + tail)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.text_counts(result, 1, 1)
                self.assertRegex(result.stdout + result.stderr, r"(?i)partial|invalid")

    # Machine consumers need explicit coverage, stable identity and recovery.
    def test_json_preserves_valid_metrics_ids_and_unverified_input_provenance(self):
        result, data = self.machine(
            [decision(), decision(repo="b/y", route="standard")],
            [finding(audit_only=True), finding(repo="b/y"), finding(repo="c/z")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["counts_state"], "measured")
        expected = {"routed_prs": 2, "expensive_reviews": 1, "matched_findings": 1,
                    "unmatched_findings": 2, "invalid_decisions": 0,
                    "invalid_findings": 0, "excluded_rows": 0}
        for name, value in expected.items():
            self.assertEqual(data["counts"][name], value, name)
        self.assertEqual(data["metrics"]["expensive_review_share"], 0.5)
        self.assertEqual(data["metrics"]["findings_per_expensive_review"], 1.0)
        self.assertEqual(data["metrics"]["audit_sample_defects"], 1)
        self.assertEqual(data["source"]["kind"], "supplied")
        self.assertEqual(data["source"]["ledger"], str(self.ledger))
        self.assertEqual(data["source"]["findings"], str(self.findings))
        self.assertIs(data["source"]["window_verified"], False)
        self.assertEqual(data["window"]["promotion_days"], 90)
        for name, expected_ts in (("now", NOW), ("cutoff", CUTOFF)):
            ts = datetime.datetime.fromisoformat(data["window"][name].replace("Z", "+00:00"))
            self.assertEqual(ts, datetime.datetime.fromisoformat(expected_ts.replace("Z", "+00:00")))
        self.assertEqual([(p["id"], p["repo"], p["pr"], p["expensive"]) for p in data["prs"]],
                         [("a/x#1", "a/x", 1, True), ("b/y#1", "b/y", 1, False)])
        self.assertEqual([(b["id"], b["count"]) for b in data["buckets"]],
                         [("tier0-lint", 3)])
        self.assertEqual([(b["id"], b["count"]) for b in data["promotion_candidates"]],
                         [("tier0-lint", 3)])
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["reason_counts"], {})

    def test_json_route_exclusion_is_counted_with_actionable_reason(self):
        result, data = self.machine([decision(), decision(pr=2, route="nonsense")])
        self.assert_partial(result, data, "invalid_route", invalid_decisions=1)
        self.assertEqual(data["counts"]["routed_prs"], 1)
        self.assertEqual(data["counts"]["expensive_reviews"], 1)
        self.assertEqual(data["metrics"]["expensive_review_share"], 1.0)

    def test_json_identity_exclusion_does_not_fabricate_a_second_pr(self):
        for fields, code in (({"pr": 0}, "invalid_pr"),
                             ({"repo": "unqualified"}, "invalid_repo")):
            with self.subTest(fields=fields):
                result, data = self.machine([decision(), decision(**fields)])
                self.assert_partial(result, data, code, invalid_decisions=1)
                self.assertEqual(data["counts"]["routed_prs"], 1)

    def test_json_bad_fields_are_validated_before_dedup_date_or_audit_arithmetic(self):
        for fields in ({"ts": []}, {"reasons": {}}, {"reasons": ["size>1000", 1]},
                       {"audit_sample": True}):
            with self.subTest(fields=fields):
                result, data = self.machine([decision(route="standard"), decision(**fields)])
                self.assert_partial(result, data, "invalid_field", invalid_decisions=1)
                self.assertEqual(data["counts"]["routed_prs"], 1)
                self.assertEqual(data["counts"]["expensive_reviews"], 0)

    def test_json_finding_coverage_separates_matched_unmatched_and_invalid(self):
        for fields, code in (({"bucket": []}, "invalid_bucket"),
                             ({"bucket": None}, "invalid_bucket"),
                             ({"bucket": 2}, "invalid_bucket"),
                             ({"audit_only": "true"}, "invalid_audit")):
            with self.subTest(fields=fields):
                result, data = self.machine(findings=[finding(), finding(repo="b/y"),
                                                      finding(**fields)])
                self.assert_partial(result, data, code, invalid_findings=1)
                self.assertEqual(data["counts"]["matched_findings"], 1)
                self.assertEqual(data["counts"]["unmatched_findings"], 1)
                self.assertEqual(data["metrics"]["findings_per_expensive_review"], 1.0)
                self.assertEqual([(b["id"], b["count"]) for b in data["buckets"]],
                                 [("tier0-lint", 2)])

    def test_json_invalid_finding_identity_is_reported_not_silently_skipped(self):
        result, data = self.machine(findings=[finding(), finding(pr=True)])
        self.assert_partial(result, data, "invalid_pr", invalid_findings=1)
        self.assertEqual(data["counts"]["matched_findings"], 1)

    def test_json_parse_failures_and_non_objects_have_reason_counts(self):
        for source in ("ledger", "findings"):
            for tail, code in (("{broken\n", "invalid_json"), ("[]\n", "non_object"),
                               ("null\n", "non_object")):
                with self.subTest(source=source, code=code, tail=tail):
                    raw = json.dumps(decision() if source == "ledger" else finding()) + "\n" + tail
                    result, data = self.machine(**{"raw_" + source: raw})
                    self.assert_partial(result, data, code,
                                        invalid_decisions=int(source == "ledger"),
                                        invalid_findings=int(source == "findings"))

    def test_empty_ledger_is_unknown_not_a_verified_zero_spend_week(self):
        result, data = self.machine([])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "unknown")
        self.assertEqual(data["counts_state"], "unknown")
        self.assertEqual(data["counts"]["routed_prs"], 0)
        self.assertIsNone(data["metrics"]["expensive_review_share"])
        self.assertIsNone(data["metrics"]["findings_per_expensive_review"])
        self.assertIs(data["source"]["window_verified"], False)
        self.assertIn("empty_ledger", [e["code"] for e in data["errors"]])

    def test_all_invalid_ledger_never_returns_a_clean_zero(self):
        result, data = self.machine([decision(route="nonsense")])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(data["status"], {"partial", "invalid", "unknown"})
        self.assertNotEqual(data["counts_state"], "measured")
        self.assertEqual(data["counts"]["invalid_decisions"], 1)
        self.assertEqual(data["counts"]["routed_prs"], 0)
        self.assertIsNone(data["metrics"]["expensive_review_share"])
        self.assertIn("invalid_route", [e["code"] for e in data["errors"]])

    def test_missing_supplied_file_is_structured_not_a_traceback(self):
        missing = str(self.work / "not-present.jsonl")
        result, data = self.machine(options=("--ledger", missing))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(data["status"], {"invalid", "unknown", "partial"})
        self.assertIn("missing_input", [e["code"] for e in data["errors"]])

    def test_json_replay_and_ordered_ids_are_deterministic(self):
        rows = [decision(repo="b/y"), decision(), decision(route="standard")]
        finds = [finding(bucket="tier1-docs-diff"), finding(), finding()]
        _, first = self.machine(rows, finds)
        _, replay = self.machine(rows, finds)
        _, reordered = self.machine(list(reversed(rows)), list(reversed(finds)))
        self.assertEqual(first, replay, "--now replay must not embed current wall clock")
        self.assertEqual(first, reordered, "IDs/counts must not depend on input ordering")

    def test_dry_run_is_identical_offline_read_only_replay(self):
        normal = self.invoke(findings=[finding()] * 3)
        dry = self.invoke(findings=[finding()] * 3, options=("--dry-run",))
        self.assertEqual(dry.returncode, normal.returncode, dry.stderr)
        self.assertEqual(dry.stdout, normal.stdout)
        _, normal_json = self.machine(findings=[finding()] * 3)
        _, dry_json = self.machine(findings=[finding()] * 3, options=("--dry-run",))
        self.assertEqual(dry_json, normal_json)

    def test_invalid_flags_never_invoke_commands_network_or_rule_writers(self):
        for options in (("--apply",), ("--promotion-days",),
                        ("--dry-run", "--apply"), ("--now", "bad")):
            with self.subTest(options=options):
                result = self.invoke(options=options)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_json_usage_errors_are_actionable_and_not_unstructured_usage(self):
        for options in (("--apply",), ("--promotion-days", "-1"), ("--now", "bad")):
            with self.subTest(options=options):
                result, data = self.machine(options=options)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(data["status"], "invalid")
                self.assertIn("invalid_argument", [e["code"] for e in data["errors"]])
                self.assertFalse(data["retryable"])

    def test_help_makes_offline_check_and_advisory_json_interface_discoverable(self):
        result = self.invoke(options=("--help",))
        self.assertEqual(result.returncode, 0, result.stderr)
        for token in ("--json", "--dry-run", "test_review_report_input_integrity"):
            self.assertIn(token, result.stdout)
        self.assertRegex(result.stdout.lower(), r"advisory|analysis.only|not.*gate")


if __name__ == "__main__":
    unittest.main()
