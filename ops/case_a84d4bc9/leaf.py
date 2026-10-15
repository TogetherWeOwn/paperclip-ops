#!/usr/bin/env python3
"""case_672a252f contained closed-book leaf executor: one admitted cell, no inference without gates.

Binding: CTO contained-execution decision on case_0cff4afd
(`contained-execution-decision` document, CHANGES; owner tier-pin preserved;
shadow admission is not a grant). The independent re-check stays on case_fa736482;
do not create a second review card for this implementation.

What this module is: the deterministic, controller-side leaf of an already
admitted, tier-pinned review-child run. Frozen screened bundle in, bounded
Read/Grep-only tool worker, fixed CLIProxy transport, session seal,
controller-only result persistence. It enforces in code:

- binding projection: the child's company, child, assignee, native run,
  tier label, issue pin and evidence revision, plus ANTHROPIC_MODEL from
  THAT child's run. Exact string equality between run model and pin, and
  tier membership in the caller-supplied permitted set. HOLD (no inference)
  on missing pin, mismatch, ambiguous alias, excluded model, changed
  binding or unverifiable run ownership. No agent floor, tier guess,
  default, --models list or router re-selection fills a gap.
- admission re-check at EVERY request boundary (initial request, each tool-loop
  continuation, each transport retry) from fresh authoritative observations
  supplied by the caller. HOLD on missing/stale/denied. A shadow report,
  usage snapshot or prior observation is not a grant.
- finite limits declared below and enforced in code; one cell in flight
  (atomic claim file; released only after the session is sealed and the
  result persisted).
- Read and Grep only. No Glob capability is offered: the owner specified
  file read/grep and the decision forbids inferring Glob from the old
  three-tool audit allowlist. Filename discovery happens inside bounded Grep
  via `files_only`.
- CLIProxy-fixed transport with redirects refused. The credential is owned by
  the caller's transport closure and never enters this module: the leaf
  receives a `post` callable, never a secret, profile path or environment
  binding to hunt through. Model-facing tools have no network implementation.
- full byte capture (every transmitted prompt/tool-definition byte, every
  response segment, tool arguments/results, final output, identity evidence)
  with held-out byte/phrase screening on both directions. A hit is VOID.
  Missing/truncated/unparseable evidence is UNVERIFIED, never CLEAN.
  This module never scores: every record carries `"scoreable": false`.

Out of scope: scoring, ensembles, waves, any pin/label/permission/credential
change, normal-router workarounds, and any second cell while one is in flight.

Exit codes (CLI): 0 = OK sealed and persisted; 1 = VOID (containment/identity
violation, nothing scoreable); 2 = HOLD (no inference performed);
3 = UNVERIFIED (incomplete evidence). `--dry-check` performs binding,
admission, bundle and prompt screening only and never calls the transport.

The review prompt below derives from dispatch.PROMPT with the tool/transport
framing corrected to the contained boundary (Read/Grep only; result returned,
never posted). Its sha256 is bound into every execution record.
"""
import argparse
import base64
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import time
import urllib.request

import audit
import stage

# --------------------------------------------------------------------------
# Declared finite bounds. Charged against the whole cell, not the first call.
# --------------------------------------------------------------------------
MAX_TURNS = 8                # model turns per cell (tool-loop continuations included)
MAX_TOOL_CALLS = 24          # Read/Grep invocations per cell
MAX_PROMPT_BYTES = 2 * 1024 * 1024     # frozen view + diff text admitted per request
MAX_RESPONSE_BYTES = 128 * 1024       # accepted response segments per request
MAX_TOOL_RESULT_BYTES = 64 * 1024     # bytes returned to the model per tool call
MAX_READ_BYTES = 256 * 1024           # bytes read from one file per Read call
MAX_READ_LINES = 2000                 # lines returned per Read call
MAX_GREP_PATTERN_CHARS = 512          # regex source cap (no timeout op in `re`)
MAX_GREP_MATCHES = 200                # matches returned per Grep call
MAX_GREP_FILES_SCANNED = 5000         # files scanned per Grep call
MAX_TRACE_BYTES = 32 * 1024 * 1024    # captured evidence per cell
TIME_BUDGET_S = 600                   # wall clock per cell
MAX_REQUEST_ATTEMPTS = 3              # 1 initial + 2 transport retries, then stop
MAX_ADMISSION_AGE_S = 300             # fresh observation ceiling per boundary
TOOLSET = ("Read", "Grep")            # the only model-facing operations
EXPECTED_ROUTE = "cliproxy"           # the only authorized inference route
CONTROLLER_REVISION = "leaf-v1"

