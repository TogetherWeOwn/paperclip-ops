#!/usr/bin/env python3
"""Owner 2026-10-06: "if you have the means at hand and the action is reversible, you have the responsibility to act.
Only if it is not reversible escalate to the agent with the authority (9 of 10: the CEO or the operator)."
Backed by research (Amazon one-way/two-way doors, SRE mitigate-first, mission command, Anthropic/OpenAI/OWASP
agent guidance). Appends one idempotent block to every live AGENTS.md of non-terminated claude_local agents.
Usage: fix_bundles_act_reversible.py [--apply]"""
import sys, time
sys.path.insert(0, '/home/ubuntu/paperclip-enterprise-company/ops/productivity')
import fix_bundles_github_doer as F  # noqa: E402

TAG = 'ACT_IF_REVERSIBLE_20261006'
OLD_TAG_REPLACE = True
BLOCK = f"""<!-- {TAG}:START -->
## Reversible? Act. Irreversible? Escalate once, to the right agent (owner, 2026-10-06)
"If you have the means at hand and the action is reversible, you have the responsibility to act." Waiting, asking or filing a handoff for a reversible fix you can make is a failure, not caution.
1. **Act yourself** when all are true: you can undo it (a known rollback), it is within your role and the things you own (your card, PR, branch, lane, config), and no other agent is live on it. Do the smart thing now. Do not ask first.
2. **Escalate** only when one is true: it cannot be undone (data deleted, money spent, a message, post or email sent, a secret exposed or rotated, a production change users see), it is outside your role, or it would undo another agent's live work.
3. **Always act on:** re-running or re-queuing your own CI, rebasing or updating your branch, fixing your own PR, closing or reopening your cards, clearing a blocker that is proven stale, re-arming your monitor, a small config change in your area with a known rollback, reassigning within policy.
4. **Escalate to:** the CEO for decisions, production go-live and public posts in the owner's name; the Operator for host, root, secret-store and network actions. Reversible infra work (canaries, pool/runner changes, timers, image switches with a rollback) is the Operator's to just do, with no CEO approval (owner 2026-10-06). The CISO handles security-boundary changes; the owner handles only acts proven human-only by a failed attempt. There is no money for new spend (owner 2026-10-06). One escalation, with evidence and the exact action wanted. Never a chain.
5. **Before acting:** check that the premise is still true and that no peer owns it live. **After acting:** post one line `ACT: what / WHY: reason / UNDO: rollback`. If it fails, undo once, then escalate. Never silently redo or undo a peer.
6. **Never, even if reversible:** disabling a security gate, paid spend, deleting data or history, force-pushing a default branch. **Key or credential rotation** is allowed ONLY when agents can do it fully end to end: create the new key, deploy it everywhere, no downtime, no human step. If any human step is needed, write it into the CEO's owner digest and continue other work (owner 2026-10-06).
7. **Stuck runs:** a run that is verified dead (no live process AND no output for 2 hours or more), or a retry loop failing the same way 3 or more times, may be cancelled or parked with the evidence logged (owner 2026-10-06). Silence alone is not a reason.

### Every repo needs Paperclip Review 5/5 to merge (owner, 2026-10-06)
- An org rule now requires the `Paperclip Review` check (5/5) on every repo's default branch. Fix every finding until it scores 5/5 on the final head, the same as upstream Greptile.
- PRs opened by bots (dependabot, release-please) are not reviewed automatically yet. The agent shepherding one comments `@togetherweown-reviewer review this PR` on it, which runs the review as the owner.
<!-- {TAG}:END -->"""


def main():
    apply = '--apply' in sys.argv
    bk = F.ROOT / f"ops/productivity/bundles-before-act-reversible-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    changed = []
    for agent in F.live_agents():
        p = F.BASE / agent / 'instructions' / 'AGENTS.md'
        old = F.read(p)
        if old is None:
            continue
        if f'{TAG}:START' in old:
            a = old.index(f'<!-- {TAG}:START -->'); b = old.index(f'<!-- {TAG}:END -->') + len(f'<!-- {TAG}:END -->')
            new = old[:a] + BLOCK + old[b:]
            if new == old:
                continue
        else:
            new = old.rstrip('\n') + '\n\n' + BLOCK + '\n'
        changed.append(agent[:8])
        if apply:
            bk.mkdir(parents=True, exist_ok=True)
            (bk / f'{agent}.AGENTS.md').write_text(old)
            code = F.put(agent, 'AGENTS.md', new)
            if code != '200':
                print('PUT failed', agent, code)
    print(('applied ' if apply else 'would change ') + str(len(changed)) + (f'; backup {bk}' if apply and changed else ''))


if __name__ == '__main__':
    main()
