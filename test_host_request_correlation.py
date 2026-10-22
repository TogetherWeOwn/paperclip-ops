#!/usr/bin/env python3
"""Host-request issue/run correlation acceptance (EXPECTED-RED contract v1).

Public-safe, fully offline contract for host-request filing provenance: a
submission path that emits one structured issueId/runId pair per run-context
filing, and an age-alert consumer that resolves that pair to the filing issue
for the alert DESTINATION ONLY. No network, no credentials, no live queue, no
host paths: every case runs in a per-test temporary directory with fake
publication commands and synthetic identifiers.

EXPECTED-RED. This suite is red by design against its default implementation
under test (the synthetic known-bad submitter below, which mimics the
confirmed defect shape: validates only content, copies the input unchanged,
captures no provenance, and reports success unconditionally). Do NOT wire this
file into the fast gate or any required suite list until the paired Builder
card lands a conforming submission path; the red tests below are the Builder's
acceptance criteria, and wiring them early would red the whole gate. The
green controls prove the harness itself is honest (see RED/GREEN MAP).

IMPLEMENTATION SEAM (how the Builder turns this green without touching it):
  HOST_REQUEST_SUBMITTER=/path/to/submitter ... python3 -m unittest test_host_request_correlation
The pointed-to executable must honor the CALLER contract below. Unset (the
default) selects the synthetic known-bad fixture and the contract tests fail.
A set-but-missing path refuses loudly instead of silently passing.

CALLER CONTRACT v1 (submission path; the part the Builder implements):
  argv: submitter --queue-dir DIR --log-file FILE [--issue-id X] [--run-id Y]
                 [--provenance-manual] REQUEST.json
  env: PAPERCLIP_TASK_ID / PAPERCLIP_RUN_ID are the run context. Values must
       be UUID-shaped; anything else is malformed provenance.
  content: REQUEST.json must carry non-empty string what/why/rollback plus an
       optional string command. That validation is preserved exactly: a
       content refusal publishes nothing.
  run-context filing (env present, no conflicting flags): exit 0, exactly one
       new queue file, stdout carries `queued <basename>`, the log carries an
       `ok` line naming the basename, and the record preserves every content
       key byte-for-byte while adding exactly one structured block:
         "provenance": {"issueId": TASK, "runId": RUN, "source": "run"}
  explicit args equal to env: same outcome (no silent rewrite; values agree).
  conflict (env present and explicit args differ, partial, or malformed; env
       present with --provenance-manual): exit 2, stderr names the conflict,
       NO publication. Never silently replace one identity with another.
  missing run context (no env, no flags): exit 2 naming the missing run
       context, NO publication. Never invent a run identity, never bind
       another task, never file a silent orphan.
  malformed run context (non-UUID env): exit 2, NO publication. Never claim
       verified provenance from an arbitrary environment.
  caller-supplied pair without env (--issue-id/--run-id, no env): exit 2 as
       unverifiable provenance, NO publication. A caller-supplied pair is
       never trusted as verified identity; the manual path below is the
       explicit alternative.
  manual path (--provenance-manual, no env): exit 0 with an explicit record:
         "provenance": {"source": "manual"}
       No issueId/runId is invented. The consumer routes these to the default
       manual-review destination. This is the non-run submission policy:
       explicit, auditable, and never a silent orphan.
  publication truthfulness: validation/provenance refusal (exit 2) writes no
       queue file. A copy/owner/mode/log failure during publication exits 1
       with NO `queued` marker on stdout and NO `ok` marker in the log. Any
       output loss is not absence proof; no retry or cleanup is implied.
  auditability: stdout/stderr/log lines carry the record basename only (never
       absolute queue paths) and never echo secret-shaped environment values.

RECORD CONTRACT v1 (what the consumer reads):
  The queued record is the original content plus the single `provenance`
  object above. Classification derives ONLY from content keys, never from
  provenance. Field-name basis verified in public source at base 40194e6b:
  host-ops/broker.py pins requester keys {"agentId", "runId", "issueId"}, and
  the parent orphan-diagnosis card names structured `issueId` as the key the
  age-alert router needs. The installed helper, its queue directory layout,
  and the host age-alert router module itself are host-private and were NOT
  found in this repository (bounded search: no tracked submission carrier,
  no staging-executor classifier, no alert-router module); the Builder must
  confirm the exact consumer field path against the canonical consumer when
  the source repair furnishes it, and must NOT parse free-text card names
  into authority under any path shape.

CONSUMER CONTRACT v1 (reference behavior pinned in-test; the Builder does not
reimplement the router, but the end-to-end tests prove correlation changes the
alert destination only):
  route(record) -> {"destination", "classification", "authorized", "executed"}.
  A run-provenance record routes to issue:<issueId>; a manual record routes
  to manual-review; a record without structured provenance routes to
  unrouted. Classification stays exactly what the content says (unknown for
  non-staging content) regardless of provenance; authorized and executed are
  always False here (correlation metadata is routing data, never an
  authority grant); production-marked content is refused outright even with
  perfect provenance. Flipping provenance on identical content changes the
  destination and nothing else.

RED/GREEN MAP (default known-bad implementation):
  RED (contract; fail as assertion failures, not harness errors):
    run-context filing, explicit-args-agree, missing/malformed/conflicting/
    claimed/manual provenance outcomes, copy/owner/mode/log publication
    truthfulness, end-to-end routed-to-filing-issue.
  GREEN (controls; prove the harness is not vacuous):
    content validation refusals, optional-command preservation, orphan and
    manual routing pins, destination-only correlation pin, class-unknown and
    refuse-production negatives, secret/path audit guards, known-bad
    documentation pin, seam load-bearing pin.

Style follows test_alert_queue.py (stdlib unittest, fixture builders, one
behavior per test). Synthetic identifiers only; nothing here names a real
card, host path, credential, or live request.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Synthetic fixtures (never real identities, cards, or requests).
# ---------------------------------------------------------------------------

SYN_TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SYN_RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_TASK_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_RUN_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SYN_STAMP = "20260101T000000Z"  # fixed synthetic stamp; not a live request date
CANARY_SECRET = "synthetic-canary-token-for-log-scrub-test"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
RECORD_NAME_RE = re.compile(r"^req-\d{8}T\d{6}Z-[0-9a-f]+\.json$")
QUEUED_RE = re.compile(r"^queued (\S+)$", re.M)
OK_MARK_RE = re.compile(r"\bok\b")

SUBMITTER_ENV_VAR = "HOST_REQUEST_SUBMITTER"

# The request content keys whose validation this contract preserves.
CONTENT_KEYS = ("what", "why", "rollback")


def scratch_dir():
    """Per-run scratch root when the harness provides one, else system tmp."""
    return (os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
            or os.environ.get("PAPERCLIP_SCRATCH_DIR") or None)


# ---------------------------------------------------------------------------
# Synthetic known-bad submitter.
#
# A behavioral stand-in for the confirmed defect shape: validates only
# what/why/rollback plus optional command, copies the input unchanged,
# captures no provenance, and reports queued/ok even when a publication step
# fails. It is written fresh from this string for every test; it is NOT the
# installed bytes and contains no host path (queue dir, log file, and owner
# arrive via argv/env). Publication goes through PATH-resolved cp/chown/chmod
# so failure injection uses fake commands, never the live filesystem.
# ---------------------------------------------------------------------------

KNOWN_BAD_SUBMITTER = """\
#!/usr/bin/env python3
import json, os, random, subprocess, sys

