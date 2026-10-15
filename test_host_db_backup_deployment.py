#!/usr/bin/env python3
# case_b4863264: the bundle manifest is bundle-scoped (it lists systemd/* paths so
# the installer can verify the whole reviewed bundle in WORK_DIR), but the
# pre-fix installer copied it verbatim into the release directory, which holds
# only host_db_backup.sh + REVISION. The unit's ExecStartPre
# `sha256sum --check --strict` then failed on every start with exactly the 3
# FAILED-open-or-read lines the case_0931610e install attempt measured, so the
# timer could never run. The fix regenerates a release-scoped manifest from
# the verified bytes and proves the unit's own check passes before claiming
# INSTALLED. These tests pin both halves: the installer bytes, and the
# mechanism itself, executed against a real built bundle.

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SERVICE = ROOT / "systemd" / "paperclip-host-db-backup.service"
TIMER = ROOT / "systemd" / "paperclip-host-db-backup.timer"
INSTALLER = ROOT / "systemd" / "install-host-db-backup.sh"
BUILDER = ROOT / "systemd" / "build-host-db-backup-bundle.sh"


def installer_source() -> str:
    return INSTALLER.read_text()


def manifest_generation_line(source: str) -> str:
    # The one sha256sum invocation that WRITES a manifest (has a redirect),
    # as opposed to the two --check invocations that read one.
    lines = [
        line for line in source.splitlines()
        if "sha256sum " in line and " > " in line and "--check" not in line
    ]
    assert len(lines) == 1, f"expected one manifest-generation line, got {lines}"
    return lines[0]


