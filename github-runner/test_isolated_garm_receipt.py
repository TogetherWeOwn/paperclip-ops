import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from garm.validate_receipt import (
    FORBIDDEN_ACTIONS, InvalidReceipt, MAX_BYTES, ROLES, SCHEMA, parse, validate,
)

AT = "2026-10-03T03:30:00Z"


def fixture(role="privileged-private"):
    contract = ROLES[role]
    versions = {tool: "1.0.0" for tool in contract["tools"]}
    versions.update(runner="2.999.0", node="24.0.0", psql="16.0.0")
    if role == "privileged-private":
        versions.update(php="8.5.0", composer="2.0.0", docker="27.0.0",
                        docker_compose="2.0.0", mysqldump="8.0.0")
    fingerprints = {
        "privileged-private": ("a", "d", "e"),
        "isolated-private": ("f", "1", "2"),
    }
    fingerprint, profile, inputs = (value * 64 for value in fingerprints[role])
    job = {
        "run": "fixture-run-1", "runner": "garm-fixture-1", "vm": "fixture-vm-1",
        "host": "node7b1ff338", "pool": "fixture-node7b1ff338-pool", "provider": "fixture-provider",
        "image_fingerprint": fingerprint, "mapping_verified": True,
        "completed_at": "2026-10-03T03:27:00Z", "conclusion": "success",
        "artifacts_complete": True,
    }
    cleanup = {key: job[key] for key in ("run", "runner", "vm", "host", "pool", "provider")}
    cleanup.update(observed_at="2026-10-03T03:28:00Z", vm_absent=True, registration_absent=True)
    return {
        "schema": SCHEMA, "evidence_class": "synthetic", "role": role,
        "source": {"revision": "b" * 40, "bundle_sha256": "c" * 64},
        "target": {
            "host": "node7b1ff338", "pool": "fixture-node7b1ff338-pool", "provider": "fixture-provider",
            "garm_version": "0.2.1", "provider_version": "0.1.3", "lxd_version": "5.21",
            "selector": "fixture-node7b1ff338-exclusive", "inventory_complete": True,
            "enabled_pools": [
                {"pool": "fixture-node7b1ff338-pool", "host": "node7b1ff338", "provider": "fixture-provider",
                 "selectors": ["two-ephemeral", "fixture-node7b1ff338-exclusive"]},
                {"pool": "fixture-node575e13db-pool", "host": "node575e13db", "provider": "fixture-old-provider",
                 "selectors": ["two-ephemeral"]},
            ],
        },
        "image": {
            "fingerprint": fingerprint, "profile_sha256": profile, "inputs_sha256": inputs,
            "sudo": contract["sudo"], "docker": contract["docker"], "tool_versions": versions,
            "node24_runner_smoke": True, "rebuild_receipt_present": True,
        },
        "trust": {
            "repository": "TogetherWeOwn/fixture-private", "visibility": "private",
            "runner_group_verified": True, "trusted_ref_policy_verified": True,
            "public_pr_excluded": True, "required_checks_verified": True,
            "blocking_gate_unchanged": True, "cancel_in_progress": False,
            "db_target": "disposable-ci-service",
        },
        "budget": {
            "observed_at": "2026-10-03T03:25:00Z", "all_reservations_accounted": True,
            "pressure_clear": True,
            "filesystems": [{"mount": "/", "used_percent": 50}, {"mount": "/home", "used_percent": 60}],
            "configured_max": 2, "safe_max": 1, "active_vms": 0, "reserved_vms": 0, "requested_vms": 1,
            "available_cpu": 4, "requested_cpu": 2,
            "available_ram_mib": 8192, "requested_ram_mib": 4096,
            "available_disk_gib": 40, "requested_disk_gib": 20,
        },
        "probes": {name: {"result": "pass", "positive_control_passed": True}
                   for name in contract["probes"]},
        "job": job, "cleanup": cleanup,
        "actions": {name: False for name in FORBIDDEN_ACTIONS},
    }


