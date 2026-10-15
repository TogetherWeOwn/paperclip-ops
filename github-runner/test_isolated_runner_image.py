"""Offline pre-baked runner-image receipt tests; fabricated data only, never admission."""

import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garm.validate_runner_image import (
    InvalidReceipt,
    ROLE,
    SCHEMA,
    load_runner_env,
    parse,
    validate,
)
from garm import validate_runner_pins as pins_mod

GARM_DIR = Path(__file__).with_name("garm")
FIXTURE_PATH = GARM_DIR / "fixture-runner-image.json"
RUNNER_ENV_PATH = Path(__file__).with_name("actions-runner.env")
TIMEOUT_S = 10


def fixture_receipt():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def runner_env():
    return load_runner_env(RUNNER_ENV_PATH)


def mutate(path, value):
    doc = fixture_receipt()
    node = doc
    *parents, leaf = path
    for key in parents:
        node = node[key]
    node[leaf] = value
    return doc


def drop(*path):
    doc = fixture_receipt()
    node = doc
    *parents, leaf = path
    for key in parents:
        node = node[key]
    del node[leaf]
    return doc


def run_cli(doc_bytes):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        handle.write(doc_bytes)
        name = handle.name
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(GARM_DIR / "validate_runner_image.py"),
             name, "--runner-env", str(RUNNER_ENV_PATH)],
            capture_output=True, text=True, timeout=TIMEOUT_S)
    finally:
        Path(name).unlink(missing_ok=True)
    return proc


class FixtureConsistencyTest(unittest.TestCase):
    def test_positive_control_validates(self):
        result = validate(fixture_receipt(), runner_env())
        self.assertEqual(result["alias"], "garm-runner-20261006")
        self.assertFalse(result["admission_authorized"])
        self.assertFalse(result["host_verified"])
        self.assertFalse(result["migration_complete"])

    def test_fixture_matches_checked_in_text(self):
        raw = FIXTURE_PATH.read_bytes().decode("utf-8")
        self.assertEqual(parse(raw), fixture_receipt())

    def test_fixture_role_is_generic_private(self):
        self.assertEqual(fixture_receipt()["role"], ROLE)


class ReceiptRejectionTest(unittest.TestCase):
    def test_missing_mysqldump_red(self):
        doc = drop("tool_versions", "mysqldump")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_missing_docker_compose_red(self):
        doc = drop("tool_versions", "docker_compose")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_missing_gh_red(self):
        doc = drop("tool_versions", "gh")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_missing_go_red(self):
        doc = drop("tool_versions", "go")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_missing_govulncheck_red(self):
        doc = drop("tool_versions", "govulncheck")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_govulncheck_drift_red(self):
        doc = mutate(("tool_versions", "govulncheck"), "1.7.0")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_go_series_red(self):
        doc = mutate(("tool_versions", "go"), "1.22.0")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_compose_v1_red(self):
        doc = mutate(("tool_versions", "docker_compose"), "1.29.2")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_floating_tool_version_red(self):
        doc = mutate(("tool_versions", "docker"), "latest")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_runner_drift_from_env_red(self):
        doc = mutate(("tool_versions", "runner"), "2.336.0")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_wrong_role_red(self):
        doc = mutate(("role",), "isolated-private")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_mutable_alias_red(self):
        doc = mutate(("alias",), "garm-runner-current")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_alias_date_mismatch_red(self):
        doc = mutate(("alias",), "garm-runner-20261007")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_tcp_relay_red(self):
        doc = mutate(("docker", "tcp_relay"), True)
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_remote_docker_host_red(self):
        doc = mutate(("docker", "docker_host_set"), True)
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_registration_guarantee_weakened_red(self):
        doc = mutate(("guarantees", "no_registration"), False)
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_output_equal_to_base_red(self):
        doc = fixture_receipt()
        doc["output"]["fingerprint"] = doc["base"]["digest"]
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_live_admission_claim_red(self):
        doc = mutate(("admission_authorized",), True)
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_host_verified_claim_red(self):
        doc = mutate(("host_verified",), True)
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_malformed_clock_red(self):
        doc = mutate(("built_at",), "2026-10-06 15:30:00")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())

    def test_placeholder_digest_red(self):
        doc = mutate(("output", "fingerprint"), "UNKNOWN")
        with self.assertRaises(InvalidReceipt):
            validate(doc, runner_env())