class HostDbBackupDeploymentContract(unittest.TestCase):
    def test_installer_verifies_bundle_manifest_before_installing(self):
        # The regenerated release manifest is only as trustworthy as the bytes
        # it is generated from; the bundle-scope check in WORK_DIR is what
        # makes them the reviewed bytes. It must stay ahead of the install.
        source = installer_source()
        self.assertIn('(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)', source)
        check_at = source.index('(cd "$WORK_DIR"')
        release_at = source.index('RELEASE_DIR="/usr/local/libexec/paperclip-host-db-backup/$SOURCE_REF"')
        self.assertLess(check_at, release_at)

    def test_installer_does_not_copy_bundle_manifest_into_release(self):
        # The exact case_b4863264 defect: the bundle manifest lists systemd/*
        # paths that are never installed, so copying it into the release dir
        # breaks the unit's preflight permanently.
        source = installer_source()
        self.assertNotIn('"$WORK_DIR/REVISION" "$WORK_DIR/SHA256SUMS" "$RELEASE_DIR/"', source)
        self.assertNotIn('"$WORK_DIR/SHA256SUMS" "$RELEASE_DIR', source)

    def test_installer_regenerates_release_scoped_manifest(self):
        source = installer_source()
        line = manifest_generation_line(source)
        self.assertIn('(cd "$RELEASE_DIR"', line)
        self.assertIn("/usr/bin/sha256sum host_db_backup.sh REVISION", line)
        self.assertIn('"$WORK_DIR/SHA256SUMS.release"', line)
        self.assertIn('"$WORK_DIR/SHA256SUMS.release" "$RELEASE_DIR/SHA256SUMS"', source)

    def test_installer_proves_unit_preflight_before_claiming_installed(self):
        # The unit runs `sha256sum --check --strict` in the release dir
        # before every start; the installer must run that same check before
        # printing INSTALLED, so a broken release fails the install, not the
        # first timer tick weeks later.
        source = installer_source()
        release_check = '(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)'
        self.assertIn(release_check, source)
        self.assertLess(
            source.index(manifest_generation_line(source)),
            source.index(release_check),
        )
        self.assertLess(
            source.index(release_check),
            source.index("printf 'INSTALLED paperclip-host-db-backup"),
        )

    def test_release_manifest_covers_exactly_the_installed_byte_set(self):
        # The invariant case_b4863264 violated: every file installed into the
        # release dir must be covered by the regenerated manifest, and the
        # manifest must cover nothing else (a covered-but-absent path is the
        # FAILED-open-or-read the unit died on).
        source = installer_source()
        installed = set(re.findall(r'"\$RELEASE_DIR/([A-Za-z0-9_.\-]+)"', source))
        self.assertGreaterEqual(installed, {"host_db_backup.sh", "REVISION", "SHA256SUMS"})
        covered = manifest_generation_line(source).split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
        self.assertEqual(set(covered), installed - {"SHA256SUMS"})

    def test_unit_preflight_checks_installed_release_manifest(self):
        # The other end of the contract: the unit checks the manifest the
        # installer regenerates, in the directory the installer fills.
        source = SERVICE.read_text()
        self.assertIn(
            "WorkingDirectory=/usr/local/libexec/paperclip-host-db-backup/@SOURCE_REF@",
            source,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/sha256sum --check --strict "
            "/usr/local/libexec/paperclip-host-db-backup/@SOURCE_REF@/SHA256SUMS",
            source,
        )

    def test_bundle_manifest_still_covers_payload_units_and_installer(self):
        # The fix must narrow the RELEASE manifest, never the BUNDLE one:
        # shrinking the bundle manifest would "fix" the preflight by ceasing
        # to verify the reviewed unit files at all.
        source = BUILDER.read_text()
        for member in (
            "host_db_backup.sh",
            "systemd/install-host-db-backup.sh",
            "systemd/paperclip-host-db-backup.service",
            "systemd/paperclip-host-db-backup.timer",
        ):
            self.assertIn(member, source)
        self.assertIn('sha256sum "${files[@]}" REVISION > SHA256SUMS', source)

    def test_installer_only_requires_executables_it_actually_runs(self):
        # A preflight that refuses on a binary nothing ever invokes turns a
        # cosmetic path difference into a failed install (the case_d2b472ea runuser
        # lesson, mirrored from the liveness deployment suite).
        source = installer_source()
        service = SERVICE.read_text()
        timer = TIMER.read_text()
        required = re.findall(r"(?m)^for executable in (.+); do$", source)
        self.assertEqual(len(required), 1)
        elsewhere = source.replace(required[0], "", 1)
        for executable in required[0].split():
            invoked = executable in elsewhere or executable in service or executable in timer
            self.assertTrue(invoked, f"{executable} is required but never invoked")

    def test_build_script_is_executable_as_the_docs_invoke_it(self):
        # The doc and the install card both call ./systemd/build-...sh directly.
        # A 0644 mode makes that documented command fail with permission denied.
        self.assertTrue(BUILDER.stat().st_mode & 0o111, "build script is not executable")

    def test_old_layout_fails_preflight_and_regenerated_manifest_passes(self):
        # case_b4863264, executed rather than asserted as text: build a real bundle
        # with the real builder, lay out a release dir the way the pre-fix
        # installer did, and watch the unit's ExecStartPre equivalent fail
        # with the exact 3 FAILED-open-or-read lines from the case_0931610e
        # attempt; then lay it out per the fixed installer and watch it pass.
        #
        # The fixture is built with `git init` rather than by cloning this
        # repo, so it holds wherever the suite runs. The manifest-generation
        # command is read out of the installer under test, not hardcoded
        # here, so this follows the fix instead of duplicating it.
        payload = [
            "host_db_backup.sh",
            "systemd/install-host-db-backup.sh",
            "systemd/paperclip-host-db-backup.service",
            "systemd/paperclip-host-db-backup.timer",
        ]
        with tempfile.TemporaryDirectory() as td:
            work = pathlib.Path(td) / "repo"
            (work / "systemd").mkdir(parents=True)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                   "GIT_CONFIG_GLOBAL": str(pathlib.Path(td) / "gitconfig"),
                   "GIT_CONFIG_SYSTEM": os.devnull}

            def git(*a):
                r = subprocess.run(["git", *a], cwd=work, env=env,
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"git {a[0]}: {r.stderr}")
                return r.stdout.strip()

            shutil.copyfile(BUILDER, work / "systemd" / "build-host-db-backup-bundle.sh")
            (work / "systemd" / "build-host-db-backup-bundle.sh").chmod(0o755)
            # Stub payload: the builder only needs these paths to exist at the
            # commit. sha256sum does not care what the bytes mean.
            for rel in payload:
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_text(f"stub for {rel}\n")

            git("init", "--quiet", "-b", "trusted")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "trusted line")
            trusted = git("rev-parse", "HEAD")

            bundle = pathlib.Path(td) / "bundle.tar"
            built = subprocess.run(
                [str(work / "systemd" / "build-host-db-backup-bundle.sh"),
                 "--source-ref", trusted, "--trusted-line", "trusted",
                 "--output", str(bundle)],
                cwd=work, env=env, capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, f"builder failed: {built.stderr}")
            self.assertTrue(bundle.exists(), "builder produced no bundle")

            extract = pathlib.Path(td) / "extract"
            extract.mkdir()
            r = subprocess.run(["tar", "-xf", str(bundle), "-C", str(extract)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("systemd/paperclip-host-db-backup.service",
                          (extract / "SHA256SUMS").read_text())

            # Pre-fix layout: payload + REVISION + the bundle manifest,
            # copied verbatim into the release dir.
            old = pathlib.Path(td) / "release-old"
            old.mkdir()
            shutil.copyfile(extract / "host_db_backup.sh", old / "host_db_backup.sh")
            shutil.copyfile(extract / "REVISION", old / "REVISION")
            shutil.copyfile(extract / "SHA256SUMS", old / "SHA256SUMS")
            bad = subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"],
                                 cwd=old, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0,
                                "pre-fix layout unexpectedly passed its own preflight")
            combined = bad.stdout + bad.stderr
            self.assertEqual(combined.count("FAILED open or read"), 3, combined)

            # Fixed layout: payload + REVISION + a manifest regenerated by the
            # installer's own generation command.
            covered = manifest_generation_line(installer_source()
                        ).split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
            new = pathlib.Path(td) / "release-new"
            new.mkdir()
            shutil.copyfile(extract / "host_db_backup.sh", new / "host_db_backup.sh")
            shutil.copyfile(extract / "REVISION", new / "REVISION")
            gen = subprocess.run(["sha256sum", *covered],
                                 cwd=new, capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            (new / "SHA256SUMS").write_text(gen.stdout)
            good = subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"],
                                  cwd=new, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0,
                             f"fixed layout failed its own preflight: {good.stdout}{good.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
