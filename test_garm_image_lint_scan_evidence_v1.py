#!/usr/bin/env python3
"""Independent, offline acceptance contract for garm-image-lint.v1.

Run: python3 -B -m unittest -v test_garm_image_lint_scan_evidence_v1
CI discovery: test_garm_image_lint.sh runs this module on its unfiltered path.
The implementation owner imports this file unchanged; corrections belong to
its acceptance owner. Inputs are synthetic, the isolated profile is copied,
and both real CLI entrypoints run only in disposable directories.

Additive JSON contract (--json, exactly one finite object on stdout, stderr
empty for handled outcomes; ordinary text mode keeps stderr diagnostics):
  schema: "garm-image-lint.v1"; exit_code: integer matching process status.
  scan_complete: true iff at least one input was evaluated and none refused.
  measurement_state: "complete", "partial", or "not_scanned". A completed
    scan may contain rule violations; this is structural evidence, not image
    execution, admission, deployment, a vulnerability audit, or a build receipt.
  requested_files/evaluated_files/refused_files: raw operand strings in argv
    order, including duplicates; evaluated and refused partition occurrences.
    A valid UTF-8 input with a structural finding is evaluated, not refused.
    A global profile failure refuses every requested occurrence without scan.
  counts: requested/evaluated/refused/findings, all nonnegative integers.
  findings: entries with code (existing GARM-* rule ID), file, 1-based line
    (0 when a whole-file parse refusal has no location), nonempty message,
    retryable=false, next_action="fix_input". Order follows
    input occurrences, then (line, code), independent of text/JSON selection.
  errors: entries with code, file (null only without a file), line=0,
    nonempty message, retryable=false, and the following stable remediation:
      INPUT_READ_ERROR -> restore_input; INPUT_DECODE_ERROR -> encode_utf8;
      PROFILE_READ_ERROR -> restore_profile; PROFILE_INVALID -> fix_profile;
      NO_INPUT -> provide_input.
    Per-input errors follow argv order; decode/read errors are NOT structural
    GARM-FORMAT-09 findings. Profile errors name the isolated profile path.
Exit: 1 if any rule failed, even with refused inputs; else 2 for refusal or
invalid invocation; else 0. No input means not_scanned, never complete/clean.
Parser boundary: --help/-h is human help even with --json. Unknown flags and
malformed flag operands fail before report construction (2, empty stdout,
human stderr, no traceback); -- ends flag parsing. Empty file operands are
read refusals. No --profile or production/mutating flag is introduced here.

No policy registry, transport, host commands, network, image execution, or
shared-checkout Git fixtures. Copied scripts and fixture trees must not change.
The documentation owner must publish these meanings in the canonical interface
docs, repair the stale wrapper pointer, and retain the same nine-rule policy.
"""

import itertools
import json
import math
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
PROFILE = Path("github-runner/garm/isolated-image-profile.json")
DIGEST = "a" * 64
GOOD = f"""FROM ubuntu:24.04@sha256:{DIGEST}
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates=1.0 && rm -rf /var/lib/apt/lists/*
COPY app /app
USER runner
ENTRYPOINT ["/app/run"]
"""
RULE_CASES = (
    ("GARM-BASE-01", GOOD.replace(f"ubuntu:24.04@sha256:{DIGEST}", "ubuntu:24"), 1),
    ("GARM-USER-02", GOOD.replace("USER runner", "USER root"), 4),
    ("GARM-PRIV-03", GOOD.replace("COPY app /app", "RUN sudo true"), 3),
    ("GARM-APT-04", GOOD.replace("--no-install-recommends ", ""), 2),
    ("GARM-COPY-05", GOOD.replace("COPY app /app", "ADD app /app"), 3),
    ("GARM-EXEC-06", GOOD.replace('["/app/run"]', "/app/run"), 5),
    ("GARM-SECRET-07", GOOD + "ENV GH_TOKEN=synthetic-not-a-credential\n", 6),
    ("GARM-FETCH-08", GOOD + "RUN curl https://example.invalid/install | sh\n", 6),
    ("GARM-FORMAT-09", GOOD + "MAINTAINER nobody@example.com\n", 6),
)
ERROR_ACTIONS = {
    "INPUT_READ_ERROR": "restore_input",
    "INPUT_DECODE_ERROR": "encode_utf8",
    "PROFILE_READ_ERROR": "restore_profile",
    "PROFILE_INVALID": "fix_profile",
    "NO_INPUT": "provide_input",
}


class ScanEvidenceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="garm-scan-evidence-v1-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cli = self.root / "cli"
        self.cli.mkdir()
        for name in ("garm_image_lint.py", "garm_image_lint.sh"):
            shutil.copyfile(HERE / name, self.cli / name)
        self.profile = self.cli / PROFILE
        self.profile.parent.mkdir(parents=True)
        shutil.copyfile(HERE / PROFILE, self.profile)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.called = self.root / "forbidden-command-called"
        for name in ("docker", "podman", "hadolint", "curl", "wget", "apt", "apt-get",
                     "pip", "pip3", "npm", "npx", "ssh", "sudo", "systemctl", "git"):
            binary = self.bin / name
            binary.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(name)
                              + " >> " + shlex.quote(str(self.called)) + "\nexit 99\n")
            binary.chmod(0o755)
        # Allowlist rather than inherit: strips ALL Git locators/config and
        # identities, BASH_ENV/ENV, provider credentials and Python injections.
        self.env = {
            "PATH": f"{self.bin}:{Path(sys.executable).resolve().parent}:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.home),
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        self.good = self.input("good.Dockerfile", GOOD)
        self.bad = self.input("bad.Dockerfile", GOOD.replace("USER runner", "USER root"))
        self.missing = self.inputs / "missing.Dockerfile"
        self.undecodable = self.input("invalid-utf8.Dockerfile", b"\xff\xfe\n")

    def input(self, name, content):
        path = self.inputs / name
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): (
                path.stat().st_mode,
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.root.rglob("*")
        }

    def invoke(self, entrypoint, operands, json_mode=False, flags=(), separator=False):
        command = ([sys.executable, "-B", str(self.cli / "garm_image_lint.py")]
                   if entrypoint == "python" else ["/bin/bash", str(self.cli / "garm_image_lint.sh")])
        args = (["--json"] if json_mode else []) + list(flags)
        args += (["--"] if separator else []) + [str(path) for path in operands]
        before = self.snapshot()
        result = subprocess.run(command + args, cwd=self.inputs, env=self.env,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(self.snapshot(), before, "lint must not write files or execute fixture commands")
        self.assertFalse(self.called.exists(), "lint invoked a forbidden external command")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertNotIn("synthetic-not-a-credential", result.stdout + result.stderr)
        return result

    def assert_finite(self, value):
        if isinstance(value, float):
            self.assertTrue(math.isfinite(value), "non-finite JSON number")
        elif isinstance(value, dict):
            for child in value.values():
                self.assert_finite(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_finite(child)

    def report(self, entrypoint, operands, expected, evaluated, refused,
               *, error_pairs=(), findings=None, separator=False):
        result = self.invoke(entrypoint, operands, json_mode=True, separator=separator)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(result.stderr, "", "handled JSON outcomes use stdout only")

        def unique_object(pairs):
            output = {}
            for key, value in pairs:
                if key in output:
                    raise ValueError("duplicate JSON key: " + key)
                output[key] = value
            return output

        try:
            data = json.loads(result.stdout, object_pairs_hook=unique_object)
        except ValueError as error:
            self.fail(f"expected one JSON object, got {result.stdout!r}: {error}")
        self.assertIsInstance(data, dict)
        self.assert_finite(data)
        self.assertEqual(data["schema"], "garm-image-lint.v1")
        self.assertIs(type(data["exit_code"]), int)
        self.assertEqual(data["exit_code"], expected)
        complete = bool(evaluated) and not refused
        state = "complete" if complete else "partial" if evaluated else "not_scanned"
        self.assertIs(data["scan_complete"], complete)
        self.assertEqual(data["measurement_state"], state)
        self.assertEqual(data["requested_files"], [str(path) for path in operands])
        self.assertEqual(data["evaluated_files"], [str(path) for path in evaluated])
        self.assertEqual(data["refused_files"], [str(path) for path in refused])
        counts = data["counts"]
        for key, want in (("requested", len(operands)), ("evaluated", len(evaluated)),
                          ("refused", len(refused)), ("findings", len(data["findings"]))):
            self.assertIs(type(counts[key]), int)
            self.assertEqual(counts[key], want)
        self.assertEqual(counts["evaluated"] + counts["refused"], counts["requested"])
        self.assertEqual([(item["code"], item["file"]) for item in data["errors"]],
                         [(code, None if path is None else str(path)) for code, path in error_pairs])
        for item in data["errors"]:
            self.assertIs(type(item["line"]), int)
            self.assertEqual(item["line"], 0)
            self.assertIs(item["retryable"], False)
            self.assertEqual(item["next_action"], ERROR_ACTIONS[item["code"]])
            self.assertIsInstance(item["message"], str)
            self.assertTrue(item["message"].strip())
        for item in data["findings"]:
            self.assertIn(item["code"], {case[0] for case in RULE_CASES})
            self.assertIn(item["file"], data["evaluated_files"])
            self.assertIs(type(item["line"]), int)
            self.assertGreaterEqual(item["line"], 0)
            self.assertIs(item["retryable"], False)
            self.assertEqual(item["next_action"], "fix_input")
            self.assertIsInstance(item["message"], str)
            self.assertTrue(item["message"].strip())
        if findings is not None:
            self.assertEqual([(item["code"], item["file"], item["line"])
                              for item in data["findings"]],
                             [(code, str(path), line) for code, path, line in findings])
        return data, result


class ExistingTextControls(ScanEvidenceFixture):
    """Controls name existing behavior, not invented preimplementation failures."""

    def test_clean_text_is_offline_and_leaves_inputs_unchanged(self):
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                result = self.invoke(entrypoint, [self.good])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_text_retains_nine_stable_rule_ids_and_source_locations(self):
        for entrypoint, (code, content, line) in itertools.product(("python", "shell"), RULE_CASES):
            with self.subTest(entrypoint=entrypoint, code=code):
                path = self.input("rule.Dockerfile", content)
                result = self.invoke(entrypoint, [path])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"RULE {code} {path}:{line} ", result.stderr)

    def test_text_rule_failure_precedes_unreadable_in_all_mixed_orders(self):
        for entrypoint, operands in itertools.product(
                ("python", "shell"), itertools.permutations((self.good, self.bad, self.missing))):
            with self.subTest(entrypoint=entrypoint, operands=operands):
                result = self.invoke(entrypoint, operands)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"RULE GARM-USER-02 {self.bad}:4 ", result.stderr)
                self.assertIn(str(self.missing), result.stderr)

    def test_text_missing_input_and_no_operands_keep_exit_two(self):
        for entrypoint, operands in itertools.product(("python", "shell"), ([], [self.missing])):
            with self.subTest(entrypoint=entrypoint, operands=operands):
                result = self.invoke(entrypoint, operands)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip())