class ParserHardeningTest(unittest.TestCase):
    def test_duplicate_key_rejected(self):
        raw = '{"a": 1, "a": 2}'
        with self.assertRaises(InvalidReceipt):
            parse(raw)

    def test_non_finite_rejected(self):
        with self.assertRaises(InvalidReceipt):
            parse('{"a": NaN}')

    def test_malformed_json_rejected(self):
        with self.assertRaises(InvalidReceipt):
            parse('{"a": ')

    def test_oversize_rejected(self):
        with self.assertRaises(InvalidReceipt):
            parse(" " * (1024 * 1024 + 1))

    def test_runner_env_drift_rejected(self):
        env = copy.deepcopy(runner_env())
        env["RUNNER_VERSION"] = "0.0.0"
        with self.assertRaises(InvalidReceipt):
            validate(fixture_receipt(), env)


def synthetic_pins(**overrides):
    env = runner_env()
    pins = {
        "schema": "garm-runner-image-pins.v1",
        "runner": {"version": env["RUNNER_VERSION"], "archive": env["RUNNER_ARCHIVE"],
                   "sha256": env["RUNNER_SHA256"]},
        "node_tarball": {"file": "node24-linux-x64.tar.gz", "sha256": "a" * 64},
        "go_tarball": {"file": "go1.26.6.linux-amd64.tar.gz", "sha256": "b" * 64},
        "keyrings": [], "apt_sources": [],
        "apt_packages": [{"name": "ca-certificates", "version": "20240203"}],
        "tool_versions": {"runner": env["RUNNER_VERSION"], "node": "24.12.0",
                          "php": "8.5.11", "composer": "2.9.3", "psql": "16.15",
                          "docker": "27.5.1", "docker_compose": "2.32.4",
                          "mysqldump": "8.0.42", "gh": "2.45.0",
                          "go": "1.26.6", "govulncheck": "1.8.0"},
    }
    pins.update(overrides)
    return pins


class PinsValidationTest(unittest.TestCase):
    def test_pins_positive_control(self):
        inputs_sha256 = pins_mod.validate(synthetic_pins(), runner_env())
        self.assertRegex(inputs_sha256, r"^[0-9a-f]{64}$")

    def test_pins_canonical_hash_stable(self):
        first = pins_mod.validate(synthetic_pins(), runner_env())
        second = pins_mod.validate(synthetic_pins(), runner_env())
        self.assertEqual(first, second)

    def test_pins_runner_drift_red(self):
        pins = synthetic_pins()
        pins["runner"]["version"] = "0.0.0"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_floating_tool_red(self):
        pins = synthetic_pins()
        pins["tool_versions"]["docker"] = "latest"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_compose_v1_red(self):
        pins = synthetic_pins()
        pins["tool_versions"]["docker_compose"] = "1.29.2"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_php83_series_red(self):
        pins = synthetic_pins()
        pins["tool_versions"]["php"] = "8.3.6"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_composer_v1_series_red(self):
        pins = synthetic_pins()
        pins["tool_versions"]["composer"] = "1.10.27"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_govulncheck_drift_red(self):
        pins = synthetic_pins()
        pins["tool_versions"]["govulncheck"] = "1.7.0"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_go_series_red(self):
        pins = synthetic_pins()
        pins["tool_versions"]["go"] = "1.22.0"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_missing_go_tarball_red(self):
        pins = synthetic_pins()
        del pins["go_tarball"]
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_unpinned_apt_red(self):
        pins = synthetic_pins()
        pins["apt_packages"] = [{"name": "docker-ce", "version": "latest"}]
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_secret_key_red(self):
        pins = synthetic_pins()
        pins["api_token"] = "value"
        with self.assertRaises(pins_mod.InvalidPins):
            pins_mod.validate(pins, runner_env())

    def test_pins_context_hash_mismatch_red(self):
        with tempfile.TemporaryDirectory() as context:
            with self.assertRaises(pins_mod.InvalidPins):
                pins_mod.validate(synthetic_pins(), runner_env(), context)

    def test_pins_context_match_pass(self):
        import hashlib
        with tempfile.TemporaryDirectory() as context:
            env = runner_env()
            runner_blob = b"synthetic-runner-tarball"
            node_blob = b"synthetic-node-tarball"
            go_blob = b"synthetic-go-tarball"
            Path(context, env["RUNNER_ARCHIVE"]).write_bytes(runner_blob)
            Path(context, "node24-linux-x64.tar.gz").write_bytes(node_blob)
            Path(context, "go1.26.6.linux-amd64.tar.gz").write_bytes(go_blob)
            pins = synthetic_pins()
            pins["runner"]["sha256"] = hashlib.sha256(runner_blob).hexdigest()
            pins["node_tarball"]["sha256"] = hashlib.sha256(node_blob).hexdigest()
            pins["go_tarball"]["sha256"] = hashlib.sha256(go_blob).hexdigest()
            drifted = copy.deepcopy(env)
            drifted["RUNNER_SHA256"] = pins["runner"]["sha256"]
            drifted["RUNNER_ARCHIVE"] = env["RUNNER_ARCHIVE"]
            pins["runner"]["version"] = drifted["RUNNER_VERSION"]
            pins["tool_versions"]["runner"] = drifted["RUNNER_VERSION"]
            inputs_sha256 = pins_mod.validate(pins, drifted, context)
            self.assertRegex(inputs_sha256, r"^[0-9a-f]{64}$")

    def test_pins_cli_pass(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(GARM_DIR / "validate_runner_pins.py"),
             "/dev/stdin", "--runner-env", str(RUNNER_ENV_PATH)],
            input=json.dumps(synthetic_pins()), capture_output=True,
            text=True, timeout=TIMEOUT_S)
        self.assertEqual(proc.returncode, 0)
        self.assertRegex(proc.stdout.strip(), r"^[0-9a-f]{64}$")

    def test_pins_cli_secret_refused_without_value_echo(self):
        pins = synthetic_pins(api_token="sekrit-value")
        proc = subprocess.run(
            [sys.executable, "-B", str(GARM_DIR / "validate_runner_pins.py"),
             "/dev/stdin", "--runner-env", str(RUNNER_ENV_PATH)],
            input=json.dumps(pins), capture_output=True,
            text=True, timeout=TIMEOUT_S)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("sekrit-value", proc.stdout + proc.stderr)


