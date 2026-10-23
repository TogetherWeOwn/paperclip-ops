#!/usr/bin/env python3
"""Stuck-run reaper (owner rulings 2026-10-06 ~23:45Z, items 4+5; memory owner-audit-decisions-20261006).

Two narrow actions, at most MAX_ACTIONS per tick:

1. DEAD RUN -> cancel through the board route (POST /api/heartbeat-runs/<id>/cancel).
   A run is dead only when ALL hold:
     - status running/preparing/finalizing;
     - its last activity (max of last_output_at, updated_at, process_started_at, created_at) is older than 2 h;
     - no live local process, proven by an explicit answer from a successfully
       completed in-container probe: probe_container_pid prints alive/absent
       from inside the paperclip container (run PIDs are container-namespace
       PIDs, never test them on the host). A probe that errors, times out, or
       prints anything else is UNMEASURED: the run is retained and the tick
       reports the diagnostic. A run with no recorded PID keeps the
       pre-existing path and counts as absent (no local process to find);
     - for the SSH-environment Operator agent, no paperclip-operator host process has its cwd under the run's
       remote run dir (.paperclip-runtime/runs/<run id>).
   Silence alone is never enough.

2. RUNAWAY RETRY LOOP -> park the card. The 3 newest runs of an issue are all failed, all inside 3 h and all
   in the same error class, and the issue has no live work (ops/lib/live_run_guard). Parking adds a real
   blocked-by edge to the parking-lot anchor card (a status-only "blocked" is undone by the stalled-issue
   dispatcher), keeps existing blockers, and prepends an evidence note to the description (a comment would wake the
   assignee into another retry).

Never kills processes or DB backends, never mass-cancels. Ledger: stuck-run-reaper.jsonl. Pause: touch PAUSE.
Usage: stuck_run_reaper.py [--dry-run] | stuck_run_reaper.py --recover-state FILE
   or: stuck_run_reaper.py report [--top N] [--min-idle-hours H] [--json] [--dry-run]
State: missing, empty or corrupt state stops the run before any action (docs/runbooks/guard-state-recovery.md).
Undo: dead-run cancels auto-redispatch the card; parked cards are released by removing the anchor edge
(PATCH blockedByIssueIds without the anchor id, status todo).

The report subcommand is a read-only ranked triage inventory over the FULL
candidate set (rows beyond MAX_ACTIONS keep ordinary ranks; the action cap is
stated separately). It reuses the same scanners and pure classifiers as the
actor (find_dead/find_loops, is_dead/loop_verdict/classify). Once the newest
three qualify a loop, report counts/IDs cover all same-class failures inside
LOOP_WINDOW. Report qualification and evidence share the captured report
cutoff; the actor still uses its existing newest-three scan. Reporting never
cancels, parks, or writes ledger/state/API mutations. UNMEASURED probes list as
unmeasured with diagnostics (never ranked dead); loop cards with live work
list as deferred_live_work (never parkable).
"""
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ops/lib'))
import atomic_state  # noqa: E402
import live_run_guard  # noqa: E402

COMPANY = '00000000-0000-4000-8000-000000000004'
OPERATOR_AGENT = '00000000-0000-4000-8000-000000000052'
API = 'http://127.0.0.1:3100/api'
STATE_DIR = Path.home() / '.local/state/stuck-run-reaper'
PAUSE = STATE_DIR / 'PAUSE'
STATE = STATE_DIR / 'state.json'
LEDGER = ROOT / 'ops/productivity/stuck-run-reaper.jsonl'
ACTIONS_LOG = ROOT / 'ops/worker-vps/operator-actions.log'
DEAD_AFTER = dt.timedelta(hours=2)
LOOP_WINDOW = dt.timedelta(hours=3)
LOOP_RUNS = 3
MAX_ACTIONS = 5
ANCHOR_TITLE = 'PARKING LOT: runaway retry loops (stuck_run_reaper, owner ruling 2026-10-06)'

UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

# Outcomes of container PID inspection. Only a successfully completed probe
# whose stdout names the answer counts as measured; anything else is
# unmeasured and must never cancel a run.
PID_ALIVE = 'alive'
PID_ABSENT = 'absent'
PID_UNMEASURED = 'unmeasured'
PROBE_TIMEOUT = 30

