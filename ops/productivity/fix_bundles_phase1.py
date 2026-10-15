#!/usr/bin/env python3
"""Productivity Phase 1 (owner GO 2026-10-04): rewrite self-inflicted churn/blocking/review rules in agent AGENTS.md.
Idempotent; backs up each file; writes through the board API (audited). --apply to write. Same text is folded into
the bundle templates by fold_templates (see report) so a re-render keeps it."""
import json, subprocess, sys, time
from pathlib import Path

ROOT = Path('/home/ubuntu/paperclip-enterprise-company')
COMPANY = '00000000-0000-4000-8000-000000000004'
BASE = Path(f'/home/ubuntu/stacks/paperclip/data/instances/default/companies/{COMPANY}/agents')
APPLY = '--apply' in sys.argv
BACKUP = ROOT / f"ops/productivity/bundles-backup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"

REPLACE = [
    ("Delegate to any idle agent matching the role; engineering only to engineers.",
     "Finish your assigned card. Never reassign a card already assigned to a live agent. Delegate only a newly created "
     "child slice with a `blocks` edge (engineering only to engineers). If blocked, set `blockedByIssueIds` (+ monitor where "
     "allowed); never unassign or reassign to unblock."),
    ("If the work is bigger, create child cards with `blocks` edges and delegate them to matching idle agents.",
     "If the work is bigger, create NEW child slices with `blocks` edges and assign each new slice to a matching agent; "
     "never reassign an existing card that another agent holds."),
    ("- `status: \"blocked\"`, with `blockedByIssueIds` pointing at the real blocking card. Create that card if it doesn't exist.",
     "- `status: \"blocked\"` ONLY with `blockedByIssueIds` pointing at the live leaf card that is doing the work, plus an "
     "`unblockDescriptor` naming its owner and the exact done-test. Never block on a CI-queue, review-tracker or other "
     "cancellable tracker card, or on an intermediate blocked card. Waiting on CI or a review verdict: stay `in_progress` "
     "with `executionPolicy.monitor`. A blocked card with no live edge, descriptor or pending interaction is `todo`."),
    ("Woken by unblock, dispatch or retry with the same blocker or error as your last run on this card and no new comment: "
     "leave its status, blockers and monitor as they are, post nothing, exit",
     "Woken by unblock, dispatch or retry with the same blocker or error as your last run on this card and no new comment: "
     "re-check the blocker first; if it is done/cancelled or is not the live leaf doing the work, fix the edge (or set "
     "`todo`) and continue; otherwise leave status and monitor as they are, post nothing, exit"),
    ("- CI-green to verdict comment p50: at most 4 h", "- CI-green to verdict comment p50: at most 1 h"),
    ("Comment `Review <sha>: APPROVE` or `Review <sha>: CHANGES` plus the list;",
     "Comment `Review <sha>: APPROVE` or `Review <sha>: CHANGES` plus the list on the review card AND on the author's card "
     "(that is how the author learns the verdict; authors never block on a review card);"),
]
BLOCK = """<!-- PRODUCTIVITY_PHASE1_20261004:START -->
## Flow rules (owner productivity plan, 2026-10-04): measured self-inflicted waste
- **Finish, don't reshuffle.** Never reassign or unassign a card another live agent holds: reassignment cancels its running work (957 runs/7d). Supply comes from NEW child slices with a `blocks` edge.
- **Waiting is not blocked.** Waiting on CI, a review verdict or a monitor = stay `in_progress` with `executionPolicy.monitor`. Authors never block on a review card; the reviewer posts the verdict on the author's card.
- **Block only on the live leaf** doing the work, with an edge + `unblockDescriptor` (owner + done-test). Never on CI-queue/review-tracker/cancellable cards or an intermediate blocked card. When a blocker is cancelled, remove the edge and continue or pick the real leaf. No live edge, descriptor or pending interaction = `todo`.
- **Don't comment on a card that has a live/queued run or a scheduled retry**: a comment cancels the retry. PATCH without a comment, or wait for the run to end.
<!-- PRODUCTIVITY_PHASE1_20261004:END -->"""


def sh(*a, input=None):
    return subprocess.run(a, input=input, text=True, capture_output=True)


def agents():
    out = sh('docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At', '-F', '|', '-c',
             f"select id, name from agents where company_id='{COMPANY}' and status<>'terminated'").stdout
    return [l.split('|', 1) for l in out.splitlines() if l]


changed = 0
counts = {a[:40]: 0 for a, _ in REPLACE}
for aid, name in agents():
    path = BASE / aid / 'instructions' / 'AGENTS.md'
    r = sh('sudo', '-n', 'cat', str(path))
    if r.returncode != 0:
        continue
    old = new = r.stdout
    for a, b in REPLACE:
        if a in new:
            new = new.replace(a, b); counts[a[:40]] += 1
    if 'PRODUCTIVITY_PHASE1_20261004:START' not in new:
        new = new.rstrip('\n') + '\n\n' + BLOCK + '\n'
    if new == old:
        continue
    changed += 1
    if APPLY:
        BACKUP.mkdir(parents=True, exist_ok=True)
        (BACKUP / f'{aid}.AGENTS.md').write_text(old)
        resp = sh('curl', '-sS', '-K', str(ROOT / '.pc.curl'), '-X', 'PUT', '-H', 'content-type: application/json',
                  f'http://127.0.0.1:3100/api/agents/{aid}/instructions-bundle/file', '--data-binary', '@-',
                  input=json.dumps({'path': 'AGENTS.md', 'content': new}))
        ok = sh('sudo', '-n', 'grep', '-c', 'PRODUCTIVITY_PHASE1_20261004:START', str(path)).stdout.strip()
        if ok != '1':
            print(f'  VERIFY FAIL {name}: {resp.stdout[:120]}')
print(json.dumps(counts, indent=1))
print(f"{'APPLIED' if APPLY else 'DRY'}: {changed} bundles; backup {BACKUP if APPLY else '-'}")
