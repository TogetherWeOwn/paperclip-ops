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
import time
from pathlib import Path

ROOT = Path('/home/ubuntu/paperclip-enterprise-company')
LOG = ROOT / 'ops/worker-vps/operator-actions.log'
STATE = ROOT / 'ops/productivity/pipeline-stall-watch.state.json'
CID = 'ef993a7e-5ea7-445f-ba88-27a6a2690c3a'
SERVICES = ['review-gate.service', 'review-carry.service', 'pr-automerge.service']
REPEAT_S = 3600


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def failures(unit):
    out = sh('journalctl', '--user', '-u', unit, '--since', '-30 min', '--no-pager', '-o', 'cat')
    return len(re.findall(r"Failed with result 'exit-code'", out))


def last_gate_tick_age_min():
    tail = sh('tail', '-n', '4000', str(LOG)).splitlines()
    for line in reversed(tail):
        # A guarded tick that holds work for reviewer capacity/backlog is a
        # successful tick: the gate ran. Only admits/requests counted before,
        # so recent backpressure read as silence.
        if ' review-gate: ' in line and ('gate admits' in line or 'requested review' in line or 'gate holds' in line):
            try:
                t = calendar.timegm(time.strptime(line[:20], '%Y-%m-%dT%H:%M:%SZ'))
            except ValueError:
                continue  # Malformed timestamp: skip the line, keep scanning history.
            return (time.time() - t) / 60
    return None


def carry_auth_errors():
    tail = sh('tail', '-n', '4000', str(LOG)).splitlines()
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 1800))
    return sum(1 for l in tail if l[:20] >= cutoff and 'credential response invalid' in l)


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
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for unit in SERVICES:
        n = failures(unit)
        if n >= 3:
            alert(f'fail:{unit}', f'{unit} failed {n}x in 30 min',
                  f'`{unit}` exited with errors {n} times in the last 30 min. Check `journalctl --user -u {unit}`.', state)
    age = last_gate_tick_age_min()
    if age is not None and age > 20:
        alert('gate-silent', f'review-gate silent {age:.0f} min',
              f'review-gate has not logged a successful tick for {age:.0f} min, so no reviews are being requested.', state)
    n = carry_auth_errors()
    if n:
        alert('carry-auth', 'review carry auth failing', f'{n} "credential response invalid" lines in 30 min (review carry).', state)
    cc = capacity_collapse()
    if cc and cc[0] < 1 and cc[1] > 10:
        alert('capacity-collapse', f'model-capacity target fell to {cc[0]:.2f}',
              f'The model-capacity target C* fell from {cc[1]:.1f} to {cc[0]:.2f} within 2 h. Check '
              '`GET /api/plugins/<model-capacity>/api/ladder` for accounts with empty `rungs` (null-cost or unmatched '
              'arms).', state)
    STATE.write_text(json.dumps(state))


if __name__ == '__main__':
    main()
