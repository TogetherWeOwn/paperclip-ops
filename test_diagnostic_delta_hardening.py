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
                                   POLICY, POLICY_VALUE, WORKFLOW_PATH)

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


class QuotedDispatchContainers(RepositoryFixture):
    @staticmethod
    def quoted(container, typed, quote='"'):
        name = "workflow_dispatch" if container == "workflow_dispatch" else "inputs"
        quoted = f"{quote}{name}{quote}:"
        # Only the container under test is quoted; the others stay bare so the
        # block recognition path being exercised is unambiguous.
        dispatch_line = quoted if container == "workflow_dispatch" else "workflow_dispatch:"
        inputs_line = quoted if container == "inputs" else "inputs:"
        text = (f"name: ship\non:\n  {dispatch_line}\n    {inputs_line}\n"
                "      target:\n        description: deployment target\n")
        return text + ("        type: string\n" if typed else "")

    def test_quoted_containers_do_not_silently_clear_untyped_inputs(self):
        for container in ("workflow_dispatch", "inputs"):
            with self.subTest(container=container):
                self.put(WORKFLOW_PATH, self.quoted(container, True))
                self.base = self.commit()
                self.assert_counts(self.check(0, head=self.base), 0, 0, 0, 0)
                self.put(WORKFLOW_PATH, self.quoted(container, False))
                self.assert_counts(self.check(1), 0, 1, 1, 0)

    def test_single_quoted_inputs_block_preserves_typed_and_untyped_classification(self):
        # Block-recognition regex, not the inline fallback: a bare
        # workflow_dispatch with a single-quoted `inputs:` key must still be
        # parsed as a real block, so dropping single-quote support there cannot
        # silently reclassify a typed input as clean or an untyped one as unknown.
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                self.put(WORKFLOW_PATH, self.quoted("inputs", True, quote=quote))
                self.base = self.commit()
                self.assert_counts(self.check(0, head=self.base), 0, 0, 0, 0)
                self.put(WORKFLOW_PATH, self.quoted("inputs", False, quote=quote))
                self.assert_counts(self.check(1), 0, 1, 1, 0)

    def test_quoted_outer_on_inline_nonempty_inputs_are_unmeasurable(self):
        for label, outer in (("bare", "on"), ("double", '"on"'), ("single", "'on'")):
            with self.subTest(outer=label):
                self.put(WORKFLOW_PATH,
                         f"name: ship\n{outer}: {{workflow_dispatch: "
                         "{inputs: {target: {description: x}}}}}\njobs: {}\n")
                self.assertEqual(self.check(2)["code"], "unsupported_coverage")

    def test_quoted_outer_on_block_untyped_input_is_a_finding(self):
        self.put(WORKFLOW_PATH,
                 'name: ship\n"on":\n  workflow_dispatch:\n    inputs:\n'
                 "      target:\n        description: deployment target\njobs: {}\n")
        self.assert_counts(self.check(1), 0, 1, 1, 0)

    def test_quoted_empty_containers_are_clean_with_exact_retained_skips(self):
        cases = (
            ('on:\n  workflow_dispatch:\n    "inputs": {}\n', 3,
             "workflow_dispatch inputs use an inline empty form; nothing to type-check"),
            ("on:\n  workflow_dispatch:\n    'inputs': {}\n", 3,
             "workflow_dispatch inputs use an inline empty form; nothing to type-check"),
            ('on:\n  "workflow_dispatch": {}\n', 2,
             "inline workflow_dispatch form; inputs not parsed"),
            ("on:\n  'workflow_dispatch': {}\n", 2,
             "inline workflow_dispatch form; inputs not parsed"),
        )
        for text, line, reason in cases:
            with self.subTest(text=text):
                self.put(WORKFLOW_PATH, text)
                self.base = self.commit()
                report = self.check(0, head=self.base)
                self.assert_counts(report, 0, 0, 0, 0)
                expected = [{"path": WORKFLOW_PATH, "line": line,
                             "category": "skipped", "reason": reason}]
                for side in ("base", "head"):
                    self.assertEqual(report["coverage"][side]["skipped"], expected)
                    self.assertEqual(report["coverage"][side]["unknown"], [])

    def test_quoted_nonempty_inline_inputs_are_not_certified(self):
        self.put(WORKFLOW_PATH,
                 'on:\n  workflow_dispatch:\n    "inputs": {target: {description: x}}\n')
        self.assertEqual(self.check(2)["code"], "unsupported_coverage")


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
    def test_unsupported_shapes_never_become_clean(self):
        for text in ("on: *events\n",
                     '"o\\u006e":\n  workflow_dispatch: {}\n',
                     "on:\n  workflow_dispatch:\n    inputs:\n"
                     "      target:\n        type: |\n          choice\n"):
            with self.subTest(text=text):
                self.put(WORKFLOW_PATH, text)
                self.assertEqual(self.check(2)["code"], "unsupported_coverage")


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


if __name__ == "__main__":
    unittest.main()