# The organic review task, reframed to the contained boundary. `Glob` is
# deliberately absent: filename discovery is a bounded Grep mode.
LEAF_PROMPT = """You are reviewing one pull request as the independent reviewer. You did not write it.

Inputs, all read-only, in the review directory:
- `reviewer_view.json`: the PR title and description.
- `pr.diff`: the change under review (base to head).
- `tree/`: the repository checked out at the PR head. Use Read and Grep to follow the
  change into code it calls or is called by. Grep with files_only lists matching file
  names when you need to discover files. Do not run anything.

Decide whether this change is safe to merge. Look for defects a careful maintainer would
block on: wrong or inverted logic, missing or weakened checks, security and data-exposure
problems, error handling that hides failure, off-by-one and boundary mistakes, units, state
and concurrency mistakes, and behaviour that contradicts the PR's own description.

Rules:
- Report only defects you can point to in the code. Cite the file and the line in `tree/`.
- Severity: P0 = data loss, security breach or outage; P1 = incorrect behaviour a user or
  operator will hit; P2 = minor or latent. Do not report style, naming or missing tests.
- If the change is sound, say so with an empty findings list. A clean review is a valid answer.
- Do not invent problems to look thorough.
- Your only tools are Read and Grep on paths inside the review directory. No other tool
  exists; do not ask for one.

Finish with one JSON object and nothing after it:

{"verdict": "APPROVE" | "CHANGES",
 "findings": [{"file": "<path in tree/>", "line": <int>, "severity": "P0"|"P1"|"P2",
               "title": "<one line>", "rationale": "<why it is wrong, one short paragraph>"}]}

`verdict` is CHANGES when any finding is P0 or P1, otherwise APPROVE."""


class Hold(Exception):
    """Binding or admission refused: stop with NO inference performed."""


class Void(Exception):
    """Containment or identity violation: nothing from this cell is usable."""


class Unverified(Exception):
    """Evidence incomplete: cannot attest to what the cell did."""


class TransientError(Exception):
    """Transport-level retryable failure (never a model verdict)."""


# --------------------------------------------------------------------------
# Binding, admission, transport contracts (caller-supplied facts only)
# --------------------------------------------------------------------------

def controller_sha():
    """sha256 of this source file: binds the record to the exact code that ran."""
    return hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()


