#!/usr/bin/env python3
"""Offline suite for the fleet run-stall alarm (case_b79f6544).

No network, no credential, no database server. Three layers:

  1. REPLAY. fleet_run_stall.sql is translated mechanically (rules in its
     header) to sqlite with a pinned clock and replayed against run counts
     measured on 2026-10-08: created per UTC hour 15h=108, 16h=197, 17h=64,
     18h=4, 19h=0, 20h=0. 18h uses the four real creation times; 15h, 16h and
     17h are spread evenly, plus the 17:08:37Z row that started at 18:02:05Z.
     21h is synthetic: 92 creations from 21:11:10Z to 21:33Z (measured: 179).
     At every five-minute cron tick the database holds only the rows that
     existed at that instant. The probe must be quiet through the normal hours
     (16h=197 above all) and fire on every tick from 18:25Z to 21:10Z, through
     the 18:02Z-21:11Z gap. UTC-night cases mirror it: a quiet 00:00-06:00Z
     stretch at a sparser-than-day cadence (no false page) and an overnight
     stall with last activity 02:00:05Z that first fires at 02:25Z, plus a
     23:47Z-00:10Z midnight crossing that behaves exactly like its daytime
     analog. Controls prove each condition of the rule can fail,
     and each mutant of the SQL must turn a named property red.
  2. STATIC. One read-only SELECT, no other verb; Gatus fragment, runbook anchor
     and registry entry agree with it.
  3. PUSH SCRIPT. psql and curl are PATH stubs. Healthy pushes true, a stall
     pushes false with the numbers, every unreadable shape pushes false, missing
     env refuses with no push, and neither the database URL nor the Gatus token
     ever reaches argv.

Run:  python3 -m unittest test_case_da79e241_fleet_run_stall
"""
import datetime as dt
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MOD = ROOT / "ops" / "case_b79f6544"
SQL_FILE = MOD / "fleet_run_stall.sql"
PUSH = MOD / "push-fleet-run-stall.sh"
YAML = MOD / "gatus-endpoints.yaml"
OPDOC = MOD / "OPERATOR.md"
REGISTRY = ROOT / "ops" / "case_658bf6b9" / "MODULES.md"

UTC = dt.timezone.utc
DAY = dt.datetime(2026, 10, 8, tzinfo=UTC)


def at(hour, minute=0, second=0):
    return DAY.replace(hour=hour, minute=minute, second=second)


# --------------------------------------------------------------------------
# The measured 2026-10-08 run timeline: (created_at, started_at or None).
# --------------------------------------------------------------------------
def spread(hour, count, first=None, until=None):
    """`count` evenly spaced creations in an hour, each started two seconds on."""
    first = first or at(hour)
    until = until or at(hour) + dt.timedelta(hours=1)
    step = (until - first) / count
    return [first + step * i for i in range(count)]


def incident_runs():
    created = []
    created += spread(15, 108)
    created += spread(16, 197)
    created += spread(17, 63)
    # 18h=4: the real creation times; 17h's 64th row is created at 17:08:37Z
    # but only started at 18:02:05Z, the last start before the gap.
    created += [at(18, 0, 20), at(18, 0, 21), at(18, 1, 20), at(18, 1, 57)]
    # 19h=0 and 20h=0.
    created += spread(21, 92, first=at(21, 11, 10), until=at(21, 33, 0))
    rows = [(c, c + dt.timedelta(seconds=2)) for c in created]
    rows.append((at(17, 8, 37), at(18, 2, 5)))
    return rows


def per_hour(rows):
    out = {}
    for created, _ in rows:
        out[created.hour] = out.get(created.hour, 0) + 1
    return out


# --------------------------------------------------------------------------
# UTC-night fixtures (case_fb090a82): the 2026-10-08 sample has no night hours,
# so these are synthetic. They pin the 20-min window to behave identically
# day and night, including across the UTC-midnight date rollover.
# --------------------------------------------------------------------------
def overnight_healthy_runs():
    """Sparse but alive night: a run every 12 min 00:00-06:00Z, started 2s on.

    Deliberately off-phase with the 5-minute cron and sparser than the
    measured daytime cadence, so a quiet replay proves the window does not
    false-page on a low-volume idle-but-alive night.
    """
    runs = []
    moment = at(0, 0)
    while moment <= at(6, 0):
        runs.append((moment, moment + dt.timedelta(seconds=2)))
        moment += dt.timedelta(minutes=12)
    return runs


def overnight_stall_runs():
    """Night stall mirroring the 18:02Z gap: steady runs, last start 02:00:05Z.

    Pre-gap runs every 10 min 00:00-01:50Z plus the final row created at
    02:00:00Z and started at 02:00:05Z (the 5 s created/started split mirrors
    the incident's 18:01:57Z/18:02:05Z pair); dispatch resumes 03:00Z.
    """
    runs = []
    moment = at(0, 0)
    while moment <= at(1, 50):
        runs.append((moment, moment + dt.timedelta(seconds=2)))
        moment += dt.timedelta(minutes=10)
    runs.append((at(2, 0, 0), at(2, 0, 5)))
    moment = at(3, 0)
    while moment <= at(4, 0):
        runs.append((moment, moment + dt.timedelta(seconds=2)))
        moment += dt.timedelta(minutes=10)
    return runs


