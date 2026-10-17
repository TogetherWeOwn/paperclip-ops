#!/usr/bin/env python3
"""Public CI regression checks; no network, host inventory or credentials."""
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

import public_safety
import yaml

ROOT = Path(__file__).resolve().parents[2]
# Reviewed immutable callee: its sole job uses fromJSON(inputs.runs-on-json).
# New reusable workflows or pins require inspection, not blind trust in an input.
HOSTED_TEST_GUARD = (
    "TogetherWeOwn/.github/.github/workflows/test-guard.yml@"
    "866275517a9875f02110f946411401db8907ec8a"
)
# Reviewed immutable shadow review engine (TogetherWeOwn/.github#26, merged):
# warn-only changed-file signals; on this public repo its runner input
# defaults to ubuntu-latest when runs-on-json is omitted.
REVIEW_RULES_GUARD = (
    "TogetherWeOwn/.github/.github/workflows/review-rules.yml@"
    "63699075b492914c137281acffd4c89e51cc1c4c"
)
# Pack names accepted by the pinned review-rules reusable at REVIEW_RULES_GUARD.
REVIEW_RULES_PACKS = frozenset({"rust", "ts", "python-shell-ci", "php", "security"})
# Input keys accepted by the pinned review-rules reusable at REVIEW_RULES_GUARD.
REVIEW_RULES_INPUTS = frozenset({"packs", "mode", "runs-on-json"})