# (substring in error text, class) — first match wins; order matters.
ERROR_CLASSES = (
    ('no space left on device', 'enospc'),
    ('ENOSPC', 'enospc'),
    ('did not send all necessary objects', 'bundle_import'),
    ('Low-trust execution requires a sandbox', 'low_trust_no_sandbox'),
    ('continuation_task_ownership_changed', 'continuation_ownership_changed'),
    ('Managed workspace path', 'managed_workspace_path'),
    ('Persisted git worktree', 'persisted_worktree_invalid'),
    ('not a git repository', 'not_a_git_repo'),
    ('EACCES', 'eacces'),
    ('Not logged in', 'not_logged_in'),
)


def utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    if not s:
        return None
    s = s.strip().replace(' ', 'T')
    if s.endswith('+00'):
        s += ':00'
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def classify(error_code, error):
    """Stable error class for loop detection: known phrases first, else code + normalized first line."""
    text = error or ''
    for needle, cls in ERROR_CLASSES:
        if needle in text:
            return cls
    first = next((l for l in text.splitlines() if l.strip() and not l.startswith('hint:')), '')
    first = re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}', '<id>', first)
    first = re.sub(r'/tmp/[^\s\'"]+', '<tmp>', first)
    first = re.sub(r'\d+', '<n>', first)[:60]
    return f"{error_code or 'unknown'}:{first}"


def last_activity(run):
    ts = [parse_ts(run.get(k)) for k in ('last_output_at', 'updated_at', 'process_started_at', 'created_at')]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


def is_dead(run, now, local, remote_alive):
    """Pure decision: run dict + liveness probes -> (dead, reason).

    local is PID_ALIVE, PID_ABSENT or PID_UNMEASURED; remote_alive is a bool
    (the remote probe already fails closed: unmeasurable counts as alive).
    An unmeasured local probe is never dead: the run is retained.
    """
    if run.get('status') not in ('running', 'preparing', 'finalizing'):
        return False, 'not_active'
    act = last_activity(run)
    if act is None or now - act < DEAD_AFTER:
        return False, 'recent_activity'
    if local == PID_UNMEASURED:
        return False, 'local_pid_unmeasured'
    if local == PID_ALIVE:
        return False, 'local_process_alive'
    if remote_alive:
        return False, 'remote_process_alive'
    return True, f"no process and no activity since {act.isoformat()}"


def loop_verdict(runs, now):
    """runs: newest first, dicts with status/created_at/error_code/error. -> (is_loop, class)."""
    if len(runs) < LOOP_RUNS:
        return False, None
    head = runs[:LOOP_RUNS]
    if any(r.get('status') != 'failed' for r in head):
        return False, None
    if any((parse_ts(r.get('created_at')) or now) < now - LOOP_WINDOW for r in head):
        return False, None
    classes = {classify(r.get('error_code'), r.get('error')) for r in head}
    if len(classes) != 1:
        return False, None
    return True, classes.pop()


# ---------- host / DB / API ----------

def psql_json(sql):
    out = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At',
                          '-c', f"select coalesce(json_agg(t),'[]') from ({sql}) t"],
                         capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    return json.loads(out or '[]')