class Binding:
    """Projected facts of ONE admitted review-child run. All strings exact."""

    def __init__(self, company_id, child_id, assignee_agent_id, native_run_id,
                 cell_id, tier_label, permitted_tiers, issue_pin, pin_revision,
                 run_model, excluded_models=(), canonical_aliases=None):
        self.company_id = company_id
        self.child_id = child_id
        self.assignee_agent_id = assignee_agent_id
        self.native_run_id = native_run_id
        self.cell_id = cell_id
        self.tier_label = tier_label
        self.permitted_tiers = tuple(permitted_tiers)
        self.issue_pin = issue_pin
        self.pin_revision = pin_revision
        self.run_model = run_model
        self.excluded_models = tuple(excluded_models)
        # Already-authorized canonical alias mappings, recorded privately by the
        # caller. Empty by default; an entry never authorizes a different model,
        # it only names an ambiguity that would otherwise be silent.
        self.canonical_aliases = dict(canonical_aliases or {})

    @classmethod
    def from_json(cls, raw):
        data = json.loads(raw) if isinstance(raw, str) else raw
        required = ("company_id", "child_id", "assignee_agent_id", "native_run_id",
                    "cell_id", "tier_label", "permitted_tiers", "issue_pin",
                    "pin_revision", "run_model")
        for key in required:
            if not data.get(key):
                raise Hold("binding %s missing or empty" % key)
        return cls(*[data[k] for k in required],
                   excluded_models=data.get("excluded_models", ()),
                   canonical_aliases=data.get("canonical_aliases"))

    def check(self):
        """Exact-match binding gate. Returns an immutable record or raises Hold."""
        if self.run_model != self.issue_pin:
            mapped = self.canonical_aliases.get(self.run_model)
            if mapped is None:
                raise Hold("run model does not equal issue pin")
            # An alias entry never bridges two different models: whether it
            # agrees or not, the binding is ambiguous without separate
            # authorization evidence, so hold either way.
            raise Hold("ambiguous model alias: %r maps to %r" % (self.run_model, mapped))
        if self.run_model in self.canonical_aliases.values():
            raise Hold("pin reached through an alias mapping; exact binding required")
        if self.tier_label not in self.permitted_tiers:
            raise Hold("tier label not in permitted set")
        if self.run_model in self.excluded_models:
            raise Hold("bound model is excluded")
        return {"company_id": self.company_id, "child_id": self.child_id,
                "assignee_agent_id": self.assignee_agent_id,
                "native_run_id": self.native_run_id, "cell_id": self.cell_id,
                "tier_label": self.tier_label, "issue_pin": self.issue_pin,
                "pin_revision": self.pin_revision, "run_model": self.run_model}


class Admission:
    """One fresh authoritative allowance observation. All gates must be true."""

    GATES = ("weekly_ok", "short_window_ok", "cooldowns_ok", "kill_rules_ok",
             "context_ok", "effort_ok", "account_ok", "pool_ok")

    def __init__(self, observed_at, source_revision, **gates):
        self.observed_at = observed_at
        self.source_revision = source_revision
        self.gates = {gate: bool(gates.get(gate, False)) for gate in self.GATES}

    @classmethod
    def from_json(cls, raw):
        data = json.loads(raw) if isinstance(raw, str) else raw
        try:
            observed_at = float(data["observed_at"])
        except (KeyError, TypeError, ValueError) as e:
            raise Hold("admission observation missing timestamp") from e
        if not data.get("source_revision"):
            raise Hold("admission observation missing source revision")
        return cls(observed_at, data["source_revision"],
                   **{gate: data.get(gate, False) for gate in cls.GATES})

    def check(self, now):
        age = now - self.observed_at
        if age < 0 or age > MAX_ADMISSION_AGE_S:
            raise Hold("admission observation stale (age %.1fs)" % age)
        denied = [gate for gate, ok in self.gates.items() if not ok]
        if denied:
            raise Hold("admission denied: %s" % ",".join(sorted(denied)))
        return {"observed_at": self.observed_at, "source_revision": self.source_revision,
                "gates": dict(self.gates)}


def cliproxy_transport(endpoint, post_fn=None):
    """Build the fixed-route transport. Redirects and route changes refused.

    The credential lives in `post_fn`'s closure (caller's grant); this module
    never receives it and records only redacted response metadata. `endpoint`
    must be the existing authorized CLIProxy URL passed explicitly by the
    caller; nothing is discovered from profiles, files or other environment.
    """
    if not endpoint.startswith("https://"):
        raise Hold("CLIProxy endpoint must be an explicit https URL")
    post = post_fn or _default_post

    def send(payload):
        if payload.get("route") != EXPECTED_ROUTE:
            raise Void("transport route is not the authorized CLIProxy route")
        try:
            return post(endpoint, payload)
        except TransientError:
            raise
        except (OSError, ValueError) as e:
            raise TransientError(str(e)) from e

    return send


