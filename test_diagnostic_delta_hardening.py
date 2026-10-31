#!/usr/bin/env python3
"""Regression coverage added during the diagnostic-delta integration rebuild."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_diagnostic_delta import (RepositoryFixture, SCRIPT, PRODUCER, BAD_SCRIPT,
                                   POLICY, POLICY_VALUE, WORKFLOW_POLICY, WORKFLOW_PATH,
                                   SCRIPT_RULE)

ROOT = Path(__file__).resolve().parent


def load_gate():
    spec = importlib.util.spec_from_file_location("delta_hardening", SCRIPT)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


class OutputLimits(unittest.TestCase):
    def test_producer_is_stopped_before_excess_stdout_or_stderr_is_collected(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "finished"
            producer = root / "producer.py"
            for channel in ("stdout", "stderr"):
                with self.subTest(channel=channel):
                    producer.write_text(
                        "import sys\nfrom pathlib import Path\n"
                        f"for _ in range(128):\n    sys.{channel}.buffer.write(b'x' * 65536)\n"
                        f"Path({str(marker)!r}).touch()\n")
                    with patch.object(gate, "PRODUCER", producer):
                        with self.assertRaises(gate.Unmeasurable) as error:
                            gate.scan(root, ["**"], {})
                    self.assertEqual(error.exception.code, "producer_output_limit")
                    self.assertFalse(marker.exists(), "Producer must be killed before draining its full output")

    def test_exact_report_and_stderr_byte_limits_are_accepted(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            producer = root / "producer.py"
            report = {"tool": "ax-lint", "mode": "warn", "root": "", "findings": [],
                      "summary": {"findings": 0, "mode": "warn"},
                      "coverage": {"skipped": [], "unknown": []}}
            report["root"] = "x" * (1024 * 1024 - len(json.dumps(report).encode()))
            raw = json.dumps(report)
            self.assertEqual(len(raw.encode()), 1024 * 1024)
            producer.write_text("import sys\n" + f"sys.stdout.write({raw!r})\n"
                                "sys.stderr.buffer.write(b'x' * 65536)\n")
            with patch.object(gate, "PRODUCER", producer):
                self.assertEqual(gate.scan(root, ["**"], {}), (set(), {"skipped": [], "unknown": []}))

    def test_first_excess_byte_on_either_pipe_is_refused_before_producer_finishes(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "finished"
            producer = root / "producer.py"
            for channel, size in (("stdout", 1024 * 1024 + 1), ("stderr", 64 * 1024 + 1)):
                with self.subTest(channel=channel):
                    if marker.exists():
                        marker.unlink()
                    producer.write_text(
                        "import sys, time\nfrom pathlib import Path\n"
                        f"sys.{channel}.buffer.write(b'x' * {size})\n"
                        f"sys.{channel}.buffer.flush()\n"
                        "time.sleep(2)\n"
                        f"Path({str(marker)!r}).touch()\n")
                    with patch.object(gate, "PRODUCER", producer):
                        with self.assertRaises(gate.Unmeasurable) as error:
                            gate.scan(root, ["**"], {})
                    self.assertEqual(error.exception.code, "producer_output_limit")
                    self.assertFalse(marker.exists(), "Collector must stop at the cap, not after limit+1 bytes finish")


class GitOutputLimits(unittest.TestCase):
    def test_git_output_is_stopped_at_the_byte_cap_not_spooled_to_completion(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "finished"
            bindir = root / "bin"
            bindir.mkdir()
            fake = bindir / "git"
            for channel, size, limit in (("stdout", 1024 + 1, 1024), ("stderr", 64 * 1024 + 1, 1024 * 1024)):
                with self.subTest(channel=channel):
                    if marker.exists():
                        marker.unlink()
                    fake.write_text(
                        "#!/usr/bin/env python3\n"
                        "import sys, time\nfrom pathlib import Path\n"
                        f"sys.{channel}.buffer.write(b'x' * {size})\n"
                        f"sys.{channel}.buffer.flush()\n"
                        "time.sleep(2)\n"
                        f"Path({str(marker)!r}).touch()\n")
                    fake.chmod(0o755)
                    with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                        with self.assertRaises(gate.Unmeasurable) as error:
                            gate.git(root, "ls-tree", limit=limit)
                    self.assertEqual(error.exception.code, "git_failed")
                    self.assertFalse(marker.exists(), "Git must be stopped at the cap, not spooled to completion")

    def test_git_stdout_at_exact_limit_is_accepted(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            fake = bindir / "git"
            fake.write_text("#!/usr/bin/env python3\nimport sys\n"
                            "sys.stdout.buffer.write(b'x' * 1024)\n")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                self.assertEqual(gate.git(root, "ls-tree", limit=1024), b"x" * 1024)

    def test_documented_git_deadline_is_pinned_at_thirty_seconds(self):
        # The published collection bound, not an incidental literal: relaxing it
        # silently buys an unbounded hang for every caller of the gate.
        self.assertEqual(load_gate().GIT_TIMEOUT, 30)

    def test_stalled_git_is_timed_out_and_reaped_with_open_or_closed_pipes(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "finished"
            pid_file = root / "pid"
            bindir = root / "bin"
            bindir.mkdir()
            fake = bindir / "git"
            for close_pipes in (False, True):
                with self.subTest(close_pipes=close_pipes):
                    if marker.exists():
                        marker.unlink()
                    fake.write_text(
                        "#!/usr/bin/env python3\n"
                        "import os, time\nfrom pathlib import Path\n"
                        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                        + ("os.close(1); os.close(2)\n" if close_pipes else "")
                        + "time.sleep(10)\n"
                        f"Path({str(marker)!r}).touch()\n")
                    fake.chmod(0o755)
                    start = time.monotonic()
                    with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                        with patch.object(gate, "GIT_TIMEOUT", 0.5):
                            with self.assertRaises(gate.Unmeasurable) as error:
                                gate.git(root, "ls-tree")
                    self.assertEqual(error.exception.code, "git_timeout")
                    self.assertTrue(error.exception.retryable)
                    self.assertLess(time.monotonic() - start, 5)
                    self.assertFalse(marker.exists(), "Stalled Git must be killed before it finishes")
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), 0)


class CollectorTimeouts(unittest.TestCase):
    def test_stalled_producer_is_reaped_with_open_or_closed_pipes(self):
        path = SCRIPT.with_name("capture_ax_report.py")
        spec = importlib.util.spec_from_file_location("ax_collector", path)
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "pid"
            for close_pipes in (False, True):
                with self.subTest(close_pipes=close_pipes):
                    script = ("import os, time\nfrom pathlib import Path\n"
                              f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                              + ("os.close(1); os.close(2)\n" if close_pipes else "")
                              + "time.sleep(10)\n")
                    start = time.monotonic()
                    with patch.object(collector, "TIMEOUT", 0.5):
                        self.assertEqual(collector.capture([sys.executable, "-I", "-c", script]), 124)
                    self.assertLess(time.monotonic() - start, 5)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), 0)


class SourceLimits(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def entry(size, index=1):
        return (b"100644", b"blob", f"{index:040x}".encode(), size)

    def test_single_file_at_limit_is_read_but_one_extra_byte_is_refused(self):
        gate = self.gate
        size = 1024 * 1024
        entry = self.entry(size)
        data = b"x" * size
        raw = entry[2] + b" blob " + str(size).encode() + b"\n" + data + b"\n"
        with patch.object(gate, "git", return_value=raw):
            self.assertEqual(gate.blobs(self.root, {"x": entry}), {"x": data})
        with patch.object(gate, "git") as git:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.blobs(self.root, {"x": self.entry(size + 1)})
            self.assertEqual(error.exception.code, "snapshot_limit")
            git.assert_not_called()

    def test_aggregate_bytes_at_limit_are_accepted_and_next_byte_is_refused(self):
        gate = self.gate
        tree = {f"scripts/{i}.sh": self.entry(1024 * 1024, i + 1) for i in range(32)}
        with patch.object(gate, "blobs", return_value={}) as blobs:
            self.assertEqual(gate.snapshot(self.root, tree, ["scripts/**"], self.root), {})
            blobs.assert_called_once()
        tree["scripts/extra.sh"] = self.entry(1, 33)
        with patch.object(gate, "blobs") as blobs:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.snapshot(self.root, tree, ["scripts/**"], self.root)
            self.assertEqual(error.exception.code, "snapshot_limit")
            blobs.assert_not_called()
        # Independently exercise the blob-allocation guard, not only metadata selection.
        tree.pop("scripts/extra.sh")
        raw = b"".join(e[2] + b" blob 1048576\n" + b"x" * 1048576 + b"\n" for e in tree.values())
        with patch.object(gate, "git", return_value=raw):
            self.assertEqual(sum(map(len, gate.blobs(self.root, tree).values())), 32 * 1024 * 1024)
        tree["scripts/extra.sh"] = self.entry(1, 33)
        with patch.object(gate, "git") as git:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.blobs(self.root, tree)
            self.assertEqual(error.exception.code, "snapshot_limit")
            git.assert_not_called()

    def test_selected_path_count_at_limit_and_one_over(self):
        gate = self.gate
        tree = {f"scripts/{i}.sh": self.entry(0) for i in range(10000)}
        with patch.object(gate, "blobs", return_value={}) as blobs:
            self.assertEqual(gate.snapshot(self.root, tree, ["scripts/**"], self.root), {})
            self.assertEqual(len(blobs.call_args.args[1]), 10000)
        tree["scripts/extra.sh"] = self.entry(0)
        with patch.object(gate, "blobs") as blobs:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.snapshot(self.root, tree, ["scripts/**"], self.root)
            self.assertEqual(error.exception.code, "snapshot_limit")
            blobs.assert_not_called()


class EnvironmentAndEvidence(RepositoryFixture):
    def test_hostile_git_environment_cannot_redirect_the_real_entry_point(self):
        self.put("scripts/new.sh", BAD_SCRIPT)
        head = self.commit()
        self.env.update(GIT_DIR=str(self.root / "missing.git"), GIT_WORK_TREE=str(self.root / "elsewhere"),
                        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.repositoryformatversion",
                        GIT_CONFIG_VALUE_0="999", GIT_OBJECT_DIRECTORY=str(self.root / "missing-objects"))
        report = self.check(1, head=head)
        self.assert_counts(report, 0, 1, 1, 0)

    def test_exact_executing_file_digests_on_clean_and_unmeasurable_results(self):
        for code, base in ((0, self.base), (2, "f" * 40)):
            with self.subTest(code=code):
                report = self.check(code, base=base)
                self.assertEqual(report["checker_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
                self.assertEqual(report["producer_sha256"], hashlib.sha256(PRODUCER.read_bytes()).hexdigest())
                collector = SCRIPT.with_name("capture_ax_report.py")
                self.assertEqual(report["collector_sha256"], hashlib.sha256(collector.read_bytes()).hexdigest())
                self.assertNotEqual(report["checker_sha256"], report["producer_sha256"])

    def test_unreadable_executing_file_digest_is_null_not_an_invented_hash(self):
        gate = load_gate()
        output = io.StringIO()
        missing = self.root / "missing-producer.py"
        with patch.object(gate, "PRODUCER", missing), redirect_stdout(output):
            self.assertEqual(gate.main(self.argv(self.base, self.base)), 2)
        report = json.loads(output.getvalue())
        self.assertIsNone(report["producer_sha256"])
        self.assertEqual(report["checker_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        with patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")):
            self.assertIsNone(gate.fingerprint(SCRIPT))


class ImmutableSnapshots(RepositoryFixture):
    def test_hex_named_branch_is_not_an_immutable_sha1_snapshot(self):
        self.put("scripts/new.sh", BAD_SCRIPT)
        head = self.commit()
        branch = "f" * 64
        self.git("branch", branch, head)
        self.assertEqual(self.check(2, head=branch)["code"], "invalid_sha")
        # Moving the same-named ref must not turn refusal into a clean verdict.
        self.git("branch", "-f", branch, self.base)
        self.assertEqual(self.check(2, head=branch)["code"], "invalid_sha")

    def test_sha256_repository_rejects_wrong_length_and_hex_refs_but_accepts_full_ids(self):
        root = Path(self.tmp.name) / "sha256-repo"
        root.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

        def git(*args):
            return subprocess.check_output(["git", *args], cwd=root, env=env,
                                           text=True, timeout=10).strip()

        git("init", "-q", "--object-format=sha256")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (root / "AGENTS.md").write_text("# map\n")
        (root / POLICY).write_text(json.dumps(POLICY_VALUE))
        git("add", "-A")
        git("commit", "-q", "-m", "test: fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        base = git("rev-parse", "HEAD")
        self.assertEqual(len(base), 64)
        self.assert_counts(self.check(0, base=base, head=base, repo=root), 0, 0, 0, 0)
        self.assertEqual(self.check(2, base=base, head=base[:40], repo=root)["code"], "invalid_sha")
        git("branch", "f" * 64, base)
        self.assertEqual(self.check(2, base=base, head="f" * 64, repo=root)["code"], "invalid_sha")


class WildcardAncestors(RepositoryFixture):
    def wildcard_base(self):
        self.put(POLICY, json.dumps({**POLICY_VALUE, "include": ["scripts/pkg-*/**"]}))
        self.put("scripts/pkg-one/run.sh", BAD_SCRIPT)
        self.base = self.commit()

    def test_symlink_replacing_wildcard_scoped_directory_is_unmeasurable(self):
        self.wildcard_base()
        self.git("rm", "-r", "-q", "scripts/pkg-one")
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts/pkg-one").symlink_to(Path(self.tmp.name) / "elsewhere")
        report = self.check(2, head=self.commit())
        self.assertEqual(report["code"], "unsupported_source")

    def test_gitlink_replacing_wildcard_scoped_directory_is_unmeasurable(self):
        self.wildcard_base()
        self.git("rm", "-r", "-q", "scripts/pkg-one")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},scripts/pkg-one")
        # Commit the index directly: the fixture's add -A would stage the
        # gitlink's deletion because no worktree directory backs it.
        self.git("commit", "-q", "-m", "test: fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        report = self.check(2, head=self.git("rev-parse", "HEAD"))
        self.assertEqual(report["code"], "unsupported_source")


class WorkflowScopeRefusal(RepositoryFixture):
    """Workflow YAML is refused as a diagnostic input, whatever its syntax.

    The scope decision refutes every workflow form by refusing the path, not
    by recognising the form. These cases therefore assert one uniform exit-2
    outcome across the syntax variants an earlier producer tried to parse, so
    no variant can be re-admitted by a later parser change.
    """

    def setUp(self):
        super().setUp()
        self.put(POLICY, json.dumps(WORKFLOW_POLICY))
        self.base = self.commit()

    # One entry per previously-recognised or previously-mis-recognised form.
    VARIANTS = {
        "block-typed": "name: ship\non:\n  workflow_dispatch:\n    inputs:\n"
                       "      target:\n        description: t\n        type: string\n",
        "block-untyped": "name: ship\non:\n  workflow_dispatch:\n    inputs:\n"
                         "      target:\n        description: t\n",
        "quoted-containers": 'name: ship\non:\n  "workflow_dispatch":\n    "inputs":\n'
                             "      target:\n        description: t\n",
        "single-quoted-inputs": "name: ship\non:\n  workflow_dispatch:\n    'inputs':\n"
                                "      target:\n        description: t\n",
        "quoted-outer-on": 'name: ship\n"on":\n  workflow_dispatch:\n    inputs:\n'
                           "      target:\n        description: t\njobs: {}\n",
        "inline-empty": "name: ship\non:\n  workflow_dispatch:\n    inputs: {}\n",
        "inline-nonempty": "on:\n  workflow_dispatch:\n    inputs: {target: {description: x}}\n",
        "utf8-bom": "\ufeffon:\n  workflow_dispatch:\n    inputs:\n"
                    "      target:\n        description: t\n",
        "uniform-root-indent": "  name: ship\n  on:\n    workflow_dispatch:\n"
                               "      inputs:\n        target:\n          description: t\n",
        "multiline-flow": "on: {\n  workflow_dispatch: {inputs: {target: {description: x}}}\n  }\n",
        "explicit-key-node": "on:\n  workflow_dispatch:\n    ? inputs\n    :\n"
                             "      target:\n        description: t\n",
        "anchored-key": "on:\n  workflow_dispatch:\n    &k inputs:\n"
                        "      target:\n        description: t\n",
        "hash-in-plain-scalar": "on: {push: {branches: [feature#1]}, workflow_dispatch: "
                                "{inputs: {target: {description: x}}}}\n",
        "next-line-scalar": "on:\n  workflow_dispatch:\n    inputs:\n      target:\n"
                            "        description:\n          \"Choose target\n"
                            "        type: string\n        done\"\n",
        "anchored-block-run": "on:\n  workflow_dispatch:\n    inputs:\n      target:\n"
                              "        description: t\njobs:\n  b:\n    steps:\n"
                              "      - run: &shell |\n          echo \"stage: 'ready\"\n",
        "empty-function": "",
    }

    def test_every_workflow_variant_is_unmeasurable_not_clean(self):
        for label, text in self.VARIANTS.items():
            with self.subTest(variant=label):
                self.put(WORKFLOW_PATH, text)
                report = self.check(2)
                self.assertEqual(report["code"], "unsupported_coverage", report)
                self.assertIn(WORKFLOW_PATH, report["refused"]["head"])

    def test_base_only_workflow_refuses_even_when_the_head_removes_it(self):
        # Policy comes from the base, so a workflow in the base scope alone
        # refuses; deleting it in the candidate cannot buy a clean verdict.
        self.put(WORKFLOW_PATH, self.VARIANTS["block-untyped"])
        self.base = self.commit()
        self.git("rm", "-q", WORKFLOW_PATH)
        report = self.check(2)
        self.assertEqual(report["refused"], {"base": [WORKFLOW_PATH], "head": []})

    def test_head_only_workflow_refuses_even_when_the_base_was_clean(self):
        # The base carries the workflow-capable policy but no workflow file,
        # so only the candidate introduces the refused input.
        self.base = self.commit()
        self.put(WORKFLOW_PATH, self.VARIANTS["utf8-bom"])
        report = self.check(2)
        self.assertEqual(report["refused"], {"base": [], "head": [WORKFLOW_PATH]})

    def test_workflow_present_in_both_snapshots_still_refuses(self):
        # The grandfathered-syntax case the decision names explicitly: sharing
        # syntax with the base is not a licence to measure it.
        self.put(WORKFLOW_PATH, self.VARIANTS["block-untyped"])
        self.base = self.commit()
        report = self.check(2, head=self.base)
        self.assertEqual(report["refused"], {"base": [WORKFLOW_PATH], "head": [WORKFLOW_PATH]})

    def test_refusal_precedes_parsing_and_reports_no_counts(self):
        # A refused snapshot must not report findings it never measured; a
        # clean-but-unparsed workflow must not look like successful coverage.
        self.put(WORKFLOW_PATH, self.VARIANTS["inline-empty"])
        report = self.check(2)
        for key in ("counts", "new", "resolved", "coverage"):
            self.assertNotIn(key, report, "A refused run must not report a measurement")
        self.assertFalse(report["retryable"], report)

    def test_yaml_suffix_is_refused_in_both_spellings(self):
        # Fresh fixture per spelling: a leftover workflow from the previous
        # subtest would otherwise be the one named in the refusal.
        for name in (".github/workflows/ship.yml", ".github/workflows/ship.yaml"):
            with self.subTest(name=name):
                self.setUp()
                self.put(name, self.VARIANTS["block-untyped"])
                report = self.check(2)
                self.assertEqual(report["refused"]["head"], [name])


def producer_report_failing_open(root, blocked, *, every_read=False):
    """Run the real producer with ``open`` failing for *blocked*.

    Runs in a separate interpreter so the patch never leaks into the test
    process. ``every_read`` fails both the candidate probe and the text read;
    otherwise only the first read (the probe) fails and the second succeeds.
    """
    root, blocked = str(root), str(blocked)
    script = f"""
