#!/usr/bin/env python3
"""PR-first work supply (owner 2026-10-07 ~19:15Z: "I see many PRs that have not been merged, so there is work to do...
what is the best smart way to solve this so it is never an issue again?").

Finishing open work beats starting new work. At 19:10Z 92 PRs were open but only 21 had an active card. The supply
planner (an LLM with a strict READY rule) did not count open PRs as supply, and orphan_prs.py was never on a timer, so
~70 PRs sat unowned while 0/12 roles had work. This is deterministic, with no model judgement: within complete listing
coverage, every open PR in REPOS gets an owning card and its agent is told the single next action, derived from GitHub state:

  draft            -> finish it and mark it ready (gh pr ready), or close it with a reason if superseded
  conflicts        -> rebase/merge the base branch and resolve conflicts
  ci_failing       -> fix the failing checks on the PR branch
  review_findings  -> address every Paperclip Review finding until 5/5
  review_request   -> request review: `@togetherweown-reviewer review` via built-in GitHub (owner identity)
  merge_ready      -> re-check the exact head, then squash-merge (the doer merges; the review bot cannot); never on a held card

No owning card: create one (assignee and project from REPOS, PR linked as a work product). Owning card in any open
status: comment once per (PR, head, action). The comment wakes the assignee, who keeps their context; every nudge
passes card_hold() first, so blocked, backlog, descriptor-held, recovery-held and out-of-focus cards get no nudge
until the hold clears, and no card status is changed here. Cards with a live run are retried next
tick. TWO repos first; at most MAX_ACTIONS per run. Dry-run by default; --apply acts. PAUSE file stops it.

Pending-attachment ownership: a created card ID is persisted under a key of repository+PR alone
(`<repo>#<n>:pending-card`), independent of head/action, with the same-directory tmp+replace
atomic write as every other state update. The per-(head, action) success receipt is recorded only
after the PR work-product attachment is confirmed (POST returns an id, or a re-read finds that
same pending card linking the PR). Error or ambiguous attachment outcomes keep the pending entry
and retry or read-reconcile against that same card on later ticks; a changed head/action or a
restarted tick never creates a second card. When another open card already owns the PR the pending
entry is orphaned: the retry never attaches, drops the pending key, and reconciles against the
existing owner instead, so one PR never gains two open owners. A pending retry is bookkeeping, not
a wake: the retry-attach itself never posts a nudge, ignores holds, and never counts against
MAX_NEW_CARDS (it counts against MAX_ACTIONS like any action). A tick whose re-read confirms the
attachment resumes normal reconciliation and may nudge the now-owning card.

Diagnostics: creation logs `created <ident> for <who>: <key>`; a failed attachment logs
`attachment for <ident> failed (<detail>); retry next tick` and keeps the pending key. A
read-confirmed late attachment logs `attached <repo>#<n> to <ident> on retry` (with
`confirmed by read after error` when the POST failed but the work product had landed). An
orphaned pending entry logs `pending <ident> orphaned: <repo>#<n> already owned by <other>;
dropped pending for cleanup, using <other>` and posts no work product. Confirmation checks the
pending card itself (`card_has_pr`), never the top-ranked owner, so an ambiguous POST with
another owner in place clears instead of retrying every tick and re-posting a duplicate
`pull_request` row (the server dedupes only paperclip artifacts, not PR rows).
Recovery: inspect the pending key in `ops/productivity/pr-supply.state.json`. If the card was
closed or the work product is visible on another open card, remove only the pending key (never
delete cards: historical duplicates stay). The next tick then reconciles normally. A
`<repo>#<n>:create-timeout` key holds `since` (the create-attempt start minus 60 s, the
title-lookup bound) plus `at` (when the marker was written, the recheck-window start) and
clears itself once its window passes; an unreadable one restarts it.

Bounds: every subprocess is bounded (env-overridable *_TIMEOUT_S below). Curl uses
--max-time 30s (PR_SUPPLY_CURL_TIMEOUT_S, the process gets 10s more to exit); a timed-out
curl returns {}, which callers treat as a failure with no state write. A {} reply to a card
create is unknown, not a refusal, because the server may have committed the card. The PR then
gets a `<repo>#<n>:create-timeout` marker holding the create-attempt start minus 60 s (the
title-lookup bound, so a card committed during the timed-out POST is still found) plus the
marker write time (the recheck-window start), and no card is POSTed for it until a
`Finish <repo>#<n>` card created since the create attempt began is found by title (adopted as
pending) or CREATE_RECHECK_MIN (30 min) passes with none. `gh pr list` uses timeout 60s (PR_SUPPLY_GH_TIMEOUT_S); on timeout,
non-zero exit, empty stdout, invalid or non-list JSON, or an empty GH_TOKEN the tick logs one line
`pr-supply: list failed for <repo>: <reason>` and makes zero writes for that repo, continuing with
the other repos. An empty token refuses before `gh` is called. A tick with any failed listing
leaves merge-ready-held.txt unchanged. If every repo's listing fails in one tick the script exits
non-zero (never a healthy empty run).
Over-bound detection: `gh pr list` asks for PR_SUPPLY_LIST_BOUND+1 rows (101) in one call. When the
probe row arrives the repo holds more open PRs than the bound: probe_prs returns ListingResult
(first 100 rows, incomplete=True); list_prs is a legacy rows-only adapter, never the health path.
Read-only probe diagnostics go to stdout, including
`pr-supply: list incomplete for <repo>: bound 100 observed 101+`. An --apply tick records
`<repo>:list-incomplete` {bound, observed, observed_min, at} in state for the next tick;
observed and observed_min are both 101, a lower bound, never an exact repository total.
The bounded set is still reconciled within MAX_ACTIONS/MAX_NEW_CARDS; the summary counts
bounded PRs only and merge-ready-held.txt stays unchanged, exactly as on a failed listing.
A later complete listing clears the marker. ANY incomplete tick returns status 2 after its
bounded work (including dry-run); __main__ propagates it. All-failed refusal stays status 1.
The first 100 preserve gh's newest-first listing order; unseen PRs have no coverage or
fairness guarantee. gh 2.102.0 cursor-paginates internally past its 100-row PAGE cap to
honor --limit 101, within the same bounded subprocess; no separate count query is used.
Apply ticks hold a nonblocking flock on the stable STATE-sibling `<state-name>.lock`
from before state reads through all actions, marker/receipt saves and HELD handling.
Contenders skip with stdout only, before listing or state access; the lock inode is never
unlinked. Close releases it on exceptions too. PAUSE/pressure skips and dry-run create no lock;
dry-run creates no log, state or HELD file either.
"""
import calendar
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_error_rerequest as rr  # noqa: E402  (psql, gh_env, comment, CID, ROOT)

