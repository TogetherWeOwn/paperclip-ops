"""Stub pre-packet controller regions for offline patch testing.

Contains ONLY the anchor lines the ci-policy packet touches, transcribed from
the reviewed pr-controller-wakes.patch post-image and placed at the same line
numbers, so the packet test can apply the ci patch with --fuzz=0 exactly as
the host install does. It is not a runtime controller.
"""

IMPORTS = [
    'import review_error_rerequest as rr  # noqa: E402  (psql, gh_env, comment, card_hold, pr_url_patterns, live_run_guard)',
    'from pr_controller_wakes import post_wake, reconcile_wake  # noqa: E402',
    'from pr_supply import REPOS as SUPPLY_REPOS  # noqa: E402',
]

REWAKE = [
    '            continue',
    '        text = (f"**Reminder: still {cur.replace(\'_\', \' \')} after {since_h}h on the same head.** "',
    '                + wake_text(full, pr, cur, detail) + "\\n\\nFix and push once now; don\'t merge main unless conflict/main-red.")',
    '        rec = state.get(key) or {}',
    '        code = post_wake(rec, wkey, iid, text, api)',
    '        log(f\'posted RE-wake {ident} for {key}: {cur} @ {pr["headRefOid"][:8]} unchanged {since_h}h HTTP {code}\')',
    '        if code.startswith(\'2\'):',
    '            state[key] = rec',
    '            save(state)',
]

WAKE = [
    '                text += ("\\n\\nThis card is parked (`blocked` or waiting on its own unblock action) and this is a new PR "',
    '                         "event on it. If your parking reason no longer applies, set the card to `in_progress` and act on "',
    '                         "this head; if it still applies, say why in one line and leave it parked.")',
    '            code = post_wake(rec, wkey, iid, text, api)',
    '            log(f\'posted wake {ident} for {key}: {cur} @ {head[:8]} HTTP {code}\')',
    '            if code.startswith(\'2\'):',
    '                save(state)',
]


def stub_source():
    lines = []
    while len(lines) < 38:
        lines.append(f'# stub padding line {len(lines) + 1}')
    lines.extend(IMPORTS)  # 39-41
    while len(lines) < 497:
        lines.append(f'# stub padding line {len(lines) + 1}')
    # Wrappers make the stub valid Python (compile-checked after patching);
    # anchor lines keep their exact host indentation.
    lines.append('def _rewake_site():')  # 498
    lines.append('    for key in []:')  # 499
    lines.append('        if True:')  # 500
    lines.extend(REWAKE)  # 501-509
    while len(lines) < 688:
        lines.append(f'# stub padding line {len(lines) + 1}')
    lines.append('def _wake_site():')  # 688
    lines.append('    for key in []:')  # 689
    lines.append('        if True:')  # 690
    lines.append('            if True:')  # 691
    lines.extend(WAKE)  # 692-698
    lines.append('    rewakes = rewake_stale(state, rewake_cand, pressure)')
    lines.append('    save(state)')
    lines.append('    return done')
    return '\n'.join(lines) + '\n'
