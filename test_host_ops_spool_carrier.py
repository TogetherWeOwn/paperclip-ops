#!/usr/bin/env python3
"""Offline carrier fixtures; no Podman render or host-permission evidence."""

import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "host-ops/spool_carrier.py"
SPEC = importlib.util.spec_from_file_location("spool_carrier", SCRIPT)
carrier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = carrier
SPEC.loader.exec_module(carrier)
# Alphabetic hex keeps the uppercase-rejection negative control meaningful.
COMPANY_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
COMPANY_B = "00000000-0000-4000-8000-000000000021"


class CarrierTests(unittest.TestCase):
    def setUp(self):
        self.template = carrier.TEMPLATE.read_bytes()
        self.result = carrier.render_candidate(self.template, COMPANY_A)

    def test_positive_control_exact_four_views(self):
        prefix = f"/var/spool/paperclip-host-ops/companies/{COMPANY_A}"
        self.assertEqual(self.result["mounts"], [
            {"source": f"{prefix}/requests", "destination": "/run/paperclip-host-ops/requests",
             "options": "rw,nosuid,nodev,noexec"},
            {"source": f"{prefix}/previews", "destination": "/run/paperclip-host-ops/previews",
             "options": "ro,nosuid,nodev,noexec"},
            {"source": f"{prefix}/responses", "destination": "/run/paperclip-host-ops/responses",
             "options": "ro,nosuid,nodev,noexec"},
            {"source": "/etc/paperclip-host-ops/response-signing.pub.pem",
             "destination": "/run/paperclip-host-ops/response-signing.pub.pem",
             "options": "ro,nosuid,nodev,noexec"},
        ])

    def test_emits_real_volume_entries_only_in_container(self):
        section = None
        actual = []
        for line in self.result["candidateQuadlet"].splitlines():
            if line.startswith("["):
                section = line
            if line.startswith("Volume=") and "/run/paperclip-host-ops/" in line:
                self.assertEqual(section, "[Container]")
                actual.append(line)
        self.assertEqual(actual, [mount.quadlet() for mount in carrier.company_mounts(COMPANY_A)])
        self.assertEqual(len(actual), 4)

    def test_preserves_all_original_lines_and_placeholders(self):
        original = self.template.decode("ascii")
        result = self.result["candidateQuadlet"]
        added = {mount.quadlet() for mount in carrier.company_mounts(COMPANY_A)}
        restored = "\n".join(line for line in result.splitlines()
                             if line not in added
                             and not line.startswith("# OFFLINE CANDIDATE ONLY:")
                             and not line.startswith("# rw is not write-only proof;")) + "\n"
        # The mount block contributes only one additional blank separator.
        self.assertEqual(restored.replace("\n\n\n[Service]", "\n\n[Service]"), original)
        for key in ("ReadOnly=true", "ReadOnlyTmpfs=false", "Network=none",
                    "NoNewPrivileges=true", "DropCapability=all", "User=@RUN_UID@:@RUN_GID@"):
            self.assertEqual(result.splitlines().count(key), 1)
        self.assertNotIn("[Install]", result.splitlines())
        self.assertNotIn("RestrictNamespaces", result)
        parsed = configparser.ConfigParser(interpolation=None, strict=False)
        parsed.read_string(result)
        self.assertEqual(parsed["Container"]["User"], "@RUN_UID@:@RUN_GID@")

    def test_distinct_company_outputs_and_no_global_spool_mount(self):
        other = carrier.render_candidate(self.template, COMPANY_B)
        a = {m["source"] for m in self.result["mounts"]}
        b = {m["source"] for m in other["mounts"]}
        self.assertEqual(a & b, {"/etc/paperclip-host-ops/response-signing.pub.pem"})
        self.assertNotIn(COMPANY_A, other["candidateQuadlet"])
        self.assertNotIn(COMPANY_B, self.result["candidateQuadlet"])
        forbidden = ("/keys", "/audit", "/state", "/processing", "/quarantine",
                     "/.staging", ".sock", "/var/lib/", "/var/log/")
        for mount in self.result["mounts"]:
            self.assertFalse(any(value in mount["source"] for value in forbidden))
            self.assertNotIn(mount["source"], ("/var/spool/paperclip-host-ops",
                                             "/var/spool/paperclip-host-ops/companies"))
            self.assertNotIn(mount["destination"], ("/run", "/run/paperclip-host-ops"))

    def test_no_automatic_relabel_chown_or_privilege_options(self):
        for mount in self.result["mounts"]:
            self.assertEqual(set(mount["options"].split(",")) - {"ro", "rw"},
                             {"nosuid", "nodev", "noexec"})
        text = self.result["candidateQuadlet"]
        self.assertNotIn("PodmanArgs=", text)
        self.assertNotIn("AddCapability=", text)
        self.assertNotIn("SecurityLabelDisable=", text)

    def test_source_drift_refuses_even_if_syntactically_valid(self):
        for changed in (self.template + b"\n", self.template.replace(b"ReadOnly=true", b"ReadOnly=false"),
                        self.template.replace(b"Network=none", b"Network=host"),
                        self.template.replace(b"DropCapability=all", b"AddCapability=all"),
                        self.template.replace(b"[Service]", b"Volume=/var:/leak:rw\n[Service]")):
            with self.subTest(changed=changed[:80]), self.assertRaises(carrier.Refusal):
                carrier.render_candidate(changed, COMPANY_A)

    def test_no_server_carrier_support(self):
        server = (ROOT / "deploy/paperclip-immutable/paperclip.container").read_bytes()
        with self.assertRaises(carrier.Refusal):
            carrier.render_candidate(server, COMPANY_A)

    def test_rejects_invalid_company_ids_and_injection(self):
        for company in (None, False, 123, "", COMPANY_A.upper(), COMPANY_A + "\n",
                        COMPANY_A + "/../" + COMPANY_B, "%h", "@COMPANY_ID@", ".", "..",
                        COMPANY_A + ":/root:rw", COMPANY_A + "\nVolume=/var:/leak:rw"):
            with self.subTest(company=company), self.assertRaises(carrier.Refusal):
                carrier.render_candidate(self.template, company)

    def test_metadata_cannot_claim_activation_or_permission_proof(self):
        self.assertFalse(self.result["activationReady"])
        self.assertFalse(self.result["hostAcceptance"])
        self.assertFalse(self.result["writeOnlyIngressProven"])
        self.assertFalse(self.result["companyIsolationProven"])
        self.assertEqual(self.result["kind"], "offline-company-spool-carrier")
        self.assertEqual(self.result["pending"], [
            "trusted-launcher-company-binding", "broker-company-routing", "journaled-provisioning",
            "observed-uid-gid-mapping", "requester-dac-probes", "host-generator-render",
            "exact-head-review-and-merge"])
        self.assertIn("shared-UID requesters are not isolated", self.result["candidateQuadlet"])

    def test_candidate_and_input_hashes_bind_exact_bytes(self):
        self.assertEqual(self.result["canonicalTemplateSha256"], hashlib.sha256(self.template).hexdigest())
        self.assertEqual(self.result["candidateSha256"],
                         hashlib.sha256(self.result["candidateQuadlet"].encode()).hexdigest())
        self.assertEqual(self.result, carrier.render_candidate(self.template, COMPANY_A))
        self.assertNotEqual(self.result["candidateSha256"],
                            carrier.render_candidate(self.template, COMPANY_B)["candidateSha256"])

    def test_no_oversized_or_nonbytes_inputs(self):
        for value in (None, "[Container]", b"x" * (carrier.MAX_TEMPLATE_BYTES + 1)):
            with self.assertRaises(carrier.Refusal):
                carrier.render_candidate(value, COMPANY_A)

    def test_reader_refuses_symlink_fifo_and_oversized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.write_bytes(self.template)
            self.assertEqual(carrier.read_template(source), self.template)
            link, fifo, large = (root / name for name in ("link", "fifo", "large"))
            link.symlink_to(source)
            os.mkfifo(fifo)
            large.write_bytes(b"x" * (carrier.MAX_TEMPLATE_BYTES + 1))
            for path in (link, fifo, large, root):
                with self.subTest(path=path.name), self.assertRaises((OSError, carrier.Refusal)):
                    carrier.read_template(path)

    def test_cli_positive_and_refusal_controls(self):
        good = subprocess.run([sys.executable, str(SCRIPT), "--company-id", COMPANY_A],
                              capture_output=True, text=True, timeout=5)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout), self.result)
        bad = subprocess.run([sys.executable, str(SCRIPT), "--company-id", "../escape"],
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, "")
        self.assertNotIn("../escape", bad.stderr)

    def test_cli_has_no_install_activate_paths_or_approval_switches(self):
        for arg in ("--install", "--activate", "--host-acceptance", "--template", "--source-root",
                    "--writes-enabled", "--company-authorized"):
            result = subprocess.run([sys.executable, str(SCRIPT), "--company-id", COMPANY_A, arg],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")


class UnprivilegedDacCounterexamples(unittest.TestCase):
    """Real local syscalls, not mapped-host acceptance or broker-owner fixtures.

    Owner-mode 0300 models a non-enumerable writable directory. It deliberately
    does NOT pretend the test process is the deployed broker/requester group.
    These controls prevent calling an rw bind or no-list DAC "write-only".
    """

    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0, "DAC controls require a non-root test process")
        self.tmp = tempfile.TemporaryDirectory(prefix="spool-dac-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ingress = self.root / "ingress"
        self.ingress.mkdir(mode=0o700)
        self.addCleanup(self.ingress.chmod, 0o700)

    def test_no_list_does_not_deny_read_of_a_known_request_name(self):
        self.ingress.chmod(0o300)
        with self.assertRaises(PermissionError):
            os.listdir(self.ingress)
        path = self.ingress / "known.synthetic-request"
        path.write_bytes(b"synthetic request, no secrets")
        path.chmod(0o640)
        self.assertEqual(path.read_bytes(), b"synthetic request, no secrets")

    def test_second_same_uid_process_can_read_and_remove_known_request(self):
        self.ingress.chmod(0o1300)  # sticky, write/search, no enumeration
        path = self.ingress / "known.synthetic-request"
        path.write_bytes(b"synthetic request, no secrets")
        path.chmod(0o600)
        child = subprocess.run([sys.executable, "-c",
                                "from pathlib import Path; import os, sys; "
                                "p=Path(sys.argv[1]); "
                                "assert p.read_bytes()==b'synthetic request, no secrets'; "
                                "p.unlink(); print(os.geteuid())", str(path)],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), str(os.geteuid()))
        self.assertFalse(path.exists())
        with self.assertRaises(PermissionError):
            os.listdir(self.ingress)

    def test_owner_can_restore_read_access_to_own_write_only_file(self):
        self.ingress.chmod(0o300)
        path = self.ingress / "known.synthetic-request"
        path.write_bytes(b"synthetic request, no secrets")
        path.chmod(0o200)
        with self.assertRaises(PermissionError):
            path.read_bytes()
        path.chmod(0o600)
        self.assertEqual(path.read_bytes(), b"synthetic request, no secrets")


if __name__ == "__main__":
    unittest.main()