class CliTest(unittest.TestCase):
    def test_cli_pass_exit_zero(self):
        proc = run_cli(FIXTURE_PATH.read_bytes())
        self.assertEqual(proc.returncode, 0)
        body = json.loads(proc.stdout)
        self.assertEqual(body["alias"], "garm-runner-20261006")

    def test_cli_fail_exit_one_without_value_echo(self):
        doc = mutate(("output", "fingerprint"), "0" * 63 + "zz")
        proc = run_cli(json.dumps(doc).encode("utf-8"))
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("zz", proc.stdout + proc.stderr)

    def test_cli_missing_file_exit_one(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(GARM_DIR / "validate_runner_image.py"),
             str(GARM_DIR / "no-such-receipt.json"),
             "--runner-env", str(RUNNER_ENV_PATH)],
            capture_output=True, text=True, timeout=TIMEOUT_S)
        self.assertEqual(proc.returncode, 1)


BUILDER_PATH = GARM_DIR / "build-runner-image.sh"


class BuilderSetupContractTest(unittest.TestCase):
    def test_go_install_runs_from_runner_home_with_runner_home_env(self):
        builder = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn("cd '$RUN_HOME' && sudo -u '$RUN_USER' -H", builder)

    def test_go_tools_are_exposed_on_path(self):
        builder = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn("ln -s /opt/go/bin/go /usr/local/bin/go", builder)
        self.assertIn(
            "ln -s /home/runner/go/bin/govulncheck /usr/local/bin/govulncheck",
            builder)
        self.assertIn("command -v govulncheck", builder)

    def test_builder_refuses_preexisting_go_tree_before_creation(self):
        builder = BUILDER_PATH.read_text(encoding="utf-8")
        guard = 'if ! lxc exec "$BUILDER_NAME" -- test ! -e /opt/go; then'
        self.assertIn(guard, builder)
        self.assertLess(builder.index(guard),
                        builder.index('mkdir -p "$RUN_HOME" /opt/node24 /opt/go'))
        self.assertLess(builder.index(guard),
                        builder.index("tar -xzf /opt/stage-go.tar.gz"))

    def test_pinned_go_source_is_pruned_from_credential_scan(self):
        builder = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn("-path /opt/go/src", builder)
        self.assertIn('"*.pem"', builder)

    def test_credential_scan_fails_closed_and_checks_outside_go_source(self):
        builder = BUILDER_PATH.read_text(encoding="utf-8")
        start = builder.index("if ! IMG_CREDENTIAL_SCAN=")
        end = builder.index(
            '\nlxc exec "$BUILDER_NAME" -- rm -rf "$CTX_TMP"', start)
        block = builder[start:end]
        with tempfile.TemporaryDirectory(prefix="credential scan ") as tmp:
            root = Path(tmp)
            for name in ("home", "root", "opt", "bin"):
                (root / name).mkdir()
            go_source = root / "opt/go/src/crypto/x509"
            go_source.mkdir(parents=True)
            (go_source / "platform_root_key.pem").touch()
            lxc = root / "bin/lxc"
            lxc.write_text(
                "#!/bin/sh\n"
                '[ "${LXC_FAIL:-0}" = 1 ] && exit 41\n'
                "shift 3\n"
                'exec "$@"\n',
                encoding="utf-8")
            lxc.chmod(0o755)
            quote_start = block.index("bash -c '") + len("bash -c ")
            quote_end = block.index("' 2>/dev/null", quote_start)
            scan = block[quote_start + 1:quote_end]
            scan = scan.replace(
                "find /home /root /opt",
                "find %s %s %s" % tuple(shlex.quote(str(root / name))
                                          for name in ("home", "root", "opt")))
            scan = scan.replace(
                "-path /opt/node24",
                "-path %s" % shlex.quote(str(root / "opt/node24")))
            scan = scan.replace(
                "-path /opt/go/src",
                "-path %s" % shlex.quote(str(root / "opt/go/src")))
            block = block[:quote_start] + shlex.quote(scan) + block[quote_end + 1:]
            script = (
                """set -Eeuo pipefail
refuse() { printf 'REFUSED: %s\\n' "$1" >&2; exit 2; }
"""
                + block)
            env = dict(os.environ)
            env.pop("BASH_ENV", None)
            env["PATH"] = str(root / "bin") + os.pathsep + env["PATH"]
            env["BUILDER_NAME"] = "builder"
            env["CTX_TMP"] = "/unused"

            def run_scan(extra_env=None):
                test_env = dict(env)
                test_env.update(extra_env or {})
                return subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True,
                    timeout=TIMEOUT_S, env=test_env)

            clean = run_scan()
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertNotIn("platform_root_key.pem", clean.stdout)

            runner_cache = root / "home/runner/go/pkg/mod/example"
            runner_cache.mkdir(parents=True)
            (runner_cache / "unexpected.pem").touch()
            found = run_scan()
            self.assertEqual(found.returncode, 2)
            self.assertIn("credential artifacts present", found.stderr)
            self.assertNotIn("unexpected.pem", found.stdout + found.stderr)

            find = root / "bin/find"
            find.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
            find.chmod(0o755)
            failed_find = run_scan()
            self.assertEqual(failed_find.returncode, 2)
            self.assertIn("credential scan failed", failed_find.stderr)

            failed_lxc = run_scan({"LXC_FAIL": "1"})
            self.assertEqual(failed_lxc.returncode, 2)
            self.assertIn("credential scan failed", failed_lxc.stderr)