class ReceiptTest(unittest.TestCase):
    def reject_change(self, path, value, role="privileged-private"):
        receipt = fixture(role)
        target = receipt
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
        with self.assertRaises(InvalidReceipt, msg=path):
            validate(receipt, AT)

    def test_both_positive_controls_never_authorize(self):
        for role in ROLES:
            with self.subTest(role=role):
                result = validate(fixture(role), AT)
                self.assertEqual(result["result"], "offline_contract_valid")
                for key in ("admission_authorized", "host_verified", "migration_complete"):
                    self.assertIs(result[key], False)

    def test_checked_in_fixture_pair_is_distinct_and_matches_generator(self):
        receipts = {}
        for role in ROLES:
            receipt = parse((Path(__file__).with_name("garm") / ("fixture-" + role + ".json")).read_bytes())
            self.assertEqual(receipt, fixture(role))
            self.assertEqual(validate(receipt, AT)["result"], "offline_contract_valid")
            receipts[role] = receipt
        privileged = receipts["privileged-private"]
        isolated = receipts["isolated-private"]
        for key in ("fingerprint", "profile_sha256", "inputs_sha256"):
            self.assertNotEqual(privileged["image"][key], isolated["image"][key])

    def test_role_contract_export_matches_checker_and_keeps_actual_inputs_unknown(self):
        contract = parse((Path(__file__).with_name("garm") / "role-contracts.json").read_bytes())
        self.assertEqual(set(contract["roles"]), set(ROLES))
        for name, role in ROLES.items():
            exported = contract["roles"][name]
            self.assertIs(exported["sudo"], role["sudo"])
            self.assertIs(exported["docker"], role["docker"])
            self.assertEqual(set(exported["required_tools"]), role["tools"])
            self.assertEqual(set(exported["required_probes_with_positive_controls"]), role["probes"])
        self.assertEqual(set(contract["forbidden_preparation_actions"]), FORBIDDEN_ACTIONS)
        self.assertIs(contract["installed"], False)
        self.assertIs(contract["admission_authorized"], False)
        self.assertIs(contract["source_baseline"]["verified_live"], False)
        self.assertTrue(contract["actual_build_inputs"])
        self.assertTrue(all(value is None for value in contract["actual_build_inputs"].values()))

    def test_privileged_contract_requires_compose_plugin_and_mysql_client(self):
        # case_39309424: an image built exactly to contract must not fail the kofra
        # container-lint (`docker compose`) and MySQL-gated (`mysqldump`) lanes.
        contract = parse((Path(__file__).with_name("garm") / "role-contracts.json").read_bytes())
        exported = contract["roles"]["privileged-private"]
        for tool in ("docker_compose", "mysqldump"):
            with self.subTest(tool=tool):
                self.assertIn(tool, exported["required_tools"])
                self.assertIn(tool, ROLES["privileged-private"]["tools"])
                self.reject_missing_tool(tool)
        for probe in ("compose_config", "mysql_client"):
            with self.subTest(probe=probe):
                self.assertIn(probe, exported["required_probes_with_positive_controls"])
                receipt = fixture()
                del receipt["probes"][probe]
                with self.assertRaises(InvalidReceipt):
                    validate(receipt, AT)
        # The no-Docker isolated role has no compose plugin or MySQL client to prove.
        for tool in ("docker_compose", "mysqldump"):
            self.assertNotIn(tool, ROLES["isolated-private"]["tools"])

    def reject_missing_tool(self, tool):
        receipt = fixture()
        del receipt["image"]["tool_versions"][tool]
        with self.assertRaises(InvalidReceipt, msg="missing " + tool):
            validate(receipt, AT)

    def test_compose_must_be_v2_and_versions_exact(self):
        for path, value in [
            ("image.tool_versions.docker_compose", "1.29.2"),
            ("image.tool_versions.docker_compose", "v2.29.1"),
            ("image.tool_versions.docker_compose", "2"),
            ("image.tool_versions.docker_compose", "latest"),
            ("image.tool_versions.mysqldump", "8"),
            ("image.tool_versions.mysqldump", "latest"),
            ("image.tool_versions.mysqldump", "UNKNOWN"),
        ]:
            with self.subTest(path=path, value=value):
                self.reject_change(path, value)

    def test_cross_role_image_reuse_is_not_proved_by_a_single_receipt(self):
        receipt = fixture("isolated-private")
        privileged = fixture()
        for key in ("fingerprint", "profile_sha256", "inputs_sha256"):
            receipt["image"][key] = privileged["image"][key]
        receipt["job"]["image_fingerprint"] = receipt["image"]["fingerprint"]
        result = validate(receipt, AT)
        self.assertEqual(result["result"], "offline_contract_valid")
        self.assertIs(result["host_verified"], False)
        self.assertIs(result["admission_authorized"], False)

    def test_semantic_version_and_org_guards_without_masking(self):
        self.reject_change("image.tool_versions.composer", "1.0.0")
        self.reject_change("trust.repository", "OtherOrg/fixture-private")

    def test_public_next_names_cannot_claim_private_visibility(self):
        for repo in ("two-web-next", "two-bot-next", "TWO-WEB-NEXT", "TWO-BOT-NEXT"):
            with self.subTest(repo=repo):
                self.reject_change("trust.repository", "TogetherWeOwn/" + repo)

    def test_duplicate_inventory_guards_without_masking(self):
        receipt = fixture()
        pools = receipt["target"]["enabled_pools"]
        pools.append(copy.deepcopy(pools[1]))
        # The duplicate is nonmatching; the exclusive-selector gate still passes.
        with self.assertRaisesRegex(InvalidReceipt, "duplicate pool identity"):
            validate(receipt, AT)
        receipt = fixture()
        filesystems = receipt["budget"]["filesystems"]
        filesystems.append(copy.deepcopy(filesystems[0]))
        with self.assertRaisesRegex(InvalidReceipt, "duplicate mount"):
            validate(receipt, AT)

    def test_job_identity_guards_without_cleanup_masking(self):
        changes = {"runner": "static-runner", "host": "node575e13db", "pool": "other-pool",
                   "provider": "other-provider"}
        for key, value in changes.items():
            receipt = fixture()
            receipt["job"][key] = receipt["cleanup"][key] = value
            reason = "job.runner" if key == "runner" else "job: target identity mismatch"
            with self.subTest(key=key), self.assertRaisesRegex(InvalidReceipt, reason):
                validate(receipt, AT)

    def test_ascii_clock_in_every_timestamp_position(self):
        # strptime accepts this mixed-digit year: rejection must come from our ASCII gate.
        mixed = "٢" + AT[1:]
        self.assertEqual(datetime.strptime(mixed, "%Y-%m-%dT%H:%M:%SZ").year, 2026)
        paths = ("evaluation time", "budget.observed_at", "job.completed_at", "cleanup.observed_at")
        for path in paths:
            receipt = fixture()
            if path == "evaluation time":
                value = AT
            else:
                section, key = path.split(".")
                value = receipt[section][key]
            for index, digit in enumerate(value):
                if digit not in "0123456789":
                    continue
                changed = value[:index] + chr(ord("٠") + int(digit)) + value[index + 1:]
                with self.subTest(path=path, index=index), self.assertRaises(InvalidReceipt):
                    if path == "evaluation time":
                        validate(receipt, changed)
                    else:
                        receipt[section][key] = changed
                        validate(receipt, AT)

    def test_json_value_error_is_structured_without_overriding_specific_rejections(self):
        with patch("garm.validate_receipt.json.loads", side_effect=ValueError("DO_NOT_ECHO")):
            with self.assertRaisesRegex(InvalidReceipt, "^JSON: malformed input$"):
                parse(b"{}")
        with self.assertRaisesRegex(InvalidReceipt, "^JSON: duplicate key$"):
            parse(b'{"a":1,"a":2}')
        with self.assertRaisesRegex(InvalidReceipt, "^JSON: non-finite number$"):
            parse(b'{"a":NaN}')

    def test_cli_large_integer_rejection_is_json_not_traceback(self):
        script = str(Path(__file__).with_name("garm") / "validate_receipt.py")
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            receipt = Path(directory) / "receipt.json"
            receipt.write_bytes(b'{"DO_NOT_ECHO":' + b"9" * 5000 + b"}")
            result = subprocess.run([sys.executable, script, str(receipt), "--at", AT],
                                    env={**os.environ, "PYTHONINTMAXSTRDIGITS": "4300"},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout), {
                "result": "offline_contract_rejected", "reason": "JSON: malformed input",
                "admission_authorized": False,
            })
            self.assertNotIn("DO_NOT_ECHO", result.stdout)

    def test_missing_or_extra_fields(self):
        for section, content in fixture().items():
            if type(content) is dict:
                for key in content:
                    with self.subTest(section=section, missing=key):
                        receipt = fixture()
                        del receipt[section][key]
                        with self.assertRaises(InvalidReceipt):
                            validate(receipt, AT)
                receipt = fixture()
                receipt[section]["unexpected"] = "ignored?"
                with self.assertRaises(InvalidReceipt):
                    validate(receipt, AT)
            receipt = fixture()
            del receipt[section]
            with self.assertRaises(InvalidReceipt):
                validate(receipt, AT)
        receipt = fixture()
        receipt["secret"] = "fixture-not-a-secret"
        with self.assertRaises(InvalidReceipt):
            validate(receipt, AT)

    def test_version_and_image_failures(self):
        for path, value in [
            ("schema", "other"), ("evidence_class", "operator-recorded"), ("role", "unknown"),
            ("role", []), ("source.revision", "main"), ("source.bundle_sha256", "UNKNOWN"),
            ("image.fingerprint", "alias"), ("image.inputs_sha256", "TBD"),
            ("image.profile_sha256", "floating"), ("image.node24_runner_smoke", False),
            ("image.rebuild_receipt_present", False), ("image.tool_versions.node", "20.0.0"),
            ("image.tool_versions.php", "8.4.0"), ("image.tool_versions.composer", "2"),
            ("image.tool_versions.runner", "latest"), ("target.provider_version", "0.1.5"),
            ("target.garm_version", "latest"), ("target.lxd_version", "unverified"),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)

    def test_selector_and_target_failures(self):
        for path, value in [
            ("target.host", "coolify"), ("target.pool", "wrong"), ("target.provider", "wrong"),
            ("target.selector", "two-ephemeral"), ("target.selector", "no-match"),
            ("target.inventory_complete", False), ("target.enabled_pools", []),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)
        for change in ("duplicate_pool", "wrong_matching_host", "duplicate_selector"):
            receipt = fixture()
            pools = receipt["target"]["enabled_pools"]
            if change == "duplicate_pool":
                pools.append(copy.deepcopy(pools[0]))
            elif change == "wrong_matching_host":
                pools[0]["host"] = "node575e13db"
            else:
                pools[0]["selectors"].append("fixture-node7b1ff338-exclusive")
            with self.subTest(change=change), self.assertRaises(InvalidReceipt):
                validate(receipt, AT)

    def test_budget_failures(self):
        for path, value in [
            ("budget.observed_at", "2026-10-03T02:00:00Z"),
            ("budget.observed_at", "2026-10-03T04:00:00Z"),
            ("budget.all_reservations_accounted", False), ("budget.pressure_clear", False),
            ("budget.configured_max", 3), ("budget.safe_max", 2), ("budget.safe_max", True),
            ("budget.active_vms", 1), ("budget.reserved_vms", 1), ("budget.requested_vms", 2),
            ("budget.requested_cpu", 5), ("budget.requested_ram_mib", 8193),
            ("budget.requested_disk_gib", 41), ("budget.available_cpu", True),
            ("budget.filesystems", []),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)
        for used in (95, 96, -1, True, "50", float("nan"), float("inf")):
            receipt = fixture()
            receipt["budget"]["filesystems"][0]["used_percent"] = used
            with self.subTest(used=used), self.assertRaises(InvalidReceipt):
                validate(receipt, AT)
        receipt = fixture()
        receipt["budget"]["filesystems"] = [{"mount": "/home", "used_percent": 50}]
        with self.assertRaises(InvalidReceipt):
            validate(receipt, AT)

    def test_trust_and_actions_failures(self):
        for path, value in [
            ("trust.visibility", "public"), ("trust.runner_group_verified", False),
            ("trust.trusted_ref_policy_verified", False), ("trust.public_pr_excluded", False),
            ("trust.required_checks_verified", False), ("trust.blocking_gate_unchanged", False),
            ("trust.cancel_in_progress", True), ("trust.db_target", "production"),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)
        for action in FORBIDDEN_ACTIONS:
            with self.subTest(action=action):
                self.reject_change("actions." + action, True)

    def test_isolated_role_and_positive_probe_failures(self):
        for path in ("image.sudo", "image.docker"):
            self.reject_change(path, True, role="isolated-private")
        for role in ROLES:
            for probe in ROLES[role]["probes"]:
                with self.subTest(role=role, probe=probe):
                    self.reject_change("probes." + probe + ".result", "unknown", role)
                    self.reject_change("probes." + probe + ".positive_control_passed", False, role)
                    self.reject_change("probes." + probe + ".positive_control_passed", 1, role)

    def test_job_and_cleanup_failures(self):
        for path, value in [
            ("job.runner", "static-runner"), ("job.host", "node575e13db"), ("job.pool", "wrong"),
            ("job.provider", "wrong"), ("job.image_fingerprint", "f" * 64),
            ("job.mapping_verified", False), ("job.artifacts_complete", False),
            ("job.conclusion", "cancelled"), ("job.completed_at", "2026-10-03T03:24:00Z"),
            ("job.completed_at", "2026-10-03T04:00:00Z"),
            ("cleanup.observed_at", "2026-10-03T03:26:00Z"),
            ("cleanup.observed_at", "2026-10-03T04:00:00Z"),
            ("cleanup.vm_absent", False), ("cleanup.registration_absent", False),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)
        for key in ("run", "runner", "vm", "host", "pool", "provider"):
            self.reject_change("cleanup." + key, "other")

    def test_json_parser_fail_closed(self):
        for raw in (b'{"schema":1,"schema":2}', b'{"a":NaN}', b'{"a":Infinity}',
                    b"not json", b"[" * 2000, b"x" * (MAX_BYTES + 1), b"\xff"):
            with self.subTest(raw_size=len(raw)), self.assertRaises(InvalidReceipt):
                parse(raw)
        self.assertEqual(parse(json.dumps(fixture()).encode()), fixture())

    def test_invalid_evaluation_time(self):
        for at in ("2026-10-03T03:30:00+00:00", "2026-02-30T03:30:00Z", "now"):
            with self.subTest(at=at), self.assertRaises(InvalidReceipt):
                validate(fixture(), at)

    def test_cli_exit_status_and_no_raw_input_echo(self):
        script = str(Path(__file__).with_name("garm") / "validate_receipt.py")
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            receipt = Path(directory) / "receipt.json"
            receipt.write_text(json.dumps(fixture()))
            result = subprocess.run([sys.executable, script, str(receipt), "--at", AT],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["admission_authorized"])
            r = fixture()
            r["unexpected"] = "SENTINEL_DO_NOT_ECHO"
            receipt.write_text(json.dumps(r))
            result = subprocess.run([sys.executable, script, str(receipt), "--at", AT],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("SENTINEL_DO_NOT_ECHO", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
