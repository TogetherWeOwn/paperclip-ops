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


# Live pr_controller.py lines 335-732 appended verbatim below (exact pre-fix
# evidence for the notify-guard wiring hunks; see patches/README-pr-controller-liveness.md).
# The seam above (latest_check_contexts/classify/wake_text) is byte-identical to the
# original fixture; only this marker and the appended span were added.


def soft_hold_ok(iid):
    """True when the card's only hold is its own `blocked` status or a descriptor its assignee owns."""
    rows = rr.psql(f"""select i.unblock_descriptor->'owner'->>'agentId', i.assignee_agent_id,
        (select count(*) from issue_relations r join issues b on b.id=r.issue_id
            where r.related_issue_id=i.id and r.type='blocks' and b.status not in ('done','cancelled')),
        (select count(*) from issue_recovery_actions ra where ra.source_issue_id=i.id
            and (ra.status in ('active','escalated') or ra.evidence->'automaticRecovery'->>'replay'='blocked'))
        from issues i where i.id='{iid}'""")
    if not rows:
        return False
    owner, assignee, blockers, recovery = rows[0]
    return int(blockers) == 0 and int(recovery) == 0 and owner in ('', assignee)


def ensure_backstop(iid, ident, why, url):
    """PR cards keep ONE long backstop monitor instead of polling ones. The server's in_review validator accepts a
    scheduled monitor as the review path (routes/issues.ts: missing review_path -> 422), so a monitor must exist on an
    in_review card; it just must not poll. Short monitors are stretched to BACKSTOP_H; an in_review card without one
    gets one. The controller does the real waking on transitions."""
    rows = rr.psql(f"""select execution_policy::text, status, coalesce(extract(epoch from monitor_next_check_at)::bigint, 0)
        from issues where id='{iid}'""")
    if not rows:
        return
    pol_text, status, next_at = rows[0]
    next_at = int(next_at)
    if next_at >= NOW + BACKSTOP_MIN_H * 3600:
        return
    if not next_at and status != 'in_review':
        return
    if running(iid):
        return
    pol = json.loads(pol_text or '{}') or {'mode': 'normal', 'stages': [], 'commentRequired': True}
    mon = dict(pol.get('monitor') or {})
    target = NOW + BACKSTOP_H * 3600
    mon['nextCheckAt'] = ts(target)
    if mon.get('timeoutAt') and parse(mon['timeoutAt'][:19] + 'Z') < target + 3600:
        mon['timeoutAt'] = None
    if mon.get('externalRef') == '[redacted]':
        mon['externalRef'] = url
    mon.setdefault('kind', 'external_service')
    mon['serviceName'] = mon.get('serviceName') or 'github-pr-controller'
    mon['notes'] = ('PR controller backstop: the host PR controller wakes this card on red CI, review findings, '
                    'conflicts or merge; this monitor is only the in_review review path.')
    if 'scheduledBy' not in mon:
        mon['scheduledBy'] = 'board'
    pol['monitor'] = mon
    if SHADOW:
        log(f'would set {BACKSTOP_H}h backstop monitor on {ident} ({why})')
        return
    code, body = api('PATCH', f'issues/{iid}', {'executionPolicy': pol})
    log(f"backstop monitor {BACKSTOP_H}h on {ident} ({why}{', was ' + ts(next_at) if next_at else ', was none'}) HTTP {code}")
    if not code.startswith('2'):
        print(body[:300])


def running(iid):
    """A run is executing on the card. Queued runs of a merged/closed PR's review card are dead work (never cancelled
    here; closing the card supersedes them), so only a running run defers the closeout."""
    rows = rr.psql(f"""select exists(select 1 from heartbeat_runs h where coalesce(h.context_snapshot->>'issueId',
        h.context_snapshot->>'taskId')='{iid}' and h.status='running')""")
    return not rows or rows[0][0] != 'f'


