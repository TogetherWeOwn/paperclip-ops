import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garm.check_image_provenance import (
    REF_POLICY,
    SCHEMA,
    InvalidProvenance,
    check,
    parse,
    read_json,
)

GARM_DIR = Path(__file__).with_name("garm")
MANIFEST_PATH = GARM_DIR / "image-digest-pins.json"
COVERED_DOCS = [
    "role-contracts.json",
    "isolated-image-profile.json",
    "fixture-privileged-private.json",
    "fixture-isolated-private.json",
    "fixture-isolated-profile.json",
]


def manifest_fixture():
    return {
        "schema": SCHEMA, "evidence_class": "synthetic",
        "ref_policy": REF_POLICY, "admission_authorized": False,
        "status": "synthetic-preparation-only",
        "missing_digest_disposition": "REFUSE",
        "covers": {"generic-role": "role-contracts.json",
                   "build-class": "isolated-image-profile.json"},
        "images": {
            "runner/test-a": {
                "digest": "a" * 64,
                "pinned_ref": "test-a@sha256:" + "a" * 64,
                "role": "isolated-private",
                "profile": "role-contracts.json",
                "provenance": {"attested": False, "predicate": None,
                               "disposition": "HOLD"},
            },
        },
    }


def doc_with(digest_hex="a" * 64, extra=None):
    doc = {"schema": "test-doc", "evidence_class": "synthetic",
           "image": {"fingerprint": digest_hex}}
    if extra:
        doc.update(extra)
    return doc


def covered_pair(manifest=None):
    manifest = manifest or manifest_fixture()
    docs = [("role-contracts.json",
             {"image": {"fingerprint": "a" * 64}}),
            ("isolated-image-profile.json",
             {"image": {"fingerprint": "a" * 64}})]
    return manifest, docs


