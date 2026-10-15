#!/usr/bin/env python3
"""Quarantine nested non-git repository copies in card worktrees (stopgap until fork fix case_aacb53ba/17250 ships).

SSH (Operator) sync-back leaves <worktree>/.paperclip-repositories/<repo> without .git; the next run on that card
fails setup_failed "already exists but is not a git checkout". For each such dir whose card has no
queued/running run, move it (same filesystem) to /paperclip/instances/default/quarantine-nongit/ so Paperclip
re-clones. Quarantined copies older than 7 days are deleted. Dry-run by default; --apply moves. Rollback: mv the quarantined dir back.
"""
import re
import subprocess
import sys
import time

APPLY = '--apply' in sys.argv
Q = '/paperclip/instances/default/quarantine-nongit'
LOG = '/home/ubuntu/paperclip-enterprise-company/ops/worker-vps/operator-actions.log'
# card worktrees, plus project-local worktrees (projects/<co>/<proj>/<repo>/.paperclip/worktrees/<name>/...); the
# latter must also be untouched for 30 min because the worktree name need not match the card that uses it.
FIND = ('for d in /paperclip/instances/default/worktrees/*/*/.paperclip-repositories/*; do '
        '[ -d "$d" ] && [ ! -e "$d/.git" ] && echo "$d"; done; '
        'for d in /paperclip/instances/default/projects/*/*/*/.paperclip/worktrees/*/.paperclip-repositories/*; do '
        '[ -d "$d" ] && [ ! -e "$d/.git" ] && [ -z "$(find "$d" -maxdepth 2 -newermt -30minutes -print -quit)" ] '
        '&& echo "$d"; done')


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def live_runs(ident):
    sql = ("select count(*) from heartbeat_runs r join issues i on i.id::text = r.context_snapshot->>'issueId' "
           f"where i.identifier = '{ident}' and r.status in ('queued','running')")
    out = sh(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-Atc', sql])
    return int(out.stdout.strip() or 1) if out.returncode == 0 else 1


def main():
    dirs = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c', FIND]).stdout.split()
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    if APPLY:  # quarantined copies are regenerable sync-back leftovers; keep 7 days for evidence
        sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
            f'[ -d {Q} ] && find {Q} -mindepth 1 -maxdepth 1 -ctime +7 -exec rm -rf {{}} +'])
    for d in dirs:
        m = re.search(r'/(TOG-\d+)-', d)
        if not m:
            continue
        ident = m.group(1)
        if live_runs(ident):
            print(f'skip {ident}: live run')
            continue
        dest = f'{Q}/{ident}-{d.rsplit("/", 1)[1]}-{stamp}'
        if not APPLY:
            print(f'would move {ident}: {d}')
            continue
        r = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c', 'mkdir -p "$1" && mv "$2" "$3"', '_', Q, d, dest])
        msg = f'moved {ident} non-git nested repo to {dest}' if r.returncode == 0 else f'FAILED {ident}: {r.stderr.strip()[:200]}'
        print(msg)
        with open(LOG, 'a') as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} nongit-quarantine: {msg}\n")


if __name__ == '__main__':
    main()