ROOT = rr.ROOT
STATE = ROOT / 'ops/productivity/pr-supply.state.json'
PAUSE = ROOT / 'ops/productivity/pr-supply.PAUSE'
LOG = ROOT / 'ops/worker-vps/operator-actions.log'
APPLY = '--apply' in sys.argv
MAX_ACTIONS = 20
MAX_NEW_CARDS = 6
PR_SUPPLY_LIST_BOUND = 100  # per-repo listing cap; `gh pr list` probes with BOUND+1 to detect over-bound repos
MIN_AGE_MIN = 30
CREATE_RECHECK_MIN = 30  # a timed-out card create is looked up by title this long before it is retried
CREATE_LOOKUP_SKEW_S = 60  # title-lookup bound sits this far before the create attempt began
RENUDGE_H = 6        # an unchanged PR is nudged again after this many hours (agents that replied but did not act)
STALE_DAYS = 14      # older PRs get the finish-or-close instruction
HELD = ROOT / 'ops/productivity/merge-ready-held.txt'  # merge-ready PRs whose card is blocked, with the root blocker


def _seconds(name, default):
    v = float(os.environ.get(name, default))
    if not 0 < v <= 3600:
        raise SystemExit(f'{name} must be seconds in (0, 3600], got {v}')
    return v