class PublicCI(unittest.TestCase):
    def assert_public_hosted_routes(self, directory):
        workflows = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
        self.assertTrue(workflows, "no workflows inspected")
        for workflow in workflows:
            data = yaml.safe_load(workflow.read_text())
            self.assertIsInstance(data, dict, str(workflow))
            jobs = data.get("jobs")
            self.assertIsInstance(jobs, dict, str(workflow))
            self.assertTrue(jobs, str(workflow))
            for name, job in jobs.items():
                label = f"{workflow.name}: {name}"
                self.assertIsInstance(job, dict, label)
                if "uses" in job:
                    self.assertIn(job["uses"],
                                  (HOSTED_TEST_GUARD, REVIEW_RULES_GUARD), label)
                    self.assertNotIn("runs-on", job, label)
                    # Fail closed on job-level keys that can widen authority
                    # or execution (secrets inherit, env, if, strategy,
                    # concurrency, ...). `name` is display-only and kept for
                    # the test-guard caller's byte-identical id/name contract.
                    self.assertFalse(
                        set(job) - {"uses", "with", "permissions", "name"}, label)
                    inputs = job.get("with", {})
                    self.assertIsInstance(inputs, dict, label)
                    if job["uses"] == REVIEW_RULES_GUARD:
                        self.assertEqual(job.get("permissions"),
                                         {"contents": "read"}, label)
                        self.assert_review_rules_inputs(inputs, label)
                    else:
                        # Test-guard callers carry no job permissions today
                        # (workflow-level only); pin that to absent-or-read so
                        # a later `permissions: write-all` edit fails closed.
                        # The callee caps its own permissions, so impact is low,
                        # but the guard should prove the bound it claims.
                        perms = job.get("permissions")
                        self.assertTrue(
                            perms is None or perms == {"contents": "read"},
                            label)
                        try:
                            route = json.loads(inputs.get("runs-on-json", "null"))
                        except (TypeError, ValueError):
                            self.fail(f"{label}: runner input must be literal JSON")
                        self.assertEqual(route, ["ubuntu-latest"], label)
                else:
                    # Fail closed on expressions, matrices, groups, aliases to
                    # private labels, and missing routes. This public snapshot
                    # needs no private branch in its routing expression.
                    self.assertEqual(job.get("runs-on"), "ubuntu-latest", label)

    def assert_review_rules_inputs(self, inputs, label):
        # Fail closed on unknown inputs, expressions, enforcement mode,
        # unknown packs, and non-hosted runner overrides. The pinned
        # reusable only accepts packs/mode/runs-on-json; anything else
        # needs inspection, not a silent pass.
        self.assertFalse(set(inputs) - REVIEW_RULES_INPUTS, label)
        self.assertEqual(inputs.get("mode"), "warn", label)
        packs = inputs.get("packs", "")
        self.assertIsInstance(packs, str, label)
        self.assertTrue(packs and "${{" not in packs, label)
        selected = {pack.strip() for pack in packs.split(",")}
        self.assertTrue(selected and all(selected), label)
        self.assertFalse(selected - REVIEW_RULES_PACKS, label)
        for key, value in inputs.items():
            self.assertNotIn("${{", str(value), label)
        runner = inputs.get("runs-on-json", "")
        if runner:
            try:
                route = json.loads(runner)
            except (TypeError, ValueError):
                self.fail(f"{label}: runner input must be literal JSON")
            self.assertEqual(route, ["ubuntu-latest"], label)

    def test_all_runner_routes_are_standard_hosted(self):
        self.assert_public_hosted_routes(ROOT / ".github/workflows")

    def test_public_routing_guard_accepts_hosted_and_reviewed_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "direct.yaml").write_text(
                "on: pull_request\njobs:\n  direct:\n    runs-on: ubuntu-latest\n")
            (directory / "reusable.yml").write_text(
                "on: pull_request\njobs:\n  guard:\n    uses: " + HOSTED_TEST_GUARD
                + "\n    with:\n      runs-on-json: '[\"ubuntu-latest\"]'\n")
            (directory / "review.yml").write_text(
                "on: pull_request\njobs:\n  review-rules:\n    name: review-rules\n    permissions:\n      contents: read\n    uses: " + REVIEW_RULES_GUARD
                + "\n    with:\n      packs: python-shell-ci,security,ts\n      mode: warn\n")
            self.assert_public_hosted_routes(directory)

    def test_public_routing_guard_rejects_private_dynamic_and_unreviewed_jobs(self):
        bad_jobs = {
            "private-array": "    runs-on: [self-hosted, two-ephemeral]\n",
            "private-multiline": "    runs-on:\n      - self-hosted\n      - two-isolated\n",
            "runner-group": "    runs-on:\n      group: private-runners\n      labels: ubuntu-latest\n",
            "expression": "    runs-on: ${{ github.event.repository.private && 'ubuntu-latest' || 'self-hosted' }}\n",
            "matrix": "    strategy:\n      matrix:\n        runner: [ubuntu-latest, self-hosted]\n    runs-on: ${{ matrix.runner }}\n",
            "missing-route": "    steps: []\n",
            "unknown-reusable": "    uses: example/workflows/.github/workflows/check.yml@" + "a" * 40
                + "\n    with:\n      runs-on-json: '[\"ubuntu-latest\"]'\n",
            "reusable-private-default": "    uses: " + HOSTED_TEST_GUARD + "\n",
            "reusable-private-input": "    uses: " + HOSTED_TEST_GUARD
                + "\n    with:\n      runs-on-json: '[\"self-hosted\", \"two-ephemeral\"]'\n",
            "reusable-expression": "    uses: " + HOSTED_TEST_GUARD
                + "\n    with:\n      runs-on-json: ${{ inputs.runner }}\n",
            "review-rules-enforce": "    uses: " + REVIEW_RULES_GUARD
                + "\n    with:\n      packs: ts\n      mode: enforce\n",
            "review-rules-moving-ref": "    uses: TogetherWeOwn/.github/.github/workflows/review-rules.yml@v1"
                + "\n    with:\n      packs: ts\n      mode: warn\n",
            "review-rules-unknown-pack": "    uses: " + REVIEW_RULES_GUARD
                + "\n    with:\n      packs: ts,unreviewed-pack\n      mode: warn\n",
            "review-rules-expression-packs": "    uses: " + REVIEW_RULES_GUARD
                + "\n    with:\n      packs: ${{ inputs.packs }}\n      mode: warn\n",
            "review-rules-private-runner": "    uses: " + REVIEW_RULES_GUARD
                + "\n    with:\n      packs: ts\n      mode: warn\n      runs-on-json: '[\"self-hosted\"]'\n",
            "review-rules-unknown-input": "    uses: " + REVIEW_RULES_GUARD
                + "\n    with:\n      packs: ts\n      mode: warn\n      unreviewed-input: 'yes'\n",
            "review-rules-secrets-inherit": "    uses: " + REVIEW_RULES_GUARD
                + "\n    permissions:\n      contents: read\n    secrets: inherit\n    with:\n      packs: ts\n      mode: warn\n",
            "review-rules-env": "    uses: " + REVIEW_RULES_GUARD
                + "\n    permissions:\n      contents: read\n    env:\n      UNREVIEWED: 'yes'\n    with:\n      packs: ts\n      mode: warn\n",
            "review-rules-if": "    uses: " + REVIEW_RULES_GUARD
                + "\n    permissions:\n      contents: read\n    if: github.event_name == 'pull_request'\n    with:\n      packs: ts\n      mode: warn\n",
            "review-rules-strategy": "    uses: " + REVIEW_RULES_GUARD
                + "\n    permissions:\n      contents: read\n    strategy:\n      matrix:\n        pack: [ts]\n    with:\n      packs: ts\n      mode: warn\n",
            "review-rules-permissions-write": "    uses: " + REVIEW_RULES_GUARD
                + "\n    permissions: write-all\n    with:\n      packs: ts\n      mode: warn\n",
            "review-rules-permissions-contents-write": "    uses: " + REVIEW_RULES_GUARD
                + "\n    permissions:\n      contents: write\n    with:\n      packs: ts\n      mode: warn\n",
            "review-rules-missing-permissions": "    uses: " + REVIEW_RULES_GUARD
                + "\n    with:\n      packs: ts\n      mode: warn\n",
            "test-guard-secrets-inherit": "    uses: " + HOSTED_TEST_GUARD
                + "\n    secrets: inherit\n    with:\n      runs-on-json: '[\"ubuntu-latest\"]'\n",
            "test-guard-if": "    uses: " + HOSTED_TEST_GUARD
                + "\n    if: github.event_name == 'pull_request'\n    with:\n      runs-on-json: '[\"ubuntu-latest\"]'\n",
            "test-guard-permissions-write": "    uses: " + HOSTED_TEST_GUARD
                + "\n    permissions: write-all\n    with:\n      runs-on-json: '[\"ubuntu-latest\"]'\n",
            "test-guard-permissions-contents-write": "    uses: " + HOSTED_TEST_GUARD
                + "\n    permissions:\n      contents: write\n    with:\n      runs-on-json: '[\"ubuntu-latest\"]'\n",
        }
        for name, job in bad_jobs.items():
            for suffix in (".yml", ".yaml"):
                with self.subTest(case=name, suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    # A green route must not hide a later unsafe job or file.
                    (directory / "first.yml").write_text(
                        "jobs:\n  green:\n    runs-on: ubuntu-latest\n")
                    (directory / ("second" + suffix)).write_text(
                        "jobs:\n  green:\n    runs-on: ubuntu-latest\n  unsafe:\n" + job)
                    with self.assertRaises(AssertionError):
                        self.assert_public_hosted_routes(directory)

    def test_drift_gate_runs_public_semantics_without_private_provenance(self):
        import hashlib

        for mode in ("public", "no-marker", "no-scanner", "malformed-pin",
                     "present-blob", "present-wrong-digest"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "verification").mkdir()
                for name in ("case_c89b5362-drift-pin-mutation-gate.sh",
                             "case_5dfa9150-handoff-copy-drift.sh"):
                    (root / "verification" / name).write_bytes(
                        (ROOT / "verification" / name).read_bytes())
                (root / "README.md").write_text(
                    "not-public\n" if mode == "no-marker" else
                    "## Fresh-history publication candidate\n")
                if mode != "no-scanner":
                    (root / ".github/scripts").mkdir(parents=True)
                    (root / ".github/scripts/public_safety.py").touch()
                subprocess.run(["git", "init", "-q", str(root)], check=True,
                               capture_output=True)
                checker = root / "verification/case_5dfa9150-handoff-copy-drift.sh"
                source = checker.read_text()
                if mode.startswith("present-"):
                    payload = b"# independent synthetic reviewed bytes\n"
                    blob = subprocess.run(
                        ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
                        input=payload, check=True, capture_output=True).stdout.decode().strip()
                    digest = hashlib.sha256(payload).hexdigest()
                    if mode == "present-wrong-digest":
                        digest = "0" * 64
                    source = re.sub(r'^PINNED_BLOB="[0-9a-f]{40}"$',
                                    'PINNED_BLOB="' + blob + '"', source, flags=re.M)
                    source = re.sub(r'^PINNED_SHA256="[0-9a-f]{64}"$',
                                    'PINNED_SHA256="' + digest + '"', source, flags=re.M)
                elif mode == "malformed-pin":
                    source = re.sub(r'^PINNED_BLOB=.*$', 'PINNED_BLOB="invalid"',
                                    source, flags=re.M)
                checker.write_text(source)
                result = subprocess.run(
                    ["bash", str(root / "verification/case_c89b5362-drift-pin-mutation-gate.sh")],
                    capture_output=True, text=True, timeout=60)
                output = result.stdout + result.stderr
                self.assertEqual(checker.read_text(), source, "production pin was modified")
                if mode in ("public", "present-blob"):
                    self.assertEqual(result.returncode, 0, output)
                    self.assertIn("GATE GREEN: 8 scenarios behaved and 5 mutants were killed.", output)
                    self.assertEqual("SKIP:" in output, mode == "public", output)
                else:
                    self.assertNotEqual(result.returncode, 0, output)
                    self.assertNotIn("SKIP:", output)
                    if mode != "present-wrong-digest":
                        self.assertEqual(result.returncode, 2, output)

    def test_endpoint_acceptance_suite_is_executed_by_ci(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["suites"]
        step = next(s for s in job["steps"] if s.get("name") == "gatus module wiring gate")
        self.assertEqual(step["run"],
                         "python3 -m unittest -v test_module_wiring.py test_pr2_acceptance.py")
        self.assertNotIn("if", step)
        self.assertFalse(step.get("continue-on-error", False))
        self.assertFalse(job.get("continue-on-error", False))

    def test_required_producers_handle_merge_groups(self):
        for name in ("ci.yml", "pr-lint.yml", "public-safety.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertRegex(text, r"(?m)^  merge_group:", name)

    def test_public_safety_push_ignores_merge_queue_branches(self):
        # A push run and a merge_group run for the same queue commit share
        # the workflow's concurrency group; cancel-in-progress cancels one
        # and the cancelled required check ejects the queue entry.
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/public-safety.yml").read_text())
        push = workflow[True]["push"]  # YAML 1.1 parses bare `on:` as True
        self.assertEqual(push["branches-ignore"], ["gh-readonly-queue/**"])

    def test_lint_attests_hosted_environment_not_runner_name(self):
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        self.assertIn('"${RUNNER_ENVIRONMENT:-}" == github-hosted', text)
        self.assertNotIn('case "${RUNNER_NAME:-}"', text)

    def test_lint_checkout_includes_public_regression_inputs(self):
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        # The suite reads tracked ZIP fixtures and the adapted mutation gate.
        # A .github/vendor-only sparse checkout silently omits both.
        self.assertNotIn("sparse-checkout:", text)
        self.assertIn("test_public_ci.py", text)
        # Renderer and filing reference checker are immutable public archive inputs,
        # not tracked files. A fresh checkout needs the same checksum-verified restore.
        self.assertLess(text.index("run: bash scripts/sync-toolkit.sh"),
                        text.index("- name: Unit-test the lint"))

    def test_org_request_suite_retains_nul_control_without_binary_source(self):
        relative = "mcp/test/org-request-mcp.test.mjs"
        source = (ROOT / relative).read_text()
        self.assertIn('a forged requester argument never becomes the recorded requester', source)
        self.assertIn('from "../org-request-mcp.mjs"', source)
        self.assertIn(r'alternatives: ["safe\0er"]', source)
        self.assertNotIn(chr(0), source)
        self.assertEqual(public_safety.inspect(ROOT / relative), [])

    def test_explicit_tool_grant_map_absence_is_not_skipped(self):
        for relative in ("tool_grant_expectations.json", "absent-explicit-map.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("test_case_6c24eff5_check12_reconcile.sh",
                             "public_snapshot_test_support.py"):
                    (root / name).write_bytes((ROOT / name).read_bytes())
                (root / "README.md").write_text("## Fresh-history publication candidate\n")
                (root / ".github/scripts").mkdir(parents=True)
                (root / ".github/scripts/public_safety.py").touch()
                (root / "org_access_review.sh").touch()
                result = subprocess.run(
                    ["bash", str(root / "test_case_6c24eff5_check12_reconcile.sh")],
                    env={"PATH": os.environ["PATH"], "TOOL_GRANT_MAP": str(root / relative)},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("no readable tool_grant_expectations.json", result.stderr)
                self.assertNotIn("SKIP:", result.stdout)

    def test_contract_dependency_is_restored_before_contract_tests(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        suites = text.split("\n  suites:\n", 1)[1].split("\n  private-mutation-gates:\n", 1)[0]
        restore = suites.index("      - name: Restore toolkit-vendored files\n")
        contract = suites.index("      - name: change-gating contract holds\n")
        self.assertLess(restore, contract)
        step = suites[restore:].split("      - ", 2)[1]
        self.assertIn("bash scripts/sync-toolkit.sh", step)
        self.assertNotRegex(step, r"(?m)^\s*if:")

    def test_obsolete_split_guards_apply_only_to_private_source(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        for name in ("Fail if the moved plugin path is touched",
                     "Fail if the freeze list drifted from its generator",
                     "Fail if a toolkit-owned path is touched"):
            step = text.split("      - name: " + name + "\n", 1)[1].split("      - ", 1)[0]
            self.assertIn("        if: github.event.repository.private\n", step)
        # Public publication still runs both secret and privacy scanners.
        scan = text.split("      - name: Scan tracked files for committed secrets\n", 1)[1].split("      - ", 1)[0]
        self.assertIn("./verification/secret-scan.sh", scan)
        self.assertNotRegex(scan, r"(?m)^\s*if:")
        safety = (ROOT / ".github/workflows/public-safety.yml").read_text()
        self.assertIn("gitleaks", safety)
        self.assertIn("public_safety.py", safety)
        # The history scan fails closed: no --all sweep of unrelated
        # branches, no narrowed single-commit fallback (on a pull-request
        # merge commit -p -U0 prints no patch, passing vacuously).
        # Pushes validate BEFORE and every range validates via rev-list.
        self.assertNotIn('--log-opts="--all"', safety)
        self.assertNotIn('RANGE="-1 HEAD"', safety)
        self.assertIn("git merge-base origin/main HEAD", safety)
        self.assertIn("github.event.before", safety)
        self.assertIn('git cat-file -e "$BEFORE^{commit}"', safety)
        self.assertIn('git rev-list --count "$RANGE"', safety)
        self.assertIn("pull-request history range", safety)

    def test_history_range_survives_advanced_main(self):
        # Pins the range logic against a scratch repo shaped like the review
        # probe: a PR that adds a credential-shaped file in one commit and
        # removes it in the next, with main advanced afterwards. The old
        # origin/main ancestor check then fails (fallback territory) while
        # the merge-base range still contains both PR commits and the old
        # single-commit scan of the synthetic merge commit sees nothing.
        try:
            subprocess.run(["git", "--version"], capture_output=True,
                           check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ,
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            def git(*args):
                subprocess.run(["git", *args], cwd=tmp, env=env,
                               capture_output=True, text=True, check=True)
            git("init", "-q")
            git("checkout", "-q", "-B", "main")
            Path(tmp, "base.txt").write_text("base\n")
            git("add", "base.txt")
            git("commit", "-qm", "base")
            git("checkout", "-qb", "pr")
            Path(tmp, "secret.txt").write_text("token = hunter2-hunter2\n")
            git("add", "secret.txt")
            git("commit", "-qm", "add credential-shaped file")
            git("rm", "-q", "secret.txt")
            git("commit", "-qm", "remove credential-shaped file")
            git("checkout", "-q", "main")
            Path(tmp, "other.txt").write_text("advance\n")
            git("add", "other.txt")
            git("commit", "-qm", "advance main")
            # Synthetic pull-request merge ref on a side branch, then main
            # advances again so origin/main is no longer an ancestor of the
            # merge commit.
            git("checkout", "-qb", "tmp-merge")
            git("merge", "--no-ff", "--no-edit", "-q", "pr", "-m", "merge pr")
            git("branch", "-f", "merge-ref", "HEAD")
            git("checkout", "-q", "main")
            Path(tmp, "later.txt").write_text("later\n")
            git("add", "later.txt")
            git("commit", "-qm", "main moves after merge-ref creation")
            def out(*args):
                r = subprocess.run(["git", *args], cwd=tmp, env=env,
                                   capture_output=True, text=True,
                                   check=True)
                return r.stdout
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", "main", "merge-ref"],
                cwd=tmp, env=env, capture_output=True)
            self.assertNotEqual(
                ancestor.returncode, 0,
                "scratch repo must reproduce the stale-base trigger")
            base = out("merge-base", "main", "merge-ref").strip()
            ranged = out("log", "-p", "-U0", base + "..merge-ref")
            self.assertIn("secret.txt", ranged,
                          "merge-base range lost the PR history")
            single = out("log", "-p", "-U0", "-1", "merge-ref")
            self.assertNotIn("secret.txt", single,
                             "single-commit scan unexpectedly saw a patch")

    def _history_scan_shell(self):
        # Slice the real `run:` block out of public-safety.yml so this test
        # executes the workflow's shell, not a reimplementation. Stub only
        # the docker invocations; every git conditional stays verbatim.
        data = yaml.safe_load(
            (ROOT / ".github/workflows/public-safety.yml").read_text())
        steps = data["jobs"]["public-safety"]["steps"]
        run = next(
            s["run"] for s in steps
            if (s.get("name") or "").startswith("Scan working tree"))
        script = run.replace(
            "${{ github.event_name }}", "${EVENT_NAME}").replace(
            "${{ github.event.before }}", "${BEFORE}")
        docker_stub = (
            'docker() { python3 -c "import json, sys; '
            'print(json.dumps(sys.argv[1:]))" "$@" >> "$DOCKER_LOG"; }\n')
        return docker_stub + script + "\n"

    def _run_history_scan(self, tmp, base_env, event_name, before):
        script = self._history_scan_shell()
        log_path = Path(tmp, "docker-invocations.jsonl")
        log_path.unlink(missing_ok=True)
        env = dict(base_env, EVENT_NAME=event_name,
                   BEFORE=before or "", SCANNER="stub",
                   DOCKER_LOG=str(log_path))
        return subprocess.run(
            ["bash", "-e", "-c", script], cwd=tmp, env=env,
            capture_output=True, text=True)

    def _assert_scanned_range(self, tmp, expected):
        invocations = [json.loads(line) for line in
                       Path(tmp, "docker-invocations.jsonl").read_text().splitlines()]
        git_scans = [args for args in invocations if "git" in args]
        self.assertEqual(len(git_scans), 1, invocations)
        scan_args = git_scans[0]
        log_opts = next((arg for arg in scan_args if arg.startswith("--log-opts=")), None)
        self.assertEqual(log_opts, "--log-opts=" + expected, scan_args)
        self._assert_tree_scanned(tmp)
        return expected

    def _assert_tree_scanned(self, tmp):
        # The exit-0 paths are justified by "the working-tree scan covers
        # this event": pin that the dir scan ran before the early exit, so
        # deleting or reordering it fails this test.
        invocations = [json.loads(line) for line in
                       Path(tmp, "docker-invocations.jsonl").read_text().splitlines()]
        tree = [args for args in invocations
                if "dir" in args and "/repo" in args]
        self.assertEqual(len(tree), 1, invocations)

    def _assert_no_history_scan(self, tmp):
        invocations = [json.loads(line) for line in
                       Path(tmp, "docker-invocations.jsonl").read_text().splitlines()]
        self.assertFalse(any("git" in args for args in invocations), invocations)
        self._assert_tree_scanned(tmp)

    def test_history_scan_script_uses_fail_closed_ranges(self):
        # Executes the workflow's actual range block in scratch repos for:
        # pull_request with and without an advanced main, push with a real
        # before, zero before (new branch), and unreachable before (force-push),
        # plus empty PR, dispatch, and zero-before push ranges. The docker
        # function records argv so the gitleaks --log-opts argument itself is
        # asserted, not merely the RANGE variable. Range and failure mutations
        # must break this test.
        try:
            subprocess.run(["git", "--version"], capture_output=True,
                           check=True)
            subprocess.run(["bash", "--version"], capture_output=True,
                           check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git or bash not available")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ,
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            def git(*args):
                subprocess.run(["git", *args], cwd=tmp, env=env,
                               capture_output=True, text=True, check=True)
            def out(*args):
                r = subprocess.run(["git", *args], cwd=tmp, env=env,
                                   capture_output=True, text=True,
                                   check=True)
                return r.stdout.strip()
            git("init", "-q")
            git("checkout", "-q", "-B", "main")
            Path(tmp, "base.txt").write_text("base\n")
            git("add", "base.txt")
            git("commit", "-qm", "base")
            base0 = out("rev-parse", "main")
            git("checkout", "-qb", "pr")
            Path(tmp, "secret.txt").write_text("token = hunter2-hunter2\n")
            git("add", "secret.txt")
            git("commit", "-qm", "add credential-shaped file")
            first_pr = out("rev-parse", "HEAD")
            git("rm", "-q", "secret.txt")
            git("commit", "-qm", "remove credential-shaped file")
            pr_tip = out("rev-parse", "HEAD")
            git("checkout", "-q", "main")
            Path(tmp, "other.txt").write_text("advance\n")
            git("add", "other.txt")
            git("commit", "-qm", "advance main")
            main1 = out("rev-parse", "main")
            git("checkout", "-qb", "tmp-merge")
            git("merge", "--no-ff", "--no-edit", "-q", "pr", "-m", "merge pr")
            git("branch", "-f", "merge-ref", "HEAD")
            git("checkout", "-q", "main")
            Path(tmp, "later.txt").write_text("later\n")
            git("add", "later.txt")
            git("commit", "-qm", "main moves after merge-ref creation")
            main2 = out("rev-parse", "main")
            zero = "0" * 40
            bogus = "d" * 40

            def set_origin(sha_or_none):
                if sha_or_none is None:
                    subprocess.run(
                        ["git", "update-ref", "-d",
                         "refs/remotes/origin/main"],
                        cwd=tmp, env=env, capture_output=True)
                else:
                    git("update-ref", "refs/remotes/origin/main",
                        sha_or_none)

            def range_log(rng):
                r = subprocess.run(
                    ["git", "log", "-p", "-U0", rng], cwd=tmp, env=env,
                    capture_output=True, text=True, check=True)
                return r.stdout

            # 1. pull_request without an advanced main.
            set_origin(main1)
            git("checkout", "-q", "merge-ref")
            r = self._run_history_scan(tmp, env, "pull_request", "")
            self.assertEqual(r.returncode, 0, r.stderr)
            rng = out("merge-base", "origin/main", "HEAD") + "..HEAD"
            self._assert_scanned_range(tmp, rng)
            self.assertIn("secret.txt", range_log(rng))

            # 2. pull_request with main advanced after the merge ref.
            set_origin(main2)
            git("checkout", "-q", "merge-ref")
            r = self._run_history_scan(tmp, env, "pull_request", "")
            self.assertEqual(r.returncode, 0, r.stderr)
            rng = out("merge-base", "origin/main", "HEAD") + "..HEAD"
            self._assert_scanned_range(tmp, rng)
            self.assertIn("secret.txt", range_log(rng),
                          "merge-base range lost PR history after advance")

            # 3. push with a real before (multi-commit push scans all).
            set_origin(main2)
            git("checkout", "-q", "pr")
            r = self._run_history_scan(tmp, env, "push", first_pr)
            self.assertEqual(r.returncode, 0, r.stderr)
            rng = first_pr + "..HEAD"
            self._assert_scanned_range(tmp, rng)

            # 4. push with a zero before (new branch) falls back to
            # merge-base instead of exiting 1.
            set_origin(base0)
            git("checkout", "-q", "pr")
            r = self._run_history_scan(tmp, env, "push", zero)
            self.assertEqual(r.returncode, 0, r.stderr)
            rng = out("merge-base", "origin/main", "HEAD") + "..HEAD"
            self._assert_scanned_range(tmp, rng)
            self.assertIn("secret.txt", range_log(rng))

            # 5. push with an unreachable before (force-push) has unknown
            # rewritten history: scan all reachable history instead of an
            # empty merge-base range.
            set_origin(base0)
            git("checkout", "-q", "pr")
            r = self._run_history_scan(tmp, env, "push", bogus)
            self.assertEqual(r.returncode, 0, r.stderr)
            self._assert_scanned_range(tmp, "HEAD")
            self.assertIn("secret.txt", range_log("HEAD"))

            # 6. Fail closed on an empty pull-request range.
            set_origin(main2)
            git("checkout", "-q", "main")
            r = self._run_history_scan(tmp, env, "pull_request", "")
            self.assertNotEqual(r.returncode, 0,
                                "empty pull-request history must fail closed")
            self.assertIn("pull-request history range", r.stderr)
            self._assert_no_history_scan(tmp)

            # 7. A workflow_dispatch at main scans the tree without invoking
            # gitleaks on an empty history range.
            r = self._run_history_scan(tmp, env, "workflow_dispatch", "")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no new commits", r.stdout)
            self._assert_no_history_scan(tmp)

            # 8. A zero-before push at the main tip also has no new commits.
            r = self._run_history_scan(tmp, env, "push", zero)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no new commits", r.stdout)
            self._assert_no_history_scan(tmp)

            # 9. A non-zero unreachable push at the main tip (force-push to
            # main) must not exit 0 with no history scan: the rewritten
            # commits are unknown, so the full reachable history is scanned.
            r = self._run_history_scan(tmp, env, "push", bogus)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("no new commits", r.stdout)
            self._assert_scanned_range(tmp, "HEAD")

    def test_metadata_protocol_constant_does_not_exempt_private_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "fixture.json"
            with mock.patch.object(public_safety, "ROOT", root):
                path.write_text('{"deny_target": "169.254.169.254"}')
                self.assertEqual(public_safety.inspect(path), [])
                for octets in ((10, 255, 255, 254), (192, 168, 255, 254),
                               (169, 254, 169, 253)):
                    path.write_text(".".join(map(str, octets)))
                    self.assertIn("non-example private network address", public_safety.inspect(path))

    def test_public_artifact_env_exception_is_exact_path_and_bytes(self):
        pin = (
            "# Pinned upstream artifact. Update version, URL, and SHA-256 together.\n"
            "RUNNER_VERSION=2.337.0\n"
            "RUNNER_ARCHIVE=actions-runner-linux-x64-2.337.0.tar.gz\n"
            "RUNNER_URL=https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz\n"
            "RUNNER_SHA256=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "github-runner/actions-runner.env"
            path.parent.mkdir()
            with mock.patch.object(public_safety, "ROOT", root):
                path.write_text(pin)
                self.assertEqual(public_safety.inspect(path), [])
                path.write_text(pin + "UNREVIEWED_VARIABLE=fixture\n")
                self.assertIn("private artifact or live environment file", public_safety.inspect(path))
                other = root / "other.env"
                other.write_text(pin)
                self.assertIn("private artifact or live environment file", public_safety.inspect(other))

    def test_reviewed_zip_exception_requires_exact_path_bytes_and_public_payload(self):
        for relative, (_, member, public_name) in public_safety.PUBLIC_ZIP_FIXTURES.items():
            source = ROOT / relative
            raw = source.read_bytes()
            payload = (source.parent / public_name).read_bytes()
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / relative
                path.parent.mkdir(parents=True)
                public = path.parent / public_name
                public.write_bytes(payload)
                path.write_bytes(raw)
                with mock.patch.object(public_safety, "ROOT", root):
                    self.assertEqual(public_safety.inspect(path), [])
                    other = root / "other.zip"
                    other.write_bytes(raw)
                    self.assertIn("unreviewed binary payload", public_safety.inspect(other))
                    path.write_bytes(raw + b"unreviewed trailer")
                    self.assertIn("unreviewed binary payload", public_safety.inspect(path))
                    path.write_bytes(raw)
                    public.write_bytes(payload + b" ")
                    self.assertIn("unreviewed binary payload", public_safety.inspect(path))
                    public.unlink()
                    self.assertIn("unreviewed binary payload", public_safety.inspect(path))

    def test_unreviewed_zip_with_matching_public_json_is_still_refused(self):
        import zipfile
        relative, (_, member, public_name) = next(iter(public_safety.PUBLIC_ZIP_FIXTURES.items()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / relative
            path.parent.mkdir(parents=True)
            (path.parent / public_name).write_text('{}')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr(member, '{}')
            with mock.patch.object(public_safety, "ROOT", root):
                self.assertIn("unreviewed binary payload", public_safety.inspect(path))

    def test_private_filing_integration_requires_absence_and_no_explicit_ref(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("filing_strip_tests", ROOT / "verification/test_filing_strip.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for case in ("public-absent", "nonpublic-absent", "partial-bundle", "explicit-ref"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "verification").mkdir()
                for name in (module.GATE, "verification/upstream-bundle-filing-gate.sh"):
                    (root / name).write_bytes((ROOT / name).read_bytes())
                (root / "README.md").write_text("private source\n" if case == "nonpublic-absent"
                                               else "## Fresh-history publication candidate\n")
                (root / ".github/scripts").mkdir(parents=True)
                (root / ".github/scripts/public_safety.py").touch()
                if case == "partial-bundle":
                    (root / "docs/upstream").mkdir(parents=True)
                environment = {"PATH": os.environ["PATH"]}
                if case == "explicit-ref":
                    environment["GATE_REF"] = "missing-synthetic-ref"
                result = unittest.TestResult()
                with mock.patch.object(module, "ROOT", root), mock.patch.dict(os.environ, environment, clear=True):
                    module.FilingStrip("test_private_report_integration").run(result)
                self.assertEqual(len(result.skipped), 1 if case == "public-absent" else 0)
                self.assertEqual(len(result.failures), 0 if case == "public-absent" else 1)
                self.assertEqual(result.errors, [])

    def test_posthoc_private_card_skip_requires_marker_and_exact_alias(self):
        script_name = "verification/case_092cd51d-public-fork-disclosure-audit.sh"
        original = (ROOT / script_name).read_text()
        dispatch = "\nif [ \"${1:-}\" = '--provenance-diagnostics' ]; then"
        self.assertIn(dispatch, original)
        # Only definitions and the offline POSTHOC self-test; never the audit's API path.
        script = original.split(dispatch, 1)[0] + "\nposthoc_test\n"
        for case in ("public-redacted", "nonpublic-redacted", "unknown-alias"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "verification").mkdir()
                text = script
                if case == "unknown-alias":
                    self.assertIn("|case_fcdcc723|", text)
                    text = text.replace("|case_fcdcc723|", "|unknown-alias|")
                (root / script_name).write_text(text)
                (root / "public_snapshot_test_support.py").write_bytes((ROOT / "public_snapshot_test_support.py").read_bytes())
                (root / "README.md").write_text("private source\n" if case == "nonpublic-redacted"
                                               else "## Fresh-history publication candidate\n")
                (root / ".github/scripts").mkdir(parents=True)
                (root / ".github/scripts/public_safety.py").touch()
                result = subprocess.run(["bash", str(root / script_name)], env={"PATH": os.environ["PATH"]},
                                        capture_output=True, text=True, timeout=20)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0 if case == "public-redacted" else 1, output)
                if case == "public-redacted":
                    self.assertIn("SKIP: 11 private historical card identities", output)
                    self.assertIn("111/111 POSTHOC checks correct", output)
                else:
                    self.assertNotIn("SKIP:", output)
                    self.assertIn("bad card:", output)

    def test_withdrawal_fixture_boundary_and_digest_fail_closed(self):
        for case in ("public-absent", "nonpublic-absent", "partial-classification",
                     "partial-document", "changed-fixture"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                gate = "verification/case_786dae75-runbook-withdrawal-render-gate.sh"
                for name in (gate, "operator_runbook.sh", "public_snapshot_test_support.py",
                             "tests/fixtures/operator-withdrawal-synthetic.json"):
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((ROOT / name).read_bytes())
                (root / "README.md").write_text("private source\n" if case == "nonpublic-absent"
                                               else "## Fresh-history publication candidate\n")
                (root / ".github/scripts").mkdir(parents=True)
                (root / ".github/scripts/public_safety.py").touch()
                if case == "partial-classification":
                    (root / "operator_runbook_classification.json").write_text("{}")
                if case == "partial-document":
                    (root / "docs").mkdir()
                    (root / "docs/OPERATOR-RUNBOOK.md").write_text("partial")
                if case == "changed-fixture":
                    path = root / "tests/fixtures/operator-withdrawal-synthetic.json"
                    original = path.read_text()
                    self.assertIn("synthetic withdrawn rendering", original)
                    path.write_text(original.replace("synthetic withdrawn rendering", "synthetic changed rendering"))
                result = subprocess.run(["bash", str(root / gate)], env={"PATH": os.environ["PATH"]},
                                        capture_output=True, text=True, timeout=20)
                output = result.stdout + result.stderr
                if case == "public-absent":
                    self.assertEqual(result.returncode, 0, output)
                    self.assertIn("All mutants caught", output)
                else:
                    self.assertNotEqual(result.returncode, 0, output)
                    self.assertNotIn("All mutants caught", output)
                if case in ("public-absent", "changed-fixture"):
                    self.assertIn("SKIP: private deployed classification/render binding", output)
                else:
                    self.assertNotIn("SKIP:", output)
                if case == "changed-fixture":
                    self.assertIn("synthetic expected render drifted", output)

    def test_private_tier_zero_skip_is_absence_only(self):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("staged_drift_tests", ROOT / "test_staged_drift.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", [str(ROOT)] + sys.path):
            spec.loader.exec_module(module)
        for case in ("public-absent", "nonpublic-absent", "partial-source",
                     "dangling-source", "existing-rules", "malformed-pin", "present-history"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = {"PATH": os.environ["PATH"], "HOME": tmp,
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
                subprocess.run(["git", "init", "--quiet", tmp], env=env, check=True)
                pin = "0" * 40
                if case == "present-history":
                    subprocess.run(["git", "-C", tmp, "-c", "user.name=Synthetic Fixture",
                                    "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
                                    "--quiet", "-m", "test: synthetic source\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>"],
                                   env=env, check=True)
                    pin = subprocess.check_output(["git", "-C", tmp, "rev-parse", "HEAD"], env=env, text=True).strip()
                files = {
                    "README.md": "private source\n" if case == "nonpublic-absent" else "## Fresh-history publication candidate\n",
                    ".github/scripts/public_safety.py": "# fixture marker\n",
                    str(module.SCRIPT): (ROOT / module.SCRIPT).read_text(),
                    str(module.STAGE / "SOURCE"): "SOURCE_SHA=" + ("invalid" if case == "malformed-pin" else pin) + "\n",
                }
                for name, text in files.items():
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                source = root / ".github/workflows/tier-0-quality-gate.yml"
                source.parent.mkdir(parents=True)
                if case == "partial-source":
                    source.write_text("{}")
                if case == "dangling-source":
                    source.symlink_to(root / "missing")
                if case == "existing-rules":
                    (root / "semgrep-rules").mkdir()
                result = unittest.TestResult()
                with mock.patch.object(module, "ROOT", root):
                    module.HistoricalSourcePort("test_retained_private_source_port").run(result)
                self.assertEqual(len(result.skipped), 1 if case == "public-absent" else 0)
                self.assertEqual(len(result.failures), 0 if case == "public-absent" else 1)
                self.assertEqual(result.errors, [])
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("run: python3 test_staged_drift.py", workflow)
        self.assertIn("python3 test_staged_drift.py || fail", (ROOT / "scripts/verify-fast.sh").read_text())

    def test_private_render_skip_keeps_carrier_and_userns_refusals(self):
        for case in ("public-absent", "bad-carrier", "partial-receipt", "nonpublic-absent"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("test_paperclip_immutable_runtime.sh", "public_snapshot_test_support.py",
                             "deploy/paperclip-immutable/paperclip.container",
                             "deploy/paperclip-immutable/agent-run.container.in"):
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((ROOT / name).read_bytes())
                (root / "README.md").write_text(
                    "private source\n" if case == "nonpublic-absent"
                    else "## Fresh-history publication candidate\n")
                (root / ".github/scripts").mkdir(parents=True)
                (root / ".github/scripts/public_safety.py").touch()
                binary = root / "bin"
                binary.mkdir()
                stub = binary / "unshare"
                stub.write_text("#!/bin/sh\nexit 1\n")
                stub.chmod(0o755)
                if case == "bad-carrier":
                    path = root / "deploy/paperclip-immutable/paperclip.container"
                    original = path.read_text()
                    self.assertIn("ReadOnly=true", original)
                    path.write_text(original.replace("ReadOnly=true", "ReadOnly=false"))
                if case == "partial-receipt":
                    path = root / "deploy/paperclip-immutable/generated/board-quadlet-render.json"
                    path.parent.mkdir(parents=True)
                    path.write_text("{}")
                result = subprocess.run(
                    ["bash", str(root / "test_paperclip_immutable_runtime.sh")],
                    env={"PATH": str(binary) + ":" + os.environ["PATH"]},
                    capture_output=True, text=True, timeout=10)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 3 if case == "public-absent" else 1, output)
                self.assertNotIn("PASS:", output)
                if case == "public-absent":
                    self.assertIn("SKIP: historical private-carrier", output)
                    self.assertIn("REFUSE: unprivileged user namespaces", output)
                else:
                    self.assertNotIn("SKIP:", output)
                    self.assertIn("FAIL:", output)

    def test_private_artifact_skip_is_named_marked_and_absence_only(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "public_snapshot_test_support", ROOT / "public_snapshot_test_support.py")
        support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(support)
        for relative in support.PRIVATE_ARTIFACTS:
            for scanner in (False, True):
                for marker in (False, True):
                    for artifact in (None, "empty", "partial", "directory", "dangling-symlink"):
                        with self.subTest(relative=relative, scanner=scanner, marker=marker,
                                          artifact=artifact), tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            (root / "README.md").write_text(
                                "## Fresh-history publication candidate\n" if marker else "private source\n")
                            if scanner:
                                (root / ".github/scripts").mkdir(parents=True)
                                (root / ".github/scripts/public_safety.py").touch()
                            path = root / relative
                            path.parent.mkdir(parents=True, exist_ok=True)
                            if artifact == "empty":
                                path.touch()
                            elif artifact == "partial":
                                path.write_text("invalid partial JSON")
                            elif artifact == "directory":
                                path.mkdir()
                            elif artifact == "dangling-symlink":
                                path.symlink_to(root / "absent-target")
                            self.assertEqual(support.private_artifact_absent(root, relative),
                                             scanner and marker and artifact is None)
        with self.assertRaises(ValueError):
            support.private_artifact_absent(ROOT, "unreviewed-fixture.json")
        with self.assertRaises(ValueError):
            support.private_artifact_absent(ROOT, "../tests/dead-feed-2026-08-26.jsonl")

    def test_auth_mutation_gate_rejects_unattributable_results(self):
        import importlib.util
        import json
        spec = importlib.util.spec_from_file_location(
            "auth_mutation_gate", ROOT / "verification/auth-denial-mutation-gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        baseline = dict(success=True, run=55, failures=[], errors=[], skipped=[],
                        loader_errors=[], expected_failures=0, unexpected_successes=0)
        cases = [
            (False, (), {}, True),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS)}, True),
            (False, (), {"skipped": sorted(gate.PRIVATE_TESTS)}, False),
            (True, ("named-control",), {"skipped": sorted(gate.PRIVATE_TESTS)}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS) + ["unexpected"]}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS)[:-1]}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS), "errors": ["startup-error"]}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS), "run": 5}, False),
            (False, ("named-control",), {"run": 0}, False),
            (False, ("named-control",), {"loader_errors": ["missing test"]}, False),
            (False, ("named-control",), {"expected_failures": 1}, False),
            (False, ("named-control",), {"unexpected_successes": 1}, False),
        ]
        for public_absence, names, changes, accepted in cases:
            result = {**baseline, **changes}
            proc = subprocess.CompletedProcess([], 0, json.dumps(result), "")
            with self.subTest(public_absence=public_absence, names=names, changes=changes), \
                    mock.patch.object(gate, "private_artifact_absent", return_value=public_absence), \
                    mock.patch.object(gate.subprocess, "run", return_value=proc):
                if accepted:
                    self.assertEqual(gate.run_suite("unused", names), result)
                else:
                    with self.assertRaises(RuntimeError):
                        gate.run_suite("unused", names)

    def test_mutation_stage_preserves_exact_private_artifact_boundary(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from public_snapshot_test_support import (
            private_artifact_absent, stage_private_artifact)
        relative = "tests/dead-feed-2026-08-26.jsonl"
        for public in (False, True):
            for state in ("absent", "empty", "partial", "directory", "dangling"):
                with self.subTest(public=public, state=state), tempfile.TemporaryDirectory() as tmp:
                    source, stage = Path(tmp) / "source", Path(tmp) / "stage"
                    source.mkdir()
                    (source / "public_snapshot_test_support.py").write_text(
                        (ROOT / "public_snapshot_test_support.py").read_text())
                    if public:
                        (source / "README.md").write_text("## Fresh-history publication candidate\n")
                        marker = source / ".github/scripts/public_safety.py"
                        marker.parent.mkdir(parents=True)
                        marker.write_text("# actual source marker\n")
                    artifact = source / relative
                    artifact.parent.mkdir(parents=True)
                    if state in ("empty", "partial"):
                        artifact.write_text("" if state == "empty" else "incomplete\n")
                    elif state == "directory":
                        artifact.mkdir()
                    elif state == "dangling":
                        artifact.symlink_to("absent-target")
                    if state in ("directory", "dangling") or (state == "absent" and not public):
                        with self.assertRaises(OSError):
                            stage_private_artifact(source, stage, relative)
                    else:
                        skipped = stage_private_artifact(source, stage, relative)
                        self.assertEqual(skipped, public and state == "absent")
                        self.assertEqual(private_artifact_absent(stage, relative), skipped)
                        if not skipped:
                            self.assertEqual((stage / relative).read_bytes(), artifact.read_bytes())
                        else:
                            self.assertEqual((stage / ".github/scripts/public_safety.py").read_bytes(),
                                             (source / ".github/scripts/public_safety.py").read_bytes())
                    with self.assertRaises(ValueError):
                        stage_private_artifact(source, stage, "../unreviewed")

    def test_private_bakeoff_skip_requires_both_markers_and_complete_absence(self):
        source = (ROOT / "test_case_61df0b37_review_bakeoff.sh").read_text()
        cases = [
            (True, True, None, 0, True),
            (True, True, None, 1, False),  # harness failures cannot be hidden
            (False, True, None, 0, False),
            (True, False, None, 0, False),
            (False, False, None, 0, False),
            (True, True, "dataset", 0, False),
            (True, True, "results", 0, False),
            (True, True, "RESULTS-r1.md", 0, True),  # report is not its private inputs
        ]
        for scanner, marker, artifact, harness_rc, success in cases:
            with self.subTest(scanner=scanner, marker=marker, artifact=artifact,
                              harness_rc=harness_rc), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "suite.sh").write_text(source)
                (root / "README.md").write_text(
                    "## Fresh-history publication candidate\n" if marker else "private source\n")
                if scanner:
                    (root / ".github/scripts").mkdir(parents=True)
                    (root / ".github/scripts/public_safety.py").touch()
                if artifact:
                    path = root / "eval/review-bakeoff" / artifact
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("partial bundle\n")
                # Only the expensive harness is stubbed here. The real shell's
                # marker/absence predicates and nonpublic failure path execute.
                (root / "bin").mkdir()
                stub = root / "bin/python3"
                stub.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = -m ] && [ \"$2\" = unittest ]; then\n"
                    "  printf 'harness-called\\n'\n"
                    f"  exit {harness_rc}\n"
                    "fi\nexit 77\n")
                stub.chmod(0o755)
                proc = subprocess.run(
                    ["bash", str(root / "suite.sh")], cwd=root, text=True,
                    capture_output=True, timeout=15,
                    env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.defpath})
                self.assertIn("harness-called", proc.stdout)
                self.assertEqual(proc.returncode == 0, success, proc.stdout + proc.stderr)
                skipped = scanner and marker and artifact in (None, "RESULTS-r1.md")
                self.assertEqual("SKIP: historical private bake-off" in proc.stdout, skipped)
                if not skipped:
                    self.assertIn("FAIL dataset shape", proc.stdout)
                    self.assertIn("FAIL no token-shaped string", proc.stdout)

    def test_renamed_archive_sources_use_public_paths(self):
        manifest = (ROOT / "vendor/toolkit-files.txt").read_text().splitlines()
        expected = {
            "verification/case_9e76f76d-recorded-command.sh": "verification/recorded-monitor-command-gate.sh",
            "verification/case_9e76f76d-recorded-command.txt": "verification/recorded-monitor-command.txt",
            "verification/case_46e26fa6-mutation-gate.sh": "verification/plugin-deploy-drift-detector-mutation-gate.sh",
            "verification/case_f909ca1f-mutation-gate.sh": "verification/grant-attribution-resolver-mutation-gate.sh",
        }
        for dest, source in expected.items():
            self.assertIn(f"{dest}|{source}", manifest)


if __name__ == "__main__":
    unittest.main()
