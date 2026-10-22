#!/usr/bin/env python3
"""Host glue: bridge the deployed pr_controller ci_red path to pr_controller_ci.

The patch packet (patches/pr-controller-ci.patch) calls
:func:`handle_ci_red` from the controller's wake path when ``cur == 'ci_red'``.
This module translates the classifier's ``detail`` payload into the helper's
check rows, injects the host's ``gh``/API/ledger clients, logs every rerun and
cap comment with an UNDO line, and maps the helper outcome back to a
'handled' (skip the normal wake) / 'proceed' (wake as today) verdict.

Fail-safe direction: any unrecognized ``detail`` shape, any ``gh``/API
failure, or any exception in this module returns 'proceed' (or 'woke' in
shadow terms) so the controller behaves exactly as before the packet. A
helper ``'post_failed'`` outcome also returns 'proceed': nothing was posted,
so the normal wake path retries with its own retry semantics. The new host
state is ``rec['ci_policy']`` (per-PR, owned by the helper) plus
``state['ci_policy_shared']`` (host-level flake window shared across PRs).

After every live tick the fresh comment id (or None when this tick posted
nothing) is stored as ``rec['ci_policy']['tick_comment_id']`` so the
call-site patch correlates exactly the comment posted on this tick -- never
a stale id from an earlier wake. Ticks that post nothing (reran,
suppressed, green) set neither ``pending_wake`` nor ``woke``; like
:func:`pr_controller_wakes.post_wake`, ``woke`` is only ever set by
reconciliation on a claimed wake, never optimistically.

The companion entry :func:`handle_ci_reset` runs when the wake-site patch
sees ``cur != 'ci_red'`` on a record that still holds policy state: an
all-green result clears rounds/cap and records pass-flakes, anything else
is a no-op, and the host wake always proceeds. Without it the helper would
never see the green transition and a cap would mute the PR forever.

Shadow mode (``shadow=True``): the helper runs against null sinks under a
separate ``rec['ci_policy_shadow']`` key (and a ``'shadow'`` sub-key of the
shared store, so shadow counts never leak into live flake windows), the
would-be decision is logged, and this function always returns 'proceed' so
the live wake path is unchanged. The 24h shadow counts come from these lines.

Operator log lines (appended via the injected ``log_action`` callable, which
the host wires to ops/worker-vps/operator-actions.log)::

    ci-policy reran owner/repo#7 run 101 attempt 1 (check.yml/rust tests)
      UNDO: gh run cancel 101  # only before the retry concludes; else none needed
    ci-policy capped owner/repo#7 comment <id> rounds=3
      UNDO: delete the cap comment; clear rec['ci_policy'] to re-arm wakes
"""

import functools
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import pr_controller_ci as ci
from pr_controller_ci import kill_switch_active

try:
    from flake_ledger import LedgerClient, quarantine_card
except Exception:  # pragma: no cover - host stages flake_ledger.py beside us
    LedgerClient = None
    quarantine_card = None

CI_STATE_KEY = 'ci_policy'
GH_TIMEOUT_S = 120


def shadow_active():
    """True when CI_POLICY_SHADOW=1 asks for log-only shadow mode."""
    return os.environ.get('CI_POLICY_SHADOW', '').strip() == '1'

_SKIP_CONCLUSIONS = frozenset({
    '', 'pending', 'queued', 'waiting', 'requested', 'in_progress',
    'stale', 'cancelled', 'skipped', 'neutral',
})


def _first(item, *keys):
    for key in keys:
        value = item.get(key)
        if value is not None and value != '':
            return value
    return None


