"""Immutable, fixture-only acceptance contract for opt-in repository totals.

Run:
  python3 -B -m unittest -v test_review_report_input_integrity \
      test_review_report_empty_ledger test_review_repository_breakdown_acceptance

Required additive JSON subset (extra aggregate fields are allowed):
  repository_breakdown: {
    schema_version: 1,
    status: ok | partial | not_evaluated,
    counts_state: measured | partial | unknown,  # inherited global state
    unattributed_counts: {invalid_decisions, invalid_findings, excluded_rows},
    repositories: [{repo: 'owner/name',
      counts: {routed_prs, expensive_reviews, matched_findings, unmatched_findings},
      metrics: {expensive_review_share, findings_per_expensive_review,
                audit_sample_defects},
      buckets: [{id: '<validated bucket>', count: N}]}]
  }

Repositories and bucket IDs sort ascending, with one row per validated repo.
Counts use the existing semantics: decisions dedup by (repo, pr), any expensive
wins, findings are NOT deduped, and only expensive-routed findings match.
Ratios use each repository's denominators (null at zero). Buckets include all
valid findings, including unmatched ones, and missing bucket means 'other'.
Invalid rows belong only to global unattributed_counts, even when their repo
field looks valid. A partial global cohort makes the entire breakdown partial,
not a completeness claim for unaffected slices. Empty-decision early returns,
missing inputs and usage errors are not_evaluated with no invented repo rows;
the existing global output and exits must survive unchanged.

The default v1 schema/text and main() callable remain compatible. New fields
must not echo raw evidence or excluded identities. Existing validated global
prs/buckets identities are deliberately NOT subject to that no-echo rule.
--by-repo requires --json; --dry-run is still the same read-only alias.

Mutation targets: joining by PR number alone; last-decision-wins; counting raw
rather than deduped decisions; deduping repeated findings; matching standard
routes; dropping findings-only repos; averaging shares; guessing invalid-row
repos; hiding partial/unknown state; fabricating empty-cohort metrics; leaking
notes/rejected identities; and changing global promotion/window/default output.

All reports run in isolated bounded Python children with no credentials, empty
command PATH, and the existing immutable input-integrity audit guard. No report
implementation, router, collector, network, or board mutation is mocked or run.
"""
import collections
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_review_report_input_integrity as containment


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "review_routing_weekly.py"
NOW = "2026-10-09T00:00:00Z"
RAW_MARKER = "SYNTHETIC_RAW_BODY_DO_NOT_DISCLOSE"
REJECTED_REPO = "rejected/evidence-only"
REJECTED_PR = 987654321
DEFAULT_KEYS = {
    "schema_version", "status", "counts_state", "counts", "metrics", "source",
    "window", "prs", "buckets", "promotion_candidates", "reason_counts",
    "errors", "retryable", "next_action",
}
COUNT_KEYS = ("routed_prs", "expensive_reviews", "matched_findings",
              "unmatched_findings")


def decision(repo="a/alpha", pr=7, route="expensive", **fields):
    return {"repo": repo, "pr": pr, "route": route, "ts": NOW, **fields}


def finding(repo="a/alpha", pr=7, bucket="tier0-lint", audit_only=False, **fields):
    return {"repo": repo, "pr": pr, "bucket": bucket,
            "audit_only": audit_only, **fields}


def cohort():
    decisions = [
        decision(), decision(route="standard"), decision(),
        decision(pr=8, route="standard"), decision(pr=10),
        decision(repo="b/beta", route="standard"),
        decision(repo="b/beta", pr=9), decision(repo="b/beta", pr=9),
        decision(repo="c/clear", pr=11, route="standard"),
        decision(repo="c/clear", pr=12, route="standard"),
    ]
    findings = [
        finding(audit_only=True), finding(audit_only=True),
        finding(pr=10, bucket="tier1-docs-diff"),
        finding(pr=8, audit_only=True),
        finding(pr=99, bucket="security-boundary", audit_only=True),
        finding(repo="b/beta", bucket="tier1-docs-diff", audit_only=True),
        finding(repo="b/beta", pr=9, bucket="tier0-tests", audit_only=True),
        {"repo": "b/beta", "pr": 9},  # Deliberate missing-bucket fallback.
        finding(repo="d/orphan", audit_only=True),
    ]
    return decisions, findings


