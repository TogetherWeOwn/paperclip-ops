#!/usr/bin/env python3
"""Phase 1 (owner GO 2026-10-04): prepend superseding blocks to churn-causing routines; famine cadence 2x/day.
Idempotent (marker check). Backups in ops/productivity/backup-20261004T2300Z/. --apply to write."""
import json, subprocess, sys
APPLY = '--apply' in sys.argv
B = 'ops/productivity/backup-20261004T2300Z'
NOREASSIGN = """<!-- PRODUCTIVITY_PHASE1_20261004:START -->
## ASSIGNMENT DISCIPLINE (owner productivity plan, 2026-10-04): supersedes every assignment/wake number and reallocation rule below
Measured: 957 runs/7d were cancelled "before issue reassignment" (186 agent-hours), mostly from planner/watchdog/steward reallocation; 71% of those cards are now done anyway.
- **Never reassign a card that is already assigned to an active agent**, and never one with a live/queued run or assignee activity in the last 24h. Reassigning kills the running work. The only exception: the assignee is terminated/paused/in error for >2h; then reassign once and say why.
- **At most 2 reassignments of already-assigned cards per firing** (prefer 0). Assigning NEW or unassigned cards is separate: up to 6 per firing.
- Supply comes from new child slices with a `blocks` edge, not from moving existing work between agents.
- Blocked cards: block only on the live leaf card doing the work (edge + unblockDescriptor naming owner and done-test). Never block on CI-queue/review-tracker/cancellable cards or on an intermediate blocked card. A blocked card with no live edge, descriptor or pending interaction goes back to todo.
- Never post a comment on a card that has a live/queued run or a scheduled retry: on this Paperclip build a comment cancels the retry. Use a PATCH without a comment, or wait.
<!-- PRODUCTIVITY_PHASE1_20261004:END -->

"""
FAMINE = """<!-- PRODUCTIVITY_PHASE1_20261004:START -->
## CADENCE (owner productivity plan, 2026-10-04): twice-daily DIGEST, not a pager
This census fired 144x/24h and paged the CEO without ever admitting work. It now runs at 09:17Z and 21:17Z. Post ONE digest comment on the CEO desk card only when certified READY < target for 2 consecutive censuses; otherwise record the census on your own firing card and close it. Admission stays with the O1 planner.
<!-- PRODUCTIVITY_PHASE1_20261004:END -->

"""
O1_EXTRA = """<!-- PRODUCTIVITY_O1_20261004:START -->
## ADMIT FIRST, THEN MEASURE (owner productivity plan, 2026-10-04)
Keep ONE persistent document named `ready-list` on this routine's firing card lineage (or the planner's standing card). Each firing: (1) admit work first by assigning NEW/unassigned priority-path slices (≤6) and creating missing child slices; (2) then update `ready-list`, re-GETting ONLY cards whose updatedAt changed since the last firing instead of GETting every candidate every 20 minutes.
<!-- PRODUCTIVITY_O1_20261004:END -->

"""
TARGETS = {
 '00000000-0000-4000-8000-000000000043': NOREASSIGN + O1_EXTRA,   # O1 planner
 '00000000-0000-4000-8000-000000000044': NOREASSIGN,              # CEO watchdog judgement
 '00000000-0000-4000-8000-000000000045': NOREASSIGN,              # Director delivery steward
 '00000000-0000-4000-8000-000000000046': NOREASSIGN,              # DevOps CI steward
 '00000000-0000-4000-8000-000000000047': FAMINE,                  # famine watchdog
}
def api(method, path, body=None):
    a = ['curl', '-sS', '-K', '.pc.curl', '-X', method, f'http://127.0.0.1:3100/api{path}', '-H', 'content-type: application/json']
    if body is not None: a += ['--data-binary', '@-']
    r = subprocess.run(a, input=json.dumps(body) if body is not None else None, text=True, capture_output=True)
    try: return json.loads(r.stdout)
    except ValueError: return {'_raw': r.stdout[:200]}
for rid, block in TARGETS.items():
    cur = api('GET', f'/routines/{rid}')
    desc = cur.get('description') or ''
    marker = block.split('\n', 1)[0]
    if marker in desc and 'PRODUCTIVITY_O1' not in block:
        print(rid, 'already applied'); continue
    if 'PRODUCTIVITY_O1_20261004:START' in desc: print(rid, 'already applied'); continue
    new = block + desc
    print(rid, cur.get('title','')[:60], len(desc), '->', len(new))
    if APPLY:
        r = api('PATCH', f'/routines/{rid}', {'description': new})
        print('   patched rev', r.get('latestRevisionNumber'), 'ok' if (r.get('description') or '').startswith('<!-- PRODUCTIVITY') else r)
if APPLY:
    r = api('PATCH', '/routine-triggers/00000000-0000-4000-8000-000000000048', {'cronExpression': '17 9,21 * * *', 'label': 'twice-daily famine digest (owner productivity plan 10-04)'})
    print('famine trigger ->', r.get('cronExpression'), r.get('nextRunAt'))
