#!/usr/bin/env python3
"""Idle-waiter guard (owner GO 2026-10-05 ~12:35Z).

Cards whose assignee keeps posting "nothing new" notes are polling something with monitors and getting
re-woken by continuation. Each wake burns a run and adds noise (228 of 1,068 agent comments in 6h).
The prose rule in the bundles does not stop it, so this is the mechanism:

- WAITER: open card, assignee agent posted >= MIN_NOTES no-op notes in the last 6h, nobody else
  posted anything (no new input), and no run is live on it.
- DOER: the card the waiter says it waits on ("waiting on/for", "blocked on/by", "until", "pending",
  "depends on", "after" + TOG-id in its own recent notes or monitor notes), or an existing open blocker.
  It must be open (todo/in_progress/in_review), assigned to an agent, not a passive tracker
  (monitor/watch/tracking/babysit/steward/umbrella/program/epic), and not blocked itself.
- 1-3 doers: set the waiter `blocked` on them (native: no monitor, continuation or disposition
  wakes; Paperclip wakes it once when a doer resolves).
- Waiting on a GitHub PR/CI with no doer card: leave it (a monitor is the right tool until event wakes land).
- `Operator:` cards: the doer is the host operator, so they are reported, not flagged to the CEO.
- No doer: never park. List it once in the CEO rollup card so someone is assigned.

Dry run by default; --apply to act. At most MAX_PARK parks per run; a card is not touched again for 24h.
Stop: touch ~/.local/state/flow-guard/PAUSE. Rollback per card: PATCH status in_progress, blockedByIssueIds [].
State: missing, empty, malformed or invalid state stops the run before any API call. Recover with
--recover-state FILE (docs/runbooks/guard-state-recovery.md).
"""
import json, math, os, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import atomic_state  # noqa: E402

ROOT = '/home/ubuntu/paperclip-enterprise-company'
CID = '00000000-0000-4000-8000-000000000004'
CEO = '00000000-0000-4000-8000-000000000031'
STATE_DIR = os.path.expanduser('~/.local/state/flow-guard')
STATE = f'{STATE_DIR}/idle-waiters.json'
ROLLUP_TITLE = 'Waiting cards with nobody doing the work: assign a doer'
PSQL_TIMEOUT = 60
API_TIMEOUT = 90  # backstop above curl's own -m 60
MIN_NOTES, MAX_PARK, MAX_DOERS = 2, 10, 3
NOOP = (r'(no-op|nothing new|no new (activity|input|external|evidence|comments?)|duplicate wake|handled quietly|'
        r'nothing actionable|no action taken|no change(s)? (made|needed))')
WAIT_REF = re.compile(r'(?:wait(?:ing|s)?\s+(?:on|for)|blocked\s+(?:on|by)|until|pending|depends\s+on|after)'
                      r'\s+(?:the\s+)?(?:live\s+)?(?:leaf\s+)?\[?\*{0,2}(TOG-\d+)', re.I)
PASSIVE = re.compile(r'monitor|watch|tracking|babysit|steward|umbrella|program|epic|rollup|digest', re.I)
PR_WAIT = re.compile(r'(pull/\d+|PR #\d+|#\d{3,5}\b).{0,80}(CI|checks?|review|merge|green|queued)', re.I)


def psql(sql):
    out = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At',
                          '-F', '\x1f', '-v', 'ON_ERROR_STOP=1'], input=sql, capture_output=True, text=True,
                         check=True, timeout=PSQL_TIMEOUT).stdout
    return [l.split('\x1f') for l in out.splitlines() if l]


def api(method, path, body):
    try:
        out = subprocess.run(['curl', '-sK', f'{ROOT}/.pc.curl', '-m', '60', '-X', method,
                              '-H', 'content-type: application/json',
                              f'http://127.0.0.1:3100/api/{path}', '--data-binary', json.dumps(body)],
                             capture_output=True, text=True, timeout=API_TIMEOUT).stdout
    except subprocess.TimeoutExpired:
        return {'_timeout': f'{method} {path} timed out after {API_TIMEOUT}s (unmeasured)'}
    try:
        return json.loads(out)
    except ValueError:
        return {'raw': out[:200]}


def log(msg):
    with open(f'{ROOT}/ops/worker-vps/operator-actions.log', 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} idle-waiters: {msg}\n")


