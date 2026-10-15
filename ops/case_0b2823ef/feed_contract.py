#!/usr/bin/env python3
"""CLIProxy lane-feed contract: exact servedModels + durable exclusions (case_0b2823ef).

Source-only prerequisite for host cards case_b9ae3ea2 (served IDs in the lane
feed) and case_ad9f6568 (durable zen-free exclusion). This module is the reviewed,
unit-tested home of the two narrow behaviors the Operator installs into the
host producer ``live_accounts.py`` (host SHA256
``d8cab34855fee1507b8c72eae8c05ae76cb6da1e03250a650c1acdf29e2baa2d``):

1. ``servedModels``: top-level ``string[]`` of the EXACT CLIProxy
   ``GET /v1/models`` ``data[].id`` values on each feed refresh. No prefix
   stripping, no derivation from per-account ``models``, no frozen snapshot.
   A missing/failed/empty/malformed oracle yields ``None`` and the key is
   OMITTED (fail-open: the model-capacity consumer treats absence as "no
   authoritative list"). Stale IDs are never re-served as current, and a
   fresh authoritative empty list is never fabricated.
2. Durable exclusions: an entry ``{"durable": true, ...}`` excludes forever.
   Every other shape keeps the current expiring-``until`` semantics, so all
   existing entries and all unrelated provider/model exclusions behave
   exactly as before. Invalid input (non-dict entry, non-boolean ``durable``,
   unparseable ``until``) never means forever.

This module performs no I/O at import and needs no credentials: the fetch
transport is injected, so tests use synthetic fixtures and the host patch
supplies the real loopback opener. Never import the host producer here:
its module-level ``load_key()`` opens a host-only file.
"""

import datetime as _dt
import json as _json
import re
import time as _time
import urllib.request as _urllib_request

# Default oracle location: same host+port the producer already uses for the
# management API, OpenAI-compatible path. Overridable on the host via the
# CLIPROXY_SERVED_MODELS_URL environment variable (see RUNBOOK.md).
SERVED_MODELS_URL_DEFAULT = "http://127.0.0.1:8317/v1/models"
SERVED_MODELS_TIMEOUT = 15

# Exclusion-map key for a permanent entry. Boolean True ONLY; any other
# truthy value ("true", 1, [...]) falls through to until-semantics so that
# invalid input can never silently mean forever.
DURABLE_FLAG = "durable"


class ServedModelsError(Exception):
    """The oracle did not yield a usable authoritative list."""


