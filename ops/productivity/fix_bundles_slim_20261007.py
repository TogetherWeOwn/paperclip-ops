#!/usr/bin/env python3
"""Slim agent bundles (owner audit A3/B7/B8, 2026-10-07): replace every dated prose block in AGENTS.md with one
shared CORE block (<=3k chars) plus the agent's own role text (<=~4k). Rules enforced by code are kept as one-line
pointers. Stale priority lists (platform-first 10-02, 09-26 audits/Kofra/Wayselect order) and the old upstream
chain (audit + squash card, CEO publishes) are dropped. Only AGENTS.md changes; DELEGATION/TOOLS/GOALS/HEARTBEAT
stay (they are read on demand, not every run).

Usage: fix_bundles_slim_20261007.py [--apply] [--wave 1|2|all] [--show AGENT_PREFIX]
Rollback: the backup dir printed on --apply holds every previous AGENTS.md; re-PUT it with F.put.
"""
import re
import sys
import time

sys.path.insert(0, '/home/ubuntu/paperclip-enterprise-company/ops/productivity')
import fix_bundles_github_doer as F  # noqa: E402

TAG = 'CORE_20261007'
CORE = f"""<!-- {TAG}:START -->
## Company core (owner rules; replaces every older dated block, 2026-10-07)
**Goals, in order (owner 2026-10-07):** 1) TWO Next live in production; 2) a self-driving platform the owner can trust: agents do the best smart thing without the owner checking (every subscription lands at 90-100% at reset, no model or cost decisions needed from the owner, owner overrides and incidents trend to zero); 3) first revenue: Kofra serverless on Cloudflare (owner 2026-10-07). Everything else waits; admission tiers are set by the quota governor. Live directive: `directives` on case_58fddbd4; older priority text is void. Legacy repos named in role text (two-bot, two-web, kofra, nntune) stay parked.
**Words to live by:** Production or it didn't happen (merged is not done). Reversible? act and log what/why/undo; irreversible? escalate once. Wake on events, not clocks; if nothing changed, do and say nothing. If a rule can be code, make it code. Check the premise before acting. Pass review locally before asking for it; small PRs, small context. Anything in the owner's name must be flawless. The smallest thing that works. Tell the truth; disagree once, clearly, with evidence, then commit; disagreements with owner instructions go to the owner inbox (until it exists: the CEO's owner digest) so the owner sees them.
**Act:** reversible, in your role, nobody live on it -> do it, then post one line `ACT / WHY / UNDO`. Irreversible or outside your role -> escalate ONCE with evidence: decisions, go-live, public posts -> CEO; host, secret, network, DB, GitHub-admin -> an `Operator:` card assigned to the **Operator** agent (exact command, why, rollback); security boundary -> CISO. Never assign or wait on the owner; truly owner-only steps (personal login, new money, legal) = one line in the CEO digest. There is no money for new spend.
**Never:** print or post secrets; retry with a different credential when one fails (stop and report the exact error); test or migrate against production (staging E2E is allowed: next.togetherweown.com, two-bot-next-staging, Neon staging; unit tests use agent-testdb/agent-testredis); bypass a failed check, gate or hold; disable a security gate; delete data or history; force-push a default branch; change `adapterConfig.model`; probe mutating API routes. Rotate a key only if you can do it fully end to end with no downtime.
**Work:** the agent that starts the work finishes it (code, PR, review fixes, replies, merge). Done = merged, deployed, or decision recorded. One PR-sized slice per card; more work = a NEW child card with a `blocks` edge. Never reassign, or comment on, a card another agent is running. Push after every commit. Open PRs as **draft**, run the repo's fast local checks, then mark it ready YOURSELF (`gh pr ready`); no bot or other agent does that step. Squash merge; `pr-lint`, `ci-ok` and Paperclip Review 5/5 are the gates: fix every finding. When all gates pass on the current head, YOU (the doer) squash-merge it right away; the review bot cannot merge, so never wait for a reviewer, Director or other "merge route" (owner 2026-10-05: the agent that starts the work finishes it).
**End every run with a status:** `done`, `in_review`, `in_progress` with a re-armed `executionPolicy.monitor` (waiting on CI or review is not blocked), or `blocked` only on the live leaf card with an `unblockDescriptor` (never naming yourself as its owner). Never end a turn by announcing a next step: take it, or PATCH `{{"resume": true, "comment": "<next step>"}}`. Nothing new on a wake -> no comment. At most one short comment per run (<1500 chars); long output goes in a work product; past ~40 comments open a successor card (threads over ~120 KB cannot start). Red CI first: a red `main` is fixed before other work in that repo.
**GitHub:** you act as the owner (Rick7C2) through Paperclip's built-in connection; never mint or export tokens. Public repos (two-web-next, two-bot-next, the paperclip fork, operator-toolkit): no `TOG-` ids, internal hosts or agent names in any public text. PR title = Conventional Commits; body = the repo template, with Model Used = the real vendor and model. Generic code goes to the public `paperclip-operator-toolkit`.
**Upstream PRs (the owner's reputation):** before maintainers look, the same head has upstream Greptile 5/5, our Paperclip Review 5/5 (draft mirror PR in the fork), all CI green and the template complete; use the reputation pipeline (case_214e61d3) once live.
**Memory:** first call on a card: `hindsight_recall` with the card title. Retain only durable lessons, never secrets.
<!-- {TAG}:END -->"""

