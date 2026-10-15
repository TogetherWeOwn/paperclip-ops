#!/usr/bin/env python3
"""Owner 2026-10-05: every agent acts on GitHub as the owner through Paperclip's built-in GitHub connection (old App
minter/broker/shim/company-bot MCP retired), and the agent that starts a piece of work finishes it (no publication
handoffs). Upstream PRs need upstream Greptile 5/5 AND our Paperclip Review 5/5 on the same head.

Rewrites stale lines in every live AGENTS.md / TOOLS.md of non-terminated agents and appends one idempotent block.
Backs up every file it changes. Usage: fix_bundles_github_doer.py [--apply]"""
import json, re, subprocess, sys, time
from pathlib import Path

ROOT = Path('/home/ubuntu/paperclip-enterprise-company')
BASE = Path('/home/ubuntu/stacks/paperclip/data/instances/default/companies/00000000-0000-4000-8000-000000000004/agents')
STEWARD = '00000000-0000-4000-8000-000000000051'
TAG = 'GITHUB_DOER_RULE_20261005'
APPLY = '--apply' in sys.argv

BLOCK = f"""<!-- {TAG}:START -->
## GitHub as the owner, and finish what you start (owner, 2026-10-05)
- Every agent acts on GitHub as the owner (Rick7C2) through Paperclip's built-in GitHub connection. `git`, `gh` and the GitHub tools use it automatically. The old App token broker, gh shim and company-bot GitHub tool are retired: never mint, export or ask for tokens. If GitHub refuses, paste the exact error on your card.
- **The agent that starts the work finishes it**: code, push, PR body, every reply to maintainers, Greptile and our Paperclip Review, re-requests and follow-ups, until merged. No handoff cards to another agent to publish, push, post or reply. Split off a card only for a genuinely separate piece of work.
- **Upstream PRs** (any repo outside TogetherWeOwn). Before asking maintainers to look, the same head must have:
  1. upstream Greptile 5/5, with every Greptile comment fixed or answered;
  2. our Paperclip Review 5/5: open a draft mirror PR from the same branch into the TogetherWeOwn fork (base = a branch holding the exact upstream base commit) and fix everything it finds;
  3. all required CI green and every PR-template checkbox ticked.
  Done = merged upstream.
- Public text you post anywhere: no `TOG-` refs, internal hostnames, agent names or secrets. Check it yourself before posting.
- Do not poll GitHub. The upstream PR watcher comments on your card when a review, comment, check result or merge arrives. Keep only a 24h backstop monitor on PR-waiting cards.
<!-- {TAG}:END -->"""

AGENTS_SUBS = [
    ("2. Upstream PRs, comments and edits: the CEO publishes them as the owner via its GitHub connector.",
     "2. Upstream PRs, comments and edits: the agent doing the work publishes them itself, as the owner, through Paperclip's GitHub connection (every agent has it)."),
]
STEWARD_SUBS = [
    ("- Own all upstream-PR monitor cards: read every maintainer, bot (Greptile etc.) and CI comment; make sure each request is fixed correctly by the owning work agent; draft the replies.",
     "- Own every upstream PR you start, end to end until merged: fix what maintainers, Greptile, our Paperclip Review and CI raise, push, post every reply, tick the checklist."),
    ("- the final public-text gate: exact text + target URL, ready to click, with a no-leak check on every artifact\n- upstream-PR monitor cards and reply drafts",
     "- upstream PRs you start, end to end, posted by you as the owner\n- upstream-standards advice when another agent asks (you are not a gate in their path)"),
    ("Decide alone: whether a public draft is clean and ready to hand to the CEO. Not your job: general engineering; posting upstream yourself; org-wide policy. Repos and projects: public repos and forks read-only plus drafts; never push upstream yourself (the bot App cannot post upstream anyway).",
     "Decide alone: everything on your own upstream PRs. Not your job: taking over PRs another agent started; org-wide policy. Repos: public repos and the TogetherWeOwn forks, read and write as the owner."),
    ("## Trust ramp — stage 1 (now)\n- Drafts only. Every public post, PR open, PR edit or comment is prepared \"ready to click\" (exact text + target URL) on a card and handed to the CEO, who publishes it as the owner via its GitHub connector (owner rule 2026-09-28); never route a click to the owner. Never post upstream yourself.",
     "## Posting (owner 2026-10-05: stage 2 approved)\n- You open, edit, push and reply upstream yourself, as the owner, through Paperclip's GitHub connection. No \"ready to click\" drafts and no CEO handoff."),
    ("- Stage 2 (auto-post) only on the owner's explicit say-so after a clean track record.\n", ""),
    ("GitHub access via the bot MCP connector only. Never the owner connector.",
     "GitHub access via Paperclip's built-in GitHub connection (acts as the owner)."),
]
SHELL_RE = re.compile(r"^- Shell \(container user `node`\):.*$", re.M)
SHELL_NEW = ("- Shell (container user `node`): `git` and `gh` act as the owner through Paperclip's built-in GitHub connection "
             "(per-run wrappers first on PATH); never export `GH_TOKEN`. Also `node`, `python3`, `jq`, `rg`, `curl`.")