def extract_heredoc(marker):
    lines = BUILDER_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if "<<'" + marker + "'" in line)
    end = next(i for i, line in enumerate(lines)
               if line.rstrip("\n") == marker and i > start)
    return "".join(lines[start + 1:end])


def extract_govulncheck_module_record(raw):
    builder_line = next(
        (line for line in BUILDER_PATH.read_text(encoding="utf-8").splitlines()
         if line.startswith("IMG_RAW_GOVULNCHECK=")), None)
    awk_clause = "| awk '"
    if builder_line is None or awk_clause not in builder_line:
        raise AssertionError("builder must extract govulncheck module metadata")
    awk_start = builder_line.index(awk_clause) + len(awk_clause)
    awk_end = builder_line.find("')", awk_start)
    if awk_end < 0:
        raise AssertionError("builder awk extraction must be shell-quoted")
    proc = subprocess.run(
        ["awk", builder_line[awk_start:awk_end]], input=raw,
        capture_output=True, text=True, timeout=TIMEOUT_S)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def synthetic_raws(pins):
    tools = pins["tool_versions"]
    govulncheck_metadata = (
        "govulncheck: go1.26.6\n"
        "\tpath\tgolang.org/x/vuln/cmd/govulncheck\n"
        "\tmod\tgolang.org/x/vuln\tv%s\th1:synthetic"
        % tools["govulncheck"])
    return {
        "IMG_RAW_RUNNER": tools["runner"],
        "IMG_RAW_NODE": "v" + tools["node"],
        "IMG_RAW_PHP": "PHP %s (cli) (built: Jun  5 2025 12:00:00) (NTS)"
                       % tools["php"],
        "IMG_RAW_COMPOSER": "Composer version %s 2025-01-01" % tools["composer"],
        "IMG_RAW_PSQL": "psql (PostgreSQL) %s" % tools["psql"],
        "IMG_RAW_DOCKER": "Docker version %s, build synthetic" % tools["docker"],
        "IMG_RAW_COMPOSE": "Docker Compose version v%s"
                           % tools["docker_compose"],
        "IMG_RAW_MYSQLDUMP": "mysqldump  Ver %s for Linux on x86_64 (synthetic)"
                             % tools["mysqldump"],
        "IMG_RAW_GH": "gh version %s (2025-07-18 synthetic)"
                      % tools["gh"],
        "IMG_RAW_GO": "go version go%s linux/amd64" % tools["go"],
        "IMG_RAW_GOVULNCHECK": extract_govulncheck_module_record(
            govulncheck_metadata),
    }