def probe_container_pid(pid):
    """Inspect /proc/<pid> inside the paperclip container -> (state, detail).

    Only a probe that completes (exit 0) and prints exactly 'alive' or
    'absent' is measured. Docker daemon/exec errors (which can also exit 1),
    timeouts, unparsable PIDs and any other output are PID_UNMEASURED with a
    diagnostic in detail; the caller must leave the run untouched.

    Pre-existing missing-PID path, preserved: a run with no recorded PID
    counts as PID_ABSENT (no local process to find).
    """
    if pid is None or (isinstance(pid, str) and not pid.strip()):
        return PID_ABSENT, 'no PID recorded'
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return PID_UNMEASURED, f'unparsable PID {pid!r}'
    try:
        r = subprocess.run(['docker', 'exec', 'paperclip', 'sh', '-c',
                            f'if test -d /proc/{pid_int}; then echo alive; else echo absent; fi'],
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return PID_UNMEASURED, f'probe timed out after {PROBE_TIMEOUT}s'
    except OSError as e:
        return PID_UNMEASURED, f'probe failed to start: {e}'
    words = (r.stdout or '').strip().split()
    if r.returncode == 0 and words == ['alive']:
        return PID_ALIVE, ''
    if r.returncode == 0 and words == ['absent']:
        return PID_ABSENT, ''
    last_err = (r.stderr or '').strip().splitlines()
    hint = last_err[-1][:160] if last_err else f'exit {r.returncode}'
    return PID_UNMEASURED, f'probe output not explicit: out={words[:2]!r} err={hint!r}'


def operator_remote_alive(run_id):
    """Any paperclip-operator host process whose cwd is inside this run's remote run dir."""
    script = ('for p in $(pgrep -u paperclip-operator 2>/dev/null); do '
              'readlink /proc/$p/cwd 2>/dev/null; done')
    r = subprocess.run(['sudo', '-n', 'sh', '-c', script], capture_output=True, text=True, timeout=30)
    if r.returncode not in (0, 1):
        return True  # cannot see -> fail closed (treat as alive)
    return any(f'/runs/{run_id}' in line for line in r.stdout.splitlines())


def api(method, path, body=None):
    args = ['curl', '-sS', '-w', '\n%{http_code}', '-K', str(ROOT / '.pc.curl'), '-X', method, f'{API}{path}']
    if body is not None:
        args += ['-H', 'content-type: application/json', '--data-binary', '@-']
    r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                       timeout=60)
    text, _, code = r.stdout.rpartition('\n')
    try:
        payload = json.loads(text or 'null')
    except ValueError:
        payload = None
    return code.strip(), payload


def log(line, rec):
    with ACTIONS_LOG.open('a') as f:
        f.write(f"{utc().strftime('%Y-%m-%dT%H:%M:%SZ')} stuck-run-reaper: {line}\n")
    with LEDGER.open('a') as f:
        f.write(json.dumps(rec) + '\n')


def validate_state(s):
    if not isinstance(s, dict) or not set(s) <= {'anchor_id', 'anchor_pending'}:
        raise ValueError('state holds only anchor_id and anchor_pending')
    if 'anchor_id' in s and not (isinstance(s['anchor_id'], str) and UUID.match(s['anchor_id'])):
        raise ValueError('anchor_id must be a uuid')
    pending = s.get('anchor_pending', False)
    if not isinstance(pending, bool) or (pending and 'anchor_id' in s):
        raise ValueError('anchor_pending must be a bool and cannot coexist with anchor_id')


def anchor_id(state, dry):
    if state.get('anchor_pending'):
        raise atomic_state.StateError('a parking-lot anchor create has no recorded outcome (anchor_pending); '
                                      'recover the anchor id from the board before the reaper runs again')
    aid = state.get('anchor_id')
    if aid:
        row = psql_json(f"select status from issues where id='{aid}'")
        if row and row[0]['status'] not in ('done', 'cancelled'):
            return aid
    if dry:
        return '<anchor-to-be-created>'
    state.pop('anchor_id', None)
    state['anchor_pending'] = True
    atomic_state.save(STATE, state, validate_state)
    code, body = api('POST', f'/companies/{COMPANY}/issues', {
        'title': ANCHOR_TITLE, 'status': 'backlog', 'priority': 'low', 'assigneeAgentId': OPERATOR_AGENT,
        'description': ('Cards parked here hit the same failure 3+ times in 3 h (runaway retry loop). '
                        'Owner ruling 2026-10-06: park, do not keep retrying. To release a card after its root cause '
                        'is fixed, remove this card from its blockers and set it to todo. Do not close this card.')})
    created = body.get('id') if isinstance(body, dict) else None
    if code in ('200', '201') and isinstance(created, str) and UUID.match(created):
        state.pop('anchor_pending')
        state['anchor_id'] = created
        try:
            atomic_state.save(STATE, state, validate_state)
        except OSError as e:
            raise atomic_state.StateError(f'anchor {created} was created but not recorded ({e}); '
                                          'recover it from the board') from e
        return created
    if code.startswith('4'):
        state.pop('anchor_pending')
        atomic_state.save(STATE, state, validate_state)
        raise RuntimeError(f'anchor create rejected http={code}')
    raise atomic_state.StateError(f'anchor create outcome unknown (http={code}); anchor_pending stays set')


