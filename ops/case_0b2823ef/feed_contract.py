#!/usr/bin/env python3
"""Synthetic helpers for the CLIProxy lane-feed carrier (case_0b2823ef).

The install patch in this directory carries only the reviewed ``servedModels``
behavior onto host ``live_accounts.py`` base SHA256
``93a99797b20726182f14938b394f58c4ec6b3adcc87fdfc7bcce787e465bf75e``. It does
not alter the host's exclusions or other feed behavior. In particular, any
`durable` exclusion semantics represented by the local helper functions below
are NOT installed by this patch; the current host continues its existing
`until` behavior. Those helpers remain covered by prior hermetic tests only.

``servedModels`` is a top-level ``string[]`` of exact CLIProxy
``GET /v1/models`` ``data[].id`` values on every feed build. The oracle's
transport is injected for tests; failures and empty/malformed lists omit the
field rather than inventing an authoritative empty list or reusing stale IDs.
Never import the live producer here: its module-level ``load_key()`` opens a
host-only file.
"""

import datetime as _dt
import json as _json
import re
import time as _time
import urllib.request as _urllib_request

# Default oracle location: same host+port the producer already uses for the
# management API, OpenAI-compatible path. Overridable via the environment.
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
                if not line or line.startswith("#"):
                    continue
                bearer = line.split("=", 1)[1].strip() if "=" in line else line
                if len(bearer) >= 2 and bearer[0] == bearer[-1] and bearer[0] in "\"'":
                    bearer = bearer[1:-1]
                if bearer:
                    return bearer
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
    stripped and nothing is canonicalized. Only object rows with nonblank
    string ``id`` values are accepted; malformed rows are skipped, not treated
    as model policy.
    """
    if not isinstance(payload, dict):
        raise ServedModelsError("oracle payload is not an object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ServedModelsError("oracle payload has no data[] list")
    ids = []
    for row in data:
        mid = row.get("id") if isinstance(row, dict) else None
        if isinstance(mid, str) and mid.strip():
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
