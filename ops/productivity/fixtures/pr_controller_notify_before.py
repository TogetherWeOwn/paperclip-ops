def latest_check_contexts(contexts):
    """Collapse proven Actions attempts only; other workflows and non-Actions contexts remain gates."""
    latest, unknown = {}, []
    for c in contexts:
        suite = c.get('checkSuite') or {}
        run = suite.get('workflowRun') or {}
        workflow = (run.get('workflow') or {}).get('databaseId')
        try:
            order = (int(run['databaseId']), int(c['databaseId']))
        except (KeyError, TypeError, ValueError):
            unknown.append(c)
            continue
        if not workflow or not c.get('name'):
            unknown.append(c)
            continue
        key = (workflow, c['name'])
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, c)
    return unknown + [c for _, c in latest.values()]


def classify(pr, required, reviews, full):
    """-> (state, detail). detail carries failing checks or the review link."""
    if pr['isDraft']:
        idle_h = (NOW - parse(pr['_committed'])) / 3600 if pr.get('_committed') else 0
        return ('draft_idle' if idle_h >= DRAFT_IDLE_H else 'draft'), {}
    if pr['mergeable'] == 'CONFLICTING' or pr['mergeStateStatus'] == 'DIRTY':
        return 'conflict', {}
    pending, red, results = False, [], {}
    for c in latest_check_contexts(pr['_contexts']):
        name = c.get('name') or c.get('context') or ''
        if name == 'Paperclip Review':
            continue
        if 'state' in c and 'status' not in c:
            done, concl, url = c['state'] not in ('PENDING', 'EXPECTED'), c['state'], c.get('targetUrl') or ''
        else:
            done, concl, url = c.get('status') == 'COMPLETED', c.get('conclusion') or '', c.get('detailsUrl') or ''
        if concl in ('CANCELLED', 'STALE'):
            continue  # superseded duplicate run (e.g. a re-triggered pr-lint); a required check left with only this run stays unmet
        if not done:
            # A long non-required job (e.g. nightly suites) must not hold review once every required check passed.
            if not required or name in required:
                pending = True
        elif concl in RED:
            red.append((name, url))
        results[name] = results.get(name, True) and done and concl in ('SUCCESS', 'NEUTRAL', 'SKIPPED')
    if red:
        return 'ci_red', {'red': red, 'pending': pending}
    if pending or (required and not all(results.get(r) for r in required)):
        return 'ci_running', {}
    rv = reviews.get((full.lower(), pr['number'], pr['headRefOid']))
    if not rv:
        return 'green', {}
    state, score, url = rv
    if state in ('queued', 'running', 'received'):
        return 'reviewing', {}
    if state in ('superseded', 'cancelled'):
        return 'green', {}  # no live review on this head; the review gate re-requests it
    if state == 'completed':
        if score == '5':
            return 'passed', {}
        return 'findings', {'score': score, 'url': url}
    return 'review_error', {'review_state': state}


def wake_text(full, pr, state, detail):
    url, n = pr['url'], pr['number']
    head = pr['headRefOid'][:8]
    if state == 'ci_red':
        lines = '\n'.join(f'- `{name}`: {u}' for name, u in detail['red'][:6])
        more = ' Some checks are still running; fix these first.' if detail.get('pending') else ''
        quality = sorted({m.group(1) for name, u in detail['red'] if re.search(r'lighthouse|quality', name, re.I)
                          for m in [re.match(r'(https://github\.com/[^/]+/[^/]+/actions/runs/\d+)', u or '')] if m})
        hint = (f"\n\nWeb quality gate: the run summary lists every failing audit, score vs ci/quality-budget.json and the "
                f"offending resources: {', '.join(quality)} . Fix the cause; never lower the budget to go green."
                if quality else '')
        what = (f"**CI is red** on head `{head}`. Failing checks:\n{lines}{hint}\n\nOpen the logs, fix the cause on this PR "
                f"branch and push once.{more} Don't merge or rebase main into it unless the same check is also red on "
                "main (main-red); a no-conflict merge voids the review.")
    elif state == 'findings':
        what = (f"**Paperclip Review scored {detail.get('score') or '<5'}/5** on head `{head}`. Review: "
                f"{detail.get('url') or url}. Address every finding in ONE push. Don't push again while the re-review "
                "runs; the review gate re-requests it automatically once CI is green. Don't merge or rebase main into "
                "it (no conflict): that voids the review and costs a review run.")
    elif state == 'conflict':
        what = (f"it has **merge conflicts** with its base on head `{head}`. Update it from the base branch, resolve, "
                "run the tests, and push once.")
    elif state == 'queue_ejected':
        what = (f"the **merge queue ejected head `{head}` for failed checks**: it is green alone but red combined with the "
                f"current base. The failing merge_group run is under "
                f"https://github.com/{full}/actions?query=event%3Amerge_group . Merge the base branch into this PR "
                "branch, reproduce the failure locally, fix it and push once. The new head gets CI and a review, then "
                "auto-merge re-queues it. This head is never re-queued as is.")
    else:
        what = (f"it has been a **draft with no commits for {DRAFT_IDLE_H}h+**. Finish it and run `gh pr ready`, or close "
                "it with a one-line reason if it is superseded.")
    return (f"**{full}#{n}** ({url}): {what}\n\n(host PR controller: you are woken only when this PR needs you: red CI, "
            "findings, conflicts. Don't set monitors on this card; reviews are requested and 5/5 heads auto-merge "
            "without you.)")
