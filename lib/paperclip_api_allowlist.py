#!/usr/bin/env python3
"""Shared Paperclip API host allowlist (warn-first shadow).

Every bearer-bearing sender checks the base it is about to send to against
an operator-owned host file (one https origin per line, `#` comments).
Phase 1 is shadow only: a base that is not allowlisted emits a structured
WARNING plus a counter but still sends. Missing/unreadable files and
malformed lines refuse (fail closed) in every mode; the enforce flip is a
separate follow-up that needs no code change (mode comes from the
environment). There is no in-repo fallback list: a silent fallback would be
the vulnerability in another shape.

Check order for a structurally clean base (callers run their structural
gate first; this module re-validates so `--check` is safe on raw input):
  1. Run-scoped bridge: marker `PAPERCLIP_API_BRIDGE_MODE=queue_v1` plus an
     `http://127.0.0.1:<port>` base (dynamic port) is allowed. The marker
     alone widens nothing: any other base falls through to the rules below.
  2. Test hatch: `PAPERCLIP_API_ALLOWLIST_TEST` exactly `1` with a test-only
     base (IPv4 loopback `127.0.0.0/8`, `::1`, `localhost`, or a `.invalid`
     final-label suffix) is allowed without consulting the host file, so
     hermetic suites stay offline. Hatch plus any other host refuses.
  3. Host file: exact-origin match after normalization (lowercase host,
     default port dropped, one trailing slash stripped). The comparison
     includes any residual non-default port: a candidate with one never
     matches a portless entry. No subdomain or suffix matching. Candidates
     carrying a path never match (bases are bare origins at the gate point).
  4. No match: `warn` (the default) logs and sends; `enforce` refuses. An
     unknown mode value refuses.

`MODE=warn` is an availability rollback, not a security boundary: it shares
the environment namespace with the base it validates, so a fully hostile
environment sets both. Operator-owned, audit-logged, time-bounded.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import urllib.parse

DEFAULT_ALLOWLIST_FILE = "/etc/paperclip/allowed-api-hosts"
HATCH_VAR = "PAPERCLIP_API_ALLOWLIST_TEST"
MODE_VAR = "PAPERCLIP_API_ALLOWLIST_MODE"
BRIDGE_MODE_VAR = "PAPERCLIP_API_BRIDGE_MODE"
BRIDGE_MODE_VALUE = "queue_v1"
COUNTER_VAR = "PAPERCLIP_API_ALLOWLIST_COUNTER_FILE"

MODE_WARN = "warn"
MODE_ENFORCE = "enforce"

# Process-wide shadow counter: incremented once per shadow warning that is
# actually emitted (quiet pre-checks never touch it). Tests pin increments;
# production counting is aggregation over the structured log lines.
SHADOW_COUNT = 0

_DEFAULT_PORTS = {"http": 80, "https": 443}


class AllowlistRefused(Exception):
    """The base must not receive the bearer (fail closed, zero requests)."""


class _OriginError(ValueError):
    """A base or file line is not a clean origin."""


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _sanitize(raw: str, limit: int = 200) -> str:
    return "".join(
        char if 0x20 <= ord(char) < 0x7F else "\\x%02x" % ord(char)
        for char in raw[:limit]
    )


def _parse_origin(base: str) -> dict:
    """Split a base into scheme/host/port/path, refusing dirty shapes."""
    if not isinstance(base, str) or not base:
        raise _OriginError("empty base")
    candidate = base[:-1] if base.endswith("/") else base
    if not candidate.startswith(("http://", "https://")):
        raise _OriginError("not an http(s) URL")
    if "@" in candidate:
        raise _OriginError("carries userinfo")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in candidate):
        raise _OriginError("carries whitespace/control characters")
    if "{" in candidate or "}" in candidate:
        raise _OriginError("carries braces")
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        raise _OriginError("unparseable URL") from None
    if parts.username or parts.password:
        raise _OriginError("carries userinfo")
    if parts.query or parts.fragment:
        raise _OriginError("carries a query/fragment")
    decoded_netloc = urllib.parse.unquote(parts.netloc)
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in decoded_netloc):
        raise _OriginError("carries encoded controls in the authority")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise _OriginError("has an empty host")
    if any(char in "[]{}," for char in host):
        raise _OriginError("authority is not one bare host")
    if not parts.netloc.startswith("[") and parts.netloc.count(":") > 1:
        raise _OriginError("authority is not one bare host")
    try:
        port = parts.port
    except ValueError:
        raise _OriginError("has a non-numeric port") from None
    if port is None and parts.netloc.endswith(":"):
        raise _OriginError("has an empty port")
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    return {"scheme": scheme, "host": host, "port": port, "path": parts.path}


def _origin_key(parsed: dict) -> str:
    host = parsed["host"]
    if ":" in host:
        host = "[%s]" % host
    key = "%s://%s" % (parsed["scheme"], host)
    if parsed["port"] is not None:
        key += ":%d" % parsed["port"]
    return key


def _is_loopback_host(host: str) -> bool:
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.IPv4Address(host).is_loopback
    except ipaddress.AddressValueError:
        return False


def _is_test_only_host(host: str) -> bool:
    return _is_loopback_host(host) or host.endswith(".invalid")


def load_allowlist(path: str = DEFAULT_ALLOWLIST_FILE) -> set[str]:
    """Read the operator-owned host file into canonical https origins."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise AllowlistRefused(
            "refusing: allowlist file missing or unreadable: %s (%s)"
            % (path, getattr(exc, "strerror", None) or exc.__class__.__name__)
        ) from exc
    origins: set[str] = set()
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed = _parse_origin(line)
        except _OriginError as exc:
            raise AllowlistRefused(
                "refusing: allowlist file %s:%d: malformed origin %s (%s)"
                % (path, lineno, _sanitize(line, 120), exc)
            ) from exc
        if parsed["scheme"] != "https":
            raise AllowlistRefused(
                "refusing: allowlist file %s:%d: malformed origin %s "
                "(entries are https-only)" % (path, lineno, _sanitize(line, 120))
            )
        if parsed["path"] not in ("", "/"):
            raise AllowlistRefused(
                "refusing: allowlist file %s:%d: malformed origin %s "
                "(entries are bare origins, no path)"
                % (path, lineno, _sanitize(line, 120))
            )
        if parsed["host"].endswith("."):
            raise AllowlistRefused(
                "refusing: allowlist file %s:%d: malformed origin %s "
                "(trailing-dot host)" % (path, lineno, _sanitize(line, 120))
            )
        origins.add(_origin_key(parsed))
    return origins