import builtins, io, json, runpy, sys
root, blocked, every = {root!r}, {blocked!r}, {str(every_read)!r}
real_open = builtins.open
seen = {{"blocked_reads": 0, "reads": 0}}

def guarded_open(file, *a, **k):
    path = str(file)
    if path.startswith(blocked):
        seen["reads"] += 1
        if every or seen["reads"] == 1:
            seen["blocked_reads"] += 1
            raise PermissionError(13, "Permission denied", path)
    return real_open(file, *a, **k)

builtins.open = guarded_open
sys.argv = ["ax_lint.py", "--root", root, "--mode", "warn", "--json"]
buffer = io.StringIO()
stdout, sys.stdout = sys.stdout, buffer
status = 0
try:
    runpy.run_path({str(PRODUCER)!r}, run_name="__main__")
except SystemExit as exit_status:
    status = exit_status.code or 0
finally:
    sys.stdout = stdout
sys.stderr.write(buffer.getvalue())
sys.stdout.write(buffer.getvalue())
raise SystemExit(status)
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                            capture_output=True, text=True, timeout=30)
    return result


def failing_producer_scan(gate, blocked):
    """Replace the gate's producer call with the real producer, one read lost.

    Returns the gate's own ``(identities, coverage)`` shape by reusing its
    real validator, so the injected failure travels the production path from
    report bytes to refusal. *gate* is the already-loaded module: loading it
    again would define a second ``Unmeasurable`` class that callers cannot
    catch.
    """
    def scan(root, patterns, sources):
        result = producer_report_failing_open(root, Path(root) / blocked, every_read=True)
        assert result.returncode == 0, result.stderr
        return gate.validate_report(result.stdout, patterns, sources)
    return scan