def _normalize_check(item):
    """Translate one classifier check row, or None when unusable/unconcluded."""
    if not isinstance(item, dict):
        return None
    conclusion = str(_first(item, 'conclusion', 'state', 'status') or '')
    if conclusion.lower() in _SKIP_CONCLUSIONS:
        return None
    if conclusion.lower() in ('success', 'succeeded'):
        conclusion = 'success'
    else:
        conclusion = 'failure'
    run_id = _first(item, 'run_id', 'runId', 'run-id', 'id')
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        return None
    attempt = _first(item, 'attempt', 'run_attempt', 'runAttempt')
    if attempt is not None:
        try:
            attempt = int(attempt)
        except (TypeError, ValueError):
            attempt = None
    return {
        'workflow': _first(item, 'workflow', 'workflow_file', 'path', 'file'),
        'job': _first(item, 'job', 'name', 'check_name'),
        'test': _first(item, 'test', 'test_name'),
        'run_id': run_id,
        'attempt': attempt,
        'conclusion': conclusion,
        'private_repo': bool(item.get('private_repo', False)),
        'paid_runner': bool(item.get('paid_runner', False)),
    }


def checks_from_detail(detail):
    """Best-effort check rows from a ci_red classifier payload.

    Returns (checks, reason): ``checks`` is the helper-shaped list (possibly
    empty) and ``reason`` is a short string for shadow logging. Empty with a
    reason always degrades to today's wake.
    """
    if not isinstance(detail, dict):
        return [], 'detail is not an object'
    raw = detail.get('checks')
    if raw is None:
        raw = detail.get('failing_checks')
    if raw is None:
        raw = detail.get('failing')
    if raw is None:
        return [], 'detail carries no checks/failing_checks/failing'
    if not isinstance(raw, list):
        return [], 'checks payload is not a list'
    checks = []
    for item in raw:
        row = _normalize_check(item)
        if row is not None:
            checks.append(row)
    if not checks:
        return [], 'no concluded check rows with a run id'
    return checks, 'ok'


def _gh_json(gh, *argv):
    code, text = gh(['gh', *argv])
    if code != 0:
        raise OSError(f'gh {" ".join(argv)} exited {code}')
    return json.loads(text)


def resolve_repo_private(gh, full):
    """True when the repo is private; fail-safe True when unknowable."""
    try:
        return bool(_gh_json(gh, 'api', f'repos/{full}', '--jq', '.private'))
    except Exception:
        return True


def resolve_run_self_hosted(gh, full, run_id):
    """True when every job in the run used a self-hosted runner.

    Fail-safe False (assume paid GitHub-hosted) when unknowable.
    """
    try:
        payload = _gh_json(
            gh, 'api', f'repos/{full}/actions/runs/{run_id}/jobs',
            '--paginate', '--jq', '[.jobs[] | .labels]')
    except Exception:
        return False
    if not isinstance(payload, list) or not payload:
        return False
    for labels in payload:
        names = {str(label).lower() for label in (labels or [])
                 if isinstance(label, str)}
        if 'self-hosted' not in names:
            return False
    return True


def annotate_cost(checks, *, gh, full, log):
    """Fill private_repo/paid_runner per check via the GitHub API.

    A failed lookup degrades that check to paid/private (wake, no rerun).
    """
    try:
        private = resolve_repo_private(gh, full)
    except Exception as exc:  # pragma: no cover - resolve never raises
        log(f'ci-policy cost lookup failed for {full}: {exc}')
        private = True
    hosted = {}
    for check in checks:
        check['private_repo'] = private
        if private:
            run_id = check['run_id']
            if run_id not in hosted:
                try:
                    hosted[run_id] = resolve_run_self_hosted(
                        gh, full, run_id)
                except Exception as exc:
                    log(f"ci-policy runner lookup failed run {run_id}: {exc}")
                    hosted[run_id] = False
            check['paid_runner'] = not hosted[run_id]
        else:
            check['paid_runner'] = False
    return checks


class _NullSinks:
    """Shadow-mode sinks: record would-be side effects, perform none."""

    def __init__(self):
        self.gh_calls = []
        self.api = self._Api()
        self.cards = self._Cards()
        self.recorded = []
        ledger = self._Ledger(self.recorded)
        self.ledger = ledger

    def gh(self, argv):
        self.gh_calls.append(list(argv))
        return (0, 'shadow: no rerun requested')

    def quarantine_card(self, payload):
        return self.cards(payload)

    class _Api:
        def __init__(self):
            self.calls = []

        def __call__(self, method, path, payload):
            self.calls.append((method, path, dict(payload)))
            return ('201', json.dumps({'id': 'shadow-comment'}))

    class _Cards:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(dict(payload))
            return 'shadow-card'

    class _Ledger:
        def __init__(self, recorded):
            self.recorded = recorded

        def verdict(self, repo, key):
            return 'no'

        def record_flake(self, entry):
            self.recorded.append(dict(entry))