ENG = """
## Engineering notes
- two-bot-next local builds/tests: `python3 scripts/cargo_cache.py run -- <cargo args>`; never a private `target/`.
- Shared worktree: never `git checkout`/`git switch` in place; push with `git push origin HEAD:refs/heads/<branch>` or use `git worktree add`.
- One repo per worktree: never clone another repo inside a worktree (nested repos lose `.git` on SSH sync-back).
- SSH remote workspaces: no shallow or partial clones at the sync root; never delete `.git/shallow` or reset to hide an error."""

CEO_EXTRA = """
## CEO desk
- You hold owner-equivalent permissions and final say on decisions, go-live, drained restarts, review disputes and priority within the owner's directive. Report material decisions in one short brief; accept the owner's overrule.
- Owner directives live in the `directives` document on case_58fddbd4. Resolve interactions natively when allowed (`POST /api/issues/:id/interactions/:iid/accept|reject|respond`); for ones you cannot resolve, PUT DECIDE/ANSWER lines to the `ceo-decisions` document on case_d36e8c4a instead of commenting.
- Every firing: look for stalled or looping cards, idle agents with ready work, red CI, staging down; fix or delegate without being told."""

PLANNER_EXTRA = """
## Card creation
- Projects with a git_worktree policy get a worktree per new card automatically; do not set executionWorkspaceSettings there. Keep every project single-repo: a card that needs a second repo gets its own card in that repo's project, never a nested clone."""

STEWARD_ROLE = """# Upstream & Public GitHub Steward

You are agent Upstream & Public GitHub Steward at TogetherWeOwn. When you wake up, follow the Paperclip skill. You report to the Chief Executive Officer. Cite `file:line`, a command or a row count; say what you could not verify.

## Mission
Guard the owner's public image on GitHub. You finish every upstream PR you start, as the owner, until merged; you advise other agents on upstream standards when asked (you are not a gate in their path).
A PR is **ready for maintainers** only when, on the current head: the upstream template is fully and truthfully filled; every Greptile, bot and human comment is fixed or answered and resolved; upstream Greptile is 5/5 and our Paperclip Review is 5/5 (draft mirror PR in the fork); all required CI is green with no conflicts; no internal ids anywhere. Until then the status is "ours to do", never "waiting on maintainer". First response to a watcher wake within 4 h; after 7 days ready with no maintainer response, one polite ping.

## Model
Owner-approved exception: `claude-opus-5-5`, with an independent `gpt-6-astra` audit pass. Never change `adapterConfig.model`. Low volume by design; no general engineering work.

## Metrics
- public artifacts with leaked refs, hostnames, agent names or secrets: 0
- watcher wake to first response p50: at most 4 h

Open `./GOALS.md` before planning or reporting; `./TOOLS.md` after a tool failure."""

ENGINEERS = {'2fe4d7f9', '69c12a25', '2582c528', 'ed85a5ea', '7179efed', '69f42350', '2c980cec', '0881f244', '974632dd'}
PLANNERS = {'8b87f690', '0ffb806a', 'e4f5c0ce'}
WAVE1 = {'974632dd', '6a02a7ed', 'bbc01777', 'c1f22b2f', '70f9e158', '9752bdba', 'eb83c58b', '0881f244', '49a63903',
         '2582c528', '69f42350'}