def closeout(full, n, merged, cards, env, rec):
    """PR merged/closed: close Reviewer cards, finish 'Finish' cards, one closeout note to other owner cards.
    Returns the number of cards acted on."""
    acted = 0
    repo = full.lower()
    rev = rr.psql(f"""select distinct i.id, i.identifier from issues i join chat_conversations c on c.issue_id=i.id
        where i.company_id='{CID}' and i.assignee_agent_id='{REVIEWER}' and i.status not in ('done','cancelled')
        and lower(c.external_thread_id) like 'github:{repo}:{n}:%'""")
    status = 'done' if merged else 'cancelled'
    verb = 'merged' if merged else 'closed without merge'
    for iid, ident in rev:
        if running(iid):
            continue
        acted += 1
        if SHADOW:
            log(f'would set review card {ident} {status} ({full}#{n} {verb})')
            continue
        code, _ = api('PATCH', f'issues/{iid}', {'status': status, 'comment': f'{full}#{n} was {verb}; review card '
                                                 'closed by the host PR controller.'})
        log(f'review card {ident} -> {status} ({full}#{n} {verb}) HTTP {code}')
    for iid, ident, st, title in cards:
        if rr.live_run_guard.issue_has_live_work(iid):
            continue
        if title.startswith('Finish ') and merged and only_merged_prs(iid, env):
            acted += 1
            if SHADOW:
                log(f'would set {ident} done ({full}#{n} merged; card exists only to own it)')
                continue
            code, _ = api('PATCH', f'issues/{iid}', {'status': 'done', 'comment': f'{full}#{n} merged. This card only '
                                                     'owned that PR (PR_CLOSEOUT: done = merged). Closed by the host PR controller.'})
            log(f'{ident} -> done ({full}#{n} merged) HTTP {code}')
            continue
        if rec.get('noted') or rr.card_hold(iid):
            continue
        acted += 1
        text = (f"**{full}#{n} was {verb}.** Close out per PR_CLOSEOUT: if this card has no other open PR or remaining "
                "acceptance work, set it done (merged) or say what replaces it (closed). (host PR controller)")
        if SHADOW:
            log(f'would send closeout note to {ident} ({full}#{n} {verb})')
            continue
        code = rr.comment(iid, text)
        log(f'closeout note to {ident} ({full}#{n} {verb}) HTTP {code}')
        if code.startswith('2'):
            rec['noted'] = True
    return acted


def only_merged_prs(iid, env):
    urls = [r[0] for r in rr.psql(f"select url from issue_work_products where issue_id='{iid}' and type='pull_request'")]
    for u in urls:
        parts = u.rstrip('/').split('/')
        try:
            full, n = f'{parts[-4]}/{parts[-3]}', int(parts[-1])
        except (IndexError, ValueError):
            return False
        st = gh(['pr', 'view', str(n), '-R', full, '--json', 'state', '--jq', '.state'], env).strip()
        if st != 'MERGED':
            return False
    return bool(urls)


def pr_final_states(pairs, env):
    """[(full, n)] -> {(full, n): 'MERGED'|'CLOSED'|'OPEN'} via aliased GraphQL batches."""
    out = {}
    for i in range(0, len(pairs), 40):
        chunk = pairs[i:i + 40]
        parts = [f'p{j}:repository(owner:"{f.split("/")[0]}",name:"{f.split("/")[1]}"){{pullRequest(number:{n}){{state}}}}'
                 for j, (f, n) in enumerate(chunk)]
        raw = gh(['api', 'graphql', '-f', 'query={' + ' '.join(parts) + '}'], env)
        try:
            data = json.loads(raw).get('data') or {}
        except ValueError:
            continue
        for j, key in enumerate(chunk):
            node = ((data.get(f'p{j}') or {}).get('pullRequest') or {})
            if node.get('state'):
                out[key] = node['state']
    return out


def rewake_stale(state, cand, pressure):
    """Re-wake owner cards of PRs stuck on the same (state, head) past the stale window. TWO repos first, then oldest.
    Skips: host pressure, PR hold files, live work on the card, card holds. Never wakes the Reviewer (owner_cards
    excludes it)."""
    if pressure or not cand:
        return 0
    done = 0
    for _, _, key, full, pr, cur, detail, cards, wkey, since_h in sorted(cand, key=lambda c: (c[0], c[1])):
        if done >= MAX_REWAKES:
            break
        repo_name = full.split('/')[-1]
        if (ROOT / f"ops/productivity/pr-automerge.hold-{repo_name}-{pr['number']}").exists():
            continue
        iid, ident = cards[0][0], cards[0][1]
        if rr.live_run_guard.issue_has_live_work(iid) or rr.card_hold(iid):
            continue
        done += 1
        if SHADOW:
            log(f'would RE-wake {ident} for {key}: {cur} @ {pr["headRefOid"][:8]} unchanged {since_h}h')
            continue
        text = (f"**Reminder: still {cur.replace('_', ' ')} after {since_h}h on the same head.** "
                + wake_text(full, pr, cur, detail) + "\n\nFix and push once now; don't merge main unless conflict/main-red.")
        code = rr.comment(iid, text)
        log(f'RE-woke {ident} for {key}: {cur} @ {pr["headRefOid"][:8]} unchanged {since_h}h HTTP {code}')
        if code.startswith('2'):
            rec = state.get(key) or {}
            rec.setdefault('rewake', {})[wkey] = ts()
            state[key] = rec
            save(state)
    return done