def err(msg):
    print(f"synth-submitter: {msg}", file=sys.stderr)

def run(tool, *argv):
    try:
        proc = subprocess.run([tool, *argv], capture_output=True, text=True)
    except OSError as exc:
        err(f"{tool} failed to start: {exc}")
        return 1
    if proc.returncode != 0:
        err(f"{tool} failed: {(proc.stderr or '').strip()}")
        return proc.returncode
    return 0

def main(argv):
    queue = None
    log = None
    src = None
    args = list(argv)
    # Known-bad: provenance flags/env are accepted-and-ignored. The defect is
    # that NO provenance is captured, so swallowing these arguments without
    # effect is the honest mimic (a refusal here would be stricter than the
    # confirmed helper, which validates only content).
    while args:
        token = args.pop(0)
        if token == "--queue-dir":
            queue = args.pop(0)
        elif token == "--log-file":
            log = args.pop(0)
        elif token in ("--issue-id", "--run-id"):
            args.pop(0)
        elif token.startswith("--"):
            pass
        elif src is None:
            src = token
        else:
            err("too many positional arguments")
            return 2
    if not queue or not log or not src:
        err("usage: submitter --queue-dir DIR --log-file FILE REQUEST.json")
        return 2
    try:
        with open(src, encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, ValueError) as exc:
        err(f"unreadable request: {exc}")
        return 2
    for key in ("what", "why", "rollback"):
        if not isinstance(request.get(key), str) or not request[key].strip():
            err(f"invalid content: {key} must be a non-empty string")
            return 2
    if "command" in request and not isinstance(request["command"], str):
        err("invalid content: command must be a string")
        return 2
    name = "req-%s-%s.json" % ("STAMP", "%06x" % random.getrandbits(24))
    dest = os.path.join(queue, name)
    owner = os.environ.get("HOST_REQUEST_OWNER", "")
    # Known-bad publication: failures are observed (stderr) but the success
    # echoes below run anyway -- the false-success shape.
    run("cp", "--", src, dest)
    if owner:
        run("chown", owner, dest)
    run("chmod", "0640", dest)
    try:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("STAMP synth-submitter: host-request ok dest=%s\\n" % name)
    except OSError as exc:
        err(f"log append failed: {exc}")
    print("queued %s" % name)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