class JsonScanEvidence(ScanEvidenceFixture):
    def test_complete_clean_report_matches_text_exit(self):
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                _, result = self.report(entrypoint, [self.good], 0, [self.good], [], findings=[])
                self.assertEqual(result.returncode, self.invoke(entrypoint, [self.good]).returncode)

    def test_complete_rule_report_preserves_each_rule_and_line(self):
        for entrypoint, (code, content, line) in itertools.product(("python", "shell"), RULE_CASES):
            with self.subTest(entrypoint=entrypoint, code=code):
                path = self.input("rule.Dockerfile", content)
                data, result = self.report(entrypoint, [path], 1, [path], [])
                self.assertIn((code, str(path), line),
                              [(item["code"], item["file"], item["line"]) for item in data["findings"]])
                self.assertEqual(result.returncode, self.invoke(entrypoint, [path]).returncode)

    def test_structural_format_failure_is_evaluated_not_a_decode_error(self):
        path = self.input("structural.Dockerfile", GOOD + "INVENTED data\n")
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                self.report(entrypoint, [path], 1, [path], [],
                            findings=[("GARM-FORMAT-09", path, 0)])

    def test_input_read_refusal_is_not_a_structural_finding(self):
        directory = self.inputs / "directory-not-a-file"
        directory.mkdir()
        for entrypoint, path in itertools.product(("python", "shell"), (self.missing, directory, "")):
            with self.subTest(entrypoint=entrypoint, path=path):
                _, result = self.report(entrypoint, [path], 2, [], [path], findings=[],
                                        error_pairs=[("INPUT_READ_ERROR", path)])
                self.assertEqual(result.returncode, self.invoke(entrypoint, [path]).returncode)

    def test_invalid_utf8_is_refused_without_traceback_in_both_modes(self):
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                text = self.invoke(entrypoint, [self.undecodable])
                self.assertEqual(text.returncode, 2, text.stderr)
                self.assertEqual(text.stdout, "")
                self.assertTrue(text.stderr.strip())
                self.assertNotIn("GARM-FORMAT-09", text.stderr)
                self.report(entrypoint, [self.undecodable], 2, [], [self.undecodable], findings=[],
                            error_pairs=[("INPUT_DECODE_ERROR", self.undecodable)])

    def test_partial_scan_keeps_observed_findings_in_every_mixed_order(self):
        for refused, error_code in ((self.missing, "INPUT_READ_ERROR"),
                                    (self.undecodable, "INPUT_DECODE_ERROR")):
            for entrypoint, operands in itertools.product(
                    ("python", "shell"), itertools.permutations((self.good, self.bad, refused))):
                with self.subTest(entrypoint=entrypoint, operands=operands):
                    evaluated = [path for path in operands if path != refused]
                    _, result = self.report(entrypoint, operands, 1, evaluated, [refused],
                                            findings=[("GARM-USER-02", self.bad, 4)],
                                            error_pairs=[(error_code, refused)])
                    self.assertEqual(result.returncode, self.invoke(entrypoint, operands).returncode)

    def test_partial_scan_without_findings_never_claims_complete(self):
        for entrypoint, operands in itertools.product(
                ("python", "shell"), itertools.permutations((self.good, self.missing))):
            with self.subTest(entrypoint=entrypoint, operands=operands):
                self.report(entrypoint, operands, 2, [self.good], [self.missing], findings=[],
                            error_pairs=[("INPUT_READ_ERROR", self.missing)])

    def test_all_refused_errors_follow_requested_order(self):
        errors = {self.missing: "INPUT_READ_ERROR", self.undecodable: "INPUT_DECODE_ERROR"}
        for entrypoint, operands in itertools.product(
                ("python", "shell"), itertools.permutations((self.missing, self.undecodable))):
            with self.subTest(entrypoint=entrypoint, operands=operands):
                self.report(entrypoint, operands, 2, [], operands, findings=[],
                            error_pairs=[(errors[path], path) for path in operands])

    def test_findings_follow_operand_occurrences_then_line_and_code(self):
        multi = self.input("z-multi.Dockerfile", GOOD.replace("USER runner", "USER root")
                           .replace("COPY app /app", "ADD app /app")
                           + "MAINTAINER nobody@example.com\n")
        other = self.input("a-other.Dockerfile", GOOD.replace('ENTRYPOINT ["/app/run"]',
                                                             "ENTRYPOINT /app/run"))
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                self.report(entrypoint, [multi, other, multi], 1, [multi, other, multi], [], findings=[
                    ("GARM-COPY-05", multi, 3), ("GARM-USER-02", multi, 4),
                    ("GARM-FORMAT-09", multi, 6), ("GARM-EXEC-06", other, 5),
                    ("GARM-COPY-05", multi, 3), ("GARM-USER-02", multi, 4),
                    ("GARM-FORMAT-09", multi, 6),
                ])
                self.report(entrypoint, [self.missing, self.good, self.missing], 2, [self.good],
                            [self.missing, self.missing], findings=[], error_pairs=[
                                ("INPUT_READ_ERROR", self.missing), ("INPUT_READ_ERROR", self.missing),
                            ])

    def test_repeated_invocations_and_entrypoints_emit_identical_json_bytes(self):
        operands = [self.bad, self.good, self.missing]
        outputs = []
        for entrypoint in ("python", "shell", "python", "shell"):
            with self.subTest(entrypoint=entrypoint, invocation=len(outputs)):
                _, result = self.report(entrypoint, operands, 1, [self.bad, self.good], [self.missing],
                                        findings=[("GARM-USER-02", self.bad, 4)],
                                        error_pairs=[("INPUT_READ_ERROR", self.missing)])
                outputs.append(result.stdout)
        self.assertTrue(all(output == outputs[0] for output in outputs))

    def test_paths_are_data_and_report_exact_raw_operand_identities(self):
        names = ("space name.Dockerfile", "line\nbreak.Dockerfile", "snow-☃.Dockerfile",
                 "semi;$(touch NOT_EXECUTED).Dockerfile", "star[*]?.Dockerfile", "-leading.Dockerfile")
        for name in names:
            self.input(name, GOOD)
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                self.report(entrypoint, names, 0, names, [], findings=[], separator=True)
                text = self.invoke(entrypoint, names, separator=True)
                self.assertEqual(text.returncode, 0, text.stderr)
                self.assertEqual((text.stdout, text.stderr), ("", ""))

    def test_no_operands_is_explicitly_not_scanned(self):
        for entrypoint, separator in itertools.product(("python", "shell"), (False, True)):
            with self.subTest(entrypoint=entrypoint, separator=separator):
                self.report(entrypoint, [], 2, [], [], findings=[], error_pairs=[("NO_INPUT", None)],
                            separator=separator)

    def test_json_is_opt_in_not_inherited_from_the_shell(self):
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                self.report(entrypoint, [self.bad], 1, [self.bad], [],
                            findings=[("GARM-USER-02", self.bad, 4)])
                result = self.invoke(entrypoint, [self.bad])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"RULE GARM-USER-02 {self.bad}:4 ", result.stderr)


