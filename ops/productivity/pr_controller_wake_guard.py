"""Suppress false author-action wakes for shared-base/empty-list CI red.

Notification-only gate for the host PR controller's wake-emission sites (the
initial wake and the stale reminder) and for the host PR pump, which
re-renders a stored ``ci_red`` state through ``wake_text`` with an explicitly
empty red list and no fresh checks query.

The defect: ``classify()`` appends every completed red check and returns
``ci_red``, so an unchanged head whose required gates are green but whose
shared-base ``readiness-sweep`` is red still pages the PR author to "fix and
push" -- work a branch cannot do. The pump can even render that instruction
with an empty failing-check list. This module decides *whether to wake*;
verdicts, merge/review eligibility, check outputs, required checks and gates
are unchanged. A suppressed wake still leaves CI red, and the review gate
still owns re-requests.

Contract (frozen by the acceptance suite):

- ``KNOWN_BASE_EXECUTED``: checks known to execute against the shared base
  tree rather than the PR head. A red member is not author-actionable.
- ``actionable_red(red, required, known_base_executed=())``: ordered
  ``[(name, url)]`` author-actionable subset of the ``classify()`` red list.
- ``author_wake(state, detail, required, known_base_executed=())``:
  ``('wake'|'suppress', reason)``. ``suppress`` fires only when
  ``state == 'ci_red'`` and the actionable list is empty.

Reconciliations:

- R1. An empty red list suppresses under ANY required value, including falsy
  ``set()``/``None``: it names no check to fix, so no failure can be hidden.
- R2. Required membership wins over known-base-executed: a failing required
  check merge-blocks the PR, so hiding it would stall the author silently.
- R3. Unknown checks (in neither set) are actionable: fail closed.
- R4. Only the verdict is contractual; the reason is a non-empty string.
- R5. Off the ``ci_red`` path the guard always returns ``wake`` ("not
  suppressed by this guard"); the controller's own wake-state gate still owns
  which states wake.
- R6. ``actionable_red`` with falsy ``required`` (rules fetch failed) returns
  the full red list unchanged, so no future caller mistakes an unknown policy
  for an empty one.

Pure stdlib, deterministic, no I/O. Inputs are never mutated and every call
returns a fresh list.
"""

# Checks known to execute against the shared base tree: red here cannot be
# repaired by a PR branch push, so red is not author-actionable. Stays a
# single member until a follow-up host excerpt refines it.
KNOWN_BASE_EXECUTED = ('readiness-sweep',)


def actionable_red(red, required, known_base_executed=()):
    """Return the ordered author-actionable subset of a ``classify()`` red list.

    Each entry is a ``(name, url)`` pair. A failing check is actionable
    unless it is a known shared-base check outside the required set:
    required membership wins (R2) and unknown checks stay actionable (R3).
    Falsy ``required`` means the policy is unknown, so the full list is
    returned unchanged (R6). Order is preserved; inputs are not mutated.
    """
    items = list(red)
    if not required:
        return items
    known = set(known_base_executed or ())
    wanted = set(required)
    return [entry for entry in items
            if entry[0] in wanted or entry[0] not in known]


def author_wake(state, detail, required, known_base_executed=()):
    """Decide whether a controller state warrants an author-action wake.

    Returns ``('wake'|'suppress', reason)`` with a non-empty reason string.
    Only ``ci_red`` with an empty actionable list suppresses: off-path
    states always wake (R5), an empty red list suppresses under any required
    value (R1), and a non-empty red list with falsy ``required`` wakes
    fail-closed. ``detail`` and ``required`` are not mutated.
    """
    if state != 'ci_red':
        return 'wake', f'state {state!r} is not ci_red; this guard only gates ci_red wakes'
    red = detail.get('red') or []
    if not red:
        return 'suppress', 'empty actionable list names no check to fix or push for'
    if not required:
        return 'wake', 'required set unknown; fail closed to the unguarded wake'
    actionable = actionable_red(red, required, known_base_executed=known_base_executed)
    if not actionable:
        return 'suppress', 'only known shared-base checks are red; a branch push cannot repair them'
    return 'wake', 'author-actionable failure present'
