"""Fixture tests for scripts/quality-gates/no_pull_request_target.sh.

Each case builds a throwaway git repo holding a copy of the gate script and
one workflow fixture, then runs the gate in it. Mentions that are not trigger
positions (comments, step names, script paths) must pass; every live
pull_request_target trigger form must fail. No network.
Run: python3 -m unittest test_no_pull_request_target
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
GATE = REPO_ROOT / "scripts" / "quality-gates" / "no_pull_request_target.sh"

JOB = """\
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

PASS = {
    "header comment names the trigger": (
        "# pull_request_target is banned; see CONSTRAINTS.md\n"
        "on:\n  pull_request:\n" + JOB),
    "trailing comment names the trigger": (
        "on:\n  pull_request:\n" + JOB.replace(
            "echo hi", "echo hi  # no pull_request_target here")),
    "step name and script path contain the token": (
        "on:\n  pull_request:\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        '      - name: "Gate 5 (HARD): no pull_request_target in workflows"\n'
        "        run: bash scripts/quality-gates/no_pull_request_target.sh\n"),
    # Quoted "on" without the banned token must not fail (no false
    # positive on the quoted-key handling).
    "quoted on without banned token": (
        '"on": [push, pull_request]\n' + JOB),
    # The token in a step script is outside the trigger block: steps-only
    # mentions must keep passing after the block-boundary fix.
    "token in run script passes": (
        "on:\n  push:\n" + JOB.replace(
            "echo hi", "echo pull_request_target is banned")),
    # Quoted `*` globs are not YAML aliases: the alias check is
    # quote-aware, so these clean workflows must pass.
    "quoted glob in paths passes": (
        "on:\n  push:\n    paths: ['**/*-lock.json']\n" + JOB),
    "quoted glob sequence item passes": (
        "on:\n  push:\n    paths:\n      - 'src/*Test.java'\n" + JOB),
    "double-quoted glob passes": (
        'on:\n  push:\n    branches: ["*-release"]\n' + JOB),
    # An apostrophe in a plain scalar is literal text, not a quote: it must
    # neither blind the scan to a later alias nor hide a `#` comment that
    # names the trigger.
    "apostrophe in name with trigger comment in block": (
        "name: Don't deploy\non:\n  push:\n    branches: [main]  # never pull_request_target\n" + JOB),
}