def served_bearer(key_file="", fallback_key=""):
    """Bearer for the /v1/models oracle: existing KEY unless overridden.

    Returns ``fallback_key`` when no key file is configured; otherwise the
    first usable line (``KEY=value`` or bare). Raises
    :class:`ServedModelsError` when the configured file yields no usable
    line, so callers' ``except Exception`` omits ``servedModels`` instead
    of failing the feed.
    """
    if key_file:
        with open(key_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") if "=" in line else line
        raise ServedModelsError("no bearer in CLIPROXY_SERVED_MODELS_KEY_FILE")
    return fallback_key


def served_iso(ts):
    """Normalize an ISO-8601 or epoch timestamp to ``...Z``; None if unparseable.

    Mirrors the host producer's ``iso()`` so exclusion ``until`` values parse
    identically here and on the host.
    """
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)) or (
            isinstance(ts, str) and re.fullmatch(r"\d+(\.\d+)?", ts)
        ):
            return (
                _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        return (
            _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            .astimezone(_dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:
        return None


def extract_served_ids(payload):
    """Exact ``data[].id`` strings from a parsed ``GET /v1/models`` body.

    Raises :class:`ServedModelsError` on anything that is not a usable
    authoritative list: non-dict payload, missing/non-list ``data``, or zero
    usable ids (empty oracle). Order is preserved, first-seen wins on
    duplicates, and values are kept VERBATIM: provider prefixes are never
    stripped and nothing is canonicalized. Non-string or blank entries are
    skipped as malformed oracle rows, not as model policy.
    """
    if not isinstance(payload, dict):
        raise ServedModelsError("oracle payload is not an object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ServedModelsError("oracle payload has no data[] list")
    ids = []
    for row in data:
        mid = row.get("id") if isinstance(row, dict) else row
        if isinstance(mid, str) and mid:
            if mid not in ids:
                ids.append(mid)
    if not ids:
        raise ServedModelsError("oracle served zero ids")
    return ids


def fetch_served_models(url, key, opener=None, timeout=SERVED_MODELS_TIMEOUT):
    """GET the oracle and return exact ``data[].id`` values.

    Uses the existing approved runtime auth pattern (``Authorization: Bearer``
    with the host management key) over loopback only; no new credential, no
    new network scope. Any transport failure, non-200 status, non-JSON body,
    or unusable list raises :class:`ServedModelsError` -- callers omit the
    key, never fabricate ``[]`` or retain a previous list.
    """
    open_url = opener or _urllib_request.urlopen
    req = _urllib_request.Request(
        url,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="GET",
    )
    try:
        with open_url(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            raw = resp.read().decode()
    except ServedModelsError:
        raise
    except Exception as exc:
        raise ServedModelsError("oracle unreachable: %s" % (exc,)) from exc
    if status != 200:
        raise ServedModelsError("oracle status %d" % (status,))
    try:
        payload = _json.loads(raw)
    except Exception as exc:
        raise ServedModelsError("oracle body is not JSON") from exc
    return extract_served_ids(payload)


def attach_served_models(doc, ids):
    """Attach (or deliberately omit) ``servedModels`` on the feed document.

    ``ids`` must be the fresh oracle result, or ``None`` when the oracle
    failed. A non-empty list is published verbatim; anything else REMOVES the
    key so a failed refresh never fabricates an authoritative empty list and
    never retains stale IDs as current. Returns ``doc``.
    """
    if isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids):
        doc["servedModels"] = list(ids)
    else:
        doc.pop("servedModels", None)
    return doc


def exclusion_is_active(entry, now_ts=None):
    """True when an exclusion-map entry excludes its target right now.

    Contract (case_ad9f6568):

    * ``entry`` is not a dict -> False (invalid input never excludes).
    * ``entry["durable"] is True`` (boolean identity) -> True, permanently.
      Any other value for ``durable`` -- ``"true"``, ``1``, ``[...]`` --
      is INVALID and falls through to ``until`` semantics; it never means
      forever.
    * Otherwise the entry excludes only with a parseable ``until`` in the
      future -- exactly the current host behavior, preserved for every
      existing expiring entry.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get(DURABLE_FLAG) is True:
        return True
    until = served_iso(entry.get("until"))
    if not until:
        return False
    try:
        return (
            _dt.datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp()
            > (now_ts if now_ts is not None else _time.time())
        )
    except Exception:
        return False


def active_dropped_models(excl, now_ts=None):
    """Model ids dropped from every account's list by active ``model:`` entries."""
    if not isinstance(excl, dict):
        return set()
    now = now_ts if now_ts is not None else _time.time()
    return {
        k[6:]
        for k, e in excl.items()
        if k.startswith("model:") and exclusion_is_active(e, now)
    }


def apply_exclusions(accounts, excl, now_ts=None):
    """Apply the exclusion map to feed accounts; mirrors the host producer.

    Model-level entries drop the id (suffix-matched, as on the host) from
    every account's ``models`` list; account/provider entries mark matching
    accounts ``unavailable`` with their ``excludedReason`` (200 chars, as on
    the host). Non-matching accounts and unrelated entries pass through
    untouched. Mutates and returns ``accounts``.
    """
    if not isinstance(excl, dict):
        return accounts
    now = now_ts if now_ts is not None else _time.time()
    dropped = active_dropped_models(excl, now)
    if dropped:
        for acc in accounts:
            if isinstance(acc.get("models"), list):
                acc["models"] = [
                    m for m in acc["models"] if m.rsplit("/", 1)[-1] not in dropped
                ]
    for acc in accounts:
        for key in (acc.get("accountKey"), acc.get("provider")):
            e = excl.get(key) if key else None
            if exclusion_is_active(e, now):
                acc["health"] = "unavailable"
                acc["excludedReason"] = str(e.get("reason", ""))[:200]
                break
    return accounts
