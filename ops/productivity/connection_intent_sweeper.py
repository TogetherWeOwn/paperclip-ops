#!/usr/bin/env python3
"""Decline bogus "Connect Anthropic" connection intents (human_only) that strand cards on the owner.

tog.5 classifies some run failures as `claude_auth_required` even when the real error is a provider stream drop
("stream disconnected before completion", e.g. gpt-6-luna(max) through CLIProxy) or a Claude 429 storm, and then
opens a human_only `connection_intent` for service "anthropic". Agents never need a direct Anthropic connection:
every model is reached through CLIProxy (http://cliproxy:8317) and the owner does not connect provider accounts per
card, so such an intent only idles the card. 2026-10-07 06:20Z: 13 pending (case_f4498080 production path among them).

Declines ONLY with positive per-intent evidence: the card's latest failed run within EVIDENCE_WINDOW before the intent
has error_code claude_auth_required AND an error text matching the known non-auth signatures (stream disconnect,
429/rate limit, overloaded). Anything else (e.g. a genuine "Not logged in"/401) is logged and left untouched.
Declines pending intents with serviceSlug in SERVICES older than MIN_AGE_MIN via the supported
POST /api/connection-intents/:id/decline. Dry-run by default; --apply declines. Rollback: the agent may re-request.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = 'http://127.0.0.1:3100/api'
CID = '00000000-0000-4000-8000-000000000004'
LOG = ROOT / 'ops/worker-vps/operator-actions.log'
SERVICES = ('anthropic',)
MIN_AGE_MIN = 2
EVIDENCE_WINDOW_MIN = 30
SIGNATURES = ("stream disconnected", "stream error", "stream closed", "429", "rate limit", "rate_limit",
              "overloaded", "usage limit")
APPLY = '--apply' in sys.argv
PSQL_TIMEOUT = 60
CURL_TIMEOUT = 60
REASON = ("Host operator: no direct Anthropic connection is needed. Agents reach every model through CLIProxy "
          "(http://cliproxy:8317), and the owner does not connect provider accounts per card. This request came from "
          "a run failure that Paperclip labelled claude_auth_required; the real cause was a provider stream "
          "disconnect or rate limit. Continue the card normally; the next run retries on the fleet model.")


def psql(sql):
    out = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip',
                          '-AtF', '\t', '-c', sql], capture_output=True, text=True, check=True,
                         timeout=PSQL_TIMEOUT).stdout
    return [l.split('\t') for l in out.splitlines() if l]


def main():
    slugs = ','.join(f"'{s}'" for s in SERVICES)
    try:
        rows = psql(f"""select t.id, i.identifier, t.payload->>'requestingAgentName',
        coalesce((select lower(r.error_code || ' ' || coalesce(r.error,'')) from heartbeat_runs r
                  where r.context_snapshot->>'issueId'=i.id::text and r.status='failed'
                  and r.created_at between t.created_at - interval '{EVIDENCE_WINDOW_MIN} minutes' and t.created_at
                  order by r.created_at desc limit 1), '') from issue_thread_interactions t
        join issues i on i.id=t.issue_id where i.company_id='{CID}' and t.kind='connection_intent'
        and t.status='pending' and t.payload->>'serviceSlug' in ({slugs})
        and t.created_at < now() - interval '{MIN_AGE_MIN} minutes' order by t.created_at""")
    except subprocess.TimeoutExpired:
        print(f'connection-intent-sweeper: FAILED: db query timed out after {PSQL_TIMEOUT}s '
              f'(unmeasured; nothing declined)', file=sys.stderr)
        sys.exit(1)
    done, failed = [], []
    for iid, ident, agent, err in rows:
        if not (err.startswith('claude_auth_required') and any(sig in err for sig in SIGNATURES)):
            print(f'left {iid} on {ident} ({agent}): no known misclassification evidence ({err[:80]!r})')
            continue
        if not APPLY:
            print(f'would decline {iid} on {ident} ({agent})')
            continue
        try:
            r = subprocess.run(['curl', '-sK', str(ROOT / '.pc.curl'), '-X', 'POST',
                                f'{API}/connection-intents/{iid}/decline', '-H', 'content-type: application/json',
                                '-d', json.dumps({'reason': REASON}), '-o', '/dev/null', '-w', '%{http_code}'],
                               capture_output=True, text=True, timeout=CURL_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f'FAILED decline {iid} on {ident} ({agent}): timed out after {CURL_TIMEOUT}s '
                  f'(unmeasured; not declined)')
            failed.append(ident)
            continue
        done.append(f'{ident}:{r.stdout.strip()}')
        print(f'declined {iid} on {ident} ({agent}) HTTP {r.stdout.strip()}')
    if done:
        with LOG.open('a') as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} connection-intent-sweeper: declined "
                    f"bogus Connect-Anthropic intents {done}\n")
    if failed:
        print(f'connection-intent-sweeper: FAILED: {len(failed)} decline(s) timed out ({", ".join(failed)})',
              file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