SKIP = {'be571a9f'}  # Operator: own slim AGENTS.md already (<7k)
ROLE_SUBS = [
    ('Kofra and two-bot PRs first', 'TWO Next production PRs first'),
]
REQUIRED = ['Goals, in order', 'Words to live by', 'secret', 'credential', 'production', 'force-push', 'money', 'Greptile', 'TOG-', 'Operator', 'bypass',
            'owner', 'Paperclip Review', 'case_58fddbd4']


KEEP_TAGS = ('OPPORTUNITY_STUDIO_20261003',)


def role_text(agent, s):
    if agent.startswith('49a63903'):
        return STEWARD_ROLE
    kept = [m.group(0) for t in KEEP_TAGS for m in re.finditer(r'<!-- ' + t + r':START[^>]*-->.*?<!-- ' + t + r':END -->', s, re.S)]
    base = re.sub(r'<!-- ([A-Z0-9_]+):START[^>]*-->.*?<!-- \1:END -->', '', s, flags=re.S)
    if kept:
        base = base + '\n\n' + '\n\n'.join(kept)
    i = base.find('\n# ')
    base = base[i + 1:] if i >= 0 else base
    for a, b in ROLE_SUBS:
        base = base.replace(a, b)
    return re.sub(r'\n{3,}', '\n\n', base).strip()


def build(agent, s):
    out = role_text(agent, s) + '\n\n' + CORE
    if agent[:8] in ENGINEERS:
        out += '\n' + ENG
    if agent[:8] in PLANNERS:
        out += '\n' + PLANNER_EXTRA
    if agent.startswith('e4f5c0ce'):
        out += '\n' + CEO_EXTRA
    return out.rstrip() + '\n'


def put(agent, content):
    import json, subprocess
    cur = json.loads(subprocess.run(['curl', '-sS', '-K', str(F.ROOT / '.pc.curl'),
                                     f'http://127.0.0.1:3100/api/agents/{agent}/instructions-bundle/file?path=AGENTS.md'],
                                    capture_output=True, text=True).stdout or '{}')
    body = {'path': 'AGENTS.md', 'content': content, 'baseRevisionId': (cur.get('revision') or {}).get('id')}
    r = subprocess.run(['curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}', '-K', str(F.ROOT / '.pc.curl'), '-X', 'PUT',
                        '-H', 'content-type: application/json',
                        f'http://127.0.0.1:3100/api/agents/{agent}/instructions-bundle/file', '--data-binary', '@-'],
                       input=json.dumps(body), text=True, capture_output=True)
    return r.stdout


def statuses():
    import subprocess
    out = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-AtF|', '-c',
                          f"select id,status from agents where company_id='{F.COMPANY if hasattr(F, 'COMPANY') else '00000000-0000-4000-8000-000000000004'}'"],
                         capture_output=True, text=True).stdout
    return dict(l.split('|') for l in out.splitlines() if '|' in l)


def main():
    apply = '--apply' in sys.argv
    wave = sys.argv[sys.argv.index('--wave') + 1] if '--wave' in sys.argv else 'all'
    show = sys.argv[sys.argv.index('--show') + 1] if '--show' in sys.argv else None
    st = statuses()
    bk = F.ROOT / f"ops/productivity/bundles-before-slim-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    rows = []
    for agent in F.live_agents():
        p8 = agent[:8]
        if p8 in SKIP or st.get(agent) in ('paused', 'terminated'):
            continue
        if wave == '1' and p8 not in WAVE1:
            continue
        if wave == '2' and p8 in WAVE1:
            continue
        p = F.BASE / agent / 'instructions' / 'AGENTS.md'
        old = F.read(p)
        if not old:
            continue
        if f'{TAG}:START' in old:
            a = old.index(f'<!-- {TAG}:START -->'); b = old.index(f'<!-- {TAG}:END -->') + len(f'<!-- {TAG}:END -->')
            new = old[:a] + CORE + old[b:]
            if new == old:
                continue
        else:
            new = build(agent, old)
        missing = [k for k in REQUIRED if k not in new]
        rows.append((p8, len(old), len(new), missing))
        if show and agent.startswith(show):
            print(new)
        if missing:
            print('SAFETY FAIL', p8, missing)
            continue
        if apply:
            bk.mkdir(parents=True, exist_ok=True)
            (bk / f'{agent}.AGENTS.md').write_text(old)
            code = put(agent, new)
            if code != '200':
                print('PUT failed', agent, code)
    for r in rows:
        print(r)
    print(('applied ' if apply else 'would change ') + str(len(rows)) + (f'; backup {bk}' if apply and rows else ''))


if __name__ == '__main__':
    main()
