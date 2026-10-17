"""Hermetic negative controls over the real retained/removed CI inventory."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from verification import offline_ci_contract as gate


class OfflineContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (gate.ROOT / ".github/workflows/ci.yml").read_text()
        cls.contract = json.loads(gate.CONTRACT.read_text())
        cls.blocks, extras = gate.matched_blocks(cls.text, cls.contract["retained"])
        if extras:
            raise AssertionError(extras)

    def test_real_workflow_and_immutable_pin(self):
        self.assertEqual(gate.validate(self.text, self.contract), [])

    def test_every_retained_producer_is_required(self):
        self.assertTrue(self.contract["retained"])
        for step in self.contract["retained"]:
            with self.subTest(step=step["name"]):
                block = self.blocks[step["id"]]
                mutant = self.text.replace(block, "", 1)
                self.assertNotEqual(mutant, self.text)
                errors = gate.validate(mutant, self.contract)
                self.assertTrue(any(step["name"] in error for error in errors), errors)

    def test_private_run_condition_and_error_policy_cannot_be_weakened(self):
        name = "request-record integrity suite"
        block = self.blocks[name + ":1"]
        for replacement in (
            f"      - name: {name}\n        run: true\n",
            block.replace("        if:", "        if: false #", 1),
            block + "        continue-on-error: true\n",
        ):
            with self.subTest(replacement=replacement):
                self.assertNotEqual(replacement, block)
                self.assertTrue(gate.validate(self.text.replace(block, replacement, 1),
                                              self.contract))

    def test_every_removed_input_is_forbidden_even_under_a_new_name(self):
        self.assertTrue(self.contract["removed"])
        marker = "  suites:\n"
        # Place the rogue step in a different job, so a suites-only name
        # allowlist cannot be the reason this negative control passes.
        self.assertTrue(any(step["forbidden_inputs"] for step in self.contract["removed"]))
        for step in self.contract["removed"]:
            for command in step["forbidden_inputs"]:
                with self.subTest(step=step["name"], command=command):
                    rogue = ("  duplicate-portable:\n    steps:\n"
                             "      - name: renamed duplicate\n"
                             f"        run: bash ./{command}\n\n")
                    mutant = self.text.replace(marker, rogue + marker, 1)
                    self.assertNotEqual(mutant, self.text)
                    errors = gate.validate(mutant, self.contract)
                    self.assertTrue(any("portable input reintroduced" in e for e in errors),
                                    errors)

    def test_every_removed_producer_is_forbidden_in_another_job(self):
        self.assertTrue(self.contract["removed"])
        for step in self.contract["removed"]:
            command = step["private_command"]
            rogue = ("  duplicate-portable:\n    steps:\n"
                     "      - name: renamed duplicate\n"
                     f"        working-directory: {step['working_directory']}\n"
                     "        run: |\n" + textwrap.indent(command, "          ") + "\n\n")
            with self.subTest(step=step["id"]):
                errors = gate.validate(self.text.replace("  suites:\n", rogue + "  suites:\n", 1),
                                       self.contract)
                self.assertTrue(any("portable producer reintroduced" in e for e in errors),
                                errors)

    def test_retained_package_tests_are_not_globally_banned(self):
        for directory in ("plugins/dispatch", "plugins/cliproxy-insight"):
            for scope in ("step", "job", "workflow"):
                default = ("    defaults:\n      run:\n        shell: bash\n"
                           f"        working-directory: {directory}\n") if scope == "job" else ""
                step_dir = f"        working-directory: {directory}\n" if scope == "step" else ""
                rogue = ("  directory-control:\n" + default + "    steps:\n"
                         "      - name: package test\n" + step_dir + "        run: npm test\n\n")
                mutant = self.text.replace("  suites:\n", rogue + "  suites:\n", 1)
                if scope == "workflow":
                    mutant = mutant.replace("\njobs:\n", "\ndefaults:\n  run:\n"
                                            f"    working-directory: {directory}\n\njobs:\n", 1)
                errors = gate.validate(mutant, self.contract)
                with self.subTest(directory=directory, scope=scope):
                    # A new workflow default also retargets implicit retained
                    # producers, unlike a directory local to another job.
                    self.assertEqual(bool(errors), scope == "workflow", errors)

    def test_removed_command_in_a_comment_is_not_an_invocation(self):
        command = self.contract["removed"][0]["forbidden_inputs"][0]
        marker = "  suites:\n"
        comment_job = ("  documentation-only:\n    steps:\n"
                       "      - name: documented cutover\n"
                       f"        run: |\n          # ./{command} moved\n"
                       "          true\n\n")
        self.assertEqual(gate.validate(self.text.replace(marker, comment_job + marker, 1),
                                       self.contract), [])

    def test_duplicate_retained_step_and_unnamed_run_fail_closed(self):
        block = next(iter(self.blocks.values()))
        self.assertTrue(gate.validate(self.text.replace(block, block + block, 1),
                                      self.contract))
        self.assertTrue(gate.validate(self.text.replace(block,
                                      block + "      - run: true\n", 1), self.contract))

    def test_alternate_indent_and_last_line_cannot_hide_a_producer(self):
        block = next(iter(self.blocks.values()))
        rogue = "       - name: hidden step\n         run: true\n"
        self.assertTrue(gate.validate(self.text.replace(block, block + rogue, 1),
                                      self.contract))
        suites_at_eof = (self.text.split("  suites:\n", 1)[0] + "  suites:\n" +
                         gate.jobs(self.text)["suites"].rstrip() + "\n      - run: true")
        self.assertEqual(len(gate.steps("    steps:\n      - run: true")), 1)
        self.assertTrue(gate.validate(suites_at_eof, self.contract))

    def test_partition_and_removed_evidence_are_not_optional(self):
        for key in ("baseline_ids", "retained", "removed"):
            contract = copy.deepcopy(self.contract)
            contract[key].pop()
            with self.subTest(key=key):
                self.assertTrue(gate.validate(self.text, contract))
        for field in ("toolkit_commands", "forbidden_inputs", "evidence"):
            contract = copy.deepcopy(self.contract)
            contract["removed"][0][field] = None if field == "forbidden_inputs" else []
            with self.subTest(field=field):
                self.assertTrue(gate.validate(self.text, contract))

    def test_private_compatibility_manifest_and_archive_pin_are_required(self):
        self.assertTrue(self.contract["private_compatibility"]["entries"])
        for field in ("entries", "sha256"):
            contract = copy.deepcopy(self.contract)
            if field == "entries":
                contract["private_compatibility"][field].pop()
            else:
                contract["private_compatibility"][field] = "0" * 64
            with self.subTest(field=field):
                self.assertTrue(gate.validate(self.text, contract))

    def test_missing_digest_cannot_accept_a_deleted_producer(self):
        contract = copy.deepcopy(self.contract)
        step = contract["retained"][0]
        mutant = self.text.replace(self.blocks[step["id"]], "", 1)
        step["definition_sha256"] = None
        self.assertTrue(gate.validate(mutant, contract))

    def test_inherited_suite_policy_and_workflow_env_cannot_be_weakened(self):
        for field in ("continue-on-error: true", "env: {SKIP_SECURITY: '1'}",
                      "defaults: {run: {shell: 'bash {0}'}}"):
            mutant = self.text.replace("  suites:\n", f"  suites:\n    {field}\n", 1)
            self.assertIn("retained suites job policy changed",
                          gate.validate(mutant, self.contract))
        mutant = self.text.replace("\njobs:\n", "\nenv: {SKIP_SECURITY: '1'}\n\njobs:\n", 1)
        self.assertIn("retained workflow defaults or environment changed",
                      gate.validate(mutant, self.contract))

    def test_quoted_leading_job_cannot_hide_a_removed_producer(self):
        for quote in ('"', "'"):
            rogue = (f"  {quote}quoted-first{quote}:\n    steps:\n"
                     "      - name: hidden duplicate\n"
                     "        run: bash test_sibling_guard.sh\n")
            mutant = self.text.replace("\njobs:\n", "\njobs:\n" + rogue, 1)
            errors = gate.validate(mutant, self.contract)
            self.assertTrue(any("unrecognised job key" in error for error in errors), errors)

    def test_absolute_portable_input_invocations_are_forbidden(self):
        for prefix in ("$GITHUB_WORKSPACE/", "${GITHUB_WORKSPACE}/", "/checkout/", "./"):
            rogue = ("  duplicate-portable:\n    steps:\n"
                     "      - name: absolute duplicate\n"
                     f'        run: bash "{prefix}test_sibling_guard.sh"\n')
            errors = gate.validate(self.text.replace("\njobs:\n", "\njobs:\n" + rogue, 1),
                                   self.contract)
            self.assertIn("portable input reintroduced: test_sibling_guard.sh", errors)

    def test_all_manifest_mappings_are_bound_to_reviewed_contract(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR")) as directory:
            root = Path(directory)
            for name in ("vendor/toolkit-files.txt", "vendor/toolkit-pins.txt",
                         "vendor/toolkit-tracked-allowlist.txt", "scripts/sync-toolkit.sh"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gate.ROOT / name, root / name)
            manifest = root / "vendor/toolkit-files.txt"
            text = manifest.read_text()
            with mock.patch.object(gate, "ROOT", root):
                self.assertEqual(gate.validate(self.text, self.contract), [])
                manifest.write_text(text.replace("test_provisioning_policy.sh\n",
                                                 "test_provisioning_policy.sh|test_blocked_audit.sh\n", 1))
                self.assertNotEqual(text, manifest.read_text())
                self.assertIn("reviewed toolkit destination/source mappings changed",
                              gate.validate(self.text, self.contract))

    def test_warm_old_bytes_are_not_accepted_as_a_filename_sentinel(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR")) as directory:
            root = Path(directory)
            dest = "test_omniroute_rehearsal_translator.mjs"
            expected = b"reviewed v0.6.0 bytes\n"
            contract = {"restored_files_sha256": {dest: hashlib.sha256(expected).hexdigest()}}
            (root / dest).write_bytes(b"old v0.2.2 bytes\n")
            self.assertTrue((root / dest).exists())
            self.assertTrue(gate.restored_errors(root, contract))
            (root / dest).write_bytes(expected)
            self.assertEqual(gate.restored_errors(root, contract), [])
            (root / dest).unlink()
            self.assertTrue(gate.restored_errors(root, contract))

    def test_consolidated_input_bytes_are_checked_not_only_filenames(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR")) as directory:
            root = Path(directory)
            dest = "tracked-toolkit-input.py"
            expected = b"reviewed public source\n"
            contract = {"restored_files_sha256": {},
                        "consolidated_files_sha256": {
                            dest: hashlib.sha256(expected).hexdigest()}}
            self.assertTrue(gate.restored_errors(root, contract), "missing tracked input accepted")
            (root / dest).write_bytes(b"stale public source\n")
            self.assertTrue(gate.restored_errors(root, contract), "wrong tracked bytes accepted")
            (root / dest).write_bytes(expected)
            self.assertEqual(gate.restored_errors(root, contract), [])

    def test_consolidated_closure_cannot_omit_reclassify_or_duplicate_a_path(self):
        consolidated = self.contract.get("consolidated_files_sha256", {})
        self.assertTrue(consolidated, "consolidation must pin tracked source bytes")
        path = next(iter(consolidated))
        for mode in ("omit", "reclassify", "duplicate", "invalid-hash"):
            with self.subTest(mode=mode):
                contract = copy.deepcopy(self.contract)
                if mode in ("omit", "reclassify"):
                    digest = contract["consolidated_files_sha256"].pop(path)
                    if mode == "reclassify":
                        contract["restored_files_sha256"][path] = digest
                elif mode == "duplicate":
                    contract["restored_files_sha256"][path] = consolidated[path]
                else:
                    contract["consolidated_files_sha256"][path] = "not-a-sha256"
                self.assertTrue(gate.validate(self.text, contract))

    def test_removed_producers_require_an_immutable_input_closure(self):
        self.assertTrue(self.contract["removed"])
        pinned = (set(self.contract["restored_files_sha256"])
                  | set(self.contract["consolidated_files_sha256"]))
        for item in self.contract["removed"]:
            self.assertTrue(item["input_paths"])
            self.assertLessEqual(set(item["input_paths"]), pinned)
        for inputs in (None, [], ["ci_failure_drain.sh"], ["unknown-private-input.py"]):
            contract = copy.deepcopy(self.contract)
            contract["removed"][0]["input_paths"] = inputs
            errors = gate.validate(self.text, contract)
            self.assertTrue(any("immutable restored input closure" in e for e in errors), errors)

    def test_wrong_pin_fails(self):
        contract = copy.deepcopy(self.contract)
        contract["toolkit"]["commit"] = "0" * 40
        self.assertTrue(gate.validate(self.text, contract))

    def test_unreadable_workflow_fails(self):
        self.assertTrue(gate.validate("not a workflow", self.contract))


class OfflineContractProposeTest(unittest.TestCase):
    NEW_STEP = ("      - name: pin-proposal fixture probe\n"
                "        run: bash ./test_pin_proposal_fixture.sh\n")
    NEW_ID = "pin-proposal fixture probe:1"
    CHANGED_ID = "request-record integrity suite:1"

    @classmethod
    def setUpClass(cls):
        cls.text = (gate.ROOT / ".github/workflows/ci.yml").read_text()
        cls.contract = json.loads(gate.CONTRACT.read_text())

    def add_step_mutant(self):
        block = next(iter(
            gate.matched_blocks(self.text, self.contract["retained"])[0].values()))
        return self.text.replace(block, block + self.NEW_STEP, 1)

    def changed_step_mutant(self):
        blocks = gate.matched_blocks(self.text, self.contract["retained"])[0]
        block = blocks[self.CHANGED_ID]
        changed = block.rstrip("\n") + ("\n        env:\n"
                                        "          PIN_PROPOSAL_FIXTURE: '1'\n")
        self.assertNotEqual(changed, block)
        return self.text.replace(block, changed, 1)

    def rogue_input_mutant(self, step):
        block = next(iter(
            gate.matched_blocks(self.text, self.contract["retained"])[0].values()))
        rogue = ("      - name: renamed duplicate\n"
                 f"        run: bash ./{step['forbidden_inputs'][0]}\n")
        return self.text.replace(block, block + rogue, 1)

    def rogue_producer_mutant(self, step):
        block = next(iter(
            gate.matched_blocks(self.text, self.contract["retained"])[0].values()))
        rogue = ("      - name: renamed duplicate\n"
                 f"        working-directory: {step['working_directory']}\n"
                 "        run: |\n" +
                 textwrap.indent(step["private_command"], "          ") + "\n")
        return self.text.replace(block, block + rogue, 1)

    def run_gate(self, *argv):
        return subprocess.run(
            [sys.executable, str(gate.ROOT / "verification/offline_ci_contract.py"),
             *argv],
            capture_output=True, text=True, cwd=str(gate.ROOT))

    def test_propose_new_step_applies_cleanly(self):
        contract = copy.deepcopy(self.contract)
        proposed, notes, errors = gate.compute_proposal(
            self.add_step_mutant(), contract)
        self.assertEqual(errors, [])
        self.assertTrue(notes)
        self.assertEqual(gate.validate(self.add_step_mutant(), proposed), [])
        self.assertIn(self.NEW_ID, proposed["baseline_ids"])
        self.assertIn(self.NEW_ID,
                      [entry["id"] for entry in proposed["retained"]])
        self.assertEqual(proposed["suites_job_sha256"],
                         contract["suites_job_sha256"])
        self.assertEqual(proposed["workflow_defaults_sha256"],
                         contract["workflow_defaults_sha256"])
        self.assertEqual(contract, self.contract)

    def test_propose_output_ends_with_review_line(self):
        with tempfile.TemporaryDirectory(
                dir=os.environ.get("PAPERCLIP_SCRATCH_DIR")) as directory:
            workflow = Path(directory) / "ci.yml"
            contract_path = Path(directory) / "contract.json"
            workflow.write_text(self.add_step_mutant())
            contract_path.write_text(json.dumps(self.contract, indent=2))
            completed = self.run_gate("--propose", "--workflow", str(workflow),
                                      "--contract", str(contract_path))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            lines = completed.stdout.splitlines()
            self.assertTrue(lines)
            self.assertEqual(lines[-1], gate.REVIEW_LINE)
            self.assertIn(self.NEW_ID, completed.stdout)
            self.assertEqual(json.loads(contract_path.read_text()),
                             self.contract)

    def test_write_applies_proposal_without_touching_workflow(self):
        with tempfile.TemporaryDirectory(
                dir=os.environ.get("PAPERCLIP_SCRATCH_DIR")) as directory:
            workflow = Path(directory) / "ci.yml"
            contract_path = Path(directory) / "contract.json"
            mutant = self.add_step_mutant()
            workflow.write_text(mutant)
            contract_path.write_text(json.dumps(self.contract, indent=2))
            before = hashlib.sha256(gate.CONTRACT.read_bytes()).hexdigest()
            completed = self.run_gate("--write", "--workflow", str(workflow),
                                      "--contract", str(contract_path))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.splitlines()[-1], gate.REVIEW_LINE)
            self.assertEqual(
                gate.validate(mutant,
                              json.loads(contract_path.read_text())), [])
            self.assertEqual(workflow.read_text(), mutant)
            self.assertEqual(
                hashlib.sha256(gate.CONTRACT.read_bytes()).hexdigest(), before)

    def test_changed_step_refused_without_flag(self):
        contract = copy.deepcopy(self.contract)
        _, _, errors = gate.compute_proposal(self.changed_step_mutant(),
                                             contract)
        self.assertTrue(errors)
        self.assertTrue(any("--allow-changed" in error for error in errors),
                        errors)
        self.assertEqual(contract, self.contract)

    def test_changed_step_proposed_with_flag(self):
        contract = copy.deepcopy(self.contract)
        proposed, notes, errors = gate.compute_proposal(
            self.changed_step_mutant(), contract, [self.CHANGED_ID])
        self.assertEqual(errors, [])
        self.assertTrue(any(self.CHANGED_ID in note for note in notes), notes)
        self.assertEqual(gate.validate(self.changed_step_mutant(), proposed),
                         [])
        self.assertEqual(proposed["baseline_ids"], contract["baseline_ids"])

    def test_removed_producer_never_proposed(self):
        self.assertTrue(self.contract["removed"])
        for step in self.contract["removed"]:
            mutants = [self.rogue_producer_mutant(step)]
            if step["forbidden_inputs"]:
                mutants.append(self.rogue_input_mutant(step))
            for mutant in mutants:
                with self.subTest(step=step["id"]):
                    contract = copy.deepcopy(self.contract)
                    proposed, _, errors = gate.compute_proposal(
                        mutant, contract)
                    self.assertIsNone(proposed)
                    self.assertTrue(errors, mutant)
                    self.assertTrue(any("removed" in error for error in errors),
                                    errors)
                    self.assertEqual(contract, self.contract)

    def test_removed_refusal_on_command_line(self):
        with tempfile.TemporaryDirectory(
                dir=os.environ.get("PAPERCLIP_SCRATCH_DIR")) as directory:
            workflow = Path(directory) / "ci.yml"
            contract_path = Path(directory) / "contract.json"
            workflow.write_text(
                self.rogue_input_mutant(self.contract["removed"][0]))
            contract_path.write_text(json.dumps(self.contract, indent=2))
            completed = self.run_gate("--propose", "--workflow", str(workflow),
                                      "--contract", str(contract_path))
            self.assertEqual(completed.returncode, 1, completed.stdout)
            self.assertIn("removed", completed.stdout)

    def test_nothing_to_do_is_empty_and_zero(self):
        proposed, notes, errors = gate.compute_proposal(
            self.text, copy.deepcopy(self.contract))
        self.assertIsNone(proposed)
        self.assertEqual(notes, [])
        self.assertEqual(errors, [])
        completed = self.run_gate("--propose")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_unapproved_failure_names_propose(self):
        errors = gate.validate(self.add_step_mutant(), self.contract)
        self.assertTrue(any("--propose" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
