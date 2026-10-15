#!/usr/bin/env python3
# ===========================================================================
# two_bot_staging_precheck.py — ingress-independent deploy pre-checks for
# two-bot-next staging (case_be75d8d7, leaf of case_70b26044).
#
# WHY THIS EXISTS. The staging deploy pipeline (deploy-staging.yml) can only
# run with staging ingress: STAGING_WORKER_URL, STAGING_OWNERSHIP_CONTROL_TOKEN
# and CLOUDFLARE_API_TOKEN, plus a reachable Worker to snapshot and verify
# against (staging_rollout.py prepare/verify). When ingress is down (case_fa4355b2)
# every deploy-side signal goes dark with it. These pre-checks run WITHOUT
# ingress, credentials, network or host actions: they validate the checked-in
# staging deploy surface and record the built-image digests as evidence, on a
# plain runner with only python3 and the two source files.
#
# "COMPOSE CONFIG" MAPPING. two-bot-next carries no Compose file (verified:
# no docker-compose.yml at the repo root; deploy/ holds systemd units only).
# The deploy-config analogue is wrangler/wrangler.toml [env.staging] — the
# Container/DO/vars wiring a staging deploy actually ships — so that is what
# the config half validates. (This repo's own Compose files stay covered by
# test_case_60330d6f_status_pilot.sh §7a digest-pinning; this tool does not re-own
# them.)
#
# WHAT IT CHECKS (all offline, all without values ever entering output).
#   W1  [env.staging] exists (refuse when absent: nothing staging was measured)
#   W2  top-level name/main/compatibility_date present
#   W3  staging repeats the full Container wiring (class TwoBotContainer,
#       image ../Dockerfile, instance_type lite, max_instances 1)
#   W4  staging repeats the DO wiring (binding TWO_BOT, exports storage sqlite)
#   W5  staging version_metadata binding CF_VERSION_METADATA present
#   W6  every top-level [vars] key is repeated in staging vars, plus
#       TWO_GUILD_NAME (vars are non-inheritable; case_f1f05ef7 was this exact hole)
#   W7  staging BOT_PORT agrees with the Dockerfile EXPOSE/LISTEN_ADDR ports
#   W8  INTERNAL_ACTIONS_INGRESS is staging-only ("1" in staging, absent
#       top-level and in every other env; case_5d4ea03f)
#   W9  secret sweep: no secret-NAMED plain var and no secret-SHAPED value in
#       any vars table (findings name keys/paths only, never values)
#   W10 staging observability enabled (case_0c01d398)
#   D1  every external Dockerfile FROM carries @sha256:64hex; earlier-stage
#       aliases and scratch are not external images (a floating tag is mutable)
#   D2  digest record: stages, file hashes, verdict — evidence, not a gate
#       (case_90df828d: digests are recorded, never an execution precondition)
#
# EXIT CODES are the interface; callers gate on these, never on printed text.
#   0  pass: every check held, record written (with --record)
#   1  finding: at least one W*/D1 check failed
#   2  refused: measured nothing (missing/unreadable file, no TOML parser,
#      unparseable TOML, no [env.staging], malformed/missing FROM, usage or
#      unexpected internal error). Subcommands are required; flags follow them.
#
# SUBCOMMANDS.
#   check    validate + (with --record PATH) write the digest record
#   surface  print the flattened staging surface under review (evidence)
#
# VALUE HYGIENE. Findings and surface output name paths and keys only. Var
# VALUES are printed solely for SAFE_VALUE_KEYS (known-nonsecret constants)
# that are scalars and match no SECRET_VALUE_RES shape; everything else appears
# as a key name. The digest record carries digests,
# hashes and names — never var values. A suite case feeds a sentinel secret
# and asserts it appears nowhere on either stream.
# ===========================================================================
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ME = "two_bot_staging_precheck.py"
TOOL_VERSION = 1

EXIT_OK, EXIT_FINDING, EXIT_REFUSED = 0, 1, 2

try:
    import tomllib
except ImportError:  # fail closed where the stdlib parser is absent
    tomllib = None  # type: ignore[assignment]

