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
