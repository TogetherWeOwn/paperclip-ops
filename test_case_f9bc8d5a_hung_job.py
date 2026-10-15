#!/usr/bin/env python3
# ===========================================================================
# test_case_f9bc8d5a_hung_job.py -- offline suite for the hung CI job evaluator
# and its Gatus module (ops/case_e1e23b50/, case_703e1a90; spec case_e1e23b50)
#
# A detector that scores a stuck job as healthy greenwashes the one failure
# the queue-age signal cannot see. So this file does two jobs.
#
#   1. THE BEHAVIOUR (class Behaviour). Recorded Checks-API payload shapes in
#      ops/case_e1e23b50/fixtures drive the evaluator and the push script through
#      a stub gh and a stub curl: young in_progress (ignored), WARN, HUNG, old
#      queued and completed timed_out/cancelled (excluded), a superseded SHA
#      (never read), null started_at, malformed JSON, HTTP 403 and a full
#      check-runs page (all fail closed), plus the exact threshold boundaries,
#      the PR cap, hostile names and every refusal. It also pins the data
#      contract: every job with timeout-minutes in ANY workflow file has a row
#      that equals it, a job with no row is HUNG past GitHub's 360m default
#      + 15m (the other repos in GH_REPOS have no table), the endpoint
#      fragment names OWNER and RUNBOOK, the registry lists the endpoint, and
#      no code path names an actions/ API.
#
#   2. THE PROOF IT CAN GO RED (class Mutants). Each mutant copies the module
#      into a temp tree, breaks ONE invariant, and demands that the Behaviour
#      suite fail on that copy. A mutant that survives means an invariant is
#      asserted nowhere. A control run on the unmutated copy must stay green.
#
# Offline: stdlib only, no network, no credential, no host.
# ===========================================================================
import concurrent.futures
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("HUNG_TEST_ROOT") or HERE
IS_MUTANT_COPY = bool(os.environ.get("HUNG_TEST_ROOT"))
TOOL_DIR = os.path.join(ROOT, "ops", "case_e1e23b50")
EVAL = os.path.join(TOOL_DIR, "hung_job_eval.py")
PUSH = os.path.join(TOOL_DIR, "push-ci-hung-job.sh")
THRESHOLDS = os.path.join(TOOL_DIR, "hung_job_thresholds.json")
FIXTURES = os.path.join(TOOL_DIR, "fixtures")
FRAGMENT = os.path.join(TOOL_DIR, "gatus-endpoints.yaml")
OPERATOR = os.path.join(TOOL_DIR, "OPERATOR.md")
MODULES = os.path.join(ROOT, "ops", "case_658bf6b9", "MODULES.md")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")

KEY = "platform_ci-hung-job"
NOW = "2026-10-04T12:00:00Z"
MAIN = "9d1c4e7a2b6f0835c4d1e9a7b3f2068c5e4d7a10"
PR1 = "4b2f9c0e6a1d83754c9e0b2a6f1d8e3c7a5b9042"
TOKEN = "test-token-not-a-secret"

# The 17 recorded scenarios. Listing them here makes a deleted fixture a
# failure instead of a quietly shorter loop.
EXPECTED_FIXTURES = {
    "bad-started-at", "cap-full", "completed-timed-out", "forbidden-403", "healthy", "hung",
    "main-hung", "malformed-json", "no-check-runs-list", "null-started-at", "old-queued",
    "superseded-sha", "total-count-over", "unknown-threshold", "warn-and-hung", "warn",
    "young-in-progress",
}

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
path = args[1] if len(args) == 2 and args[0] == "api" else None
with open(os.environ["GH_CALL_LOG"], "a") as fh:
    fh.write((path if path is not None else "BAD-ARGV " + " ".join(args)) + "\n")
if path is None:
    sys.stderr.write("gh stub: only `gh api <path>` is served\n")
    sys.exit(2)
route = json.load(open(os.environ["GH_SCENARIO"]))["routes"].get(path)
if route is None:
    sys.stderr.write("gh: Not Found (HTTP 404)\n")
    sys.exit(1)
if route.get("rc", 0) != 0:
    sys.stderr.write(route.get("stderr", "") + "\n")
    sys.exit(route["rc"])
if "raw" in route:
    sys.stdout.write(route["raw"])
    sys.exit(0)
body = route["body"]
pad = route.get("pad_check_runs_to")
if pad:
    runs = body["check_runs"]
    while len(runs) < pad:
        runs.append({"id": 700000 + len(runs), "name": "filler", "status": "completed",
                     "conclusion": "success", "started_at": "2026-10-04T11:00:00Z",
                     "completed_at": "2026-10-04T11:01:00Z", "head_sha": runs[0]["head_sha"]})
    body["total_count"] = max(body["total_count"], pad)
print(json.dumps(body))
'''

FAKE_CURL = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ARGV_LOG"
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cat "${!j}" >> "$CFG_LOG"; echo >> "$CFG_LOG"; fi
done
printf '200'
'''