class ReadFailuresFailMeasurement(RepositoryFixture):
    """A lost source read is unknown coverage, never a clean scan.

    The checker materializes its snapshot with fixed 0644/0755 modes, so a
    chmod cannot reproduce the review's I/O failure end to end. The producer
    is therefore driven directly with ``open`` failing for one scoped script,
    which is the real scan path that used to ``return False`` / ``continue``.
    """

    def test_real_producer_reports_unreadable_script_as_unknown_coverage(self):
        self.put("scripts/deploy.sh", BAD_SCRIPT)
        blocked = self.put("scripts/blocked.sh", BAD_SCRIPT)
        result = producer_report_failing_open(self.root, blocked, every_read=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)

        # The readable defect is still measured; the lost read is reported.
        self.assertEqual([(f["rule_id"], f["path"]) for f in report["findings"]],
                         [(SCRIPT_RULE, "scripts/deploy.sh")])
        unknown = report["coverage"]["unknown"]
        self.assertEqual([entry["path"] for entry in unknown], ["scripts/blocked.sh"])
        self.assertEqual(unknown[0]["category"], "unknown")
        self.assertTrue(unknown[0]["reason"].strip(),
                        "A lost read must carry a reason, not a blank coverage entry")

    def test_unreadable_candidate_detection_is_unknown_not_skipped(self):
        # Shebang/candidate detection is its own read. Failing it must not make
        # the file look like a non-candidate that was cleanly excluded: it is
        # reported unknown, and the ratchet then refuses the whole measurement.
        # The file is deliberately not scanned after the failed probe; issuing
        # a second read would contradict the first and could silently disagree.
        blocked = self.put("scripts/blocked.sh", BAD_SCRIPT)
        result = producer_report_failing_open(self.root, blocked)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([entry["path"] for entry in report["coverage"]["unknown"]],
                         ["scripts/blocked.sh"], report)
        self.assertEqual([f["path"] for f in report["findings"]], [],
                         "An unmeasurable file must not also be reported as a finding")

    def test_read_failure_inside_the_ratchet_is_unmeasurable_not_clean(self):
        # End to end through the real environment precedence: the base scan
        # reports unknown coverage for an in-scope unreadable script, and the
        # ratchet refuses with unsupported_coverage rather than reporting a
        # clean delta with empty coverage (the defect in the 3/5 review).
        self.put(POLICY, json.dumps(dict(POLICY_VALUE, include=["AGENTS.md", "scripts/**"])))
        self.put("scripts/deploy.sh", BAD_SCRIPT)
        self.base = self.commit()
        gate = load_gate()
        with patch.object(gate, "PRODUCER", PRODUCER), \
                patch.object(gate, "scan", failing_producer_scan(gate, "scripts/deploy.sh")):
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.check(self.root, self.base, self.base)
        self.assertEqual(error.exception.code, "unsupported_coverage")
        self.assertIn("Rewrite unsupported scoped inputs", error.exception.next_action)

    def test_readable_scope_stays_clean(self):
        # Control: the same fixture without a lost read measures clean, so the
        # refusal above is caused by the read failure alone.
        self.put(POLICY, json.dumps(dict(POLICY_VALUE, include=["AGENTS.md", "scripts/**"])))
        self.base = self.commit()
        report = self.check(0, head=self.base)
        self.assert_counts(report, 0, 0, 0, 0)


