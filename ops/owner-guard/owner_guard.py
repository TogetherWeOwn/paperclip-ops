#!/usr/bin/env python3
"""owner-escalation-guard (owner 2026-10-04: "I'm tired of shit being escalated to me constantly that can be handled by
the agents"). Every 30 min: find open TogetherWeOwn cards whose title/description route work to the owner and, ONCE per
card, post the routing rule + who can actually do it, asking the assignee to re-route to the CEO with the precise missing
capability. Once per UTC day, post a digest of still-owner-routed cards on the pinned CEO directive card (case_58fddbd4).
Rate limit: 10 comments per run (5 when load1 > 60). --dry-run prints only. PAUSE file stops it.
Missing or corrupt state stops the run; --recover-state FILE restores it (docs/runbooks/guard-state-recovery.md).
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cardops import comment, sql  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import atomic_state  # noqa: E402

HERE = Path(__file__).resolve().parent
STATE, LOG, PAUSE = HERE / 'state.json', HERE / 'guard.log', HERE / 'PAUSE'
IDENT = re.compile(r'^[A-Z]+-\d+$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
COMPANY = '00000000-0000-4000-8000-000000000004'
DIGEST_CARD = 'case_58fddbd4'
DRY = '--dry-run' in sys.argv
# Owner-routing language. Product phrases about the owner's *user journey* ("owner click-path", "owner-testable",
# "owner click pages") are not escalations and are excluded.
# Cards that hand a concrete action to the owner. Policy prose ("never assign the owner", "owner-only actions are batched")
# and the owner's product journey ("owner click-path", "owner-testable") are deliberately not matched.
TITLE_ROUTE = re.compile(r"(owner[- ]click(?![- ]?path)|owner[- ]only|click new pr|owner action|for the owner|human[- ]only|owner screenshot)", re.I)
DESC_ROUTE = re.compile(r"(owner single click|owner: do this|ready to click|owner[- ]click package|only your github login|human org-admin|"
                        r"org-admin github session|owner must click|before the owner clicks|once the owner clicks|the owner clicks on|"
                        r"owner screenshot batch|then comment \"done\" here)", re.I)
SKIP = {'case_d36e8c4a', 'case_58fddbd4', 'case_dc8671fe'}  # pinned owner directives / owner program parent (policy text, not asks)
MSG = ("**owner-escalation-guard:** this card routes work to the owner. Owner rule (2026-09-28, restated 2026-10-04): "
       "**the owner is not an escalation path.** The only exceptions are purchases and acts PROVEN human-only by a failed attempt (paste the exact error).\n\n"
       "Who can actually do it:\n"
       "- **CEO:** acts as the owner on GitHub (Paperclip Cloud owner connector). It opens and updates upstream PRs and handles anything that needs the owner's GitHub identity.\n"
       "- **Operator App token:** repo/org admin, rulesets, environments, variables, secrets, runner registrations. Agents' 403s come from their narrowed run tokens, not from a human-only boundary.\n"
       "- **Operator host:** installs, deploys, timers, plugin package repoints. Route via an `Operator:` card on the CEO, never to the owner.\n\n"
       "Action: retitle without 'owner', re-route to the CEO (or do it), and state the precise missing capability if one really remains. "
       "Matched: \"{m}\"")


def log(line):
    line = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ ') + line
    print(line)
    if not DRY:
        with LOG.open('a') as f:
            f.write(line + '\n')


def validate_state(s):
    if not isinstance(s, dict) or set(s) != {'commented', 'digest_day'}:
        raise ValueError('state holds exactly commented and digest_day')
    if not isinstance(s['commented'], dict) or not all(
            IDENT.match(k) and isinstance(v, str) for k, v in s['commented'].items()):
        raise ValueError('commented maps card identifiers to ISO timestamps')
    if not isinstance(s['digest_day'], str) or (s['digest_day'] and not DAY.match(s['digest_day'])):
        raise ValueError('digest_day is YYYY-MM-DD or empty')


def save_state(state):
    atomic_state.save(STATE, state, validate_state)


def post_once(state, ident, body):
    # Recorded before posting, so a failed save can never lead to a post.
    state['commented'][ident] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_state(state)
    if comment(ident, body):
        return
    # A False return un-records the card so the next run retries it; an exception keeps it suppressed.
    del state['commented'][ident]
    save_state(state)


def recover_state(path):
    doc = json.loads(Path(path).read_text())
    prov = doc.get('provenance') or {}
    if prov.get('complete') is not True or not prov.get('source') or not prov.get('retrieved_at'):
        raise ValueError('provenance needs complete=true, source and retrieved_at')
    commented, digest = doc.get('commented'), doc.get('digest')
    if not isinstance(commented, dict) or not all(
            isinstance(v, dict) and v.get('comment_id') and v.get('created_at') for v in commented.values()):
        raise ValueError('each commented entry needs comment_id and created_at')
    if digest is not None and not (isinstance(digest, dict) and digest.get('day') and digest.get('comment_id')):
        raise ValueError('digest is null or {day, comment_id}')
    state = {'commented': {k: v['created_at'] for k, v in commented.items()},
             'digest_day': digest['day'] if digest else ''}
    atomic_state.recover(STATE, state, validate_state, doc)


def main():
    if PAUSE.exists():
        return
    state = atomic_state.load(STATE, validate_state)
    rows = sql(f"""select identifier, coalesce(title,''), regexp_replace(coalesce(description,''), '\\s+', ' ', 'g')
                   from issues where company_id='{COMPANY}' and status not in ('done','cancelled')
                   and (title ~* 'owner|human' or description ~* 'owner|human') order by updated_at desc""")
    hits = []
    for line in rows.splitlines():
        parts = line.split('|', 2)
        if len(parts) < 3:
            continue
        ident, title, desc = parts
        if ident in SKIP:
            continue
        m = TITLE_ROUTE.search(title) or DESC_ROUTE.search(desc)
        if m:
            hits.append((ident, title, m.group(0)))
    budget = 5 if os.getloadavg()[0] > 60 else 10
    sent = 0
    for ident, title, matched in hits:
        if ident in state['commented'] or sent >= budget:
            continue
        log(f"{'DRY ' if DRY else ''}guard {ident} [{matched}] {title[:80]}")
        if not DRY:
            post_once(state, ident, MSG.format(m=matched))
        sent += 1
    today = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
    still = [(i, t) for i, t, _ in hits if TITLE_ROUTE.search(t)]
    if still and state.get('digest_day') != today and sent < budget:
        body = ("**owner-escalation-guard daily digest:** open cards whose TITLE still routes work to the owner. "
                "CEO, re-route or prove human-only (exact error) for each:\n" + '\n'.join(f"- {i}: {t[:110]}" for i, t in still[:30]))
        log(f"{'DRY ' if DRY else ''}digest {DIGEST_CARD}: {len(still)} cards")
        if not DRY:
            previous = state['digest_day']
            state['digest_day'] = today
            save_state(state)
            if not comment(DIGEST_CARD, body):
                state['digest_day'] = previous
                save_state(state)
    log(f"{'DRY ' if DRY else ''}summary: {len(hits)} owner-routed cards, {sent} newly flagged, title-routed {len(still)}")


if __name__ == '__main__':
    try:
        if '--recover-state' in sys.argv:
            recover_state(sys.argv[sys.argv.index('--recover-state') + 1])
        else:
            main()
    except atomic_state.StateError as e:
        sys.exit(f'owner-escalation-guard: refusing to act: {e}')