def _bump_counter(environ) -> None:
    global SHADOW_COUNT
    SHADOW_COUNT += 1
    counter_path = environ.get(COUNTER_VAR, "")
    if not counter_path:
        return
    try:
        try:
            with open(counter_path, encoding="utf-8") as handle:
                current = int(handle.read().strip() or "0")
        except FileNotFoundError:
            current = 0
        with open(counter_path, "w", encoding="utf-8") as handle:
            handle.write("%d\n" % (current + 1))
    except (OSError, ValueError):
        # Observability only: a counter write must never fail the send.
        pass


def check_api_base_allowed(
    base: str,
    sender: str,
    *,
    allowlist_path: str = DEFAULT_ALLOWLIST_FILE,
    environ=None,
    quiet: bool = False,
) -> str:
    """Verdict for sending to `base`: match/bridge/hatch/shadow, else raise.

    Returns "file_match", "bridge", "hatch", or "shadow" (warn mode: the
    caller still sends). Raises AllowlistRefused before any request is
    built otherwise. With quiet=True no log line is emitted and no counter
    moves: a fail-fast pre-check whose owner reports via its own error.
    """
    env = os.environ if environ is None else environ

    def refuse(reason: str) -> AllowlistRefused:
        message = "refusing: allowlist %s sender=%s" % (reason, sender)
        if not quiet:
            _log(message)
        return AllowlistRefused(message)

    try:
        parsed = _parse_origin(base)
    except _OriginError as exc:
        raise refuse("base is not a clean http(s) base (%s)" % exc) from None
    origin = _origin_key(parsed)
    host = parsed["host"]

    # 1. Run-scoped bridge: explicit marker plus the observed dynamic-port
    # loopback shape. The marker alone widens nothing; anything else falls
    # through.
    if (
        env.get(BRIDGE_MODE_VAR, "") == BRIDGE_MODE_VALUE
        and parsed["scheme"] == "http"
        and host == "127.0.0.1"
        and parsed["port"] is not None
    ):
        return "bridge"

    # 2. Test hatch: env-only, honored only with a test-only base.
    if env.get(HATCH_VAR, "") == "1":
        if _is_test_only_host(host):
            return "hatch"
        raise refuse(
            "test hatch with non-test base base=%s" % _sanitize(origin)
        ) from None

    # 3. Host file: missing/unreadable/malformed refuses in every mode.
    try:
        allowed = load_allowlist(allowlist_path)
    except AllowlistRefused as exc:
        if not quiet:
            _log("%s sender=%s" % (exc, sender))
        raise AllowlistRefused("%s sender=%s" % (exc, sender)) from None

    if parsed["path"] not in ("", "/"):
        reason = "path-not-matched"
        matched = False
    else:
        reason = "no-match"
        matched = origin in allowed
    if matched:
        return "file_match"

    # 4. No match: warn-and-send in shadow, refuse in enforce.
    mode = env.get(MODE_VAR, "") or MODE_WARN
    if mode == MODE_WARN:
        if not quiet:
            _log(
                "WARNING allowlist_shadow base_host=%s sender=%s base=%s "
                "mode=warn reason=%s" % (host, sender, origin, reason)
            )
            _bump_counter(env)
        return "shadow"
    if mode == MODE_ENFORCE:
        raise refuse(
            "base not allowlisted base=%s mode=enforce" % _sanitize(origin)
        ) from None
    raise refuse("unknown %s=%s" % (MODE_VAR, _sanitize(mode, 40))) from None


def _check_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run the API host allowlist without sending."
    )
    parser.add_argument("--check", required=True, help="base URL to validate")
    parser.add_argument("--sender", default="check")
    parser.add_argument("--file", default=DEFAULT_ALLOWLIST_FILE)
    parser.add_argument("--mode", default=None,
                        help="override %s for this run" % MODE_VAR)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.mode is not None:
        env[MODE_VAR] = args.mode
    # The verdict runs the exact sender path first (bridge/hatch before the
    # file): with a hatch/bridge base and a missing file the senders allow
    # without consulting the file, so --check must too. The entries count is
    # display-only and degrades to "?" when the file is unreadable.
    try:
        verdict = check_api_base_allowed(
            args.check, args.sender, allowlist_path=args.file, environ=env)
    except AllowlistRefused as exc:
        refused = exc
    else:
        refused = None
    try:
        entries = str(len(load_allowlist(args.file)))
    except AllowlistRefused:
        entries = "?"
    if refused is not None:
        print("REFUSE base=%s file=%s entries=%s reason=%s" % (
            _sanitize(args.check), args.file, entries, refused))
        return 1
    print("ALLOW base=%s file=%s entries=%s verdict=%s" % (
        _sanitize(args.check), args.file, entries, verdict))
    return 0


if __name__ == "__main__":
    sys.exit(_check_command(sys.argv[1:]))
