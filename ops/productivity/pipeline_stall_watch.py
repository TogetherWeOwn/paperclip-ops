#!/usr/bin/env python3
"""Pipeline stall watchdog (host 2026-10-22).

Why: on 2026-10-21/22 review_gate crashed 69 times (21:56Z-04:12Z) and review_carry failed for ~9h; no review was requested
for ~6h and merges slowed, and nothing alerted. Agents need "a pipe that works, watched by machines".

Every 5 min, report-only checks:
  1. a pipeline service (review-gate, review-carry, pr-automerge) failed >= 3 times in the last 30 min (systemd journal);
  2. review-gate has not logged a successful tick in 20 min;
  3. "credential response invalid" (review carry auth) in the operator log in the last 30 min.
Any hit opens (or comments once per hour on) a CEO card "[pipeline-stall] <check>" so the CEO routes the fix; it never
changes pipeline state itself. Rollback: systemctl --user disable --now pipeline-stall-watch.timer
"""
import calendar
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import atomic_state  # noqa: E402

ROOT = Path('/home/ubuntu/paperclip-enterprise-company')
LOG = ROOT / 'ops/worker-vps/operator-actions.log'
STATE = ROOT / 'ops/productivity/pipeline-stall-watch.state.json'
# Synthetic placeholder: the host install step substitutes the real company id.
CID = '00000000-0000-4000-8000-000000000004'
SERVICES = ['review-gate.service', 'review-carry.service', 'pr-automerge.service']
REPEAT_S = 3600
GATE_SILENCE_MIN = 20


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def failures(unit):
    out = sh('journalctl', '--user', '-u', unit, '--since', '-30 min', '--no-pager', '-o', 'cat')
    return len(re.findall(r"Failed with result 'exit-code'", out))


def _parse_log_ts(line):
    """Epoch seconds for a log line's leading UTC timestamp, or None when malformed."""
    try:
        return calendar.timegm(time.strptime(line[:20], '%Y-%m-%dT%H:%M:%SZ'))
    except ValueError:
        return None


def last_gate_tick_age_min():
    tail = sh('tail', '-n', '4000', str(LOG)).splitlines()
    for line in reversed(tail):
        # A guarded tick that holds work for reviewer capacity/backlog is a
        # successful tick: the gate ran. Only admits/requests counted before,
        # so recent backpressure read as silence.
        if ' review-gate: ' in line and ('gate admits' in line or 'requested review' in line or 'gate holds' in line):
            t = _parse_log_ts(line)
            if t is None:
                continue  # Malformed timestamp: skip the line, keep scanning history.
            return (time.time() - t) / 60
    return None


def stale_history_without_tick():
    """True when the tail holds timestamped history older than the silence window.

    Distinguishes a fresh install (empty/unparseable tail: no alert) from a
    gate that had time to tick but left no recognized tick (alert). Called only
    when last_gate_tick_age_min() found no tick.
    """
    tail = sh('tail', '-n', '4000', str(LOG)).splitlines()
    if not tail:
        return False
    now = time.time()
    for line in tail:
        t = _parse_log_ts(line)
        if t is not None and (now - t) / 60 > GATE_SILENCE_MIN:
            return True
    return False


def carry_auth_errors():
    tail = sh('tail', '-n', '4000', str(LOG)).splitlines()
    cutoff = time.time() - 1800
    n = 0
    for line in tail:
        if 'credential response invalid' not in line:
            continue
        t = _parse_log_ts(line)
        if t is not None and t >= cutoff:
            n += 1
    return n


def capacity_collapse():
    """(latest C*, highest C* in the 2 h before it) from model-capacity-caps lines, or None."""
    tail = sh('tail', '-n', '6000', str(LOG)).splitlines()
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 7200))
    vals = []
    for line in tail:
        if line[:20] >= cutoff and 'model-capacity-caps' in line:
            m = re.search(r'C\*=([0-9.]+)', line)
            if m:
                try:
                    vals.append(float(m.group(1)))
                except ValueError:
                    continue  # Malformed capacity value (e.g. C*=1.2.3): skip, keep scanning.
    return (vals[-1], max(vals[:-1])) if len(vals) >= 2 else None


def alert(key, title, body, state):
    import sys
    sys.path.insert(0, str(ROOT / 'ops/productivity'))
    import pr_controller as P
    last = state.get(key, {})
    if time.time() - last.get('at', 0) < REPEAT_S:
        return
    ceo = sh('docker', 'exec', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At', '-c',
             f"select id from agents where company_id='{CID}' and name='Chief Executive Officer'").strip()
    card = last.get('card')
    if card:
        d = json.loads(P.api('GET', f'issues/{card}')[1] or '{}')
        if d.get('status') in ('done', 'cancelled'):
            card = None
    if card:
        P.api('POST', f'issues/{card}/comments', {'body': f'Still stalled: {body}'})
    else:
        r = json.loads(P.api('POST', f'companies/{CID}/issues', {
            'title': f'[pipeline-stall] {title}', 'assigneeAgentId': ceo, 'status': 'todo', 'priority': 'critical',
            'description': f"{body}\n\nRaised by the host pipeline-stall watchdog (`ops/productivity/pipeline_stall_watch.py`). "
                           "Route the fix to the owner of the failing script or service; close this card once the check is clean."})[1])
        card = r.get('identifier')
    state[key] = {'at': time.time(), 'card': card}
    with LOG.open('a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} pipeline-stall-watch: {title} -> {card}. "
                "UNDO: n/a (alert only)\n")


def main():
    try:
        state = json.loads(STATE.read_text())
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    for unit in SERVICES:
        n = failures(unit)
        if n >= 3:
            alert(f'fail:{unit}', f'{unit} failed {n}x in 30 min',
                  f'`{unit}` exited with errors {n} times in the last 30 min. Check `journalctl --user -u {unit}`.', state)
    age = last_gate_tick_age_min()
    if age is not None and age > GATE_SILENCE_MIN:
        alert('gate-silent', f'review-gate silent {age:.0f} min',
              f'review-gate has not logged a successful tick for {age:.0f} min, so no reviews are being requested.', state)
    elif age is None and stale_history_without_tick():
        alert('gate-silent', 'review-gate silent (no recognized tick in recent history)',
              'review-gate logged no recognized successful tick (gate admits / requested review / gate holds) in the '
              f'last {GATE_SILENCE_MIN} min of operator history, so no reviews are being requested.', state)
    n = carry_auth_errors()
    if n:
        alert('carry-auth', 'review carry auth failing', f'{n} "credential response invalid" lines in 30 min (review carry).', state)
    cc = capacity_collapse()
    if cc and cc[0] < 1 and cc[1] > 10:
        alert('capacity-collapse', f'model-capacity target fell to {cc[0]:.2f}',
              f'The model-capacity target C* fell from {cc[1]:.1f} to {cc[0]:.2f} within 2 h. Check '
              '`GET /api/plugins/<model-capacity>/api/ladder` for accounts with empty `rungs` (null-cost or unmatched '
              'arms).', state)
    atomic_state.write_json_atomic(STATE, state)


if __name__ == '__main__':
    main()
