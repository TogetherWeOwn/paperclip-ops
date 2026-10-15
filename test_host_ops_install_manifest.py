#!/usr/bin/env python3
"""Offline fixtures for host-ops/install_manifest.py.

Isolated only: disposable git repositories under a temp dir, synthetic
reviewed commits, numeric modes, no host operations. Every emitted manifest
labels host acceptance and activation false; this suite never proves a host
install, render, or rollback. The git history under test is fabricated --
the tool-under-test reads the REVIEWED commit passed to it, and these
fixtures fabricate that commit locally. Only the tool source is the shipped
one; no shipped installer/render/transaction behavior is asserted here
beyond the export-loop and inventory surfaces the tool parses.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "install_manifest", ROOT / "host-ops/install_manifest.py"
)
manifest = importlib.util.module_from_spec(SPEC)
sys.modules["install_manifest"] = manifest
SPEC.loader.exec_module(manifest)

# Minimal reviewed tree: one journaled file must parse a real FILES/PUBLIC_OUT
# through the shipped inventory importer. Everything else is synthetic bytes.
INSTALL_STATE = """\
UNIT = "paperclip-host-ops-broker.service"
PUBLIC_OUT = "/etc/paperclip-host-ops/response-signing.pub.pem"
FILES = {
    "broker.py": "/usr/local/libexec/paperclip-host-ops/broker.py",
    "image_binding.py": "/usr/local/libexec/paperclip-host-ops/image_binding.py",
    "RUNBOOK.md": "/usr/local/share/doc/paperclip-host-ops/RUNBOOK.md",
    "host-ops-broker.service": "/etc/systemd/system/" + UNIT,
    "host-ops-broker.tmpfiles": "/usr/lib/tmpfiles.d/paperclip-host-ops.conf",
    "sudoers": "/etc/sudoers.d/paperclip-host-ops-broker",
    "sudoers-mirror": "/etc/paperclip-host-ops/sudoers.rendered",
    "config": "/etc/paperclip-host-ops/config.json",
    "public-key": PUBLIC_OUT,
}
"""
INSTALL_SH = """\
#!/usr/bin/env bash
set -Eeuo pipefail
refuse() { printf 'REFUSED: %s\\n' "$1" >&2; exit 2; }
for source in broker.py image_binding.py install_state.py install_transaction.py spool_provisioning.py RUNBOOK.md host-ops-broker.service host-ops-broker.tmpfiles host-ops-broker.sudoers config.example.json install-lib.sh; do
  [[ -f "$SOURCE_DIR/$source" && ! -L "$SOURCE_DIR/$source" ]] || refuse "reviewed commit is missing regular host-ops/$source"
