#!/usr/bin/env python3
"""Two-web promoter live-identity helper (public sanitized port).

Derives "live production" from the actual Cloudflare deployment -- the active
Worker version id mapped to its build commit -- and refuses to guess when that
mapping is missing. Never consults release metadata or tags.

Cloudflare contract (public docs, offline fixtures here):
  1. GET /accounts/{account_id}/workers/scripts/{script_name}/deployments:
     the first deployment serves live traffic; read its versions list with
     version ids and traffic percentages.
  2. GET /accounts/{account_id}/builds/builds?version_ids={version_id}:
     result.builds maps a version id to a build record carrying a commit hash.

Fail-closed rules:
  - no deployments, or a first deployment with no versions -> refuse.
  - any version entry that is not an object with a version_id -> refuse
    (never silently drop an unidentified split).
  - any non-numeric, non-finite (NaN/inf) or out-of-range traffic
    percentage, or percentages that do not total 100 when all are
    known -> refuse.
  - more than one version sharing live traffic -> refuse (no silent pick).
    Versions at 0% are staged, not serving, and do not block promotion.
  - live version with no known build, a build with no commit, or non-object
    build metadata -> refuse.

Standalone port: this helper is not wired into any deploy script and
performs no deploy. The rollback target is the live commit hash itself,
to be redeployed through the two-web Cloudflare pipeline
(deployments API / dashboard); it is never replayed via
scripts/dispatch_deploy.sh, which manages only the dispatch-src
in-place checkout from a different repository.

Offline: stdlib only, no network, no credentials, no subprocess. Callers pass
already-fetched JSON payloads (dicts) so unit tests and the CLI stay hermetic.
Real account ids, tokens and host names live outside this repo and are injected
at runtime; fixtures below use synthetic identifiers only.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


class LiveIdentityError(Exception):
    """Raised when the live production identity cannot be established."""


def _active_versions(deployments_payload: dict) -> list[dict]:
    """Return the versions list of the first (live-traffic) deployment."""
    try:
        result = deployments_payload.get("result", {})
        deployments = result.get("deployments", [])
    except AttributeError as exc:
        raise LiveIdentityError(f"deployments payload is not an object: {exc}") from exc
    if not isinstance(deployments, list) or not deployments:
        raise LiveIdentityError("no deployments: cannot determine live production version")
    first = deployments[0]
    if not isinstance(first, dict):
        raise LiveIdentityError("first deployment is not an object: refusing to guess live version")
    versions = first.get("versions", [])
    if not isinstance(versions, list) or not versions:
        raise LiveIdentityError("live deployment carries no versions: refusing to guess live version")
    return versions


def _builds_by_version(builds_payload: dict) -> dict:
    """Normalize result.builds (object or list) to {version_id: build record}."""
    try:
        result = builds_payload.get("result", {})
        builds = result.get("builds", {})
    except AttributeError as exc:
        raise LiveIdentityError(f"builds payload is not an object: {exc}") from exc
    if isinstance(builds, dict):
        return builds
    if isinstance(builds, list):
        normalized: dict = {}
        for entry in builds:
            vid = entry.get("version_id") if isinstance(entry, dict) else None
            if isinstance(entry, dict) and isinstance(vid, str) and vid:
                normalized[vid] = entry
        return normalized
    raise LiveIdentityError("builds payload has no usable result.builds mapping")


def _validate_versions(versions: list) -> list[tuple[str, float | None]]:
    """Validate every version entry; refuse closed on malformed input.

    Returns [(version_id, percentage)] where percentage is None (unknown
    share, treated as serving) or a number. Missing percentage defaults
    to 100 to preserve single-version fixtures without traffic data.
    """
    validated: list[tuple[str, float | None]] = []
    for idx, entry in enumerate(versions):
        if not isinstance(entry, dict):
            raise LiveIdentityError(
                f"live deployment entry #{idx} is not an object: refusing to guess live version"
            )
        version_id = entry.get("version_id")
        if not isinstance(version_id, str) or not version_id:
            raise LiveIdentityError(
                f"live deployment entry #{idx} has no version_id: refusing to guess live version"
            )
        pct = entry.get("percentage", 100)
        if pct is None:
            validated.append((version_id, None))
        elif isinstance(pct, bool) or not isinstance(pct, (int, float)):
            raise LiveIdentityError(
                f"live version {version_id} has non-numeric percentage {pct!r}: "
                "refusing to guess live version"
            )
        elif not math.isfinite(pct) or not 0 <= pct <= 100:
            raise LiveIdentityError(
                f"live version {version_id} has out-of-range percentage {pct!r}: "
                "refusing to guess live version"
            )
        else:
            validated.append((version_id, pct))
    return validated


def _commit_of(build: dict, version_id: str) -> tuple[str, str]:
    """Return (commit_hash, build_uuid) or raise with a clear reason."""
    if not isinstance(build, dict):
        raise LiveIdentityError(f"live version {version_id} has no known build: refusing to promote")
    build_uuid = build.get("build_uuid", "") or build.get("id", "") or ""
    if not isinstance(build_uuid, str):
        raise LiveIdentityError(
            f"live version {version_id} has non-string build id: refusing to promote"
        )
    metadata = build.get("build_trigger_metadata", {}) or {}
    if not isinstance(metadata, dict):
        raise LiveIdentityError(
            f"live version {version_id} has non-object build metadata: refusing to promote"
        )
    commit = (
        metadata.get("commit_hash", "")
        or build.get("commit_hash", "")
        or build.get("commit", "")
        or ""
    )
    if not isinstance(commit, str) or not commit:
        raise LiveIdentityError(
            f"live version {version_id} maps to a build with no commit hash: refusing to promote"
        )
    return commit, build_uuid


def derive_live_prod_identity(deployments_payload: dict, builds_payload: dict) -> dict:
    """Derive the live production identity from Cloudflare state.

    Returns {"version_id": ..., "commit": ..., "build_uuid": ...}.
    Raises LiveIdentityError on any ambiguity or unknown mapping.
    """
    versions = _active_versions(deployments_payload)
    validated = _validate_versions(versions)
    if not validated:
        raise LiveIdentityError("live deployment has no version id: refusing to guess live version")
    serving = [(vid, pct) for vid, pct in validated if pct is None or pct > 0]
    if not serving:
        raise LiveIdentityError("live deployment has no serving version: refusing to guess live version")
    if all(pct is not None for _, pct in validated):
        total = sum(pct for _, pct in validated)  # type: ignore[misc]
        if total != 100:
            raise LiveIdentityError(
                f"traffic percentages total {total}, expected 100: refusing to guess live version"
            )
    distinct_serving = sorted(set(vid for vid, _ in serving))
    if len(distinct_serving) != 1 or len(serving) != 1:
        raise LiveIdentityError(
            f"mixed-version deployment serving {sorted(set(vid for vid, _ in validated))}: "
            "refusing to pick one as live"
        )
    version_id = distinct_serving[0]
    builds = _builds_by_version(builds_payload)
    if version_id not in builds:
        raise LiveIdentityError(
            f"live version {version_id} has no known build mapping: refusing to promote"
        )
    commit, build_uuid = _commit_of(builds[version_id], version_id)
    return {"version_id": version_id, "commit": commit, "build_uuid": build_uuid}


def decide_promotion(live_commit: str, candidate_commit: str) -> str:
    """Decide the promotion outcome from the Cloudflare-derived live commit."""
    if not isinstance(live_commit, str) or not isinstance(candidate_commit, str):
        raise LiveIdentityError("live and candidate commits are both required")
    live = live_commit.strip().lower()
    candidate = candidate_commit.strip().lower()
    if not live or not candidate:
        raise LiveIdentityError("live and candidate commits are both required")
    if live == candidate:
        return "already-live"
    return "promote"


def rollback_target(live_commit: str) -> str:
    """Return the rollback target for the live identity (never a tag).

    The target is the live commit hash itself, to be redeployed through
    the two-web Cloudflare pipeline. This helper emits no deploy argv:
    scripts/dispatch_deploy.sh manages only the dispatch-src checkout
    from a different repository and can never replay a two-web Worker
    build commit.
    """
    if not isinstance(live_commit, str) or not live_commit:
        raise LiveIdentityError("live commit is required for a rollback target")
    return live_commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive live production identity from Cloudflare deployment + builds JSON."
    )
    parser.add_argument("--deployments", required=True, help="Path to deployments JSON file")
    parser.add_argument("--builds", required=True, help="Path to builds JSON file")
    parser.add_argument("--candidate", default=None, help="Candidate commit to compare against live")
    args = parser.parse_args(argv)

    try:
        deployments = json.loads(Path(args.deployments).read_text())
        builds = json.loads(Path(args.builds).read_text())
        identity = derive_live_prod_identity(deployments, builds)
    except LiveIdentityError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"refused: unreadable input ({exc})", file=sys.stderr)
        return 2

    print(json.dumps(identity, indent=2, sort_keys=True))
    if args.candidate is not None:
        try:
            print(decide_promotion(identity["commit"], args.candidate))
        except (LiveIdentityError, TypeError, AttributeError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