IDENT_RE = re.compile(r"^As of ?2026-09-24, the native `paperclip-for-github` connection.*$", re.M)
IDENT_NEW = ("GitHub identity (owner 2026-10-05): every agent acts as the owner (Rick7C2) through Paperclip's built-in GitHub "
             "connection. The company App token broker, gh shim and company-bot GitHub tool are retired. A GitHub 403 means "
             "the owner's account lacks that permission: paste the exact error on your card.")
TOOLS_SUBS = [
    ("- No host access. Host needs are one `Operator:` card assigned to the owner.",
     "- No host access. Host needs are one `Operator:` card assigned to the CEO (never the owner)."),
]


def live_agents():
    q = ("select id from agents where company_id='00000000-0000-4000-8000-000000000004' and status<>'terminated' "
         "and adapter_type='claude_local' and name<>'TogetherWeOwn Reviewer'")
    out = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-Atc', q],
                         capture_output=True, text=True, check=True).stdout
    return [x for x in out.split() if x]


def read(p):
    r = subprocess.run(['sudo', '-n', 'cat', str(p)], capture_output=True, text=True)
    return None if r.returncode else r.stdout


def put(agent, path, content):
    r = subprocess.run(['curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}', '-K', str(ROOT / '.pc.curl'), '-X', 'PUT',
                        '-H', 'content-type: application/json',
                        f'http://127.0.0.1:3100/api/agents/{agent}/instructions-bundle/file', '--data-binary', '@-'],
                       input=json.dumps({'path': path, 'content': content}), text=True, capture_output=True)
    return r.stdout


def main():
    bk = ROOT / f"ops/productivity/bundles-before-github-doer-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    changed = []
    for agent in live_agents():
        for name in ('AGENTS.md', 'TOOLS.md'):
            p = BASE / agent / 'instructions' / name
            old = read(p)
            if old is None:
                continue
            new = old
            if name == 'AGENTS.md':
                for a, b in AGENTS_SUBS + (STEWARD_SUBS if agent == STEWARD else []):
                    new = new.replace(a, b)
                if f'{TAG}:START' not in new:
                    new = new.rstrip('\n') + '\n\n' + BLOCK + '\n'
            else:
                new = SHELL_RE.sub(SHELL_NEW, new)
                new = IDENT_RE.sub(IDENT_NEW, new)
                for a, b in TOOLS_SUBS:
                    new = new.replace(a, b)
            if new == old:
                continue
            changed.append(f'{agent[:8]}/{name}')
            if APPLY:
                bk.mkdir(parents=True, exist_ok=True)
                (bk / f'{agent}.{name}').write_text(old)
                code = put(agent, name, new)
                if code != '200':
                    print('PUT failed', agent, name, code)
    print(('applied ' if APPLY else 'would change ') + str(len(changed)) + ' files' + (f'; backup {bk}' if APPLY and changed else ''))
    print(' '.join(changed))


if __name__ == '__main__':
    main()