REQUIRED_CONTAINER_CLASS = "TwoBotContainer"
REQUIRED_CONTAINER_IMAGE = "../Dockerfile"
REQUIRED_DO_BINDING = "TWO_BOT"
REQUIRED_DO_CLASS = "TwoBotContainer"
REQUIRED_VERSION_BINDING = "CF_VERSION_METADATA"
REQUIRED_INSTANCE_TYPE = "lite"
REQUIRED_MAX_INSTANCES = 1
REQUIRED_REGIONS = ["ENAM"]
REQUIRED_JURISDICTION = "us"
REQUIRED_EXPORT_STORAGE = "sqlite"
EXTRA_REQUIRED_VARS = {"TWO_GUILD_NAME"}
INGRESS_VAR = "INTERNAL_ACTIONS_INGRESS"
INGRESS_ENVS = {"staging"}
# Secret bindings that must stay Operator-set Worker secrets, never plain vars
# (mirrors OPTIONAL_SECRET_BINDINGS in check-env-bindings.py).
NEVER_PLAIN_VARS = {"OPS_ALERT_WEBHOOK_URL", "METRICS_SCRAPE_TOKEN"}
# Var keys whose VALUES are known-nonsecret constants and may be quoted in
# surface output when is_quotable. Every other value is never printed, on any
# path, in any mode.
SAFE_VALUE_KEYS = {
    "BOT_PORT",
    "KEEPALIVE_SECONDS",
    "TWO_GUILD_NAME",
    "INTERNAL_ACTIONS_INGRESS",
}
SECRET_KEY_RE = re.compile(
    r"token|secret|password|passwd|credential|private[_-]?key|api[_-]?key"
    r"|apikey|bearer|dsn|database[_-]?url|webhook|auth",
    re.IGNORECASE,
)
SECRET_VALUE_RES = (
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"xox[bpas]-[A-Za-z0-9-]+"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
FROM_RE = re.compile(r"^FROM(?:\s+(.*))?$", re.IGNORECASE)
DIGEST_SUFFIX_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
EXPOSE_RE = re.compile(r"^EXPOSE\s+(.+)$", re.IGNORECASE)
LISTEN_INVALID = "__invalid_listen__"
# ASCII whitespace for Dockerfile keyword splits (BuildKit's splitCommand uses
# reWhitespace = [\t\v\f\r ]+; logical lines never contain \n, which is also
# accepted here as harmless). Never \s: it matches NBSP/U+00A0, so
# `ONBUILD RUN\xa0<<...` would look like a heredoc BuildKit does not see.
_ENV_RE = re.compile(r"(?i)^ENV(?:[ \t\r\n\v\f]|$)")
_ONBUILD_RE = re.compile(r"(?i)^ONBUILD[ \t\r\n\v\f]+")
_DIRECTIVE_RE = re.compile(
    r"(syntax|escape|check)[ \t\r\n\f]*=[ \t\r\n\f]*(.+?)[ \t\r\n\f]*",
    re.IGNORECASE | re.ASCII,
)
_RUNLIKE_RE = re.compile(r"(?i)^(RUN|COPY|ADD)(?:[ \t\r\n\v\f]|$)")

# Go's unicode.IsSpace (White Space property) used by BuildKit's shell lexer
# (frontend/dockerfile/shell/lex.go). Python's str.isspace() is broader: it is
# also true for U+001C-U+001F, which Go does not treat as spaces, so a tool
# splitting on str.isspace() sees a heredoc BuildKit does not (and vice versa
# for the other direction). Split only on the Go set.
_GO_SPACE_CODES = frozenset(
    [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680,
     0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
    + list(range(0x2000, 0x200B))
)
_GO_SPACE_CHARS = "".join(map(chr, sorted(_GO_SPACE_CODES)))
_ASCII_SPACE = " \t\n\r\v\f"


def _is_go_space(char: str) -> bool:
    return ord(char) in _GO_SPACE_CODES


def _go_strip(text: str) -> str:
    return text.strip(_GO_SPACE_CHARS)


def _go_lstrip(text: str) -> str:
    return text.lstrip(_GO_SPACE_CHARS)


class Refused(Exception):
    """Measured nothing — never conflated with pass or finding."""


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise Refused("unreadable file: %s" % path) from exc


def load_toml(path: Path) -> dict:
    if tomllib is None:
        raise Refused("no TOML parser on this interpreter")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise Refused("unreadable file: %s" % path) from exc
    except Exception as exc:  # tomllib.TOMLDecodeError and kin
        raise Refused("unparseable TOML: %s" % path) from exc
    if not isinstance(data, dict):
        raise Refused("unparseable TOML: %s" % path)
    return data


def split_shell_words_raw(
    instruction: str, escape: str = "\\", heredoc: bool = True
) -> list[str]:
    """Split on whitespace, respecting quotes and escapes, keeping them raw."""
    # BuildKit splits instruction words on whitespace (unicode.IsSpace) and
    # never strips shell comments, so `# <<EOF` still opens a heredoc.
    # Quotes and escapes only decide where a word ends; they stay in the word.
    # Like BuildKit's shell lexer (processPossibleHeredoc in
    # frontend/dockerfile/shell/lex.go), a `<<` operator also absorbs directly
    # following spaces/tabs into the same word, so `RUN cat << EOF` still
    # opens a heredoc with delimiter `EOF`. ENV words have no `<<` operator
    # (BuildKit's parseWords), so callers pass heredoc=False there; escape is
    # the parser escape directive.
    words: list[str] = []
    current: list[str] = []
    index = 0
    length = len(instruction)
    quote: str | None = None
    while index < length:
        char = instruction[index]
        if quote is not None:
            current.append(char)
            if char == escape and quote == '"' and index + 1 < length:
                current.append(instruction[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
        elif char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
        elif char == escape and index + 1 < length:
            current.append(char)
            current.append(instruction[index + 1])
            index += 2
        elif char == escape:
            # A trailing escape is dropped, as BuildKit's parseWords and lexer do.
            index += 1
        elif heredoc and char == "<" and instruction[index + 1:index + 2] == "<":
            current.append("<<")
            index += 2
            while index < length and instruction[index] in (" ", "\t", "\r"):
                current.append(instruction[index])
                index += 1
        elif _is_go_space(char):
            if current:
                words.append("".join(current))
                current = []
            while index < length and _is_go_space(instruction[index]):
                index += 1
        else:
            current.append(char)
            index += 1
    if quote is not None:
        # Unterminated quote: BuildKit's processStopOn returns [] + error and
        # heredocsFromLine drops the error (`words, _ :=`), so no heredocs.
        # Return no words so e.g. `RUN echo <<"EXPOSE 8080" don't` hides no
        # alias (frontend/dockerfile/shell/lex.go, parser.go).
        return []
    if current:
        words.append("".join(current))
    return words


# re.ASCII: Go's RE2 \d is ASCII-only, while Python's \d also matches
# non-ASCII digits (e.g. U+0661). Without ASCII, `RUN echo ١<<"EXPOSE 8080"`
# looks like a heredoc to the tool but not to BuildKit. Go's \s is
# [\t\n\f\r ] (no \v), so the whitespace class is spelled out.
HEREDOC_WORD_RE = re.compile(r"^\d*<<(-?)[ \t\r\n\f]*([^<]*)$", re.ASCII)


def _shell_unquote(word: str, escape: str = "\\") -> tuple[str, bool]:
    """Remove quotes and escapes as BuildKit's shell lexer does.

    Returns (value, terminated); an open quote at the end is not terminated.
    Variable expansion is not performed.
    """
    # Quotes group and are removed, and an escape resolves to the escaped
    # character (so `E\\OF` reads as `EOF`). Callers that cannot see the
    # expansion must treat a `$` in the result as unknown.
    name: list[str] = []
    index = 0
    length = len(word)
    quote: str | None = None
    while index < length:
        char = word[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                name.append(char)
            index += 1
        elif quote == '"':
            if char == '"':
                quote = None
                index += 1
            elif (
                char == escape
                and index + 1 < length
                and word[index + 1] in ('"', "$", escape)
            ):
                name.append(word[index + 1])
                index += 2
            else:
                name.append(char)
                index += 1
        elif char in ("'", '"'):
            quote = char
            index += 1
        elif char == escape:
            # A trailing escape is dropped (the lexer breaks at EOF).
            if index + 1 < length:
                name.append(word[index + 1])
            index += 2
        else:
            name.append(char)
            index += 1
    return "".join(name), quote is None


def _heredoc_name(remainder: str) -> str:
    """Derive a heredoc delimiter the way BuildKit's heredocFromMatch does."""
    # BuildKit runs the shell lexer's ProcessWords over the remainder, and
    # there is no `#`/operator special-casing.
    # (frontend/dockerfile/shell/lex.go)
    return _shell_unquote(remainder)[0]


def heredoc_delimiters(instruction: str, lineno: int) -> list[tuple[str, bool]]:
    """Find heredoc delimiters using BuildKit's word rule, not shell grammar."""
    # https://docs.docker.com/reference/dockerfile/#here-documents
    # BuildKit's canContainHeredoc covers RUN/COPY/ADD, looking through an
    # ONBUILD prefix, and never JSON-form (args that parse as a JSON string
    # array, per parseMaybeJSON — a `[`-led shell form like
    # `RUN [ -d /tmp ] && cat <<EOF` still carries a heredoc). It splits the
    # instruction into words and treats every word matching
    # ^\d*<<(-?)\s*([^<]*)$ as a heredoc whose delimiter is the remainder
    # with BuildKit's quote and escape removal applied (an empty remainder
    # opens no heredoc).
    # (frontend/dockerfile/parser/parser.go, frontend/dockerfile/shell/lex.go)
    text = _go_lstrip(instruction)
    inner = text
    if _ONBUILD_RE.match(text):
        inner = _ONBUILD_RE.sub("", text, count=1)
    if not _RUNLIKE_RE.match(inner):
        return []
    if "<<" not in inner:
        return []
    args = _go_strip(_RUNLIKE_RE.sub("", inner, count=1))
    if args.startswith("["):
        try:
            parsed = json.loads(args)
        except Exception:
            parsed = None
        if isinstance(parsed, list) and all(
            isinstance(item, str) for item in parsed
        ):
            return []
    # Fail closed on ${...} re-splitting: BuildKit's processDollar returns an
    # unset expansion raw and splits it on whitespace ignoring quotes, so
    # `RUN echo ${X:-'a <<EOF\'}` carries a heredoc the shell-word split below
    # cannot see. Refuse instead of risking a missed alias (exit 2).
    if "${" in inner and "<<" in inner:
        raise Refused("unhandled ${...} with << at line %d" % lineno)
    delimiters = []
    for word in split_shell_words_raw(inner):
        match = HEREDOC_WORD_RE.match(word)
        if not match:
            continue
        strip_tabs = match.group(1) == "-"
        delimiter = _heredoc_name(match.group(2))
        if not delimiter:
            continue
        delimiters.append((delimiter, strip_tabs))
    return delimiters


def dockerfile_instructions(text: str):
    """Yield (lineno, instruction, escape); RUN/COPY heredoc data is never a stage."""
    # https://docs.docker.com/reference/dockerfile/#escape
    # https://docs.docker.com/reference/dockerfile/#here-documents
    escape = "\\"
    pending = ""
    start = 0
    heredocs: list[tuple[str, bool]] = []
    directives_open = True
    seen_directives: set[str] = set()
    for lineno, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if heredocs:
            delimiter, strip_tabs = heredocs[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                heredocs.pop(0)
            continue
        stripped = _go_strip(line)
        if directives_open:
            # Ordinary comments, blank lines, and instructions close this window.
            # Only leading Go spaces are trimmed: a trailing NBSP stays in the
            # value, as in BuildKit, and the ASCII-only pattern keeps the Kelvin
            # sign and long s from folding into check or syntax.
            # https://docs.docker.com/reference/dockerfile/#parser-directives
            lead = _go_lstrip(line)
            directive = (
                _DIRECTIVE_RE.fullmatch(_go_lstrip(lead[1:]))
                if lead.startswith("#") else None
            )
            if directive:
                key, value = directive.group(1).lower(), directive.group(2)
                if key in seen_directives:
                    raise Refused("duplicate parser directive at line %d" % lineno)
                seen_directives.add(key)
                if key == "escape":
                    if value not in ("\\", "`"):
                        raise Refused("invalid escape directive at line %d" % lineno)
                    escape = value
                continue
            directives_open = False
        if not stripped or stripped.startswith("#"):
            continue
        if not pending:
            start = lineno
        content = line.rstrip(" \t")
        escape_re = re.escape(escape)
        # BuildKit joins a line ending in exactly one escape; an escaped escape does not join.
        if re.search(r"(?:^|[^%s])%s$" % (escape_re, escape_re), content):
            pending += content[:-1]
            continue
        # BuildKit left-trims only the first physical line and keeps trailing
        # bytes, so `\<VT>` stays an escaped VT and not a trailing escape.
        instruction = _go_lstrip(pending + line)
        pending = ""
        heredocs = heredoc_delimiters(instruction, start)
        yield start, instruction, escape
    if pending or heredocs:
        raise Refused("unterminated Dockerfile instruction")


def exposed_ports(spec: str) -> list[tuple[int, int]]:
    """Return (low, high) pairs without expanding ranges.

    Refuses on out-of-range bounds or inverted ranges so a hostile EXPOSE
    line cannot exhaust memory in the CI run. Ports are nonsecret constants.
    """
    ranges: list[tuple[int, int]] = []
    for word in spec.split():
        match = re.fullmatch(r"(\d+)(?:-(\d+))?(?:/\w+)?", word)
        if match:
            low, high = int(match.group(1)), int(match.group(2) or match.group(1))
            if low > high or low > 65535 or high > 65535:
                raise Refused("invalid EXPOSE port range: %s" % word)
            ranges.append((low, high))
    return ranges


def _listen_port(value: str) -> str:
    """Port bound by a LISTEN_ADDR value, or LISTEN_INVALID unless 0.0.0.0:<port>."""
    host, _, port = value.rpartition(":")
    if host == "0.0.0.0" and re.fullmatch(r"[0-9]+", port):
        return str(int(port))
    return LISTEN_INVALID


def _env_listen_assignment(instruction: str, escape: str) -> str | None:
    """Port an ENV instruction gives LISTEN_ADDR (last wins), or None if it gives none.

    Follows BuildKit's parseNameVal: words split on Go whitespace with quotes and
    escapes kept raw. A first word without '=' is the legacy `ENV KEY VALUE`
    form, whose value is the rest of the line. Keys and values then lose their
    quotes and escapes. Unparseable input, or a key that could expand to
    LISTEN_ADDR, is LISTEN_INVALID.
    """
    text = _go_strip(instruction)
    if not _ENV_RE.match(text):
        return None
    rest = _ENV_RE.sub("", text, count=1)
    # BuildKit scans for leading `--` flags on the untrimmed remainder and reads
    # only ASCII spaces there. A bare `--` ends the flag list; any other flag is
    # a parse error because ENV defines none.
    body = rest.lstrip(_ASCII_SPACE)
    if body.startswith("--"):
        after = body[2:]
        if after and after[0] not in _ASCII_SPACE:
            return LISTEN_INVALID
        args = _go_strip(after)
    else:
        args = _go_strip(rest)
    if not args:
        return LISTEN_INVALID
    words = split_shell_words_raw(args, escape=escape, heredoc=False)
    if not words:
        return LISTEN_INVALID
    pairs: list[tuple[str, str]] = []
    if "=" in words[0]:
        for word in words:
            if "=" not in word:
                return LISTEN_INVALID
            raw_key, raw_value = word.split("=", 1)
            pairs.append((raw_key, raw_value))
    else:
        parts = re.split(r"[\t\v\f\r ]+", args, maxsplit=1)
        if len(parts) != 2:
            return LISTEN_INVALID
        pairs.append((parts[0], parts[1]))
    listen: str | None = None
    for raw_key, raw_value in pairs:
        if "$" in raw_key:
            return LISTEN_INVALID
        key, key_ok = _shell_unquote(raw_key, escape)
        value, value_ok = _shell_unquote(raw_value, escape)
        if not (key_ok and value_ok):
            return LISTEN_INVALID
        if key == "LISTEN_ADDR":
            listen = _listen_port(value)
    return listen


def parse_dockerfile(
    path: Path,
) -> tuple[list[dict], list[tuple[int, int]], list[str]]:
    """Returns (stages, expose_ranges, listen_ports). Refuses on no FROM."""
    try:
        # Read bytes and split on LF only: BuildKit's scanner splits lines on
        # '\n' only, so a lone '\r' stays inside the line (a `# note\rRUN ...`
        # line is one comment). Path.read_text() uses universal newlines and
        # would split it into a comment plus a RUN, seeing a heredoc BuildKit
        # does not. Decoding utf-8-sig still strips a leading BOM.
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise Refused("unreadable file: %s" % path) from exc
    except UnicodeDecodeError as exc:
        raise Refused("unreadable file: %s" % path) from exc
    stages: list[dict] = []
    aliases: set[str] = set()
    onbuild_stages: set[str] = set()
    # Per-stage EXPOSE/LISTEN snapshots so a stage built FROM an earlier
    # alias starts from a copy of that stage's image config (BuildKit:
    # ds.image = clone(ds.base.image) in dockerfile2llb). Ports are nonsecret.
    stage_ports: dict[str, tuple[list[tuple[int, int]], list[str]]] = {}
    current_alias = ""
    expose: list[tuple[int, int]] = []
    listen: list[str] = []
    for lineno, stripped, escape in dockerfile_instructions(text):
        match = FROM_RE.match(stripped)
        if match:
            # FROM flags precede the image; prior aliases and scratch do not
            # fetch an external image: https://docs.docker.com/reference/dockerfile/#from
            parts = (match.group(1) or "").split()
            while parts and parts[0].startswith("--"):
                flag = parts.pop(0)
                if "=" not in flag or not flag.split("=", 1)[1]:
                    raise Refused("malformed FROM at line %d" % lineno)
            if len(parts) not in (1, 3) or (
                len(parts) == 3 and parts[1].lower() != "as"
            ):
                raise Refused("malformed FROM at line %d" % lineno)
            ref = parts[0]
            stage = parts[2] if len(parts) == 3 else ""
            external = ref.lower() not in aliases and ref.lower() != "scratch"
            if not external and ref.lower() in onbuild_stages:
                # BuildKit runs a base stage's ONBUILD triggers in the child, which
                # this pre-check does not evaluate, so refuse rather than guess.
                raise Refused("ONBUILD in base stage %s at line %d" % (ref, lineno))
            digest_match = DIGEST_SUFFIX_RE.search(ref) if external else None
            stages.append(
                {
                    "line": lineno,
                    "stage": stage,
                    "base": ref.split("@")[0],
                    "digest": digest_match.group(0)[len("@") :]
                    if digest_match
                    else "",
                    "external": external,
                }
            )
            # Only the final stage ships, so only its ports and listen address
            # count, including those inherited from its base stage. A stage
            # built FROM an earlier alias starts from a copy of that stage's
            # EXPOSE and LISTEN_ADDR; external images and scratch start empty.
            # A later ENV LISTEN_ADDR replaces the inherited value (Docker ENV
            # replaces the variable); EXPOSE keeps accumulating because Docker
            # merges exposed ports.
            if current_alias:
                stage_ports[current_alias.lower()] = (list(expose), list(listen))
            base_alias = ref.lower()
            if not external and base_alias in stage_ports:
                base_expose, base_listen = stage_ports[base_alias]
                expose = list(base_expose)
                listen = list(base_listen)
            else:
                expose = []
                listen = []
            current_alias = stage
            if stage:
                aliases.add(stage.lower())
            continue
        if current_alias and _ONBUILD_RE.match(stripped):
            onbuild_stages.add(current_alias.lower())
        expose_match = EXPOSE_RE.match(stripped)
        if expose_match:
            expose.extend(exposed_ports(expose_match.group(1)))
        # Docker ENV replaces the variable, so an ENV LISTEN_ADDR in the final
        # stage replaces the inherited value whatever its host. Only 0.0.0.0:<port>
        # counts as listening; anything else is a sentinel so the port check
        # fails instead of passing on the inherited port.
        listen_port = _env_listen_assignment(stripped, escape)
        if listen_port is not None:
            listen = [listen_port]
    if not stages:
        raise Refused("no FROM lines in %s" % path)
    return stages, expose, listen


def check_wrangler(cfg: dict) -> list[str]:
    """Staging deploy-surface checks W1-W10. Names paths/keys only."""
    findings: list[str] = []
    for key in ("name", "main", "compatibility_date"):
        if not cfg.get(key):
            findings.append("top-level: missing %s" % key)
    envs = cfg.get("env")
    if not isinstance(envs, dict) or "staging" not in envs:
        raise Refused("no [env.staging] section")
    staging = envs["staging"]
    if not isinstance(staging, dict):
        raise Refused("no [env.staging] section")

    # W3: staging Container wiring repeats the deploy-bearing fields.
    containers = staging.get("containers")
    if not isinstance(containers, list) or not containers:
        findings.append("[env.staging.containers]: missing container wiring")
    else:
        seen_class = False
        for entry in containers:
            if not isinstance(entry, dict):
                continue
            if entry.get("class_name") == REQUIRED_CONTAINER_CLASS:
                seen_class = True
                if entry.get("image") != REQUIRED_CONTAINER_IMAGE:
                    findings.append(
                        "[env.staging.containers]: image is not the "
                        "checked-in Dockerfile"
                    )
                if entry.get("instance_type") != REQUIRED_INSTANCE_TYPE:
                    findings.append(
                        "[env.staging.containers]: instance_type drift"
                    )
                max_instances = entry.get("max_instances")
                if type(max_instances) is not int or (
                    max_instances != REQUIRED_MAX_INSTANCES
                ):
                    findings.append(
                        "[env.staging.containers]: max_instances drift"
                    )
                constraints = entry.get("constraints")
                if not isinstance(constraints, dict):
                    findings.append(
                        "[env.staging.containers.constraints]: missing "
                        "jurisdiction pin"
                    )
                else:
                    if constraints.get("regions") != REQUIRED_REGIONS:
                        findings.append(
                            "[env.staging.containers.constraints]: "
                            "regions drift"
                        )
                    if (
                        constraints.get("jurisdiction")
                        != REQUIRED_JURISDICTION
                    ):
                        findings.append(
                            "[env.staging.containers.constraints]: "
                            "jurisdiction drift"
                        )
        # MUTATION-ANCHOR-START: container-class gate (the offline suite
        # deletes this block to prove it is load-bearing: the mutant must
        # pass a config whose staging containers omit TwoBotContainer).
        if not seen_class:
            findings.append(
                "[env.staging.containers]: class %s absent"
                % REQUIRED_CONTAINER_CLASS
            )
        # MUTATION-ANCHOR-END

    # W4: staging DO wiring.
    do_bindings = staging.get("durable_objects", {}).get("bindings")
    do_ok = False
    if isinstance(do_bindings, list):
        for binding in do_bindings:
            if (
                isinstance(binding, dict)
                and binding.get("name") == REQUIRED_DO_BINDING
                and binding.get("class_name") == REQUIRED_DO_CLASS
            ):
                do_ok = True
    if not do_ok:
        findings.append(
            "[env.staging.durable_objects.bindings]: binding %s absent"
            % REQUIRED_DO_BINDING
        )
    exports = staging.get("exports", {}).get(REQUIRED_CONTAINER_CLASS)
    if not isinstance(exports, dict) or exports.get("storage") != (
        REQUIRED_EXPORT_STORAGE
    ):
        findings.append(
            "[env.staging.exports]: storage drift for %s"
            % REQUIRED_CONTAINER_CLASS
        )

    # W5: staging version metadata.
    version_meta = staging.get("version_metadata")
    if (
        not isinstance(version_meta, dict)
        or version_meta.get("binding") != REQUIRED_VERSION_BINDING
    ):
        findings.append("[env.staging.version_metadata]: binding drift")

    # W6: every top-level var repeated in staging (vars are non-inheritable).
    top_vars = cfg.get("vars", {})
    staging_vars = staging.get("vars", {})
    if not isinstance(top_vars, dict):
        findings.append("[vars]: missing top-level vars table")
        top_vars = {}
    if not isinstance(staging_vars, dict):
        findings.append("[env.staging.vars]: missing vars table")
        staging_vars = {}
    required_vars = set(top_vars) | EXTRA_REQUIRED_VARS
    for key in sorted(required_vars):
        if key not in staging_vars:
            findings.append(
                "[env.staging.vars]: missing repeated var %s" % key
            )

    # W8: ingress var is staging-only.
    if staging_vars.get(INGRESS_VAR) != "1":
        findings.append(
            "[env.staging.vars]: %s is not the staging-only default-dark "
            "ingress" % INGRESS_VAR
        )
    if INGRESS_VAR in top_vars:
        findings.append(
            "[vars]: %s is staging-only and must never be top-level"
            % INGRESS_VAR
        )
    for env_name, env in envs.items():
        if env_name in INGRESS_ENVS:
            continue
        if isinstance(env, dict) and INGRESS_VAR in env.get("vars", {}):
            findings.append(
                "[env.%s.vars]: %s is staging-only" % (env_name, INGRESS_VAR)
            )

    # W9: secret sweep over every vars table (keys/paths only, never values).
    tables = [("[vars]", top_vars)]
    tables += [
        ("[env.%s.vars]" % name, env.get("vars", {}))
        for name, env in envs.items()
        if isinstance(env, dict)
    ]
    for prefix, variables in tables:
        if not isinstance(variables, dict):
            continue
        for key in sorted(variables):
            if key in NEVER_PLAIN_VARS:
                findings.append(
                    "%s: %s must be an Operator-set Worker secret, never "
                    "a plain var" % (prefix, key)
                )
                continue
            if SECRET_KEY_RE.search(key):
                findings.append(
                    "%s: secret-named plain var %s" % (prefix, key)
                )
                continue
            if any(
                shape.search(text)
                for text in nested_strings(variables[key])
                for shape in SECRET_VALUE_RES
            ):
                findings.append(
                    "%s: secret-shaped value under %s" % (prefix, key)
                )

    # W10: staging observability (the case_0c01d398 diagnosis path).
    observability = staging.get("observability")
    if not isinstance(observability, dict) or (
        observability.get("enabled") is not True
    ):
        findings.append("[env.staging.observability]: staging logs disabled")

    return findings


def check_ports(
    staging_vars: dict, expose: list[tuple[int, int]], listen: list[str]
) -> list[str]:
    """Cross-file port agreement W7. Ports are nonsecret constants."""
    findings: list[str] = []
    bot_port = str(staging_vars.get("BOT_PORT", ""))
    if not bot_port:
        findings.append("[env.staging.vars]: missing BOT_PORT")
        return findings
    try:
        port_num = int(bot_port.strip())
    except (ValueError, AttributeError):
        port_num = None
    if port_num is None or not any(
        low <= port_num <= high for low, high in expose
    ):
        findings.append(
            "BOT_PORT agrees with no Dockerfile EXPOSE port"
        )
    if bot_port not in listen and (
        port_num is None or str(port_num) not in listen
    ):
        findings.append(
            "BOT_PORT agrees with no Dockerfile LISTEN_ADDR port"
        )
    return findings


def build_record(
    wrangler_path: Path,
    dockerfile_path: Path,
    stages: list[dict],
    findings: list[str],
) -> dict:
    return {
        "tool": ME,
        "version": TOOL_VERSION,
        "wrangler_sha256": sha256_file(wrangler_path),
        "dockerfile_sha256": sha256_file(dockerfile_path),
        "stages": [
            {
                "stage": item["stage"],
                "base": item["base"],
                "digest": item["digest"],
            }
            for item in stages
        ],
        "findings": sorted(findings),
        "verdict": "pass" if not findings else "finding",
    }


def cmd_check(args: argparse.Namespace) -> int:
    wrangler_path = Path(args.wrangler)
    dockerfile_path = Path(args.dockerfile)
    try:
        cfg = load_toml(wrangler_path)
        stages, expose, listen = parse_dockerfile(dockerfile_path)
        findings = check_wrangler(cfg)
        staging_vars = cfg["env"]["staging"].get("vars", {})
        if not isinstance(staging_vars, dict):
            staging_vars = {}
        findings += check_ports(staging_vars, expose, listen)
        # MUTATION-ANCHOR-DIGEST-START: digest-pinning gate (the offline
        # suite deletes this block to prove it is load-bearing: the mutant
        # must pass a Dockerfile with a floating tag).
        for item in stages:
            if item["external"] and not item["digest"]:
                findings.append(
                    "Dockerfile line %d: stage %s base is not digest-pinned"
                    % (item["line"], item["stage"] or item["base"])
                )
        # MUTATION-ANCHOR-DIGEST-END
    except Refused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    record = build_record(
        wrangler_path, dockerfile_path, stages, findings
    )
    if args.format == "json":
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        if findings:
            for item in sorted(findings):
                print("FINDING %s" % item)
        else:
            print("PASS: staging surface holds; %d image stage(s) pinned" % (
                sum(item["external"] for item in stages)
            ))
    if args.record:
        try:
            Path(args.record).write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print("REFUSED: cannot write record: %s" % exc, file=sys.stderr)
            return EXIT_REFUSED
    return EXIT_OK if not findings else EXIT_FINDING


def nested_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from nested_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_strings(item)


def is_quotable(value: object) -> bool:
    if not isinstance(value, (str, int, float)):
        return False
    return not any(shape.search(str(value)) for shape in SECRET_VALUE_RES)


def cmd_surface(args: argparse.Namespace) -> int:
    wrangler_path = Path(args.wrangler)
    try:
        cfg = load_toml(wrangler_path)
        envs = cfg.get("env")
        if not isinstance(envs, dict) or "staging" not in envs:
            raise Refused("no [env.staging] section")
        staging = envs["staging"]
        if not isinstance(staging, dict):
            raise Refused("no [env.staging] section")
    except Refused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    lines = [
        "name: %s" % cfg.get("name", ""),
        "main: %s" % cfg.get("main", ""),
        "compatibility_date: %s" % cfg.get("compatibility_date", ""),
    ]
    for entry in staging.get("containers", []):
        if isinstance(entry, dict):
            lines.append(
                "container: %s image=%s instance=%s max=%s"
                % (
                    entry.get("class_name", ""),
                    entry.get("image", ""),
                    entry.get("instance_type", ""),
                    entry.get("max_instances", ""),
                )
            )
    bindings = staging.get("durable_objects", {}).get("bindings", [])
    for binding in bindings if isinstance(bindings, list) else []:
        if isinstance(binding, dict):
            lines.append(
                "do-binding: %s class=%s"
                % (binding.get("name", ""), binding.get("class_name", ""))
            )
    variables = staging.get("vars", {})
    for key in sorted(variables) if isinstance(variables, dict) else []:
        if key in SAFE_VALUE_KEYS and is_quotable(variables[key]):
            lines.append("var: %s=%s" % (key, variables[key]))
        else:
            lines.append("var: %s (value withheld)" % key)
    print("\n".join(lines))
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=ME,
        description="Ingress-independent deploy pre-checks for "
        "two-bot-next staging. Supply check or surface before its flags.",
    )
    # Define shared options once, only on the subcommands. Missing/incorrect
    # command order is a usage error (2), not an implicit check of cwd files.
    # https://docs.python.org/3/library/argparse.html#sub-commands
    inputs = argparse.ArgumentParser(add_help=False)
    inputs.add_argument("--wrangler", default="wrangler.toml",
                        help="path to the wrangler.toml under review")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", parents=[inputs],
                           help="validate and record digests")
    check.add_argument("--dockerfile", default="Dockerfile",
                       help="path to the Dockerfile under review")
    check.add_argument("--record", default="",
                       help="write the digest record JSON here")
    check.add_argument("--format", default="text",
                       choices=("text", "json"))
    sub.add_parser("surface", parents=[inputs],
                   help="print the staging surface under review")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.command == "surface":
            return cmd_surface(args)
        return cmd_check(args)
    except Exception:
        # Exception messages and tracebacks can contain input var values.
        print("REFUSED: internal error", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