def run_heredoc(marker, args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-B", "-"] + args,
        input=extract_heredoc(marker), capture_output=True,
        text=True, timeout=TIMEOUT_S, env=env)


class BuilderHeredocTest(unittest.TestCase):
    def test_measure_heredoc_pass_matches_pins(self):
        pins = synthetic_pins()
        with tempfile.TemporaryDirectory() as tmp:
            pins_path = Path(tmp, "pins.json")
            measured_path = Path(tmp, "measured.json")
            pins_path.write_text(json.dumps(pins), encoding="utf-8")
            proc = run_heredoc("MEASURE_EOF",
                               [str(pins_path), str(measured_path)],
                               synthetic_raws(pins))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            measured = json.loads(measured_path.read_text(encoding="utf-8"))
            self.assertEqual(measured, pins["tool_versions"])

    def test_measure_heredoc_drift_red(self):
        pins = synthetic_pins()
        raws = synthetic_raws(pins)
        raws["IMG_RAW_DOCKER"] = "Docker version 99.0.0, build synthetic"
        with tempfile.TemporaryDirectory() as tmp:
            pins_path = Path(tmp, "pins.json")
            measured_path = Path(tmp, "measured.json")
            pins_path.write_text(json.dumps(pins), encoding="utf-8")
            proc = run_heredoc("MEASURE_EOF",
                               [str(pins_path), str(measured_path)], raws)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(measured_path.exists())

    def test_receipt_heredoc_writes_validatable_receipt(self):
        pins = synthetic_pins()
        with tempfile.TemporaryDirectory() as tmp:
            pins_path = Path(tmp, "pins.json")
            measured_path = Path(tmp, "measured.json")
            out_path = Path(tmp, "receipt.json")
            pins_path.write_text(json.dumps(pins), encoding="utf-8")
            measure = run_heredoc("MEASURE_EOF",
                                  [str(pins_path), str(measured_path)],
                                  synthetic_raws(pins))
            self.assertEqual(measure.returncode, 0, measure.stderr)
            args = [str(out_path), "garm-runner-20261006",
                    "2026-10-06T15:30:00Z", "a" * 40, "b" * 64,
                    "c" * 64, "d" * 64, str(pins_path), "e" * 64,
                    "5.21.3", str(measured_path)]
            proc = run_heredoc("RECEIPT_EOF", args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(out_path.read_text(encoding="utf-8"))
            result = validate(receipt, runner_env())
            self.assertEqual(result["alias"], "garm-runner-20261006")
            self.assertEqual(receipt["tool_versions"], pins["tool_versions"])

    def test_receipt_heredoc_argc_drift_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp, "receipt.json")
            args = [str(out_path), "garm-runner-20261006",
                    "2026-10-06T15:30:00Z", "a" * 40, "b" * 64,
                    "c" * 64, "d" * 64, "/dev/null", "e" * 64, "5.21.3"]
            proc = run_heredoc("RECEIPT_EOF", args)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(out_path.exists())


if __name__ == "__main__":
    unittest.main()