class InvocationAndProfileBoundaries(ScanEvidenceFixture):
    def test_missing_profile_refuses_all_inputs_without_evaluating_rules(self):
        self.profile.unlink()
        for entrypoint in ("python", "shell"):
            with self.subTest(entrypoint=entrypoint):
                self.report(entrypoint, [self.bad, self.good], 2, [], [self.bad, self.good], findings=[],
                            error_pairs=[("PROFILE_READ_ERROR", self.profile)])
                self.assertEqual(self.invoke(entrypoint, [self.bad, self.good]).returncode, 2)

    def test_invalid_profiles_refuse_without_traceback(self):
        original = self.profile.read_bytes()
        valid = json.loads(original)
        wrong_type = json.loads(original)
        wrong_type["base"]["distribution"] = 42
        for content in (b"{", b"[]", b"{}", b"\xff", json.dumps(wrong_type).encode("utf-8")):
            self.profile.write_bytes(content)
            for entrypoint in ("python", "shell"):
                with self.subTest(entrypoint=entrypoint, profile=content):
                    self.report(entrypoint, [self.bad], 2, [], [self.bad], findings=[],
                                error_pairs=[("PROFILE_INVALID", self.profile)])
                    text = self.invoke(entrypoint, [self.bad])
                    self.assertEqual(text.returncode, 2, text.stderr)
                    self.assertNotIn("GARM-USER-02", text.stderr)
        self.profile.write_text(json.dumps(valid))

    def test_help_is_human_output_and_discovers_json_even_when_json_is_requested(self):
        for entrypoint, json_mode, help_flag in itertools.product(
                ("python", "shell"), (False, True), ("--help", "-h")):
            with self.subTest(entrypoint=entrypoint, json_mode=json_mode, help_flag=help_flag):
                result = self.invoke(entrypoint, [], json_mode=json_mode, flags=[help_flag])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--json", result.stdout + result.stderr)
                with self.assertRaises(ValueError):
                    json.loads(result.stdout)

    def test_bad_flag_after_a_rule_failure_is_a_parser_error_not_a_scan(self):
        for entrypoint, json_mode in itertools.product(("python", "shell"), (False, True)):
            with self.subTest(entrypoint=entrypoint, json_mode=json_mode):
                result = self.invoke(entrypoint, [self.bad, "--unknown"], json_mode=json_mode)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip())
                self.assertNotIn("GARM-USER-02", result.stderr)

    def test_parser_errors_have_a_declared_text_boundary_not_partial_json(self):
        for entrypoint, json_mode, flags in itertools.product(
                ("python", "shell"), (False, True),
                (("--unknown",), ("--json=true",), ("--profile",),
                 ("--apply",), ("--write",), ("--execute",), ("--production",))):
            with self.subTest(entrypoint=entrypoint, json_mode=json_mode, flags=flags):
                result = self.invoke(entrypoint, [self.good], json_mode=json_mode, flags=flags)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