""".replace("STAMP", SYN_STAMP)


class MissingSubmitterError(FileNotFoundError):
    """The configured submitter path does not exist: refuse, never pass."""


def resolve_submitter(env):
    """Return the implementation under test for this run.

    HOST_REQUEST_SUBMITTER set -> that exact executable (missing paths
    refuse loudly). Unset -> None, and the caller installs the synthetic
    known-bad fixture (the default EXPECTED-RED implementation).
    """
    configured = env.get(SUBMITTER_ENV_VAR)
    if not configured:
        return None
    if not os.path.isfile(configured):
        raise MissingSubmitterError(
            f"{SUBMITTER_ENV_VAR}={configured} does not exist; refusing to "
            "run the contract against an absent implementation")
    return configured


# ---------------------------------------------------------------------------
# Reference consumer (pins CONSUMER CONTRACT v1; not the Builder's target).
# ---------------------------------------------------------------------------

class ConsumerRefusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# Synthetic content classes. Only content keys feed classification; provenance
# never does. Production-marked content refuses no matter the provenance.
STAGING_CLASSES = {"staging-deploy", "staging-restart"}


def classify_content(record):
    """Classify from content keys only. Provenance is never an input."""
    cls = record.get("requestClass", "unknown")
    if cls == "production":
        raise ConsumerRefusal(
            "prod_target_refused",
            "production target refused; correlation never authorizes it")
    if cls in STAGING_CLASSES:
        return cls
    return "unknown"


def route_alert(record):
    """Resolve the alert destination from STRUCTURED provenance only.

    Returns destination/classification/authorized/executed. Free-text card
    names are never parsed: without a structured provenance block the record
    is unrouted. Correlation metadata is routing data, never authority, so
    authorized/executed are always False here.
    """
    classification = classify_content(record)
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return {"destination": "unrouted", "classification": classification,
                "authorized": False, "executed": False,
                "provenanceSource": None}
    source = provenance.get("source")
    issue_id = provenance.get("issueId")
    if source == "run" and isinstance(issue_id, str) and issue_id:
        destination = f"issue:{issue_id}"
    elif source == "manual":
        destination = "manual-review"
    else:
        destination = "unrouted"
    return {"destination": destination, "classification": classification,
            "authorized": False, "executed": False,
            "provenanceSource": source}


# ---------------------------------------------------------------------------
# Harness.
# ---------------------------------------------------------------------------

FAKE_TOOL = """\
#!/usr/bin/env python3
import sys
print("fake-%s: injected failure" % TOOL, file=sys.stderr)
sys.exit(1)
"""


def write_executable(path, text):
    path = Path(path)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class CorrelationCase(unittest.TestCase):
    """One temp queue + log + request per test; PATH fakes inject failures."""

    def setUp(self):
        for tool in ("python3", "cp", "chown", "chmod"):
            self.assertIsNotNone(shutil.which(tool),
                                 f"required tool missing: {tool}")
        temp = tempfile.TemporaryDirectory(prefix="host-req-correlation-",
                                           dir=scratch_dir())
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.queue = self.work / "queue"
        self.queue.mkdir()
        self.log = self.work / "submit.log"
        self.bin = self.work / "bin"
        self.bin.mkdir()
        # Base environment: sealed from the ambient run except for the
        # explicit run-context and seam variables each test sets. The canary
        # proves audit lines never echo secret-shaped values.
        self.env = {"PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
                    "HOME": str(self.work),
                    "TMPDIR": str(self.work),
                    "SYNTHETIC_CANARY": CANARY_SECRET}
        configured = resolve_submitter(os.environ)
        if configured is None:
            self.submitter = write_executable(self.work / "known-bad-submitter",
                                              KNOWN_BAD_SUBMITTER)
            self.using_known_bad = True
        else:
            self.submitter = configured
            self.using_known_bad = False

    # -- fixtures ------------------------------------------------------

    def write_request(self, name="request.json", **content):
        body = {"what": "synthetic filing",
                "why": "contract exercise",
                "rollback": "delete the synthetic record"}
        body.update(content)
        path = self.work / name
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        return str(path)

    def install_fake_tools(self, *tools):
        for tool in tools:
            write_executable(self.bin / tool,
                             FAKE_TOOL.replace("TOOL", repr(tool)))

    def current_owner(self):
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name

    # -- invocation ----------------------------------------------------

    def submit(self, request_path, *extra_args, task_id=None, run_id=None,
               extra_env=None):
        env = dict(self.env)
        env["HOST_REQUEST_OWNER"] = self.current_owner()
        if task_id is not None:
            env["PAPERCLIP_TASK_ID"] = task_id
        if run_id is not None:
            env["PAPERCLIP_RUN_ID"] = run_id
        if SUBMITTER_ENV_VAR in os.environ:
            env[SUBMITTER_ENV_VAR] = os.environ[SUBMITTER_ENV_VAR]
        if extra_env:
            env.update(extra_env)
        argv = [self.submitter, "--queue-dir", str(self.queue),
               "--log-file", str(self.log), *extra_args, request_path]
        return subprocess.run(argv, env=env, cwd=self.work, capture_output=True,
                              text=True, timeout=30)

    def queued_records(self):
        return sorted(self.queue.glob("req-*.json"))

    def read_record(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def log_text(self):
        # The log-failure case replaces the log file with a directory; an
        # unreadable log path reads as empty (no ok marker can be present).
        if not self.log.is_file():
            return ""
        return self.log.read_text(encoding="utf-8")

    # -- shared assertions ----------------------------------------------

    def assertRefusedWithoutPublication(self, proc, *, returncode=2,
                                        stderr_match=None):
        self.assertEqual(proc.returncode, returncode,
                         f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        if stderr_match is not None:
            self.assertRegex(proc.stderr, stderr_match,
                             f"refusal must say why: {proc.stderr!r}")
        self.assertEqual(self.queued_records(), [],
                         "a refusal must publish no queue file")
        self.assertNotRegex(proc.stdout, r"\bqueued\b",
                            "a refusal must not print a queued marker")
        self.assertNotRegex(self.log_text(), OK_MARK_RE,
                            "a refusal must not log an ok marker")

    def assertNoSuccessMarkers(self, proc):
        self.assertNotEqual(proc.returncode, 0,
                            f"publication failure must be nonzero: "
                            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertNotRegex(proc.stdout, r"\bqueued\b",
                            "failed publication must not print queued")
        self.assertNotRegex(self.log_text(), OK_MARK_RE,
                            "failed publication must not log ok")

    def assertAuditClean(self, proc):
        for label, text in (("stdout", proc.stdout), ("stderr", proc.stderr),
                            ("log", self.log_text())):
            self.assertNotIn(CANARY_SECRET, text,
                             f"canary secret leaked into {label}")
            self.assertNotIn(str(self.work), text,
                             f"absolute workspace path leaked into {label}")


# ---------------------------------------------------------------------------
# A. Run-context filing: one consistent structured pair (RED by design).
# ---------------------------------------------------------------------------

class RunContextProvenanceTest(CorrelationCase):
    def test_run_context_filing_emits_one_structured_pair(self):
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        match = QUEUED_RE.search(proc.stdout)
        self.assertIsNotNone(match, f"no queued line: {proc.stdout!r}")
        records = self.queued_records()
        self.assertEqual(len(records), 1,
                         "one filing emits exactly one queue file")
        self.assertEqual(match.group(1), records[0].name,
                         "queued marker must name the published record")
        record = self.read_record(records[0])
        # Content preserved byte-for-byte; provenance added as one block.
        for key in CONTENT_KEYS:
            self.assertEqual(record.get(key),
                             json.loads(Path(src).read_text())[key])
        self.assertEqual(record.get("provenance"),
                         {"issueId": SYN_TASK_ID, "runId": SYN_RUN_ID,
                          "source": "run"})
        routed = route_alert(record)
        self.assertEqual(routed["destination"], f"issue:{SYN_TASK_ID}")
        self.assertAuditClean(proc)

    def test_explicit_args_matching_env_are_accepted_unchanged(self):
        src = self.write_request()
        proc = self.submit(src, "--issue-id", SYN_TASK_ID,
                           "--run-id", SYN_RUN_ID,
                           task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.queued_records()
        self.assertEqual(len(records), 1)
        record = self.read_record(records[0])
        self.assertEqual(record.get("provenance"),
                         {"issueId": SYN_TASK_ID, "runId": SYN_RUN_ID,
                          "source": "run"})


# ---------------------------------------------------------------------------
# B. Missing / malformed / conflicting provenance (RED by design).
# ---------------------------------------------------------------------------

class ProvenanceRefusalTest(CorrelationCase):
    def test_missing_run_context_refuses_without_orphan(self):
        src = self.write_request()
        proc = self.submit(src)
        self.assertRefusedWithoutPublication(proc, stderr_match=r"[Rr]un context")

    def test_malformed_run_context_refuses(self):
        for task_id, run_id in (("not-a-uuid", SYN_RUN_ID),
                                (SYN_TASK_ID, "not-a-uuid"),
                                ("", SYN_RUN_ID),
                                (SYN_TASK_ID, "")):
            with self.subTest(task_id=task_id, run_id=run_id):
                # Fresh queue per variant so one publication cannot mask
                # another variant's refusal.
                for stale in self.queued_records():
                    stale.unlink()
                src = self.write_request(name=f"malformed-{task_id!r}.json")
                proc = self.submit(src, task_id=task_id, run_id=run_id)
                self.assertRefusedWithoutPublication(
                    proc, stderr_match=r"[Pp]rovenance|[Uu][Uu]ID|[Rr]un context")

    def test_conflicting_explicit_args_refuse_without_replacement(self):
        variants = [
            ("different issue", ["--issue-id", OTHER_TASK_ID,
                                 "--run-id", SYN_RUN_ID]),
            ("different run", ["--issue-id", SYN_TASK_ID,
                               "--run-id", OTHER_RUN_ID]),
            ("partial pair", ["--issue-id", SYN_TASK_ID]),
            ("malformed override", ["--issue-id", "not-a-uuid",
                                    "--run-id", SYN_RUN_ID]),
        ]
        for label, extra in variants:
            with self.subTest(label):
                for stale in self.queued_records():
                    stale.unlink()
                src = self.write_request()
                proc = self.submit(src, *extra, task_id=SYN_TASK_ID,
                                   run_id=SYN_RUN_ID)
                self.assertRefusedWithoutPublication(
                    proc, stderr_match=r"[Cc]onflict|[Mm]ismatch|[Pp]rovenance")

    def test_manual_flag_with_run_context_refuses(self):
        src = self.write_request()
        proc = self.submit(src, "--provenance-manual",
                           task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertRefusedWithoutPublication(
            proc, stderr_match=r"[Cc]onflict|[Mm]anual")

    def test_caller_supplied_pair_without_env_is_unverifiable(self):
        src = self.write_request()
        proc = self.submit(src, "--issue-id", SYN_TASK_ID,
                           "--run-id", SYN_RUN_ID)
        self.assertRefusedWithoutPublication(
            proc, stderr_match=r"[Uu]nverifiable|[Pp]rovenance|[Rr]un context")

    def test_manual_flag_records_explicit_manual_provenance(self):
        src = self.write_request()
        proc = self.submit(src, "--provenance-manual")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.queued_records()
        self.assertEqual(len(records), 1)
        record = self.read_record(records[0])
        # Explicit manual handling: no invented identities, ever.
        self.assertEqual(record.get("provenance"), {"source": "manual"})
        self.assertNotIn("issueId", record.get("provenance"))
        self.assertNotIn("runId", record.get("provenance"))
        routed = route_alert(record)
        self.assertEqual(routed["destination"], "manual-review")
        self.assertAuditClean(proc)


# ---------------------------------------------------------------------------
# C. Preserved submission validation (GREEN controls).
# ---------------------------------------------------------------------------

class ValidationPreservedTest(CorrelationCase):
    def test_missing_content_key_refuses_without_publication(self):
        base = {"what": "synthetic filing",
                "why": "contract exercise",
                "rollback": "delete the synthetic record"}
        for missing in CONTENT_KEYS:
            with self.subTest(missing=missing):
                for stale in self.queued_records():
                    stale.unlink()
                body = {k: v for k, v in base.items() if k != missing}
                path = self.work / f"missing-{missing}.json"
                path.write_text(json.dumps(body) + "\n", encoding="utf-8")
                proc = self.submit(str(path), task_id=SYN_TASK_ID,
                                   run_id=SYN_RUN_ID)
                self.assertRefusedWithoutPublication(proc)

    def test_empty_content_value_refuses_without_publication(self):
        src = self.write_request(why="   ")
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertRefusedWithoutPublication(proc)

    def test_optional_command_is_preserved(self):
        src = self.write_request(command="synthetic --dry-run")
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.queued_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(self.read_record(records[0]).get("command"),
                         "synthetic --dry-run")

    def test_non_string_command_refuses(self):
        src = self.write_request(command=["not", "a", "string"])
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertRefusedWithoutPublication(proc)


# ---------------------------------------------------------------------------
# D. Publication truthfulness: no false queued/ok (RED by design).
# ---------------------------------------------------------------------------

class PublicationTruthfulnessTest(CorrelationCase):
    def test_copy_failure_is_not_queued_ok(self):
        self.install_fake_tools("cp")
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNoSuccessMarkers(proc)

    def test_owner_failure_is_not_queued_ok(self):
        self.install_fake_tools("chown")
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNoSuccessMarkers(proc)

    def test_mode_failure_is_not_queued_ok(self):
        self.install_fake_tools("chmod")
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNoSuccessMarkers(proc)

    def test_log_append_failure_is_not_queued_ok(self):
        # The log path is a directory: the append cannot succeed, and the
        # failure must be reported instead of echoed as ok.
        self.log.mkdir()
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNoSuccessMarkers(proc)


# ---------------------------------------------------------------------------
# E. Routing changes the destination only (consumer pins GREEN; e2e RED).
# ---------------------------------------------------------------------------

def synthetic_record(content_class="unknown", provenance=None):
    record = {"what": "synthetic filing",
              "why": "contract exercise",
              "rollback": "delete the synthetic record",
              "requestClass": content_class}
    if provenance is not None:
        record["provenance"] = provenance
    return record


class RoutingOnlyTest(CorrelationCase):
    def test_orphan_routes_unrouted_without_authority_or_execution(self):
        routed = route_alert(synthetic_record())
        self.assertEqual(routed["destination"], "unrouted")
        self.assertEqual(routed["classification"], "unknown")
        self.assertFalse(routed["authorized"])
        self.assertFalse(routed["executed"])

    def test_manual_record_routes_to_manual_review_only(self):
        routed = route_alert(synthetic_record(provenance={"source": "manual"}))
        self.assertEqual(routed["destination"], "manual-review")
        self.assertEqual(routed["classification"], "unknown")
        self.assertFalse(routed["authorized"])
        self.assertFalse(routed["executed"])

    def test_correlation_changes_destination_only(self):
        provenance = {"issueId": SYN_TASK_ID, "runId": SYN_RUN_ID,
                      "source": "run"}
        orphan = route_alert(synthetic_record())
        correlated = route_alert(synthetic_record(provenance=provenance))
        self.assertEqual(orphan["destination"], "unrouted")
        self.assertEqual(correlated["destination"], f"issue:{SYN_TASK_ID}")
        for key in ("classification", "authorized", "executed"):
            self.assertEqual(orphan[key], correlated[key],
                             f"correlation must not change {key}")

    def test_class_unknown_preserved_for_non_staging_content(self):
        provenance = {"issueId": SYN_TASK_ID, "runId": SYN_RUN_ID,
                      "source": "run"}
        routed = route_alert(synthetic_record(provenance=provenance))
        self.assertEqual(routed["classification"], "unknown")
        self.assertFalse(routed["authorized"])
        self.assertFalse(routed["executed"])

    def test_staging_class_comes_from_content_not_provenance(self):
        provenance = {"issueId": SYN_TASK_ID, "runId": SYN_RUN_ID,
                      "source": "run"}
        self.assertEqual(
            route_alert(synthetic_record("staging-deploy"))["classification"],
            "staging-deploy")
        self.assertEqual(
            route_alert(
                synthetic_record("staging-deploy",
                                 provenance=provenance))["classification"],
            "staging-deploy")

    def test_production_target_refused_despite_provenance(self):
        provenance = {"issueId": SYN_TASK_ID, "runId": SYN_RUN_ID,
                      "source": "run"}
        for record in (synthetic_record("production"),
                       synthetic_record("production", provenance=provenance)):
            with self.subTest(provenance=record.get("provenance")):
                with self.assertRaises(ConsumerRefusal) as raised:
                    route_alert(record)
                self.assertEqual(raised.exception.code, "prod_target_refused")

    def test_end_to_end_filing_routes_to_filing_issue(self):
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.queued_records()
        self.assertEqual(len(records), 1)
        routed = route_alert(self.read_record(records[0]))
        self.assertEqual(routed["destination"], f"issue:{SYN_TASK_ID}")
        self.assertEqual(routed["classification"], "unknown")
        self.assertFalse(routed["authorized"])
        self.assertFalse(routed["executed"])


# ---------------------------------------------------------------------------
# F. Harness honesty pins (GREEN controls).
# ---------------------------------------------------------------------------

class HarnessHonestyTest(CorrelationCase):
    def test_default_implementation_is_the_documented_known_bad(self):
        # The red failures below mean something only if the default IUT is
        # the synthetic defect mimic. A Builder run sets the seam variable
        # and skips this pin (the conforming implementation is not known-bad).
        if not self.using_known_bad:
            self.skipTest("conforming implementation under test")
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.queued_records()
        self.assertEqual(len(records), 1)
        record = self.read_record(records[0])
        self.assertNotIn("provenance", record,
                         "known-bad fixture must stay provenance-free; if "
                         "this pin fails, the RED set below is not testing "
                         "the defect shape anymore")

    def test_missing_configured_submitter_refuses_loudly(self):
        with self.assertRaises(MissingSubmitterError):
            resolve_submitter({SUBMITTER_ENV_VAR: str(self.work / "absent")})

    def test_audit_lines_carry_no_secrets_or_paths(self):
        src = self.write_request()
        proc = self.submit(src, task_id=SYN_TASK_ID, run_id=SYN_RUN_ID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for record in self.queued_records():
            self.assertNotIn(CANARY_SECRET, record.read_text(),
                             "queue record must not absorb ambient secrets")
        self.assertAuditClean(proc)
        # The success markers name the basename, never the queue directory.
        match = QUEUED_RE.search(proc.stdout)
        self.assertIsNotNone(match)
        self.assertNotIn("/", match.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
