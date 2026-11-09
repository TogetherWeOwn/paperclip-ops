#!/usr/bin/env python3
"""Bounded synthetic ext4/FUSE evaluation; never an installed provider."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

PIN = "a4721aca27ffcbedfd79c553c78f8969c29bbc63"
CASES = ("unchanged_contents", "independent_writes", "git_identity_commit",
         "dirty_removal_refused", "interrupted_launcher_unlock", "interrupted_mount_repair",
         "explicit_cleanup", "provider_identity_path_validation")


def validate_doctor(value, source):
    if not isinstance(value, dict) or value.get("identity") != "simgit":
        raise ValueError("candidate identity mismatch")
    if value.get("version") != "0.5.0":
        raise ValueError("candidate version mismatch")
    details = value.get("repository_details") or {}
    if (value.get("repository") != str(source)
            or details.get("common_git_owner") != str(source)
            or details.get("common_git_dir") != str(source / ".git")
            or value.get("git_worktree_supported") is not True
            or value.get("populate_mode") != "overlay"):
        raise ValueError("repository or overlay capability mismatch")


def validate_allocation(value, source, expected, branch, base):
    if not isinstance(value, dict):
        raise ValueError("allocation is not an object")
    for key in ("path", "worktree", "cleanup_token"):
        raw = value.get(key)
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ValueError("allocation path is not absolute")
        path = Path(raw).resolve()
        if raw != str(path) or path != expected or path == source or source in path.parents:
            raise ValueError("allocation path is not the canonical outside-source destination")
    if (value.get("mode") != "overlay" or value.get("branch") != branch
            or value.get("base") != base or value.get("ephemeral") is not True):
        raise ValueError("allocation identity or CoW mode mismatch")
    return value["cleanup_token"]


def mounts():
    records = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        records.append({"path": path, "type": right.split()[0], "record": line})
    return records


def owned_mounts(root):
    return [m for m in mounts() if Path(m["path"]).is_relative_to(root)]


def overlay_processes(root):
    records = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            args = [os.fsdecode(a) for a in args if a]
            if args and Path(args[0]).name == "fuse-overlayfs" and any(
                    a.startswith(str(root) + "/") for a in args[1:]):
                records.append({"pid": int(entry.name), "args": args})
        except (OSError, ProcessLookupError):
            continue
    return records


def allocated(root, mounted=()):
    """Count physical backing inodes, not the synthetic inode space of FUSE views."""
    skip = {Path(m["path"]) for m in mounted}
    seen = set()
    groups = dict.fromkeys(("git_objects", "baseline_cache", "overlay_state",
                            "generated_output", "trees_and_metadata"), 0)
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = [d for d in dirs if parent / d not in skip]
        for path in [parent] + [parent / n for n in files]:
            st = path.lstat()
            identity = (st.st_dev, st.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            parts = path.relative_to(root).parts
            if "generated" in parts:
                group = "generated_output"
            elif "objects" in parts and ".git" in parts:
                group = "git_objects"
            elif "baselines" in parts:
                group = "baseline_cache"
            elif "overlays" in parts:
                group = "overlay_state"
            else:
                group = "trees_and_metadata"
            groups[group] += st.st_blocks * 512
    return {"allocated_bytes": sum(groups.values()), "categories": groups,
            "unique_inodes": len(seen), "excluded_mounted_views": sorted(map(str, skip))}


class Evaluation:
    def __init__(self, binary, root, output):
        self.binary, self.root, self.output = binary, root, output
        matrix = [{"name": f"{method}-{cache}-n{count}", "method": method,
                   "cache": cache, "n": count, "status": "UNRUN"}
                  for method in ("git", "simgit") for cache in ("cold", "warm") for count in (1, 4)]
        self.result = {"candidate_commit": PIN, "cases": [], "matrix": matrix,
                       "commands": [], "recommendation": "NO-GO (incomplete)"}
        self.env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                    "LC_ALL": "C", "SIMGIT_POPULATE": "overlay",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        self.save()

    def save(self):
        temporary = self.output / "result.tmp"
        temporary.write_text(json.dumps(self.result, indent=2) + "\n")
        temporary.replace(self.output / "result.json")

    def command(self, cwd, *args, ok=True):
        start = time.monotonic()
        p = subprocess.run(list(map(str, args)), cwd=cwd, env=self.env,
                           text=True, capture_output=True, timeout=30)
        self.result["commands"].append({"cwd": str(cwd), "argv": list(map(str, args)),
                                        "exit": p.returncode, "seconds": time.monotonic() - start,
                                        "stdout": p.stdout, "stderr": p.stderr})
        self.save()
        if ok and p.returncode:
            raise RuntimeError(f"command failed: {args}, exit {p.returncode}: {p.stderr}")
        return p

    def git(self, source, *args, **kwargs):
        return self.command(source, "git", *args, **kwargs)

    def simgit(self, source, *args, **kwargs):
        return self.command(source, self.binary, *args, **kwargs)

    def case(self, name, operation):
        try:
            detail = operation()
        except Exception as error:
            self.result["cases"].append({"name": name, "verdict": "FAIL", "detail": str(error)})
            self.save()
            raise
        self.result["cases"].append({"name": name, "verdict": "PASS", "detail": detail})
        self.save()

    def snapshot(self, root):
        os.sync()
        value = allocated(root, owned_mounts(root))
        value["filesystem_available_bytes"] = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
        return value

    def source(self, root):
        source = root / "source"
        source.mkdir(parents=True)
        self.git(source, "init", "-b", "main")
        self.git(source, "config", "user.name", "Synthetic Fixture")
        self.git(source, "config", "user.email", "fixture@example.invalid")
        self.git(source, "config", "commit.gpgsign", "false")
        self.git(source, "config", "gc.auto", "0")
        (source / ".gitignore").write_text("generated/\n")
        # Exactly 16 MiB deterministic incompressible tracked payload, no host data.
        for i in range(64):
            (source / f"file-{i:03}.bin").write_bytes(
                hashlib.shake_256(f"synthetic-v1:{i}".encode()).digest(256 * 1024))
        self.git(source, "add", ".")
        self.git(source, "commit", "-m", "Synthetic baseline")
        base = self.git(source, "rev-parse", "HEAD").stdout.strip()
        expected = self.manifest(source)
        return source, base, expected

    @staticmethod
    def manifest(tree):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(tree.glob("file-*.bin"))}

    def create(self, method, source, path, branch, base):
        if method == "git":
            self.git(source, "worktree", "add", "-b", branch, path, base)
            return str(path)
        record = json.loads(self.simgit(source, "add", branch, "--json", "--ephemeral",
                                       "--require-cow", "--base", base, "--path", path).stdout)
        token = validate_allocation(record, source, path, branch, base)
        actual = [m for m in mounts() if m["path"] == str(path)]
        if len(actual) != 1 or actual[0]["type"] != "fuse.fuse-overlayfs":
            raise ValueError(f"actual mount is not fuse-overlayfs: {actual}")
        self.result.setdefault("verified_mounts", []).extend(actual)
        return token

    def remove(self, method, source, token):
        if method == "git":
            self.git(source, "worktree", "remove", token)
        else:
            self.simgit(source, "remove", "--json", "--", token)

    def isolation(self, source, paths, branches, base, expected):
        indexes = set()
        for path, branch in zip(paths, branches):
            assert self.git(path, "symbolic-ref", "--short", "HEAD").stdout.strip() == branch
            assert self.git(path, "rev-parse", "HEAD").stdout.strip() == base
            index = self.git(path, "rev-parse", "--git-path", "index").stdout.strip()
            assert index not in indexes
            indexes.add(index)
        for i, path in enumerate(paths):
            with (path / "file-000.bin").open("r+b") as stream:
                stream.write(f"divergent-{i}".encode())
            (path / "file-001.bin").rename(path / "renamed.bin")
            (path / "file-002.bin").unlink()
            assert self.git(path, "status", "--porcelain").stdout.strip()
            assert self.manifest(source) == expected
            for untouched in paths[i + 1:]:
                assert self.manifest(untouched) == expected
            (path / "generated").mkdir()
            (path / "generated" / "output.bin").write_bytes(
                hashlib.shake_256(f"generated:{i}".encode()).digest(512 * 1024))
        for i, path in enumerate(paths):
            assert (path / "file-000.bin").read_bytes().startswith(f"divergent-{i}".encode())
            assert "file-001.bin" not in self.manifest(path)
            assert not (path / "file-002.bin").exists()
            assert hashlib.sha256((path / "renamed.bin").read_bytes()).hexdigest() == expected["file-001.bin"]
        return "In-place, rename and delete preserve source and sibling contents; generated output is separate."

    def interrupted_launcher(self, source, path, branch, root):
        ready = root / "launcher-ready"
        code = "from pathlib import Path; import signal,sys; Path(sys.argv[1]).write_text('ready'); signal.pause()"
        with (self.output / "launcher.log").open("w") as log:
            process = subprocess.Popen([str(self.binary), "run", branch, "--path", str(path),
                                        "--", "python3", "-I", "-c", code, str(ready)],
                                       cwd=source, env=self.env, stdout=log, stderr=log,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert ready.exists(), "launcher child never became ready"
                refused = self.simgit(source, "unlock", "--json", "--", path, ok=False)
                assert refused.returncode != 0, "unlock accepted a live launcher"
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        # Child was stopped with the owned process group, not left writing files.
        unlocked = json.loads(self.simgit(source, "unlock", "--json", "--", path).stdout)
        assert unlocked["was_locked"] is True
        return {"launcher_pid": process.pid, "unlock": unlocked}

    def interrupted_mount(self, source, path, root):
        marker = path / "recovery-marker"
        marker.write_text("preserve synthetic upperdir\n")
        os.sync()
        processes = [p for p in overlay_processes(root) if str(path) in p["args"]]
        if len(processes) != 1:
            raise RuntimeError(f"cannot uniquely identify owned FUSE process: {processes}")
        os.kill(processes[0]["pid"], signal.SIGKILL)
        repaired = self.simgit(source, "repair", "--json")
        assert marker.read_text() == "preserve synthetic upperdir\n"
        assert any(m["path"] == str(path) and m["type"] == "fuse.fuse-overlayfs" for m in mounts())
        self.git(path, "status", "--porcelain")
        marker.unlink()
        return {"interrupted": processes[0], "repair": json.loads(repaired.stdout)}

    def cleanup(self, method, source, root):
        # Inspect actual registrations after failures; never replay an uncertain add.
        # All data here was generated by this job. Forced cleanup is containment,
        # recorded separately from successful native provider cleanup.
        attempted = []
        if source.exists():
            listing = self.git(source, "worktree", "list", "--porcelain", ok=False)
            for line in listing.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                path = Path(line[9:])
                if path == source or not path.is_relative_to(root):
                    continue
                if method == "simgit":
                    p = self.simgit(source, "remove", "--json", "--discard-dirty", "--", path, ok=False)
                else:
                    p = self.git(source, "worktree", "remove", "--force", path, ok=False)
                attempted.append({"path": str(path), "exit": p.returncode})
        remaining_mounts = owned_mounts(root)
        remaining_processes = overlay_processes(root)
        self.result.setdefault("failure_cleanup", []).append({
            "root": str(root), "attempted": attempted, "remaining_mounts": remaining_mounts,
            "remaining_processes": remaining_processes, "allocation": self.snapshot(root)})
        self.save()
        if remaining_mounts or remaining_processes or any(p["exit"] for p in attempted):
            raise RuntimeError("explicit cleanup incomplete; stop matrix, runner disposal is final containment")

    def scenario(self, method, cache, count):
        name = f"{method}-{cache}-n{count}"
        root = self.root / name
        source, base, expected = self.source(root)
        row = next(row for row in self.result["matrix"] if row["name"] == name)
        row.update({"status": "INCOMPLETE", "before_cache": self.snapshot(root)})
        self.save()
        try:
            if method == "simgit":
                doctor = json.loads(self.simgit(source, "doctor", "--json").stdout)
                validate_doctor(doctor, source)
                row["doctor"] = doctor
            if cache == "warm":
                seed = root / "warm-seed"
                token = self.create(method, source, seed, "warm-seed", base)
                self.remove(method, source, token)
            row["before_create"] = self.snapshot(root)
            paths = [root / f"tree-{i}" for i in range(count)]
            branches = [f"synthetic-{i}" for i in range(count)]
            start = time.monotonic()
            tokens = [self.create(method, source, path, branch, base)
                      for path, branch in zip(paths, branches)]
            row["create_seconds"] = time.monotonic() - start
            row["unchanged"] = self.snapshot(root)
            def unchanged():
                for path in paths:
                    assert self.manifest(path) == expected
                    assert not self.git(path, "status", "--porcelain").stdout.strip()
                return "All tracked payload hashes match; all indexes are clean."
            self.case(name + ":unchanged_contents", unchanged)
            self.case(name + ":independent_writes",
                      lambda: self.isolation(source, paths, branches, base, expected))
            row["divergent"] = self.snapshot(root)
            if method == "simgit":
                def baseline_immutable():
                    files = list((source / ".git" / "simgit" / "baselines").rglob("file-*.bin"))
                    assert len(files) == 64
                    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == expected[p.name] for p in files)
                    return "All 64 cached baseline payloads retained their original hashes."
                self.case(name + ":baseline_immutable", baseline_immutable)
            def identities():
                heads = set()
                for path, branch in zip(paths, branches):
                    self.git(path, "add", "-A")
                    self.git(path, "commit", "-m", f"Synthetic change {branch}")
                    head = self.git(path, "rev-parse", "HEAD").stdout.strip()
                    assert head != base and head not in heads
                    heads.add(head)
                    assert self.git(source, "rev-parse", branch).stdout.strip() == head
                    assert not self.git(path, "status", "--porcelain").stdout.strip()
                assert self.git(source, "rev-parse", "HEAD").stdout.strip() == base
                return "Distinct indexes/branches/HEADs; add/commit/status work and only the owning branch advances."
            self.case(name + ":git_identity_commit", identities)
            row["committed"] = self.snapshot(root)
            if method == "simgit" and cache == "cold" and count == 4:
                dirty = paths[0] / "dirty-marker"
                dirty.write_text("must survive refused removal\n")
                def refusal():
                    p = self.simgit(source, "remove", "--json", "--", tokens[0], ok=False)
                    assert p.returncode != 0 and not p.stdout.strip()
                    assert dirty.read_text() == "must survive refused removal\n"
                    return "Safe cleanup refused dirty tree with empty JSON stdout and preserved its file."
                self.case(name + ":dirty_removal_refused", refusal)
                dirty.unlink()
                self.case(name + ":interrupted_launcher_unlock",
                          lambda: self.interrupted_launcher(source, paths[0], branches[0], root))
                self.case(name + ":interrupted_mount_repair",
                          lambda: self.interrupted_mount(source, paths[0], root))
            # Generated artifacts are explicitly owned synthetic worker output.
            # Remove them before safe native cleanup, not via discard-dirty.
            for path in paths:
                (path / "generated" / "output.bin").unlink()
                (path / "generated").rmdir()
            start = time.monotonic()
            for token in tokens:
                self.remove(method, source, token)
            row["remove_seconds"] = time.monotonic() - start
            row["after_remove"] = self.snapshot(root)
            def clean():
                assert not owned_mounts(root)
                assert not overlay_processes(root)
                assert all(not p.exists() for p in paths)
                entries = self.git(source, "worktree", "list", "--porcelain").stdout
                assert entries.count("worktree ") == 1
                return "No mounted views, owned FUSE processes, worktree paths or linked registrations remain."
            self.case(name + ":explicit_cleanup", clean)
            if method == "simgit":
                row["prune"] = json.loads(self.simgit(source, "prune", "--all", "--json").stdout)
            row["after_prune"] = self.snapshot(root)
            row["status"] = "COMPLETE"
            self.save()
        finally:
            self.cleanup(method, source, root)

    def run(self):
        self.root.mkdir()
        try:
            for method in ("git", "simgit"):
                for cache in ("cold", "warm"):
                    for count in (1, 4):
                        self.scenario(method, cache, count)
            self.result["recommendation"] = "REVIEW REQUIRED: measured savings and lifecycle evidence must both qualify; no plugin authorized"
        except Exception as error:
            self.result["error"] = str(error)
            self.result["recommendation"] = "NO-GO: incomplete or failed capability/lifecycle evaluation"
        finally:
            for case in CASES:
                if not any(c["name"].endswith(":" + case) for c in self.result["cases"]):
                    self.result["cases"].append({"name": "not-executed:" + case, "verdict": "UNRUN"})
            self.result["residual_mounts"] = owned_mounts(self.root)
            self.result["residual_processes"] = overlay_processes(self.root)
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.root.exists() or not args.binary.is_absolute() or not args.binary.is_file():
        parser.error("require a new synthetic root and an absolute candidate executable")
    Evaluation(args.binary, args.root.resolve(), args.output.resolve()).run()


if __name__ == "__main__":
    main()
