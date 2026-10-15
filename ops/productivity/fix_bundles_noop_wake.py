#!/usr/bin/env python3
"""Owner 2026-10-05: a no-op wake must never post a comment and must leave a live continuation path, else Paperclip
re-wakes the card every ~30s (issue_continuation_needed). Appends an idempotent block to every live AGENTS.md via the API."""
import json, subprocess, time
from pathlib import Path
ROOT = Path('/home/ubuntu/paperclip-enterprise-company')
BASE = Path('/home/ubuntu/stacks/paperclip/data/instances/default/companies/00000000-0000-4000-8000-000000000004/agents')
TAG = 'NOOP_WAKE_RULE_20261005'
BLOCK = f"""<!-- {TAG}:START -->
## Nothing new on a wake? Exit quietly, and leave a live path (2026-10-05)
- A wake with nothing new gets **no comment**. Comments wake people and grow threads; "no-op, no comment" posted as a comment is the bug.
- Before ending any run with the card still `in_progress`, make sure something will wake it later: a scheduled `executionPolicy.monitor` (a monitor that fired is used up, so **re-arm it**), or `blocked` on the live leaf with an `unblockDescriptor`. Otherwise set `todo` or `done`.
- An `in_progress` card with no monitor, blocker or run is re-woken by Paperclip about every 30 seconds (case_40c3499e burned 77 runs in an hour this way).
<!-- {TAG}:END -->"""
bk = ROOT / f"ops/productivity/bundles-before-noop-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
n = 0
for d in sorted(BASE.iterdir()):
    p = d / 'instructions' / 'AGENTS.md'
    r = subprocess.run(['sudo', '-n', 'cat', str(p)], capture_output=True, text=True)
    if r.returncode or f'{TAG}:START' in r.stdout:
        continue
    bk.mkdir(parents=True, exist_ok=True); (bk / f'{d.name}.AGENTS.md').write_text(r.stdout)
    new = r.stdout.rstrip('\n') + '\n\n' + BLOCK + '\n'
    subprocess.run(['curl', '-sS', '-o', '/dev/null', '-K', str(ROOT / '.pc.curl'), '-X', 'PUT', '-H', 'content-type: application/json',
                    f'http://127.0.0.1:3100/api/agents/{d.name}/instructions-bundle/file', '--data-binary', '@-'],
                   input=json.dumps({'path': 'AGENTS.md', 'content': new}), text=True)
    n += subprocess.run(['sudo', '-n', 'grep', '-c', f'{TAG}:START', str(p)], capture_output=True, text=True).stdout.strip() == '1'
print(f'updated {n}; backup {bk}')
