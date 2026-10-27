"""Behavioral acceptance driver; source is never edited except private LOG/QUEUE.

The truthful entrypoint intentionally keeps P0/P1/P2 green and F1..F4 red.
The fixed entrypoint adds post-publication log and stdout diagnostic contracts.
Append success is NOT fsync/crash durability, and output loss is NOT absence.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from op_request_harness import FIXTURE, SOURCE, Provenance, Refused, publication_case, write_new


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def ok_lines(case):
    return [line for line in case.audit().splitlines() if "op-request ok" in line]


def failed_lines(case):
    return [line for line in case.audit().splitlines() if "op-request FAILED" in line]


def queued(result):
    return [line for line in result.stdout.splitlines() if line.startswith("queued ")]


def published(case):
    files = case.files()
    require(len(files) == 1, f"expected one retained DEST, got {len(files)}")
    dest = files[0]
    require(dest.is_file() and not dest.is_symlink(), "DEST is not a regular file")
    require(dest.read_bytes() == case.src.read_bytes(), "DEST bytes differ")
    require(dest.stat().st_mode & 0o7777 == 0o640, "DEST mode is not 0640")
    events = case.events()
    require(events.count(["chown", ["root:paperclip-operator", str(dest)]]) == 1,
            "wrapper did not request exactly one root:paperclip-operator chown on DEST")
    require(events.count(["chmod", ["0640", str(dest)]]) == 1,
            "wrapper did not request exactly one 0640 chmod on DEST")
    return dest


def happy(case, result):
    require(result.returncode == 0, f"happy path rc={result.returncode}, stderr={result.stderr!r}")
    dest = published(case)
    require(queued(result) == [f"queued {dest}"], "expected one queued line naming DEST")
    lines = ok_lines(case)
    require(len(lines) == 1 and f"op-request ok dest={dest.name}" in lines[0],
            "expected one ok append naming DEST basename")


def invalid(case, result):
    require(result.returncode == 3, f"validation rc={result.returncode}, want 3")
    require(not queued(result) and not ok_lines(case), "validation leaked queued/ok")
    require(not case.files(), "validation published a request")
    require(not any(event[0] == "cp" for event in case.events()), "validation reached publication")


def faults_of(case):
    return set(case.fault.split(",")) if case.fault else set()


def publication_failure(case, result, fixed):
    faults = faults_of(case)
    if fixed:
        require(result.returncode == 1,
                f"publication failure rc={result.returncode}; stdout={result.stdout!r}; log={case.audit()!r}")
    else:
        require(result.returncode != 0,
                f"publication failure rc={result.returncode}; stdout={result.stdout!r}; log={case.audit()!r}")
    require(not queued(result) and not ok_lines(case), "publication failure leaked queued/ok")
    if faults & {"cp", "cp-partial", "cp-corrupt"}:
        require(any(event[0] == "cp" for event in case.events()), "copy step was not reached")
    if faults & {"chown", "chmod"}:
        require(any(event[0] in ("chown", "chmod") for event in case.events()), "metadata step was not reached")
    if faults == {"cp"}:
        require(not case.files(), "failed copy published a file")
        if fixed:
            require("publication incomplete" not in result.stderr,
                    "copy failure with no DEST claims an incomplete publication")
            require(case.audit() == "", "copy failure with no DEST wrote an audit line")
            failed = [line for line in result.stderr.splitlines() if "op-request FAILED" in line]
            require(len(failed) == 1, "copy failure must keep the single FAILED line only")
    else:
        require(len(case.files()) == 1, "failure silently cleaned up the partially published request")
        if fixed:
            dest = case.files()[0]
            audit_confirmed = "log" not in faults
            incomplete_publication_diagnostic(result.stderr, dest, audit_confirmed=audit_confirmed)
            if audit_confirmed:
                lines = failed_lines(case)
                require(len(lines) == 1 and f"op-request FAILED dest={dest.name}" in lines[0],
                        "partial publication lacks the FAILED audit line naming DEST")
            else:
                # The FAILED append itself failed, so the single FAILED line
                # naming DEST survives on stderr only, never in the audit log.
                failed = [line for line in result.stderr.splitlines()
                          if "op-request FAILED" in line and dest.name in line]
                require(len(failed) == 1,
                        "unconfirmed audit lacks the single stderr FAILED line naming DEST")
            if case.fault == "cp-partial":
                require(dest.read_bytes() == case.src.read_bytes()[:7],
                        "partial copy left unexpected DEST bytes")
            elif case.fault == "cp-corrupt":
                require(dest.read_bytes() != case.src.read_bytes(),
                        "corrupt copy was claimed as published")
            if "log" in faults:
                require(case.audit() == "", "unconfirmed audit wrote an audit line")


def log_failure(case, result, fixed):
    require(result.returncode == 1 if fixed else result.returncode != 0,
            f"log failure rc={result.returncode}")
    require(not queued(result), f"log failure still claims queued: {result.stdout!r}")
    published(case)  # the request survives; a failed append does not undo it


def post_publication_diagnostic(stderr, dest, audit_confirmed):
    text = stderr.lower()
    require(re.search(r"\bpublication (?:is )?complete(?:d)?\b|\brequest (?:is )?published\b", text),
            f"stderr does not explicitly say publication is complete: {stderr!r}")
    require(str(dest) in stderr or dest.name in stderr, "stderr lacks the published destination identity")
    audit_pattern = (r"\b(?:audit|log)(?: append)? (?:is )?(?:complete(?:d)?|confirmed|succeeded)\b"
                     if audit_confirmed else r"\baudit (?:is )?unconfirmed\b")
    require(re.search(audit_pattern, text), "stderr does not state the correct audit outcome")
    require(re.search(r"\b(?:do not|never|don't) (?:automatically )?(?:retry|replay)\b|\bno[- ]retry\b", text),
            "stderr lacks an explicit no-retry instruction")
    require(re.search(r"\boperator\b[^\n]{0,100}\b(?:inspect\w*|reconcil\w*)\b", text),
            "stderr lacks an operator-inspection instruction")


def incomplete_publication_diagnostic(stderr, dest, audit_confirmed):
    text = stderr.lower()
    require(re.search(r"\bpublication incomplete\b", text),
            f"stderr does not explicitly say publication is incomplete: {stderr!r}")
    require("metadata not applied" in text,
            f"stderr does not state metadata was not applied: {stderr!r}")
    require(str(dest) in stderr, "stderr lacks the full published destination path")
    audit_pattern = (r"\baudit (?:is )?recorded\b"
                     if audit_confirmed else r"\baudit (?:is )?unconfirmed\b")
    require(re.search(audit_pattern, text), "stderr does not state the correct audit outcome")
    require(re.search(r"\b(?:do not|never|don't) (?:automatically )?(?:retry|replay)\b|\bno[- ]retry\b", text),
            "stderr lacks an explicit no-retry instruction")
    require(re.search(r"\boperator\b[^\n]{0,100}\b(?:inspect\w*|reconcil\w*)\b", text),
            "stderr lacks an operator-inspection instruction")


def log_diagnostic(case, result):
    log_failure(case, result, fixed=True)
    post_publication_diagnostic(result.stderr, case.files()[0], audit_confirmed=False)


def stdout_failure(case, result):
    require(result.returncode != 0, "final stdout EPIPE was reported as success")
    dest = published(case)
    lines = ok_lines(case)
    require(len(lines) == 1 and f"op-request ok dest={dest.name}" in lines[0],
            "stdout fault was not after successful publication AND audit append")
    require(result.stdout == "request-ok\n", "stdout reader closed before validation completed")
    post_publication_diagnostic(result.stderr, dest, audit_confirmed=True)


def swap_regression(source):
    # The demonstrated attack flips SRC between a valid request and a symlink
    # to a secret file while the wrapper runs. The single O_NOFOLLOW open
    # means each run either publishes the validated snapshot or refuses;
    # foreign bytes can never reach the queue. The invariant is asserted per
    # iteration, so the loop cannot flake.
    for round in range(10):
        with publication_case(source, "") as case:
            if round % 2:
                secret = case.path / "secret.json"
                write_new(secret, b'{"what":"secret","why":"must never publish","rollback":"never"}\n')
                case.src.unlink()
                case.src.symlink_to(secret)
                result = case.run()
                require(result.returncode == 3,
                        f"symlink SRC was not refused: rc={result.returncode}")
                require(not any(event[0] == "cp" for event in case.events()),
                        "symlink SRC reached the copy step")
                require(not case.files(), "symlink SRC published a file")
                require(not queued(result) and not ok_lines(case) and not failed_lines(case),
                        "symlink SRC leaked a publication signal")
            else:
                happy(case, case.run())


def refused(case, result, reason):
    # One validation refusal: exit 3, the named stderr line, exactly one
    # REFUSED audit line, and nothing published or claimed.
    require(result.returncode == 3,
            f"{reason}: rc={result.returncode}, want 3; stderr={result.stderr!r}")
    require(f"op-request REFUSED: {reason}" in result.stderr,
            f"{reason}: stderr lacks the refusal line: {result.stderr!r}")
    require("request-ok" not in result.stdout and not queued(result),
            f"{reason}: refusal leaked a success signal")
    require(not case.files(), f"{reason}: refusal published a request")
    require(not any(event[0] in ("cp", "chown", "chmod") for event in case.events()),
            f"{reason}: refusal reached publication")
    require(not ok_lines(case) and not failed_lines(case), f"{reason}: refusal leaked a publication audit line")
    lines = [line for line in case.audit().splitlines() if "op-request REFUSED" in line]
    require(len(lines) == 1 and f"op-wrapper: op-request REFUSED {reason}" in lines[0],
            f"{reason}: expected exactly one REFUSED audit line, got {lines!r}")


def replace_src(case, body):
    case.src.unlink()
    write_new(case.src, body)


def ownership_regression(source):
    # The fake `id -u paperclip-operator` normally returns the caller's own
    # uid, which hides a dropped owner check. These faults make the operator
    # account differ from (or be unknown to) the owner of SRC.
    for fault in ("uid-mismatch", "uid-unknown"):
        with publication_case(source, fault) as case:
            refused(case, case.run(), "not-operator-owned")


def fifo_regression(source):
    # Opening a FIFO read-only blocks until a writer appears. A wrapper that
    # waits is only released late by the timer, so it must already have
    # refused well before then.
    with publication_case(source, "") as case:
        case.src.unlink()
        os.mkfifo(case.src, 0o600)

        def release():
            try:
                os.close(os.open(case.src, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass  # no blocked reader: the wrapper already refused

        timer = threading.Timer(10, release)
        timer.start()
        started = time.monotonic()
        try:
            result = case.run()
        finally:
            timer.cancel()
            release()
        elapsed = time.monotonic() - started
        require(elapsed < 6, f"FIFO SRC blocked the wrapper for {elapsed:.1f}s instead of refusing")
        refused(case, result, "not-a-file")


def non_object_regression(source):
    for body in (b"[]\n", b'"x"\n', b"7\n", b"null\n", b"true\n"):
        with publication_case(source, "") as case:
            replace_src(case, body)
            refused(case, case.run(), "bad-json-type")


def refusal_audit_regression(source):
    # Every refusal raised in the python step must leave the same audit line
    # the baseline wrote for its shell-side refusals.
    def secret_symlink(case):
        secret = case.path / "secret.json"
        write_new(secret, b'{"what":"secret","why":"must never publish","rollback":"never"}\n')
        case.src.unlink()
        case.src.symlink_to(secret)

    def directory(case):
        case.src.unlink()
        case.src.mkdir()

    scenarios = [
        ("too-large", lambda case: replace_src(case, b"x" * 17000)),
        ("not-a-file", directory),
        ("not-a-file", lambda case: case.src.unlink()),
        ("symlink", secret_symlink),
        ("invalid-json", lambda case: replace_src(case, b"{not json\n")),
        # The baseline parsed text-mode UTF-8, so a BOM or UTF-16 body was
        # refused; json.loads(bytes) would silently auto-detect and publish.
        ("invalid-json", lambda case: replace_src(
            case, b"\xef\xbb\xbf" + b'{"what":"x","why":"y","rollback":"z"}\n')),
        ("invalid-json", lambda case: replace_src(
            case, '{"what":"x","why":"y","rollback":"z"}\n'.encode("utf-16"))),
        ("bad-or-missing-key rollback", lambda case: replace_src(case, b'{"what":"x","why":"y"}\n')),
        ("bad-command-key", lambda case: replace_src(
            case, b'{"what":"x","why":"y","rollback":"z","command":7}\n')),
    ]
    for reason, prepare in scenarios:
        with publication_case(source, "") as case:
            prepare(case)
            refused(case, case.run(), reason)


def unwritable_audit_refusal(source):
    # A refusal whose audit append fails is still a refusal: exit 3, nothing
    # published, and the lost audit line is reported rather than hidden.
    with publication_case(source, "log") as case:
        replace_src(case, b"x" * 17000)
        result = case.run()
        require(result.returncode == 3, f"refusal with unwritable audit rc={result.returncode}, want 3")
        require("op-request REFUSED: too-large" in result.stderr, "refusal line missing from stderr")
        require("refusal audit append failed" in result.stderr, "lost refusal audit line was not reported")
        require(not queued(result) and not case.files(), "refusal with unwritable audit published or claimed")


def internal_error_regression(source):
    # An unexpected python-step crash exits 1 (not 0/3/10). It must be
    # reported as an internal error, never as a publication failure:
    # no copy ran, so "publication" wording would mislead the caller.
    with publication_case(source, "") as case:
        fake_python = case.path / "bin" / "python3"
        write_new(fake_python, b"#!/bin/sh\nexit 1\n")
        fake_python.chmod(0o700)
        result = case.run()
        require(result.returncode == 1,
                f"internal error rc={result.returncode}, want 1; stderr={result.stderr!r}")
        require("internal error" in result.stderr,
                f"python crash lacks the internal-error diagnostic: {result.stderr!r}")
        require("FAILED: publication" not in result.stderr,
                "python crash was misreported as a publication failure")
        require(not queued(result) and not case.files(),
                "internal error published or claimed a request")
        require(not ok_lines(case) and not failed_lines(case),
                "internal error wrote a publication audit line")


CUSTOM_FIXED_CASES = (
    ("F11 symlink swap: validated bytes or nothing", swap_regression),
    ("F12 SRC not owned by paperclip-operator (or operator unknown): refused", ownership_regression),
    ("F13 FIFO SRC: refused promptly, never waited on", fifo_regression),
    ("F14 non-object JSON body: refused with exit 3", non_object_regression),
    ("F15 python-step refusals: exit 3 plus one REFUSED audit line", refusal_audit_regression),
    ("F16 refusal with unwritable audit: still exit 3, loss reported", unwritable_audit_refusal),
    ("F17 python-step crash: internal error, not a publication failure", internal_error_regression),
)


def suite(source):
    passed, failed = 1, 0
    print("ok   - P0 provenance: verified source bytes before any execution")
    cases = [("P1 happy path", "", happy, False),
             ("P2 invalid JSON", "", invalid, True)]
    for index, fault in enumerate(("cp", "chown", "chmod"), 1):
        cases.append((f"F{index} {fault} failure", fault,
                      lambda case, result: publication_failure(case, result, source.kind == "fixed"), False))
    cases.append(("F4 log append failure", "log",
                  lambda case, result: log_failure(case, result, source.kind == "fixed"), False))
    if source.kind == "fixed":
        def fixed_failure(case, result):
            publication_failure(case, result, True)
        cases.extend([("F5 published but audit unconfirmed: identity/no-retry/operator diagnostic", "log", log_diagnostic, False),
                      ("F6 final stdout EPIPE after publication/audit: no-retry diagnostic", "stdout", stdout_failure, False),
                      ("F7 partial copy: truncated DEST kept with incomplete diagnostic", "cp-partial", fixed_failure, False),
                      ("F8 corrupt copy: wrong bytes never claimed, DEST kept", "cp-corrupt", fixed_failure, False),
                      ("F9 chown failure plus unwritable audit: unconfirmed arm", "chown,log", fixed_failure, False),
                      ("F10 chmod failure plus unwritable audit: unconfirmed arm", "chmod,log", fixed_failure, False)])
    for label, fault, check, bad_json in cases:
        with publication_case(source, fault) as case:
            result = case.run(invalid=bad_json)
            try:
                check(case, result)
            except AssertionError as error:
                failed += 1
                print(f"FAIL - {label}: {error}")
                if label.startswith("P1"):
                    raise Refused("P1 control failed; no failure-case passes may be claimed") from error
            else:
                passed += 1
                print(f"ok   - {label}")
    for label, regression in CUSTOM_FIXED_CASES if source.kind == "fixed" else ():
        try:
            regression(source)
        except AssertionError as error:
            failed += 1
            print(f"FAIL - {label}: {error}")
        else:
            passed += 1
            print(f"ok   - {label}")
    print(f"\npass={passed} fail={failed}")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("truthful", "fixed"))
    parser.add_argument("--source", type=Path, help="fixed-only scratch candidate named op-request, with its strict sibling manifest")
    args = parser.parse_args()
    if args.source and (args.kind != "fixed" or args.source.name != "op-request"):
        parser.error("--source is fixed-only and must be named op-request")
    path = args.source if args.source else (FIXTURE if args.kind == "truthful" else SOURCE)
    try:
        return suite(Provenance.load(path, args.kind))
    except (Refused, OSError, subprocess.SubprocessError) as error:
        print(f"HARNESS REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