def find_dead(now):
    """Scan candidate runs -> (dead, unmeasured).

    dead is [(run, why)]; unmeasured is [{'run', 'agent', 'issue', 'diagnostic'}]
    for runs whose PID probe produced no answer. Unmeasured runs are never
    actionable: the caller reports them and leaves them untouched.
    """
    runs = psql_json("""select r.id, r.status, r.process_pid, r.process_started_at, r.last_output_at, r.updated_at,
        r.created_at, r.agent_id, r.context_snapshot->>'issueId' as issue_id, a.name as agent
        from heartbeat_runs r join agents a on a.id=r.agent_id
        where r.status in ('running','preparing','finalizing') and a.company_id='%s'""" % COMPANY)
    out, unmeasured = [], []
    for run in runs:
        act = last_activity(run)
        if act is not None and now - act < DEAD_AFTER:
            continue  # cheap pre-filter; full check below
        local, detail = probe_container_pid(run.get('process_pid'))
        if local == PID_UNMEASURED:
            unmeasured.append({'run': run['id'], 'agent': run.get('agent'), 'issue': run.get('issue_id'),
                               'diagnostic': f'local PID probe unmeasured: {detail}'})
            continue
        remote = operator_remote_alive(run['id']) if run['agent_id'] == OPERATOR_AGENT else False
        dead, why = is_dead(run, now, local, remote)
        if dead:
            out.append((run, why))
    return out, unmeasured


def find_loops(now, anchor, *, as_of=False):
    """as_of bounds report queries to now; the actor keeps its existing scan."""
    window = "r.created_at > now() - interval '3 hours'"
    ceiling = ''
    if as_of:
        window = (f"r.created_at >= '{(now - LOOP_WINDOW).isoformat()}'::timestamptz"
                  f" and r.created_at <= '{now.isoformat()}'::timestamptz")
        ceiling = f" and created_at <= '{now.isoformat()}'::timestamptz"
    issues = psql_json(f"""select distinct r.context_snapshot->>'issueId' as issue_id from heartbeat_runs r
        join agents a on a.id=r.agent_id where a.company_id='{COMPANY}' and r.status='failed'
        and {window} and r.context_snapshot->>'issueId' is not null""")
    out = []
    for row in issues:
        iid = row['issue_id']
        if not iid or not UUID.match(iid):
            continue
        runs = psql_json(f"""select id, status, created_at, error_code, left(coalesce(error,''),2000) as error
            from heartbeat_runs where context_snapshot->>'issueId'='{iid}'{ceiling} order by created_at desc limit {LOOP_RUNS}""")
        is_loop, cls = loop_verdict(runs, now)
        if not is_loop:
            continue
        card = psql_json(f"""select i.id, i.identifier, i.status, i.title,
            coalesce((select json_agg(rel.issue_id) from issue_relations rel
                      where rel.type='blocks' and rel.related_issue_id=i.id),'[]') as blockers
            from issues i where i.id='{iid}'""")
        if not card:
            continue
        card = card[0]
        if card['status'] in ('done', 'cancelled') or anchor in (card.get('blockers') or []):
            continue
        out.append((card, cls, runs))
    return out


def park(card, cls, runs, anchor, dry):
    """Blocked-by edge to the anchor plus an evidence note PREPENDED TO THE DESCRIPTION. A comment would wake the
    assignee (seen 2026-10-06 23:41Z on case_eed1733f), which is exactly the retry the park is meant to stop."""
    blockers = list(dict.fromkeys((card.get('blockers') or []) + [anchor]))
    evidence = ', '.join(f"`{r['id'][:8]}` {str(r['created_at'])[11:16]}Z" for r in runs)
    note = (f"> **Parked by the stuck-run reaper ({utc().strftime('%Y-%m-%d %H:%MZ')}, owner ruling 2026-10-06): "
            f"runaway retry loop.** The last {len(runs)} runs all failed with error class `{cls}` within 3 h "
            f"({evidence}). **To release** after the root cause is fixed: remove the parking-lot blocker and set this "
            f"card to todo.\n\n")
    if dry:
        return 'dry'
    cur = psql_json(f"select coalesce(description,'') as d from issues where id='{card['id']}'")
    desc = cur[0]['d'] if cur else ''
    if 'Parked by the stuck-run reaper' in desc[:600]:
        note = ''
    code, _ = api('PATCH', f"/issues/{card['id']}", {'status': 'blocked', 'blockedByIssueIds': blockers,
                                                     'description': note + desc})
    return code