def default_gh(argv):
    """Run a gh argv list with a timeout; return (exit_code, text)."""
    argv = list(argv or [])
    if not argv or argv[0] != 'gh':
        raise ValueError('refusing to run non-gh command')
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
            check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f'gh call failed: {exc}') from exc
    return completed.returncode, (completed.stdout or '') + (
        completed.stderr or '')


def operator_log_path():
    """operator-actions.log anchored to this module, overridable by env."""
    override = os.environ.get('OPERATOR_ACTIONS_LOG', '').strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    return here.parent.parent / 'worker-vps' / 'operator-actions.log'


def default_log_action(line):
    """Append one timestamped line to operator-actions.log (best effort)."""
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    try:
        path = operator_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(f'{stamp} {line}\n')
    except OSError:
        pass


def default_find_card(api, company_id):
    """Find the existing [flake] card for a quarantine payload, or None.

    Any lookup failure returns None (fail-open to create); the helper's
    flake_cards state still bounds creation to one card per key.
    """
    def find(payload):
        if not company_id:
            return None
        try:
            query = quote(str(payload.get('title') or ''), safe='')
            code, body = api('GET',
                             f'companies/{company_id}/issues?q={query}',
                             {})
        except Exception:
            return None
        if not str(code).startswith('2'):
            return None
        try:
            document = json.loads(body)
        except (TypeError, ValueError):
            return None
        items = document.get('issues', document) if isinstance(
            document, dict) else document
        if not isinstance(items, list):
            return None
        want = str(payload.get('title') or '')
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get('title') or '')
            if title == want and '[flake]' in title:
                return item.get('id')
        return None
    return find


def default_quarantine_card(api, company_id=None):
    """quarantine_card callable wired to the host API client."""
    if quarantine_card is None:
        raise OSError('flake_ledger.quarantine_card is not staged')
    company = company_id or os.environ.get('PAPERCLIP_COMPANY_ID', '')
    return functools.partial(
        quarantine_card, find_card=default_find_card(api, company),
        post=api, company_id=company)


def default_ledger():
    if LedgerClient is None:
        raise OSError('flake_ledger.LedgerClient is not staged')
    return LedgerClient()


def default_deps(api, log):
    """Dependency dict for handle_ci_red from host conventions + env.

    The call-site patch passes ``**default_deps(api=api, log=log)`` so the
    patch stays small; every default is overridable by passing explicit
    kwargs instead. Shadow mode reads CI_POLICY_SHADOW=1.
    """
    return {
        'api': api,
        'gh': default_gh,
        'ledger': default_ledger(),
        'quarantine_card': default_quarantine_card(api),
        'log': log,
        'log_action': default_log_action,
        'shadow': shadow_active(),
    }


def handle_ci_red(*, full, pr, detail, rec, owner_card_id, wake_text,
                  api, gh, ledger, quarantine_card, log, log_action,
                  shadow=False, now=None, shared=None):
    """Run the CI-rerun policy for one ci_red wake decision.

    Returns 'handled' when the normal wake must be skipped (the helper
    reran, already handled the tick, or posted the wake/cap itself) and
    'proceed' when the controller must wake as today. Never raises: any
    unexpected failure returns 'proceed'. ``shared`` is the host-level
    flake store (``state['ci_policy_shared']``); omit it to keep the flake
    window in the per-PR record.
    """
    ident = f"{full}#{pr.get('number')}"
    if now is None:
        now = time.time()
    try:
        return _handle_ci_red(
            full=full, pr=pr, detail=detail, rec=rec,
            owner_card_id=owner_card_id, wake_text=wake_text, api=api, gh=gh,
            ledger=ledger, quarantine_card=quarantine_card, log=log,
            log_action=log_action, shadow=shadow, now=now, ident=ident,
            shared=shared)
    except Exception as exc:
        log(f'ci-policy {ident} host glue failed ({exc}); waking as today')
        return 'proceed'