class OfflineReportCase(unittest.TestCase):
    maxDiff = 6000

    def setUp(self):
        scratch = (os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
                   or os.environ.get("PAPERCLIP_SCRATCH_DIR")
                   or os.environ.get("TMPDIR"))
        self.tmp = tempfile.TemporaryDirectory(prefix="repo-breakdown-", dir=scratch)
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

    def supply(self, decisions, findings=(), *, ledger_tail="", findings_tail=""):
        for path, rows, tail in ((self.ledger, decisions, ledger_tail),
                                 (self.findings, findings, findings_tail)):
            path.write_text("".join(json.dumps(r, allow_nan=False) + "\n"
                                    for r in rows) + tail, encoding="utf-8")

    def invoke(self, *options, callable_main=False, omit_findings=False):
        # Snapshot all fixture content and directory entries, including new files.
        before = {str(p.relative_to(self.work)):
                  p.read_bytes() if p.is_file() else None
                  for p in self.work.rglob("*")}
        bootstrap = containment.OFFLINE_BOOTSTRAP
        if callable_main:
            bootstrap = bootstrap.replace(
                "runpy.run_path(entrypoint, run_name='__main__')",
                "namespace = runpy.run_path(entrypoint)\n"
                "sys.exit(namespace['main']())")
        args = ["--ledger", str(self.ledger), "--now", NOW]
        if not omit_findings:
            args.extend(["--findings", str(self.findings)])
        args.extend(options)
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", bootstrap,
                 str(ROOT), str(ENTRYPOINT), *args],
                cwd=self.work, env=self.env, capture_output=True, text=True,
                timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("report exceeded the five-second offline deadline")
        self.assertNotIn("OFFLINE_GUARD:", result.stderr)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)
        after = {str(p.relative_to(self.work)):
                 p.read_bytes() if p.is_file() else None
                 for p in self.work.rglob("*")}
        self.assertEqual(before, after, "report mutated synthetic input/workspace")
        return result

    def machine(self, *options, callable_main=False, omit_findings=False):
        result = self.invoke("--json", *options, callable_main=callable_main,
                             omit_findings=omit_findings)
        try:
            data = json.loads(result.stdout, parse_constant=lambda v: self.fail(
                "non-finite JSON value: " + v))
        except json.JSONDecodeError:
            self.fail("expected one JSON report: "
                      + repr((result.returncode, result.stdout, result.stderr)))
        self.assertIsInstance(data, dict)
        return result, data

    def projected(self, *options, omit_findings=False):
        baseline_result, baseline = self.machine(*options, omit_findings=omit_findings)
        result, data = self.machine("--by-repo", *options, omit_findings=omit_findings)
        self.assertEqual(result.returncode, baseline_result.returncode,
                         "opt-in must preserve global success/failure exit")
        self.assertIn("repository_breakdown", data,
                      "missing opt-in repository_breakdown projection")
        section = data["repository_breakdown"]
        self.assertIsInstance(section, dict)
        self.assertIs(type(section.get("schema_version")), int)
        self.assertEqual(section["schema_version"], 1)
        self.assertEqual({k: v for k, v in data.items() if k != "repository_breakdown"},
                         baseline, "opt-in changed the existing global JSON projection")
        self.assertEqual(section.get("counts_state"), baseline["counts_state"])
        self.assertEqual(section.get("unattributed_counts"), {
            k: baseline["counts"][k]
            for k in ("invalid_decisions", "invalid_findings", "excluded_rows")
        })
        self.assertIsInstance(section.get("repositories"), list)
        return result, data, section

    def buckets(self, rows):
        self.assertIsInstance(rows, list)
        ids = [row["id"] for row in rows]
        self.assertEqual(ids, sorted(set(ids)), "bucket IDs must be sorted and unique")
        for row in rows:
            self.assertIs(type(row["count"]), int)
            self.assertGreater(row["count"], 0)
        return {row["id"]: row["count"] for row in rows}

    def reconcile(self, data, section):
        rows = section["repositories"]
        repos = [row["repo"] for row in rows]
        self.assertEqual(repos, sorted(set(repos)), "repo rows must be sorted and unique")
        for name in COUNT_KEYS:
            for row in rows:
                self.assertIs(type(row["counts"][name]), int)
                self.assertGreaterEqual(row["counts"][name], 0)
            self.assertEqual(sum(row["counts"][name] for row in rows),
                             data["counts"][name], name)
        self.assertEqual(sum(row["metrics"]["audit_sample_defects"] for row in rows),
                         data["metrics"]["audit_sample_defects"])
        combined = collections.Counter()
        for row in rows:
            combined.update(self.buckets(row["buckets"]))
            counts, metrics = row["counts"], row["metrics"]
            self.assertIs(type(metrics["audit_sample_defects"]), int)
            self.assertGreaterEqual(metrics["audit_sample_defects"], 0)
            self.assertLessEqual(metrics["audit_sample_defects"], counts["matched_findings"])
            self.assertEqual(sum(b["count"] for b in row["buckets"]),
                             counts["matched_findings"] + counts["unmatched_findings"])
            for name, numerator, denominator in (
                    ("expensive_review_share", counts["expensive_reviews"], counts["routed_prs"]),
                    ("findings_per_expensive_review", counts["matched_findings"], counts["expensive_reviews"])):
                if denominator:
                    self.assertIn(type(metrics[name]), (int, float))
                    self.assertAlmostEqual(metrics[name], numerator / denominator)
                else:
                    self.assertIsNone(metrics[name])
            # Optional per-row state must never contradict inherited partiality.
            if "counts_state" in row:
                self.assertEqual(row["counts_state"], section["counts_state"])
            if "status" in row:
                self.assertEqual(row["status"], section["status"])
        self.assertEqual(dict(combined), self.buckets(data["buckets"]))


