"""Acceptance tests: resolve a literal role label to its source contract.

Run from the repository root:
    python3 -B -m unittest -v test_profile_role_lookup_acceptance
plus the pre-existing matrix suite as the regression control:
    python3 -B -m unittest discover -s github-runner -p test_isolated_profile_matrix.py

Feature contract under test (an optional flag on the existing offline
profile-matrix CLI, ``github-runner/garm/validate_profile_matrix.py``):

* ``--role-label LABEL`` looks up one exact, literal label string: no glob,
  regex, case-folding, trimming, list-splitting or shell expansion.
* The COMPLETE matrix in the target directory is validated first. A
  malformed, colliding or otherwise rejected matrix keeps the existing
  rejection record (``result == "profile_matrix_rejected"``) and exit 1 --
  never a partial lookup match.
* On a valid matrix the CLI prints exactly one JSON object on stdout
  (stderr empty):
      schema:  "profile-role-lookup.v1"
      outcome: "matched" | "shared_label" | "unrecognized_label"
      role_label: the exact queried string
      declaring_profile: bare filename string, or null when no single
          profile declares the label as a role label
      explicitly_excluding_profiles: sorted list of bare filenames whose
          labels.explicitly_ineligible contains the exact queried string
      explanation: a bounded string (see below)
      admission_authorized, host_verified, migration_complete: all false
  For a non-match (shared / unrecognized), the same top-level object also
  carries ``code`` (non-empty stable string), ``retryable`` (false -- the
  same query against the same matrix cannot turn into a match) and
  ``next_action`` (non-empty string).  Extra keys are permitted; the
  names above are the pinned consumer surface.
* Literal ``self-hosted`` is a shared_label, never a unique role.
* Exits: matched 0; valid-matrix shared/unrecognized lookup 2;
  invalid matrix 1 (the pre-existing rejection exit).
* Records name source files only.  They must never echo profile bodies,
  pool/role values, or enumerations of role labels other than the queried
  one.  A matched record's explanation states that declaration lookup
  does not prove live runner eligibility, selector exclusivity, capacity,
  permission or host isolation.  The same non-proof statement is also
  discoverable from ``--help`` alongside the existing matrix description.
* The lookup is read-only, deterministic, hermetic (no network or runner
  inspection) and offers no flag that selects a mutating or live mode.
* Without ``--role-label`` the default validation record, exits and public
  helpers are unchanged.

Tests named ``test_control_*`` describe behaviour that already exists and
must keep passing before and after the feature; every other test is
expected to fail until ``--role-label`` is implemented.  Everything is
observed through subprocess calls against synthetic fixture directories
(or the checked-in tree, read-only); no implementation helper is used to
compute an expectation.
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GARM = ROOT / "github-runner" / "garm"
CHECKER = GARM / "validate_profile_matrix.py"

SCHEMA = "profile-role-lookup.v1"
SENTINEL = "lookup-sentinel-never-echoed"
# Role labels used by the synthetic matrix.  None of them appears inside a
# fixture filename, so string-absence assertions on CLI output stay honest.
ALPHA, ALPHA_ALT = "two-alpha", "two-alpha-alt"
BETA, GAMMA = "two-beta", "two-gamma"
LEGACY = "two-legacy-x"  # excluded by some profiles, declared by none


def _doc(suffix, eligible, explicitly_ineligible):
    return {
        "admission_authorized": False,
        "installed": False,
        "host_verified": False,
        "migration_complete": False,
        # Role / pool / private-note values are sentinel-bearing: a lookup
        # record must never echo any of them.
        "role": f"{SENTINEL}-role-{suffix}",
        "pool": {
            "flavor_profile": f"{SENTINEL}-flavor-{suffix}",
            "runner_prefix": f"{SENTINEL}-prefix-{suffix}",
        },
        "fixture_private_note": f"{SENTINEL}-body-{suffix}",
        "labels": {
            "eligible": list(eligible),
            "explicitly_ineligible": sorted(explicitly_ineligible),
        },
    }


def synthetic_profiles():
    """Three mutually symmetric profiles, independent of the checked-in set.

    p-one declares two role labels (ALPHA and its alternate), mirroring a
    profile that carries both a class label and a fleet label.
    """
    return {
        "p-one-image-profile.json": _doc(
            "a", ["self-hosted", ALPHA, ALPHA_ALT], [BETA, GAMMA]),
        "p-two-image-profile.json": _doc(
            "b", ["self-hosted", BETA], [ALPHA, ALPHA_ALT, GAMMA, LEGACY]),
        "p-three-image-profile.json": _doc(
            "c", ["self-hosted", GAMMA], [ALPHA, ALPHA_ALT, BETA, LEGACY]),
    }


def expected_from_documents(documents):
    """Map each declared role label to (declaring, sorted excluders).

    Expectations are derived straight from fixture JSON, not from any
    checker helper: role labels are labels.eligible minus self-hosted.
    """
    expected = {}
    for name, doc in documents.items():
        labels = doc["labels"]
        for label in set(labels["eligible"]) - {"self-hosted"}:
            expected.setdefault(label, [None, []])[0] = name
        for label in labels["explicitly_ineligible"]:
            expected.setdefault(label, [None, []])[1].append(name)
    return {label: (declaring, sorted(excluders))
            for label, (declaring, excluders) in expected.items()}


def checked_in_documents():
    return {path.name: json.loads(path.read_text())
            for path in sorted(GARM.glob("*-image-profile.json"))}


class ProfileRoleLookupAcceptance(unittest.TestCase):
    maxDiff = None

    # ---------------------------------------------------------- helpers
    def run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-B", str(CHECKER), *args],
            capture_output=True, text=True, timeout=60, cwd=cwd)

    def write_tree(self, directory, profiles):
        for name, doc in profiles.items():
            (Path(directory) / name).write_text(json.dumps(doc))

    def snapshot(self, directory):
        return {path.name: path.read_bytes()
                for path in sorted(Path(directory).iterdir())}

    def single_json(self, proc, context):
        try:
            record = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"{context}: stdout is not one parseable JSON object "
                      f"(exit={proc.returncode}, stdout={proc.stdout!r}, "
                      f"stderr={proc.stderr!r})")
        self.assertIsInstance(record, dict, f"{context}: not a JSON object")
        self.assertEqual(proc.stderr, "", f"{context}: stderr not empty")
        return record

    def lookup(self, directory, label, *, flag_first=False, cwd=None):
        args = (["--role-label", label, str(directory)] if flag_first
                else [str(directory), "--role-label", label])
        return self.run_cli(*args, cwd=cwd)

    def assert_lookup_common(self, record, proc, *, label, outcome, exit_code):
        self.assertEqual(
            proc.returncode, exit_code,
            f"exit for {label!r}: stdout={proc.stdout!r} "
            f"stderr={proc.stderr!r}")
        self.assertEqual(record.get("schema"), SCHEMA)
        self.assertEqual(record.get("outcome"), outcome)
        self.assertEqual(record.get("role_label"), label)
        for key in ("admission_authorized", "host_verified",
                    "migration_complete"):
            self.assertIs(record.get(key), False,
                          f"{key} must be false in every lookup result")
        excluding = record.get("explicitly_excluding_profiles")
        self.assertIsInstance(excluding, list,
                              "explicitly_excluding_profiles must be a list")
        self.assertEqual(excluding, sorted(excluding),
                         "explicitly_excluding_profiles must be sorted")
        for name in excluding:
            self.assertIsInstance(name, str)
            self.assertNotIn("/", name, "filenames must be bare, not paths")
            self.assertTrue(name.endswith("-image-profile.json"), name)
        explanation = record.get("explanation")
        self.assertIsInstance(explanation, str)
        self.assertTrue(explanation.strip(), "explanation must be non-empty")
        self.assertLessEqual(len(explanation), 700,
                             "explanation must stay bounded")

    def assert_nonmatch_diagnostics(self, record):
        code = record.get("code")
        self.assertIsInstance(code, str)
        self.assertTrue(code.strip(), "non-match code must be non-empty")
        self.assertIs(record.get("retryable"), False,
                      "re-running the same lookup cannot change the outcome")
        next_action = record.get("next_action")
        self.assertIsInstance(next_action, str)
        self.assertTrue(next_action.strip(),
                        "non-match next_action must be non-empty")
        self.assertLessEqual(len(next_action), 700)
        self.assertIsNone(record.get("declaring_profile"))

    @staticmethod
    def _record_strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str):
                    yield key
                yield from ProfileRoleLookupAcceptance._record_strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from ProfileRoleLookupAcceptance._record_strings(item)

    @staticmethod
    def _spans(field, needle):
        start = 0
        while needle:
            found = field.find(needle, start)
            if found < 0:
                return
            yield found, found + len(needle)
            start = found + 1

    def assert_no_leak(self, output, *, queried, documents):
        self.assertNotIn(SENTINEL, output,
                         "output echoes a role/pool/profile-body value")
        others = set()
        for doc in documents.values():
            labels = doc["labels"]
            others |= set(labels["eligible"]) - {"self-hosted", queried}
        try:
            record = json.loads(output)
        except json.JSONDecodeError:
            fields = [output]
        else:
            fields = list(self._record_strings(record))
        # Value-aware, per parsed field: an occurrence of another label is
        # excused only when it lies wholly inside an echo of the exact
        # queried value in that same field (a required role_label such as
        # "two-alpha-alt" necessarily contains "two-alpha").  Any other
        # occurrence -- a separate field or list entry, a second mention in
        # prose, or a longer label that merely contains the queried one --
        # is still an enumeration of an unrelated label.
        for field in fields:
            queried_spans = list(self._spans(field, queried))
            for other in sorted(others):
                for start, end in self._spans(field, other):
                    if any(q_start <= start and end <= q_end
                           for q_start, q_end in queried_spans):
                        continue
                    self.fail(
                        f"output enumerates unrelated role label {other!r}")

    def assert_rejected_before_lookup(self, directory, label, *, raw=None):
        proc = self.run_cli(str(directory), "--role-label", label)
        self.assertEqual(
            proc.returncode, 1,
            f"invalid matrix must keep exit 1, not a lookup exit: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        record = self.single_json(proc, "rejected matrix with --role-label")
        self.assertEqual(record.get("result"), "profile_matrix_rejected")
        self.assertNotEqual(record.get("schema"), SCHEMA,
                            "partial lookup record on a rejected matrix")
        self.assertNotEqual(record.get("outcome"), "matched",
                            "partial match on a rejected matrix")
        self.assertIs(record.get("admission_authorized"), False)
        for key in ("host_verified", "migration_complete"):
            if key in record:
                self.assertIs(record[key], False)
        self.assertNotIn(SENTINEL, proc.stdout)
        return record

    # --------------------------------------------------------- controls
    def test_control_default_validation_of_checked_in_tree_unchanged(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record = self.single_json(proc, "default run")
        self.assertEqual(record.get("result"), "profile_matrix_valid")
        on_disk = sorted(path.name
                          for path in GARM.glob("*-image-profile.json"))
        self.assertEqual(record.get("profiles"), on_disk)
        for key in ("admission_authorized", "host_verified",
                    "migration_complete"):
            self.assertIs(record.get(key), False)
        self.assertNotEqual(record.get("schema"), SCHEMA,
                            "default record must not become a lookup record")

    def test_control_default_validation_and_rejection_of_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, synthetic_profiles())
            green = self.run_cli(tmp)
            self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
            record = self.single_json(green, "default synthetic run")
            self.assertEqual(record.get("result"), "profile_matrix_valid")
            self.assertEqual(record.get("profiles"),
                             sorted(synthetic_profiles()))
        with tempfile.TemporaryDirectory() as tmp:
            red = self.run_cli(tmp)
            self.assertEqual(red.returncode, 1, red.stdout + red.stderr)
            record = self.single_json(red, "default empty-directory run")
            self.assertEqual(record.get("result"), "profile_matrix_rejected")
            self.assertIs(record.get("admission_authorized"), False)
            self.assertIn("profiles: at least",
                          "\n".join(record.get("violations", [])))

    def test_control_public_helpers_preserved(self):
        runner_dir = str(ROOT / "github-runner")
        sys.dont_write_bytecode = True
        sys.path.insert(0, runner_dir)
        try:
            from garm.validate_profile_matrix import (
                PROFILE_GLOB,
                RESULT,
                InvalidMatrix,
                parse,
                validate,
                validate_directory,
            )
        finally:
            sys.path.remove(runner_dir)
        self.assertEqual(RESULT, "profile_matrix_valid")
        self.assertEqual(PROFILE_GLOB, "*-image-profile.json")
        self.assertTrue(callable(parse) and callable(validate)
                        and callable(validate_directory))
        self.assertTrue(issubclass(InvalidMatrix, Exception))
        result = validate(copy.deepcopy(synthetic_profiles()))
        self.assertEqual(result["result"], RESULT)
        for key in ("admission_authorized", "host_verified",
                    "migration_complete"):
            self.assertIs(result[key], False)

    def test_control_source_has_no_network_or_subprocess_calls(self):
        source = CHECKER.read_text()
        for token in ("urllib.request", "urlopen", "http.client",
                      "import socket", "from socket", "import subprocess",
                      "from subprocess", "Popen", "os.system",
                      "import requests", "from requests"):
            self.assertNotIn(token, source,
                             f"offline CLI source must not use {token!r}")

    def test_control_mutating_or_live_flags_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, synthetic_profiles())
            before = self.snapshot(tmp)
            for flag in ("--apply", "--live", "--install", "--write",
                         "--provision"):
                with self.subTest(flag=flag):
                    proc = self.run_cli(tmp, "--role-label", ALPHA, flag)
                    self.assertNotEqual(
                        proc.returncode, 0,
                        f"{flag} must not be accepted: {proc.stdout!r}")
                    self.assertNotIn('"outcome": "matched"', proc.stdout)
            self.assertEqual(before, self.snapshot(tmp))

    def test_control_no_leak_distinguishes_queried_from_disclosed(self):
        documents = synthetic_profiles()
        # The exact queried value, echoed as a field and in prose, is
        # allowed even though ALPHA is a prefix substring of ALPHA_ALT.
        allowed = json.dumps({
            "role_label": ALPHA_ALT,
            "explanation": f"matched {ALPHA_ALT}",
        })
        self.assert_no_leak(allowed, queried=ALPHA_ALT,
                            documents=documents)
        disclosures = [
            (ALPHA_ALT, {"role_label": ALPHA_ALT, "others": [ALPHA]}),
            (ALPHA_ALT, {"explanation": f"{ALPHA_ALT} or {ALPHA}"}),
            (ALPHA, {"role_label": ALPHA, "others": [ALPHA_ALT]}),
            (ALPHA_ALT, {"role_label": ALPHA_ALT, "others": [BETA]}),
        ]
        for queried, record in disclosures:
            with self.subTest(queried=queried, record=record):
                with self.assertRaises(AssertionError):
                    self.assert_no_leak(json.dumps(record), queried=queried,
                                        documents=documents)

    # ------------------------------------------------------ discoverable
    def test_help_documents_role_label_lookup_and_non_proof(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        help_text = " ".join(proc.stdout.split())
        lowered = help_text.lower()
        self.assertIn("--role-label", help_text)
        self.assertIn("self-hosted", lowered)
        self.assertTrue(
            "does not prove" in lowered or "proves neither" in lowered
            or "not proof" in lowered,
            "--help must state the lookup non-proof alongside the existing "
            "matrix description")
        for flag in ("--apply", "--live", "--install", "--write"):
            self.assertNotIn(flag, help_text)

    # ----------------------------------------------------------- matched
    def test_matched_unique_and_alternate_labels_synthetic(self):
        documents = synthetic_profiles()
        expected = expected_from_documents(documents)
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            for label in (ALPHA, ALPHA_ALT, BETA, GAMMA):
                with self.subTest(label=label):
                    proc = self.run_cli(tmp, "--role-label", label)
                    record = self.single_json(proc, f"lookup {label}")
                    self.assert_lookup_common(
                        record, proc, label=label, outcome="matched",
                        exit_code=0)
                    declaring, excluding = expected[label]
                    self.assertEqual(record.get("declaring_profile"),
                                     declaring)
                    self.assertNotIn("/", record["declaring_profile"])
                    self.assertEqual(
                        record.get("explicitly_excluding_profiles"),
                        excluding)
                    self.assert_no_leak(proc.stdout, queried=label,
                                        documents=documents)

    def test_matched_on_checked_in_tree_default_and_explicit_directory(self):
        documents = checked_in_documents()
        self.assertGreaterEqual(len(documents), 2)
        expected = expected_from_documents(documents)
        cases = [
            ("default directory", [], "two-test",
             "test-class-image-profile.json"),
            ("directory before flag", [str(GARM)], "two-selfhosted",
             "generic-image-profile.json"),
        ]
        for context, prefix, label, known_file in cases:
            with self.subTest(context=context, label=label):
                proc = self.run_cli(*prefix, "--role-label", label)
                record = self.single_json(proc, context)
                self.assert_lookup_common(record, proc, label=label,
                                          outcome="matched", exit_code=0)
                declaring, excluding = expected[label]
                self.assertEqual(declaring, known_file)
                self.assertEqual(record.get("declaring_profile"), known_file)
                self.assertEqual(
                    record.get("explicitly_excluding_profiles"), excluding)
        # The flag may also precede the positional directory argument.
        proc = self.lookup(GARM, "two-test", flag_first=True)
        record = self.single_json(proc, "flag before directory")
        self.assert_lookup_common(record, proc, label="two-test",
                                  outcome="matched", exit_code=0)
        self.assertEqual(record.get("declaring_profile"),
                         "test-class-image-profile.json")

    def test_new_profile_is_discovered_without_code_change(self):
        roles = [ALPHA, BETA, GAMMA, "two-delta"]
        files = {ALPHA: "p-one-image-profile.json",
                 BETA: "p-two-image-profile.json",
                 GAMMA: "p-three-image-profile.json",
                 "two-delta": "p-four-image-profile.json"}
        documents = {
            files[role]: _doc(str(index), ["self-hosted", role],
                              [other for other in roles if other != role])
            for index, role in enumerate(roles)
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            proc = self.run_cli(tmp, "--role-label", "two-delta")
            record = self.single_json(proc, "newcomer lookup")
            self.assert_lookup_common(record, proc, label="two-delta",
                                      outcome="matched", exit_code=0)
            self.assertEqual(record.get("declaring_profile"),
                             "p-four-image-profile.json")
            self.assertEqual(
                record.get("explicitly_excluding_profiles"),
                sorted(name for name in documents
                       if name != "p-four-image-profile.json"))

    def test_matched_explanation_states_what_lookup_does_not_prove(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, synthetic_profiles())
            proc = self.run_cli(tmp, "--role-label", ALPHA)
            record = self.single_json(proc, "matched explanation")
            self.assert_lookup_common(record, proc, label=ALPHA,
                                      outcome="matched", exit_code=0)
        text = record["explanation"].lower()
        self.assertTrue("does not prove" in text or "proves neither" in text
                        or "not proof" in text, text)
        self.assertIn("eligib", text)                      # live eligibility
        self.assertTrue("exclusiv" in text or "selector" in text, text)
        self.assertIn("capacity", text)
        self.assertTrue("permission" in text or "authoriz" in text
                        or "admission" in text, text)
        self.assertIn("isolat", text)                       # host isolation

    # --------------------------------------------------------- non-matches
    def test_shared_self_hosted_is_never_a_unique_role(self):
        documents = synthetic_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            proc = self.run_cli(tmp, "--role-label", "self-hosted")
            record = self.single_json(proc, "self-hosted lookup")
            self.assert_lookup_common(record, proc, label="self-hosted",
                                      outcome="shared_label", exit_code=2)
            self.assert_nonmatch_diagnostics(record)
            # No synthetic profile explicitly excludes self-hosted.
            self.assertEqual(record.get("explicitly_excluding_profiles"), [])

    def test_unrecognized_label_on_valid_matrix(self):
        documents = synthetic_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            for label in ("two-unknown-zzz", "ubuntu-latest"):
                with self.subTest(label=label):
                    proc = self.run_cli(tmp, "--role-label", label)
                    record = self.single_json(proc, f"unknown {label}")
                    self.assert_lookup_common(
                        record, proc, label=label,
                        outcome="unrecognized_label", exit_code=2)
                    self.assert_nonmatch_diagnostics(record)
                    self.assertEqual(
                        record.get("explicitly_excluding_profiles"), [])
                    self.assert_no_leak(proc.stdout, queried=label,
                                        documents=documents)

    def test_excluded_only_label_is_unrecognized_with_its_excluders(self):
        documents = synthetic_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            proc = self.run_cli(tmp, "--role-label", LEGACY)
            record = self.single_json(proc, "excluded-only lookup")
            self.assert_lookup_common(record, proc, label=LEGACY,
                                      outcome="unrecognized_label",
                                      exit_code=2)
            self.assert_nonmatch_diagnostics(record)
            self.assertEqual(
                record.get("explicitly_excluding_profiles"),
                ["p-three-image-profile.json", "p-two-image-profile.json"])

    def test_shared_and_unrecognized_have_distinct_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, synthetic_profiles())
            codes = {}
            for label, outcome in (("self-hosted", "shared_label"),
                                   ("two-unknown-zzz",
                                    "unrecognized_label")):
                proc = self.run_cli(tmp, "--role-label", label)
                record = self.single_json(proc, f"{outcome} code")
                self.assert_lookup_common(record, proc, label=label,
                                          outcome=outcome, exit_code=2)
                self.assert_nonmatch_diagnostics(record)
                codes[outcome] = record["code"]
            self.assertNotEqual(codes["shared_label"],
                                codes["unrecognized_label"],
                                "distinct non-match causes need distinct "
                                "stable codes")

    def test_exact_literal_matching_only(self):
        lookalikes = [
            "two-alpha*", "two-*",
            "two-.*", "two-[a]lpha", "TWO-ALPHA", "two-alpha ",
            " two-alpha", "two-alpha,two-beta", "two-alpha|two-beta",
            "self-hoste", "self-hosted*",
        ]
        documents = synthetic_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            for label in lookalikes:
                with self.subTest(label=label):
                    proc = self.run_cli(tmp, "--role-label", label)
                    record = self.single_json(proc, f"literal {label!r}")
                    self.assert_lookup_common(
                        record, proc, label=label,
                        outcome="unrecognized_label", exit_code=2)
                    self.assert_nonmatch_diagnostics(record)

    def test_shell_metacharacter_label_is_literal_and_executes_nothing(self):
        documents = synthetic_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            before = self.snapshot(tmp)
            label = "$(touch lookup-pwned)"
            proc = self.run_cli(tmp, "--role-label", label, cwd=tmp)
            record = self.single_json(proc, "metacharacter lookup")
            self.assert_lookup_common(record, proc, label=label,
                                      outcome="unrecognized_label",
                                      exit_code=2)
            self.assertFalse((Path(tmp) / "lookup-pwned").exists())
            self.assertEqual(before, self.snapshot(tmp))

    # ------------------------------------------------ invalid matrices
    def test_invalid_matrix_collision_never_returns_partial_match(self):
        documents = synthetic_profiles()
        documents["p-extra-image-profile.json"] = _doc(
            "x", ["self-hosted", BETA], [ALPHA, ALPHA_ALT, GAMMA])
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            self.assert_rejected_before_lookup(tmp, BETA)

    def test_invalid_matrix_bad_authorization_or_installed_flags(self):
        mutations = [
            ("admission_authorized true",
             lambda d: d.update(admission_authorized=True)),
            ("installed true", lambda d: d.update(installed=True)),
            ("admission_authorized missing",
             lambda d: d.pop("admission_authorized")),
        ]
        for description, mutate in mutations:
            with self.subTest(mutation=description):
                documents = copy.deepcopy(synthetic_profiles())
                mutate(documents["p-two-image-profile.json"])
                with tempfile.TemporaryDirectory() as tmp:
                    self.write_tree(tmp, documents)
                    # ALPHA's own declaring profile is untouched: only
                    # whole-matrix validation before lookup can reject.
                    self.assert_rejected_before_lookup(tmp, ALPHA)

    def test_invalid_matrix_malformed_profile_or_pool_spec(self):
        for extra_name in ("bad-image-profile.json", "x-pool-spec.json"):
            with self.subTest(extra=extra_name):
                with tempfile.TemporaryDirectory() as tmp:
                    self.write_tree(tmp, synthetic_profiles())
                    (Path(tmp) / extra_name).write_text("{not json")
                    self.assert_rejected_before_lookup(tmp, ALPHA)

    def test_invalid_matrix_missing_symmetric_exclusion(self):
        documents = copy.deepcopy(synthetic_profiles())
        documents["p-two-image-profile.json"]["labels"][
            "explicitly_ineligible"].remove(ALPHA)
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            self.assert_rejected_before_lookup(tmp, ALPHA)

    def test_invalid_empty_directory_keeps_exit_1_not_lookup_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_rejected_before_lookup(tmp, ALPHA)

    # -------------------------------------------------------- properties
    def test_lookup_is_deterministic_and_never_mutates_fixtures(self):
        documents = synthetic_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            before = self.snapshot(tmp)
            for label, exit_code in ((ALPHA, 0), ("self-hosted", 2),
                                     ("two-unknown-zzz", 2)):
                with self.subTest(label=label):
                    first = self.run_cli(tmp, "--role-label", label)
                    second = self.run_cli(tmp, "--role-label", label)
                    self.assertEqual(first.returncode, exit_code)
                    self.assertEqual(first.returncode, second.returncode)
                    self.assertEqual(first.stdout, second.stdout,
                                     "identical lookup must be byte-stable")
                    self.assertEqual(first.stderr, second.stderr)
                    self.single_json(first, f"determinism {label}")
            self.assertEqual(before, self.snapshot(tmp),
                             "read-only lookup changed a fixture")

    def test_lookup_never_echoes_bodies_pool_values_or_unrelated_labels(self):
        documents = synthetic_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            for label in (ALPHA, BETA, "two-unknown-zzz"):
                with self.subTest(label=label):
                    proc = self.run_cli(tmp, "--role-label", label)
                    self.single_json(proc, f"leak scan {label}")
                    self.assert_no_leak(proc.stdout, queried=label,
                                        documents=documents)
                    # The queried label itself may be echoed; a
                    # never-queried excluded-only label must not appear.
                    if label != LEGACY:
                        self.assertNotIn(LEGACY, proc.stdout)

    def test_false_proof_flags_in_every_lookup_outcome(self):
        documents = synthetic_profiles()
        outcomes = [(ALPHA, "matched", 0),
                    ("self-hosted", "shared_label", 2),
                    ("two-unknown-zzz", "unrecognized_label", 2),
                    (LEGACY, "unrecognized_label", 2)]
        with tempfile.TemporaryDirectory() as tmp:
            self.write_tree(tmp, documents)
            for label, outcome, exit_code in outcomes:
                with self.subTest(label=label):
                    proc = self.run_cli(tmp, "--role-label", label)
                    record = self.single_json(proc, f"flags {label}")
                    self.assert_lookup_common(
                        record, proc, label=label, outcome=outcome,
                        exit_code=exit_code)


if __name__ == "__main__":
    unittest.main()
