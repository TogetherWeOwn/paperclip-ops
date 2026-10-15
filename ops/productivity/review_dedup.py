#!/usr/bin/env python3
"""Same-head review idempotency guard.

Measured over one week of review rows: 633 same-head groups, 970 extra
rows, all with DISTINCT delivery_ids (separately triggered, not fan-out).
Second rows by triggering event: ready_for_review 501, mention 365,
synchronize 136, opened 92, reopened 20, comment 7.

What is actually enforced in this repo, and by which function:

- CI trigger step (.github/scripts/review_trigger_step.py) calls
  check_run_suppresses(): never open a second request while a
  non-completed Paperclip Review run covers the head, or a completed
  success/failure verdict does. Completed error outcomes stay retryable.
- Error reconciler (ops/productivity/review_error_rerequest.py) calls
  covering_review_exists(): its wake is a card comment, not a review
  request, so only a completed verdict or a live attempt suppresses it.
  Repeated errors must not suppress it, or outage-stranded heads would
  never be woken. One wake per head comes from the reconciler's state
  key, not from an error count.
- head_decision() plus idempotency_key() reserve the stricter
  new-review-request rule (an errored head gets at most one retry;
  unrecognized row states fail closed toward suppress). No production
  path in this repo calls them today: the reconciler deliberately does
  not, because it must wake on repeated errors and on unknown states
  rather than strand the head. Adopters that open real review requests
  (e.g. a review gate) should call head_decision(); the TwoDeliveriesOneRow
  tests pin its contract (two deliveries, same head -> one row).

Server row vocabulary (chat_github_reviews.state) follows the review-gate
taxonomy: completed = published verdict; queued/running = live;
error/incomplete = failed attempt; manual_required = needs a human mention;
superseded = abandoned when the head moved (the green gate marks queued or
running rows superseded when a newer assessment exists). A superseded row
carries no verdict and no live attempt for this head, so it is ignored: it
neither suppresses nor consumes the error budget. A state this file does not
recognize fails closed toward suppress: piling a second request onto a head
we cannot prove is bare is the harm being removed, and a wrongly suppressed
request surfaces as a visible skip notice (the reconciler and manual-mention
paths still exist) while a duplicate review burns reviewer budget silently.
"""

REVIEW_CHECK_NAME = 'Paperclip Review'

# GitHub check-run side. Any non-completed Paperclip Review run means a review
# is already covering the head. Completed runs suppress only on a real verdict
# (success/failure); error outcomes (action_required, cancelled, timed_out,
# stale) stay retryable so gateway-outage recovery keeps its re-request path.
LIVE_CHECK_STATUSES = frozenset({'queued', 'in_progress', 'waiting', 'requested', 'pending'})
TERMINAL_CHECK_CONCLUSIONS = frozenset({'success', 'failure'})

# Server row side (chat_github_reviews.state).
LIVE_ROW_STATES = frozenset({'queued', 'running'})
PUBLISHED_ROW_STATES = frozenset({'completed'})
ERROR_ROW_STATES = frozenset({'error', 'incomplete'})
MANUAL_ROW_STATES = frozenset({'manual_required'})
# Abandoned when the head moved; ignored by every decision below (neither a
# cover nor a spent attempt). Kept separate so a head that moved away and back
# (rows [superseded, error]) still gets its first retry.
SUPERSEDED_ROW_STATES = frozenset({'superseded'})
MAX_ERROR_RETRIES = 1


def idempotency_key(repo, number, head_sha):
    """Canonical key for one (repo, PR, head): ``owner/name#n@sha``.

    The repo is lowercased because both ``togetherweown/`` and
    ``TogetherWeOwn/`` occur in stored rows. Matches the ``R#P@H`` state keys
    already used by review_error_rerequest.py and the review gate.
    """
    return f'{(repo or "").lower()}#{number}@{head_sha or ""}'


def check_run_suppresses(name, status, conclusion):
    """True when a GitHub check-run means "do not request another review".

    Only Paperclip Review runs suppress; every other check is ignored. A
    non-completed review run (queued, in progress, waiting) suppresses, as
    does a completed success/failure verdict. Completed error outcomes stay
    retryable. An unknown status fails closed toward suppress.
    """
    if name != REVIEW_CHECK_NAME:
        return False
    if (status or '') != 'completed':
        return True
    return (conclusion or '') in TERMINAL_CHECK_CONCLUSIONS


def covering_review_exists(row_states):
    """True when a completed verdict or a live attempt already covers the head.

    The error reconciler uses this narrower check instead of head_decision:
    its wake is a card comment, not a review request, and its state key
    already limits it to one wake per head — so repeated errors must not
    suppress it, or outage-stranded heads would never be woken.
    """
    states = [(s or '').strip().lower() for s in row_states]
    return any(s in PUBLISHED_ROW_STATES or s in LIVE_ROW_STATES for s in states)


def head_decision(row_states):
    """Return (action, reason) for one head given its server row states.

    For paths that open real review requests (not the error reconciler,
    which uses covering_review_exists so repeated errors still wake).
    Action is 'request' or 'suppress'. Precedence: published verdict ->
    in-flight attempt -> exhausted error budget -> unrecognized state ->
    request. A bare head, a first error, a manual_required head, or a head
    whose only non-error rows are superseded requests; anything else
    suppresses.
    """
    states = [(s or '').strip().lower() for s in row_states]
    if any(s in PUBLISHED_ROW_STATES for s in states):
        return 'suppress', 'head already has a completed review'
    if any(s in LIVE_ROW_STATES for s in states):
        return 'suppress', 'a review is already in flight for this head'
    errors = sum(1 for s in states if s in ERROR_ROW_STATES)
    if errors > MAX_ERROR_RETRIES:
        return 'suppress', 'error retry budget exhausted (one retry max)'
    known = (LIVE_ROW_STATES | PUBLISHED_ROW_STATES | ERROR_ROW_STATES
             | MANUAL_ROW_STATES | SUPERSEDED_ROW_STATES)
    unknown = sorted({s for s in states if s and s not in known})
    if unknown:
        return 'suppress', 'unrecognized row state(s) %s; refusing to pile on' % ', '.join(unknown)
    if errors:
        return 'request', 'first retry after an errored attempt'
    return 'request', 'no live, completed, or repeated-error row for this head'