FAIL = {
    "block mapping key": (
        "on:\n  pull_request_target:\n    types: [opened]\n" + JOB),
    "scalar trigger": "on: pull_request_target\n" + JOB,
    "flow list": "on: [push, pull_request_target]\n" + JOB,
    "trailing comment on key": (
        "on:\n  pull_request_target:  # risky\n" + JOB),
    "quoted key": 'on:\n  "pull_request_target":\n' + JOB,
    "block sequence item": (
        "on:\n  - push\n  - pull_request_target\n" + JOB),
    "multi-line flow list": (
        "on: [\n  push,\n  pull_request_target\n]\n" + JOB),
    # Reviewer bypass probes: quoted "on" and next-line flow
    # lists slipped the head revision; the audit silencer is built by
    # concatenation so this fixture file never carries a literal
    # suppression comment itself (floor guard bans new suppressions).
    "quoted on key with inline flow list": (
        '"on": [push, pull_request_target]\n' + JOB),
    "flow list on line after on": (
        "on:\n  [push, pull_request_target]\n" + JOB),
    "reviewer bypass: quoted on plus audit silencer": (
        '"on": [push, pull_request_target]'
        + (" # zizmor" + ": ignore[dangerous-triggers]")
        + "\n" + JOB),
    # Reviewer probes: a block sequence may sit at
    # the same indent as its key, so zero-indent `- ` items continue the
    # trigger block; and a uniformly indented file still has one top-level
    # mapping whose `on:` must be found off column 0.
    "zero-indent sequence": (
        "name: probe\non:\n- push\n- pull_request_target\n" + JOB),
    "minimal zero-indent sequence": "on:\n- pull_request_target\n",
    "indented root mapping": (
        "  name: probe\n  on:\n    pull_request_target:\n"
        "  jobs:\n    probe:\n      runs-on: ubuntu-latest\n"
        "      steps:\n        - run: echo probe\n"),
    "sequence at key indent in indented root": (
        "  on:\n  - push\n  - pull_request_target\n"
        "  jobs:\n    probe:\n      runs-on: ubuntu-latest\n"
        "      steps:\n        - run: echo probe\n"),
    # Paperclip Review round 1: YAML anchors/aliases are live triggers
    # (GitHub Actions expands them), and a top-level flow mapping may
    # continue at column 0. Both must fail (fail-closed).
    "anchor alias trigger": (
        "name: &trig pull_request_target\non: *trig\n" + JOB),
    "flow mapping continuation at column 0": (
        "on: {push: {},\npull_request_target: {}}\n" + JOB),
    # Paperclip Review round 2: the bracket-depth fail-closed is defeated
    # by a multi-line quoted scalar (quote state must persist across
    # lines), a root flow document has no `on:` line (fail closed), and a
    # YAML double-quoted escape hides the token (backslash fail-closed).
    "multi-line quoted scalar hides trigger": (
        'on: {push: {}, a: "foo\n } a #b", pull_request_target: {}}\n' + JOB),
    "root flow document trigger": (
        '{"on": {"pull_request_target": {}}, '
        '"jobs": {"j": {"runs-on": "ubuntu-latest"}}}\n'),
    "escaped token trigger": (
        'on:\n  - "pull_request\\x5ftarget"\n' + JOB),
    # Paperclip Review round 3: the alias scan missed the explicit `?` key
    # form, any alias after a plain-scalar apostrophe in the block, and
    # anchor names with punctuation; all three resolve via yaml v2.
    "explicit-key alias trigger": (
        "x: &t pull_request_target\non:\n  ? *t\n  : {}\n" + JOB),
    "apostrophe hides later alias": (
        "x: &t pull_request_target\non:\n  push:\n    branches: [main]\n"
        "  workflow_dispatch:\n    inputs:\n      a:\n"
        "        description: it's\n  *t : {}\n" + JOB),
    "punctuation anchor alias trigger": (
        "x: &~t pull_request_target\non: *~t\n" + JOB),
    "plus anchor alias trigger": (
        "x: &+t pull_request_target\non: *+t\n" + JOB),
    # Paperclip Review round 3 (late-novel): an escaped or aliased top-level
    # `on:` never matches the extractor, so any other `on:`-looking line
    # (nested mapping key or a line inside a `run: |` script) would win as a
    # clean decoy. Both must fail closed.
    "escaped on key with decoy": (
        '"\\x6fn": pull_request_target\njobs:\n  j:\n'
        "    runs-on: ubuntu-latest\n    env:\n      on: x\n"
        "    steps:\n      - run: echo hi\n"),
    "aliased on key with decoy": (
        "k: &k on\n*k : pull_request_target\njobs:\n  j:\n"
        "    runs-on: ubuntu-latest\n    env:\n      on: x\n"
        "    steps:\n      - run: echo hi\n"),
    "escaped on key with run-script decoy": (
        '"\\x6fn": pull_request_target\njobs:\n  j:\n'
        "    runs-on: ubuntu-latest\n    steps:\n      - run: |\n"
        "        on: x\n"),
    "explicit top-level on key with decoy": (
        "k: &k on\n? *k\n: pull_request_target\njobs:\n  j:\n"
        "    runs-on: ubuntu-latest\n    env:\n      on: x\n"
        "    steps:\n      - run: echo hi\n"),
}


def run_gate(workflow_text):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gate = root / "scripts" / "quality-gates" / GATE.name
        gate.parent.mkdir(parents=True)
        shutil.copy(GATE, gate)
        workflows = root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        if workflow_text is not None:
            (workflows / "ci.yml").write_text(workflow_text)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        done = subprocess.run(["bash", str(gate)], cwd=root,
                              capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr


class TestNoPullRequestTarget(unittest.TestCase):
    def test_non_trigger_mentions_pass(self):
        for label, text in PASS.items():
            with self.subTest(label):
                code, out = run_gate(text)
                self.assertEqual(code, 0, out)

    def test_live_trigger_forms_fail(self):
        for label, text in FAIL.items():
            with self.subTest(label):
                code, out = run_gate(text)
                self.assertEqual(code, 1, out)

    def test_no_workflows_passes(self):
        code, out = run_gate(None)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