def save(state):
    if APPLY:
        tmp = STATE.with_name(f'{STATE.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        tmp.replace(STATE)


def main():
    if PAUSE.exists():
        return
    from host_ok import host_ok
    ok, why = host_ok()
    pressure = not ok  # under pressure: no wakes, notes or new cards (they start runs); closeouts and monitor clears still run
    if pressure:
        print(f'host under pressure ({why}): closeouts and monitor clears only this run')
    env = rr.gh_env()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    reviews = latest_reviews()
    agents = {r[0]: r[1] for r in rr.psql(f"select name, id from agents where company_id='{CID}' and status<>'terminated'")}
    projects = {r[0]: r[1] for r in rr.psql(f"select name, id from projects where company_id='{CID}' and archived_at is null")}
    repos = [r[0] for r in rr.psql(f"select provider_resource_id from chat_endpoint_resources where endpoint_id='{EP}' "
                                   "and enabled and availability='available'")]
    seen, wakes, new_cards, counts = set(), 0, 0, {}
    rewake_cand = []
    # First run: transitions pr_supply already nudged (same PR, head and need) count as delivered, so cutover is not a
    # wake flood.
    supply = json.loads((ROOT / 'ops/productivity/pr-supply.state.json').read_text())
    supply_map = {'conflicts': 'conflict', 'ci_failing': 'ci_red', 'review_findings': 'findings', 'draft': 'draft_idle'}
    seeded = {}
    for k in supply:
        pr_head, _, need = k.rpartition(':')
        if need in supply_map:
            seeded[f'{pr_head.lower()}'] = seeded.get(pr_head.lower(), set()) | {supply_map[need]}
    for full in repos:
        if full.split('/')[-1].lower() in STOPPED_REPOS:  # owner 2026-10-13: old NNTune stopped (replaced by nntunev2)
            continue
        owner, name = full.split('/', 1)
        try:
            repo = fetch_repository(full, env)
        except (ValueError, KeyError, TypeError):
            print(f'{full}: GraphQL failed; skipped this tick')
            for k in state:
                if k.lower().startswith(full.lower() + '#'):
                    seen.add(k)  # don't treat its PRs as closed
            continue
        if not repo or repo.get('isArchived'):
            continue
        for pr in repo['pullRequests']['nodes']:
            key = f"{full}#{pr['number']}"
            seen.add(key)
            if pr['title'].startswith('chore(main): release') or pr['title'].startswith('chore: release'):
                continue  # release PRs: release train / tag-on-promote
            if any(l.get('name') == 'host-owned' for l in ((pr.get('labels') or {}).get('nodes') or [])):
                continue  # host is the single writer; owner cards and wakes would add a second writer (10-15 paperclip-ops#8)
            # PR.commits walks long branch histories and can time out every PR in the repo.
            commit = (pr.get('headRef') or {}).get('target') or {}
            if commit.get('oid') != pr['headRefOid']:
                continue  # missing/deleted head or a push during the query; never act on mismatched checks
            pr['_committed'] = commit.get('committedDate')
            contexts = (commit.get('statusCheckRollup') or {}).get('contexts') or {}
            if contexts.get('pageInfo', {}).get('hasNextPage'):
                print(f'{key}: check inventory truncated; skipped rather than infer green')
                continue
            pr['_contexts'] = contexts.get('nodes') or []
            cur, detail = classify(pr, required_checks(full, pr['baseRefName'], env), reviews, full)
            if cur == 'passed' and f"{full.split('/')[-1]}#{pr['number']}@{pr['headRefOid']}".lower() in ejected_heads():
                # Green alone, red in the merge group; nobody re-enqueues this head, so only its owner can move it
                # (10-17: two-bot-next#1008 sat 12 h as 'passed' after update-branch was refused).
                cur = 'queue_ejected'
            counts[cur] = counts.get(cur, 0) + 1
            rec = state.get(key) or {}
            head = pr['headRefOid']
            if rec.get('state') != cur or rec.get('head') != head:
                rec.update({'state': cur, 'head': head, 'since': ts()})
                if cur == 'ci_red':
                    rec['red_since'] = ts()
            rec['url'] = pr['url']
            rec['head_ref'] = pr.get('headRefName')
            if 'woke' not in rec and cur in seeded.get(f'{key}@{head}'.lower(), ()):
                rec['woke'] = f'{cur}@{head}'
            state[key] = rec
            cards = owner_cards(full, pr['number'])
            if len(cards) > 1 and not pressure and rec.get('dupe_noted') != cards[0][1]:
                for iid, ident, st, _ in cards[1:]:
                    if rr.live_run_guard.issue_has_live_work(iid):
                        continue
                    if SHADOW:
                        log(f'would note duplicate owner {ident} -> owner {cards[0][1]} for {key}')
                        continue
                    rr.comment(iid, f"**{key}** ({pr['url']}) is owned by **{cards[0][1]}** (oldest active card). Don't act "
                                    "on this PR from this card; the PR controller wakes only the owner. (host PR controller)")
                    log(f'noted duplicate owner {ident} -> owner {cards[0][1]} for {key}')
                if not SHADOW:
                    rec['dupe_noted'] = cards[0][1]
                    state[key] = rec
                    save(state)
            if not cards and not SHADOW and adopt_author(key, pr['url'], pr.get('headRefName')):
                cards = owner_cards(full, pr['number'])
            age_min = (NOW - parse(pr['createdAt'])) / 60
            if not cards and not pressure and not pr['isDraft'] and age_min >= 60 and new_cards < MAX_NEW_CARDS and name in SUPPLY_REPOS:
                who, proj, rank = SUPPLY_REPOS[name]
                new_cards += 1
                if SHADOW:
                    log(f'would create owner card for {who}: {key} ({cur})')
                else:
                    desc = (f"Own {pr['url']} until it is **merged**, or **closed with a stated reason**. The host PR "
                            "controller wakes this card when the PR needs you (red CI, review findings, conflicts); "
                            "reviews are requested and 5/5 heads auto-merge without you. Gates: `ci-ok` and Paperclip "
                            "Review 5/5 on the current head.\n\n"
                            f"Work on the PR branch `{pr.get('headRefName')}` (head `{pr['headRefOid'][:12]}`). Your assigned "
                            "workspace starts on a new branch from main, so first run `git fetch origin "
                            f"{pr.get('headRefName')}` and `git reset --hard FETCH_HEAD` in it (it holds no work of yours yet), "
                            f"then push fixes with `git push origin HEAD:refs/heads/{pr.get('headRefName')}`. That is "
                            "authorized for this card; never force-push.")
                    code, body = api('POST', f'companies/{CID}/issues', {
                        'title': f"Finish {name}#{pr['number']}", 'description': desc, 'status': 'todo',
                        'priority': 'high' if rank == 0 else 'medium', 'assigneeAgentId': agents.get(who),
                        'projectId': projects.get(proj)})
                    try:
                        c = json.loads(body)
                    except ValueError:
                        c = {}
                    if c.get('id'):
                        api('POST', f"issues/{c['id']}/work-products", {'type': 'pull_request', 'provider': 'github',
                                                                       'url': pr['url'], 'title': key, 'status': 'active'})
                        log(f"created owner card {c.get('identifier')} for {who}: {key} ({cur})")
                        cards = owner_cards(full, pr['number'])
                    else:
                        log(f'owner card create failed for {key}: HTTP {code}')
            if cards:
                ensure_backstop(cards[0][0], cards[0][1], f'owner of open {key}', pr['url'])
            for iid, ident in rr.psql(f"""select distinct i.id, i.identifier from issues i join chat_conversations c
                    on c.issue_id=i.id where i.assignee_agent_id='{REVIEWER}' and i.status not in ('done','cancelled')
                    and (i.monitor_next_check_at is not null or i.status='in_review')
                    and lower(c.external_thread_id) like 'github:{full.lower()}:{pr['number']}:%'"""):
                ensure_backstop(iid, ident, f'review card of {key}', pr['url'])
            if cur not in WAKE_STATES or age_min < MIN_AGE_MIN or pressure:
                continue
            if cur == 'ci_red' and detail.get('pending') and (NOW - parse(rec['red_since'])) / 60 < RED_SETTLE_MIN:
                continue
            wkey = f'{cur}@{head}'
            if rec.get('woke') == wkey and cards:
                name_l = full.split('/')[-1].lower()
                stale_h = STALE_H_DRAFT if cur == 'draft_idle' else (STALE_H_FOCUS if name_l in FOCUS_REPOS else STALE_H_OTHER)
                since_h = (NOW - parse(rec.get('since') or ts())) / 3600
                last = (rec.get('rewake') or {}).get(wkey)
                if since_h >= stale_h and (not last or (NOW - parse(last)) / 3600 >= stale_h) \
                        and not (cur == 'draft_idle' and last):
                    rewake_cand.append((0 if name_l in FOCUS_REPOS else 1, -since_h, key, full, pr, cur, detail, cards, wkey, round(since_h, 1)))
            if rec.get('woke') == wkey or not cards or wakes >= MAX_WAKES:
                if SHADOW:
                    print(f"  skip {key} {cur}: {'already delivered' if rec.get('woke') == wkey else 'no owner card' if not cards else 'wake cap'}")
                continue
            iid, ident = cards[0][0], cards[0][1]
            if rr.live_run_guard.issue_has_live_work(iid):
                if SHADOW:
                    print(f'  skip {key} {cur}: {ident} live run')
                continue  # retried next tick; the transition stays undelivered
            hold = rr.card_hold(iid)
            # A NEW PR transition is exactly what a soft-blocked card waits for: agents park PR cards as `blocked`
            # (often after a stale-recovery 409) or record a self-owned "act on the next controller wake" descriptor,
            # and holding the wake for those reasons deadlocked 23 PRs on 10-16. Real blocker edges, active
            # recovery and focus holds still win.
            soft = hold in ('status blocked', 'unblock descriptor') and soft_hold_ok(iid)
            if hold and not soft:
                rec['held'] = f'{ident}: {hold}'
                if SHADOW:
                    print(f'  skip {key} {cur}: {ident} held ({hold})')
                continue
            rec.pop('held', None)
            wakes += 1
            if SHADOW:
                log(f'would wake {ident} for {key}: {cur} @ {head[:8]}')
                continue
            text = wake_text(full, pr, cur, detail)
            if soft:
                text += ("\n\nThis card is parked (`blocked` or waiting on its own unblock action) and this is a new PR "
                         "event on it. If your parking reason no longer applies, set the card to `in_progress` and act on "
                         "this head; if it still applies, say why in one line and leave it parked.")
            code = rr.comment(iid, text)
            log(f'woke {ident} for {key}: {cur} @ {head[:8]} HTTP {code}')
            if code.startswith('2'):
                rec['woke'] = wkey
                save(state)
    rewakes = rewake_stale(state, rewake_cand, pressure)
    save(state)  # transitions are recorded before the slow closeout phase
    # PRs that left the open set since the last tick, plus open Reviewer cards of PRs that are no longer open.
    gone = [k for k in state if k not in seen and not state[k].get('final')]
    rev_rows = rr.psql(f"""select distinct split_part(c.external_thread_id, ':', 2), split_part(c.external_thread_id, ':', 3)
        from issues i join chat_conversations c on c.issue_id=i.id where i.company_id='{CID}'
        and i.assignee_agent_id='{REVIEWER}' and i.status not in ('done','cancelled') and c.external_thread_id like 'github:%'""")
    open_keys = {k.lower() for k in seen}
    cand = {}
    for k in gone:
        f, n = k.rsplit('#', 1)
        cand[(f, int(n))] = k
    for f, n in rev_rows:
        if n.isdigit() and f'{f}#{n}'.lower() not in open_keys:
            cand.setdefault((f, int(n)), f'{f}#{n}')
    finals = pr_final_states(list(cand), env)
    closed = 0
    for (f, n), k in cand.items():
        st = finals.get((f, n))
        if st not in ('MERGED', 'CLOSED') or closed >= MAX_CLOSE:
            continue
        cards = owner_cards(f, n)
        if not cards and k in state and not SHADOW and adopt_author(k, state[k].get('url'), state[k].get('head_ref')):
            cards = owner_cards(f, n)
        if k not in state:
            cards = [c for c in cards if c[3].startswith('Finish ')]  # backfill: only PR-owning 'Finish' cards
        rec = state.setdefault(k, {}) if not SHADOW else dict(state.get(k) or {})
        closed += closeout(f, n, st == 'MERGED', [] if pressure else cards, env, rec)
        if not SHADOW:
            rec['final'] = st
            save(state)
    live_cand = set(cand.values())
    state = {k: v for k, v in state.items() if not v.get('final') or k in live_cand}
    save(state)
    print(f'{ts()} states {json.dumps(counts, sort_keys=True)}; {len(ACTIONS)} actions ({wakes} wakes, {rewakes} re-wakes)')


if __name__ == '__main__':
    main()
