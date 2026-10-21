#!/usr/bin/env bash
# Offline queue-depth contracts for the public status-page bundle. All gh and
# push traffic terminates in local fixtures; date parsing still uses real date.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 - "$HERE" <<'PY'
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(sys.argv.pop())
SOURCE = ROOT / "ops/case_658bf6b9"
NOW = "1767225600"  # 2026-01-01T00:00:00Z
OLD = "2020-01-01T00:00:00Z"
OLDER = "2019-01-01T00:00:00Z"

# One executable fixture dispatches by basename. Unknown calls fail closed;
# none of these shims can fall through to a real gh or HTTP request.
STUB = r'''
import json, os, sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

name = Path(sys.argv[0]).name
args = sys.argv[1:]
fx = os.environ["GH_FIXTURE"]
old = "2020-01-01T00:00:00Z"
if name == "date":
    if args == ["+%s"]:
        print(os.environ["FIXTURE_NOW"])
    elif fx == "cutoff-error" and args[:2] == ["-u", "-d"]:
        sys.exit(7)
    else:
        os.execv(os.environ["REAL_DATE"], ["date", *args])
elif name == "curl":
    assert args == ["--config", args[1], "-o", "/dev/null", "-w", "%{http_code}"]
    config = Path(args[1]).read_text().splitlines()
    url = next(line.split('"')[1] for line in config if line.startswith("url = "))
    parsed = urlsplit(url)
    assert parsed.netloc == "127.0.0.1:1"
    query = parse_qs(parsed.query)
    with open(os.environ["PUSH_LOG"], "a") as log:
        log.write(json.dumps({"path": parsed.path, "success": query["success"][0],
                              "error": query.get("error", [""])[0]}) + "\n")
    print("200")
elif name == "gh":
    with open(os.environ["GH_CALL_LOG"], "a") as log:
        log.write(json.dumps(args) + "\n")
    if args[:2] == ["run", "list"]:
        repo = args[args.index("--repo") + 1]
        status = args[args.index("--status") + 1]
        assert status in ("queued", "in_progress")
        assert args[args.index("--created") + 1] == "<2025-12-31T23:00:00Z"
        if fx == "queued-error" and status == "queued":
            sys.exit(7)
        if fx == "active-error" and status == "in_progress":
            sys.exit(8)
        if fx == "malformed" and status == "queued":
            print("not json")
            sys.exit(0)
        runs = [{"databaseId": 7, "createdAt": old}]
        if fx in ("empty", "queued-error", "active-error", "malformed"):
            runs = []
        elif fx == "burst":
            # Ignore --created deliberately: the client cutoff still excludes
            # 60 young runs. One queued run is accompanied by 15 active runs.
            ids = (7,) if status == "queued" else tuple(range(8, 23))
            runs = [{"databaseId": n, "createdAt": old if n == 7 else "2020-02-01T00:00:00Z"}
                    for n in ids]
            runs += [{"databaseId": 100+n, "createdAt": "2999-01-01T00:00:00Z"}
                     for n in range(60)]
            runs.reverse()
        elif fx == "xrepo" and repo == "o/b":
            runs[0]["createdAt"] = "2019-01-01T00:00:00Z"
        elif fx == "truncated":
            runs = runs * 2 if status == "queued" else []
        elif fx not in ("single", "xrepo", "jobs-error", "jobs-json", "bad-date", "below-threshold"):
            sys.exit(97)
        print(json.dumps(runs))
    elif len(args) == 2 and args[0] == "api":
        assert args[1] in ("repos/o/a/actions/runs/7/jobs?per_page=100",
                           "repos/o/b/actions/runs/7/jobs?per_page=100")
        if fx == "jobs-error":
            sys.exit(9)
        if fx == "jobs-json":
            print("not json")
        else:
            stamp = "2019-01-01T00:00:00Z" if fx == "xrepo" and "/o/b/" in args[1] else old
            if fx == "below-threshold":
                stamp = "2025-12-31T23:30:00Z"
            if fx == "bad-date":
                stamp = "not-a-date"
            print(json.dumps({"jobs": [{"name": "check", "status": "queued", "created_at": stamp}]}))
    else:
        sys.exit(98)
else:
    sys.exit(99)
'''