# A hung curl or gh call used to wedge the tick until someone killed it by hand.
PR_SUPPLY_CURL_TIMEOUT_S = _seconds('PR_SUPPLY_CURL_TIMEOUT_S', 30)  # curl --max-time; the process gets 10 s more
PR_SUPPLY_GH_TIMEOUT_S = _seconds('PR_SUPPLY_GH_TIMEOUT_S', 60)  # every gh pr list call
REPOS = {  # repo: (assignee agent, project, rank: lower runs first)
    'two-bot-next': ('Founding Engineer', 'TWO Bot Next', 0),
    'two-web-next': ('Web Engineer', 'TWO Web Next', 0),
    'kofra': ('Web Engineer', 'Kofra', 1),
    'paperclip-ops': ('DevOps & Reliability Engineer', 'Ops Tooling', 2),
    'paperclip-operator-toolkit': ('Automation Engineer', 'Operator Toolkit (public)', 2),
    'paperclip-model-router': ('Automation Engineer', 'Model Router Plugin', 2),
    'paperclip': ('Upstream & Public GitHub Steward', 'Paperclip Fork', 2),
    'nntune': ('Founding Engineer', 'NNTune', 3),
    'routeware-shadow-api': ('DevOps & Reliability Engineer', 'Routeware Shadow API', 3),
}
ACTION = {
    'draft': "it is still a **draft**. Finish the work and mark it ready yourself (`gh pr ready`), or close it with a "
             "one-line reason if it is superseded or no longer wanted.",
    'conflicts': "it has **merge conflicts**. Update it from the base branch, resolve the conflicts, and push.",
    'ci_failing': "**CI is failing** on the current head. Fix the failing checks on the PR branch.",
    'review_findings': "**Paperclip Review found issues**. Address every finding and push until it scores 5/5.",
    'review_request': "it needs a **(re-)review**. Comment `@togetherweown-reviewer review` on the PR through "
                      "Paperclip's built-in GitHub (owner identity). A mention from the App or a bot starts a "
                      "low-trust review that cannot run.",
    'merge_ready': "it is **CLEAN with Paperclip Review 5/5**. Re-check the exact head before you merge: CI green, every "
                   "finding answered, and no open CISO, CEO, security or release gate. A clean check never overrides a hold.",
}


def classify(p):
    chk = p.get('statusCheckRollup') or []
    name = lambda c: c.get('name') or c.get('context') or ''
    concl = lambda c: c.get('conclusion') or c.get('state') or c.get('status') or ''
    rv = [concl(c) for c in chk if name(c) == 'Paperclip Review']
    failing = any(concl(c) in ('FAILURE', 'ERROR', 'TIMED_OUT') and name(c) != 'Paperclip Review' for c in chk)
    if p['isDraft']:
        return 'draft'
    if p['mergeStateStatus'] == 'DIRTY':
        return 'conflicts'
    if failing:
        return 'ci_failing'
    if not rv or rv[0] == 'ACTION_REQUIRED':
        return 'review_request'
    if rv[0] == 'FAILURE':
        return 'review_findings'
    if rv[0] == 'SUCCESS' and p['mergeStateStatus'] == 'CLEAN':
        return 'merge_ready'
    return None  # pending checks/review in flight: nothing to do yet


def owning_card(repo_full, n):
    match = ' or '.join(f"lower(w.url) like '{p}'" for p in rr.pr_url_patterns(repo_full, n))
    rows = rr.psql(f"""select i.id, i.identifier, i.status from issues i where i.company_id='{rr.CID}'
        and i.status not in ('done','cancelled') and i.origin_kind is distinct from 'chat_channel'
        and exists (select 1 from issue_work_products w where w.issue_id=i.id and ({match}))
        order by (i.status in ('in_progress','todo','in_review')) desc, i.updated_at desc limit 1""")
    return rows[0] if rows else None