class CurrentBehaviorControls(OfflineReportCase):
    """GREEN controls distinguish a missing feature from broken prerequisites."""

    def test_default_json_schema_and_hand_counted_global_metrics(self):
        self.supply(*cohort())
        result, data = self.machine()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(data), DEFAULT_KEYS)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual((data["status"], data["counts_state"]), ("ok", "measured"))
        self.assertEqual(data["counts"], {
            "routed_prs": 7, "expensive_reviews": 3, "matched_findings": 5,
            "unmatched_findings": 4, "invalid_decisions": 0,
            "invalid_findings": 0, "excluded_rows": 0,
        })
        self.assertEqual(data["metrics"], {
            "expensive_review_share": 3 / 7, "findings_per_expensive_review": 5 / 3,
            "audit_sample_defects": 3,
        })
        self.assertEqual(self.buckets(data["buckets"]), {
            "tier0-lint": 4, "tier1-docs-diff": 2, "tier0-tests": 1,
            "security-boundary": 1, "other": 1,
        })
        self.assertEqual(data["promotion_candidates"], [{"id": "tier0-lint", "count": 4}])
        self.assertEqual([p["id"] for p in data["prs"]], [
            "a/alpha#7", "a/alpha#8", "a/alpha#10", "b/beta#7", "b/beta#9",
            "c/clear#11", "c/clear#12",
        ])
        self.assertEqual(data["source"]["kind"], "supplied")
        self.assertIs(data["source"]["window_verified"], False)

    def test_default_text_and_dry_run_remain_byte_identical(self):
        self.supply(*cohort())
        normal = self.invoke()
        dry = self.invoke("--dry-run")
        self.assertEqual((normal.returncode, normal.stdout, normal.stderr),
                         (dry.returncode, dry.stdout, dry.stderr))
        self.assertEqual(normal.returncode, 0)
        self.assertEqual(normal.stdout, "\n".join([
            "routed_prs: 7", "expensive_reviews: 3", "expensive_review_share: 0.429",
            "findings_per_expensive_review: 1.67", "audit_sample_defects: 3",
            "bucket_histogram:", "  other: 1", "  security-boundary: 1",
            "  tier0-lint: 4", "  tier0-tests: 1", "  tier1-docs-diff: 2",
            "promotion_candidates (>=3 in 90d, becomes Semgrep/lint rule or Tier-1 prompt):",
            "  tier0-lint: 4", "",
        ]))
        self.assertEqual(self.machine()[1], self.machine("--dry-run")[1])

    def test_existing_main_callable_keeps_output_and_return_code(self):
        for rows in ([decision()], []):
            with self.subTest(empty=not rows):
                self.supply(rows, [finding()])
                cli, cli_data = self.machine()
                called, called_data = self.machine(callable_main=True)
                self.assertEqual(cli.returncode, called.returncode)
                self.assertEqual(cli_data, called_data)

    def test_invalid_rows_and_empty_cohort_are_controlled_nonzero_results(self):
        self.supply([decision(), decision(route="unknown")], [finding(pr=True)])
        result, data = self.machine()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((data["status"], data["counts_state"]), ("partial", "partial"))
        self.assertEqual(data["counts"]["excluded_rows"], 2)
        self.supply([], [finding(), finding(pr=True)])
        result, data = self.machine()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((data["status"], data["counts_state"]), ("unknown", "unknown"))
        self.assertEqual(data["counts"]["unmatched_findings"], 0)
        self.assertEqual(data["buckets"], [])
        self.assertEqual(data["counts"]["invalid_findings"], 1)