def recover_state(doc_path):
    doc = json.loads(Path(doc_path).read_text())
    prov = doc.get('provenance') or {}
    if prov.get('complete') is not True or not prov.get('source') or not prov.get('retrieved_at') \
            or prov.get('title_search') != ANCHOR_TITLE:
        raise ValueError('provenance needs complete=true, source, retrieved_at and title_search=ANCHOR_TITLE')
    matches = doc.get('title_search_matches')
    if not isinstance(matches, list) or not all(
            isinstance(m, dict) and isinstance(m.get('id'), str) and UUID.match(m['id'])
            and isinstance(m.get('status'), str) for m in matches):
        raise ValueError('title_search_matches must list {"id", "status"} for every title match, in any status')
    open_ids = [m['id'] for m in matches if m['status'] not in ('done', 'cancelled')]
    if len(open_ids) > 1:
        raise ValueError('more than one open anchor matches the title; close duplicates first')
    state = {'anchor_id': open_ids[0]} if open_ids else {}
    atomic_state.recover(STATE, state, validate_state, doc, may_replace=lambda old: bool(old.get('anchor_pending')))


def run(dry=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if PAUSE.exists():
        print('paused')
        return
    state = atomic_state.load(STATE, validate_state)
    now = utc()
    actions = 0
    report = {'at': now.isoformat(), 'dry': dry, 'dead': [], 'unmeasured': [], 'loops': []}

    dead, unmeasured = find_dead(now)
    for skipped in unmeasured:
        report['unmeasured'].append(skipped)
        if not dry:
            log(f"retained run {skipped['run']} ({skipped['agent']}, issue {skipped['issue']}): "
                f"{skipped['diagnostic']}; no cancel attempted.",
                {**skipped, 'kind': 'pid_probe_unmeasured', 'at': now.isoformat()})

    for run_row, why in dead:
        if actions >= MAX_ACTIONS:
            break
        item = {'run': run_row['id'], 'agent': run_row['agent'], 'issue': run_row.get('issue_id'), 'why': why}
        if not dry:
            code, _ = api('POST', f"/heartbeat-runs/{run_row['id']}/cancel", {})
            item['http'] = code
            log(f"cancelled DEAD run {run_row['id']} ({run_row['agent']}, issue {run_row.get('issue_id')}): {why}; "
                f"http={code}. UNDO: the cancel auto-redispatches the card.", {**item, 'kind': 'dead_run_cancel',
                                                                                'at': now.isoformat()})
        report['dead'].append(item)
        actions += 1

    anchor = anchor_id(state, dry) if actions < MAX_ACTIONS else None
    if anchor:
        for card, cls, runs in find_loops(now, anchor):
            if actions >= MAX_ACTIONS:
                break
            item = {'issue': card['identifier'], 'issue_id': card['id'], 'status': card['status'], 'class': cls,
                    'runs': [r['id'] for r in runs]}
            if live_run_guard.issue_has_live_work(card['id']):
                item['skipped'] = 'live_work'
                report['loops'].append(item)
                continue
            code = park(card, cls, runs, anchor, dry)
            item['http'] = code
            if not dry:
                log(f"PARKED {card['identifier']} (runaway loop, class {cls}, runs {', '.join(r['id'][:8] for r in runs)}) "
                    f"behind anchor {anchor}; http={code}. UNDO: remove the anchor from blockedByIssueIds and set todo.",
                    {**item, 'kind': 'loop_park', 'anchor': anchor, 'at': now.isoformat()})
            report['loops'].append(item)
            actions += 1

    print(json.dumps(report, indent=1))
    return report


# ---------- read-only ranked triage report (M12.5) ----------

REPORT_SCHEMA_VERSION = 1
REPORT_TOP_DEFAULT = 10
REPORT_TOP_MIN = 1
REPORT_TOP_MAX = 100

REPORT_USAGE = """usage: stuck_run_reaper.py report [--top N] [--min-idle-hours H] [--json] [--dry-run]

Read-only ranked triage inventory over the full candidate set (no cap at
MAX_ACTIONS; the cap is stated separately in the output).

  --top N            rows per section, 1-100 (default 10)
  --min-idle-hours H extra idle gate on dead runs only; never relaxes the
                     built-in 2 h dead rule (default: no extra gate)
  --json             versioned JSON envelope on stdout
  --dry-run          no-op alias (the report is always read-only)"""


def report_usage_error(msg):
    print(f'stuck-run-reaper report: {msg}\n{REPORT_USAGE}', file=sys.stderr)
    raise SystemExit(2)


def parse_report_args(args):
    """Parse report flags -> (top, min_idle_hours, as_json). Refuses (exit 2) on unknown flags."""
    top, min_idle, as_json = REPORT_TOP_DEFAULT, None, False
    i = 0
    while i < len(args):
        tok, inline = args[i], None
        if tok.startswith('--') and '=' in tok:
            tok, inline = tok.split('=', 1)
        if tok == '--json':
            if inline is not None:
                report_usage_error('--json takes no value')
            as_json = True
        elif tok == '--dry-run':
            if inline is not None:
                report_usage_error('--dry-run takes no value')
            # no-op alias: the report path is always read-only
        elif tok in ('--top', '--min-idle-hours'):
            val = inline
            if val is None:
                i += 1
                val = args[i] if i < len(args) else None
            if val is None or not str(val).strip():
                report_usage_error(f'{tok} needs a value')
            if tok == '--top':
                try:
                    top = int(str(val).strip())
                except ValueError:
                    report_usage_error(f'--top must be an integer {REPORT_TOP_MIN}-{REPORT_TOP_MAX}')
                if not REPORT_TOP_MIN <= top <= REPORT_TOP_MAX:
                    report_usage_error(f'--top must be {REPORT_TOP_MIN}-{REPORT_TOP_MAX}')
            else:
                try:
                    min_idle = float(str(val).strip())
                except ValueError:
                    report_usage_error('--min-idle-hours must be a non-negative number')
                if not (min_idle >= 0 and min_idle != float('inf') and min_idle == min_idle):
                    report_usage_error('--min-idle-hours must be a non-negative number')
                try:
                    dt.timedelta(hours=min_idle)
                except OverflowError:
                    report_usage_error('--min-idle-hours is too large')
        else:
            report_usage_error(f'unknown argument {args[i]!r}')
        i += 1
    return top, min_idle, as_json


def _idle_hours(run_row, now):
    act = last_activity(run_row)
    if act is None:
        return 0.0, None
    return (now - act).total_seconds() / 3600, act


def _newest_failure_ts(runs):
    best = None
    for r in runs:
        t = parse_ts(r.get('created_at'))
        if t is not None and (best is None or t > best):
            best = t
    return best


def report_loop_runs(issue_id, error_class, now):
    """Full-window evidence for reporting only; never broadens loop qualification."""
    runs = psql_json(f"""select id, status, created_at, error_code, left(coalesce(error,''),2000) as error
        from heartbeat_runs where context_snapshot->>'issueId'='{issue_id}' and status='failed'
        and created_at >= '{(now - LOOP_WINDOW).isoformat()}'::timestamptz
        and created_at <= '{now.isoformat()}'::timestamptz order by created_at desc, id asc""")
    return [r for r in runs if classify(r.get('error_code'), r.get('error')) == error_class]


def build_report(now, top, min_idle_hours):
    """Rank the full inventory. Read-only: no API writes, no ledger/state access.

    find_loops takes anchor=None: without state access the report cannot know
    the parking anchor, so already-parked loops stay visible (full inventory).
    as_of=True shares now with the evidence window, excluding later runs before
    selecting the newest three without changing the actor's scan.
    """
    threshold = max(DEAD_AFTER, dt.timedelta(hours=min_idle_hours or 0))
    dead, unmeasured = find_dead(now)
    loops = find_loops(now, None, as_of=True)

    scored, gated = [], 0
    for run_row, why in dead:
        idle_h, act = _idle_hours(run_row, now)
        if act is None or now - act < threshold:
            gated += 1
            continue
        scored.append({'run_id': run_row['id'], 'issue_id': run_row.get('issue_id'),
                       'agent': run_row.get('agent'), 'idle_hours': round(idle_h, 2),
                       'idle_seconds': (now - act).total_seconds(),
                       'last_activity': act.isoformat(), 'evidence': why or 'dead'})
    scored.sort(key=lambda d: (-d['idle_seconds'], d['run_id']))
    dead_rows = scored[:top]
    for rank, row in enumerate(dead_rows, 1):
        row['rank'] = rank
        del row['idle_seconds']

    unmeasured_rows = [{'run_id': u.get('run'), 'agent': u.get('agent'),
                        'issue_id': u.get('issue'),
                        'diagnostic': u.get('diagnostic', '')} for u in unmeasured]

    ranked_loops, deferred = [], []
    for card, cls, runs in loops:
        iid = card['id']
        try:
            live = live_run_guard.issue_has_live_work(iid)
        except Exception as e:
            deferred.append({'issue_id': iid, 'issue': card.get('identifier'), 'status': card.get('status'),
                             'reason': f'deferred_live_work: live-work probe failed ({e}); treating as live'})
            continue
        if live:
            deferred.append({'issue_id': iid, 'issue': card.get('identifier'), 'status': card.get('status'),
                             'reason': 'deferred_live_work: issue has live work; never parkable'})
            continue
        runs = report_loop_runs(iid, cls, now)
        newest = _newest_failure_ts(runs)
        ranked_loops.append({'issue_id': iid, 'issue': card.get('identifier'), 'status': card.get('status'),
                             'error_class': cls or classify(None, None),
                             'runs': len(runs), 'run_ids': [x.get('id') for x in runs],
                             'newest_failure': newest.isoformat() if newest else None,
                             '_newest_ts': newest.timestamp() if newest else 0.0})
    ranked_loops.sort(key=lambda l: (-l['runs'], -l['_newest_ts'], l['issue_id']))
    loop_rows = ranked_loops[:top]
    for rank, row in enumerate(loop_rows, 1):
        row['rank'] = rank
        del row['_newest_ts']

    excluded = {}
    if unmeasured_rows:
        excluded['unmeasured'] = len(unmeasured_rows)
    if deferred:
        excluded['deferred_live_work'] = len(deferred)
    if gated:
        excluded['below_min_idle_hours'] = gated

    return {'schema_version': REPORT_SCHEMA_VERSION, 'generated_at': now.isoformat(),
            'top': top, 'min_idle_hours': min_idle_hours, 'action_cap': MAX_ACTIONS,
            'dead_runs': dead_rows, 'retry_loops': loop_rows,
            'unmeasured': unmeasured_rows, 'deferred_live_work': deferred,
            'excluded': excluded,
            'error': {'code': 'ok', 'retryable': False,
                      'next_action': 'inspect the ranked rows; the actor cancels/parks at most action_cap per tick'}}


def _plural(n, one, many=None):
    return f'{n} {one if n == 1 else (many or one + "s")}'


def format_report_text(env):
    lines = ['reaper triage report: %s, %s, %s, %s (action cap: %d per tick)' % (
        _plural(len(env['dead_runs']), 'dead run'),
        _plural(len(env['retry_loops']), 'retry loop'),
        _plural(len(env['unmeasured']), 'unmeasured'),
        _plural(len(env['deferred_live_work']), 'deferred_live_work'),
        env['action_cap'])]
    for row in env['dead_runs']:
        lines.append('rank %d DEAD %s idle_hours=%.2f last_activity=%s evidence=%s' % (
            row['rank'], row['run_id'], row['idle_hours'], row['last_activity'], row['evidence']))
    for row in env['retry_loops']:
        lines.append('rank %d LOOP %s issue_id=%s error_class=%s runs=%d run_ids=%s newest_failure=%s' % (
            row['rank'], row['issue'], row['issue_id'], row['error_class'], row['runs'],
            ','.join(x for x in row['run_ids'] if x), row['newest_failure']))
    for row in env['unmeasured']:
        lines.append('UNMEASURED %s agent=%s issue_id=%s diagnostic=%s' % (
            row['run_id'], row['agent'], row['issue_id'], row['diagnostic']))
    for row in env['deferred_live_work']:
        lines.append('DEFERRED %s issue_id=%s reason=%s' % (row['issue'], row['issue_id'], row['reason']))
    return '\n'.join(lines)


def report_cmd(args):
    top, min_idle, as_json = parse_report_args(args)
    now = utc()
    env = build_report(now, top, min_idle)
    print(json.dumps(env, indent=1) if as_json else format_report_text(env))
    return env


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ['report']:
        return report_cmd(args[1:])
    try:
        if '--recover-state' in args:
            recover_state(args[args.index('--recover-state') + 1])
        else:
            run(dry='--dry-run' in args)
    except atomic_state.StateError as e:
        sys.exit(f'stuck-run-reaper: refusing to act: {e}')


if __name__ == '__main__':
    main()