def write_exec(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def ago(minutes=0, seconds=0):
    import datetime
    base = datetime.datetime(2026, 10, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return (base - datetime.timedelta(minutes=minutes, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def mkrun(rid, name, status="in_progress", started=None, head=PR1, conclusion=None):
    return {"id": rid, "name": name, "head_sha": head, "status": status, "conclusion": conclusion,
            "started_at": started, "completed_at": None, "app": {"slug": "github-actions"}}


def cr_path(repo, ref):
    return f"repos/{repo}/commits/{ref}/check-runs?per_page=100"


def pulls_path(repo):
    return f"repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page=31"


def pr_entry(n, sha):
    return {"number": n, "head": {"sha": sha}, "base": {"ref": "main"}}


def scenario(pr_runs=(), main_runs=(), repo="o/r", pulls=None, routes=None):
    r = {cr_path(repo, "main"): {"rc": 0, "body": {"total_count": len(main_runs), "check_runs": list(main_runs)}},
         pulls_path(repo): {"rc": 0, "body": pulls if pulls is not None else [pr_entry(12, PR1)]},
         cr_path(repo, PR1): {"rc": 0, "body": {"total_count": len(pr_runs), "check_runs": list(pr_runs)}}}
    r.update(routes or {})
    return {"now": NOW, "repos": repo, "routes": r}


def expand_matrix_name(name, matrix):
    """Runtime check-run names for a ci.yml display name.

    GitHub reports the EXPANDED name, never the literal expression, so
    `model-selection mutants (shard ${{ matrix.shard }}/${{ strategy.job-total }})`
    with `shard: [1, ..., 16]` becomes one row per `shard i/16`. Each
    `${{ matrix.<dim> }}` takes every value of that dim's list and
    `${{ strategy.job-total }}` is the total combo count. A token the parser
    cannot resolve keeps the literal name so the coverage test still demands a
    human look instead of silently dropping the job.
    """
    tokens = re.findall(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}", name)
    dims = [(d, matrix[d]) for d in dict.fromkeys(tokens) if d in matrix]
    if "${{" in name and not dims:
        return [name]
    combos = [()]
    for _, values in dims:
        combos = [c + (v,) for c in combos for v in values]
    total = 1
    for _, values in dims:
        total *= len(values)
    out = []
    for combo in combos or [()]:
        expanded = name
        for (dim, _), value in zip(dims, combo):
            expanded = re.sub(r"\$\{\{\s*matrix\." + re.escape(dim) + r"\s*\}\}", value, expanded)
        expanded = re.sub(r"\$\{\{\s*strategy\.job-total\s*\}\}", str(total), expanded)
        out.append(expanded)
    if any("${{" in n for n in out):
        out.append(name)
    return out


def ci_jobs(text):
    """{display name: timeout minutes} for every ci.yml job with timeout-minutes.

    Matrix jobs expand to their runtime check-run names (see
    expand_matrix_name): the coverage test demands a threshold row per runtime
    name, so deleting a row turns it red.
    """
    jobs, cur, in_jobs = {}, None, False
    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs and re.match(r"^\S", line) and not line.startswith("#"):
            in_jobs = False
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*(#.*)?$", line)
        if m:
            cur = m.group(1)
            jobs[cur] = {"name": cur, "timeout": None, "matrix": {}}
            continue
        if cur is None:
            continue
        m = re.match(r"^    name:\s*(.+?)\s*$", line)
        if m:
            val = m.group(1)
            if val[:1] in ("'", '"') and val.count(val[0]) >= 2:
                val = val[1:val.index(val[0], 1)]
            else:
                val = re.sub(r"\s+#.*$", "", val)
            jobs[cur]["name"] = val
        m = re.match(r"^    timeout-minutes:\s*(\d+)\s*(#.*)?$", line)
        if m:
            jobs[cur]["timeout"] = int(m.group(1))
        m = re.match(r"^        ([A-Za-z0-9_-]+):\s*\[([^\]]*)\]\s*(#.*)?$", line)
        if m:
            # A `strategy.matrix:` dimension list, e.g. `shard: [1, 2, ...]`.
            # Eight-space indent keeps job-level lists (`needs: [...]`, 4-space)
            # out; an unrecognised shape simply never feeds an expansion.
            jobs[cur]["matrix"][m.group(1)] = [v.strip() for v in m.group(2).split(",") if v.strip()]
    out = {}
    for job in jobs.values():
        if job["timeout"] is None:
            continue
        for name in expand_matrix_name(job["name"], job["matrix"]):
            out[name] = job["timeout"]
    return out


def workflow_jobs(directory):
    """({check-run name: timeout minutes}, [conflict text]) over every workflow file.

    GitHub reports a job under its display name whichever workflow it came
    from, and the threshold table is keyed by that name alone, so two files
    reusing one name with different timeouts cannot both be right: they are
    returned as conflicts instead of one silently winning.
    """
    jobs, conflicts, seen = {}, [], {}
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(directory, fname), encoding="utf-8") as fh:
            found = ci_jobs(fh.read())
        for name, minutes in found.items():
            if name in jobs and jobs[name] != minutes:
                conflicts.append(f"{name!r}: {seen[name]} says {jobs[name]}m, {fname} says {minutes}m")
                continue
            jobs[name] = minutes
            seen.setdefault(name, fname)
    return jobs, conflicts


class Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="case_f9bc8d5a-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.bin = os.path.join(cls.tmp, "bin")
        os.makedirs(cls.bin)
        cls.gh = os.path.join(cls.bin, "gh")
        write_exec(cls.gh, FAKE_GH)
        write_exec(os.path.join(cls.bin, "curl"), FAKE_CURL)
        cls.scn = os.path.join(cls.tmp, "scenario.json")
        cls.calls = os.path.join(cls.tmp, "gh.log")
        cls.argv_log = os.path.join(cls.tmp, "argv.log")
        cls.cfg_log = os.path.join(cls.tmp, "cfg.log")

    def load_calls(self):
        with open(self.calls, encoding="utf-8") as fh:
            return [l for l in fh.read().splitlines() if l]

    def arm(self, sc):
        with open(self.scn, "w", encoding="utf-8") as fh:
            json.dump(sc, fh)
        for p in (self.calls, self.argv_log, self.cfg_log):
            open(p, "w").close()
        env = dict(os.environ)
        env.update({"GH_SCENARIO": self.scn, "GH_CALL_LOG": self.calls,
                    "ARGV_LOG": self.argv_log, "CFG_LOG": self.cfg_log})
        return env

    def evaluate(self, sc, thresholds=None, repos=None, now=None, extra=()):
        env = self.arm(sc)
        cmd = [sys.executable, EVAL, "--repos", repos or sc["repos"], "--now", now or sc["now"],
               "--gh", self.gh]
        if thresholds:
            cmd += ["--thresholds", thresholds]
        cmd += list(extra)
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        out = None
        if p.returncode == 0:
            out = json.loads(p.stdout)
        return p.returncode, out, p.stderr

    def push(self, sc, env_over=None, drop=(), path_prefix=None, tool=None):
        env = self.arm(sc)
        env.update({"GATUS_URL": "http://127.0.0.1:9", "GATUS_EXTERNAL_TOKEN": TOKEN,
                    "GH_REPOS": sc["repos"], "GH_BIN": self.gh, "HUNG_JOB_NOW": sc["now"],
                    "TMPDIR": self.tmp,
                    "PATH": (path_prefix + os.pathsep if path_prefix else "") + self.bin + os.pathsep + os.environ["PATH"]})
        env.update(env_over or {})
        for k in drop:
            env.pop(k, None)
        p = subprocess.run(["bash", tool or PUSH], capture_output=True, text=True, env=env, timeout=120)
        return p

    def pushed(self):
        """[(success, decoded error)] for every push to the endpoint, in order."""
        with open(self.cfg_log, encoding="utf-8") as fh:
            text = fh.read()
        out = []
        for m in re.finditer(r'url = "[^"]*/endpoints/([^/]+)/external\?success=(true|false)(?:&error=([^"]*))?"', text):
            out.append((m.group(1), m.group(2), urllib.parse.unquote(m.group(3) or "")))
        return out

    def thresholds_file(self, doc):
        # A fresh file per call: cases built in one dict must not share a path.
        self.__class__.thr_seq = getattr(self.__class__, "thr_seq", 0) + 1
        path = os.path.join(self.tmp, f"thr-{self.thr_seq}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        return path


class Behaviour(Harness):
    # ---- recorded scenarios --------------------------------------------------
    def test_every_recorded_fixture_exists(self):
        have = {f[:-5] for f in os.listdir(FIXTURES) if f.endswith(".json")}
        self.assertEqual(EXPECTED_FIXTURES - have, set(), "a recorded fixture was deleted")

    def test_recorded_scenarios(self):
        for name in sorted(EXPECTED_FIXTURES):
            with self.subTest(fixture=name):
                with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
                    sc = json.load(fh)
                exp = sc["expect"]
                rc, out, err = self.evaluate(sc)
                self.assertEqual(rc, 0, err)
                self.assertEqual(out["success"], exp["success"], out["text"])
                self.assertEqual(out["verdict"], exp["verdict"], out["text"])
                if exp.get("text_empty"):
                    self.assertEqual(out["text"], "")
                for needle in exp.get("text_has", []):
                    self.assertIn(needle, out["text"])
                for needle in exp.get("text_lacks", []):
                    self.assertNotIn(needle, out["text"])
                calls = self.load_calls()
                self.assertEqual([c for c in calls if c not in sc["routes"]], [], "unrouted call")
                for sha in exp.get("never_called", []):
                    self.assertFalse([c for c in calls if sha in c], f"read superseded sha {sha[:7]}")
                if "call_count" in exp:
                    self.assertEqual(len(calls), exp["call_count"])

    def test_no_recorded_scenario_calls_an_actions_path(self):
        for name in sorted(EXPECTED_FIXTURES):
            with self.subTest(fixture=name):
                with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
                    sc = json.load(fh)
                self.evaluate(sc)
                for call in self.load_calls():
                    self.assertNotRegex(call, r"(^|/)actions(/|$)", call)
                    self.assertRegex(call, r"/(commits/[^/]+/check-runs|pulls)\?")

    def test_source_never_names_an_actions_api(self):
        for path in (EVAL, PUSH):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(re.search(r"repos/[^\s\"']*/actions/", text), path)
            self.assertIsNone(re.search(r"/actions/(runs|jobs)", text), path)

    # ---- thresholds: exact boundaries ---------------------------------------
    def classify(self, name, minutes, seconds=0, thresholds=None):
        sc = scenario([mkrun(2001, name, started=ago(minutes, seconds))])
        rc, out, err = self.evaluate(sc, thresholds=thresholds)
        self.assertEqual(rc, 0, err)
        return out

    def test_warn_starts_strictly_above_075_of_timeout(self):
        # Privilege ceiling suites: timeout 20m, WARN above 15m, HUNG above 35m.
        self.assertEqual(self.classify("Privilege ceiling suites", 15)["verdict"], "HEALTHY")
        self.assertEqual(self.classify("Privilege ceiling suites", 15, 1)["verdict"], "WARN")

    def test_hung_starts_strictly_above_timeout_plus_15m(self):
        out = self.classify("Privilege ceiling suites", 35)
        self.assertEqual((out["verdict"], out["success"]), ("WARN", True))
        out = self.classify("Privilege ceiling suites", 35, 1)
        self.assertEqual((out["verdict"], out["success"]), ("HUNG", False))

    def test_private_mutation_job_uses_its_50m_timeout_boundaries(self):
        for minutes, seconds, verdict, success in (
            (37, 30, "HEALTHY", True),
            (37, 31, "WARN", True),
            (65, 0, "WARN", True),
            (65, 1, "HUNG", False),
        ):
            with self.subTest(minutes=minutes, seconds=seconds):
                out = self.classify("Private mutation gates", minutes, seconds)
                self.assertEqual((out["verdict"], out["success"]), (verdict, success))

    def test_warn_never_pages_on_its_own(self):
        out = self.classify("Offline suites", 130)
        self.assertEqual((out["verdict"], out["success"]), ("WARN", True))
        self.assertIn("Offline suites", out["text"])

    def test_age_floor_is_5_minutes_and_beats_a_tiny_threshold_row(self):
        thr = self.thresholds_file({"schema": "case_f9bc8d5a-hung-job-thresholds-v1", "jobs": {"Tiny job": 1}})
        self.assertEqual(self.classify("Tiny job", 4, 59, thresholds=thr)["verdict"], "HEALTHY")
        self.assertEqual(self.classify("Tiny job", 5, 0, thresholds=thr)["verdict"], "HEALTHY")
        self.assertEqual(self.classify("Tiny job", 5, 1, thresholds=thr)["verdict"], "WARN")

    def test_unknown_threshold_is_named_not_healthy_and_respects_the_floor(self):
        young = self.classify("No row job", 5, 0)
        self.assertEqual(young["unknown"], 0)
        old = self.classify("No row job", 5, 1)
        self.assertEqual(old["unknown"], 1)
        self.assertIn("UNKNOWN-threshold 1", old["text"])
        self.assertIn("No row job", old["text"])
        self.assertTrue(old["success"])

    def test_threshold_join_is_by_exact_check_run_name(self):
        out = self.classify("offline suites", 300)  # case differs: not the row
        self.assertEqual((out["hung"], out["unknown"]), (0, 1))

    def test_no_row_job_is_hung_strictly_above_the_360m_default_plus_15m(self):
        # GitHub's default job timeout is 360m: a started job with no row gets that
        # limit, never "healthy forever".
        out = self.classify("No row job", 375)
        self.assertEqual((out["verdict"], out["success"], out["unknown"]), ("HEALTHY", True, 1))
        self.assertIn("pages above 375m", out["text"])
        out = self.classify("No row job", 375, 1)
        self.assertEqual((out["verdict"], out["success"], out["hung"], out["unknown"]),
                         ("HUNG", False, 1, 0))
        self.assertIn("No row job", out["text"])
        self.assertIn("no row", out["text"])

    def test_a_stuck_job_in_a_repo_with_no_table_pages(self):
        # Review repro: GH_REPOS is shared, but the table holds this repo's names.
        # A two-bot-next `rust tests` run stuck 300m is still inside the backstop;
        # 400m is not.
        for minutes, verdict, success in ((300, "HEALTHY", True), (400, "HUNG", False)):
            with self.subTest(minutes=minutes):
                sc = scenario([mkrun(2201, "rust tests", started=ago(minutes))], repo="TogetherWeOwn/two-bot-next")
                rc, out, err = self.evaluate(sc)
                self.assertEqual((out["verdict"], out["success"]), (verdict, success), out["text"])
                if not success:
                    self.assertIn("two-bot-next PR#12", out["text"])

    def test_a_stuck_pr_lint_pages_on_its_own_row(self):
        # Review repro: the required pr-lint check (5m timeout) had no row.
        self.assertEqual(self.classify("pr-lint", 20)["verdict"], "WARN")
        out = self.classify("pr-lint", 20, 1)
        self.assertEqual((out["verdict"], out["success"], out["unknown"]), ("HUNG", False, 0))

    # ---- classifier exclusions ------------------------------------------------
    def test_only_in_progress_is_a_candidate(self):
        for status, conclusion in (("queued", None), ("waiting", None), ("pending", None),
                                   ("requested", None), ("completed", "timed_out"),
                                   ("completed", "cancelled"), ("completed", "success"),
                                   ("completed", None)):
            with self.subTest(status=status, conclusion=conclusion):
                sc = scenario([mkrun(2101, "Offline suites", status=status, started=ago(600),
                                     conclusion=conclusion)])
                rc, out, err = self.evaluate(sc)
                self.assertEqual((out["verdict"], out["success"], out["text"]), ("HEALTHY", True, ""))

    def test_in_progress_without_started_at_is_excluded_not_failed(self):
        sc = scenario([mkrun(2111, "Offline suites", started=None)])
        rc, out, err = self.evaluate(sc)
        self.assertEqual((out["verdict"], out["success"], out["text"]), ("HEALTHY", True, ""))

    def test_run_reporting_another_head_sha_is_ignored(self):
        other = "c7e3a1f95b2d40688a1c3e5f7092b4d6e8f0a1b3"
        sc = scenario([mkrun(2121, "Offline suites", started=ago(600), head=other)])
        rc, out, err = self.evaluate(sc)
        self.assertEqual((out["verdict"], out["success"]), ("HEALTHY", True))

    def test_stuck_job_on_main_is_named_main(self):
        sc = scenario(main_runs=[mkrun(2131, "Failure log drain", started=ago(60), head=MAIN)])
        rc, out, err = self.evaluate(sc)
        self.assertFalse(out["success"])
        self.assertIn("o/r main", out["text"])

    def test_two_repos_are_both_polled_and_the_failing_one_is_named(self):
        sc = scenario([], repo="o/r")
        sc["routes"].update(scenario([mkrun(2141, "Offline suites", started=ago(300))], repo="p/q")["routes"])
        sc["repos"] = "o/r p/q"
        rc, out, err = self.evaluate(sc)
        self.assertEqual((out["success"], out["hung"]), (False, 1))
        self.assertIn("p/q PR#12", out["text"])
        self.assertTrue([c for c in self.load_calls() if c.startswith("repos/p/q/")])

    # ---- fail closed -----------------------------------------------------------
    def test_a_full_100_run_page_fails_closed(self):
        runs = [mkrun(3000 + i, "Operator runbook gates", status="completed", started=ago(50),
                      conclusion="success") for i in range(100)]
        rc, out, err = self.evaluate(scenario(runs))
        self.assertFalse(out["success"])
        self.assertIn("page full", out["text"])

    def test_99_runs_is_not_a_full_page(self):
        runs = [mkrun(3000 + i, "Operator runbook gates", status="completed", started=ago(50),
                      conclusion="success") for i in range(99)]
        rc, out, err = self.evaluate(scenario(runs))
        self.assertEqual((out["success"], out["text"]), (True, ""))

    def test_pr_list_failures_fail_closed(self):
        for label, route in (("unreadable", {"rc": 1, "stderr": "gh: Server Error (HTTP 500)"}),
                             ("not json", {"rc": 0, "raw": "<html>"}),
                             ("not a list", {"rc": 0, "body": {"message": "nope"}})):
            with self.subTest(label):
                rc, out, err = self.evaluate(scenario(routes={pulls_path("o/r"): route}))
                self.assertEqual((out["success"], out["verdict"]), (False, "FAIL"))

    def test_malformed_pr_entries_fail_closed_and_hostile_sha_is_never_requested(self):
        hostile = "../../actions/runs/1/jobs"
        sc = scenario(pulls=[pr_entry(7, hostile), {"number": "x", "head": {"sha": PR1}},
                             {"number": 8, "head": None}, "junk", pr_entry(12, PR1)])
        rc, out, err = self.evaluate(sc)
        self.assertEqual((out["success"], out["verdict"]), (False, "FAIL"))
        self.assertFalse([c for c in self.load_calls() if "actions" in c or ".." in c])
        self.assertTrue([c for c in self.load_calls() if PR1 in c], "the good PR head must still be polled")

    def test_non_object_and_nameless_in_progress_entries_fail_closed(self):
        rc, out, err = self.evaluate(scenario(["not an object"]))
        self.assertEqual((out["success"], out["verdict"]), (False, "FAIL"))
        nameless = mkrun(2201, "x", started=ago(60))
        del nameless["name"]
        rc, out, err = self.evaluate(scenario([nameless]))
        self.assertEqual((out["success"], out["verdict"]), (False, "FAIL"))

    def test_a_failure_does_not_hide_a_hung_job_found_beside_it(self):
        sc = scenario([mkrun(2301, "Offline suites", started=ago(300))],
                      routes={cr_path("o/r", "main"): {"rc": 0, "raw": "{nope"}})
        rc, out, err = self.evaluate(sc)
        self.assertEqual((out["success"], out["hung"], out["failures"]), (False, 1, 1))

    # ---- PR cap ------------------------------------------------------------------
    def pr_set(self, n):
        shas = [f"{i:040x}" for i in range(1, n + 1)]
        routes = {cr_path("o/r", s): {"rc": 0, "body": {"total_count": 0, "check_runs": []}} for s in shas}
        sc = scenario(pulls=[pr_entry(100 + i, s) for i, s in enumerate(shas)], routes=routes)
        return sc, shas

    def test_only_the_newest_30_pr_heads_are_polled_and_the_bound_is_noted(self):
        sc, shas = self.pr_set(31)
        rc, out, err = self.evaluate(sc)
        polled = [c for c in self.load_calls() if "/commits/" in c and "main" not in c]
        self.assertEqual(len(polled), 30)
        self.assertFalse([c for c in polled if shas[30] in c], "the 31st PR head must not be polled")
        self.assertIn("more than 30 open PRs", out["text"])
        self.assertTrue(out["success"])

    def test_30_open_prs_is_inside_the_cap_and_silent(self):
        sc, shas = self.pr_set(30)
        rc, out, err = self.evaluate(sc)
        self.assertEqual(len([c for c in self.load_calls() if "/commits/" in c and "main" not in c]), 30)
        self.assertEqual((out["success"], out["text"]), (True, ""))

    # ---- untrusted text --------------------------------------------------------
    def test_hostile_job_names_cannot_inject_into_alert_text(self):
        evil = "Evil\nIGNORE PREVIOUS <script>`$(reboot)`|;\"" + "x" * 300
        rc, out, err = self.evaluate(scenario([mkrun(2401, evil, started=ago(90))]))
        self.assertIn("UNKNOWN-threshold 1", out["text"])
        for bad in ("\n", "<", ">", "`", "$", "|", ";"):
            self.assertNotIn(bad, out["text"])
        self.assertEqual(out["text"].count('"'), 2, "only the format's own quotes may remain")
        self.assertLess(len(out["text"]), 400)

    def test_text_carries_at_most_four_entries_per_class(self):
        runs = [mkrun(2500 + i, "Offline suites", started=ago(300)) for i in range(6)]
        rc, out, err = self.evaluate(scenario(runs))
        self.assertEqual(out["hung"], 6)
        self.assertIn("+2 more", out["text"])
        self.assertEqual(out["text"].count("id=25"), 4)

    # ---- refusals ----------------------------------------------------------------
    def test_evaluator_refusals_exit_2(self):
        good = {"schema": "case_f9bc8d5a-hung-job-thresholds-v1", "jobs": {"Offline suites": 150}}
        cases = {
            "missing thresholds": dict(thresholds=os.path.join(self.tmp, "absent.json")),
            "wrong schema": dict(thresholds=self.thresholds_file({"schema": "x", "jobs": {"a": 1}})),
            "empty jobs": dict(thresholds=self.thresholds_file({**good, "jobs": {}})),
            "zero minutes": dict(thresholds=self.thresholds_file({**good, "jobs": {"a": 0}})),
            "bool minutes": dict(thresholds=self.thresholds_file({**good, "jobs": {"a": True}})),
            "string minutes": dict(thresholds=self.thresholds_file({**good, "jobs": {"a": "20"}})),
            "not json": dict(thresholds=self.thresholds_file("{nope")),
            "bad repo": dict(repos="not-a-repo"),
            "dotdot repo": dict(repos="o/.."),
            "no repo": dict(repos=" "),
            "bad now": dict(now="yesterday"),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                rc, out, err = self.evaluate(scenario(), **kw)
                self.assertEqual(rc, 2, err)
                self.assertIsNone(out)
        env = self.arm(scenario())
        p = subprocess.run([sys.executable, EVAL, "--repos", "o/r", "--now", NOW,
                            "--gh", os.path.join(self.tmp, "no-such-gh")],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 2)

    # ---- the push script ------------------------------------------------------------
    def fixture(self, name):
        with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_push_healthy_is_one_success_true_push_and_exit_0(self):
        p = self.push(self.fixture("healthy"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.pushed(), [(KEY, "true", "")])
        self.assertIn("HEALTHY", p.stdout)

    def test_push_hung_is_one_success_false_push_naming_the_job_and_exit_1(self):
        p = self.push(self.fixture("hung"))
        self.assertEqual(p.returncode, 1)
        pushes = self.pushed()
        self.assertEqual([(k, s) for k, s, _ in pushes], [(KEY, "false")])
        self.assertIn("Privilege ceiling suites", pushes[0][2])
        self.assertIn("id=1031", pushes[0][2])

    def test_push_pages_for_a_stuck_job_that_has_no_threshold_row(self):
        sc = scenario([mkrun(2301, "rust tests", started=ago(400))], repo="o/two-bot-next")
        p = self.push(sc)
        self.assertEqual(p.returncode, 1)
        pushes = self.pushed()
        self.assertEqual([(k, s) for k, s, _ in pushes], [(KEY, "false")])
        self.assertIn("rust tests", pushes[0][2])
        self.assertIn("id=2301", pushes[0][2])

    def test_push_warn_only_is_success_true_with_the_text_on_stdout(self):
        p = self.push(self.fixture("warn"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([(k, s) for k, s, _ in self.pushed()], [(KEY, "true")])
        self.assertIn("WARN 1", p.stdout)
        self.assertIn("Offline suites", p.stdout)

    def test_push_403_and_malformed_json_push_false(self):
        for name in ("forbidden-403", "malformed-json", "cap-full"):
            with self.subTest(name):
                p = self.push(self.fixture(name))
                self.assertEqual(p.returncode, 1)
                self.assertEqual([(k, s) for k, s, _ in self.pushed()], [(KEY, "false")])

    def test_push_exactly_once_per_run_even_with_many_findings(self):
        self.push(self.fixture("warn-and-hung"))
        self.assertEqual(len(self.pushed()), 1)

    def test_bearer_token_travels_in_the_config_not_in_argv(self):
        self.push(self.fixture("hung"))
        with open(self.argv_log, encoding="utf-8") as fh:
            argv = fh.read()
        with open(self.cfg_log, encoding="utf-8") as fh:
            cfg = fh.read()
        self.assertNotIn(TOKEN, argv)
        self.assertIn(TOKEN, cfg)
        self.assertIn("--config", argv)

    def test_push_refusals_exit_2_with_zero_pushes(self):
        for var in ("GATUS_URL", "GATUS_EXTERNAL_TOKEN", "GH_REPOS"):
            with self.subTest(var):
                p = self.push(self.fixture("healthy"), drop=(var,))
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertEqual(self.pushed(), [])
        p = self.push(self.fixture("healthy"), env_over={"GH_REPOS": "bad repo/name/x"})
        self.assertEqual((p.returncode, self.pushed()), (2, []))

    def test_unreadable_thresholds_refuse_with_zero_pushes(self):
        copy = os.path.join(self.tmp, "no-thr")
        shutil.rmtree(copy, True)
        shutil.copytree(TOOL_DIR, os.path.join(copy, "ops", "case_e1e23b50"))
        os.makedirs(os.path.join(copy, "ops", "case_658bf6b9"))
        shutil.copy(os.path.join(ROOT, "ops", "case_658bf6b9", "_gatus_push.sh"),
                    os.path.join(copy, "ops", "case_658bf6b9", "_gatus_push.sh"))
        os.remove(os.path.join(copy, "ops", "case_e1e23b50", "hung_job_thresholds.json"))
        p = self.push(self.fixture("healthy"), tool=os.path.join(copy, "ops", "case_e1e23b50", "push-ci-hung-job.sh"))
        self.assertEqual((p.returncode, self.pushed()), (2, []))

    def shim_python(self, mode):
        d = os.path.join(self.tmp, "pyshim-" + mode)
        os.makedirs(d, exist_ok=True)
        body = {"crash": "exit 3", "garbage": "echo garbage; exit 0"}[mode]
        write_exec(os.path.join(d, "python3"),
                   '#!/usr/bin/env bash\nfor a in "$@"; do\n  case "$a" in *hung_job_eval.py) '
                   + body + ' ;; esac\ndone\nexec "' + sys.executable + '" "$@"\n')
        return d

    def test_an_evaluator_crash_pushes_false_never_green(self):
        p = self.push(self.fixture("healthy"), path_prefix=self.shim_python("crash"))
        self.assertEqual(p.returncode, 1)
        pushes = self.pushed()
        self.assertEqual([(k, s) for k, s, _ in pushes], [(KEY, "false")])
        self.assertIn("crashed", pushes[0][2])

    def test_unparseable_evaluator_output_pushes_false(self):
        p = self.push(self.fixture("healthy"), path_prefix=self.shim_python("garbage"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual([(k, s) for k, s, _ in self.pushed()], [(KEY, "false")])

    # ---- the data contract --------------------------------------------------------
    def test_every_workflow_job_with_a_timeout_has_an_equal_threshold_row(self):
        # Every workflow, not only ci.yml: pr-lint (required), docs-verdict, the
        # disclosure audit and the GARM canaries all post checks on heads we poll.
        jobs, conflicts = workflow_jobs(WORKFLOWS)
        with open(THRESHOLDS, encoding="utf-8") as fh:
            rows = json.load(fh)["jobs"]
        self.assertEqual(conflicts, [], "one check-run name, two different timeouts")
        self.assertGreaterEqual(len(jobs), 18, "workflow parse found suspiciously few jobs")  # 2026-10-10: two repo-split guard jobs folded into secret-scan
        self.assertIn("pr-lint", jobs, "pr-lint.yml was not read: the coverage test only sees ci.yml")
        for name, minutes in sorted(jobs.items()):
            self.assertIn(name, rows,
                          f'a workflow job {name!r} has timeout-minutes {minutes} but no row: add '
                          f'"{name}": {minutes} to ops/case_e1e23b50/hung_job_thresholds.json')
            self.assertEqual(rows[name], minutes, f"{name!r}: row disagrees with the workflow timeout-minutes")

    def test_workflow_scan_reports_a_reused_name_with_different_timeouts(self):
        d = tempfile.mkdtemp(prefix="case_f9bc8d5a-wf-")
        self.addCleanup(shutil.rmtree, d, True)
        job = "jobs:\n  probe:\n    name: probe\n    timeout-minutes: %d\n"
        for fname, minutes in (("a.yml", 15), ("b.yml", 15), ("c.yml", 30)):
            with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                fh.write(job % minutes)
        jobs, conflicts = workflow_jobs(d)
        self.assertEqual(jobs, {"probe": 15})
        self.assertEqual(len(conflicts), 1)
        self.assertIn("c.yml says 30m", conflicts[0])

    def test_threshold_rows_are_positive_whole_minutes(self):
        with open(THRESHOLDS, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["schema"], "case_f9bc8d5a-hung-job-thresholds-v1")
        for name, minutes in doc["jobs"].items():
            self.assertIs(type(minutes), int, name)
            self.assertGreater(minutes, 0, name)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_fragment_declares_the_endpoint_with_owner_runbook_and_10m_heartbeat(self):
        frag = self.read(FRAGMENT)
        self.assertIn("external-endpoints:", frag)
        self.assertRegex(frag, r"(?m)^  - name: ci-hung-job$")
        self.assertRegex(frag, r"(?m)^    group: platform$")
        self.assertRegex(frag, r'(?m)^    token: "\$\{GATUS_EXTERNAL_TOKEN\}"$')
        self.assertRegex(frag, r"(?m)^      interval: 10m$")
        descs = re.findall(r'(?m)^\s+description: "([^"]*)"', frag)
        self.assertEqual(len(descs), 2)
        types = re.findall(r"(?m)^\s+- type: (\w+)$", frag)
        self.assertEqual(sorted(types), ["custom", "discord"])
        for d in descs:
            self.assertRegex(d, r"\bOWNER \S")
            m = re.search(r"\bRUNBOOK (ops/case_e1e23b50/OPERATOR\.md)#([a-z0-9-]+)", d)
            self.assertIsNotNone(m, d)
            self.assertIn(m.group(2), self.heading_slugs(OPERATOR), f"RUNBOOK anchor #{m.group(2)} has no heading")

    def heading_slugs(self, path):
        heads = re.findall(r"(?m)^#+\s+(.+?)\s*$", self.read(path))
        return {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-") for h in heads}

    def test_operator_doc_carries_the_cron_line_and_a_rollback(self):
        op = self.read(OPERATOR)
        self.assertRegex(op, r"(?m)^\*/5 \* \* \* \* root set -a; \. \S+ && exec /opt/status-page/current/ops/case_e1e23b50/push-ci-hung-job\.sh$")
        self.assertIn("Rollback", op)
        self.assertIn("Operator:", op)

    def cron_command(self):
        """The documented cron payload with only its paths substituted.

        Ordinary assignments must reach the child; a failed source must not
        run it. A regex matching the command is not a behavioral test, so the
        next two tests execute the extracted payload under a clean /bin/sh.
        """
        op = self.read(OPERATOR)
        lines = [l for l in op.splitlines()
                 if re.match(r"^[*0-9/ ]+ root ", l) and l.rstrip().endswith("/push-ci-hung-job.sh")]
        self.assertEqual(len(lines), 1, "expected exactly one documented cron line for the pusher")
        cmd = lines[0].split(" root ", 1)[1].strip()
        return (cmd.replace("/root/.config/paperclip/case_60330d6f.env", os.path.join(self.tmp, "cron.env"))
                   .replace("/opt/status-page/current/ops/case_e1e23b50/push-ci-hung-job.sh", PUSH))

    def run_cron(self, cmd, env_body):
        """Run the cron payload under env -i + /bin/sh like cron does."""
        with open(os.path.join(self.tmp, "cron.env"), "w", encoding="utf-8") as fh:
            fh.write(env_body)
        outer = {"PATH": self.bin + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
                 "TMPDIR": self.tmp, "GH_SCENARIO": self.scn, "GH_CALL_LOG": self.calls,
                 "ARGV_LOG": self.argv_log, "CFG_LOG": self.cfg_log}
        return subprocess.run(["env", "-i"] + [k + "=" + v for k, v in outer.items()]
                              + ["/bin/sh", "-c", cmd],
                              capture_output=True, text=True, timeout=120)

    def test_documented_cron_exports_ordinary_assignments_into_the_pusher(self):
        sc = self.fixture("healthy")
        self.arm(sc)
        cmd = self.cron_command()
        env_body = ("GATUS_URL=http://127.0.0.1:9\nGATUS_EXTERNAL_TOKEN=%s\nGH_REPOS=%s\n"
                    "GH_BIN=%s\nHUNG_JOB_NOW=%s\n" % (TOKEN, sc["repos"], self.gh, sc["now"]))
        p = self.run_cron(cmd, env_body)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([(k, s) for k, s, _ in self.pushed()], [(KEY, "true")])

    def test_documented_cron_stops_without_running_when_sourcing_fails(self):
        self.arm(self.fixture("healthy"))
        cmd = self.cron_command()
        p = self.run_cron(cmd, "false\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.pushed(), [])
        self.assertNotIn("push-ci-hung-job", p.stdout + p.stderr,
                         "a failed source must not start the pusher at all")

    def test_registry_lists_the_endpoint(self):
        self.assertIsNotNone(re.search(r"(?<![A-Za-z0-9_/-])platform/ci-hung-job(?![A-Za-z0-9_-])", self.read(MODULES)))

    def test_script_names_its_key_in_code_not_only_in_a_comment(self):
        code = "\n".join(l for l in self.read(PUSH).splitlines() if not l.lstrip().startswith("#"))
        self.assertRegex(code, r"(?<![A-Za-z0-9_-])platform_ci-hung-job(?![A-Za-z0-9_-])")

    def test_no_credential_shaped_value_is_committed(self):
        paths = [EVAL, PUSH, THRESHOLDS, FRAGMENT, OPERATOR] + [
            os.path.join(FIXTURES, f) for f in os.listdir(FIXTURES)]
        for path in paths:
            text = self.read(path)
            self.assertIsNone(re.search(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|-----BEGIN [A-Z ]*KEY", text), path)
            self.assertIsNone(re.search(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}", text), path)

    @unittest.skipUnless(os.path.exists(os.path.join(ROOT, "ops", "case_658bf6b9", "check_module_wiring.py")),
                         "module wiring gate (case_f3500d44) not on this base yet")
    def test_module_wiring_gate_accepts_the_endpoint_without_an_allowlist_entry(self):
        gate = os.path.join(ROOT, "ops", "case_658bf6b9", "check_module_wiring.py")
        p = subprocess.run([sys.executable, gate, "--root", ROOT, "--json"], capture_output=True,
                           text=True, timeout=60)
        doc = json.loads(p.stdout)
        self.assertEqual([f for f in doc["findings"] if f.get("key") == KEY], [], "this endpoint is not wired")
        if not IS_MUTANT_COPY:
            # A mutant copy holds only this module, so every other module's
            # allowlist entry reads as an orphan there; the real tree must pass whole.
            self.assertEqual(p.returncode, 0, json.dumps(doc["findings"]))
        self.assertIn(f"OK      {KEY}", doc["report"])
        allow = os.path.join(ROOT, "ops", "case_658bf6b9", "module_wiring_allowlist.json")
        if os.path.exists(allow):
            self.assertNotIn(KEY, self.read(allow))


# ---------------------------------------------------------------------------
# Mutants: one broken invariant each; the Behaviour suite must go red on it.
# ---------------------------------------------------------------------------
E, P, T, F, O, M = ("hung_job_eval.py", "push-ci-hung-job.sh", "hung_job_thresholds.json",
                    "gatus-endpoints.yaml", "OPERATOR.md", "../case_658bf6b9/MODULES.md")
MUTANTS = [
    ("queued-counts-as-hung", E, 'if run.get("status") != "in_progress":',
     'if run.get("status") not in ("in_progress", "queued", "waiting"):'),
    ("completed-counts-as-hung", E, 'if run.get("status") != "in_progress":',
     'if run.get("status") not in ("in_progress", "completed"):'),
    ("null-started-at-read-as-epoch-zero", E, "if started is None:\n            continue",
     'if started is None:\n            started = "1970-01-01T00:00:00Z"'),
    ("age-floor-removed", E, "AGE_FLOOR_SECONDS = 300", "AGE_FLOOR_SECONDS = 0"),
    ("superseded-head-sha-not-checked", E, "head != expect_sha", "False"),
    ("hung-boundary-inclusive", E, "age > (minutes + HUNG_GRACE_MINUTES) * 60",
     "age >= (minutes + HUNG_GRACE_MINUTES) * 60"),
    ("warn-boundary-inclusive", E, "age > minutes * WARN_SECONDS_PER_TIMEOUT_MINUTE",
     "age >= minutes * WARN_SECONDS_PER_TIMEOUT_MINUTE"),
    ("warn-factor-halved", E, "WARN_SECONDS_PER_TIMEOUT_MINUTE = 45", "WARN_SECONDS_PER_TIMEOUT_MINUTE = 30"),
    ("hung-grace-dropped", E, "HUNG_GRACE_MINUTES = 15", "HUNG_GRACE_MINUTES = 0"),
    ("warn-pages", E, "success = not (res.hung or res.failures)", "success = not (res.hung or res.failures or res.warn)"),
    ("unknown-threshold-silent", E, "res.unknown.append(item)", "pass"),
    ("unreadable-api-swallowed", E, 'res.failures.append(f"{where}: check-runs unreadable ({why})")', "pass"),
    ("malformed-json-swallowed", E, 'res.failures.append(f"{where}: check-runs JSON does not parse")', "pass"),
    ("missing-check-runs-list-swallowed", E, 'res.failures.append(f"{where}: check-runs JSON has no check_runs list")', "pass"),
    ("full-page-by-length-ignored", E, "len(runs) >= PAGE_SIZE or (", "("),
    ("full-page-by-total-count-ignored", E, "or (isinstance(total, int) and not isinstance(total, bool) and total > len(runs))", ""),
    ("bad-started-at-swallowed", E, 'res.failures.append(f"{where}: unparseable started_at on an in_progress check run")', "pass"),
    ("reads-an-actions-endpoint", E, "commits/{ref}/check-runs?per_page=", "actions/runs/{ref}?per_page="),
    ("pr-cap-off-by-one", E, "for pr in prs[:PR_CAP]:", "for pr in prs[:PR_CAP + 1]:"),
    ("pr-cap-bound-not-noted", E, "if len(prs) > PR_CAP:", "if len(prs) > PR_CAP + 100:"),
    ("main-head-not-polled", E, 'if not poll_sha(gh, repo, "main", "main", None, now, jobs, res):\n        return', "pass"),
    ("pr-heads-not-polled", E, 'if not poll_sha(gh, repo, f"PR#{number}", sha, sha, now, jobs, res):\n            return', "pass"),
    ("pr-list-unreadable-swallowed", E, 'res.failures.append(f"{repo}: open PR list unreadable ({why})")', "pass"),
    ("malformed-pr-entry-swallowed", E, 'res.failures.append(f"{repo}: open PR entry without an integer number and 40-hex head sha")', "pass"),
    ("non-object-check-run-swallowed", E, 'res.failures.append(f"{where}: check-runs entry is not an object")', "pass"),
    ("nameless-in-progress-run-swallowed", E, 'res.failures.append(f"{where}: in_progress check run without a string name and integer id")', "pass"),
    ("job-names-not-sanitised", E, 'return _UNSAFE.sub("?", str(value))[:limit]', "return str(value)[:limit]"),
    ("pr-head-sha-not-validated", E, " or not _SHA.match(sha)", ""),
    ("text-entry-cap-removed", E, "MAX_ITEMS = 4", "MAX_ITEMS = 99"),
    ("zero-minute-row-accepted", E, " or minutes <= 0", ""),
    ("push-always-true", P, 'gatus_push platform_ci-hung-job "$success" "$text" || exit 1',
     'gatus_push platform_ci-hung-job true "$text" || exit 1'),
    ("crash-pushes-true", P, 'gatus_push platform_ci-hung-job false "hung-job evaluator crashed (exit $rc)"',
     'gatus_push platform_ci-hung-job true "hung-job evaluator crashed (exit $rc)"'),
    ("garbage-output-pushes-true", P, 'gatus_push platform_ci-hung-job false "hung-job evaluator output unparseable"',
     'gatus_push platform_ci-hung-job true "hung-job evaluator output unparseable"'),
    ("evaluator-refusal-exits-0", P, "exit 2 ;;\n  *)", "exit 0 ;;\n  *)"),
    ("pushes-the-wrong-key", P, 'gatus_push platform_ci-hung-job "$success"', 'gatus_push platform_ci-hung-jab "$success"'),
    ("hung-exits-0", P, '[[ "$success" == "true" ]]\n', "true\n"),
    ("no-row-job-never-hung", E, "if not known:\n            minutes = DEFAULT_TIMEOUT_MINUTES",
     "if not known:\n            minutes = 10 ** 9"),
    ("no-row-default-timeout-shortened", E, "DEFAULT_TIMEOUT_MINUTES = 360", "DEFAULT_TIMEOUT_MINUTES = 359"),
    ("no-row-default-timeout-lengthened", E, "DEFAULT_TIMEOUT_MINUTES = 360", "DEFAULT_TIMEOUT_MINUTES = 361"),
    ("no-row-job-hung-without-grace", E, "if age > (minutes + HUNG_GRACE_MINUTES) * 60:\n            item[\"limit\"]",
     "if age > (minutes + HUNG_GRACE_MINUTES * known) * 60:\n            item[\"limit\"]"),
    ("ci-job-row-deleted", T, '    "dispatch suite": 20,\n', ""),
    ("ci-job-row-disagrees", T, '"Offline suites": 150', '"Offline suites": 151'),
    ("pr-lint-row-deleted", T, '    "pr-lint": 5,\n', ""),
    ("other-workflow-row-deleted", T, '    "first-pickup": 30,\n', ""),
    ("other-workflow-row-disagrees", T, '"docs-verdict": 5', '"docs-verdict": 6'),
    ("cron-payload-drops-export", O, "root set -a; . ", "root . "),
    ("cron-payload-runs-after-failed-source", O, "&& exec ", "; "),
    ("fragment-heartbeat-1h", F, "interval: 10m", "interval: 1h"),
    ("fragment-runbook-dropped", F, " RUNBOOK ops/case_e1e23b50/OPERATOR.md#hung-job-runbook", ""),
    ("cron-line-dropped", O, "/opt/status-page/current/ops/case_e1e23b50/push-ci-hung-job.sh", "(removed)"),
    ("registry-entry-dropped", M, "platform/ci-hung-job", "platform/ci-hung-jab"),
]


def build_copy(dst):
    for rel in ("ops/case_e1e23b50", "ops/case_658bf6b9/_gatus_push.sh", "ops/case_658bf6b9/MODULES.md",
                ".github/workflows"):
        src, out = os.path.join(HERE, rel), os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, out)
        else:
            shutil.copy2(src, out)
    gate = os.path.join(HERE, "ops", "case_658bf6b9")
    for extra in ("check_module_wiring.py", "module_wiring_allowlist.json"):
        if os.path.exists(os.path.join(gate, extra)):
            shutil.copy2(os.path.join(gate, extra), os.path.join(dst, "ops", "case_658bf6b9", extra))


def run_behaviour(root):
    env = dict(os.environ, HUNG_TEST_ROOT=root)
    p = subprocess.run([sys.executable, "-m", "unittest", "-f", "test_case_f9bc8d5a_hung_job.Behaviour"],
                       cwd=HERE, env=env, capture_output=True, text=True, timeout=900)
    return p.returncode, p.stderr[-1500:]


@unittest.skipIf(IS_MUTANT_COPY, "running inside a mutant copy")
class Mutants(unittest.TestCase):
    results = {}

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="case_f9bc8d5a-mut-")
        cls.addClassCleanup(shutil.rmtree, cls.base, True)
        jobs = [("CONTROL", None, None, None)] + [(m[0],) + m[1:] for m in MUTANTS]
        anchors = {}

        def prepare(job):
            name, rel, old, new = job[0], job[1], job[2], job[3]
            root = os.path.join(cls.base, name)
            build_copy(root)
            if rel is not None:
                path = os.path.normpath(os.path.join(root, "ops", "case_e1e23b50", rel))
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                anchors[name] = text.count(old)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text.replace(old, new, 1))
            return name, root

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            prepared = list(pool.map(prepare, jobs))
            cls.anchors = anchors
            futures = {name: pool.submit(run_behaviour, root) for name, root in prepared}
            for name, fut in futures.items():
                cls.results[name] = fut.result()

    def test_control_copy_is_green(self):
        rc, tail = self.results["CONTROL"]
        self.assertEqual(rc, 0, tail)

    def test_every_mutant_anchor_exists_exactly_where_expected(self):
        for name, *_ in MUTANTS:
            with self.subTest(name):
                self.assertGreaterEqual(self.anchors[name], 1, f"mutant anchor vanished: {name}")

    def test_every_mutant_turns_the_behaviour_suite_red(self):
        for name, *_ in MUTANTS:
            with self.subTest(name):
                rc, tail = self.results[name]
                self.assertNotEqual(rc, 0, f"mutant {name} SURVIVED: an invariant is asserted nowhere")


if __name__ == "__main__":
    unittest.main(verbosity=2)
