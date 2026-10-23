#!/usr/bin/env python3
"""Propose benign agent-error resets for the single recovery writer.

Reads live board state, prints JSONL. No writes, no credentials on argv.
One benign error agent yields one ``reset_agent_error`` proposal; every
other error agent yields a ``finding`` row routing to its owning lead.
Quiet (no error agents) prints nothing but still exits 0 -- the driver's
empty-output refusal is the contract working, not a failure here.

The writer re-verifies every conjunct against fresh reads before touching
anything, so this script is advisory: a bug here can only cause refusals
or missed resets, never a wrong reset. The benign check below must match
``recovery_writer.benign_reset_evidence`` exactly; the cross-agreement
test pins shared fixtures against both.

Benign signature (exit-143 false-failure): the agent's LATEST failed run
has result subtype ``success`` plus
``unmanagedBackgroundTask.terminalResultSeen=true``, and finished more
than 10 minutes ago. Only the latest failed run counts: resetting on an
older benign run while the newest failure is real would clear evidence
the owning lead needs.

Trust note: this script is NOT in the host bundle (bundle members are
pinned by the deployment contract). It runs as the operator-configured
detector command. That is safe because its output is untrusted input --
the writer re-reads the live agent and the exact run and refuses on
anything stale or non-benign.

  PAPERCLIP_API_URL=... PAPERCLIP_API_KEY=... PAPERCLIP_COMPANY_ID=... \\
    python3 agent_error_proposer.py > proposals.jsonl
  python3 recovery_writer.py --proposals proposals.jsonl   # dry-run
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from paperclip_api_allowlist import (
    AllowlistRefused, check_api_base_allowed)

AGENT_ERROR_MIN = 10
BENIGN_SUBTYPE = "success"


def parse_time(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment
    except ValueError:
        return None


def run_is_benign(run: dict[str, Any]) -> tuple[bool, str]:
    """Mirror of recovery_writer.benign_reset_evidence on a live run record.

    Must match the writer exactly: subtype success plus
    unmanagedBackgroundTask.terminalResultSeen is True. Strict identity --
    "true"/1 must not pass. Anything else is not benign.
    """
    result = run.get("resultJson")
    if not isinstance(result, dict):
        return False, "run has no resultJson; routing to the owning lead"
    background = result.get("unmanagedBackgroundTask")
    background = background if isinstance(background, dict) else {}
    seen = background.get("terminalResultSeen")
    if result.get("subtype") != BENIGN_SUBTYPE or seen is not True:
        return False, (
            f"run is not the benign exit-143 signature "
            f"(subtype={result.get('subtype')!r}, "
            f"terminalResultSeen={seen!r}); routing to the owning lead"
        )
    return True, ""


def propose(
    now: datetime.datetime,
    agents: list[dict[str, Any]],
    runs_by_agent: dict[str, list[dict[str, Any]]],
    age_min: int = AGENT_ERROR_MIN,
) -> list[dict[str, Any]]:
    """Build proposal/finding rows for error-stuck agents. Pure; testable."""
    records: list[dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, dict) or agent.get("status") != "error":
            continue
        agent_id = agent.get("id")
        if not agent_id:
            continue
        lead = agent.get("reportsTo") or "owning-lead"
        failed = [
            run for run in runs_by_agent.get(str(agent_id), [])
            if isinstance(run, dict) and run.get("status") == "failed"
        ]
        if not failed:
            continue
        latest = max(
            failed,
            key=lambda run: str(run.get("finishedAt") or run.get("createdAt") or ""),
        )
        run_id = latest.get("id")
        if not run_id:
            continue
        benign, cause = run_is_benign(latest)
        if not benign:
            records.append({
                "type": "finding",
                "detector": "watchdog/agent_error",
                "reason": "non-benign agent error",
                "severity": "high",
                "owner": lead,
                "route": "owning-lead",
                "evidence": {"agentId": agent_id, "runId": run_id,
                             "finishedAt": latest.get("finishedAt")},
                "suggestedAction": f"{cause}. Owning lead triages; "
                                   f"no auto-reset outside the benign signature.",
            })
            continue
        finished = parse_time(latest.get("finishedAt"))
        if finished is None:
            continue
        age = (now - finished).total_seconds() / 60
        if age <= age_min:
            continue
        records.append({
            "type": "proposal",
            "detector": "watchdog/agent_error",
            "reason": "benign_agent_error",
            "mutation": "reset_agent_error",
            "identifier": str(agent.get("name") or agent_id),
            "agentId": agent_id,
            "runId": run_id,
            "finishedAt": latest.get("finishedAt"),
            "result": {
                "subtype": BENIGN_SUBTYPE,
                "unmanagedBackgroundTask": {"terminalResultSeen": True},
            },
            "before": {"agentId": agent_id, "runId": run_id,
                       "status": "error",
                       "finishedAt": latest.get("finishedAt")},
            "after": {"agentId": agent_id, "status": "idle"},
            "note": "Benign exit-143 false-failure past "
                    f"{age_min}m; auto-reset to idle.",
        })
    return records


def _valid_api_base(base):
    # BASE host validation: the bearer below is sent ONLY to this base.
    # Refuse a base that is not an http(s) URL with a non-empty bare host:
    # no userinfo (`user@host` misdirects the credential), no control
    # characters or DEL -- raw anywhere, or %-encoded in the authority
    # (urllib decodes those before the transport, which then raises outside
    # the module's error contract) -- no query or fragment (the appended
    # /api path would be swallowed into them), a real hostname (an
    # empty `:port`-only authority is not a host), a numeric port, and no
    # `{`/`}` anywhere (curl expands `{a,b}` globs, so one base is one host
    # -- parity with the shell gate). A well-formed hostile host still
    # passes; blocking that needs an operator-owned host allowlist, not a
    # per-script guess.
    if not base.startswith(("http://", "https://")):
        return False
    if "@" in base:
        return False
    if any(ord(char) < 0x21 or ord(char) == 0x7f for char in base):
        return False
    if "{" in base or "}" in base:
        return False
    try:
        parts = urllib.parse.urlsplit(base)
    except ValueError:
        return False
    if parts.username or parts.password:
        return False
    if parts.query or parts.fragment:
        return False
    decoded_netloc = urllib.parse.unquote(parts.netloc)
    if any(ord(char) < 0x21 or ord(char) == 0x7f for char in decoded_netloc):
        return False
    host = parts.hostname or ""
    if not host:
        return False
    if any(char in "[]{}," for char in host):
        return False
    if not parts.netloc.startswith("[") and parts.netloc.count(":") > 1:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None and parts.netloc.endswith(":"):
        return False
    return True


def api_get(base_url: str, api_key: str, path: str) -> Any:
    if not _valid_api_base(base_url):
        raise RuntimeError(
            "refusing: PAPERCLIP_API_URL is not a clean http(s) base")
    try:
        check_api_base_allowed(base_url, sender="agent_error_proposer")
    except AllowlistRefused as exc:
        raise RuntimeError(str(exc)) from exc
    request = urllib.request.Request(
        base_url + path,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors="replace")
        raise RuntimeError(f"GET {path}: HTTP {error.code}: {raw[:300]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GET {path}: {error.reason}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id",
                        default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-url", default=os.environ.get("PAPERCLIP_API_URL"))
    parser.add_argument("--age-min", type=int, default=AGENT_ERROR_MIN)
    # NOTE: no --api-key flag on purpose. Credentials travel by inherited
    # environment, never on argv: /proc cmdline is world-readable.
    args = parser.parse_args(argv)
    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not (args.api_url and api_key and args.company_id):
        parser.error("need --api-url/--company-id and PAPERCLIP_API_KEY env")
    if args.age_min < 1:
        parser.error("--age-min must be positive")
    base = args.api_url.rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    agents = api_get(base, api_key, f"/api/companies/{args.company_id}/agents")
    agents = agents if isinstance(agents, list) else []
    runs_by_agent: dict[str, list[dict[str, Any]]] = {}
    for agent in agents:
        if not isinstance(agent, dict) or agent.get("status") != "error":
            continue
        agent_id = agent.get("id")
        if not agent_id:
            continue
        runs = api_get(base, api_key,
                       f"/api/companies/{args.company_id}/heartbeat-runs"
                       f"?agentId={agent_id}&limit=5")
        runs_by_agent[str(agent_id)] = (
            runs if isinstance(runs, list) else [])
    now = datetime.datetime.now(datetime.timezone.utc)
    for record in propose(now, agents, runs_by_agent, args.age_min):
        print(json.dumps(record, sort_keys=True))
    print(f"proposer: {len(agents)} agents read, "
          f"{sum(1 for a in agents if isinstance(a, dict) and a.get('status') == 'error')} "
          f"in error", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"agent_error_proposer.py: {error}", file=sys.stderr)
        raise SystemExit(2)
