#!/usr/bin/env python3
"""Rerun one red CI job before waking the PR author, then cap CI-red rounds.

The host PR controller calls :func:`handle_ci_tick` once per pass for one PR.
On the first red result for an eligible check the helper requests
``gh run rerun <id> --failed`` once per workflow run and holds the author wake
until that attempt concludes. A rerun that passes on the same head is recorded
as one flake in the ledger and stays quiet; a rerun that fails wakes exactly
as today (the caller-rendered ``wake_text`` is posted verbatim) and counts one
CI round. After 3 red rounds the helper posts one owner-card comment asking to
split the PR or fix/quarantine the failing test, then stays silent until a
head goes all green. Three flakes for one job/test in a rolling 7 days open
one ``[flake]`` card via the injected ``quarantine_card`` callable.

Fail-safe direction: anything the helper does not positively recognize as an
eligible test-only rerun wakes as today. Deny lists win over the allow-list,
unknown attempts/run ids wake, and ledger failures are absorbed (a failed
``record_flake`` is queued in state and retried on later ticks). The veto is
run-wide: ``gh run rerun --failed`` reruns every failed job in the run, so one
failing deny-listed sibling vetoes the whole run even when another job in it
is eligible. The helper never raises for ledger, card, ``gh`` or API
failures; ``gh`` failures (an exception or a nonzero exit code) leave the
tick unhandled so the next pass retries, while a failed wake/cap post
returns action ``'post_failed'`` so the host falls through to its normal wake
path (which owns the retry semantics) instead of dropping the wake.

State schema (owned by the helper, JSON-serializable, survives restarts)::

    {
      'flights': {flight_key: {'attempt', 'head', 'workflow', 'job', 'test', 'ts'}},
      'reran_runs': [run_id, ...],      # never rerun the same run twice
      'handled': [tick_key, ...],       # ticks already woken/capped
      'round_count': int,               # red rounds since last all-green
      'rounds': [{'head', 'job', ...}], # evidence for the cap comment (last 3)
      'flakes': [{'repo', 'workflow', 'job', 'test', 'ts'}],
      'pending_flakes': [entry, ...],   # queued while the ledger was down
      'flake_cards': [flake_key, ...],  # keys already carded
    }

A flight key is ``'<run_id>\\x1f<job>'`` (:func:`_flight_key`): one entry per
rerun job, since several required checks routinely share a workflow run and a
run-keyed flight would attribute a sibling's success to the wrong job. One
``gh run rerun --failed`` request covers the whole run, so fresh failures in
one run issue a single request and record one flight per eligible job. Bare
run-id keys from older states never match a lookup and expire via the TTL.

Cross-PR flake aggregation: pass ``shared=<host-level dict>`` and the flake
window (``flakes``/``pending_flakes``/``flake_cards``) lives there, so three
flakes for one job/test across different PRs still open one card. Flights,
handled ticks and the round counter always stay in the per-PR ``state``.
Omit ``shared`` to keep everything in ``state`` (unit tests do this).

Kill switch (no code change): set ``PR_CONTROLLER_CI_DISABLE`` to a truthy
value (``1``/``true``/``yes``/``on``) or create the file named by
``PR_CONTROLLER_CI_DISABLE_FILE``. The helper then wakes on first red exactly
like the pre-rerun controller and requests no reruns.

Shadow mode: pass ``dry_run=True`` to compute the decision without side
effects (no ``gh``/API/card/record calls) and without touching ``state``.
The host runs shadow state separately so dry runs can never leak flights or
rounds into the live state file.
"""

import copy
import json
import os
from pathlib import Path

# Test-only workflow file basenames that may be rerun once. Matched against
# the workflow file basename (case-insensitive); the host resolves each check
# run to its workflow file via the workflow-run `path` field. Extend for new
# repos here, or define CI_WORKFLOWS in flake_ledger, which takes precedence
# when present (see _allow_list). Never add deploy/migrate/drill/smoke here:
# the contract test rejects allow-list names containing those tokens.
CI_WORKFLOWS = frozenset({'check.yml', 'ci.yml', 'e2e.yml', 'pr-gates.yml'})

