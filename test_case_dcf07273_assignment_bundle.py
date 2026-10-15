"""case_d5304306: assignment host source, slice 1 (focus, issues, reviews).

Fast tier: no database, no network, no credential. The SQL runs under a SQLite
translation (the case_b79f6544 pattern) against a fixture that uses the Postgres
column names. The builder, the normalizer and the detector then run as CLIs.
"""

import datetime
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HERE = ROOT / "ops" / "case_d5304306"
SQL_PATH = HERE / "assignment_evidence.sql"
BUILDER = HERE / "build_assignment_bundle.py"
NORMALIZER = ROOT / "watchdog" / "assignment_evidence.py"
DETECTOR = ROOT / "watchdog" / "detectors.py"

COMPANY = "00000000-0000-4000-8000-000000000004"
FOCUS_ISSUE = "00000000-0000-4000-8000-000000000126"
FOCUS_DOC = "00000000-0000-4000-8000-000000000184"
FOCUS_REVISION = "00000000-0000-4000-8000-000000000185"
PROJECT_MODEL = "00000000-0000-4000-8000-000000000034"
PROJECT_WEB = "00000000-0000-4000-8000-000000000032"
PROJECT_BOT = "00000000-0000-4000-8000-000000000033"
PROJECT_LEGACY = "00000000-0000-4000-8000-000000000186"
ROOTS = (
    "00000000-0000-4000-8000-000000000187",
    "00000000-0000-4000-8000-000000000188",
    "00000000-0000-4000-8000-000000000189",
    "00000000-0000-4000-8000-000000000190",
    "00000000-0000-4000-8000-000000000191",
)
AGENT = "00000000-0000-4000-8000-000000000056"
NOW = datetime.datetime(2026, 10, 9, 5, 0, 0, tzinfo=datetime.timezone.utc)
NOW_ARG = "2026-10-09T05:00:00Z"


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def ago(minutes=0, hours=0):
    moment = NOW - datetime.timedelta(minutes=minutes, hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def membership_body(projects, roots):
    lines = ["# Focus membership policy", "", "## Membership at v1", "",
             "| Scope | Id | Name | Directive item |", "|---|---|---|---|"]
    lines += [f"| P | `{p}` | project | 0 |" for p in projects]
    lines += [f"| R | `{r}` | root | 0 |" for r in roots]
    return "\n".join(lines) + "\n"


def issue(n, identifier, status, *, project=PROJECT_LEGACY, parent=None, agent=None,
          user=None, created=None, version=0, hidden=None):
    return {"id": uid(n), "company_id": COMPANY, "identifier": identifier,
            "parent_id": parent, "project_id": project, "status": status,
            "status_version": version, "created_at": created or ago(hours=2),
            "hidden_at": hidden, "assignee_agent_id": agent, "assignee_user_id": user}


def move(n, issue_n, minutes, to, frm, shape="previous", action="issue.updated"):
    if shape == "previous":
        details = {"status": to, "_previous": {"status": frm}}
    else:
        details = {"changes": {"status": {"from": frm, "to": to}}}
    return {"id": uid(n), "company_id": COMPANY, "entity_type": "issue",
            "entity_id": uid(issue_n), "action": action,
            "details": json.dumps(details), "created_at": ago(minutes=minutes)}


def fixture():
    roots = []
    for root_id, n in zip(ROOTS, range(101, 106)):
        row = issue(n, f"TOG-R{n - 101}", "in_progress", agent=AGENT, created=ago(hours=3))
        row["id"] = root_id
        roots.append(row)
    issues = roots + [
        issue(1, "case_8c5fe4cc", "backlog", project=PROJECT_MODEL),
        issue(2, "case_d02d0252", "todo", project=PROJECT_MODEL),
        issue(3, "case_a6dbab15", "backlog", project=PROJECT_MODEL, agent=AGENT),
        # version 1: checkout bumped status_version and logged only
        # issue.checked_out, which the SQL does not read as a status move.
        issue(4, "case_4ac9f045", "in_progress", parent=ROOTS[1], agent=AGENT, version=1),
        issue(5, "case_0dd2d987", "done", project=PROJECT_MODEL),
        issue(6, "case_15089482", "in_review", agent=AGENT),
        issue(7, "case_4ee01500", "todo"),
        issue(8, "case_1ef046c6", "backlog", project=PROJECT_MODEL, agent=AGENT, version=3),
        issue(9, "case_f052904a", "todo", project=PROJECT_MODEL, hidden=ago(hours=1)),
        issue(10, "case_38d056f3", "todo", project=PROJECT_MODEL, user="owner-user-1"),
        issue(11, "case_5ab07325", "backlog", project=PROJECT_WEB, agent=AGENT),
        issue(12, "case_de259302", "in_review", agent=AGENT),
        issue(13, "case_0a5489df", "cancelled", project=PROJECT_MODEL),
        issue(14, "case_2a0ef960", "paused", project=PROJECT_MODEL),
        issue(15, "case_5c700a98", "in_progress", project=PROJECT_BOT, agent=AGENT,
              version=1),
        issue(16, "case_7aede21a", "blocked", agent=AGENT),
        # case_d468cad2: checked out, so status_version 1 with no logged status move.
        issue(17, "case_d468cad2", "in_progress", project=PROJECT_MODEL, agent=AGENT,
              version=1),
        # case_2761dc9b: reassigned, so the last logged move (todo) no longer matches
        # the current status (in_progress).
        issue(18, "case_2761dc9b", "in_progress", project=PROJECT_MODEL, agent=AGENT,
              version=2),
    ]
    focus_issue = issue(14105, "case_58fddbd4", "blocked")
    focus_issue["id"] = FOCUS_ISSUE
    issues.append(focus_issue)
    activity = [
        move(1001, 3, 200, "todo", "backlog"),
        move(1002, 3, 90, "backlog", "todo"),
        move(1003, 4, 10, "in_progress", "todo", shape="changes"),
        move(1004, 11, 30, "backlog", "todo"),
        move(1005, 11, 5, "todo", "backlog", action="issue.comment_added"),
        move(1006, 15, 5, "in_progress", "todo"),
        move(1007, 15, 1, "in_progress", "in_progress"),
        move(1008, 6, 15, "in_review", "in_progress"),
        move(1009, 12, 40, "in_review", "in_progress"),
        move(1010, 18, 50, "todo", "backlog"),
    ]
    work_products = [
        {"id": uid(6000), "company_id": COMPANY, "issue_id": uid(6), "type": "pull_request",
         "status": "ready_for_review", "updated_at": ago(minutes=20)},
        {"id": uid(12000), "company_id": COMPANY, "issue_id": uid(12),
         "type": "pull_request", "status": "changes_requested", "updated_at": ago(minutes=5)},
        {"id": uid(3000), "company_id": COMPANY, "issue_id": uid(7), "type": "branch",
         "status": "ready_for_review", "updated_at": ago(minutes=5)},
    ]
    projects = [{"id": p, "company_id": COMPANY, "archived_at": None}
                for p in (PROJECT_MODEL, PROJECT_WEB, PROJECT_BOT, PROJECT_LEGACY)]
    return {
        "projects": projects,
        "issues": issues,
        "activity": activity,
        "work_products": work_products,
        "documents": [{"id": FOCUS_DOC, "company_id": COMPANY,
                       "latest_revision_id": FOCUS_REVISION, "latest_revision_number": 1}],
        "document_revisions": [{"id": FOCUS_REVISION, "company_id": COMPANY,
                                "document_id": FOCUS_DOC, "revision_number": 1,
                                "body": membership_body((PROJECT_MODEL, PROJECT_WEB, PROJECT_BOT),
                                                        ROOTS)}],
        "issue_documents": [{"id": uid(900), "company_id": COMPANY, "issue_id": FOCUS_ISSUE,
                             "document_id": FOCUS_DOC, "key": "focus-membership"}],
    }


SCHEMA = {
    "projects": "id TEXT, company_id TEXT, archived_at TEXT",
    "issues": ("id TEXT, company_id TEXT, identifier TEXT, parent_id TEXT, project_id TEXT, "
               "status TEXT, status_version INTEGER, created_at TEXT, hidden_at TEXT, "
               "assignee_agent_id TEXT, assignee_user_id TEXT"),
    "activity": "id TEXT, company_id TEXT, entity_type TEXT, entity_id TEXT, action TEXT, "
                "details TEXT, created_at TEXT",
    "work_products": "id TEXT, company_id TEXT, issue_id TEXT, type TEXT, status TEXT, "
                     "updated_at TEXT",
    "documents": "id TEXT, company_id TEXT, latest_revision_id TEXT, latest_revision_number INTEGER",
    "document_revisions": ("id TEXT, company_id TEXT, document_id TEXT, revision_number INTEGER, "
                           "body TEXT"),
    "issue_documents": "id TEXT, company_id TEXT, issue_id TEXT, document_id TEXT, key TEXT",
}
TABLE_NAMES = {"activity": "activity_log", "work_products": "issue_work_products"}


def write_fixture_db(path, data):
    con = sqlite3.connect(path)
    try:
        for key, columns in SCHEMA.items():
            name = TABLE_NAMES.get(key, key)
            con.execute(f"CREATE TABLE {name} ({columns})")
            rows = data[key]
            if not rows:
                continue
            names = list(rows[0].keys())
            placeholders = ", ".join("?" for _ in names)
            con.executemany(
                f"INSERT INTO {name} ({', '.join(names)}) VALUES ({placeholders})",
                [tuple(row[n] for n in names) for row in rows])
        con.commit()
    finally:
        con.close()


def translate(sql, company_id):
    body = re.sub(r"--[^\n]*", "", sql)
    body = body.replace(":'company_id'", f"'{company_id}'")
    body = body.replace("json_build_object(", "json_object(")
    return re.sub(
        r"([A-Za-z_][A-Za-z0-9_.]*)\s*#>>\s*'\{([^}]*)\}'",
        lambda m: f"json_extract({m.group(1)}, '$.{m.group(2).replace(',', '.')}')", body)


def run_sql(data):
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "fixture.sqlite")
        write_fixture_db(db, data)
        con = sqlite3.connect(db)
        try:
            cur = con.execute(translate(SQL_PATH.read_text(encoding="utf-8"), COMPANY))
            return [row[0] for row in cur.fetchall()]
        finally:
            con.close()


