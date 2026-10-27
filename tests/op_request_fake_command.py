#!/usr/bin/python3
"""Test double for privilege/IO boundaries. All filesystem operands stay private."""

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys


root = Path(os.environ["OPREQ_SANDBOX"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
fault = os.environ.get("OPREQ_FAULT", "")
faults = set(fault.split(",")) if fault else set()
mode = os.environ["OPREQ_MODE"]
if os.geteuid() == 0 or not root.is_absolute() or root.resolve() != root:
    sys.exit("fake refuses root or an invalid sandbox")


def private(raw):
    path = Path(raw)
    if not path.is_absolute() or not path.resolve().is_relative_to(root) or path == root:
        sys.exit(f"fake {name}: operand outside sandbox")
    return path


def fail():
    print(f"fake {name}: injected {fault} failure", file=sys.stderr)
    sys.exit(1)


def real():
    os.execv("/usr/bin/" + name, [name, *args])


with (root / "events.jsonl").open("a") as stream:
    stream.write(json.dumps([name, args]) + "\n")

if mode == "publication":
    if name == "id" and args == ["-u", "paperclip-operator"]:
        if "uid-unknown" in faults:
            fail()  # like `id` for a missing account: no stdout, status 1
        # uid-mismatch: the operator account is NOT the owner of src.json.
        print(os.getuid() + (1 if "uid-mismatch" in faults else 0))
    elif name == "cp" and len(args) == 3 and args[0] == "--":
        # The wrapper copies from an already-open fd (/dev/fd/N), never by
        # re-resolving the SRC path, so resolve before comparing operands.
        src = private(args[1])
        resolved = src.resolve()
        dest = private(args[2])
        # Two copy sources are in contract. The legacy fixture copies SRC
        # itself (resolved src.json, by path or by fd). The fixed wrapper
        # copies its validated staging snapshot through the inherited fd
        # (/dev/fd/N), which resolves to a file under the sandbox (the same
        # resolved-operand rule as the snapshot copy boundary double).
        legacy_src = resolved == root / "src.json"
        fd_src = (args[1].startswith("/dev/fd/") and args[1][8:].isdigit()
                  and resolved != root and resolved.is_relative_to(root))
        if dest.parent != root / "queue" or not (legacy_src or fd_src):
            sys.exit("unexpected publication cp operands")
        if "cp" in faults:
            fail()
        if "cp-partial" in faults:
            dest.write_bytes(src.read_bytes()[:7])
            fail()
        if "cp-corrupt" in faults:
            dest.write_bytes(b'{"what":"corrupt","why":"never validated","rollback":"never"}\n')
            sys.exit(0)
        shutil.copyfile(src, dest)
    elif name in ("chown", "chmod") and len(args) == 2:
        dest = private(args[1])
        expected = "root:paperclip-operator" if name == "chown" else "0640"
        if args[0] != expected or dest.parent != root / "queue" or not dest.is_file():
            sys.exit("unexpected publication metadata operands")
        if name in faults:
            fail()
        if name == "chmod":
            dest.chmod(0o640)
            if fault == "stdout":
                ready, release = int(os.environ["OPREQ_READY_FD"]), int(os.environ["OPREQ_RELEASE_FD"])
                if os.write(ready, b"1") != 1 or not select.select([release], [], [], 10)[0]:
                    sys.exit("output fault handshake timed out")
                if os.read(release, 1) != b"1":
                    sys.exit("output fault not released")
    else:
        sys.exit(f"unexpected publication command: {name}")
elif mode == "swap":
    if name == "id" and args == ["-u"]:
        print(0)  # fake root identity; real process remains the unprivileged test uid
    elif name == "stat":
        if len(args) != 4 or args[0] != "-c" or args[2] != "--":
            sys.exit("unexpected stat arguments")
        private(args[3])
        result = subprocess.run(["/usr/bin/stat", *args], capture_output=True, text=True, timeout=5)
        if result.returncode:
            sys.stderr.write(result.stderr)
            sys.exit(result.returncode)
        value = result.stdout
        if args[1] == "%u":
            value = "0\n"
        elif args[1] == "%u:%g:%a":
            value = "0:" + value.split(":", 1)[1]
        sys.stdout.write(value)
    elif name == "mktemp":
        private(args[-1])
        stage = "backup" if ".backup." in args[-1] else "swap"
        if fault == "mktemp-" + stage:
            fail()
        if fault == "mktemp-empty-" + stage:
            sys.exit(0)
        real()
    elif name == "cp":
        if len(args) != 4 or args[:2] != ["-a", "--"]:
            sys.exit("unexpected staging cp arguments")
        src, dest = (private(value) for value in args[-2:])
        stage = "backup" if ".backup." in str(dest) else "swap"
        if fault == "cp-" + stage:
            fail()
        if fault in ("cp-partial-" + stage, "cp-corrupt-" + stage):
            dest.write_bytes(src.read_bytes()[:7])
            if fault.startswith("cp-partial"):
                fail()
            sys.exit(0)
        real()
    elif name == "cat":
        if len(args) != 2 or args[0] != "--":
            sys.exit("unexpected staging cat arguments")
        src = private(args[1])
        if fault in ("cat-partial", "cat-corrupt"):
            sys.stdout.buffer.write(src.read_bytes()[:7])
            sys.stdout.buffer.flush()
            if fault == "cat-partial":
                fail()
            sys.exit(0)
        real()
    elif name in ("mv", "ln"):
        if len(args) != 4 or args[:2] != ["-T", "--"]:
            sys.exit("unexpected replacement arguments")
        for value in args[-2:]:
            private(value)
        if fault == name:
            fail()
        real()
    elif name == "rm":
        if len(args) != 3 or args[:2] != ["-rf", "--"]:
            sys.exit("unexpected cleanup arguments")
        private(args[2])
        real()
    else:
        sys.exit(f"unexpected swap command: {name}")
else:
    sys.exit("unknown fake mode")
