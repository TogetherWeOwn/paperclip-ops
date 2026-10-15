#!/usr/bin/env python3
"""Legacy controller for the CI_nodecf677d86_RUNNER variable (case_0bf02bac).

The owner has two on-demand self-hosted runners (`node93f4bccf-r1`, `node93f4bccf-r2`,
label `two-node93f4bccf`). The current ci.yml has no home-PC consumer: the generic
mutation matrix moved to toolkit CI and the remaining private mutation job is
hard-pinned to the isolated pool. Keep this controller dormant unless a future
workflow change adds an approved home-PC consumer and its operator/CISO gates.

Modes:
  check  read-only: report observed runners, desired state and current
         variable. Exit 0 aligned, 1 drift, 2 usage/config, 3 unmeasured.
  apply  reconcile: create/update the variable when an idle home-PC runner
         exists, delete it when none does. On observation failure, attempt
         to delete stale routing and exit 3 even if deletion succeeds.
         A failed delete is uncontained, never reported as cleared.

The credential travels by inherited environment only (`GH_TOKEN`), never on
argv and never into logs. No host, no systemd, no Paperclip: pure stdlib +
the GitHub REST API, so the offline suite drives it through a stub transport.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ORG_DEFAULT = "TogetherWeOwn"
REPO_DEFAULT = "TogetherWeOwn/paperclip-ops"
LABEL_DEFAULT = "two-node93f4bccf"
VAR_DEFAULT = "CI_nodecf677d86_RUNNER"
VALUE_DEFAULT = '["self-hosted","two-node93f4bccf"]'
API_DEFAULT = "https://api.github.com"
REQUEST_TIMEOUT = 10
OBSERVATION_BUDGET = 30


class GuardError(RuntimeError):
    """A refused or unmeasurable step. The message is already log-safe."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token, api_url):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.observation_deadline = time.monotonic() + OBSERVATION_BUDGET

    def request(self, method, path, body=None):
        timeout = REQUEST_TIMEOUT
        if method == "GET":
            timeout = min(timeout, self.observation_deadline - time.monotonic())
            if timeout <= 0:
                raise GuardError("observation budget exhausted")
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "paperclip-ops/ci-node93f4bccf-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.api_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                return resp.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            raise GuardError(f"github HTTP {exc.code} on {method} {path}", status=exc.code) from exc
        except (OSError, http.client.HTTPException) as exc:
            # HTTPException (IncompleteRead, BadStatusLine) is not an OSError;
            # a body cut short mid-read must still count as an unmeasurable step.
            raise GuardError(f"github request failed on {method} {path}: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise GuardError(f"github response was not JSON on {method} {path}") from exc


def list_runners(gh, org):
    runners = []
    page = 1
    while True:
        status, payload = gh.request(
            "GET", f"/orgs/{org}/actions/runners?per_page=100&page={page}"
        )
        if status != 200 or not isinstance(payload, dict):
            raise GuardError(f"runner list returned HTTP {status}")
        batch = payload.get("runners")
        if not isinstance(batch, list):
            raise GuardError("runner list response lacks runners array")
        runners.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return runners


def select_node93f4bccf(runners, label):
    matching = []
    for r in runners:
        if not isinstance(r, dict) or not isinstance(r.get("labels"), list):
            raise GuardError("runner entry lacks labels array")
        if any(not isinstance(l, dict) or not isinstance(l.get("name"), str)
               for l in r["labels"]):
            raise GuardError("runner label entry lacks name")
        if label not in [l["name"] for l in r["labels"]]:
            continue
        if (r.get("status") not in ("online", "offline")
                or not isinstance(r.get("busy"), bool)
                or not isinstance(r.get("name"), str)):
            raise GuardError("home-PC runner entry lacks valid name/status/busy")
        matching.append(r)
    online = [r for r in matching if r["status"] == "online"]
    idle = [r for r in online if r["busy"] is False]
    return matching, online, idle


def read_variable(gh, repo, name):
    try:
        status, payload = gh.request(
            "GET", f"/repos/{repo}/actions/variables/{urllib.parse.quote(name, safe='')}"
        )
    except GuardError as exc:
        if exc.status == 404:
            return None
        raise
    if status != 200 or not isinstance(payload, dict):
        raise GuardError(f"variable read returned HTTP {status}")
    if not isinstance(payload.get("value"), str):
        raise GuardError("variable response lacks value string")
    return payload["value"]


def write_variable(gh, repo, name, value, exists):
    path = f"/repos/{repo}/actions/variables/{urllib.parse.quote(name, safe='')}"
    if value is None:
        if not exists:
            return "already-clear"
        try:
            status, _ = gh.request("DELETE", path)
        except GuardError as exc:
            if exc.status == 404:
                return "already-clear"
            raise
        if status != 204:
            raise GuardError(f"variable delete returned HTTP {status}")
        return "cleared"
    if not exists:
        status, _ = gh.request("POST", f"/repos/{repo}/actions/variables",
                               {"name": name, "value": value})
        if status not in (200, 201):
            raise GuardError(f"variable create returned HTTP {status}")
        return "set"
    status, _ = gh.request("PATCH", path, {"name": name, "value": value})
    if status not in (200, 204):
        raise GuardError(f"variable update returned HTTP {status}")
    return "set"


def observe(gh, org, repo, label, var):
    runners = list_runners(gh, org)
    matching, online, idle = select_node93f4bccf(runners, label)
    current = read_variable(gh, repo, var)
    names = sorted(str(r.get("name", "?")) for r in idle)
    return {
        "matching": len(matching),
        "online": len(online),
        "idle": len(idle),
        "idle_names": names,
        "current": current,
    }


def _observation_expired(signum, frame):
    raise GuardError("observation budget exhausted")


def observe_bounded(gh, org, repo, label, var):
    """observe() under a hard wall-clock bound.

    The per-socket timeout does not bound a body that drips bytes, so a SIGALRM
    ends the whole observation at OBSERVATION_BUDGET and leaves the separate
    delete budget intact. Without SIGALRM (non-POSIX) the monotonic deadline in
    GitHub.request remains the only bound.
    """
    if not hasattr(signal, "setitimer"):
        return observe(gh, org, repo, label, var)
    previous = signal.signal(signal.SIGALRM, _observation_expired)
    signal.setitimer(signal.ITIMER_REAL, OBSERVATION_BUDGET)
    try:
        return observe(gh, org, repo, label, var)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def describe(exc):
    """Log-safe text: GuardError messages are already safe; anything else is
    reported by type only so a payload or credential never reaches the log."""
    return str(exc) if isinstance(exc, GuardError) else type(exc).__name__


def clear_route(gh, repo, var, context):
    """Attempt the unconditional DELETE. Never raises: a failed clear is
    UNCONTAINED (the route may still be set), not CLEARED."""
    try:
        outcome = write_variable(gh, repo, var, None, exists=True)
        print(f"{outcome.upper()} {var} {context}")
    except Exception as exc:  # noqa: BLE001 - containment must not end in a traceback
        print(f"UNCONTAINED: routing clear failed: {describe(exc)}", file=sys.stderr)


def parse_args(argv):
    ap = argparse.ArgumentParser(description="reconcile the legacy home-PC routing variable")
    ap.add_argument("--mode", choices=("check", "apply"), default="check")
    ap.add_argument("--org", default=os.environ.get("nodecf677d86_GH_ORG", ORG_DEFAULT))
    ap.add_argument("--repo", default=os.environ.get("nodecf677d86_GH_REPO", REPO_DEFAULT))
    ap.add_argument("--label", default=os.environ.get("nodecf677d86_LABEL", LABEL_DEFAULT))
    ap.add_argument("--var", default=os.environ.get("nodecf677d86_VAR", VAR_DEFAULT))
    ap.add_argument("--value", default=os.environ.get("nodecf677d86_VALUE", VALUE_DEFAULT))
    ap.add_argument("--api-url", default=os.environ.get("nodecf677d86_API_URL", API_DEFAULT))
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("REFUSED: GH_TOKEN is unset; refusing to read or write", file=sys.stderr)
        return 2
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        print("REFUSED: --value is not JSON", file=sys.stderr)
        return 2
    if not isinstance(value, list) or not value:
        print("REFUSED: --value must be a non-empty JSON list", file=sys.stderr)
        return 2
    gh = GitHub(token, args.api_url)
    try:
        obs = observe_bounded(gh, args.org, args.repo, args.label, args.var)
    except Exception as exc:  # noqa: BLE001 - any failure leaves state unknown
        print(f"REFUSED: {describe(exc)}", file=sys.stderr)
        if args.mode == "apply":
            # Current state is unknown: DELETE unconditionally, not via another
            # failing read. This never sets a route or substitutes credentials.
            clear_route(gh, args.repo, args.var, "after observation failure")
        return 3
    desired = args.value if obs["idle"] else None
    print(
        "node93f4bccf runners: matching={matching} online={online} idle={idle} "
        "idle_names={names} var={var} current={current!r} desired={desired!r}".format(
            matching=obs["matching"], online=obs["online"], idle=obs["idle"],
            names=",".join(obs["idle_names"]) or "-",
            var=args.var, current=obs["current"], desired=desired,
        )
    )
    if args.mode == "check":
        return 0 if obs["current"] == desired else 1
    try:
        if obs["current"] == desired:
            print(f"KEPT {args.var} unchanged")
            return 0
        outcome = write_variable(gh, args.repo, args.var, desired, obs["current"] is not None)
        print(f"{outcome.upper()} {args.var}")
        return 0
    except Exception as exc:  # noqa: BLE001 - never end a write failure in a traceback
        if desired is None:
            # The planned write was the clear: the route may still be set.
            print(f"UNCONTAINED: routing clear failed: {describe(exc)}", file=sys.stderr)
        else:
            print(f"REFUSED: {describe(exc)}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