class ImageProvenanceTest(unittest.TestCase):
    def test_positive_control_never_authorizes(self):
        manifest, docs = covered_pair()
        result = check(manifest, docs)
        self.assertEqual(result["result"], "provenance_check_pass")
        for key in ("admission_authorized", "host_verified",
                    "migration_complete"):
            self.assertIs(result[key], False)

    def test_checked_in_manifest_matches_generator_shape(self):
        manifest = parse(MANIFEST_PATH.read_bytes())
        self.assertEqual(manifest["schema"], SCHEMA)
        self.assertEqual(manifest["ref_policy"], REF_POLICY)
        self.assertIs(manifest["admission_authorized"], False)

    def test_checked_in_docs_pass(self):
        manifest = parse(MANIFEST_PATH.read_bytes())
        docs = [(name, parse((GARM_DIR / name).read_bytes()))
                for name in COVERED_DOCS]
        self.assertEqual(check(manifest, docs)["result"],
                         "provenance_check_pass")

    def test_manifest_pins_match_checked_in_fixture_trios(self):
        manifest = parse(MANIFEST_PATH.read_bytes())
        pins = manifest["images"]
        for fixture, key in (("fixture-privileged-private.json",
                              "runner/privileged-private"),
                             ("fixture-isolated-private.json",
                              "runner/isolated-private"),
                             ("fixture-isolated-profile.json",
                              "runner/isolated-profile-build-class")):
            receipt = parse((GARM_DIR / fixture).read_bytes())
            image = receipt["image"]
            self.assertEqual(pins[key]["digest"], image["fingerprint"])
            self.assertEqual(pins[key]["profile_sha256"],
                             image["profile_sha256"])
            self.assertEqual(pins[key]["inputs_sha256"],
                             image["inputs_sha256"])

    def test_mutable_tag_refused(self):
        manifest, _ = covered_pair()
        for bad in ("ubuntu:24.04",
                    "Ubuntu:24.04",
                    "REGISTRY.EXAMPLE.COM/Runner:Latest",
                    "registry.example.com/runner:latest",
                    "garm-runner:2.999.0",
                    "img:24"):
            docs = [("role-contracts.json",
                     {"base_image": bad,
                      "image": {"fingerprint": "a" * 64}}),
                    ("isolated-image-profile.json",
                     {"image": {"fingerprint": "a" * 64}})]
            with self.assertRaises(InvalidProvenance, msg=bad):
                check(manifest, docs)

    def test_pinned_ref_with_unknown_digest_refused(self):
        manifest, _ = covered_pair()
        docs = [("role-contracts.json",
                 {"base_image": "test-a@sha256:" + "b" * 64,
                  "image": {"fingerprint": "a" * 64}}),
                ("isolated-image-profile.json",
                 {"image": {"fingerprint": "a" * 64}})]
        with self.assertRaises(InvalidProvenance):
            check(manifest, docs)

    def test_missing_digest_refused(self):
        manifest = manifest_fixture()
        docs = [("role-contracts.json", {"image": {}}),
                ("isolated-image-profile.json",
                 {"image": {"fingerprint": "a" * 64}})]
        with self.assertRaises(InvalidProvenance):
            check(manifest, docs)

    def test_unknown_fingerprint_refused(self):
        manifest, _ = covered_pair()
        docs = [("role-contracts.json",
                 {"image": {"fingerprint": "c" * 64}}),
                ("isolated-image-profile.json",
                 {"image": {"fingerprint": "a" * 64}})]
        with self.assertRaises(InvalidProvenance):
            check(manifest, docs)

    def test_live_attestation_claim_refused_despite_pinned_digest(self):
        manifest, _ = covered_pair()
        docs = [("role-contracts.json",
                 {"image": {"fingerprint": "a" * 64},
                  "provenance_attested": True}),
                ("isolated-image-profile.json",
                 {"image": {"fingerprint": "a" * 64}})]
        with self.assertRaises(InvalidProvenance):
            check(manifest, docs)

    def test_manifest_without_covered_profile_refused(self):
        manifest, _ = covered_pair()
        docs = [("role-contracts.json",
                 {"image": {"fingerprint": "a" * 64}})]
        with self.assertRaises(InvalidProvenance):
            check(manifest, docs)

    def test_manifest_mutable_policy_refused(self):
        manifest = manifest_fixture()
        manifest["ref_policy"] = "semver-tags-allowed"
        docs = [("role-contracts.json",
                 {"image": {"fingerprint": "a" * 64}}),
                ("isolated-image-profile.json",
                 {"image": {"fingerprint": "a" * 64}})]
        with self.assertRaises(InvalidProvenance):
            check(manifest, docs)

    def test_manifest_duplicate_pin_refused(self):
        manifest = manifest_fixture()
        manifest["images"]["runner/test-b"] = copy.deepcopy(
            manifest["images"]["runner/test-a"])
        docs = [("role-contracts.json",
                 {"image": {"fingerprint": "a" * 64}}),
                ("isolated-image-profile.json",
                 {"image": {"fingerprint": "a" * 64}})]
        with self.assertRaises(InvalidProvenance):
            check(manifest, docs)

    def test_timestamps_and_versions_are_not_image_tags(self):
        manifest, _ = covered_pair()
        docs = [("role-contracts.json",
                 {"at": "2026-10-03T03:30:00Z", "node": "24.0.0",
                  "image": {"fingerprint": "a" * 64}}),
                ("isolated-image-profile.json",
                 {"image": {"fingerprint": "a" * 64}})]
        self.assertEqual(check(manifest, docs)["result"],
                         "provenance_check_pass")

    def test_fail_closed_parser(self):
        with self.assertRaises(InvalidProvenance):
            parse(b"{invalid}")
        with self.assertRaises(InvalidProvenance):
            parse(b'{"a": 1, "a": 2}')
        with self.assertRaises(InvalidProvenance):
            parse(b'{"a": Infinity}')
        with self.assertRaises(InvalidProvenance):
            parse(b"x" * (1024 * 1024 + 1))

    def test_cli_exit_status_and_no_raw_value_echo(self):
        script = str(GARM_DIR / "check_image_provenance.py")
        secret_digest = "d" * 64
        manifest = manifest_fixture()
        manifest["images"]["runner/test-a"]["digest"] = secret_digest
        manifest["images"]["runner/test-a"]["pinned_ref"] = \
            "test-a@sha256:" + secret_digest
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            manifest_path = tmpdir / "pins.json"
            manifest_path.write_text(json.dumps(manifest))
            good = tmpdir / "good.json"
            good.write_text(json.dumps({
                "image": {"fingerprint": secret_digest}}))
            other = tmpdir / "other.json"
            other.write_text(json.dumps({
                "image": {"fingerprint": secret_digest}}))
            manifest["covers"] = {"a": "good.json", "b": "other.json"}
            manifest_path.write_text(json.dumps(manifest))
            result = subprocess.run(
                [sys.executable, script, str(manifest_path),
                 str(good), str(other)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("provenance_check_pass", result.stdout)
            bad = tmpdir / "good.json"
            bad.write_text(json.dumps({
                "base_image": "ubuntu:24.04",
                "image": {"fingerprint": secret_digest}}))
            result = subprocess.run(
                [sys.executable, script, str(manifest_path),
                 str(bad), str(other)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("provenance_check_rejected", result.stdout)
            self.assertNotIn(secret_digest, result.stdout)
            self.assertNotIn("ubuntu:24.04", result.stdout)

    def run_cli_case(self, expected, *, doc=None, path=(), value=None,
                     remove=False, manifest_path=()):
        manifest = parse(MANIFEST_PATH.read_bytes())
        docs = {name: parse((GARM_DIR / name).read_bytes())
                for name in COVERED_DOCS}
        if path or manifest_path:
            target = manifest if manifest_path else docs[doc]
            keys = manifest_path or path
            for key in keys[:-1]:
                target = target[key]
            if remove:
                del target[keys[-1]]
            else:
                target[keys[-1]] = value
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pins = root / "pins.json"
            pins.write_text(json.dumps(manifest))
            for name, payload in docs.items():
                (root / name).write_text(json.dumps(payload))
            result = subprocess.run(
                [sys.executable, "-B", str(GARM_DIR / "check_image_provenance.py"),
                 str(pins), *[str(root / name) for name in docs]],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload["admission_authorized"], False)
        self.assertEqual(payload["result"], "provenance_check_pass" if expected == 0
                         else "provenance_check_rejected")
        if type(value) is str and value and value != "false":
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_cli_known_pinned_references_pass(self):
        manifest = parse(MANIFEST_PATH.read_bytes())
        base = manifest["images"]["base/ubuntu-24.04"]["digest"]
        self.run_cli_case(0)
        for ref in ("ubuntu@sha256:" + base,
                    "ubuntu:24.04@sha256:" + base,
                    "my__runner@sha256:" + base,
                    "registry.example.com:5000/team/my__runner@sha256:" + base):
            with self.subTest(ref=ref):
                self.run_cli_case(0, doc="isolated-image-profile.json",
                                  path=("base_image",), value=ref)

    def test_cli_reference_policy_refuses_implicit_and_explicit_tags(self):
        for ref in ("ubuntu", "ubuntu:24.04", "my__runner:latest",
                    "registry.example.com:5000/team/my__runner:latest",
                    "Ubuntu:24.04", "REGISTRY.EXAMPLE.COM/Runner:Latest",
                    "https://registry.example.com/runner:latest",
                    "2026-10-03T03:30:00Z", "", None, 42):
            with self.subTest(ref=ref):
                self.run_cli_case(1, doc="isolated-image-profile.json",
                                  path=("base_image",), value=ref)
        manifest = parse(MANIFEST_PATH.read_bytes())
        pin = manifest["images"]["runner/isolated-private"]
        for non_image_digest in ("3" * 64, pin["profile_sha256"],
                                 pin["inputs_sha256"]):
            with self.subTest(non_image_digest=non_image_digest):
                self.run_cli_case(1, doc="isolated-image-profile.json",
                                  path=("base_image",),
                                  value="ubuntu@sha256:" + non_image_digest)

    def test_cli_all_actual_fingerprint_locations_refuse_unknown_or_missing(self):
        fields = [
            ("role-contracts.json", ("actual_build_inputs", "base_image_fingerprint")),
            ("role-contracts.json", ("actual_build_inputs", "output_image_fingerprint")),
            ("isolated-image-profile.json", ("base", "base_image_fingerprint")),
            ("isolated-image-profile.json", ("output", "output_image_fingerprint")),
            ("fixture-isolated-private.json", ("image", "fingerprint")),
            ("fixture-isolated-private.json", ("job", "image_fingerprint")),
            ("fixture-privileged-private.json", ("image", "fingerprint")),
            ("fixture-privileged-private.json", ("job", "image_fingerprint")),
            ("fixture-isolated-profile.json", ("image", "fingerprint")),
        ]
        manifest = parse(MANIFEST_PATH.read_bytes())
        pin = manifest["images"]["runner/isolated-private"]
        for doc, path in fields:
            for value in ("3" * 64, pin["profile_sha256"], pin["inputs_sha256"],
                          "invalid", 42, False, [], {}):
                with self.subTest(doc=doc, path=path, value=value):
                    self.run_cli_case(1, doc=doc, path=path, value=value)
            with self.subTest(doc=doc, path=path, missing=True):
                self.run_cli_case(1, doc=doc, path=path, remove=True)
            if doc.startswith("fixture-"):
                with self.subTest(doc=doc, path=path, null=True):
                    self.run_cli_case(1, doc=doc, path=path, value=None)

    def test_profile_nulls_require_explicit_synthetic_hold(self):
        manifest = parse(MANIFEST_PATH.read_bytes())
        for name in ("role-contracts.json", "isolated-image-profile.json"):
            for key, value in (("status", "ready"), ("admission_authorized", True),
                               ("missing_input_disposition", "build-ready")):
                doc = parse((GARM_DIR / name).read_bytes())
                doc[key] = value
                docs = [(target, doc if target == name else
                         parse((GARM_DIR / target).read_bytes()))
                        for target in COVERED_DOCS]
                with self.subTest(doc=name, key=key):
                    with self.assertRaises(InvalidProvenance):
                        check(manifest, docs)

    def test_cli_manifest_rejects_live_provenance(self):
        for key, value in (("attested", True), ("attested", "false"),
                           ("predicate", {"verified": True}),
                           ("disposition", "READY")):
            with self.subTest(key=key, value=value):
                self.run_cli_case(1, manifest_path=("images", "runner/isolated-private",
                                                   "provenance", key), value=value)

    def test_read_json_rejects_oversize_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.json"
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaises(InvalidProvenance):
                read_json(path)


if __name__ == "__main__":
    unittest.main()