def card_has_pr(card_id, repo_full, n):
    """True when that specific open card links the PR (same filters as owning_card).

    owning_card() returns only the single top-ranked owner, so comparing the
    pending card against it cannot confirm the pending attachment and cannot
    see a second owner. Check the pending card itself instead.
    """
    safe_id = str(card_id).replace("'", "''")
    match = ' or '.join(f"lower(w.url) like '{p}'" for p in rr.pr_url_patterns(repo_full, n))
    rows = rr.psql(f"""select 1 from issues i where i.id='{safe_id}' and i.company_id='{rr.CID}'
        and i.status not in ('done','cancelled') and i.origin_kind is distinct from 'chat_channel'
        and exists (select 1 from issue_work_products w where w.issue_id=i.id and ({match})) limit 1""")
    return bool(rows)


def find_card_by_title(repo, n, since):
    rows = rr.psql(f"""select i.id, i.identifier from issues i where i.company_id='{rr.CID}'
        and i.title='Finish {repo}#{n}' and i.status not in ('done','cancelled')
        and i.created_at >= '{since}'::timestamptz order by i.created_at desc limit 1""")
    return rows[0] if rows else None


PENDING_SUFFIX = ':pending-card'


def pending_key(repo_full, n):
    return f'{repo_full}#{n}{PENDING_SUFFIX}'


def pending_entry(state, repo_full, n):
    v = state.get(pending_key(repo_full, n))
    if not isinstance(v, dict):
        return None
    if not isinstance(v.get('card_id'), str) or not isinstance(v.get('identifier'), str):
        return None
    return v


def attach_work_product(card_id, pr_url, full, n):
    """POST the PR work product; (True, id) only when the server confirms it.

    Anything else (transport exception, non-JSON, or a payload without an id)
    is (False, detail): the caller keeps the pending entry and retries or
    read-reconciles against the same card. No broader transport rewrite.
    """
    try:
        r = api('POST', f'issues/{card_id}/work-products', {'type': 'pull_request', 'provider': 'github',
                                                            'url': pr_url, 'title': f'{full}#{n}', 'status': 'active'})
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    if isinstance(r, dict) and r.get('id'):
        return True, str(r.get('id'))
    try:
        detail = json.dumps(r)[:200]
    except (TypeError, ValueError):
        detail = str(r)[:200]
    return False, detail or 'empty response'


def save_state(state):
    tmp = STATE.with_name(f'{STATE.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE)


def root_blocker(iid):
    chain, cur = [], iid
    for _ in range(8):
        rows = rr.psql(f"""select b.id, b.identifier, b.status, coalesce(a.name,'-') from issue_relations ir
            join issues b on b.id=ir.issue_id left join agents a on a.id=b.assignee_agent_id
            where ir.related_issue_id='{cur}' and ir.type='blocks' and b.status not in ('done','cancelled') limit 1""")
        if not rows:
            break
        cur = rows[0][0]
        chain.append(f'{rows[0][1]}({rows[0][2]},{rows[0][3]})')
    return ' <- '.join(chain) or 'no blocker edge (status-only block)'


def api(method, path, body):
    """Control-plane call; {} on timeout or bad JSON, which callers already treat as failure."""
    try:
        r = subprocess.run(['curl', '-sK', str(ROOT / '.pc.curl'), '-X', method, f'{rr.API}/{path}',
                            '-H', 'content-type: application/json', '-d', json.dumps(body),
                            '--max-time', f'{PR_SUPPLY_CURL_TIMEOUT_S:g}'],
                           capture_output=True, text=True, timeout=PR_SUPPLY_CURL_TIMEOUT_S + 10)
    except subprocess.TimeoutExpired:
        return {}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def log(msg, *, persist=True):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} pr-supply: {msg}"
    print(line)
    if APPLY and persist:
        with LOG.open('a') as f:
            f.write(line + '\n')