# Case-insensitive substring denies. Applied to BOTH the workflow name and the
# job name: a deny hit anywhere wakes as today without a rerun. Over-denying
# only misses a rerun (fail-safe); under-denying would rerun a security or
# deploy job, so this list is deliberately broad.
DENY_SUBSTRINGS = frozenset({
    # Security / supply-chain / policy jobs.
    'security', 'supply', 'policy', 'gitleaks', 'audit', 'zizmor',
    'codeql', 'secret', 'sast', 'semgrep', 'trivy', 'grype', 'osv',
    'scorecard', 'sbom', 'provenance', 'attest', 'license', 'dependabot',
    'snyk',
    # Deploy-like workflows.
    'deploy', 'migrate', 'drill', 'smoke', 'release', 'publish',
    'backup', 'restore', 'rollback',
})

MAX_ROUNDS = 3          # red rounds before the cap comment replaces the wake
FLAKE_CARD_THRESHOLD = 3  # flakes for one job/test before carding
FLAKE_WINDOW_S = 7 * 24 * 3600  # rolling window for the flake count
FLIGHT_TTL_S = 6 * 3600  # rerun older than this without a conclusion expires
MAX_ATTEMPT = 2         # runs at this attempt or higher are never rerun

_STATE_LIST_BOUNDS = {
    'handled': 200,
    'reran_runs': 1000,
    'flakes': 500,
    'pending_flakes': 50,
    'flake_cards': 200,
    'rounds': MAX_ROUNDS,
}

_TRUTHY = {'1', 'true', 'yes', 'on'}


def kill_switch_active():
    """True when the kill switch restores wake-on-first-red behaviour."""
    if os.environ.get('PR_CONTROLLER_CI_DISABLE', '').strip().lower() in _TRUTHY:
        return True
    flag = os.environ.get('PR_CONTROLLER_CI_DISABLE_FILE', '').strip()
    if flag:
        try:
            if Path(flag).exists():
                return True
        except OSError:
            pass
    return False


def _allow_list():
    """Test-only workflow names, preferring flake_ledger when it defines one."""
    try:
        from flake_ledger import CI_WORKFLOWS as names
    except Exception:
        return CI_WORKFLOWS
    if not names:
        return CI_WORKFLOWS
    try:
        cleaned = frozenset(str(name) for name in names)
    except TypeError:
        return CI_WORKFLOWS
    return cleaned or CI_WORKFLOWS


def _normalize_workflow(workflow):
    """Basename + lowercase so paths and display-case variants still match."""
    text = str(workflow or '').strip().replace('\\', '/')
    base = text.rsplit('/', 1)[-1]
    return base.lower()


def _denied(workflow, job):
    blob = f'{workflow or ""}\n{job or ""}'.lower()
    return any(token in blob for token in DENY_SUBSTRINGS)


def is_eligible(check):
    """True when a failing check may be rerun once by the helper."""
    if not isinstance(check.get('workflow'), str) or not check['workflow'].strip():
        return False
    if not isinstance(check.get('job'), str) or not check['job'].strip():
        return False
    if _normalize_workflow(check.get('workflow')) not in {
            name.lower() for name in _allow_list()}:
        return False
    if _denied(check.get('workflow'), check.get('job')):
        return False
    if check.get('private_repo') and check.get('paid_runner'):
        return False
    attempt = check.get('attempt')
    if type(attempt) is not int or attempt < 1 or attempt >= MAX_ATTEMPT:
        return False
    if check.get('run_id') is None:
        return False
    return True


def _flight_key(run_id, job):
    """One flight per rerun job: ``'<run_id>\\x1f<job>'``.

    The unit separator cannot plausibly appear in a job name, so sibling
    jobs sharing a workflow run never collide. Job match (plus the attempt
    gate) is what attributes a conclusion to the flight that caused it.
    """
    return f'{run_id}\x1f{job or ""}'