ERRORS = {
    "cutoff-error": "could not compute the run-age cutoff",
    "queued-error": "gh run list (queued) failed for o/a",
    "active-error": "gh run list (in_progress) failed for o/a",
    "malformed": "unparseable run list for o/a",
    "truncated": "run list hit QUEUE_AGE_RUN_LIMIT=2 for o/a; oldest runs unseen",
}


class QueueDepth(unittest.TestCase):
    def setUp(self):
        for tool in ("bash", "date", "jq", "python3"):
            self.assertIsNotNone(shutil.which(tool), f"required tool missing: {tool}")
        self.assertTrue((SOURCE / "_ci_queue_age.sh").is_file())
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        temp = tempfile.TemporaryDirectory(prefix="queue-age-depth-", dir=scratch)
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        for name in ("gh", "date", "curl"):
            shim = bin_dir / name
            shim.write_text(f"#!{sys.executable}\n" + STUB)
            shim.chmod(0o755)
        # Do not inherit credentials, BASH_ENV, shell options or production
        # settings. The only gh/curl available to the source are our fixtures.
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.work),
            "TMPDIR": str(self.work), "SOURCE": str(SOURCE / "_ci_queue_age.sh"),
            "REAL_DATE": shutil.which("date"), "FIXTURE_NOW": NOW,
            "GH_BIN": str(bin_dir / "gh"), "GH_FIXTURE": "empty",
            "GH_CALL_LOG": str(self.work / "gh.jsonl"), "PUSH_LOG": str(self.work / "push.jsonl"),
            "GH_REPOS": "o/a", "QUEUE_AGE_RUN_LIMIT": "1000",
            "GATUS_URL": "http://127.0.0.1:1", "GATUS_EXTERNAL_TOKEN": "T",
        }

    def helper(self, fixture, *, mode="on", calls=None, seed="", limit="1000"):
        if calls is None:
            calls = "ci_oldest_waiting_job 60 o/a || rc=$?"
        # A new bash, not a shell function invoked inside `if`/`||`: those
        # contexts suppress errexit throughout the function under test.
        script = "set -uo pipefail\nset " + ("-e" if mode == "on" else "+e") + r'''
. "$SOURCE"
source_opts="$-"
''' + seed + "\nrc=0\n" + calls + r'''
case "$-" in *e*) mode=on ;; *) mode=off ;; esac
case "$source_opts" in *e*) source_mode=on ;; *) source_mode=off ;; esac
printf '%s\n' "$rc" "${CQA_RUNS_INSPECTED-unset}" "${CQA_OLDEST-unset}" \
  "${CQA_OLDEST_REPO-unset}" "${CQA_JOBS_UNREADABLE-unset}" "${CQA_ERROR-unset}" \
  "${CQA_NOW-unset}" "$source_mode" "$mode" survived
'''
        env = dict(self.env, GH_FIXTURE=fixture, QUEUE_AGE_RUN_LIMIT=limit)
        result = subprocess.run(["bash", "-c", script], env=env, cwd=self.work,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        values = result.stdout.splitlines()
        self.assertEqual(len(values), 10, result.stdout + result.stderr)
        self.assertEqual(values[6:], [NOW, mode, mode, "survived"])
        return values[:6]

    def push(self, fixture, *, error="", rc=0, limit="1000", missing_env=False):
        log = Path(self.env["PUSH_LOG"])
        log.write_text("")
        env = dict(self.env, GH_FIXTURE=fixture, QUEUE_AGE_RUN_LIMIT=limit)
        if missing_env:
            env["GATUS_URL"] = ""
        result = subprocess.run(["bash", str(SOURCE / "push-ci-queue-age.sh")],
                                env=env, cwd=self.work, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, rc, result.stderr + result.stdout)
        pushes = [json.loads(line) for line in log.read_text().splitlines()]
        if missing_env:
            self.assertEqual(pushes, [])
            self.assertIn("gatus_push: refusing — GATUS_URL is unset", result.stderr)
        else:
            self.assertEqual(pushes, [{"path": "/api/v1/endpoints/platform_ci-queue-age/external",
                                      "success": "false" if error else "true", "error": error}])

    def test_01_initializes_and_resets_globals(self):
        want = ["0", "0", "", "", "0", ""]
        self.assertEqual(self.helper("empty"), want)
        seed = "CQA_RUNS_INSPECTED=99; CQA_OLDEST=stale; CQA_OLDEST_REPO=stale; CQA_JOBS_UNREADABLE=9; CQA_ERROR=stale"
        self.assertEqual(self.helper("empty", seed=seed), want)

    def test_02_single_run_depth_and_breach_text(self):
        self.assertEqual(self.helper("single"), ["0", "1", OLD, "o/a", "0", ""])
        # The fixture clock fixes the entire message, not just a loose suffix.
        self.push("single", error="oldest runner-waiting job 3156480m old in o/a (threshold 60m; 1 old queued run(s))")

    def test_03_burst_counts_queued_runs_not_active_runs(self):
        self.assertEqual(self.helper("burst"), ["0", "1", OLD, "o/a", "0", ""])
        calls = [json.loads(line) for line in Path(self.env["GH_CALL_LOG"]).read_text().splitlines()]
        self.assertEqual([args for args in calls if args[0] == "api"],
                         [["api", "repos/o/a/actions/runs/7/jobs?per_page=100"]])
        self.push("burst", error="oldest runner-waiting job 3156480m old in o/a (threshold 60m; 1 old queued run(s))")

    def test_04_accumulates_across_repositories(self):
        self.assertEqual(self.helper("xrepo", calls="ci_oldest_waiting_job 60 o/a o/b"),
                         ["0", "2", OLDER, "o/b", "0", ""])

    def test_05_empty_call_resets_populated_depth(self):
        calls = "ci_oldest_waiting_job 60 o/a\nexport GH_FIXTURE=empty\nci_oldest_waiting_job 60 o/a"
        self.assertEqual(self.helper("single", calls=calls), ["0", "0", "", "", "0", ""])
        self.push("empty")

    def test_06_exact_run_list_error_contracts(self):
        for fixture, error in ERRORS.items():
            with self.subTest(fixture=fixture):
                limit = "2" if fixture == "truncated" else "1000"
                self.assertEqual(self.helper(fixture, seed="CQA_RUNS_INSPECTED=99", limit=limit),
                                 ["1", "0", "", "", "0", error])
                self.push(fixture, error=error, rc=1, limit=limit)
                # A refused failure push returns 2; the cron must still exit 1.
                self.push(fixture, rc=1, limit=limit, missing_env=True)

    def test_07_jobs_and_timestamp_error_contracts(self):
        for fixture in ("jobs-error", "jobs-json"):
            with self.subTest(fixture=fixture):
                self.assertEqual(self.helper(fixture), ["0", "1", "", "", "1", ""])
                self.push(fixture, error="jobs API unreadable for 1 run(s)", rc=1)
                self.push(fixture, rc=1, missing_env=True)
        self.push("bad-date", error="unparseable timestamp not-a-date", rc=1)
        self.push("bad-date", rc=1, missing_env=True)

    def check_caller(self, mode):
        for fixture in ("empty", "single", "jobs-error", *ERRORS):
            with self.subTest(mode=mode, fixture=fixture):
                expected_rc = "1" if fixture in ERRORS else "0"
                # -e callers guard expected failures, but call success directly.
                # +e callers capture even failures UNGUARDED, proving the helper
                # restores +e before returning instead of killing the caller.
                calls = "ci_oldest_waiting_job 60 o/a"
                calls += " || rc=$?" if mode == "on" and expected_rc == "1" else "\nrc=$?"
                values = self.helper(fixture, mode=mode, calls=calls,
                                     limit="2" if fixture == "truncated" else "1000")
                self.assertEqual(values[0], expected_rc)
                self.assertEqual(values[5], ERRORS.get(fixture, ""))

    def test_08_errexit_on_caller_survives(self):
        self.check_caller("on")

    def test_09_errexit_off_caller_survives(self):
        self.check_caller("off")

    def test_10_healthy_push_failures_are_not_swallowed(self):
        self.push("empty")
        self.push("empty", rc=2, missing_env=True)
        self.push("below-threshold")
        self.push("below-threshold", rc=2, missing_env=True)


unittest.main(verbosity=2)
PY
