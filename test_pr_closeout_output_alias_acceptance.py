#!/usr/bin/env python3
"""Immutable offline acceptance: refuse output aliases before either artifact is overwritten.

Run: python3 -B -m unittest -v test_pr_closeout_ledger test_pr_closeout_output_alias_acceptance

These tests intentionally precede the implementation. They invoke the actual
``pr_closeout_ledger.py`` CLI boundary via subprocess with synthetic offline
fixtures only (tempfiles under a per-test mkdtemp; no network, credentials,
board/host state or production files). Green here must mean the GUARD works.

WHAT THIS SUITE PINS (baseline RED: the tool sequentially opens ledger-out
then plan-out with mode "w" and no alias validation, so every collision
below currently succeeds and overwrites):

  * Output-output collisions refuse BEFORE EITHER artifact is opened:
    identical strings, ../ normalization, absolute/relative spellings,
    existing symlink aliases, symlinked parent paths and hard links
    (filesystem identity where available). The complete path pair is
    validated before any truncation, directory creation or replacement.
  * Input-evidence protection: either output aliasing the registry,
    snapshot or optional cards input refuses; protected inputs stay
    byte-identical.
  * Intentional ``--prior-ledger`` in-place replacement keeps working
    when the prior path is NOT also a protected input; a prior path
    that IS a protected input still refuses (evidence wins).
  * Machine contract: nonzero existing write/usage exit (2), a stable
    ``output_path_conflict`` code for determined aliases and exact
    ``output_path_unverifiable`` when safe determination fails,
    ``retryable`` false, and a FIXED ``next_action`` naming distinct
    paths. ``--json`` refusals are finite, parseable JSON envelopes;
    text refusals carry the code plus the fixed remediation. Neither
    stream may disclose supplied paths, the fixture directory, evidence
    bytes or arbitrary OSError text, traceback, or success prose.
    Refusals are idempotent. Symlink loops exercise determination failure
    even when the runtime bypasses directory permission bits.
  * Unreadable registry/snapshot/optional cards evidence retains normal
    mode's existing exit 5 with distinct writable outputs, including
    when --json is supplied (no invented evidence-error JSON envelope).
  * Positive controls (green on baseline AND after the build):
    ordinary distinct outputs succeed with unchanged proposal/exit
    semantics, and prior-ledger in-place replacement succeeds.
  * The CLI help states the distinct-outputs requirement.

NON-GOALS (builder/reviewer owned, not asserted here): the long-form doc
page wording, atomic multi-file persistence, hostile-concurrency/TOCTOU
guarantees, and any independently added preview mode (which must stay
no-write without depending on this guard).
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "pr_closeout_ledger.py")

HEAD_A = "a" * 40
REQUIRED_CHECKS = ["check", "gitleaks", "pr-lint"]
EXIT_REFUSED = 2  # existing write/usage class (cannot-write-output today)
CODE_CONFLICT = "output_path_conflict"
CODE_UNVERIFIABLE = "output_path_unverifiable"
NEXT_ACTION = (
    "Choose distinct paths for --ledger-out and --plan-out that differ "
    "from each other and from --registry, --snapshot and --cards.")


def registry_doc(marker):
    return {"entries": [{"repo": "two-web-next", "pr": 291,
                         "admission": "admitted",
                         "acceptanceMarker": marker}]}


def snapshot_doc(marker):
    key = "two-web-next#291"
    return {
        "snapshots": {key: {
            "repo": "two-web-next", "pr": 291, "headSha": HEAD_A,
            "mergeableState": "clean", "merged": False, "draft": False,
            "releasePlease": False,
            "checks": [{"name": name, "sha": HEAD_A, "conclusion": "success"}
                       for name in REQUIRED_CHECKS],
            "requiredChecks": list(REQUIRED_CHECKS),
            "companyIndependentReviewRequired": True,
            "companySecurityReviewRequired": False,
            "acceptanceMarker": marker,
        }},
        "reviews": {key: {"verdict": "APPROVE", "sha": HEAD_A}},
        "cards": {},
    }


def cards_doc(marker):
    return {"cards": {"case_3a0e31bb": {
        "id": "00000000-0000-4000-8000-000000000179",
        "identifier": "case_3a0e31bb", "status": "in_progress",
        "acceptanceMarker": marker}}}


def prior_doc():
    return {"records": {}}


class OutputAliasBase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="output-alias-acceptance-")
        self.marker = "acceptance-marker-%s" % os.path.basename(self.work)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    # -- fixtures ------------------------------------------------------
    def path(self, *parts):
        return os.path.join(self.work, *parts)

    def write_json(self, relpath, doc):
        full = self.path(relpath)
        with open(full, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return full

    def write_inputs(self, with_cards=False, with_prior=False):
        paths = {
            "registry": self.write_json("registry.json",
                                        registry_doc(self.marker)),
            "snapshot": self.write_json("snapshot.json",
                                        snapshot_doc(self.marker)),
        }
        if with_cards:
            paths["cards"] = self.write_json("cards.json",
                                             cards_doc(self.marker))
        if with_prior:
            paths["prior"] = self.write_json("prior.json", prior_doc())
        return paths

    def write_sentinel(self, relpath, role):
        full = self.path(relpath)
        parent = os.path.dirname(full)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        body = ("sentinel-%s-%s\n" % (role, self.marker)).encode("utf-8")
        with open(full, "wb") as handle:
            handle.write(body)
        return full, body

    # -- invocation ----------------------------------------------------
    def run_tool(self, *argv, cwd=None):
        return subprocess.run(
            [sys.executable, TOOL, *argv],
            capture_output=True, text=True, timeout=60,
            cwd=cwd or self.work)

    def base_argv(self, paths, ledger_out, plan_out, extra=None):
        argv = ["--registry", paths["registry"],
               "--snapshot", paths["snapshot"],
               "--ledger-out", ledger_out, "--plan-out", plan_out]
        if "cards" in paths:
            argv += ["--cards", paths["cards"]]
        if "prior" in paths:
            argv += ["--prior-ledger", paths["prior"]]
        if extra:
            argv += extra
        return argv

    # -- refusal assertions --------------------------------------------
    def assert_bytes_preserved(self, before, absent=()):
        for path, body in before.items():
            self.assertTrue(os.path.isfile(path),
                            "refusal must not delete %s" % path)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), body,
                                 "refusal must leave %s byte-identical"
                                 % path)
        for path in absent:
            self.assertFalse(os.path.lexists(path),
                             "refusal must not create %s" % path)

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def assert_safe_failure_text(self, proc):
        combined = proc.stdout + proc.stderr
        self.assertNotIn("Traceback (most recent call last)", combined)
        self.assertNotIn("[Errno", combined)
        self.assertNotIn("Errno ", combined)
        self.assertNotIn(self.marker, combined,
                         "refusal must not echo evidence bytes")
        path_options = {
            "--registry", "--snapshot", "--cards", "--prior-ledger",
            "--ledger-out", "--plan-out"}
        supplied_paths = [value for option, value in
                          zip(proc.args, proc.args[1:])
                          if option in path_options]
        for path in [self.work, *supplied_paths]:
            for spelling in {path, json.dumps(path)[1:-1]}:
                self.assertNotIn(spelling, combined,
                                 "refusal must not disclose paths in either "
                                 "stream")
        return combined

    def assert_text_refusal(self, proc, before, absent=(),
                            code=CODE_CONFLICT):
        self.assertEqual(proc.returncode, EXIT_REFUSED,
                         "collision must exit %d, got %d: stdout=%r stderr=%r"
                         % (EXIT_REFUSED, proc.returncode,
                            proc.stdout, proc.stderr))
        combined = self.assert_safe_failure_text(proc)
        self.assertIn(code, combined,
                      "refusal must carry the stable code %r: %r"
                      % (code, combined))
        self.assertEqual(proc.stdout, "",
                         "text refusal must not print success prose")
        self.assertEqual(proc.stderr,
                         "pr_closeout_ledger: %s: %s\n" % (code, NEXT_ACTION),
                         "text refusal must contain only the stable code "
                         "and fixed distinct-paths remedy")
        self.assert_bytes_preserved(before, absent)

    def parse_envelope(self, proc):
        try:
            data = json.loads(
                proc.stdout,
                parse_constant=lambda value: self.fail(
                    "non-finite JSON value: " + value))
        except json.JSONDecodeError:
            self.fail("--json refusal must be one parseable JSON envelope, "
                      "not text/usage: %r (stderr=%r)"
                      % (proc.stdout, proc.stderr))
        self.assertIsInstance(data, dict)
        return data

    def assert_json_refusal(self, proc, before, absent=(),
                            code=CODE_CONFLICT):
        self.assertEqual(proc.returncode, EXIT_REFUSED,
                         "collision must exit %d, got %d: stdout=%r stderr=%r"
                         % (EXIT_REFUSED, proc.returncode,
                            proc.stdout, proc.stderr))
        data = self.parse_envelope(proc)
        self.assertEqual(data.get("code"), code)
        self.assertIs(data.get("retryable"), False)
        self.assertEqual(data, {"code": code, "retryable": False,
                                "next_action": NEXT_ACTION},
                         "JSON refusal must have only the fixed finite schema")
        self.assertEqual(proc.stderr, "",
                         "JSON refusal must not disclose paths on stderr")
        self.assert_safe_failure_text(proc)
        self.assert_bytes_preserved(before, absent)
        return data


class OutputOutputCollision(OutputAliasBase):
    def test_identical_output_strings_refuse_before_either_write(self):
        paths = self.write_inputs()
        same, body = self.write_sentinel("same.json", "shared-output")
        proc = self.run_tool(*self.base_argv(paths, same, same))
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_text_refusal(
            proc, {same: body,
                   paths["registry"]: reg, paths["snapshot"]: snap})

    def test_identical_nonexistent_outputs_refuse_and_create_nothing(self):
        paths = self.write_inputs()
        target = self.path("never-created.json")
        proc = self.run_tool(*self.base_argv(paths, target, target))
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_text_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap},
            absent=[target])

    def test_dotdot_normalized_alias_refuses(self):
        paths = self.write_inputs()
        direct, body = self.write_sentinel("out.json", "ledger-target")
        aliased = os.path.join(self.work, "sub", "..", "out.json")
        os.mkdir(self.path("sub"))
        for ledger_out, plan_out in ((direct, aliased), (aliased, direct)):
            with self.subTest(ledger_out=ledger_out, plan_out=plan_out):
                proc = self.run_tool(
                    *self.base_argv(paths, ledger_out, plan_out))
                reg = open(paths["registry"], "rb").read()
                snap = open(paths["snapshot"], "rb").read()
                self.assert_text_refusal(
                    proc, {direct: body,
                           paths["registry"]: reg, paths["snapshot"]: snap})

    def test_absolute_relative_alias_refuses(self):
        paths = self.write_inputs()
        abs_path, body = self.write_sentinel("ledger.json", "ledger-target")
        proc = self.run_tool(
            "--registry", paths["registry"], "--snapshot", paths["snapshot"],
            "--ledger-out", "ledger.json",
            "--plan-out", abs_path)
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_text_refusal(
            proc, {abs_path: body,
                   paths["registry"]: reg, paths["snapshot"]: snap})

    def test_symlink_file_alias_refuses(self):
        paths = self.write_inputs()
        target, body = self.write_sentinel("target.json", "link-target")
        link = self.path("link.json")
        os.symlink(target, link)
        for ledger_out, plan_out in ((target, link), (link, target)):
            with self.subTest(ledger_out=ledger_out, plan_out=plan_out):
                proc = self.run_tool(
                    *self.base_argv(paths, ledger_out, plan_out))
                reg = open(paths["registry"], "rb").read()
                snap = open(paths["snapshot"], "rb").read()
                self.assert_text_refusal(
                    proc, {target: body,
                           paths["registry"]: reg, paths["snapshot"]: snap})
                self.assertTrue(os.path.islink(link))

    def test_symlinked_parent_alias_refuses(self):
        paths = self.write_inputs()
        os.mkdir(self.path("realdir"))
        target, body = self.write_sentinel(
            os.path.join("realdir", "out.json"), "parent-target")
        os.symlink(self.path("realdir"), self.path("linkdir"))
        proc = self.run_tool(*self.base_argv(
            paths, target, self.path("linkdir", "out.json")))
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_text_refusal(
            proc, {target: body,
                   paths["registry"]: reg, paths["snapshot"]: snap})

    def test_hardlink_alias_refuses(self):
        paths = self.write_inputs()
        first, body = self.write_sentinel("first.json", "hardlink-target")
        second = self.path("second.json")
        try:
            os.link(first, second)
        except OSError as error:
            self.skipTest("hard links unavailable: %s" % error)
        proc = self.run_tool(*self.base_argv(paths, first, second))
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_text_refusal(
            proc, {first: body, second: body,
                   paths["registry"]: reg, paths["snapshot"]: snap})

    def test_collision_in_nonexistent_directory_refuses_without_creating(self):
        paths = self.write_inputs()
        missing_dir = self.path("no-such-dir")
        target = os.path.join(missing_dir, "out.json")
        proc = self.run_tool(*self.base_argv(paths, target, target))
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_text_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap},
            absent=[missing_dir, target])

    def test_refusal_is_idempotent(self):
        paths = self.write_inputs()
        same, body = self.write_sentinel("same.json", "shared-output")
        argv = self.base_argv(paths, same, same)
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        before = {same: body,
                  paths["registry"]: reg, paths["snapshot"]: snap}
        for _ in range(2):
            proc = self.run_tool(*argv)
            self.assert_text_refusal(proc, before)


class InputEvidenceProtection(OutputAliasBase):
    def _assert_unreadable_protected_evidence_keeps_exit_5(self, role):
        # Moving alias inspection before input validation must not reclassify
        # unreadable evidence as a usage/output-path failure.
        paths = self.write_inputs(with_cards=True)
        locked = self.path("locked-evidence")
        os.mkdir(locked)
        hidden = os.path.join(locked, role + ".json")
        os.rename(paths[role], hidden)
        paths[role] = hidden
        ledger_out, ledger_body, plan_out, plan_body = self._sentinel_outputs()
        before = {path: self.read_bytes(path) for path in paths.values()}
        before.update({ledger_out: ledger_body, plan_out: plan_body})
        for json_mode in (False, True):
            with self.subTest(input=role, json_mode=json_mode):
                os.chmod(locked, 0)
                try:
                    try:
                        with open(hidden, "rb"):
                            pass
                    except PermissionError:
                        pass
                    else:
                        self.skipTest("filesystem ignores permission bits here")
                    proc = self.run_tool(*self.base_argv(
                        paths, ledger_out, plan_out,
                        extra=["--json"] if json_mode else []))
                finally:
                    os.chmod(locked, 0o755)
                self.assert_bytes_preserved(before)
                self.assertEqual(
                    proc.returncode, 5,
                    "unreadable %s must retain evidence exit 5, not an "
                    "output-path refusal: stdout=%r stderr=%r"
                    % (role, proc.stdout, proc.stderr))
                self.assertEqual(proc.stdout, "",
                                 "normal-mode evidence errors do not acquire "
                                 "a --json evidence envelope")
                self.assertTrue(proc.stderr.strip())
                self.assertNotIn(CODE_CONFLICT, proc.stderr)
                self.assertNotIn(CODE_UNVERIFIABLE, proc.stderr)
                self.assertNotIn("Traceback (most recent call last)", proc.stderr)

    def test_unreadable_registry_keeps_evidence_exit_5(self):
        self._assert_unreadable_protected_evidence_keeps_exit_5("registry")

    def test_unreadable_snapshot_keeps_evidence_exit_5(self):
        self._assert_unreadable_protected_evidence_keeps_exit_5("snapshot")

    def test_unreadable_cards_keeps_evidence_exit_5(self):
        self._assert_unreadable_protected_evidence_keeps_exit_5("cards")

    def _sentinel_outputs(self):
        ledger_out, ledger_body = self.write_sentinel(
            "ledger.json", "ledger-sentinel")
        plan_out, plan_body = self.write_sentinel(
            "plan.json", "plan-sentinel")
        return ledger_out, ledger_body, plan_out, plan_body

    def test_ledger_out_aliased_to_registry_refuses(self):
        paths = self.write_inputs()
        _, ledger_body, plan_out, plan_body = self._sentinel_outputs()
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        proc = self.run_tool(*self.base_argv(
            paths, paths["registry"], plan_out))
        self.assert_text_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                   self.path("ledger.json"): ledger_body,
                   plan_out: plan_body})

    def test_plan_out_aliased_to_registry_refuses(self):
        paths = self.write_inputs()
        ledger_out, ledger_body, _, plan_body = self._sentinel_outputs()
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        proc = self.run_tool(*self.base_argv(
            paths, ledger_out, paths["registry"]))
        self.assert_text_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                   ledger_out: ledger_body,
                   self.path("plan.json"): plan_body})

    def test_either_output_aliased_to_snapshot_refuses(self):
        for which in ("ledger", "plan"):
            with self.subTest(output=which):
                paths = self.write_inputs()
                ledger_out, ledger_body, plan_out, plan_body = (
                    self._sentinel_outputs())
                reg = open(paths["registry"], "rb").read()
                snap = open(paths["snapshot"], "rb").read()
                if which == "ledger":
                    argv = self.base_argv(
                        paths, paths["snapshot"], plan_out)
                else:
                    argv = self.base_argv(
                        paths, ledger_out, paths["snapshot"])
                proc = self.run_tool(*argv)
                self.assert_text_refusal(
                    proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                           ledger_out: ledger_body, plan_out: plan_body})

    def test_either_output_aliased_to_cards_refuses(self):
        for which in ("ledger", "plan"):
            with self.subTest(output=which):
                paths = self.write_inputs(with_cards=True)
                ledger_out, ledger_body, plan_out, plan_body = (
                    self._sentinel_outputs())
                reg = open(paths["registry"], "rb").read()
                snap = open(paths["snapshot"], "rb").read()
                cards = open(paths["cards"], "rb").read()
                if which == "ledger":
                    argv = self.base_argv(paths, paths["cards"], plan_out)
                else:
                    argv = self.base_argv(paths, ledger_out, paths["cards"])
                proc = self.run_tool(*argv)
                self.assert_text_refusal(
                    proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                           paths["cards"]: cards,
                           ledger_out: ledger_body, plan_out: plan_body})

    def test_symlink_output_to_input_refuses(self):
        paths = self.write_inputs()
        _, ledger_body, plan_out, plan_body = self._sentinel_outputs()
        link = self.path("ledger-link.json")
        os.symlink(paths["registry"], link)
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        proc = self.run_tool(*self.base_argv(paths, link, plan_out))
        self.assert_text_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                   self.path("ledger.json"): ledger_body,
                   plan_out: plan_body})

    def test_hardlink_output_to_input_refuses(self):
        paths = self.write_inputs()
        link = self.path("registry-hardlink.json")
        try:
            os.link(paths["registry"], link)
        except OSError as error:
            self.skipTest("hard links unavailable: %s" % error)
        _, ledger_body, plan_out, plan_body = self._sentinel_outputs()
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        proc = self.run_tool(*self.base_argv(paths, link, plan_out))
        self.assert_text_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                   link: reg,
                   self.path("ledger.json"): ledger_body,
                   plan_out: plan_body})

    def test_prior_ledger_equal_to_protected_input_still_refuses(self):
        # The prior-ledger exemption never extends to a path that is also a
        # protected registry/snapshot/cards input: evidence wins.
        paths = self.write_inputs()
        _, ledger_body, plan_out, plan_body = self._sentinel_outputs()
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        proc = self.run_tool(
            "--registry", paths["registry"], "--snapshot", paths["snapshot"],
            "--prior-ledger", paths["registry"],
            "--ledger-out", paths["registry"], "--plan-out", plan_out)
        self.assert_text_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                   self.path("ledger.json"): ledger_body,
                   plan_out: plan_body})


class FailClosedAndMachineContract(OutputAliasBase):
    def test_json_mode_output_collision_is_parseable_envelope(self):
        paths = self.write_inputs()
        same, body = self.write_sentinel("same.json", "shared-output")
        proc = self.run_tool(
            *self.base_argv(paths, same, same, extra=["--json"]))
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_json_refusal(
            proc, {same: body,
                   paths["registry"]: reg, paths["snapshot"]: snap})

    def test_json_mode_input_alias_is_parseable_envelope(self):
        paths = self.write_inputs()
        _, ledger_body, plan_out, plan_body = self._sentinel_outputs()
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        proc = self.run_tool(
            *self.base_argv(paths, paths["registry"], plan_out,
                            extra=["--json"]))
        self.assert_json_refusal(
            proc, {paths["registry"]: reg, paths["snapshot"]: snap,
                   self.path("ledger.json"): ledger_body,
                   plan_out: plan_body})

    def test_json_mode_symlink_alias_is_parseable_envelope(self):
        paths = self.write_inputs()
        target, body = self.write_sentinel("target.json", "link-target")
        link = self.path("link.json")
        os.symlink(target, link)
        proc = self.run_tool(
            *self.base_argv(paths, target, link, extra=["--json"]))
        reg = open(paths["registry"], "rb").read()
        snap = open(paths["snapshot"], "rb").read()
        self.assert_json_refusal(
            proc, {target: body,
                   paths["registry"]: reg, paths["snapshot"]: snap})

    def test_next_action_is_fixed_across_collision_shapes(self):
        paths = self.write_inputs()
        same, _ = self.write_sentinel("same.json", "shared-output")
        _, _, plan_out, _ = self._sentinel_outputs()
        first = self.run_tool(
            *self.base_argv(paths, same, same, extra=["--json"]))
        second = self.run_tool(
            *self.base_argv(paths, paths["snapshot"], plan_out,
                            extra=["--json"]))
        self.assertEqual(first.returncode, EXIT_REFUSED, first.stderr)
        self.assertEqual(second.returncode, EXIT_REFUSED, second.stderr)
        first_action = self.parse_envelope(first).get("next_action")
        second_action = self.parse_envelope(second).get("next_action")
        self.assertIsInstance(first_action, str)
        self.assertTrue(first_action.strip())
        self.assertEqual(first_action, second_action,
                         "next_action must be one fixed string, not shaped "
                         "per collision")

    def test_unverifiable_paths_fail_closed(self):
        paths = self.write_inputs(with_cards=True)
        locked = self.path("locked")
        os.mkdir(locked)
        hidden, hidden_body = self.write_sentinel(
            os.path.join("locked", "output.json"), "locked-output")
        other, other_body = self.write_sentinel("other.json", "other-output")
        before = {path: self.read_bytes(path) for path in paths.values()}
        before.update({hidden: hidden_body, other: other_body})
        for ledger_out, plan_out in ((hidden, other), (other, hidden)):
            for json_mode in (False, True):
                with self.subTest(ledger_out=ledger_out, json_mode=json_mode):
                    os.chmod(locked, 0)
                    try:
                        try:
                            os.stat(hidden)
                        except PermissionError:
                            pass
                        else:
                            self.skipTest("filesystem ignores permission bits "
                                          "here")
                        proc = self.run_tool(*self.base_argv(
                            paths, ledger_out, plan_out,
                            extra=["--json"] if json_mode else []))
                    finally:
                        os.chmod(locked, 0o755)
                    assertion = (self.assert_json_refusal if json_mode
                                 else self.assert_text_refusal)
                    assertion(proc, before, code=CODE_UNVERIFIABLE)

    def test_symlink_loop_outputs_are_unverifiable_in_text_and_json(self):
        # Unlike chmod denial, ELOOP still exercises the error branch when
        # the runtime is privileged enough to bypass directory permissions.
        paths = self.write_inputs(with_cards=True)
        first = self.path("loop-a.json")
        second = self.path("loop-b.json")
        os.symlink("loop-b.json", first)
        os.symlink("loop-a.json", second)
        with self.assertRaises(OSError) as raised:
            os.stat(first)
        self.assertEqual(raised.exception.errno, errno.ELOOP)
        other, other_body = self.write_sentinel("other.json", "other-output")
        before = {path: self.read_bytes(path) for path in paths.values()}
        before[other] = other_body
        for ledger_out, plan_out in ((first, other), (other, first)):
            for json_mode in (False, True):
                with self.subTest(ledger_out=ledger_out, json_mode=json_mode):
                    proc = self.run_tool(*self.base_argv(
                        paths, ledger_out, plan_out,
                        extra=["--json"] if json_mode else []))
                    self.assertTrue(os.path.islink(first))
                    self.assertTrue(os.path.islink(second))
                    self.assertEqual(os.readlink(first), "loop-b.json")
                    self.assertEqual(os.readlink(second), "loop-a.json")
                    assertion = (self.assert_json_refusal if json_mode
                                 else self.assert_text_refusal)
                    assertion(proc, before, code=CODE_UNVERIFIABLE)

    def test_refusals_do_not_disclose_relative_path_spellings(self):
        # All CLI paths are relative here: leaking argv must be caught even
        # when neither the fixture directory nor its content marker is echoed.
        paths = self.write_inputs(with_cards=True, with_prior=True)
        same, body = self.write_sentinel("shared output.json", "shared-output")
        before = {path: self.read_bytes(path) for path in paths.values()}
        before[same] = body
        relative = {role: os.path.relpath(path, self.work)
                    for role, path in paths.items()}
        for json_mode in (False, True):
            with self.subTest(json_mode=json_mode):
                proc = self.run_tool(*self.base_argv(
                    relative, "shared output.json", "./shared output.json",
                    extra=["--json"] if json_mode else []))
                assertion = (self.assert_json_refusal if json_mode
                             else self.assert_text_refusal)
                assertion(proc, before)

    def test_help_states_distinct_output_requirement(self):
        proc = self.run_tool("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("distinct", proc.stdout.lower(),
                      "--help must state that ledger-out and plan-out "
                      "must be distinct from each other and the inputs")

    def _sentinel_outputs(self):
        ledger_out, ledger_body = self.write_sentinel(
            "ledger.json", "ledger-sentinel")
        plan_out, plan_body = self.write_sentinel(
            "plan.json", "plan-sentinel")
        return ledger_out, ledger_body, plan_out, plan_body


class PositiveControls(OutputAliasBase):
    def test_distinct_outputs_succeed(self):
        paths = self.write_inputs(with_cards=True)
        ledger_out = self.path("ledger-new.json")
        plan_out = self.path("plan-new.json")
        proc = self.run_tool(*self.base_argv(paths, ledger_out, plan_out))
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        for path in (ledger_out, plan_out):
            self.assertTrue(os.path.isfile(path))
        with open(ledger_out, encoding="utf-8") as handle:
            ledger_doc = json.load(handle)
        with open(plan_out, encoding="utf-8") as handle:
            plan_doc = json.load(handle)
        self.assertIn("records", ledger_doc)
        self.assertIn("two-web-next#291", ledger_doc["records"])
        self.assertIn("proposals", plan_doc)
        self.assertIsInstance(plan_doc["proposals"], list)

    def test_distinct_outputs_json_mode_succeeds(self):
        paths = self.write_inputs()
        ledger_out = self.path("ledger-new.json")
        plan_out = self.path("plan-new.json")
        proc = self.run_tool(
            *self.base_argv(paths, ledger_out, plan_out, extra=["--json"]))
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        plan_doc = json.loads(proc.stdout)
        self.assertIn("proposals", plan_doc)
        self.assertTrue(os.path.isfile(ledger_out))
        self.assertTrue(os.path.isfile(plan_out))

    def test_prior_ledger_in_place_replacement_succeeds(self):
        paths = self.write_inputs(with_prior=True)
        plan_out = self.path("plan-new.json")
        proc = self.run_tool(
            "--registry", paths["registry"], "--snapshot", paths["snapshot"],
            "--prior-ledger", paths["prior"],
            "--ledger-out", paths["prior"], "--plan-out", plan_out)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        with open(paths["prior"], encoding="utf-8") as handle:
            ledger_doc = json.load(handle)
        self.assertIn("records", ledger_doc)
        self.assertIn("two-web-next#291", ledger_doc["records"])
        with open(plan_out, encoding="utf-8") as handle:
            plan_doc = json.load(handle)
        self.assertIn("proposals", plan_doc)

    def test_proposal_and_exit_compatibility_preserved(self):
        # The minimal unmapped fixture proposes exactly one owned
        # owner_unmapped row and exits 1 today; the guard must not change
        # proposal or evaluation semantics for ordinary distinct outputs.
        paths = self.write_inputs()
        ledger_out = self.path("ledger-new.json")
        plan_out = self.path("plan-new.json")
        proc = self.run_tool(*self.base_argv(paths, ledger_out, plan_out))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("owner_unmapped", proc.stdout)


if __name__ == "__main__":
    unittest.main()