def _run_vetoed(checks, run_id):
    """True when a failing deny-listed sibling shares this workflow run.

    ``gh run rerun <id> --failed`` reruns every failed job in the run, not
    just the eligible one, so a failing security/supply-chain/policy or
    deploy-like sibling vetoes the whole run. Passing siblings cannot veto:
    ``--failed`` leaves them alone.
    """
    want = str(run_id)
    for check in checks:
        if (check.get('conclusion') or 'failure') == 'success':
            continue
        if str(check.get('run_id')) != want:
            continue
        if _denied(check.get('workflow'), check.get('job')):
            return True
    return False


def _retry_concluded(check, flight):
    """True unless the success row positively predates the rerun.

    A concluding retry has an attempt above the flight's; a missing attempt
    (classifiers that omit it) still counts, since the job match already
    ties the row to this flight. Only an explicit attempt at or below the
    flight's attempt withholds the flake: that success cannot be the retry.
    """
    attempt = check.get('attempt')
    if type(attempt) is not int:
        return True
    try:
        base = int(flight.get('attempt') or 0)
    except (TypeError, ValueError):
        return True
    return attempt > base


def _check_tuple(check):
    return (
        str(check.get('workflow') or ''),
        str(check.get('job') or ''),
        str(check.get('test') or ''),
        str(check.get('run_id') or ''),
        str(check.get('attempt') or ''),
        str(check.get('conclusion') or ''),
    )


def _tick_key(head_sha, checks):
    parts = sorted(_check_tuple(check) for check in checks)
    return f'{head_sha}|{json.dumps(parts, sort_keys=True)}'


def _flake_key(repo, workflow, job, test):
    return '|'.join([str(repo or ''), str(workflow or ''),
                     str(job or ''), str(test or '')])


def _trim(state):
    for key, bound in _STATE_LIST_BOUNDS.items():
        items = state.get(key)
        if isinstance(items, list) and len(items) > bound:
            state[key] = items[-bound:]


def _prune_flights(state, now):
    flights = state.get('flights')
    if not isinstance(flights, dict):
        state['flights'] = {}
        return
    expired = [run_id for run_id, flight in flights.items()
               if not isinstance(flight, dict)
               or not isinstance(flight.get('ts'), (int, float))
               or now - flight['ts'] > FLIGHT_TTL_S]
    for run_id in expired:
        flights.pop(run_id, None)


def _prune_flakes(state, now):
    flakes = state.get('flakes')
    if not isinstance(flakes, list):
        state['flakes'] = []
        return
    state['flakes'] = [entry for entry in flakes
                       if isinstance(entry, dict)
                       and isinstance(entry.get('ts'), (int, float))
                       and now - entry['ts'] < FLAKE_WINDOW_S]


def _safe_verdict(ledger, repo, key):
    try:
        verdict = ledger.verdict(repo, key)
    except Exception:
        return 'unavailable'
    text = str(verdict or '').strip().lower()
    return text if text in ('yes', 'no') else 'unavailable'


def _safe_record(ledger, entry):
    try:
        ledger.record_flake(dict(entry))
    except Exception:
        return False
    return True


def _flush_pending(state, ledger):
    pending = state.get('pending_flakes')
    if not isinstance(pending, list) or not pending:
        state['pending_flakes'] = []
        return
    kept = []
    for entry in pending:
        if not isinstance(entry, dict) or not _safe_record(ledger, entry):
            if isinstance(entry, dict):
                kept.append(entry)
    state['pending_flakes'] = kept


def _post(api, owner_card_id, body):
    """Post one owner-card comment; return the comment id or None."""
    try:
        _code, payload = api('POST', f'issues/{owner_card_id}/comments',
                             {'body': body})
    except Exception:
        return None
    try:
        comment_id = json.loads(payload).get('id')
    except (TypeError, ValueError, AttributeError):
        return None
    return str(comment_id) if comment_id else None


def _gh_ok(result):
    """True when a gh callable's return spells success.

    The host adapter returns ``(exit_code, text)`` and never raises for a
    rejected rerun (403/422, reruns disabled); fakes return ``None`` or a
    ``(0, ...)`` tuple. Only explicit nonzero exit evidence fails: anything
    else degrades to success so unknown-but-harmless adapters keep working.
    """
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result == 0
    if isinstance(result, (tuple, list)) and len(result) > 0:
        return _gh_ok(result[0])
    return True