def _handle_ci_red(*, full, pr, detail, rec, owner_card_id, wake_text,
                   api, gh, ledger, quarantine_card, log, log_action,
                   shadow, now, ident, shared):
    checks, reason = checks_from_detail(detail)
    if reason != 'ok':
        log(f'ci-policy {ident} no check rows ({reason}); waking as today')
        if shadow:
            log_action(f'ci-policy shadow {ident} would-wake '
                       f'reason=no-check-rows')
        return 'proceed'
    annotate_cost(checks, gh=gh, full=full, log=log)
    helper_pr = {'repo': str(full).split('/')[-1],
                 'number': pr.get('number'),
                 'head_sha': pr.get('headRefOid') or pr.get('head_sha'),
                 'owner_card_id': owner_card_id}
    if shadow:
        # Shadow state persists across passes (so would-cap needs 3 rounds)
        # but under a separate key that live mode never reads. Null sinks
        # record the would-be side effects without performing any.
        state = rec.setdefault('ci_policy_shadow', {})
        shadow_shared = None
        if isinstance(shared, dict):
            shadow_shared = shared.setdefault('shadow', {})
            if not isinstance(shadow_shared, dict):
                shadow_shared = shared['shadow'] = {}
        null = _NullSinks()
        outcome = ci.handle_ci_tick(
            pr=helper_pr, checks=checks, state=state, gh=null.gh, api=null.api,
            ledger=null.ledger, quarantine_card=null.quarantine_card,
            wake_text=wake_text, now=now, shared=shadow_shared)
        action = outcome.get('action')
        log_action(f'ci-policy shadow {ident} would-{action} '
                   f'runs={",".join(str(run_id) for run_id in outcome.get("rerun_run_ids") or []) or "-"} '
                   f'wakes={len(null.api.calls)} '
                   f'cards={len(null.cards.calls)} '
                   f'head={str(helper_pr["head_sha"])[:8]}')
        return 'proceed'
    state = rec.setdefault(CI_STATE_KEY, {})
    outcome = ci.handle_ci_tick(
        pr=helper_pr, checks=checks, state=state, gh=gh, api=api,
        ledger=ledger, quarantine_card=quarantine_card,
        wake_text=wake_text, now=now,
        shared=shared if isinstance(shared, dict) else None)
    # Fresh every tick (None when this tick posted nothing) so the call
    # site never correlates a stale comment id from an earlier wake.
    state['tick_comment_id'] = outcome.get('comment_id')
    action = outcome.get('action')
    if action == 'post_failed':
        log(f'ci-policy {ident} post failed; waking as today')
        return 'proceed'
    if action == 'reran':
        for run_id in outcome.get('rerun_run_ids') or []:
            row = next((check for check in checks
                        if check['run_id'] == run_id), {})
            log_action(
                f'ci-policy reran {ident} run {run_id} '
                f"attempt {row.get('attempt')} "
                f"({row.get('workflow')}/{row.get('job')}) "
                f"head={str(helper_pr['head_sha'])[:8]} "
                f'UNDO: gh run cancel {run_id} before the retry concludes; '
                'otherwise no undo needed (read-only retry)')
        return 'handled'
    if action in ('suppressed', 'green'):
        return 'handled'
    if action == 'woke':
        log(f'ci-policy {ident} woke after failed rerun '
            f'comment={outcome.get("comment_id")} rounds={outcome.get("rounds")}')
        return 'handled'
    if action == 'capped':
        log_action(
            f"ci-policy capped {ident} comment={outcome.get('comment_id')} "
            f"rounds={outcome.get('rounds')} "
            f"UNDO: delete the cap comment and clear rec['{CI_STATE_KEY}'] "
            'to re-arm author wakes')
        return 'handled'
    log(f'ci-policy {ident} unknown action {action!r}; waking as today')
    return 'proceed'