PREV_DAY = DAY - dt.timedelta(days=1)


def at_prev(hour, minute=0, second=0):
    return PREV_DAY.replace(hour=hour, minute=minute, second=second)


def midnight_crossing_runs():
    """One row started 23:47:02Z the previous day; nothing after."""
    return [(at_prev(23, 47, 0), at_prev(23, 47, 2))]


def daytime_analog_runs():
    """Same shape as midnight_crossing_runs, shifted to 10:00Z for comparison."""
    return [(at(10, 0, 0), at(10, 0, 2))]


# --------------------------------------------------------------------------
# SQL translation, the subset documented in fleet_run_stall.sql.
# --------------------------------------------------------------------------
def translate(sql, now):
    sql = "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())
    lit = "'%s'" % now.strftime("%Y-%m-%d %H:%M:%S")
    sql = re.sub(r"now\(\)\s*-\s*INTERVAL\s*'(\d+) minutes'",
                 lambda m: "datetime(%s, '-%s minutes')" % (lit, m.group(1)), sql)
    sql = re.sub(r"EXTRACT\(EPOCH FROM \(now\(\) - ([\w.]+)\)\)",
                 lambda m: "(strftime('%%s', %s) - strftime('%%s', %s))" % (lit, m.group(1)), sql)
    sql = sql.replace("now()", lit)
    for leftover in ("now()", "INTERVAL", "EXTRACT"):
        assert leftover not in sql, "untranslated %s" % leftover
    return sql


SCHEMA = """
CREATE TABLE agents (id text PRIMARY KEY, status text NOT NULL);
CREATE TABLE issues (id text PRIMARY KEY, status text NOT NULL,
                     assignee_agent_id text, hidden_at text);
CREATE TABLE heartbeat_runs (id integer PRIMARY KEY, created_at text NOT NULL,
                             started_at text, finished_at text);
"""
AGENTS = [("idle-agent", "idle"), ("busy-agent", "running"), ("errored-agent", "error"),
          ("paused-agent", "paused"), ("gone-agent", "terminated"),
          ("pending-agent", "pending_approval")]
WORKING = [("c-todo", "todo", "idle-agent", None)]  # one assigned todo card