def _rerun(gh, run_id):
    try:
        result = gh(['gh', 'run', 'rerun', str(run_id), '--failed'])
    except Exception:
        return False
    return _gh_ok(result)


def _render_cap(pr, rounds, ledger):
    lines = [
        '**CI red: 3 red rounds on this PR -- time to re-scope.**',
        '',
        (f"This PR has failed required checks on {len(rounds)} different "
         'heads even after an automatic one-time rerun per run. The cap is '
         'reached: the controller keeps rerunning fresh failures once but '
         'posts no further author wakes until a head goes all green.'),
        '',
        ('Please split the PR into smaller slices, or fix or quarantine '
         'the failing test:'),
        '',
    ]
    for item in rounds:
        key = item.get('test') or item.get('job')
        verdict = _safe_verdict(ledger, pr.get('repo'), key)
        head8 = str(item.get('head') or '')[:8]
        lines.append(f'- {head8} {item.get("workflow")}/{item.get("job")}: '
                     f'ledger verdict {verdict}')
    lines += [
        '',
        ('Evidence: one automatic rerun was requested per run above and the '
         'retry still failed. Reset: the next all-green required-check '
         'result clears this counter.'),
    ]
    return '\n'.join(lines)


def _observe_flake(store, *, pr, check, run_id, ledger, quarantine_card, now):
    """Record one fail-then-pass flake; maybe open the [flake] card.

    ``store`` is the shared flake window (or the per-PR state when no
    shared store was passed), so the 7-day count spans PRs.
    """
    entry = {
        'repo': pr.get('repo'),
        'workflow': check.get('workflow'),
        'job': check.get('job'),
        'test': check.get('test'),
        'run_id': check.get('run_id'),
        'attempt': check.get('attempt'),
        'head_sha': pr.get('head_sha'),
    }
    if not _safe_record(ledger, entry):
        pending = store.setdefault('pending_flakes', [])
        pending.append(entry)
    flakes = store.setdefault('flakes', [])
    flakes.append({
        'repo': entry['repo'], 'workflow': entry['workflow'],
        'job': entry['job'], 'test': entry['test'], 'ts': now,
        'run_id': entry['run_id'],
    })
    key = _flake_key(entry['repo'], entry['workflow'], entry['job'],
                     entry['test'])
    carded = store.setdefault('flake_cards', [])
    if key in carded:
        return
    window = [item for item in flakes
              if isinstance(item, dict)
              and _flake_key(item.get('repo'), item.get('workflow'),
                             item.get('job'), item.get('test')) == key]
    if len(window) < FLAKE_CARD_THRESHOLD:
        return
    run_ids = sorted({str(item.get('run_id')) for item in window
                      if item.get('run_id') is not None})
    payload = {
        'title': (f"[flake] {entry['repo']} {entry['workflow']}/"
                  f"{entry['job']} flaked {len(window)} times in 7 days"),
        'repo': entry['repo'],
        'workflow': entry['workflow'],
        'job': entry['job'],
        'test': entry['test'],
        'count': len(window),
        'window_days': 7,
        'run_ids': run_ids,
    }
    try:
        quarantine_card(payload)
    except Exception:
        return
    carded.append(key)
    _ = run_id