class DispatchProducerBoundaries(unittest.TestCase):
    def scan(self, workflow):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# map\n")
            path = root / WORKFLOW_PATH
            path.parent.mkdir(parents=True)
            path.write_text(workflow)
            result = subprocess.run([sys.executable, str(PRODUCER), "--root", str(root),
                                     "--mode", "warn", "--json"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_non_event_mappings_and_run_scalar_are_not_events(self):
        report = self.scan('''name: test
on: push
env:
  "on": workflow_dispatch
  workflow_dispatch:
    inputs:
      wrong:
        description: data
jobs:
  build:
    steps:
      - run: |
          cat <<'EOF'
          on:
            workflow_dispatch:
              inputs:
                fake:
                  description: data
          EOF
''')
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["coverage"], {"skipped": [], "unknown": []})

    def test_description_scalar_does_not_supply_type(self):
        for description in ("|\n          type: string", ">-\n          type: choice",
                            '"description\n          type: string\n          end"'):
            with self.subTest(description=description):
                report = self.scan("on:\n  workflow_dispatch:\n    inputs:\n"
                                   "      target:\n        description: " + description + "\n")
                self.assertEqual([f["rule_id"] for f in report["findings"]],
                                 ["dispatch-inputs/untyped"])
                self.assertEqual(report["coverage"]["unknown"], [])

    def test_unsupported_types_and_event_aliases_are_unknown(self):
        cases = (
            "on:\n  workflow_dispatch:\n    inputs:\n      target:\n        type: |\n          choice\n",
            "on:\n  workflow_dispatch:\n    inputs:\n      target:\n        type: >-\n          choice\n",
            "on:\n  workflow_dispatch:\n    inputs:\n      target:\n        type: *kind\n",
            'on:\n  workflow_dispatch:\n    inputs:\n      target:\n        type: "ch\\u006fice"\n',
            'on:\n  workflow_dispatch:\n    inputs:\n      target:\n        type: "multi\n          line"\n',
            '"o\\u006e":\n  workflow_dispatch:\n    inputs: {}\n',
            'on:\n  "workflow_\\u0064ispatch":\n    inputs: {}\n',
            "on: *events\n",
            "on: [push, *events]\n",
            'on: {"workflow_\\u0064ispatch": {}}\n',
            "on:\n  workflow_dispatch: *dispatch\n",
            "on:\n  workflow_dispatch:\n    inputs: *inputs\n",
            "on:\n  <<: *events\n",
        )
        for text in cases:
            with self.subTest(text=text):
                report = self.scan(text)
                self.assertTrue(report["coverage"]["unknown"], report)

    def test_literal_quoted_containers_and_choice_controls(self):
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                prefix = (f"{quote}on{quote}:\n  {quote}workflow_dispatch{quote}:\n"
                          f"    {quote}inputs{quote}:\n      target:\n")
                report = self.scan(prefix + "        type: choice\n")
                self.assertEqual([f["rule_id"] for f in report["findings"]],
                                 ["dispatch-inputs/choice-without-options"])
                report = self.scan(prefix + "        type: choice\n        options:\n          - one\n")
                self.assertEqual(report["findings"], [])
                self.assertEqual(report["coverage"]["unknown"], [])


class DispatchCoverageRatchet(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.put(POLICY, json.dumps(WORKFLOW_POLICY))
        self.base = self.commit()

    def test_unsupported_shapes_never_become_clean(self):
        # These were unmeasurable because the scanner could not parse them.
        # They stay unmeasurable for the stronger reason: workflow YAML is not
        # a supported diagnostic input at all, so no shape can be re-admitted
        # by a future parser improvement.
        for text in ("on: *events\n",
                     '"o\\u006e":\n  workflow_dispatch: {}\n',
                     "on:\n  workflow_dispatch:\n    inputs:\n"
                     "      target:\n        type: |\n          choice\n"):
            with self.subTest(text=text):
                self.put(WORKFLOW_PATH, text)
                report = self.check(2)
                self.assertEqual(report["code"], "unsupported_coverage", report)
                self.assertEqual(report["refused"]["head"], [WORKFLOW_PATH])


class IntegrationContract(unittest.TestCase):
    def test_reusable_workflow_artifact_name_is_caller_selectable_without_overwrite(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/diagnostic-delta.yml").read_text(), Loader=yaml.BaseLoader)
        for trigger in ("workflow_call", "workflow_dispatch"):
            entry = workflow["on"][trigger]["inputs"]["artifact-name"]
            self.assertEqual(entry["required"], "false")
            self.assertEqual(entry["default"], "diagnostic-delta-report")
        step, = [s for s in workflow["jobs"]["diagnostic-delta"]["steps"]
                 if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(step["with"]["name"], "${{ inputs.artifact-name }}")
        self.assertNotIn("overwrite", step["with"])
    def test_ci_keeps_all_suites_and_uses_prepared_python_and_yaml(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        step, = [s for job in workflow["jobs"].values() for s in job.get("steps", [])
                 if s.get("name") == "quality-gate helper suites"]
        for suite in ("test_quality_gates", "test_floor_guard", "test_no_pull_request_target",
                      "test_docs_drift", "test_docs_explain_acceptance", "test_diagnostic_delta",
                      "test_diagnostic_delta_workflow", "test_diagnostic_delta_hardening"):
            self.assertIn(suite, step["run"].split())
        self.assertIn('"$STATIC_LABEL_PYTHON"', step["run"])
        self.assertIn("static-label-python.outputs.python-path", step["env"]["STATIC_LABEL_PYTHON"])
        self.assertEqual(step["env"]["PYTHONPATH"], "${{ runner.temp }}/static-label-guard-deps")

    def test_new_workflow_has_a_matching_hung_job_threshold(self):
        thresholds = json.loads((ROOT / "ops/case_e1e23b50/hung_job_thresholds.json").read_text())
        self.assertEqual(thresholds["jobs"].get("diagnostic-delta"), 5)

    def test_tools_ref_validator_executes_and_rejects_a_64_character_value(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/diagnostic-delta.yml").read_text(), Loader=yaml.BaseLoader)
        step, = [s for s in workflow["jobs"]["diagnostic-delta"]["steps"]
                 if s.get("name") == "Validate immutable inputs"]
        cases = (({"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": "c" * 40}, 0),
                 ({"base-sha": "a" * 64, "head-sha": "b" * 64, "tools-ref": "c" * 40}, 1),
                 ({"base-sha": "a" * 64, "head-sha": "b" * 40, "tools-ref": "c" * 40}, 1),
                 ({"base-sha": "a" * 40, "head-sha": "b" * 64, "tools-ref": "c" * 40}, 1),
                 ({"base-sha": "a" * 64, "head-sha": "b" * 64, "tools-ref": "c" * 64}, 1),
                 ({"base-sha": "a" * 40, "head-sha": "b" * 40, "tools-ref": "c" * 64}, 1))
        for inputs, expected in cases:
            with self.subTest(inputs=inputs):
                env = {"PATH": os.environ["PATH"],
                       "BASE_SHA": inputs["base-sha"], "HEAD_SHA": inputs["head-sha"],
                       "TOOLS_REF": inputs["tools-ref"], "CALL_INPUTS": json.dumps(inputs)}
                result = subprocess.run(["bash", "--noprofile", "--norc", "-c", step["run"]],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode == 0, expected == 0, result.stderr)

    def test_report_path_resolves_inside_each_distinct_workspace(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/diagnostic-delta.yml").read_text(), Loader=yaml.BaseLoader)
        raw = workflow["jobs"]["diagnostic-delta"]["env"]["REPORT_PATH"]
        self.assertTrue(raw.startswith("${{ github.workspace }}/"), raw)
        resolved = set()
        for workspace in ("/tmp/fixture-workspace-a", "/tmp/fixture-workspace-b"):
            path = raw.replace("${{ github.workspace }}", workspace)
            self.assertNotEqual(path, raw, "Report path must derive from the workspace")
            self.assertEqual(os.path.commonpath((path, workspace)), workspace)
            self.assertTrue(path.endswith(".json"), path)
            resolved.add(path)
        self.assertEqual(len(resolved), 2, "Distinct workspaces must not share one report path")


class ScopeAndOfflineBoundary(RepositoryFixture):
    def test_subdirectory_repo_argument_and_default_cwd_keep_root_policy(self):
        self.put("scripts/new.sh", BAD_SCRIPT)
        head = self.commit()
        report = self.check(1, head=head, repo=self.root / "scripts")
        self.assert_counts(report, 0, 1, 1, 0)
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT),
                                 "--base", self.base, "--head", head],
                                cwd=self.root / "scripts", env=self.env,
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["new"], 1)

    def test_recursive_glob_covers_direct_and_nested_children_but_star_does_not(self):
        for pattern, expected in (("scripts/**/*.sh", 2), ("scripts/*.sh", 1)):
            with self.subTest(pattern=pattern):
                self.put(POLICY, json.dumps({**POLICY_VALUE, "include": [pattern]}))
                base = self.commit()
                self.put("scripts/run.sh", BAD_SCRIPT)
                self.put("scripts/sub/run.sh", BAD_SCRIPT)
                head = self.commit()
                report = self.check(1, base=base, head=head)
                self.assert_counts(report, 0, expected, expected, 0)
                self.assertEqual({x["path"] for x in report["new"]},
                                 {"scripts/run.sh", "scripts/sub/run.sh"} if expected == 2
                                 else {"scripts/run.sh"})
                (self.root / "scripts/run.sh").unlink()
                (self.root / "scripts/sub/run.sh").unlink()
                self.commit()

    def test_glob_components_and_zero_directory_double_star(self):
        gate = load_gate()
        for path, pattern, expected in (
            ("run.sh", "**/*.sh", True),
            ("scripts/run.sh", "scripts/**/run.sh", True),
            ("scripts/a/b/run.sh", "scripts/**/run.sh", True),
            ("scripts/a/b/run.sh", "scripts/*/run.sh", False),
            ("scripts/x/run.py", "scripts/?/run.[ps][yh]", True),
            ("scripts/xy/run.py", "scripts/?/run.[ps][yh]", False),
            ("scripts/run.sh", "Scripts/**/*.sh", False),
            ("scripts/run.sh", "scripts/*", True),
            ("scripts/a/run.sh", "scripts/*", False),
        ):
            with self.subTest(path=path, pattern=pattern):
                self.assertEqual(gate.matches(path, [pattern]), expected)

    def test_missing_promisor_blob_cannot_fetch_when_old_git_ignores_lazy_guard(self):
        import shutil
        self.put("scripts/remote.sh", BAD_SCRIPT)
        head = self.commit()
        oid = self.git("rev-parse", head + ":scripts/remote.sh")
        remote = Path(self.tmp.name) / "remote.git"
        self.git("clone", "-q", "--bare", str(self.root), str(remote))
        self.git("config", "core.repositoryformatversion", "1")
        self.git("config", "extensions.partialClone", "origin")
        self.git("config", "remote.origin.url", remote.as_uri())
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "protocol.file.allow", "always")
        blob = self.root / ".git/objects" / oid[:2] / oid[2:]
        blob.unlink()
        objects = set((self.root / ".git/objects").rglob("*"))
        # Emulate the missing environment guard on older Git, not an old binary.
        # All transport targets are local temporary fixtures; no network is used.
        # Resolve only the OS search path, before inserting the fixture shim.
        # A managed launcher found on inherited PATH can delegate back to this
        # shim and recursively spawn processes instead of executing Git.
        found = shutil.which("git", path=os.defpath)
        self.assertIsNotNone(found, "The offline fixture requires system Git")
        real_git = str(Path(found).resolve(strict=True))
        bindir = Path(self.tmp.name) / "old-git"
        bindir.mkdir()
        wrapper = bindir / "git"
        wrapper.write_text(f"#!{sys.executable}\nimport os, sys\n"
                           "if os.environ.get('AX_FIXTURE_GIT_ACTIVE') == '1':\n"
                           "    sys.exit('recursive fixture Git invocation refused')\n"
                           "os.environ['AX_FIXTURE_GIT_ACTIVE'] = '1'\n"
                           f"os.environ['PATH'] = {os.defpath!r}\n"
                           "os.environ.pop('GIT_NO_LAZY_FETCH', None)\n"
                           f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        self.assertNotEqual(wrapper.resolve(), Path(real_git))
        self.env["PATH"] = f"{bindir}:{self.env['PATH']}"
        # Exercise the refusal without causing recursive processes. Each probe
        # has its own short deadline, independent of the gate's Git timeout.
        recursive = subprocess.run([str(wrapper), "--version"],
                                   env={**self.env, "AX_FIXTURE_GIT_ACTIVE": "1"},
                                   capture_output=True, text=True, timeout=3)
        self.assertNotEqual(recursive.returncode, 0)
        self.assertIn("recursive fixture Git invocation refused", recursive.stderr)
        version = subprocess.run([str(wrapper), "--version"], env=self.env,
                                 capture_output=True, text=True, timeout=3)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertTrue(version.stdout.startswith("git version "), version.stdout)
        report = self.check(2, head=head)
        # Git may either fail the command or print an unavailable size in the
        # tree row. Both are unmeasurable; neither may hydrate the object.
        self.assertIn(report["code"], ("git_failed", "invalid_tree"))
        self.assertEqual(set((self.root / ".git/objects").rglob("*")), objects,
                         "Measurement must not hydrate any missing object")
        # Control: the promisor is genuinely usable without the transport fence.
        self.assertEqual(self.git("cat-file", "-p", oid), BAD_SCRIPT.strip())
        self.assertEqual(self.check(1, head=head)["counts"]["new"], 1)


class OuterCollectorTimeout(unittest.TestCase):
    def test_stopped_collector_cannot_leave_producer_running_after_outer_timeout(self):
        import signal
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pids"
            marker = root / "finished"
            producer = root / "producer.py"
            producer.write_text(
                "import os, signal, time\nfrom pathlib import Path\n"
                f"Path({str(pid_file)!r}).write_text(f'{{os.getpid()}} {{os.getppid()}}')\n"
                "os.kill(os.getppid(), signal.SIGSTOP)\n"
                "time.sleep(10)\n"
                f"Path({str(marker)!r}).touch()\n")
            real_run = subprocess.run

            def short_deadline(*args, **kwargs):
                self.assertLessEqual(kwargs["timeout"], 30)
                return real_run(*args, **{**kwargs, "timeout": 2})

            pids = []
            start = time.monotonic()
            try:
                with patch.object(gate, "PRODUCER", producer), \
                        patch.object(subprocess, "run", side_effect=short_deadline):
                    with self.assertRaises(gate.Unmeasurable) as error:
                        gate.scan(root, ["**"], {})
                self.assertEqual(error.exception.code, "producer_timeout")
                self.assertTrue(error.exception.retryable)
                self.assertLess(time.monotonic() - start, 5)
                pids = [int(value) for value in pid_file.read_text().split()]
                # An orphan may await init's reap, but it must no longer execute.
                for pid in pids:
                    proc = Path(f"/proc/{pid}/stat")
                    for _ in range(50):
                        if not proc.exists() or proc.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                            break
                        time.sleep(0.01)
                    else:
                        self.fail(f"Process {pid} survived outer collector timeout")
                self.assertFalse(marker.exists())
            finally:
                if pid_file.exists():
                    pids = [int(value) for value in pid_file.read_text().split()]
                for pid in pids:
                    if pid in (os.getpid(), os.getppid()):
                        continue
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


def load_producer():
    spec = importlib.util.spec_from_file_location("ax_retained_scan", PRODUCER)
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    return producer


class RetainedSourceBoundaries(RepositoryFixture):
    def scoped_head(self, name="deploy.sh", executable=False):
        self.put(POLICY, json.dumps({**POLICY_VALUE, "include": ["scripts/quality-gates/**"]}))
        self.base = self.commit()
        path = self.put("scripts/quality-gates/" + name, BAD_SCRIPT)
        if executable:
            path.chmod(0o755)
        return self.commit()

    def test_noexec_mount_does_not_hide_git_executable_source(self):
        head = self.scoped_head("deploy", executable=True)
        gate = load_gate()
        self.assertEqual(gate.check(self.root, self.base, head)["counts"]["new"], 1)
        # Classification is metadata-only even when effective execution is denied.
        producer = load_producer()
        with patch.object(producer.os, "access", return_value=False):
            findings, unknown = producer._script_scan_all(self.root)
        self.assertEqual([f["path"] for f in findings], ["scripts/quality-gates/deploy"])
        self.assertEqual(unknown, [])

    def test_actual_noexec_projection_preserves_new_identity(self):
        noexec = Path("/dev/shm")
        if not noexec.is_dir():
            self.skipTest("No provisioned noexec temporary storage")
        real_temporary = tempfile.TemporaryDirectory
        with real_temporary(dir=noexec, prefix="ax-noexec-control-") as tmp:
            control = Path(tmp) / "control"
            control.write_text(BAD_SCRIPT)
            control.chmod(0o755)
            if os.access(control, os.X_OK):
                self.skipTest("Provisioned temporary storage is executable")
        head = self.scoped_head("deploy", executable=True)
        gate = load_gate()
        with patch.object(gate.tempfile, "TemporaryDirectory",
                          side_effect=lambda **kw: real_temporary(dir=noexec, **kw)):
            report = gate.check(self.root, self.base, head)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["counts"]["new"], 1)

    def test_real_scan_metadata_and_ancestor_failures_refuse_measurement(self):
        head = self.scoped_head()
        gate, producer = load_gate(), load_producer()
        self.assertEqual(gate.check(self.root, self.base, head)["counts"]["new"], 1)
        for fault in ("stat", "ancestor"):
            with self.subTest(fault=fault):
                observed = []

                def scan(root, patterns, sources):
                    real_stat, real_lstat, real_scandir = os.stat, os.lstat, os.scandir
                    blocked = root / ("scripts/quality-gates/deploy.sh" if fault == "stat" else "scripts")

                    def guarded(real):
                        def call(path, *args, **kwargs):
                            if root.name == "head" and os.fspath(path) == str(blocked):
                                observed.append(str(path))
                                raise OSError(5, "fixture source metadata/listing failure", str(path))
                            return real(path, *args, **kwargs)
                        return call

                    output = io.StringIO()
                    with patch.object(os, "stat", guarded(real_stat) if fault == "stat" else real_stat), \
                            patch.object(os, "lstat", guarded(real_lstat) if fault == "stat" else real_lstat), \
                            patch.object(os, "scandir", guarded(real_scandir) if fault == "ancestor" else real_scandir), \
                            redirect_stdout(output):
                        self.assertEqual(producer.main(["--root", str(root), "--mode", "warn", "--json"]), 0)
                    return gate.validate_report(output.getvalue(), patterns, sources)

                with patch.object(gate, "scan", scan):
                    with self.assertRaises(gate.Unmeasurable) as error:
                        gate.check(self.root, self.base, head)
                self.assertTrue(observed, "Exercise the real scan's failing metadata/listing boundary")
                self.assertEqual(error.exception.code, "unsupported_coverage")

    def test_unrelated_unknown_directory_does_not_expand_selected_scope(self):
        gate = load_gate()
        report = {"tool": "ax-lint", "mode": "warn", "root": ".", "findings": [],
                  "summary": {"findings": 0, "mode": "warn"},
                  "coverage": {"skipped": [], "unknown": [
                      {"path": "elsewhere", "line": None, "category": "unknown", "reason": "unreadable"}]}}
        self.assertEqual(gate.validate_report(json.dumps(report), ["scripts/quality-gates/**"],
                                             {"scripts/quality-gates/run.sh": b""}),
                         (set(), {"skipped": [], "unknown": []}))


class HostedBaseAvailability(RepositoryFixture):
    def test_pr_only_base_is_fetched_explicitly_before_head_history(self):
        import yaml
        steps = yaml.load((ROOT / ".github/workflows/diagnostic-delta.yml").read_text(),
                          Loader=yaml.BaseLoader)["jobs"]["diagnostic-delta"]["steps"]
        callers = [s for s in steps if s.get("with", {}).get("path") == "caller"]
        self.assertEqual([s["with"]["ref"] for s in callers],
                         ["${{ env.BASE_SHA }}", "${{ env.HEAD_SHA }}"])
        for step in callers:
            self.assertEqual(step["with"]["persist-credentials"], "false")
        private_base = self.git("commit-tree", self.base + "^{tree}", "-p", self.base,
                                "-m", "Independent PR-only base")
        self.put("scripts/new.sh", BAD_SCRIPT)
        head = self.commit()
        remote = Path(self.tmp.name) / "remote.git"
        self.git("clone", "-q", "--bare", str(self.root), str(remote))
        self.git("--git-dir=" + str(remote), "update-ref", "refs/pull/17/head", private_base)
        caller = Path(self.tmp.name) / "caller"
        caller.mkdir()

        def git(*args):
            return subprocess.check_output(["/usr/bin/git", "-C", str(caller), *args],
                                           env=self.env, text=True, stderr=subprocess.STDOUT,
                                           timeout=10).strip()

        git("init", "-q")
        git("remote", "add", "origin", remote.as_uri())
        # Same branch/tag refspec boundary as checkout's fetch-depth=0, not a
        # claim that this local fixture executes the hosted checkout action.
        history = ("+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
        git("fetch", "--no-tags", "origin", *history)
        with self.assertRaises(subprocess.CalledProcessError):
            git("cat-file", "-e", private_base + "^{commit}")
        # The wrapper first requests base by exact OID, then updates head in
        # the same object database. No credential needs to remain persisted.
        git("fetch", "--no-tags", "--depth=1", "origin", private_base)
        git("fetch", "--no-tags", "--unshallow", "origin", *history)
        git("checkout", "--detach", head)
        report = load_gate().check(caller, private_base, head)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["counts"]["new"], 1)
        self.assertEqual(report["base_sha"], private_base)


class StandaloneIncompleteCoverage(unittest.TestCase):
    def test_unsupported_roots_and_scalar_decorations_are_unknown_not_clean(self):
        from test_diagnostic_delta import WORKFLOW_VARIANTS
        producer = load_producer()
        for name in ("bom", "indented_root", "anchored_block_scalar"):
            with self.subTest(name=name):
                found, skipped, unknown = producer._scan_dispatch(
                    WORKFLOW_PATH, WORKFLOW_VARIANTS[name].splitlines())
                self.assertEqual(found, [])
                self.assertEqual(skipped, [])
                self.assertTrue(unknown, "Unsupported syntax must not silently clear standalone callers")
                self.assertTrue(all(entry["path"] == WORKFLOW_PATH and entry["reason"] for entry in unknown))
        found, skipped, unknown = producer._scan_dispatch(WORKFLOW_PATH,
            "on:\n  workflow_dispatch:\n    inputs:\n      target:\n        description: destination\n".splitlines())
        self.assertEqual([item["rule_id"] for item in found], ["dispatch-inputs/untyped"])
        self.assertEqual((skipped, unknown), ([], []))

    def test_listing_failure_is_visible_and_error_mode_fails_in_both_formats(self):
        producer = load_producer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# map\n")
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            real_listdir = os.listdir
            observed = []

            def denied(path):
                if os.fspath(path) == str(workflows):
                    observed.append(str(path))
                    raise PermissionError(13, "fixture workflow listing denied", str(path))
                return real_listdir(path)

            for mode in ("warn", "error"):
                for machine in (False, True):
                    with self.subTest(mode=mode, machine=machine):
                        output = io.StringIO()
                        with patch.object(os, "listdir", denied), redirect_stdout(output):
                            result = producer.main(["--root", str(root), "--mode", mode]
                                                   + (["--json"] if machine else []))
                        self.assertEqual(result, int(mode == "error"))
                        if machine:
                            report = json.loads(output.getvalue())
                            self.assertEqual(report["findings"], [])
                            self.assertEqual([item["path"] for item in report["coverage"]["unknown"]],
                                             [".github/workflows"])
                        else:
                            self.assertIn("[coverage/unknown]", output.getvalue())
                            self.assertIn("workflow directory listing failed", output.getvalue())
            self.assertEqual(len(observed), 4)


class ProjectionIdentityBoundaries(unittest.TestCase):
    def test_file_alias_cannot_overwrite_a_distinct_git_identity(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = {name: SourceLimits.entry(len(BAD_SCRIPT))
                    for name in ("scripts/A.sh", "scripts/a.sh")}
            sources = {name: BAD_SCRIPT.encode() for name in tree}
            real_open, real_chmod = Path.open, Path.chmod
            names = {}

            def alias(path):
                return names.setdefault(str(path).casefold(), path)

            def opened(path, *args, **kwargs):
                return real_open(alias(path), *args, **kwargs)

            def chmod(path, *args, **kwargs):
                return real_chmod(alias(path), *args, **kwargs)

            # Controlled equivalent, not a native case-insensitive filesystem.
            with patch.object(gate, "blobs", return_value=sources), \
                    patch.object(Path, "open", opened), patch.object(Path, "chmod", chmod):
                with self.assertRaises(gate.Unmeasurable) as error:
                    gate.snapshot(root, tree, ["scripts/**"], root)
            self.assertEqual(error.exception.code, "unsupported_projection")
            self.assertEqual((root / "scripts/A.sh").read_bytes(), BAD_SCRIPT.encode())

    def test_round_trip_inventory_refuses_directory_aliases(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = {name: SourceLimits.entry(0) for name in ("scripts/A/x.sh", "scripts/a/y.sh")}
            with patch.object(gate, "blobs", return_value={name: b"" for name in tree}), \
                    patch.object(gate.os, "walk", return_value=[(str(root / "scripts/A"), [], ["x.sh", "y.sh"])]):
                with self.assertRaises(gate.Unmeasurable) as error:
                    gate.snapshot(root, tree, ["scripts/**"], root)
            self.assertEqual(error.exception.code, "unsupported_projection")

    def test_case_distinct_sources_remain_distinct_on_supported_storage(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = {"scripts/A.sh": b"upper", "scripts/a.sh": b"lower"}
            tree = {name: SourceLimits.entry(len(raw)) for name, raw in sources.items()}
            with patch.object(gate, "blobs", return_value=sources):
                self.assertEqual(gate.snapshot(root, tree, ["scripts/**"], root), sources)
            self.assertEqual({name: (root / name).read_bytes() for name in sources}, sources)


if __name__ == "__main__":
    unittest.main()