def validate_state(s):
    if not isinstance(s, dict) or set(s) != {'touched', 'flagged'}:
        raise ValueError('state holds exactly touched and flagged')
    for name in ('touched', 'flagged'):
        if not isinstance(s[name], dict) or not all(
                isinstance(k, str) and (type(v) is int or (type(v) is float and math.isfinite(v)))
                for k, v in s[name].items()):
            raise ValueError(f'{name} must map card identifiers to finite timestamps')


def persist(state):
    try:
        atomic_state.save(STATE, state, validate_state)
    except OSError as e:
        raise atomic_state.StateError(
            f'{STATE} was not confirmed written ({e}); it may already hold the records of this run') from e


def card(ident):
    r = psql(f"""select i.id, i.status, coalesce(i.assignee_agent_id::text,''), left(i.title,90),
      (select count(*) from issue_relations r join issues o on o.id=r.issue_id where r.related_issue_id=i.id
         and r.type='blocks' and o.status not in ('done','cancelled'))
      from issues i where i.company_id='{CID}' and i.identifier='{ident}'""")
    return r[0] if r else None


def open_blockers(iid):
    return psql(f"""select o.identifier from issue_relations r join issues o on o.id=r.issue_id
      where r.related_issue_id='{iid}' and r.type='blocks' and o.status not in ('done','cancelled')""")


