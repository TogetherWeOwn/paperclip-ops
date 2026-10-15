#!/usr/bin/env python3
"""Offline fixtures for host-ops/spool_provisioning.py.

Isolated only: private temp roots owned by os.getuid(), numeric fixture
identities, no account lookup, no host operations. Every output labels
host acceptance false; this suite never proves a host provisioned tree.
"""

import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "spool_provisioning", ROOT / "host-ops/spool_provisioning.py"
)
prov = importlib.util.module_from_spec(SPEC)
sys.modules["spool_provisioning"] = prov
SPEC.loader.exec_module(prov)

SPOOL = "/var/spool/paperclip-host-ops"
UID = os.getuid()
GID = os.getgid()
REQUESTS_GID = os.getgid()

# Strict RFC 4122 UUIDs so the mirror exercises the same canonical form as
# the reviewed broker contract (variant nibble 8/9/a/b, version nibble 1-5).
COMPANY_A = "00000000-0000-4000-8000-000000000003"
COMPANY_B = "00000000-0000-4000-8000-000000000020"


def entries(*company_ids):
    return [{"companyId": cid, "path": f"{SPOOL}/companies/{cid}"}
            for cid in company_ids]


def provision_tree(root, company_ids, companies_mode=0o700,
                   drop=(), widen=(), wrong_gid=()):
    base = Path(root) / SPOOL.lstrip("/")
    (base / "companies").mkdir(parents=True)
    os.chmod(base / "companies", companies_mode)
    for cid in company_ids:
        company_root = base / "companies" / cid
        for name, mode, _owner, group in prov.COMPANY_TREE:
            target = company_root if name == "." else company_root / name
            if (cid, name) in drop:
                continue
            target.mkdir(parents=True, exist_ok=True)
            use_mode = 0o755 if (cid, name) in widen else mode
            os.chmod(target, use_mode)
            gid = GID + 99999 if (cid, name) in wrong_gid else (
                REQUESTS_GID if group == prov.REQUESTS_GROUP else GID)
            try:
                os.chown(target, UID, gid)
            except OSError:
                pass


class ValidationMirrorTest(unittest.TestCase):
    def test_positive_control(self):
        self.assertEqual(prov.validate_company_spools(entries(COMPANY_A, COMPANY_B), SPOOL),
                         entries(COMPANY_A, COMPANY_B))

    def test_absent_is_empty(self):
        self.assertEqual(prov.validate_company_spools([], SPOOL), [])

    def test_rejects_not_a_list(self):
        for bad in (None, {}, "x", entries(COMPANY_A) * 65):
            with self.assertRaises(prov.Refusal):
                prov.validate_company_spools(bad, SPOOL)

    def test_rejects_entry_shape(self):
        for bad in ({"companyId": COMPANY_A},
                    {"companyId": COMPANY_A, "path": f"{SPOOL}/companies/{COMPANY_A}",
                     "mode": "0700"},
                    {"companyId": COMPANY_A, "path": 7}):
            with self.assertRaises(prov.Refusal):
                prov.validate_company_spools([bad], SPOOL)

    def test_rejects_non_canonical_id(self):
        for bad in ("COMPANY-A", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "11111111-1111-1111-1111-111111111111",
                    "11111111-1111-6111-8111-111111111111",
                    "11111111-1111-4111-c111-111111111111"):
            with self.assertRaises(prov.Refusal):
                prov.validate_company_spools(
                    [{"companyId": bad, "path": f"{SPOOL}/companies/{bad}"}], SPOOL)

    def test_rejects_path_mismatch(self):
        for bad_path in (f"{SPOOL}/companies/{COMPANY_B}",
                         f"{SPOOL}/{COMPANY_A}",
                         f"/other/spool/companies/{COMPANY_A}",
                         f"{SPOOL}/companies/{COMPANY_A}/../{COMPANY_A}"):
            with self.assertRaises(prov.Refusal):
                prov.validate_company_spools(
                    [{"companyId": COMPANY_A, "path": bad_path}], SPOOL)

    def test_rejects_duplicates(self):
        with self.assertRaises(prov.Refusal):
            prov.validate_company_spools(entries(COMPANY_A, COMPANY_A), SPOOL)

    def test_no_separate_duplicate_path_refusal(self):
        # Mirror-exactness with the reviewed broker: paths are exactly
        # parent/companyId and ids unique, so equal paths imply equal ids
        # (already refused). Two distinct ids cannot share a path because
        # each path embeds its own id — assert the constructive form.
        self.assertEqual(
            prov.validate_company_spools(entries(COMPANY_A, COMPANY_B), SPOOL),
            entries(COMPANY_A, COMPANY_B))


