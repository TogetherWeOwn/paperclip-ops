import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garm.validate_build_manifest import (
    POSTGRES_PACKAGES,
    RUNNER_ENV_KEYS,
    InvalidManifest,
    load_runner_env,
)
from garm.validate_build_manifest import validate as validate_manifest
from garm.validate_canary_pool import MAX_BYTES as POOL_MAX_BYTES
from garm.validate_canary_pool import InvalidCanarySpec
from garm.validate_canary_pool import parse as parse_pool
from garm.validate_canary_pool import validate as validate_pool


def pool_fixture():
    return {
        "schema": "garm-canary-pool-spec.v1", "evidence_class": "synthetic",
        "role": "isolated-private", "host": "node575e13db",
        "provider": {"name": None, "garm_version": "0.2.1",
                     "provider_version": "0.1.3", "lxd_version": "5.21"},
        "pool": {"runner_prefix": "garm-iso",
                 "tags": ["self-hosted", "garm-managed"],
                 "forbidden_tags": ["two-ephemeral", "two-isolated",
                                    "two-selfhosted", "garm-node575e13db"],
                 "image_alias": None, "image_digest": None,
                 "flavor_profile": "garm-isolated-2x4x20",
                 "os_type": "linux", "os_arch": "amd64",
                 "min_idle_runners": 0, "max_runners": 1},
        "image_phases": {
            "phase_a_generic_private": "phase-a-generic-private-proves-pool-mechanics-only",
            "phase_b_pinned_isolated": "phase-b-pinned-isolated-proves-the-full-profile",
        },
        "missing_input_disposition": "HOLD; nulls are not pins and cannot be used to build or execute",
        "admission_authorized": False, "host_verified": False,
        "migration_complete": False, "installed": False,
        "status": "synthetic-preparation-only",
    }


def manifest_fixture(runner_env):
    return {
        "schema": "garm-isolated-build-manifest.v1",
        "evidence_class": "synthetic", "role": "isolated-private",
        "base": {"distribution": "ubuntu-24.04", "image_alias": None,
                 "base_image_fingerprint": None,
                 "packages_snapshot_sha256": None},
        "toolchain": {"node_series": "24", "node_version": None,
                      "postgres": {"flavor": "16",
                                   "packages": list(POSTGRES_PACKAGES),
                                   "versions": None},
                      "cc_present": True,
                      "runner_version": runner_env["RUNNER_VERSION"],
                      "runner_archive": runner_env["RUNNER_ARCHIVE"],
                      "runner_url": runner_env["RUNNER_URL"],
                      "runner_sha256": runner_env["RUNNER_SHA256"]},
        "runtime": {"user": "runner", "root": False, "sudo": False,
                    "login_shell": False},
        "docker": {"client_installed": False, "daemon_reachable": False,
                   "socket_present": False, "tcp_relay_reachable": False,
                   "docker_host_set": False},
        "resources": {"cpu": 2, "ram_mib": 4096, "disk_gib": 20,
                      "safe_max": 1, "configured_max": 2},
        "cloud_init_sha256": None,
        "output": {"output_image_fingerprint": None, "profile_sha256": None,
                   "inputs_sha256": None, "rebuild_receipt_present": False},
        "missing_input_disposition": "HOLD; nulls are not pins and cannot be used to build or execute",
        "admission_authorized": False, "host_verified": False,
        "migration_complete": False, "installed": False,
        "status": "synthetic-preparation-only",
    }


def repo_root():
    return Path(__file__).resolve().parent.parent