def _default_post(endpoint, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json"},
        method="POST")
    # The caller-provided opener carries the grant; this module adds none and
    # follows no redirect (a redirect to another destination is a route
    # change, not a convenience).
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(req, timeout=120) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (429, 502, 503, 504):
            raise TransientError("HTTP %s" % e.code) from e
        raise
    if len(raw) > MAX_RESPONSE_BYTES:
        raise Unverified("response exceeds declared byte bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as e:
        raise Unverified("response is not parseable JSON") from e
    if not isinstance(data, dict):
        raise Unverified("response envelope is not an object")
    return data


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Void("CLIProxy transport refused a redirect")


# --------------------------------------------------------------------------
# Bounded in-process tool worker: Read and Grep only, inside one cell root
# --------------------------------------------------------------------------

class ToolRefusal(Exception):
    """Well-formed tool error returned to the model (charged, not terminal)."""


def _canonical_root(root):
    real = pathlib.Path(root)
    if real.is_symlink() or not real.is_dir():
        raise Void("cell root is not an ordinary directory")
    try:
        return real.resolve(strict=True)
    except OSError as e:
        raise Void("cell root does not resolve") from e


def _contained_file(root_real, path):
    """Absolute path inside the cell, opened symlink-safe, verified regular."""
    if not isinstance(path, str) or not pathlib.Path(path).is_absolute():
        raise Void("tool path is not absolute: %r" % (path,))
    if ".." in pathlib.PurePosixPath(path).parts:
        raise Void("tool path escapes the cell: %r" % (path,))
    try:
        resolved = pathlib.Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise Void("tool path does not resolve inside the cell") from e
    try:
        if not resolved.is_relative_to(root_real):
            raise Void("tool path escapes the cell")
    except OSError as e:
        raise Void("tool path containment unverifiable") from e
    # O_NOFOLLOW + fstat closes a symlink swap between resolve and open.
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise Void("tool path is a link or unreadable") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Void("tool path is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)
    except OSError as e:
        raise Void("tool path read failed") from e
    if len(raw) > MAX_READ_BYTES:
        raise ToolRefusal("file exceeds the %d-byte read bound; page with offset/limit"
                          % MAX_READ_BYTES)
    if b"\0" in raw:
        raise ToolRefusal("binary asset is omitted, never silently skipped")
    try:
        return raw.decode("utf-8")
    except UnicodeError as e:
        raise ToolRefusal("non-UTF-8 asset is omitted, never silently skipped") from e


class ToolWorker:
    """Implements exactly Read and Grep over one immutable cell directory."""

    def __init__(self, root):
        self.root_real = _canonical_root(root)
        self.calls = 0

    def dispatch(self, name, args):
        if name not in TOOLSET:
            # A model-requested foreign tool is a boundary breach, not a typo.
            raise Void("forbidden tool requested: %r" % (name,))
        if not isinstance(args, dict):
            raise ToolRefusal("tool arguments must be an object")
        self.calls += 1
        if self.calls > MAX_TOOL_CALLS:
            raise Unverified("tool-call budget exhausted")
        if name == "Read":
            return self.do_read(args)
        return self.do_grep(args)

    def do_read(self, args):
        path = args.get("file_path")
        offset = args.get("offset", 0)
        limit = args.get("limit", MAX_READ_LINES)
        for value, label in ((offset, "offset"), (limit, "limit")):
            if type(value) is not int or value < 0:
                raise ToolRefusal("%s must be a non-negative integer" % label)
        if limit == 0 or limit > MAX_READ_LINES:
            raise ToolRefusal("limit must be within 1..%d" % MAX_READ_LINES)
        text = _contained_file(self.root_real, path)
        lines = text.splitlines()
        page = lines[offset:offset + limit]
        out = "\n".join(page)
        if len(out.encode()) > MAX_TOOL_RESULT_BYTES:
            raise ToolRefusal("page exceeds the result byte bound; narrow offset/limit")
        return {"path": path, "offset": offset, "truncated": offset + limit < len(lines),
                "content": out}

    def do_grep(self, args):
        pattern = args.get("pattern", "")
        subdir = args.get("path", str(self.root_real))
        files_only = args.get("files_only", False)
        include = args.get("include", "*")
        if not isinstance(pattern, str) or not pattern:
            raise ToolRefusal("pattern must be a non-empty string")
        if len(pattern) > MAX_GREP_PATTERN_CHARS:
            raise ToolRefusal("pattern exceeds the %d-char bound" % MAX_GREP_PATTERN_CHARS)
        if not isinstance(include, str) or include.startswith("/") or ".." in include.split("/"):
            raise Void("escaping Grep include pattern")
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolRefusal("invalid regex: %s" % e) from e
        if not isinstance(subdir, str) or not pathlib.Path(subdir).is_absolute():
            raise Void("Grep path is not absolute")
        try:
            sub_real = pathlib.Path(subdir).resolve(strict=True)
        except (OSError, RuntimeError) as e:
            raise Void("Grep path does not resolve inside the cell") from e
        if not sub_real.is_relative_to(self.root_real):
            raise Void("Grep path escapes the cell")
        matches, scanned, stopped = [], 0, False
        for rel, entry, is_dir in _walk_files(sub_real):
            if is_dir:
                continue
            if not fnmatch.fnmatch(pathlib.PurePosixPath(rel).name, include):
                continue
            scanned += 1
            if scanned > MAX_GREP_FILES_SCANNED:
                stopped = True
                break
            try:
                raw = entry.read_bytes()
            except OSError as e:
                raise Void("Grep cannot read a bundle entry") from e
            if len(raw) > MAX_READ_BYTES or b"\0" in raw:
                continue  # over-bound/binary assets are omitted, counted in trace
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append({"file": rel, "line": lineno,
                                    "text": None if files_only else line[:500]})
                    if len(matches) >= MAX_GREP_MATCHES:
                        stopped = True
                        break
            if stopped:
                break
        if files_only:
            seen, files = set(), []
            for match in matches:
                if match["file"] not in seen:
                    seen.add(match["file"])
                    files.append(match["file"])
            payload = {"files": files}
        else:
            payload = {"matches": matches}
        payload.update(scanned_files=scanned, capped=stopped)
        if len(json.dumps(payload).encode()) > MAX_TOOL_RESULT_BYTES:
            raise ToolRefusal("Grep result exceeds the byte bound; narrow the pattern")
        return payload


def _walk_files(root_real):
    """Yield (cell-relative name, Path, is_dir); links and specials are VOID."""
    try:
        for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
            for name in sorted(dirnames + filenames):
                entry = pathlib.Path(dirpath) / name
                try:
                    mode = entry.lstat().st_mode
                except OSError as e:
                    raise Void("Grep cannot inspect a bundle entry") from e
                is_dir = stat.S_ISDIR(mode)
                if stat.S_ISLNK(mode) or not (is_dir or stat.S_ISREG(mode)):
                    raise Void("symlink or special entry under the tool root")
                yield entry.relative_to(root_real).as_posix(), entry, is_dir
    except OSError as e:
        raise Void("Grep cannot enumerate the tool root") from e


# --------------------------------------------------------------------------
# Trace capture, screening, identity, seal, persistence
# --------------------------------------------------------------------------

class Trace:
    """Byte-complete evidence log. Credential-bearing bytes never enter here:
    the leaf never receives a credential, and headers live in the caller's
    transport closure, outside this recorder."""

    def __init__(self, heldout_regex=(), heldout_phrases=()):
        self.events = []
        self.bytes = 0
        self.heldout_regex = list(heldout_regex or [])
        self.heldout_phrases = list(heldout_phrases or [])

    def _note(self, direction, payload):
        if isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.screen(raw)
        self.bytes += len(raw)
        if self.bytes > MAX_TRACE_BYTES:
            raise Unverified("trace exceeds its declared byte bound")
        self.events.append({"direction": direction,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "bytes": len(raw),
                            "body": base64.b64encode(raw).decode("ascii")})

    def sent(self, payload):
        self._note("sent", payload)

    def received(self, payload):
        self._note("received", payload)

    def tool(self, name, args, result):
        self._note("tool", {"name": name, "args": args, "result": result})

    def screen(self, raw):
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            text = raw.decode("utf-8", "replace")
        if (stage.screen_text(text, self.heldout_regex)
                or stage.screen_text(text, self.heldout_phrases, fixed=True)):
            raise Void("held-out string or identifier in captured bytes")


def verify_identity(response, binding_record):
    """Observable served-model/route check. A gateway echo of the requested
    name is not backend identity: the response must carry an explicit served
    identity block. Missing = UNVERIFIED; disagreement = VOID."""
    identity = response.get("model_identity")
    if not isinstance(identity, dict):
        raise Unverified("response carries no model-identity evidence")
    served = identity.get("served")
    route = identity.get("route")
    if not served or not route:
        raise Unverified("model-identity evidence is incomplete")
    if route != EXPECTED_ROUTE:
        raise Void("response arrived over an unauthorized route")
    if served != binding_record["run_model"] or served != binding_record["issue_pin"]:
        raise Void("served model does not match the bound pin")
    return {"served": served, "route": route}


TOOL_DEFINITIONS = [
    {"name": "Read",
     "description": "Read a UTF-8 text file inside the review directory.",
     "input_schema": {"file_path": "absolute path under the review directory",
                      "offset": "first line (0-based)", "limit": "max lines"}},
    {"name": "Grep",
     "description": "Search review-directory text for a regex; files_only lists names.",
     "input_schema": {"pattern": "regex source", "path": "absolute directory",
                      "files_only": "bool", "include": "filename glob"}},
]


def build_prompt(bundle_dir):
    """Frozen screened inputs as the single reviewer prompt (bytes + hashes).

    The absolute tree path is part of the prompt: the model needs it to form
    absolute Read/Grep paths, and the screening gate below covers it.
    """
    cell = pathlib.Path(bundle_dir)
    view = (cell / "reviewer_view.json").read_text(encoding="utf-8")
    diff = (cell / "pr.diff").read_text(encoding="utf-8")
    prompt = (LEAF_PROMPT + "\n\n## review directory\n" + str(cell / "tree")
              + "\n\n## reviewer_view.json\n" + view
              + "\n\n## pr.diff\n" + diff)
    raw = prompt.encode("utf-8")
    if len(raw) > MAX_PROMPT_BYTES:
        raise Hold("frozen inputs exceed the declared prompt byte bound")
    return raw, {"prompt_sha256": hashlib.sha256(
        LEAF_PROMPT.encode()).hexdigest(), "request_sha256": hashlib.sha256(raw).hexdigest()}


def claim_flight(ledger_dir, cell_id, record_id):
    """One cell in flight: atomic claim, held until seal+persist releases it."""
    ledger = pathlib.Path(ledger_dir)
    ledger.mkdir(parents=True, exist_ok=True)
    claim = ledger / (cell_id + ".claim")
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as e:
        raise Hold("another cell is already in flight") from e
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps({"cell_id": cell_id, "record_id": record_id}) + "\n")
    return claim


def run_cell(binding, admission_fn, transport, bundle_dir, out_path, trace_path,
             ledger_dir, heldout_regex=(), heldout_phrases=(), dry_check=False):
    """Execute one admitted cell. Returns (exit_code, record dict).

    Raises nothing terminal to the caller: HOLD/VOID/UNVERIFIED are returned
    as sealed records with exit codes 2/1/3. `transport` is only invoked
    after binding, bundle, admission and prompt gates all pass.
    """
    record_id = "leaf-" + hashlib.sha256(
        (binding.child_id + binding.cell_id + str(time.time())).encode()).hexdigest()[:12]
    record = {"schema": "closed-book-leaf-v1", "record_id": record_id,
              "controller_revision": CONTROLLER_REVISION,
              "controller_sha256": controller_sha(),
              "toolset": list(TOOLSET), "scoreable": False,
              "limits": {"turns": MAX_TURNS, "tool_calls": MAX_TOOL_CALLS,
                         "prompt_bytes": MAX_PROMPT_BYTES,
                         "response_bytes": MAX_RESPONSE_BYTES,
                         "tool_result_bytes": MAX_TOOL_RESULT_BYTES,
                         "trace_bytes": MAX_TRACE_BYTES,
                         "time_budget_s": TIME_BUDGET_S,
                         "request_attempts": MAX_REQUEST_ATTEMPTS,
                         "admission_age_s": MAX_ADMISSION_AGE_S},
              "admissions": [], "requests": 0, "turns": 0, "tool_calls": 0}
    claim, trace = None, Trace(heldout_regex, heldout_phrases)
    deadline = time.monotonic() + TIME_BUDGET_S

    def persist(status, result=None, terminal_note=None):
        record.update(status=status, sealed=True, result=result,
                      trace_sha256=hashlib.sha256(
                          json.dumps(trace.events, sort_keys=True).encode()).hexdigest(),
                      trace_bytes=trace.bytes)
        if terminal_note:
            record["terminal_note"] = terminal_note
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        tmp.replace(out)
        traced = pathlib.Path(trace_path)
        traced.parent.mkdir(parents=True, exist_ok=True)
        tmp_trace = traced.with_suffix(".tmp")
        tmp_trace.write_text(json.dumps(trace.events) + "\n")
        tmp_trace.replace(traced)
        if claim is not None:
            claim.unlink(missing_ok=True)
        return {"HOLD": 2, "VOID": 1, "UNVERIFIED": 3, "OK": 0}[status], record

    def admit():
        try:
            admission = admission_fn()
        except (OSError, ValueError) as e:
            raise Hold("admission observation unavailable: %s" % e) from e
        if not isinstance(admission, Admission):
            raise Hold("admission provider did not return an observation")
        record["admissions"].append(admission.check(time.time()))
        return admission

    try:
        record["binding"] = binding.check()
        manifest = stage.verify_bundle(binding.cell_id)
        record["bundle"] = {"cid": binding.cell_id,
                            "reviewed_head": manifest.get("reviewed_head"),
                            "manifest_sha256": hashlib.sha256(
                                stage.manifest_path(
                                    binding.cell_id).read_bytes()).hexdigest()}
        claim = claim_flight(ledger_dir, binding.cell_id, record_id)
        admit()
        prompt_raw, prompt_hashes = build_prompt(bundle_dir)
        record["prompt"] = prompt_hashes
        trace.screen(prompt_raw)
        trace.sent({"role": "system", "prompt_sha256": prompt_hashes["prompt_sha256"],
                    "tools": TOOL_DEFINITIONS})
        trace.sent({"role": "user", "request_sha256": prompt_hashes["request_sha256"]})
        if dry_check:
            return persist("HOLD", terminal_note="dry-check: gates pass, no inference")
    except Hold as e:
        return persist("HOLD", terminal_note=str(e))
    except Void as e:
        return persist("VOID", terminal_note=str(e))
    except (OSError, ValueError) as e:
        return persist("UNVERIFIED", terminal_note="preflight evidence unreadable: %s" % e)

    worker = ToolWorker(pathlib.Path(bundle_dir) / "tree")
    messages = [{"role": "user", "content": prompt_raw.decode("utf-8")}]
    final_text = None

    try:
        for _turn in range(MAX_TURNS):
            if time.monotonic() > deadline:
                raise Unverified("cell time budget exhausted")
            admit()  # every request boundary re-checks fresh admission
            payload = {"route": EXPECTED_ROUTE, "model": binding.run_model,
                       "system_sha256": prompt_hashes["prompt_sha256"],
                       "tools": TOOL_DEFINITIONS, "messages": messages}
            trace.sent({"role": "request", "model": binding.run_model,
                        "message_bytes": len(json.dumps(messages).encode())})
            response = None
            last_error = "no attempt made"
            for _attempt in range(MAX_REQUEST_ATTEMPTS):
                record["requests"] += 1
                try:
                    response = transport(payload)
                    break
                except TransientError as e:
                    last_error = str(e)
                    if _attempt + 1 >= MAX_REQUEST_ATTEMPTS:
                        break
                    admit()  # retries are request boundaries too
            if response is None:
                trace.received({"transport_failed": last_error})
                raise Unverified("transport retries exhausted without a response")
            record["identity"] = verify_identity(response, record["binding"])
            segments = response.get("segments")
            if not isinstance(segments, list) or not segments or not all(
                    isinstance(seg, str) for seg in segments):
                raise Unverified("response has no complete text segments")
            text = "".join(segments)
            if response.get("complete") is not True:
                trace.received({"truncated": True, "bytes": len(text.encode())})
                raise Unverified("truncated response is not attestable evidence")
            trace.received({"text": text, "usage": response.get("usage", {})})
            record["turns"] += 1
            tool_calls = response.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise Unverified("tool_calls envelope is not a list")
            if not tool_calls:
                final_text = text
                break
            messages.append({"role": "assistant", "content": text,
                             "tool_calls": tool_calls})
            results = []
            for call in tool_calls:
                name = call.get("name")
                try:
                    result = worker.dispatch(name, call.get("input"))
                except ToolRefusal as e:
                    result = {"error": str(e)}
                record["tool_calls"] = worker.calls
                trace.tool(name, call.get("input"), result)
                results.append({"id": call.get("id"), "result": result})
            messages.append({"role": "tool_results", "results": results})
        else:
            raise Unverified("turn budget exhausted without a final answer")
        if final_text is None:
            raise Unverified("no final answer captured")
        trace.screen(final_text.encode("utf-8"))
        result = audit.check_result(final_text)
        if result is None:
            raise Unverified("final answer is not a valid run-bound result")
        return persist("OK", result=result)
    except Hold as e:
        return persist("HOLD", terminal_note=str(e))
    except Void as e:
        return persist("VOID", terminal_note=str(e))
    except Unverified as e:
        return persist("UNVERIFIED", terminal_note=str(e))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Contained closed-book leaf executor")
    parser.add_argument("--binding", required=True, help="JSON file: projected child-run facts")
    parser.add_argument("--admission", required=True,
                        help="JSON file: fresh allowance observation (re-read each boundary)")
    parser.add_argument("--bundle-dir", required=True, help="verified cell directory")
    parser.add_argument("--heldout", default="{}",
                        help='JSON {"regex": [...], "phrases": [...]} from the scorer')
    parser.add_argument("--out", required=True, help="sealed result record path")
    parser.add_argument("--trace", required=True, help="captured byte-trace path")
    parser.add_argument("--ledger", required=True, help="single-flight claim directory")
    parser.add_argument("--endpoint", help="explicit CLIProxy https URL (live transport)")
    parser.add_argument("--dry-check", action="store_true",
                        help="gates only; never call the transport")
    args = parser.parse_args(argv)

    try:
        binding = Binding.from_json(pathlib.Path(args.binding).read_text())
        heldout = json.loads(args.heldout)
    except Hold as e:
        print("HOLD: %s" % e)
        return 2
    except (OSError, ValueError) as e:
        print("UNVERIFIED: unreadable CLI input: %s" % e)
        return 3

    def admission_fn():
        return Admission.from_json(pathlib.Path(args.admission).read_text())

    def refused_transport(payload):
        raise AssertionError("transport invoked during a no-inference gate")

    transport = refused_transport
    if not args.dry_check:
        if not args.endpoint:
            print("refusing: live inference needs --endpoint and reviewed gates", file=sys.stderr)
            return 2
        transport = cliproxy_transport(args.endpoint)

    code, record = run_cell(
        binding, admission_fn, transport, args.bundle_dir, args.out, args.trace,
        args.ledger, heldout.get("regex", ()), heldout.get("phrases", ()),
        dry_check=args.dry_check)
    print(json.dumps({"status": record["status"], "cell": binding.cell_id,
                      "record": record["record_id"],
                      "note": record.get("terminal_note")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