class FragmentTest(unittest.TestCase):
    def test_fragment_positive_control(self):
        fragment = prov.render_tmpfiles_fragment(SPOOL, entries(COMPANY_A))
        self.assertIn(f"d {SPOOL}/companies 0700 {prov.READER_ACCOUNT} "
                      f"{prov.READER_GROUP} -", fragment.splitlines())
        self.assertIn(f"d {SPOOL}/companies/{COMPANY_A}/requests 3730 "
                      f"{prov.READER_ACCOUNT} {prov.REQUESTS_GROUP} -",
                      fragment.splitlines())
        self.assertIn(f"d {SPOOL}/companies/{COMPANY_A}/.staging 0700 "
                      f"{prov.READER_ACCOUNT} {prov.READER_GROUP} -",
                      fragment.splitlines())
        self.assertIn("OFFLINE CANDIDATE ONLY", fragment)
        # Every company-tree row rendered once for one company: 3 header
        # lines, the companies/ guard, and one line per tree row.
        self.assertEqual(len(fragment.splitlines()),
                         4 + len(prov.COMPANY_TREE))

    def test_fragment_refuses_empty(self):
        with self.assertRaises(prov.Refusal):
            prov.render_tmpfiles_fragment(SPOOL, [])

    def test_fragment_mirrors_invalid_config(self):
        with self.assertRaises(prov.Refusal):
            prov.render_tmpfiles_fragment(SPOOL, [{"companyId": "nope",
                                                   "path": f"{SPOOL}/companies/nope"}])

    def test_two_companies_render_both(self):
        fragment = prov.render_tmpfiles_fragment(SPOOL, entries(COMPANY_A, COMPANY_B))
        self.assertIn(f"d {SPOOL}/companies/{COMPANY_B}/responses 2750 "
                      f"{prov.READER_ACCOUNT} {prov.REQUESTS_GROUP} -",
                      fragment.splitlines())


class TreeValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-ops-spool-prov-")
        self.addCleanup(self.tmp.cleanup)
        provision_tree(self.tmp.name, (COMPANY_A, COMPANY_B))

    def check(self, company_ids, **kwargs):
        return prov.validate_provisioned_tree(
            self.tmp.name, SPOOL, entries(*company_ids), UID, GID, REQUESTS_GID,
            **kwargs)

    def test_positive_control(self):
        self.assertEqual(self.check((COMPANY_A, COMPANY_B)),
                         entries(COMPANY_A, COMPANY_B))

    def test_widened_companies_refuses(self):
        # Binding check the broker cannot make: 0751 carries no write bit yet
        # collapses cross-company isolation under the shared requesting group.
        os.chmod(Path(self.tmp.name) / SPOOL.lstrip("/") / "companies", 0o751)
        with self.assertRaises(prov.Refusal):
            self.check((COMPANY_A,))

    def test_companies_0750_refuses(self):
        os.chmod(Path(self.tmp.name) / SPOOL.lstrip("/") / "companies", 0o750)
        with self.assertRaises(prov.Refusal):
            self.check((COMPANY_A,))

    def test_missing_company_root_refuses(self):
        import shutil
        shutil.rmtree(Path(self.tmp.name) / SPOOL.lstrip("/") / "companies" / COMPANY_B)
        with self.assertRaises(prov.Refusal):
            self.check((COMPANY_A, COMPANY_B))

    def test_missing_view_refuses(self):
        import shutil
        shutil.rmtree(Path(self.tmp.name) / SPOOL.lstrip("/") / "companies"
                      / COMPANY_A / "previews")
        with self.assertRaises(prov.Refusal):
            self.check((COMPANY_A,))

    def test_wrong_view_mode_refuses(self):
        os.chmod(Path(self.tmp.name) / SPOOL.lstrip("/") / "companies"
                 / COMPANY_A / "responses", 0o2751)
        with self.assertRaises(prov.Refusal):
            self.check((COMPANY_A,))

    def test_symlinked_view_refuses(self):
        target = (Path(self.tmp.name) / SPOOL.lstrip("/") / "companies"
                  / COMPANY_A / "quarantine")
        import shutil
        shutil.rmtree(target)
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        target.symlink_to(outside)
        with self.assertRaises(prov.Refusal):
            self.check((COMPANY_A,))

    def test_wrong_ownership_refuses(self):
        target = (Path(self.tmp.name) / SPOOL.lstrip("/") / "companies"
                  / COMPANY_A / ".staging")
        try:
            os.chown(target, UID, GID + 77777)
        except OSError as exc:
            self.skipTest(f"fixture cannot chown to a foreign gid: {exc!r}")
        if target.lstat().st_gid != GID + 77777:
            self.skipTest("foreign-gid chown did not take effect")
        with self.assertRaises(prov.Refusal):
            self.check((COMPANY_A,))

    def test_unconfigured_company_not_validated(self):
        # A directory with no configured entry is never listed: validating one
        # company must not touch the other company's tree.
        self.assertEqual(self.check((COMPANY_A,)), entries(COMPANY_A))


class CandidateCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-ops-spool-prov-cli-")
        self.addCleanup(self.tmp.cleanup)

    def write_config(self, payload):
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(payload))
        return str(path)

    def test_candidate_positive_control(self):
        config = self.write_config({"companySpools": entries(COMPANY_A)})
        out = subprocess.run([sys.executable, str(ROOT / "host-ops/spool_provisioning.py"),
                              "--config", config, "--spool-path", SPOOL],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual(result["kind"], "offline-company-spool-provisioning")
        self.assertFalse(result["hostAcceptance"])
        self.assertFalse(result["activationReady"])
        self.assertEqual(result["entries"], entries(COMPANY_A))

    def test_candidate_refuses_missing_key(self):
        config = self.write_config({"writesEnabled": False})
        out = subprocess.run([sys.executable, str(ROOT / "host-ops/spool_provisioning.py"),
                              "--config", config, "--spool-path", SPOOL],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 2)

    def test_candidate_refuses_invalid_config(self):
        config = self.write_config({"companySpools": [{"companyId": "x"}]})
        out = subprocess.run([sys.executable, str(ROOT / "host-ops/spool_provisioning.py"),
                              "--config", config, "--spool-path", SPOOL],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