class _ResetGuards:
    """gh/api stand-ins proving the reset pass performs no side effects.

    The helper's all-green path requests no reruns and posts nothing, so
    any call here raises (which the helper absorbs fail-safe) and is
    logged by the caller as a leak.
    """

    def __init__(self):
        self.calls = []

    def gh(self, argv):
        self.calls.append(('gh', list(argv)))
        raise OSError('reset pass must not rerun')

    def api(self, method, path, payload):
        self.calls.append(('api', method, path))
        raise OSError('reset pass must not post')


def handle_ci_reset(*, full, pr, detail, rec, ledger, quarantine_card, log,
                    shadow=False, now=None, shared=None, **_ignored):
    """Reset-only transition pass for heads that left ci_red.

    The wake-site patch calls this when ``cur != 'ci_red'`` but the record
    still holds policy state. An all-green result runs the helper's green
    branch (pass-flakes recorded, rounds/cap cleared); anything else leaves
    state untouched. Never posts, never reruns, and always returns
    'proceed' so the host wake is unaffected. ``**_ignored`` swallows the
    shared ``default_deps`` (api/gh/log_action). Never raises.
    """
    ident = f"{full}#{pr.get('number')}"
    if now is None:
        now = time.time()
    try:
        return _handle_ci_reset(
            full=full, pr=pr, detail=detail, rec=rec,
            ledger=ledger, quarantine_card=quarantine_card, log=log,
            shadow=shadow, now=now, ident=ident, shared=shared)
    except Exception as exc:
        log(f'ci-policy {ident} reset pass failed ({exc}); waking as today')
        return 'proceed'


def _handle_ci_reset(*, full, pr, detail, rec, ledger, quarantine_card, log,
                     shadow, now, ident, shared):
    checks, reason = checks_from_detail(detail)
    if reason != 'ok':
        return 'proceed'  # nothing green to reset against; wake as today
    if any((check.get('conclusion') or 'failure') != 'success'
           for check in checks):
        # Still red: the ci_red path owns this head; leave rounds/cap alone.
        return 'proceed'
    if kill_switch_active():
        # The switch freezes policy state entirely; ignoring (not clearing)
        # the cap is what restores wake-on-first-red while it is set.
        log(f'ci-policy {ident} reset pass skipped: kill switch holds state')
        return 'proceed'
    helper_pr = {'repo': str(full).split('/')[-1],
                 'number': pr.get('number'),
                 'head_sha': pr.get('headRefOid') or pr.get('head_sha'),
                 'owner_card_id': None}
    guards = _ResetGuards()
    if shadow:
        state = rec.setdefault('ci_policy_shadow', {})
        shadow_shared = None
        if isinstance(shared, dict):
            shadow_shared = shared.setdefault('shadow', {})
            if not isinstance(shadow_shared, dict):
                shadow_shared = shared['shadow'] = {}
        null = _NullSinks()
        outcome = ci.handle_ci_tick(
            pr=helper_pr, checks=checks, state=state, gh=guards.gh,
            api=guards.api, ledger=null.ledger,
            quarantine_card=null.quarantine_card, wake_text='',
            now=now, shared=shadow_shared)
        log(f'ci-policy {ident} shadow reset pass: '
            f'would-{outcome.get("action")} '
            f'head={str(helper_pr["head_sha"])[:8]}')
        return 'proceed'
    state = rec.setdefault(CI_STATE_KEY, {})
    outcome = ci.handle_ci_tick(
        pr=helper_pr, checks=checks, state=state, gh=guards.gh,
        api=guards.api, ledger=ledger, quarantine_card=quarantine_card,
        wake_text='', now=now,
        shared=shared if isinstance(shared, dict) else None)
    if guards.calls:
        log(f'ci-policy {ident} reset pass leaked calls: {guards.calls!r}')
    elif outcome.get('action') == 'green':
        log(f'ci-policy {ident} reset rounds/cap on green '
            f'head={str(helper_pr["head_sha"])[:8]}')
    return 'proceed'