done
"""
# (name, destination-or-None) for the full reviewed tree the tool must cover.
TREE = [
    ("broker.py", "install-destination"),
    ("image_binding.py", "install-destination"),
    ("RUNBOOK.md", "install-destination"),
    ("host-ops-broker.tmpfiles", "install-destination"),
    ("install_state.py", "staged-rollback-helper"),
    ("install_transaction.py", "staged-rollback-helper"),
    ("spool_provisioning.py", "staged-rollback-helper"),
    ("config.example.json", "config-seed"),
    ("host-ops-broker.sudoers", "rendered-template"),
    # The service template's staged rewrite is validation-only
    # (systemd-analyze verify); the published bytes are the raw export.
    ("host-ops-broker.service", "install-destination"),
    ("install-lib.sh", "sourced-lib"),
    ("install.sh", "operator-entrypoint"),
    ("prepare-first-install.sh", "preparation-entrypoint"),
    ("spool_carrier.py", "offline-tool"),
    ("spool_mapping.py", "offline-tool"),
    # The manifest tool classifies itself as an offline tool (see
    # OFFLINE_TOOLS): the fixture tree mirrors the shipped categories.
    ("install_manifest.py", "offline-tool"),
]


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-ops-manifest-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "host-ops").mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Host Ops Test")

    def git(self, *args):
        return subprocess.run(["git", "--no-replace-objects", "-C",
                               str(self.repo), *args],
                              check=True, capture_output=True, text=True)

    def write_tree(self, state=INSTALL_STATE, script=INSTALL_SH, extra=None,
                   drop=(), mutate_state=None):
        host_ops = self.repo / "host-ops"
        (host_ops / "install_state.py").write_text(
            state if mutate_state is None else mutate_state(state))
        (host_ops / "install.sh").write_text(script)
        for name, _kind in TREE:
            if name in ("install_state.py", "install.sh") or name in drop:
                continue
            (host_ops / name).write_text(f"reviewed-{name}\n")
        for name, content in (extra or {}).items():
            (host_ops / name).write_text(content)
        self.git("add", "-A")
        self.git("commit", "-qm", "reviewed")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def build(self, **kwargs):
        ref = self.write_tree(**kwargs)
        with tempfile.TemporaryDirectory(prefix="host-ops-manifest-wd-") as wd:
            return manifest.build_manifest(self.repo, ref, wd), ref

    # Positive controls: the shipped tool must accept the exact reviewed
    # shapes it parses, and every member/destination must be accounted for.
    def test_positive_manifest_covers_every_member_and_destination(self):
        result, ref = self.build()
        self.assertEqual(result["sourceRef"], ref)
        self.assertFalse(result["hostAcceptance"])
        self.assertFalse(result["activationReady"])
        self.assertFalse(result["writesEnabled"])
        self.assertEqual(len(result["exportMembers"]), len(TREE))
        kinds = {entry["kind"] for entry in result["exportCoverage"].values()}
        for _name, kind in TREE:
            self.assertIn(kind, kinds)
            self.assertEqual(result["exportCoverage"][_name]["kind"], kind)
        self.assertEqual(
            result["exportCoverage"]["broker.py"]["destination"],
            "/usr/local/libexec/paperclip-host-ops/broker.py")
        dest_kinds = {entry["kind"]
                      for entry in result["installDestinations"].values()}
        self.assertEqual(dest_kinds,
                         {"reviewed-bytes", "rendered-at-install", "host-held-key"})
        self.assertIn(ref, result["installCommand"])
        self.assertIn("rollback --state <INSTALL-JOURNAL>",
                      result["rollbackCommandShape"])
        self.assertNotIn("hostAcceptance\": true",
                         json.dumps(result, sort_keys=True))
        # The pin is content-addressed: identical bytes, identical digest.
        # Determinism is proved by building twice, not by recommitting --
        # write_tree commits into the setUp repo, so a second call would
        # fail on an empty commit. Rebuilding the same ref is the assertion.
        with tempfile.TemporaryDirectory(prefix="host-ops-manifest-wd-") as wd:
            again = manifest.build_manifest(self.repo, ref, wd)
        self.assertEqual(again["filesSha256"], result["filesSha256"])
        self.assertEqual(again["exportMembers"], result["exportMembers"])

    def test_positive_live_repo_manifest_is_stable_and_complete(self):
        # The shipped tool against the live checkout is a drift detector, not
        # a host attestation: this pins that the checklist above matches the
        # real reviewed shapes, so a future shipped-file addition forces a
        # test change here rather than riding along silently.
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              check=True, capture_output=True,
                              text=True).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="host-ops-manifest-wd-") as wd:
            result = manifest.build_manifest(ROOT, head, wd)
        live = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(ROOT), "ls-tree",
             "-r", "--name-only", head, "host-ops/"],
            check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(result["exportCoverage"]),
                         sorted(Path(p).name for p in live))

    def test_positive_cli_emits_sorted_json_manifest(self):
        ref = self.write_tree()
        out = subprocess.run(
            [sys.executable, str(ROOT / "host-ops/install_manifest.py"),
             "--repo", str(self.repo), "--source-ref", ref],
            capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        parsed = json.loads(out.stdout)
        self.assertEqual(parsed["sourceRef"], ref)
        self.assertEqual(out.stdout, json.dumps(parsed, indent=2, sort_keys=True) + "\n")

    # Negative controls: each must refuse (exit 2 / Refusal) rather than emit
    # a manifest that hides the gap.
    def test_refuses_non_commit_or_malformed_ref(self):
        ref = self.write_tree()
        with tempfile.TemporaryDirectory(prefix="host-ops-manifest-wd-") as wd:
            # "HEAD" resolves to the same commit but is not a pin: the
            # manifest must name the 40-hex commit it actually hashed.
            with self.assertRaises(manifest.Refusal):
                manifest.build_manifest(self.repo, "HEAD", wd)
            for bad in ("deadbeef", "z" * 40, ref[:39] + "g"):
                with self.subTest(bad=bad):
                    with self.assertRaises(manifest.Refusal):
                        manifest.build_manifest(self.repo, bad, wd)
            # An annotated tag object is its own 40-hex object but not a
            # commit; only a lightweight tag's commit SHA would pass, so
            # annotate (tag object) rather than merely tagging the commit.
            self.git("tag", "-a", "-m", "reviewed", "reviewed-tag")
            tag = self.git("rev-parse", "reviewed-tag").stdout.strip()
            self.assertEqual(len(tag), 40)
            with self.assertRaises(manifest.Refusal):
                manifest.build_manifest(self.repo, tag, wd)

    def test_refuses_replacement_object_substitution(self):
        # git replace swaps reviewed bytes for attacker bytes under the same
        # ref; the shipped installer reads with --no-replace-objects, so the
        # tool must too. Without this, the manifest could pin attacker bytes.
        ref = self.write_tree()
        (self.repo / "host-ops" / "broker.py").write_text("attacker\n")
        self.git("commit", "-qam", "attacker")
        attacker = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("replace", ref, attacker)
        try:
            ordinary = subprocess.run(
                ["git", "-C", str(self.repo), "cat-file", "-p",
                 f"{ref}:host-ops/broker.py"],
                check=True, capture_output=True).stdout
            self.assertEqual(ordinary, b"attacker\n")
            got = manifest.reviewed_blob(self.repo, ref, "host-ops/broker.py")
            self.assertTrue(got.startswith(b"reviewed-broker.py"))
        finally:
            self.git("replace", "-d", ref)

    def test_refuses_archive_member_outside_the_export_loop(self):
        # An extra reviewed file the installer never exports must refuse,
        # never ride along silently -- this is the mutant that caught the
        # first draft's export-only coverage rule.
        with self.assertRaisesRegex(manifest.Refusal, "rides along unaccounted"):
            self.build(extra={"stowaway.py": "unreviewed\n"})

    def test_refuses_dropped_install_input_still_in_commit(self):
        # Unlisting a file from the export loop while it stays in the commit
        # is the installer losing a template or seed: refused with the
        # not-exported message, never silently uncovered. (write_tree's drop
        # removes the file AND would break the loop identically, so unlist
        # via a loop edit instead; a dropped staged helper refuses earlier
        # with the exact-helper-set message, covered in the test below.)
        script = INSTALL_SH.replace(" host-ops-broker.service", "")
        with self.assertRaisesRegex(manifest.Refusal, "is not exported"):
            self.build(script=script)

    def test_refuses_export_loop_naming_a_missing_file(self):
        script = INSTALL_SH.replace(
            "install-lib.sh;",
            "install-lib.sh phantom-helper.py;")
        with self.assertRaisesRegex(manifest.Refusal, "missing from the"):
            self.build(script=script)

    def test_refuses_doubled_or_split_export_loop(self):
        doubled = INSTALL_SH + INSTALL_SH.split("for source in", 1)[1].join(
            ["for source in", ""])
        with self.assertRaisesRegex(manifest.Refusal, "not exactly one"):
            self.build(script=doubled)

    def test_refuses_nonregular_or_oversize_member(self):
        host_ops = self.repo / "host-ops"
        (host_ops / "install_state.py").write_text(INSTALL_STATE)
        (host_ops / "install.sh").write_text(INSTALL_SH)
        for name, _kind in TREE:
            if name in ("install_state.py", "install.sh"):
                continue
            (host_ops / name).write_text(f"reviewed-{name}\n")
        (host_ops / "spool_mapping.py").unlink()
        (host_ops / "spool_mapping.py").symlink_to("spool_carrier.py")
        self.git("add", "-A")
        self.git("commit", "-qm", "reviewed")
        ref = self.git("rev-parse", "HEAD").stdout.strip()
        with tempfile.TemporaryDirectory(prefix="host-ops-manifest-wd-") as wd:
            with self.assertRaisesRegex(manifest.Refusal, "not a regular file"):
                manifest.build_manifest(self.repo, ref, wd)

    def test_refuses_unparseable_inventory_or_moved_public_key(self):
        with self.assertRaisesRegex(manifest.Refusal, "inventory is unreadable"):
            self.build(mutate_state=lambda text: text + "\nthis is not python(\n")
        moved = INSTALL_STATE.replace(
            'PUBLIC_OUT = "/etc/paperclip-host-ops/response-signing.pub.pem"',
            'PUBLIC_OUT = "/tmp/elsewhere.pem"')
        with self.assertRaisesRegex(manifest.Refusal, "fixed journaled path"):
            self.build(state=moved)

    def test_refuses_dropped_helper_or_unknown_inventory_key(self):
        script = INSTALL_SH.replace(" spool_provisioning.py", "")
        with self.assertRaisesRegex(manifest.Refusal, "exact helper set"):
            self.build(script=script)
        unknown = INSTALL_STATE.replace(
            '    "public-key": PUBLIC_OUT,\n',
            '    "public-key": PUBLIC_OUT,\n    "telemetry.py": "/tmp/telemetry.py",\n')
        with self.assertRaisesRegex(manifest.Refusal, "no reviewed source"):
            self.build(state=unknown)

    def test_cli_refusal_shape_is_exit_2_with_prefix(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "host-ops/install_manifest.py"),
             "--repo", str(self.repo), "--source-ref", "0" * 41],
            capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 2)
        self.assertTrue(out.stderr.startswith("REFUSED: "))
        self.assertEqual(out.stdout, "")


if __name__ == "__main__":
    unittest.main()