class RepositoryBreakdownAcceptance(OfflineReportCase):
    def test_union_counts_buckets_and_weighted_denominators_reconcile(self):
        self.supply(*cohort())
        result, data, section = self.projected()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(section["status"], "ok")
        self.reconcile(data, section)
        rows = section["repositories"]
        self.assertEqual([r["repo"] for r in rows],
                         ["a/alpha", "b/beta", "c/clear", "d/orphan"])
        expected = [
            ((3, 2, 3, 2), (2 / 3, 1.5, 2),
             {"tier0-lint": 3, "tier1-docs-diff": 1, "security-boundary": 1}),
            ((2, 1, 2, 1), (0.5, 2.0, 1),
             {"tier1-docs-diff": 1, "tier0-tests": 1, "other": 1}),
            ((2, 0, 0, 0), (0.0, None, 0), {}),
            ((0, 0, 0, 1), (None, None, 0), {"tier0-lint": 1}),
        ]
        for row, (counts, metrics, buckets) in zip(rows, expected):
            with self.subTest(repo=row["repo"]):
                self.assertEqual(tuple(row["counts"][key] for key in COUNT_KEYS), counts)
                self.assertEqual(tuple(row["metrics"][key] for key in (
                    "expensive_review_share", "findings_per_expensive_review",
                    "audit_sample_defects")), metrics)
                self.assertEqual(self.buckets(row["buckets"]), buckets)

    def test_replay_and_input_permutations_do_not_change_projection(self):
        decisions, findings = cohort()
        self.supply(decisions, findings)
        result, first, _ = self.projected()
        replay, second, _ = self.projected()
        self.assertEqual((result.stdout, result.stderr), (replay.stdout, replay.stderr))
        self.assertEqual(first, second)
        for ds, fs in ((list(reversed(decisions)), list(reversed(findings))),
                       (decisions[3:] + decisions[:3], findings[2:] + findings[:2])):
            with self.subTest(permutation=ds[0]["repo"]):
                self.supply(ds, fs)
                _, reordered, _ = self.projected()
                self.assertEqual(first, reordered)

    def test_standard_route_findings_and_audit_flags_never_become_matched(self):
        self.supply([decision(route="standard")], [finding(audit_only=True)] * 3)
        _, data, section = self.projected()
        self.reconcile(data, section)
        row, = section["repositories"]
        self.assertEqual(tuple(row["counts"][k] for k in COUNT_KEYS), (1, 0, 0, 3))
        for key, value in {"expensive_review_share": 0.0,
                           "findings_per_expensive_review": None,
                           "audit_sample_defects": 0}.items():
            self.assertEqual(row["metrics"][key], value)
        self.assertEqual(data["promotion_candidates"], [{"id": "tier0-lint", "count": 3}])

    def test_no_findings_still_emits_decision_only_repositories(self):
        self.supply([decision(), decision(repo="b/beta", route="standard")])
        _, data, section = self.projected()
        self.reconcile(data, section)
        self.assertEqual([r["repo"] for r in section["repositories"]], ["a/alpha", "b/beta"])
        self.assertEqual([r["metrics"]["findings_per_expensive_review"]
                          for r in section["repositories"]], [0.0, None])
        self.assertEqual([r["buckets"] for r in section["repositories"]], [[], []])
        # Omitted findings is also a valid no-findings input, not missing evidence.
        _, omitted, omitted_section = self.projected(omit_findings=True)
        self.assertIs(omitted["source"]["window_verified"], False)
        self.assertEqual(section, omitted_section)

    def test_invalid_parse_and_content_rows_stay_globally_unattributed(self):
        decisions, findings = cohort()
        decisions += [decision(repo=REJECTED_REPO, pr=REJECTED_PR, route="wrong"),
                      decision(pr=8, route="expensive", reasons={"note": RAW_MARKER})]
        findings += [finding(repo=REJECTED_REPO, pr=REJECTED_PR, bucket=[]),
                     finding(pr=True, notes=RAW_MARKER)]
        self.supply(decisions, findings,
                    ledger_tail='{bad ' + RAW_MARKER + '\n[]\n',
                    findings_tail='{"repo": "' + REJECTED_REPO + '\nnull\n')
        result, data, section = self.projected()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((data["status"], section["status"]), ("partial", "partial"))
        self.assertEqual(section["counts_state"], "partial")
        self.assertEqual(section["unattributed_counts"], {
            "invalid_decisions": 4, "invalid_findings": 4, "excluded_rows": 8,
        })
        self.assertEqual(data["reason_counts"], {
            "invalid_route": 1, "invalid_field": 1, "invalid_bucket": 1,
            "invalid_pr": 1, "invalid_json": 2, "non_object": 2,
        })
        self.reconcile(data, section)
        self.assertEqual([r["repo"] for r in section["repositories"]],
                         ["a/alpha", "b/beta", "c/clear", "d/orphan"])
        alpha = section["repositories"][0]
        self.assertEqual(alpha["counts"]["expensive_reviews"], 2,
                         "invalid duplicate cannot promote a standard route")
        encoded = json.dumps(section)
        for forbidden in (RAW_MARKER, REJECTED_REPO, str(REJECTED_PR)):
            self.assertNotIn(forbidden, encoded)

    def test_a_single_excluded_row_cannot_claim_clean_slices(self):
        for source in ("decision", "finding"):
            with self.subTest(source=source):
                decisions, findings = cohort()
                if source == "decision":
                    decisions.append(decision(repo=REJECTED_REPO, route="unknown"))
                else:
                    findings.append(finding(repo=REJECTED_REPO, audit_only="true"))
                self.supply(decisions, findings)
                result, data, section = self.projected()
                self.assertEqual(result.returncode, 1)
                self.assertEqual((section["status"], section["counts_state"]),
                                 ("partial", "partial"))
                self.assertEqual(section["unattributed_counts"]["excluded_rows"], 1)
                self.assertNotIn(REJECTED_REPO, json.dumps(section))
                self.reconcile(data, section)

    def test_empty_decisions_keep_early_return_counts_without_invented_rows(self):
        for findings, tail, invalid in (([], "", 0), ([finding()], "", 0),
                                        ([finding(), finding(pr=True)], "", 1),
                                        ([], "{bad\n", 1)):
            with self.subTest(findings=findings, tail=tail):
                self.supply([], findings, findings_tail=tail)
                result, data, section = self.projected()
                self.assertEqual(result.returncode, 1)
                self.assertEqual((data["status"], data["counts_state"]), ("unknown", "unknown"))
                self.assertEqual(section["status"], "not_evaluated")
                self.assertEqual(section["repositories"], [])
                self.assertEqual(data["counts"]["matched_findings"], 0)
                self.assertEqual(data["counts"]["unmatched_findings"], 0)
                self.assertEqual(data["counts"]["invalid_findings"], invalid)
                self.assertEqual(data["buckets"], [])
                self.assertIsNone(data["metrics"]["expensive_review_share"])
                self.assertIsNone(data["metrics"]["findings_per_expensive_review"])

    def test_all_invalid_decisions_never_claim_a_measured_zero_cohort(self):
        self.supply([decision(route="wrong")], [finding(repo="d/orphan")])
        result, data, section = self.projected()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((data["status"], data["counts_state"]), ("partial", "partial"))
        self.assertIn(section["status"], {"partial", "not_evaluated"})
        self.assertEqual(section["counts_state"], "partial")
        self.assertEqual(section["unattributed_counts"]["invalid_decisions"], 1)
        self.assertIsNone(data["metrics"]["expensive_review_share"])
        if section["status"] == "not_evaluated":
            self.assertEqual(section["repositories"], [])
        else:
            self.reconcile(data, section)
            self.assertEqual([r["repo"] for r in section["repositories"]], ["d/orphan"])
            self.assertEqual(section["repositories"][0]["counts"]["routed_prs"], 0)

    def test_missing_inputs_preserve_invalid_state_and_do_not_fabricate_rows(self):
        self.supply(*cohort())
        for flag in ("--ledger", "--findings"):
            with self.subTest(flag=flag):
                result, data, section = self.projected(flag, str(self.work / "absent.jsonl"))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(data["status"], "invalid")
                self.assertEqual(section["status"], "not_evaluated")
                self.assertEqual(section["repositories"], [])
                self.assertEqual([e["code"] for e in data["errors"]], ["missing_input"])

    def test_new_fields_never_echo_notes_findings_or_rejected_identities(self):
        self.supply([
            decision(notes=RAW_MARKER, reasons=[RAW_MARKER]),
            decision(repo=REJECTED_REPO, pr=REJECTED_PR, route=RAW_MARKER),
        ], [finding(notes=RAW_MARKER, body={"nested": RAW_MARKER}),
            finding(repo=REJECTED_REPO, pr=REJECTED_PR, audit_only=RAW_MARKER)])
        result, data, section = self.projected()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["prs"][0]["id"], "a/alpha#7")
        for forbidden in (RAW_MARKER, REJECTED_REPO, str(REJECTED_PR)):
            self.assertNotIn(forbidden, json.dumps(section))
        self.reconcile(data, section)
        self.assertEqual(section["repositories"][0]["counts"]["matched_findings"], 1)

    def test_global_promotion_window_and_candidates_are_not_partitioned(self):
        findings = [finding(), finding(repo="b/beta"), finding(repo="d/orphan"),
                    finding(bucket="tier0-tests", ts="2026-01-01T00:00:00Z"),
                    finding(bucket="tier0-tests", ts="2026-01-02T00:00:00Z"),
                    finding(bucket="tier0-tests", ts="2026-01-03T00:00:00Z"),
                    finding(bucket="tier1-docs-diff", ts="2099-01-01T00:00:00Z")]
        self.supply([decision(), decision(repo="b/beta", route="standard")], findings)
        for days, expected in (("90", [{"id": "tier0-lint", "count": 3}]),
                               ("365", [{"id": "tier0-lint", "count": 3},
                                        {"id": "tier0-tests", "count": 3}])):
            with self.subTest(days=days):
                _, data, section = self.projected("--promotion-days", days)
                self.assertEqual(data["promotion_candidates"], expected)
                self.assertEqual(data["window"]["promotion_days"], int(days))
                self.reconcile(data, section)

    def test_dry_run_replays_identical_additive_output_without_mutation(self):
        self.supply(*cohort())
        normal, first, _ = self.projected()
        dry, second, _ = self.projected("--dry-run")
        self.assertEqual(first, second)
        self.assertEqual((normal.returncode, normal.stdout, normal.stderr),
                         (dry.returncode, dry.stdout, dry.stderr))

    def test_by_repo_without_json_is_actionable_usage_error(self):
        self.supply(*cohort())
        for options in (("--by-repo",), ("--by-repo", "--dry-run")):
            with self.subTest(options=options):
                result = self.invoke(*options)
                self.assertEqual(result.returncode, 2)
                self.assertIn("--json", result.stdout + result.stderr)
                self.assertIn("next_action", result.stdout + result.stderr)
                self.assertNotIn("routed_prs: 7", result.stdout)

    def test_json_bad_combinations_are_controlled_actionable_nonzero(self):
        self.supply(*cohort())
        for options in (("--by-repo", "--apply"),
                        ("--by-repo", "--promotion-days", "0"),
                        ("--by-repo", "--now", "bad")):
            with self.subTest(options=options):
                result, data = self.machine(*options)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(data["status"], "invalid")
                self.assertTrue(data["next_action"].strip())
                self.assertIs(data["retryable"], False)
                self.assertIn("invalid_argument", [e["code"] for e in data["errors"]])
                # Usage-error projection may be omitted; if emitted it is unavailable.
                if "repository_breakdown" in data:
                    self.assertEqual(data["repository_breakdown"]["status"], "not_evaluated")
                    self.assertEqual(data["repository_breakdown"]["repositories"], [])

    def test_help_exposes_an_opt_in_json_example(self):
        self.supply([])
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--by-repo", result.stdout)
        self.assertRegex(" ".join(result.stdout.split()),
                         r"--json --by-repo|--by-repo --json")
        self.assertRegex(result.stdout.lower(), r"advisory|analysis.only|not.*gate")


if __name__ == "__main__":
    unittest.main()