def ts(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else None


def probe(sql, now, runs, issues=WORKING, finished=None):
    """Rows the SELECT returns when the database holds what existed at `now`."""
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO agents VALUES (?, ?)", AGENTS)
    db.executemany("INSERT INTO issues VALUES (?, ?, ?, ?)", issues)
    for i, (created, started) in enumerate(runs):
        if created > now:
            continue
        seen_start = started if started and started <= now else None
        db.execute("INSERT INTO heartbeat_runs VALUES (?, ?, ?, ?)",
                   (i, ts(created), ts(seen_start), ts(finished)))
    rows = db.execute(translate(sql, now).rstrip().rstrip(";")).fetchall()
    db.close()
    return rows


def ticks(first, last, step_min=5):
    now = first
    while now <= last:
        yield now
        now += dt.timedelta(minutes=step_min)


def properties(sql):
    """Named checks over the incident replay plus the rule's controls."""
    runs = incident_runs()
    fired = [t for t in ticks(at(15, 5), at(21, 30)) if probe(sql, t, runs)]
    night_runs = overnight_stall_runs()
    night_fired = [t for t in ticks(at(1, 30), at(3, 10)) if probe(sql, t, night_runs)]
    healthy_night = overnight_healthy_runs()
    night_quiet = [t for t in ticks(at(0, 5), at(6, 0)) if probe(sql, t, healthy_night)]
    midnight_runs = midnight_crossing_runs()
    analog_runs = daytime_analog_runs()
    quiet_16h = [t for t in ticks(at(16, 0), at(16, 55)) if probe(sql, t, runs)]
    mid = probe(sql, at(20, 0), runs)
    stalled_now = at(20, 0)
    one_old_run = [(at(18, 1, 57), at(18, 1, 58))]
    props = {
        "replay_no_fire_before_the_gap":
            not [t for t in fired if t < at(18, 22)],
        "replay_16h_197_runs_is_quiet": not quiet_16h,
        "replay_first_fire_is_1825":
            bool(fired) and fired[0] == at(18, 25),
        "replay_fires_through_to_2110":
            bool(fired) and fired[-1] == at(21, 10)
            and len(fired) == 34,  # 18:25..21:10 every 5 min, no hole
        "replay_quiet_after_the_restart_rewake":
            not [t for t in ticks(at(21, 15), at(21, 30)) if probe(sql, t, runs)],
        "replay_reports_gap_and_cards":
            len(mid) == 1 and mid[0][0] == 1 and mid[0][2] == 117
            and mid[0][1].startswith("2026-10-08 18:02:05"),
        # Overnight (case_fb090a82): the 20-min window behaves identically at night.
        "overnight_quiet_stretch_is_quiet": not night_quiet,
        "overnight_no_fire_before_the_gap":
            not [t for t in night_fired if t < at(2, 22)],
        "overnight_stall_first_fire_is_0225":
            bool(night_fired) and night_fired[0] == at(2, 25),
        "overnight_stall_fires_through_to_0255":
            bool(night_fired) and night_fired[-1] == at(2, 55)
            and len(night_fired) == 7,  # 02:25..02:55 every 5 min, no hole
        "overnight_quiet_after_the_rewake":
            not [t for t in ticks(at(3, 0), at(3, 10)) if probe(sql, t, night_runs)],
        "overnight_reports_gap_and_cards":
            len(probe(sql, at(2, 40), night_runs)) == 1
            and probe(sql, at(2, 40), night_runs)[0][1] == "2026-10-08 02:00:05",
        # Midnight crossing: 23:47Z -> 00:05Z quiet / 00:10Z fires, exactly
        # like the 10:00Z daytime analog (no date-rollover bug in SQL/harness).
        # Rows carry their own timestamps, so compare the verdict (quiet vs
        # one firing row) and the computed whole-minute gap, not the raw rows.
        "midnight_crossing_matches_daytime":
            probe(sql, at(0, 5), midnight_runs) == []
            and probe(sql, at(10, 18), analog_runs) == []
            and len(probe(sql, at(0, 10), midnight_runs)) == 1
            and len(probe(sql, at(10, 23), analog_runs)) == 1
            and probe(sql, at(0, 10), midnight_runs)[0][0]
            == probe(sql, at(10, 23), analog_runs)[0][0] == 1
            and probe(sql, at(0, 10), midnight_runs)[0][2]
            == probe(sql, at(10, 23), analog_runs)[0][2] == 22,
        # Controls: each condition of the rule can fail on its own.
        "control_no_waiting_cards_is_quiet":
            not probe(sql, stalled_now, one_old_run,
                      issues=[("c-done", "done", "idle-agent", None),
                              ("c-unassigned", "todo", None, None),
                              ("c-review", "in_review", "idle-agent", None),
                              ("c-blocked", "blocked", "idle-agent", None)]),
        "control_paused_or_gone_assignee_is_quiet":
            not probe(sql, stalled_now, one_old_run,
                      issues=[("c1", "todo", "paused-agent", None),
                              ("c2", "in_progress", "gone-agent", None),
                              ("c3", "todo", "pending-agent", None)]),
        "control_hidden_card_is_quiet":
            not probe(sql, stalled_now, one_old_run,
                      issues=[("c-hidden", "todo", "idle-agent", "2026-10-08 10:00:00")]),
        "control_in_progress_card_fires":
            bool(probe(sql, stalled_now, one_old_run,
                       issues=[("c-ip", "in_progress", "busy-agent", None)])),
        "control_errored_agent_card_fires":
            bool(probe(sql, stalled_now, one_old_run,
                       issues=[("c-err", "todo", "errored-agent", None)])),
        "control_started_run_counts_as_alive":
            not probe(sql, stalled_now,
                      [(at(17, 0), stalled_now - dt.timedelta(minutes=5))]),
        "control_finishing_a_run_is_not_activity":
            bool(probe(sql, stalled_now, one_old_run,
                       finished=stalled_now - dt.timedelta(minutes=2))),
        "control_no_run_rows_at_all_fires_with_empty_gap":
            probe(sql, stalled_now, []) == [(1, None, None)],
        "control_boundary_19m_quiet_21m_fires":
            not probe(sql, stalled_now, [(stalled_now - dt.timedelta(minutes=19),) * 2])
            and bool(probe(sql, stalled_now,
                           [(stalled_now - dt.timedelta(minutes=21),) * 2])),
    }
    return props


class ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = SQL_FILE.read_text()

    def test_fixture_hourly_counts_as_documented(self):
        # 21h=92 is synthetic; the measured hour is 179 (see incident_runs()).
        counts = per_hour(incident_runs())
        self.assertEqual({h: counts.get(h, 0) for h in range(15, 22)},
                         {15: 108, 16: 197, 17: 64, 18: 4, 19: 0, 20: 0, 21: 92})

    def test_every_property_holds_on_the_checked_in_sql(self):
        bad = [name for name, ok in properties(self.sql).items() if not ok]
        self.assertEqual(bad, [])

    def test_normal_hour_is_quiet_and_gap_hours_fire(self):
        runs = incident_runs()
        for tick in ticks(at(16, 0), at(16, 55)):  # 16h = 197 runs
            self.assertEqual(probe(self.sql, tick, runs), [], tick)
        for hour in (19, 20):  # 0 runs created
            for tick in ticks(at(hour, 0), at(hour, 55)):
                self.assertEqual(len(probe(self.sql, tick, runs)), 1, tick)

    def test_overnight_quiet_stretch_never_pages(self):
        # Sparse night (a run every 12 min, off-phase with the cron) stays
        # quiet on every tick 00:00-06:00Z: an alive-but-idle night is not
        # a stall.
        runs = overnight_healthy_runs()
        for tick in ticks(at(0, 5), at(6, 0)):
            self.assertEqual(probe(self.sql, tick, runs), [], tick)

    def test_overnight_stall_fires_at_the_20_min_boundary(self):
        # Last activity 02:00:05Z: 02:20Z is still quiet (19:55 gap) and the
        # first firing tick is 02:25Z, firing through 02:55Z, quiet again
        # once dispatch resumes at 03:00Z -- the night mirror of 18:25-21:10Z.
        runs = overnight_stall_runs()
        self.assertEqual(probe(self.sql, at(2, 20), runs), [])
        self.assertEqual(len(probe(self.sql, at(2, 25), runs)), 1)
        for tick in ticks(at(2, 25), at(2, 55)):
            self.assertEqual(len(probe(self.sql, tick, runs)), 1, tick)
        for tick in ticks(at(3, 0), at(3, 10)):
            self.assertEqual(probe(self.sql, tick, runs), [], tick)

    def test_midnight_crossing_behaves_like_daytime(self):
        # 23:47:02Z previous day: 00:05Z quiet, 00:10Z fires with a 22 min
        # gap -- identical verdicts and gaps to the 10:00Z daytime analog.
        midnight_runs = midnight_crossing_runs()
        analog_runs = daytime_analog_runs()
        self.assertEqual(probe(self.sql, at(0, 5), midnight_runs), [])
        self.assertEqual(probe(self.sql, at(10, 18), analog_runs), [])
        night_fire = probe(self.sql, at(0, 10), midnight_runs)
        day_fire = probe(self.sql, at(10, 23), analog_runs)
        self.assertEqual(len(night_fire), 1)
        self.assertEqual(len(day_fire), 1)
        self.assertEqual(night_fire[0][0], day_fire[0][0])
        self.assertEqual(night_fire[0][2], day_fire[0][2])

    def test_mutants_are_killed(self):
        mutants = {
            "window widened to 200 minutes":
                (self.sql.replace("INTERVAL '20 minutes'", "INTERVAL '200 minutes'"),
                 "replay_first_fire_is_1825"),
            "started runs ignored":
                (self.sql.replace("    UNION ALL\n    SELECT started_at AS ts FROM heartbeat_runs\n", ""),
                 "control_started_run_counts_as_alive"),
            "waiting-card requirement dropped":
                (self.sql.replace("WHERE w.waiting_cards > 0\n  AND (", "WHERE ("),
                 "control_no_waiting_cards_is_quiet"),
            "paused agents counted":
                (self.sql.replace("a.status NOT IN ('paused', 'terminated', 'pending_approval')",
                                  "a.status IS NOT NULL"),
                 "control_paused_or_gone_assignee_is_quiet"),
            "window narrowed to 10 minutes":
                (self.sql.replace("INTERVAL '20 minutes'", "INTERVAL '10 minutes'"),
                 "control_boundary_19m_quiet_21m_fires"),
            "window shrunk to 19 minutes fires the overnight stall early":
                (self.sql.replace("INTERVAL '20 minutes'", "INTERVAL '19 minutes'"),
                 "overnight_stall_first_fire_is_0225"),
            "hidden cards counted":
                (self.sql.replace("    AND i.hidden_at IS NULL\n", ""),
                 "control_hidden_card_is_quiet"),
            "in_progress cards ignored":
                (self.sql.replace("IN ('todo', 'in_progress')", "IN ('todo')"),
                 "control_in_progress_card_fires"),
        }
        for label, (mutant, killer) in mutants.items():
            with self.subTest(mutant=label):
                self.assertNotEqual(mutant, self.sql, "mutation did not apply")
                self.assertFalse(properties(mutant)[killer],
                                 "%s survived: %s still holds" % (label, killer))


class StaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = SQL_FILE.read_text()
        cls.code = "\n".join(re.sub(r"--.*$", "", line) for line in cls.sql.splitlines())

    def test_one_read_only_select(self):
        stripped = self.code.strip()
        self.assertTrue(stripped.upper().startswith("SELECT"))
        self.assertEqual(stripped.count(";"), 1)
        banned = r"\b(insert|update|delete|merge|truncate|drop|alter|grant|revoke|copy|vacuum|create|into|call|do)\b"
        self.assertIsNone(re.search(banned, self.code, re.I), self.code)
        self.assertIsNone(re.search(r"\b(password|secret|token|bearer)\b", self.code, re.I))

    def test_reads_only_three_tables(self):
        tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", self.code, re.I))
        self.assertEqual(tables, {"issues", "agents", "heartbeat_runs"})

    def test_window_is_20_minutes_and_stated_in_the_alert(self):
        self.assertIn("INTERVAL '20 minutes'", self.code)
        self.assertIn("20m", YAML.read_text())

    def test_gatus_fragment_shape(self):
        text = YAML.read_text()
        self.assertEqual(len(re.findall(r"(?m)^  - name: ", text)), 1)
        self.assertIn("name: fleet-run-stall", text)
        self.assertIn("group: paperclip", text)
        self.assertIn("interval: 10m", text)
        self.assertEqual(len(re.findall(r"failure-threshold: 2", text)), 2)
        for alert_type in ("discord", "custom"):
            self.assertIn("- type: %s" % alert_type, text)
        self.assertIn('token: "${GATUS_EXTERNAL_TOKEN}"', text)
        self.assertIn("OWNER devops", text)
        self.assertIsNone(re.search(r"://", text), "URL literal in the fragment")

    def test_runbook_anchor_cron_line_and_registry(self):
        doc = OPDOC.read_text()
        self.assertIn("### fleet-run-stall", doc)
        self.assertIn("OPERATOR.md#fleet-run-stall", YAML.read_text())
        cron = ("*/5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env "
                "&& exec /opt/ops/paperclip-ops/ops/case_b79f6544/"
                "push-fleet-run-stall.sh")
        self.assertIn(cron, doc)
        self.assertIn(cron, PUSH.read_text())
        self.assertIn(
            "( set -a; . /root/.config/paperclip/case_60330d6f.env; set +a; "
            "/opt/ops/paperclip-ops/ops/case_b79f6544/"
            "push-fleet-run-stall.sh )", doc)
        self.assertIn("Rollback: delete `/etc/cron.d/paperclip-fleet-run-stall`", doc)
        registry = REGISTRY.read_text()
        self.assertIn("paperclip_fleet-run-stall", registry)
        self.assertIn("test_case_da79e241_fleet_run_stall.py", registry)

    def test_push_script_is_executable_and_keys_the_endpoint(self):
        self.assertTrue(PUSH.stat().st_mode & stat.S_IXUSR)
        self.assertIn('KEY="paperclip_fleet-run-stall"', PUSH.read_text())


# --------------------------------------------------------------------------
# Push script against PATH stubs.
# --------------------------------------------------------------------------
DB_PASSWORD = "Pw-FAKE-0123-not-a-secret"
GATUS_TOKEN = "tok-FAKE-4567-not-a-secret"
DB_URL = "postgres://ro_user:%s@host-609b79ea.example.invalid.invalid:5432/paperclip" % DB_PASSWORD

PSQL_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_DIR/psql.argv"
cat > "$STUB_DIR/psql.stdin"
printf '%s' "${PGOPTIONS:-}" > "$STUB_DIR/psql.pgoptions"
printf '%s|%s|%s|%s' "${PGUSER:-}" "${PGPASSWORD:-}" "${PGHOST:-}" "${PGDATABASE:-}" > "$STUB_DIR/psql.pgenv"
if [[ -f "$STUB_DIR/psql.out" ]]; then cat "$STUB_DIR/psql.out"; fi
exit "$(cat "$STUB_DIR/psql.rc" 2>/dev/null || echo 0)"
"""

CURL_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$@" >> "$STUB_DIR/curl.argv"
cfg=""
while (($#)); do
  if [[ "$1" == "--config" ]]; then cfg="$2"; shift; fi
  shift
done
if [[ -n "$cfg" ]]; then { cat "$cfg"; echo '---'; } >> "$STUB_DIR/curl.cfg"; fi
printf '200'
"""

# Container-local RO mode (case_d8c8a10f): `docker exec` into the DB container as
# explicit monitor_ro. Records argv (must never carry the password), stdin
# (must be the checked-in SELECT), and the env the password travels by.
DOCKER_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_DIR/docker.argv"
cat > "$STUB_DIR/docker.stdin"
printf '%s' "${PGOPTIONS:-}" > "$STUB_DIR/docker.pgoptions"
printf '%s|%s|%s|%s' "${PAPERCLIP_RO_DB_USER:-}" "${PGPASSWORD:-}" "${PAPERCLIP_RO_DB_CTR:-}" "${PAPERCLIP_RO_DB_NAME:-}" > "$STUB_DIR/docker.pgenv"
if [[ -f "$STUB_DIR/docker.out" ]]; then cat "$STUB_DIR/docker.out"; fi
exit "$(cat "$STUB_DIR/docker.rc" 2>/dev/null || echo 0)"
"""


class PushScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, body in (("psql", PSQL_STUB), ("curl", CURL_STUB),
                           ("docker", DOCKER_STUB)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o700)
        self.addCleanup(self.tmp.cleanup)

    def run_push(self, psql_out=None, psql_rc=0, drop=()):
        stub = self.dir
        for name in ("psql.out", "psql.rc", "curl.argv", "curl.cfg", "psql.argv"):
            (stub / name).unlink(missing_ok=True)
        if psql_out is not None:
            (stub / "psql.out").write_text(psql_out)
        (stub / "psql.rc").write_text(str(psql_rc))
        env = {
            "PATH": "%s:%s" % (self.bin, os.environ["PATH"]),
            "HOME": str(self.dir),
            "STUB_DIR": str(stub),
            "TMPDIR": str(self.dir),
            "PAPERCLIP_RO_DB_URL": DB_URL,
            "GATUS_URL": "https://gatus.invalid",
            "GATUS_EXTERNAL_TOKEN": GATUS_TOKEN,
        }
        for name in drop:
            env.pop(name)
        proc = subprocess.run(["bash", str(PUSH)], env=env, capture_output=True,
                              text=True, timeout=60)
        cfg = (stub / "curl.cfg").read_text() if (stub / "curl.cfg").exists() else ""
        pushes = [c for c in cfg.split("---\n") if c.strip()]
        return proc, pushes

    @staticmethod
    def pushed(push):
        url = re.search(r'url = "([^"]+)"', push).group(1)
        from urllib.parse import parse_qs, unquote, urlsplit
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        return {"path": parts.path, "success": query["success"][0],
                "error": unquote(query.get("error", [""])[0])}

    def run_push_ro(self, docker_out=None, docker_rc=0, drop=(), user="monitor_ro"):
        stub = self.dir
        for name in ("docker.out", "docker.rc", "curl.argv", "curl.cfg",
                     "docker.argv", "docker.stdin", "docker.pgoptions",
                     "docker.pgenv"):
            (stub / name).unlink(missing_ok=True)
        if docker_out is not None:
            (stub / "docker.out").write_text(docker_out)
        (stub / "docker.rc").write_text(str(docker_rc))
        env = {
            "PATH": "%s:%s" % (self.bin, os.environ["PATH"]),
            "HOME": str(self.dir),
            "STUB_DIR": str(stub),
            "TMPDIR": str(self.dir),
            "PAPERCLIP_RO_DB_USER": user,
            "PAPERCLIP_RO_DB_CTR": "paperclip-db",
            "PAPERCLIP_RO_DB_NAME": "paperclip",
            "PAPERCLIP_MONITOR_RO_PASSWORD": DB_PASSWORD,
            "GATUS_URL": "https://gatus.invalid",
            "GATUS_EXTERNAL_TOKEN": GATUS_TOKEN,
        }
        for name in drop:
            env.pop(name, None)
        proc = subprocess.run(["bash", str(PUSH)], env=env, capture_output=True,
                              text=True, timeout=60)
        cfg = (stub / "curl.cfg").read_text() if (stub / "curl.cfg").exists() else ""
        pushes = [c for c in cfg.split("---\n") if c.strip()]
        return proc, pushes

    def assertNothingLeaks(self, proc, pushes):
        argv = ""
        for name in ("psql.argv", "curl.argv", "docker.argv"):
            path = self.dir / name
            argv += path.read_text() if path.exists() else ""
        everything = argv + proc.stdout + proc.stderr
        self.assertNotIn(DB_PASSWORD, everything)
        self.assertNotIn(GATUS_TOKEN, everything)
        self.assertNotIn("host-609b79ea.example.invalid.invalid", argv)

    def test_healthy_fleet_pushes_true(self):
        proc, pushes = self.run_push(psql_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(pushes), 1)
        got = self.pushed(pushes[0])
        self.assertEqual(got["path"], "/api/v1/endpoints/paperclip_fleet-run-stall/external")
        self.assertEqual(got["success"], "true")
        self.assertNothingLeaks(proc, pushes)

    def test_stall_pushes_false_with_the_numbers(self):
        proc, pushes = self.run_push(psql_out="5|2026-10-08 18:02:05.123456+00|199\n")
        self.assertEqual(proc.returncode, 1)
        got = self.pushed(pushes[0])
        self.assertEqual(got["success"], "false")
        self.assertIn("199m", got["error"])
        self.assertIn("5 assigned todo/in_progress cards waiting", got["error"])
        self.assertIn("2026-10-08 18:02:05", got["error"])
        self.assertNothingLeaks(proc, pushes)

    def test_stall_with_no_run_rows_says_so(self):
        proc, pushes = self.run_push(psql_out="3||\n")
        self.assertEqual(proc.returncode, 1)
        got = self.pushed(pushes[0])
        self.assertEqual(got["success"], "false")
        self.assertIn("no heartbeat_runs row exists", got["error"])
        self.assertIn("3 assigned", got["error"])

    def test_every_unreadable_shape_pushes_false(self):
        cases = {
            "database read failure": dict(psql_out="boom\n", psql_rc=2),
            "two rows": dict(psql_out="1|2026-10-08 18:02:05+00|30\n2|x|y\n"),
            "non-numeric card count": dict(psql_out="many|2026-10-08 18:02:05+00|30\n"),
            "non-numeric gap": dict(psql_out="1|2026-10-08 18:02:05+00|soon\n"),
            "hostile timestamp": dict(psql_out="1|$(touch pwn)|30\n"),
            "extra column": dict(psql_out="1|2026-10-08 18:02:05+00|30|surprise\n"),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                proc, pushes = self.run_push(**kwargs)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(len(pushes), 1)
                self.assertEqual(self.pushed(pushes[0])["success"], "false")
                self.assertFalse((self.dir / "pwn").exists())
                self.assertNothingLeaks(proc, pushes)

    def test_missing_env_refuses_without_a_push(self):
        for name in ("GATUS_URL", "GATUS_EXTERNAL_TOKEN"):
            with self.subTest(missing=name):
                proc, pushes = self.run_push(psql_out="", drop=(name,))
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(pushes, [])
                self.assertIn(name, proc.stderr)
        # No DB credential at all (neither TCP URL nor container-local RO
        # user) refuses without a push. Dropping the URL falls back to
        # docker-ro mode, which then names the missing RO user.
        with self.subTest(missing="no-db-credential"):
            proc, pushes = self.run_push(psql_out="", drop=("PAPERCLIP_RO_DB_URL",))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(pushes, [])
            self.assertIn("PAPERCLIP_RO_DB_USER", proc.stderr)

    def test_session_is_read_only_and_the_url_travels_by_environment(self):
        proc, _ = self.run_push(psql_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("default_transaction_read_only=on",
                      (self.dir / "psql.pgoptions").read_text())
        self.assertEqual((self.dir / "psql.pgenv").read_text(),
                         "ro_user|%s|host-609b79ea.example.invalid.invalid|paperclip" % DB_PASSWORD)
        self.assertEqual((self.dir / "psql.stdin").read_text(), SQL_FILE.read_text())

    def test_container_ro_healthy_pushes_true(self):
        proc, pushes = self.run_push_ro(docker_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(pushes), 1)
        got = self.pushed(pushes[0])
        self.assertEqual(got["path"], "/api/v1/endpoints/paperclip_fleet-run-stall/external")
        self.assertEqual(got["success"], "true")
        self.assertNothingLeaks(proc, pushes)

    def test_container_ro_stall_pushes_false_with_the_numbers(self):
        proc, pushes = self.run_push_ro(
            docker_out="5|2026-10-08 18:02:05.123456+00|199\n")
        self.assertEqual(proc.returncode, 1)
        got = self.pushed(pushes[0])
        self.assertEqual(got["success"], "false")
        self.assertIn("199m", got["error"])
        self.assertIn("5 assigned todo/in_progress cards waiting", got["error"])
        self.assertNothingLeaks(proc, pushes)

    def test_container_ro_session_is_read_only_as_monitor_ro(self):
        proc, _ = self.run_push_ro(docker_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("default_transaction_read_only=on",
                      (self.dir / "docker.pgoptions").read_text())
        self.assertEqual((self.dir / "docker.pgenv").read_text(),
                         "monitor_ro|%s|paperclip-db|paperclip" % DB_PASSWORD)
        argv = (self.dir / "docker.argv").read_text()
        # Credentials travel by -e names, never values, on the host argv.
        self.assertIn("PAPERCLIP_RO_DB_USER", argv)
        self.assertIn("PGPASSWORD", argv)
        self.assertIn("paperclip-db", argv)
        self.assertNotIn(DB_PASSWORD, argv)
        self.assertNotIn("monitor_ro|", argv)
        self.assertEqual((self.dir / "docker.stdin").read_text(),
                         SQL_FILE.read_text())

    def test_container_ro_refuses_non_monitor_ro_without_a_push(self):
        proc, pushes = self.run_push_ro(docker_out="", user="postgres")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(pushes, [])
        self.assertIn("monitor_ro", proc.stderr)

    def test_container_ro_missing_password_refuses_without_a_push(self):
        proc, pushes = self.run_push_ro(docker_out="",
                                        drop=("PAPERCLIP_MONITOR_RO_PASSWORD",))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(pushes, [])
        self.assertIn("PAPERCLIP_MONITOR_RO_PASSWORD", proc.stderr)

    def run_push_env(self, extra, psql_out=None, psql_rc=0,
                     docker_out=None, docker_rc=0):
        """Run the probe with an explicit env overlay (both transports set)."""
        stub = self.dir
        for name in ("psql.out", "psql.rc", "psql.argv", "psql.stdin",
                     "psql.pgoptions", "psql.pgenv", "docker.out", "docker.rc",
                     "docker.argv", "docker.stdin", "docker.pgoptions",
                     "docker.pgenv", "curl.argv", "curl.cfg"):
            (stub / name).unlink(missing_ok=True)
        if psql_out is not None:
            (stub / "psql.out").write_text(psql_out)
        (stub / "psql.rc").write_text(str(psql_rc))
        if docker_out is not None:
            (stub / "docker.out").write_text(docker_out)
        (stub / "docker.rc").write_text(str(docker_rc))
        env = {
            "PATH": "%s:%s" % (self.bin, os.environ["PATH"]),
            "HOME": str(self.dir),
            "STUB_DIR": str(stub),
            "TMPDIR": str(self.dir),
            "GATUS_URL": "https://gatus.invalid",
            "GATUS_EXTERNAL_TOKEN": GATUS_TOKEN,
        }
        env.update(extra)
        proc = subprocess.run(["bash", str(PUSH)], env=env, capture_output=True,
                              text=True, timeout=60)
        cfg = (stub / "curl.cfg").read_text() if (stub / "curl.cfg").exists() else ""
        pushes = [c for c in cfg.split("---\n") if c.strip()]
        return proc, pushes

    def test_both_transports_present_prefers_docker_ro(self):
        # Review finding 1: the shared PAPERCLIP_RO_DB_URL stays for other
        # probes; docker-ro wins when its probe-specific vars are present.
        extra = {
            "PAPERCLIP_RO_DB_URL": DB_URL,
            "PAPERCLIP_RO_DB_USER": "monitor_ro",
            "PAPERCLIP_RO_DB_CTR": "paperclip-db",
            "PAPERCLIP_RO_DB_NAME": "paperclip",
            "PAPERCLIP_MONITOR_RO_PASSWORD": DB_PASSWORD,
        }
        proc, pushes = self.run_push_env(extra, docker_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(pushes), 1)
        self.assertTrue((self.dir / "docker.argv").exists())
        self.assertFalse((self.dir / "psql.argv").exists())
        self.assertNothingLeaks(proc, pushes)

    def test_explicit_url_mode_uses_tcp_despite_ro_vars(self):
        extra = {
            "FLEET_RUN_STALL_DB_MODE": "url",
            "PAPERCLIP_RO_DB_URL": DB_URL,
            "PAPERCLIP_RO_DB_USER": "monitor_ro",
            "PAPERCLIP_RO_DB_CTR": "paperclip-db",
            "PAPERCLIP_RO_DB_NAME": "paperclip",
            "PAPERCLIP_MONITOR_RO_PASSWORD": DB_PASSWORD,
        }
        proc, pushes = self.run_push_env(extra, psql_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.dir / "psql.argv").exists())
        self.assertFalse((self.dir / "docker.argv").exists())
        self.assertNothingLeaks(proc, pushes)

    def test_explicit_docker_ro_mode_uses_container_despite_url(self):
        extra = {
            "FLEET_RUN_STALL_DB_MODE": "docker-ro",
            "PAPERCLIP_RO_DB_URL": DB_URL,
            "PAPERCLIP_RO_DB_USER": "monitor_ro",
            "PAPERCLIP_RO_DB_CTR": "paperclip-db",
            "PAPERCLIP_RO_DB_NAME": "paperclip",
            "PAPERCLIP_MONITOR_RO_PASSWORD": DB_PASSWORD,
        }
        proc, pushes = self.run_push_env(extra, docker_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.dir / "docker.argv").exists())
        self.assertFalse((self.dir / "psql.argv").exists())
        self.assertNothingLeaks(proc, pushes)

    def test_invalid_mode_refuses_without_a_push(self):
        extra = {
            "FLEET_RUN_STALL_DB_MODE": "bogus",
            "PAPERCLIP_RO_DB_URL": DB_URL,
        }
        proc, pushes = self.run_push_env(extra, psql_out="")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(pushes, [])
        self.assertIn("FLEET_RUN_STALL_DB_MODE", proc.stderr)

    def test_bare_pgpassword_alone_does_not_enable_docker_ro(self):
        # Review finding 2: the probe reads only the probe-specific password
        # name; a bare PGPASSWORD in the shared file must not enable container
        # mode (it would otherwise override URL auth for all pcsql consumers).
        extra = {
            "PAPERCLIP_RO_DB_USER": "monitor_ro",
            "PAPERCLIP_RO_DB_CTR": "paperclip-db",
            "PAPERCLIP_RO_DB_NAME": "paperclip",
            "PGPASSWORD": DB_PASSWORD,
        }
        proc, pushes = self.run_push_env(extra, docker_out="")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(pushes, [])
        self.assertIn("PAPERCLIP_MONITOR_RO_PASSWORD", proc.stderr)

    def test_url_mode_ignores_probe_password(self):
        extra = {
            "FLEET_RUN_STALL_DB_MODE": "url",
            "PAPERCLIP_RO_DB_URL": DB_URL,
            "PAPERCLIP_MONITOR_RO_PASSWORD": DB_PASSWORD,
        }
        proc, pushes = self.run_push_env(extra, psql_out="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.dir / "psql.argv").exists())
        self.assertFalse((self.dir / "docker.argv").exists())
        self.assertNothingLeaks(proc, pushes)


if __name__ == "__main__":
    unittest.main()