class ListingResult(NamedTuple):
    """Bounded rows and explicit probe verdict; copying rows cannot erase health."""

    rows: list
    incomplete: bool


def list_prs(full, env):
    """Legacy rows-only adapter. Reconciliation must use probe_prs for coverage."""
    result = probe_prs(full, env)
    return result.rows if result is not None else None


def probe_prs(full, env):
    """Open-PR listing for one repo, or None when the listing failed.

    Probes with `--limit BOUND+1` in a single call. On timeout, non-zero exit,
    empty stdout, invalid or non-list JSON, or an empty GH_TOKEN the failure is
    logged as `pr-supply: list failed for <repo>: <reason>` and the caller makes
    zero writes for that repo. An empty token refuses before `gh` is called.
    When the probe row arrives the repo is over the bound: the first BOUND rows
    are returned with `incomplete` set and one line
    `pr-supply: list incomplete for <repo>: bound <B> observed <B+1>+` is logged.
    """
    if not (env or {}).get('GH_TOKEN'):
        log(f'list failed for {full}: empty token', persist=False)
        return None
    try:
        r = subprocess.run(['gh', 'pr', 'list', '-R', full, '--state', 'open', '--limit',
                            str(PR_SUPPLY_LIST_BOUND + 1), '--json',
                            'number,title,url,isDraft,mergeStateStatus,headRefOid,statusCheckRollup,createdAt'],
                           capture_output=True, text=True, env=env, timeout=PR_SUPPLY_GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log(f'list failed for {full}: timeout after {PR_SUPPLY_GH_TIMEOUT_S:g}s', persist=False)
        return None
    if r.returncode != 0:
        log(f'list failed for {full}: gh exit {r.returncode}', persist=False)
        return None
    if not (r.stdout or '').strip():
        log(f'list failed for {full}: empty output', persist=False)
        return None
    try:
        prs = json.loads(r.stdout)
    except ValueError:
        log(f'list failed for {full}: invalid JSON', persist=False)
        return None
    if not isinstance(prs, list):
        log(f'list failed for {full}: invalid JSON', persist=False)
        return None
    if len(prs) > PR_SUPPLY_LIST_BOUND:
        log(f'list incomplete for {full}: bound {PR_SUPPLY_LIST_BOUND} observed {PR_SUPPLY_LIST_BOUND + 1}+',
            persist=False)
        return ListingResult(prs[:PR_SUPPLY_LIST_BOUND], True)
    return ListingResult(prs, False)


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from host_ok import host_ok
    ok, why = host_ok()
    if not ok:
        print(f'host under pressure ({why}): no new wakes this run')
        return
    if PAUSE.exists():
        return
    if not APPLY:
        return _reconcile()
    # Lock a stable sibling inode, not STATE (which save_state atomically replaces).
    # Closing on every return/exception releases it; never unlink a shared lock.
    with STATE.with_name(f'{STATE.name}.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('pr-supply: apply tick already running; skipped')
            return
        return _reconcile()


def _reconcile():
    """Read/reconcile/write one tick; main holds the apply lock for its full lifetime."""
    env = rr.gh_env()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    agents = {r[0]: r[1] for r in rr.psql(f"select name, id from agents where company_id='{rr.CID}' and status<>'terminated'")}
    projects = {r[0]: r[1] for r in rr.psql(f"select name, id from projects where company_id='{rr.CID}' and archived_at is null")}
    todo = []
    list_failed = 0
    list_incomplete = 0
    markers_dirty = False
    for repo, (who, proj, rank) in REPOS.items():
        full = f'TogetherWeOwn/{repo}'
        result = probe_prs(full, env)
        if result is None:
            list_failed += 1
            continue
        if (not isinstance(result, ListingResult) or type(result.incomplete) is not bool
                or not isinstance(result.rows, list) or len(result.rows) > PR_SUPPLY_LIST_BOUND):
            log(f'list failed for {full}: invalid listing result')
            list_failed += 1
            continue
        if result.incomplete:
            list_incomplete += 1
            if APPLY:
                state[f'{full}:list-incomplete'] = {
                    'bound': PR_SUPPLY_LIST_BOUND, 'observed': PR_SUPPLY_LIST_BOUND + 1,
                    'observed_min': PR_SUPPLY_LIST_BOUND + 1,
                    'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
                markers_dirty = True
        elif APPLY and state.pop(f'{full}:list-incomplete', None) is not None:
            markers_dirty = True
        for p in result.rows:
            need = classify(p)
            if not need or p['title'].startswith('chore(main): release'):
                continue
            age_min = (time.time() - calendar.timegm(time.strptime(p['createdAt'], '%Y-%m-%dT%H:%M:%SZ'))) / 60
            if age_min < MIN_AGE_MIN:
                continue
            todo.append((rank, p['createdAt'], repo, full, who, proj, p, need))
    if markers_dirty:
        save_state(state)
    if REPOS and list_failed == len(REPOS):
        print('pr-supply: all repo listings failed; no healthy empty run', file=sys.stderr)
        raise SystemExit(1)
    todo.sort(key=lambda t: (t[0], t[1]))
    actions = new_cards = 0
    held = []
    for rank, _, repo, full, who, proj, p, need in todo:
        if actions >= MAX_ACTIONS:
            break
        n, head = p['number'], p['headRefOid']
        key = f'{full}#{n}@{head}:{need}'
        pk = pending_key(full, n)
        ctk = f'{full}#{n}:create-timeout'
        pend = pending_entry(state, full, n)
        if pend:
            if not APPLY:
                log(f"would retry PR attachment for {pend['identifier']}: {full}#{n}")
                actions += 1
                continue
            try:
                seen = owning_card(full.lower(), n)
            except Exception as e:
                print(f'{full}#{n}: pending {pend["identifier"]} owner lookup failed ({type(e).__name__}); retry next tick')
                continue
            try:
                pend_owns = card_has_pr(pend['card_id'], full.lower(), n)
            except Exception as e:
                print(f'{full}#{n}: pending {pend["identifier"]} owner lookup failed ({type(e).__name__}); retry next tick')
                continue
            if pend_owns:
                state.pop(pk, None)
                save_state(state)
                if seen and seen[0] != pend['card_id']:
                    log(f"pending {pend['identifier']} orphaned: {full}#{n} already owned by "
                        f"{seen[1]}; dropped pending for cleanup, using {seen[1]}")
                    card = seen
                else:
                    log(f"attachment for {pend['identifier']} confirmed by read: {full}#{n}")
                    card = seen if seen else [pend['card_id'], pend['identifier'], 'in_progress']
            elif seen and seen[0] != pend['card_id']:
                state.pop(pk, None)
                save_state(state)
                log(f"pending {pend['identifier']} orphaned: {full}#{n} already owned by "
                    f"{seen[1]}; dropped pending for cleanup, using {seen[1]}")
                card = seen
            else:
                ok, detail = attach_work_product(pend['card_id'], p['url'], full, n)
                now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                if ok:
                    state.pop(pk, None)
                    state[key] = f"created {pend['identifier']} {now}"
                    save_state(state)
                    log(f"attached {full}#{n} to {pend['identifier']} on retry")
                    actions += 1
                    continue
                try:
                    pend_owns_after = card_has_pr(pend['card_id'], full.lower(), n)
                except Exception:
                    pend_owns_after = False
                try:
                    reconciled = owning_card(full.lower(), n)
                except Exception:
                    reconciled = None
                if pend_owns_after or (reconciled and reconciled[0] == pend['card_id']):
                    state.pop(pk, None)
                    state[key] = f"created {pend['identifier']} {now}"
                    save_state(state)
                    log(f"attached {full}#{n} to {pend['identifier']} on retry (confirmed by read after error)")
                    actions += 1
                    continue
                if reconciled and reconciled[0] != pend['card_id']:
                    state.pop(pk, None)
                    save_state(state)
                    log(f"pending {pend['identifier']} orphaned: {full}#{n} already owned by "
                        f"{reconciled[1]}; dropped pending for cleanup, using {reconciled[1]}")
                    card = reconciled
                else:
                    pend['attempts'] = int(pend.get('attempts', 0)) + 1
                    pend['last_error'] = detail
                    pend['last_attempt'] = now
                    state[pk] = pend
                    save_state(state)
                    log(f"attachment for {pend['identifier']} failed ({detail}); retry next tick")
                    actions += 1
                    continue
        prev = state.get(key)
        if isinstance(prev, str) and prev:
            last = prev.rsplit(' ', 1)[-1]
            try:
                age_h = (time.time() - calendar.timegm(time.strptime(last, '%Y-%m-%dT%H:%M:%SZ'))) / 3600
            except ValueError:
                age_h = RENUDGE_H  # legacy entries (first nudges 10-07 19-22Z, before timestamps) are due now
            if age_h < RENUDGE_H:
                continue
        if not pend:
            card = owning_card(full.lower(), n)
        age_d = (time.time() - calendar.timegm(time.strptime(p['createdAt'], '%Y-%m-%dT%H:%M:%SZ'))) / 86400
        stale = (f" This PR is **{int(age_d)} days old**: finish it today, or close it with a one-line reason (superseded, "
                 "obsolete, or no longer wanted). Leaving it open is not an option.") if age_d >= STALE_DAYS else ''
        again = ' (Reminder: nothing changed on the PR since the last nudge.)' if prev else ''
        text = (f"**{full}#{n}** ({p['url']}): {ACTION[need]}{stale}{again} "
                "(host PR-supply reconciler: open PRs are this card's work.)")
        if not card:
            if new_cards >= MAX_NEW_CARDS:
                continue
            if not APPLY:
                log(f'would create a card for {who}: {key}')
                actions += 1
                new_cards += 1
                continue
            ct = state.get(ctk)
            if isinstance(ct, dict):
                try:
                    since = ct['since']
                    at = ct.get('at', since)
                    time.strptime(since, '%Y-%m-%dT%H:%M:%SZ')
                    age_min = (time.time() - calendar.timegm(time.strptime(at, '%Y-%m-%dT%H:%M:%SZ'))) / 60
                except (KeyError, TypeError, ValueError):
                    at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                    since = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                          time.gmtime(time.time() - CREATE_LOOKUP_SKEW_S))
                    age_min = 0
                    state[ctk] = {'since': since, 'at': at}
                    save_state(state)
                    log(f'create-timeout marker for {full}#{n} was unreadable; its window restarts now')
                try:
                    found = find_card_by_title(repo, n, since)
                except Exception as e:
                    print(f'{full}#{n}: create lookup failed ({type(e).__name__}); retry next tick')
                    continue
                if found:
                    state.pop(ctk)
                    state[pk] = {'card_id': found[0], 'identifier': found[1], 'pr_url': p['url'],
                                 'first_seen': since, 'attempts': 0}
                    save_state(state)
                    log(f"create for {full}#{n} found on retry: {found[1]}; attaching next tick")
                    continue
                if age_min < CREATE_RECHECK_MIN:
                    print(f'{key}: create timed out at {at}; no new card until the title lookup clears')
                    continue
                state.pop(ctk)
                save_state(state)
                log(f'create for {full}#{n} found no card since {since}; creating again')
            desc = (f"Own {p['url']} until it is **merged**, or **closed with a stated reason** (superseded, obsolete or "
                    f"rejected).\n\nNext action now: {ACTION[need]}\n\nGates: `pr-lint`, `ci-ok` and Paperclip Review 5/5 "
                    "on the current head. Squash-merge it yourself only after an exact-head re-check, and with no open "
                    "CISO, CEO, security or release gate on this card. CLEAN and review success never override a hold "
                    "or a production approval. Created by the host PR-supply reconciler because no card owned this PR.")
            t0 = time.time()
            c = api('POST', f'companies/{rr.CID}/issues', {
                'title': f"Finish {repo}#{n}", 'description': desc,
                'status': 'todo', 'priority': 'high' if rank == 0 else 'medium', 'assigneeAgentId': agents.get(who),
                'projectId': projects.get(proj)})
            if not c:
                at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0 - CREATE_LOOKUP_SKEW_S))
                state[ctk] = {'since': since, 'at': at}
                save_state(state)
                log(f'card create got no reply for {who}: {key}; no new card until the title lookup clears')
                actions += 1
                new_cards += 1
                continue
            if not c.get('id'):
                log(f'card create failed for {who}: {key}')
                continue
            now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            state[pk] = {'card_id': c['id'], 'identifier': c.get('identifier') or c['id'], 'pr_url': p['url'],
                         'first_seen': now, 'attempts': 0}
            save_state(state)
            ok, detail = attach_work_product(c['id'], p['url'], full, n)
            if not ok:
                try:
                    created_owns = card_has_pr(c['id'], full.lower(), n)
                except Exception:
                    created_owns = False
                try:
                    reconciled = owning_card(full.lower(), n)
                except Exception:
                    reconciled = None
                if created_owns or (reconciled and reconciled[0] == c['id']):
                    state.pop(pk, None)
                    state[key] = f"created {c.get('identifier')} {now}"
                    save_state(state)
                    log(f"created {c.get('identifier')} for {who}: {key} (attachment confirmed by read after error)")
                else:
                    state[pk]['attempts'] = 1
                    state[pk]['last_error'] = detail
                    state[pk]['last_attempt'] = now
                    save_state(state)
                    log(f"attachment for {c.get('identifier')} failed ({detail}); retry next tick")
                actions += 1
                new_cards += 1
                continue
            state.pop(pk, None)
            state[key] = f"created {c.get('identifier')} {now}"
            save_state(state)
            log(f"created {c.get('identifier')} for {who}: {key}")
            actions += 1
            new_cards += 1
            continue
        iid, ident, status = card
        if rr.live_run_guard.issue_has_live_work(iid):
            continue
        hold = rr.card_hold(iid, merge=(need == 'merge_ready'))
        if hold:
            print(f'{key}: {ident} held ({hold}); no {need} nudge')
            continue
        if need == 'merge_ready':
            text = rr.merge_notice(full, n, head)
        if not APPLY:
            log(f'would wake {ident} ({status}): {key}')
            actions += 1
            continue
        code = rr.comment(iid, text)
        log(f'woke {ident} ({status}) HTTP {code}: {key}')
        actions += 1
        if code.startswith('2'):
            state[key] = f"{ident} {status} {code} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
            save_state(state)
    for _, _, _, full, _, _, p, need in todo:
        if need == 'merge_ready':
            c = owning_card(full.lower(), p['number'])
            if c and c[2] == 'blocked':
                held.append(f"{full}#{p['number']} card {c[1]}: {root_blocker(c[0])}")
    if APPLY and not list_failed and not list_incomplete:
        HELD.write_text(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' merge-ready PRs held by blocked cards '
                        '(root blocker last):\n' + ''.join(h + '\n' for h in held))
    elif APPLY:
        parts = []
        if list_failed:
            parts.append(f'{list_failed} failed')
        if list_incomplete:
            parts.append(f'{list_incomplete} incomplete')
        log(f"merge-ready-held.txt kept unchanged: {', '.join(parts)} repo listing(s) this tick")
    print(f'{len(todo)} actionable open PRs; {actions} actions this run')
    return 2 if list_incomplete else 0


if __name__ == '__main__':
    raise SystemExit(main())