class CanaryPoolSpecTest(unittest.TestCase):
    def reject_change(self, path, value):
        spec = pool_fixture()
        target = spec
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
        with self.assertRaises(InvalidCanarySpec, msg=path):
            validate_pool(spec)

    def test_positive_control_never_authorizes(self):
        result = validate_pool(pool_fixture())
        self.assertEqual(result["result"], "offline_canary_spec_valid")
        for key in ("image_pinned", "admission_authorized", "host_verified",
                    "migration_complete"):
            self.assertIs(result[key], False)

    def test_checked_in_spec_matches_generator(self):
        path = Path(__file__).with_name("garm") / "canary-pool-spec.json"
        spec = parse_pool(path.read_bytes())
        self.assertEqual(spec["pool"], pool_fixture()["pool"])
        self.assertEqual(spec["provider"], pool_fixture()["provider"])
        self.assertEqual(spec["host"], "node575e13db")
        self.assertEqual(validate_pool(spec)["result"],
                         "offline_canary_spec_valid")

    def test_old_controller_only(self):
        for host in ("node7b1ff338", "coolify", "", "node8a3e959c"):
            with self.subTest(host=host):
                self.reject_change("host", host)

    def test_distinct_runner_prefix(self):
        for prefix in ("garm-", "garm-two", "garm-old", "static-runner", ""):
            with self.subTest(prefix=prefix):
                self.reject_change("pool.runner_prefix", prefix)

    def test_tag_containment(self):
        for tags, forbidden in [
            (["self-hosted", "two-ephemeral"],
             ["two-ephemeral", "two-isolated", "two-selfhosted"]),
            (["self-hosted"], ["two-ephemeral", "two-isolated",
                               "two-selfhosted"]),
            (["self-hosted", "garm-managed"], ["two-ephemeral"]),
            (["self-hosted", "garm-managed"],
             ["two-ephemeral", "two-ephemeral", "two-selfhosted",
              "two-isolated"]),
            (["self-hosted", "garm-managed"],
             ["two-ephemeral", "two-isolated", "garm-managed"]),
        ]:
            spec = pool_fixture()
            spec["pool"]["tags"] = tags
            spec["pool"]["forbidden_tags"] = forbidden
            with self.subTest(tags=tags, forbidden=forbidden):
                with self.assertRaises(InvalidCanarySpec):
                    validate_pool(spec)

    def test_live_image_pins_are_hold(self):
        for path, value in [
            ("pool.image_alias", "ubuntu:24.04"),
            ("pool.image_digest", "a" * 64),
            ("provider.name", "lxd_node575e13db"),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)

    def test_provider_versions_and_bounds(self):
        for path, value in [
            ("provider.garm_version", "latest"),
            ("provider.provider_version", "0.1.5"),
            ("provider.lxd_version", "unverified"),
            ("pool.min_idle_runners", 1),
            ("pool.max_runners", 2),
            ("pool.os_type", "windows"),
            ("pool.os_arch", "arm64"),
            ("pool.flavor_profile", "default"),
            ("role", "privileged-private"),
            ("evidence_class", "operator-recorded"),
            ("status", "live"),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)

    def test_image_phases_prose_required(self):
        for key in ("phase_a_generic_private", "phase_b_pinned_isolated"):
            spec = pool_fixture()
            spec["image_phases"][key] = ""
            with self.subTest(key=key):
                with self.assertRaises(InvalidCanarySpec):
                    validate_pool(spec)

    def test_missing_or_extra_fields(self):
        for section, content in pool_fixture().items():
            if type(content) is dict:
                for key in content:
                    with self.subTest(section=section, missing=key):
                        spec = pool_fixture()
                        del spec[section][key]
                        with self.assertRaises(InvalidCanarySpec):
                            validate_pool(spec)
                spec = pool_fixture()
                spec[section]["unexpected"] = "ignored?"
                with self.assertRaises(InvalidCanarySpec):
                    validate_pool(spec)
            spec = pool_fixture()
            del spec[section]
            with self.assertRaises(InvalidCanarySpec):
                validate_pool(spec)

    def test_json_parser_fail_closed(self):
        for raw in (b'{"schema":1,"schema":2}', b'{"a":NaN}',
                    b'{"a":Infinity}', b"not json", b"[" * 2000,
                    b"x" * (POOL_MAX_BYTES + 1), b"\xff"):
            with self.subTest(raw_size=len(raw)), self.assertRaises(InvalidCanarySpec):
                parse_pool(raw)
        self.assertEqual(parse_pool(json.dumps(pool_fixture()).encode()),
                         pool_fixture())

    def test_cli_exit_status_and_no_raw_input_echo(self):
        script = str(Path(__file__).with_name("garm") / "validate_canary_pool.py")
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
            "PAPERCLIP_SCRATCH_DIR")
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            receipt = Path(directory) / "spec.json"
            receipt.write_text(json.dumps(pool_fixture()))
            result = subprocess.run([sys.executable, script, str(receipt)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["admission_authorized"])
            bad = pool_fixture()
            bad["unexpected"] = "SENTINEL_DO_NOT_ECHO"
            receipt.write_text(json.dumps(bad))
            result = subprocess.run([sys.executable, script, str(receipt)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("SENTINEL_DO_NOT_ECHO", result.stdout + result.stderr)


class BuildManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = load_runner_env(repo_root() / "github-runner"
                                  / "actions-runner.env")

    def reject_change(self, path, value):
        manifest = manifest_fixture(self.env)
        target = manifest
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
        with self.assertRaises(InvalidManifest, msg=path):
            validate_manifest(manifest, self.env)

    def test_positive_control_never_authorizes(self):
        result = validate_manifest(manifest_fixture(self.env), self.env)
        self.assertEqual(result["result"], "offline_build_manifest_valid")
        for key in ("image_built", "admission_authorized", "host_verified",
                    "migration_complete"):
            self.assertIs(result[key], False)

    def test_checked_in_manifest_matches_runner_env(self):
        path = Path(__file__).with_name("garm") / "isolated-image-build-manifest.json"
        manifest = parse_pool(path.read_bytes())
        toolchain = manifest["toolchain"]
        for key in RUNNER_ENV_KEYS:
            field = {"RUNNER_VERSION": "runner_version",
                     "RUNNER_ARCHIVE": "runner_archive",
                     "RUNNER_URL": "runner_url",
                     "RUNNER_SHA256": "runner_sha256"}[key]
            self.assertEqual(toolchain[field], self.env[key], field)
        self.assertEqual(
            validate_manifest(manifest, self.env)["result"],
            "offline_build_manifest_valid")

    def test_runner_env_drift_rejected(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
            "PAPERCLIP_SCRATCH_DIR")
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            drifted = dict(self.env)
            drifted["RUNNER_VERSION"] = "9.9.9"
            env_path = Path(directory) / "actions-runner.env"
            env_path.write_text("".join(
                f"{key}={drifted[key]}\n" for key in RUNNER_ENV_KEYS))
            with self.assertRaises(InvalidManifest):
                validate_manifest(manifest_fixture(self.env),
                                  load_runner_env(env_path))

    def test_base_and_output_holds(self):
        for path, value in [
            ("base.image_alias", "ubuntu:24.04"),
            ("base.base_image_fingerprint", "a" * 64),
            ("base.packages_snapshot_sha256", "b" * 64),
            ("base.distribution", "ubuntu-22.04"),
            ("cloud_init_sha256", "c" * 64),
            ("output.output_image_fingerprint", "d" * 64),
            ("output.profile_sha256", "e" * 64),
            ("output.inputs_sha256", "f" * 64),
            ("output.rebuild_receipt_present", True),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)

    def test_toolchain_pins(self):
        for path, value in [
            ("toolchain.node_series", "20"),
            ("toolchain.node_version", "24.0.0"),
            ("toolchain.postgres.flavor", "15"),
            ("toolchain.postgres.packages", ["postgresql-16"]),
            ("toolchain.postgres.versions", {}),
            ("toolchain.cc_present", False),
            ("toolchain.runner_version", "latest"),
            ("toolchain.runner_archive", "runner.tar.gz"),
            ("toolchain.runner_url", "http://example.com/runner.tar.gz"),
            ("toolchain.runner_sha256", "UNKNOWN"),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)

    def test_privilege_and_docker_surface(self):
        for path, value in [
            ("runtime.user", "root"),
            ("runtime.root", True),
            ("runtime.sudo", True),
            ("runtime.login_shell", True),
            ("docker.client_installed", True),
            ("docker.daemon_reachable", True),
            ("docker.socket_present", True),
            ("docker.tcp_relay_reachable", True),
            ("docker.docker_host_set", True),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)

    def test_resource_ceiling(self):
        for path, value in [
            ("resources.cpu", 4), ("resources.ram_mib", 8192),
            ("resources.disk_gib", 40), ("resources.safe_max", 2),
            ("resources.configured_max", 3),
        ]:
            with self.subTest(path=path):
                self.reject_change(path, value)

    def test_missing_or_extra_fields(self):
        for section, content in manifest_fixture(self.env).items():
            if type(content) is dict:
                for key in content:
                    with self.subTest(section=section, missing=key):
                        manifest = manifest_fixture(self.env)
                        del manifest[section][key]
                        with self.assertRaises(InvalidManifest):
                            validate_manifest(manifest, self.env)
                manifest = manifest_fixture(self.env)
                manifest[section]["unexpected"] = "ignored?"
                with self.assertRaises(InvalidManifest):
                    validate_manifest(manifest, self.env)
            manifest = manifest_fixture(self.env)
            del manifest[section]
            with self.assertRaises(InvalidManifest):
                validate_manifest(manifest, self.env)

    def test_json_parser_fail_closed(self):
        from garm.validate_build_manifest import MAX_BYTES, parse
        for raw in (b'{"schema":1,"schema":2}', b'{"a":NaN}',
                    b'{"a":Infinity}', b"not json", b"[" * 2000,
                    b"x" * (MAX_BYTES + 1), b"\xff"):
            with self.subTest(raw_size=len(raw)), self.assertRaises(InvalidManifest):
                parse(raw)
        fixture = manifest_fixture(self.env)
        self.assertEqual(parse(json.dumps(fixture).encode()), fixture)

    def test_cli_exit_status_and_no_raw_input_echo(self):
        script = str(Path(__file__).with_name("garm") / "validate_build_manifest.py")
        env_path = str(repo_root() / "github-runner" / "actions-runner.env")
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
            "PAPERCLIP_SCRATCH_DIR")
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            receipt = Path(directory) / "manifest.json"
            receipt.write_text(json.dumps(manifest_fixture(self.env)))
            result = subprocess.run(
                [sys.executable, script, str(receipt),
                 "--runner-env", env_path],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["admission_authorized"])
            # Default --runner-env resolves to the checked-in pins.
            defaulted = subprocess.run(
                [sys.executable, script, str(receipt)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(defaulted.returncode, 0, defaulted.stderr)
            bad = manifest_fixture(self.env)
            bad["unexpected"] = "SENTINEL_DO_NOT_ECHO"
            receipt.write_text(json.dumps(bad))
            result = subprocess.run(
                [sys.executable, script, str(receipt),
                 "--runner-env", env_path],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("SENTINEL_DO_NOT_ECHO", result.stdout + result.stderr)

    def test_cross_role_manifest_pair_distinctness(self):
        garm = Path(__file__).with_name("garm")
        manifest = parse_pool(
            (garm / "isolated-image-build-manifest.json").read_bytes())
        self.assertEqual(manifest["role"], "isolated-private")
        self.assertFalse(manifest["toolchain"].get("php_provides", False))
        self.assertFalse(manifest["toolchain"].get("composer_provides", False))
        self.assertEqual(manifest["toolchain"]["postgres"]["flavor"], "16")
        self.assertIsNone(manifest["toolchain"]["node_version"])
        self.assertIsNone(manifest["toolchain"]["postgres"]["versions"])


if __name__ == "__main__":
    unittest.main()