def handle_ci_tick(*, pr, checks, state, gh, api, ledger,
                   quarantine_card, wake_text, now, dry_run=False,
                   shared=None):
    """Run one controller pass for one PR; see the module docstring."""
    outcome = {'action': 'suppressed', 'rounds': 0, 'rerun_run_ids': []}
    if dry_run:
        outcome['dry_run'] = True
    checks = list(checks or [])
    head = pr.get('head_sha')
    owner = pr.get('owner_card_id')
    store = shared if isinstance(shared, dict) else state

    if kill_switch_active():
        failing = [check for check in checks
                   if (check.get('conclusion') or 'failure') != 'success']
        if not failing:
            outcome['action'] = 'green'
            return outcome
        if dry_run:
            outcome['action'] = 'woke'
            return outcome
        comment_id = _post(api, owner, wake_text)
        if comment_id is None:
            outcome['action'] = 'post_failed'
            _trim(state)
            return outcome
        handled = state.setdefault('handled', [])
        key = _tick_key(head, checks)
        if key not in handled:
            handled.append(key)
        outcome['action'] = 'woke'
        outcome['comment_id'] = comment_id
        _trim(state)
        return outcome

    if dry_run:
        return _decide(pr, checks, copy.deepcopy(state), gh=None, api=None,
                       ledger=ledger, quarantine_card=None,
                       wake_text=wake_text, now=now, outcome=outcome,
                       mutate=False,
                       flake_store=copy.deepcopy(store))

    _flush_pending(store, ledger)
    _prune_flights(state, now)
    _prune_flakes(store, now)
    _trim(state)
    if store is not state:
        _trim(store)
    outcome = _decide(pr, checks, state, gh=gh, api=api, ledger=ledger,
                      quarantine_card=quarantine_card, wake_text=wake_text,
                      now=now, outcome=outcome, mutate=True,
                      flake_store=store)
    _trim(state)
    if store is not state:
        _trim(store)
    return outcome