def run(apply=False):
    if os.path.exists(f'{STATE_DIR}/PAUSE'):
        return
    state = atomic_state.load(STATE, validate_state)
    now = time.time()
    state['touched'] = {k: v for k, v in state['touched'].items() if now - v < 86400}

    try:
        waiters = psql(f"""
with n as (
  select c.issue_id, count(*) k from issue_comments c join issues i on i.id=c.issue_id
  where c.created_at>now()-interval '6 hours' and c.author_agent_id=i.assignee_agent_id and c.deleted_at is null
    and c.body ~* '{NOOP}' and i.company_id='{CID}'
  group by 1 having count(*)>={MIN_NOTES})
select i.id, i.identifier, i.status, left(i.title,90),
  coalesce(i.execution_policy->'monitor'->>'notes',''),
  coalesce((select string_agg(regexp_replace(x.body,'\\s+',' ','g'), ' ') from (select body from issue_comments c3
     where c3.issue_id=i.id and c3.author_agent_id=i.assignee_agent_id and c3.deleted_at is null
     order by created_at desc limit 3) x),''),
  coalesce(regexp_replace(left(i.description,4000),'\\s+',' ','g'),'')
from n join issues i on i.id=n.issue_id
where i.status in ('in_progress','todo','in_review')
  and i.assignee_agent_id is not null
  and not exists (select 1 from issue_comments c2 where c2.issue_id=i.id and c2.created_at>now()-interval '6 hours'
                  and c2.deleted_at is null and c2.presentation is null
                  and coalesce(c2.author_agent_id::text,'')<>i.assignee_agent_id::text)
  and not exists (select 1 from heartbeat_runs r where r.context_snapshot->>'issueId'=i.id::text
                  and r.status in ('running','queued'))
order by n.k desc;""")
    except subprocess.TimeoutExpired:
        print(f'idle-waiters: FAILED: db query timed out after {PSQL_TIMEOUT}s '
              f'(unmeasured; nothing parked)', file=sys.stderr)
        raise SystemExit(1)

    parked, external, nodoer, skipped, operator = [], [], [], [], []
    for iid, ident, status, title, mnotes, notes, desc in waiters:
        if ident in state['touched']:
            skipped.append(ident)
            continue
        refs = [m for m in WAIT_REF.findall(notes + ' ' + mnotes) if m != ident]
        refs += [b[0] for b in open_blockers(iid)]
        doers = []
        for ref in dict.fromkeys(refs):
            c = card(ref)
            if not c:
                continue
            d_id, d_status, d_agent, d_title, d_blocked = c
            if d_status in ('todo', 'in_progress', 'in_review') and d_agent and not PASSIVE.search(d_title) and int(d_blocked) == 0:
                doers.append((ref, d_id, d_title))
        if 1 <= len(doers) <= MAX_DOERS:
            parked.append((iid, ident, title, doers))
        elif title.lower().startswith('operator:'):
            operator.append(ident)  # the doer is the host operator; it is in the operator queue, not the CEO's
        elif PR_WAIT.search(notes + ' ' + mnotes + ' ' + title) or re.match(r'(code )?review\b.*\bPR\b', title, re.I):
            external.append(ident)
        else:
            nodoer.append((ident, title))

    print(f'{len(waiters)} waiters: {len(parked)} park-on-doer, {len(external)} external PR/CI wait (left), '
          f'{len(nodoer)} no doer (flag CEO), {len(operator)} Operator: cards (operator queue), {len(skipped)} touched <24h')
    for _, ident, title, doers in parked:
        print(f'  PARK {ident} on {",".join(d[0] for d in doers)} | {title[:60]}')
    for ident, title in nodoer:
        print(f'  NODOER {ident} | {title[:70]}')
    if not apply:
        return

    done = 0
    for iid, ident, title, doers in parked[:MAX_PARK]:
        keep = [b[0] for b in open_blockers(iid)]
        ids = list(dict.fromkeys([card(k)[0] for k in keep] + [d[1] for d in doers]))
        names = ', '.join(d[0] for d in doers)
        state['touched'][ident] = now
        persist(state)  # saved before the PATCH: a failed save parks nothing, and a park is never repeated
        r = api('PATCH', f'issues/{iid}', {
            'status': 'blocked', 'blockedByIssueIds': ids,
            'comment': (f'Idle-waiter guard: this card kept waking with nothing new while it waits on {names}. '
                        f'It is now `blocked` on {names}, so it sleeps until that work resolves and Paperclip wakes it once. '
                        'Assignee: if this is the wrong card, re-point the blockers; do not move it back to `in_progress` '
                        'just to wait.')})
        ok = r.get('status') == 'blocked'
        done += ok
        log(f'{ident} blocked on {names} (was polling with no-op notes) ok={ok}; rollback: PATCH status in_progress, blockedByIssueIds []')

    new_flags = [(i, t) for i, t in nodoer if i not in state['flagged']]
    if new_flags:
        roll = psql(f"""select id from issues where company_id='{CID}' and title='{ROLLUP_TITLE}'
                    and status not in ('done','cancelled') limit 1""")
        lines = '\n'.join(f'- {i}: {t}' for i, t in new_flags)
        body = (f'{len(new_flags)} more card(s) keep waking with nothing new, and no open card is doing what they wait on. '
                'For each one: assign the work to someone (a doer card), then block the waiter on it; or close the '
                f'waiter if the wait is no longer needed.\n\n{lines}')
        # Saved before the POST: a failed save posts nothing, and a posted notice is never repeated.
        for i, _ in new_flags:
            state['flagged'][i] = now
        persist(state)
        if roll:
            api('POST', f'issues/{roll[0][0]}/comments', {'body': body})
        else:
            api('POST', f'companies/{CID}/issues', {
                'title': ROLLUP_TITLE, 'status': 'todo', 'priority': 'medium', 'assigneeAgentId': CEO,
                'description': ('Fed by the idle-waiter guard (ops/productivity/idle_waiters.py, owner GO 2026-10-05). '
                                'These cards poll with no-op wakes, but nothing is actively producing what they wait for. '
                                'Never fix this by blocking a waiter on a passive tracker.\n\n' + body)})
        log(f'flagged {len(new_flags)} no-doer waiter(s) to the CEO rollup: {", ".join(i for i, _ in new_flags)}')
    persist(state)
    print(f'parked {done}, flagged {len(new_flags)}')


def recover_state(doc_path):
    try:
        doc = json.loads(Path(doc_path).read_text())
    except (OSError, ValueError) as e:
        raise atomic_state.StateError(f'{doc_path} cannot be read as JSON ({e})') from e
    prov = doc.get('provenance') if isinstance(doc, dict) else None
    if not (isinstance(prov, dict) and prov.get('complete') is True and prov.get('source') and prov.get('retrieved_at')):
        raise atomic_state.StateError('provenance needs complete=true, source and retrieved_at')
    try:
        atomic_state.recover(STATE, {'touched': doc.get('touched'), 'flagged': doc.get('flagged')}, validate_state, doc)
    except ValueError as e:
        raise atomic_state.StateError(f'recovery data is invalid ({e})') from e


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    if '--recover-state' in sys.argv:
        at = sys.argv.index('--recover-state') + 1
        if at >= len(sys.argv):
            raise atomic_state.StateError('--recover-state needs a FILE')
        recover_state(sys.argv[at])
    else:
        run(apply='--apply' in sys.argv)


if __name__ == '__main__':
    try:
        main()
    except atomic_state.StateError as e:
        sys.exit(f'idle-waiters: stopped: {e}')