def run_pipeline(lines, tmp):
    rows_path = os.path.join(tmp, "rows.jsonl")
    with open(rows_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    bundle = os.path.join(tmp, "bundle.json")
    report = os.path.join(tmp, "report.json")
    proc = subprocess.run(
        [sys.executable, str(BUILDER), "--rows", rows_path, "--sql", str(SQL_PATH),
         "--bundle", bundle, "--report", report, "--now", NOW_ARG],
        capture_output=True, text=True, cwd=str(ROOT))
    return proc, bundle, report


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class SqlContract(unittest.TestCase):
    def setUp(self):
        self.body = re.sub(r"--[^\n]*", "", SQL_PATH.read_text(encoding="utf-8"))

    def test_one_read_only_statement(self):
        self.assertEqual(self.body.count(";"), 1)
        self.assertTrue(self.body.strip().endswith(";"))
        self.assertIsNone(re.search(
            r"\b(insert|update|delete|alter|create|drop|grant|revoke|truncate|copy|call|do|"
            r"merge|set|vacuum|lock|reindex|refresh)\b", self.body, re.IGNORECASE))

    def test_reads_only_the_allowed_tables(self):
        cte_names = set(re.findall(r"(\w+) AS \(", self.body))
        read = set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", self.body)) - cte_names
        self.assertEqual(read, {"issues", "projects", "issue_documents", "documents",
                                "document_revisions", "activity_log", "issue_work_products"})

    def test_never_reads_details_or_secret_columns(self):
        self.assertEqual(re.findall(r"details(?!\s*#>>)", self.body), [])
        self.assertIsNone(re.search(
            r"secret|token|password|api_key|adapter_config|runtime_config|\benv\b",
            self.body, re.IGNORECASE))

    def test_binds_the_company_through_the_psql_variable_only(self):
        self.assertGreaterEqual(self.body.count(":'company_id'"), 5)
        self.assertNotIn(COMPANY, self.body)


class SqlRuns(unittest.TestCase):
    def test_translated_sql_yields_all_three_row_kinds(self):
        data = fixture()
        kinds = [json.loads(line)["kind"] for line in run_sql(data)]
        self.assertEqual(kinds.count("focusDocument"), 1)
        self.assertEqual(kinds.count("project"), 4)
        self.assertEqual(kinds.count("issue"), len(data["issues"]))


class BuilderFixtureRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.proc, cls.bundle_path, cls.report_path = run_pipeline(
            run_sql(fixture()), cls.tmp.name)
        cls.bundle = load(cls.bundle_path)
        cls.report = load(cls.report_path)
        cls.rows = {row["id"]: row for row in cls.bundle["issues"]}
        cls.by_id = {row["identifier"]: row for row in cls.bundle["issues"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_sources_complete_with_refs(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        sources = self.bundle["sources"]
        self.assertTrue(all(sources[name]["complete"] for name in ("issues", "focus", "reviews")))
        self.assertEqual(sources["focus"]["ref"],
                         f"issue-doc:case_58fddbd4/focus-membership#{FOCUS_REVISION}")
        self.assertTrue(sources["issues"]["ref"].startswith("host-sql:assignment_evidence.sql@"))
        self.assertEqual(self.bundle["now"], NOW_ARG[:-1] + ".000Z")

    def test_focus_follows_the_rule(self):
        focused = {row["identifier"] for row in self.rows.values() if row.get("inFocus") is True}
        self.assertEqual(focused, {"case_d02d0252", "case_a6dbab15", "case_4ac9f045", "case_15089482", "case_1ef046c6", "case_38d056f3",
                                   "case_5ab07325", "case_5c700a98", "case_d468cad2", "case_2761dc9b",
                                   "TOG-R0", "TOG-R1", "TOG-R2",
                                   "TOG-R3", "TOG-R4"})
        for identifier in ("case_8c5fe4cc", "case_0dd2d987", "case_4ee01500", "case_f052904a", "case_de259302", "case_0a5489df", "case_7aede21a"):
            self.assertIs(self.by_id[identifier].get("inFocus"), False, identifier)
        self.assertEqual(self.report["counts"]["focusedByScope"]["review"], 1)
        self.assertEqual(self.report["counts"]["focusedByScope"]["root"], 6)

    def test_unknown_status_leaves_focus_unset(self):
        self.assertNotIn("inFocus", self.by_id["case_2a0ef960"])

    def test_backlog_minutes_come_from_recorded_history(self):
        self.assertEqual(self.by_id["case_a6dbab15"]["inBacklogMin"], 90.0)
        self.assertEqual(self.by_id["case_a6dbab15"]["provenance"]["inBacklogMin"],
                         {"source": "status-history", "ref": f"activity_log:{uid(1002)}"})
        self.assertEqual(self.by_id["case_5ab07325"]["inBacklogMin"], 30.0)
        self.assertEqual(self.by_id["case_5ab07325"]["provenance"]["inBacklogMin"]["ref"],
                         f"activity_log:{uid(1004)}")
        # case_1ef046c6: backlog assigned after creation (status_version 3, no logged
        # move) reads since created_at: assignee changes bump the version.
        self.assertEqual(self.by_id["case_1ef046c6"]["inBacklogMin"], 120.0)
        self.assertEqual(self.by_id["case_1ef046c6"]["provenance"]["inBacklogMin"]["ref"],
                         f"issue:{uid(8)}#created")

    def test_non_backlog_cards_are_zero_whatever_the_history_says(self):
        # Checkout (case_4ac9f045, case_5c700a98, case_d468cad2) and reassignment (case_2761dc9b) bump
        # status_version and leave no, or a mismatched, status move behind.
        for identifier in ("case_d02d0252", "case_4ac9f045", "case_5c700a98", "case_d468cad2", "case_2761dc9b"):
            with self.subTest(identifier):
                row = self.by_id[identifier]
                self.assertEqual(row["inBacklogMin"], 0.0)
                self.assertEqual(row["provenance"]["inBacklogMin"],
                                 {"source": "status-history",
                                  "ref": f"issue:{row['id']}#status"})

    def test_reviews_carry_request_or_inventory_provenance(self):
        self.assertIs(self.by_id["case_15089482"]["isReview"], True)
        self.assertEqual(self.by_id["case_15089482"]["provenance"]["isReview"],
                         {"source": "review-request", "ref": f"issue-work-product:{uid(6000)}"})
        self.assertNotIn("prAuthorAgentId", self.by_id["case_15089482"])
        self.assertIs(self.by_id["case_de259302"]["isReview"], False)
        self.assertEqual(self.by_id["case_de259302"]["provenance"]["isReview"]["source"],
                         "review-inventory")
        self.assertIs(self.by_id["case_4ee01500"]["isReview"], False)

    def test_assignee_fields_stay_honest(self):
        self.assertIsNone(self.by_id["case_d02d0252"]["assigneeAgentId"])
        self.assertEqual(self.by_id["case_a6dbab15"]["assigneeAgentId"], AGENT)
        self.assertNotIn("assigneeAgentId", self.by_id["case_38d056f3"])

    def test_hidden_card_is_out_of_focus(self):
        self.assertIs(self.by_id["case_f052904a"]["inFocus"], False)

    def test_output_is_private_and_never_overwritten(self):
        for path in (self.bundle_path, self.report_path):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        again = subprocess.run(
            [sys.executable, str(BUILDER), "--rows", "-", "--sql", str(SQL_PATH),
             "--bundle", self.bundle_path], input="", capture_output=True, text=True)
        self.assertEqual(again.returncode, 2)


class FailClosed(unittest.TestCase):
    def _run(self, data=None, lines=None):
        with tempfile.TemporaryDirectory() as tmp:
            proc, bundle, _ = run_pipeline(run_sql(data) if lines is None else lines, tmp)
            return proc, load(bundle)

    def test_focus_revision_that_does_not_resolve_is_incomplete(self):
        data = fixture()
        data["documents"][0]["latest_revision_id"] = uid(777)
        proc, bundle = self._run(data)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(bundle["sources"]["focus"]["complete"])
        self.assertTrue(all("inFocus" not in row for row in bundle["issues"]))

    def test_unresolved_listed_project_is_incomplete(self):
        data = fixture()
        data["projects"] = [p for p in data["projects"] if p["id"] != PROJECT_WEB]
        proc, bundle = self._run(data)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(bundle["sources"]["focus"]["complete"])

    def test_unresolved_listed_root_is_incomplete(self):
        data = fixture()
        data["issues"] = [r for r in data["issues"] if r["id"] != ROOTS[3]]
        proc, bundle = self._run(data)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(bundle["sources"]["focus"]["complete"])

    def test_parent_cycle_is_incomplete(self):
        data = fixture()
        by_id = {r["id"]: r for r in data["issues"]}
        by_id[uid(2)]["parent_id"] = uid(3)
        by_id[uid(3)]["parent_id"] = uid(2)
        proc, bundle = self._run(data)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(bundle["sources"]["focus"]["complete"])

    def test_unparseable_membership_line_is_incomplete(self):
        data = fixture()
        data["document_revisions"][0]["body"] += "| P | not-a-uuid | broken | 0 |\n"
        proc, bundle = self._run(data)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(bundle["sources"]["focus"]["complete"])

    def test_malformed_membership_scope_is_incomplete(self):
        scopes = ("p", "Q", "PR", "Project", "P1", "1", "`P`", "**P**", "",
                  "\u0420", "\uff30")
        for row in ("| P |", "| R |"):
            for scope in scopes:
                with self.subTest(row=row, scope=scope):
                    data = fixture()
                    body = data["document_revisions"][0]["body"]
                    data["document_revisions"][0]["body"] = body.replace(
                        row, f"| {scope} |", 1)
                    proc, bundle = self._run(data)
                    self.assertEqual(proc.returncode, 1)
                    self.assertFalse(bundle["sources"]["focus"]["complete"])

    def test_unreadable_input_writes_incomplete_bundle(self):
        proc, bundle = self._run(lines=["this is not json"])
        self.assertEqual(proc.returncode, 1)
        self.assertIsNone(bundle["issues"])
        self.assertTrue(all(not s["complete"] for s in bundle["sources"].values()))


class EndToEndVerdict(unittest.TestCase):
    def test_fixture_run_gives_a_real_assignment_verdict_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, bundle, _ = run_pipeline(run_sql(fixture()), tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            snapshot = os.path.join(tmp, "snapshot.json")
            normalized = subprocess.run(
                [sys.executable, str(NORMALIZER), "--input", bundle, "--out", snapshot],
                capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(normalized.returncode, 0, normalized.stderr)
            evidence = load(snapshot)["assignmentEvidence"]
            self.assertTrue(evidence["complete"])
            self.assertTrue(evidence["sources"]["focus"]["ref"].endswith(FOCUS_REVISION))
            verdict = subprocess.run(
                [sys.executable, str(DETECTOR), "--snapshot", snapshot],
                capture_output=True, text=True, cwd=str(ROOT))
        records = [json.loads(line) for line in verdict.stdout.splitlines() if line.strip()]
        assignment = [r for r in records if r.get("detector") == "watchdog/assignment"]
        unassigned = {r["identifier"]: r for r in assignment
                      if r.get("type") == "finding"
                      and r.get("dedupeKey", "").startswith("watchdog/assignment:unassigned-backlog:")}
        # case_1ef046c6 is the assigned-after-creation backlog card: measured at 120
        # minutes, so it now reads as a real backlog-over-1h verdict instead
        # of unknown.
        self.assertEqual(set(unassigned), {"case_d02d0252", "case_a6dbab15", "case_1ef046c6"})
        self.assertEqual(unassigned["case_a6dbab15"]["evidence"]["inBacklogMin"], 90.0)
        self.assertEqual(unassigned["case_1ef046c6"]["evidence"]["inBacklogMin"], 120.0)
        self.assertFalse(any(r.get("identifier") in ("case_4ac9f045", "case_5ab07325", "case_5c700a98",
                                                     "case_d468cad2", "case_2761dc9b")
                             and r.get("severity") != "unknown" for r in assignment))
        self.assertFalse(any("evidence incomplete" in r.get("reason", "") for r in assignment))
        self.assertTrue(any(r.get("severity") == "unknown" for r in assignment))
        self.assertFalse(any(r.get("dedupeKey", "").startswith("watchdog/assignment:review-author")
                             for r in assignment))


if __name__ == "__main__":
    unittest.main()