def _decide(pr, checks, state, *, gh, api, ledger, quarantine_card,
            wake_text, now, outcome, mutate, flake_store):
    head = pr.get('head_sha')
    owner = pr.get('owner_card_id')
    flights = state.setdefault('flights', {})
    reran = set(state.setdefault('reran_runs', []))
    handled = state.setdefault('handled', [])
    key = _tick_key(head, checks)
    outcome['rounds'] = int(state.get('round_count') or 0)

    failing = [check for check in checks
               if (check.get('conclusion') or 'failure') != 'success']
    if not failing:
        for check in checks:
            run_id = check.get('run_id')
            if run_id is None:
                continue
            fkey = _flight_key(run_id, check.get('job'))
            flight = flights.get(fkey)
            if not isinstance(flight, dict):
                continue
            flights.pop(fkey, None)
            if mutate and _retry_concluded(check, flight):
                _observe_flake(flake_store, pr=pr, check=check, run_id=run_id,
                               ledger=ledger, quarantine_card=quarantine_card,
                               now=now)
        if mutate:
            state['round_count'] = 0
            state['rounds'] = []
            state.pop('capped_head', None)
        outcome['action'] = 'green'
        outcome['rounds'] = 0
        return outcome

    if key in handled:
        outcome['action'] = 'suppressed'
        return outcome

    # Fresh eligible failures rerun first; conclusions and wakes wait a pass.
    fresh = []
    for check in checks:
        if (check.get('conclusion') or 'failure') == 'success':
            continue
        run_id = check.get('run_id')
        if run_id is None or run_id in reran \
                or str(run_id) in {str(item) for item in reran}:
            continue
        if _flight_key(run_id, check.get('job')) in flights:
            continue
        if is_eligible(check) and not _run_vetoed(checks, run_id):
            fresh.append(check)
    if fresh:
        rerun_ids = []
        requested = set()
        failed = set()
        for check in fresh:
            run_id = check.get('run_id')
            rkey = str(run_id)
            if rkey in requested:
                # One `gh run rerun --failed` retries every failed job in
                # the run, so a second eligible row in the same run only
                # needs its own flight, not a second request. A failed
                # request records no flight at all (no phantom flights).
                if mutate and rkey not in failed:
                    flights[_flight_key(run_id, check.get('job'))] = {
                        'attempt': check.get('attempt'),
                        'head': head,
                        'workflow': check.get('workflow'),
                        'job': check.get('job'),
                        'test': check.get('test'),
                        'ts': now,
                    }
                continue
            if not mutate:
                requested.add(rkey)
                rerun_ids.append(run_id)
                continue
            if not _rerun(gh, run_id):
                # Failed request stays unhandled; retried next pass. The
                # run is still marked requested so a sibling row does not
                # issue a second request for the same run in this tick.
                requested.add(rkey)
                failed.add(rkey)
                continue
            requested.add(rkey)
            flights[_flight_key(run_id, check.get('job'))] = {
                'attempt': check.get('attempt'),
                'head': head,
                'workflow': check.get('workflow'),
                'job': check.get('job'),
                'test': check.get('test'),
                'ts': now,
            }
            reran.add(run_id)
            rerun_ids.append(run_id)
        state['reran_runs'] = sorted(reran, key=str)
        if rerun_ids:
            outcome['action'] = 'reran'
            outcome['rerun_run_ids'] = rerun_ids
            return outcome
        # Every rerun request failed: fall through to conclusions/wakes below
        # so a broken `gh` degrades to today's wake instead of silence.

    # Rerun conclusions: green records a flake, red counts one round.
    # Conclusions match on (run, job): a sibling's success must neither pop
    # this job's flight nor record a flake for a job that never reran.
    concluded_red = []
    for check in checks:
        run_id = check.get('run_id')
        fkey = _flight_key(run_id, check.get('job'))
        flight = flights.get(fkey) if run_id is not None else None
        if not isinstance(flight, dict):
            continue
        if (check.get('conclusion') or 'failure') == 'success':
            flights.pop(fkey, None)
            if mutate and _retry_concluded(check, flight):
                _observe_flake(flake_store, pr=pr, check=check, run_id=run_id,
                               ledger=ledger,
                               quarantine_card=quarantine_card, now=now)
            continue
        attempt = check.get('attempt')
        try:
            concluded = type(attempt) is int and attempt > int(
                flight.get('attempt') or 0)
        except (TypeError, ValueError):
            concluded = False
        if concluded:
            concluded_red.append(check)
    if concluded_red:
        for check in concluded_red:
            flights.pop(_flight_key(check.get('run_id'), check.get('job')),
                        None)
        if mutate:
            state['round_count'] = int(state.get('round_count') or 0) + 1
            evidence = state.setdefault('rounds', [])
            for check in concluded_red:
                evidence.append({
                    'head': head,
                    'workflow': check.get('workflow'),
                    'job': check.get('job'),
                    'test': check.get('test'),
                    'run_id': check.get('run_id'),
                })
            outcome['rounds'] = state['round_count']
        else:
            outcome['rounds'] = int(state.get('round_count') or 0) + 1
        if outcome['rounds'] >= MAX_ROUNDS and not mutate:
            # Dry run past the cap still reports the capped decision once.
            pass
        if outcome['rounds'] < MAX_ROUNDS:
            if not mutate:
                outcome['action'] = 'woke'
                return outcome
            comment_id = _post(api, owner, wake_text)
            if comment_id is None:
                # Nothing posted: the host falls through to its normal wake
                # path (which owns the retry) instead of recording a wake.
                outcome['action'] = 'post_failed'
                return outcome
            handled.append(key)
            outcome['action'] = 'woke'
            outcome['comment_id'] = comment_id
            return outcome
        if outcome['rounds'] == MAX_ROUNDS:
            if not mutate:
                outcome['action'] = 'capped'
                return outcome
            body = _render_cap(pr, state.get('rounds', []), ledger)
            comment_id = _post(api, owner, body)
            if comment_id is None:
                state['round_count'] -= 1  # re-decide next pass
                outcome['action'] = 'post_failed'
                return outcome
            handled.append(key)
            state['capped_head'] = head
            outcome['action'] = 'capped'
            outcome['comment_id'] = comment_id
            return outcome
        if mutate:
            handled.append(key)
        outcome['action'] = 'suppressed'
        return outcome

    # Anything left is ineligible (deny-listed, run-vetoed, paid/private,
    # attempt >= 2, or an already-reran run whose retry never concluded):
    # wake as today.
    ineligible = [check for check in failing
                  if _flight_key(check.get('run_id'), check.get('job'))
                  not in flights]
    in_flight_only = len(ineligible) == 0
    if in_flight_only:
        outcome['action'] = 'suppressed'
        return outcome
    if not mutate:
        outcome['action'] = 'woke'
        return outcome
    comment_id = _post(api, owner, wake_text)
    if comment_id is None:
        outcome['action'] = 'post_failed'
        return outcome
    handled.append(key)
    outcome['action'] = 'woke'
    outcome['comment_id'] = comment_id
    return outcome
