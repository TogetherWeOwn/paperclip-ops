#!/usr/bin/env python3
# Deployment contract for the single recovery writer: every assertion below
# pins a property the two drifts violated, and verification/ carries a mutant
# for each one that must go red.

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SERVICE = ROOT / "systemd" / "paperclip-recovery-writer.service"
TIMER = ROOT / "systemd" / "paperclip-recovery-writer.timer"
INSTALLER = ROOT / "systemd" / "install-recovery-writer.sh"
BUILDER = ROOT / "systemd" / "build-recovery-writer-bundle.sh"
DRIVER = ROOT / "systemd" / "run-recovery-writer.sh"
WRITER = ROOT / "recovery_writer.py"
DOC = ROOT / "docs" / "recovery-writer.md"
MIGRATION = ROOT / "docs" / "recovery-writer-migration.md"


def installer_source() -> str:
    return INSTALLER.read_text()


def manifest_generation_line(source: str) -> str:
    lines = [
        line for line in source.splitlines()
        if "sha256sum " in line and " > " in line and "--check" not in line
    ]
    assert len(lines) == 1, f"expected one manifest-generation line, got {lines}"
    return lines[0]


class DeploymentContract(unittest.TestCase):
    def test_installer_requires_literal_commit_and_reviewed_bundle(self):
        source = INSTALLER.read_text()
        self.assertIn('[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]]', source)
        self.assertIn('[[ -n "$BUNDLE" && -f "$BUNDLE" ]]', source)
        self.assertIn('[[ "$(<"$WORK_DIR/REVISION")" == "$SOURCE_REF" ]]', source)
        self.assertIn('/usr/bin/sha256sum --check --strict SHA256SUMS', source)

    def test_installer_never_reads_git_or_mutable_checkout_as_root(self):
        source = INSTALLER.read_text()
        self.assertNotIn('git ', source)
        self.assertNotIn('git\n', source)
        self.assertNotIn('cp recovery_writer.py', source)
        self.assertIn('/usr/bin/tar --extract --file "$BUNDLE"', source)

    def test_unprivileged_builder_archives_installer_and_payload_from_reviewed_commit(self):
        source = BUILDER.read_text()
        self.assertIn('source_type=$(git --no-replace-objects cat-file -t "$SOURCE_REF"', source)
        self.assertIn('[[ "$source_type" == commit ]]', source)
        self.assertIn('systemd/install-recovery-writer.sh', source)
        self.assertIn('ops/lib/host_pressure.py', source)
        self.assertIn('git --no-replace-objects archive --format=tar "$SOURCE_REF"', source)
        self.assertIn('sha256sum "${files[@]}" REVISION > SHA256SUMS', source)

    def test_installer_never_targets_root_user_manager(self):
        source = INSTALLER.read_text()
        executable_lines = [line for line in source.splitlines() if not line.startswith("printf ")]
        self.assertFalse(any("systemctl --user" in line for line in executable_lines))
        self.assertIn("next: as %s, run systemctl --user daemon-reload", source)

    def test_installer_checks_every_hardcoded_runtime_executable(self):
        source = INSTALLER.read_text()
        for executable in ("/usr/bin/test", "/usr/bin/sha256sum", "/usr/bin/python3",
                           "/usr/bin/timeout"):
            self.assertIn(executable, source)
            self.assertIn('[[ -x "$executable" ]]', source)

    def test_unit_executes_versioned_installed_release_not_checkout(self):
        source = SERVICE.read_text()
        self.assertNotIn("PAPERCLIP_TOOLING_DIR", source)
        executable_lines = "\n".join(
            line for line in source.splitlines()
            if line.startswith(("WorkingDirectory=", "ExecStartPre=", "ExecStart="))
        )
        self.assertNotIn("/paperclip/instances/", executable_lines)
        self.assertGreaterEqual(source.count("/usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/"), 5)

    def test_unit_verifies_release_hashes_before_credential_preflight(self):
        source = SERVICE.read_text()
        preflights = [line for line in source.splitlines() if line.startswith("ExecStartPre=")]
        checksum_lines = [i for i, line in enumerate(preflights) if "/usr/bin/sha256sum --check --strict" in line]
        credential_lines = [i for i, line in enumerate(preflights) if "--check-service-credential" in line]
        self.assertEqual(len(checksum_lines), 1)
        self.assertEqual(len(credential_lines), 1)
        self.assertLess(checksum_lines[0], credential_lines[0])

    def test_unit_runs_exact_read_only_credential_preflight(self):
        source = SERVICE.read_text()
        lines = [line for line in source.splitlines() if line.startswith("ExecStartPre=")]
        matches = [line for line in lines if "--check-service-credential" in line]
        self.assertEqual(len(matches), 1)
        command = matches[0]
        self.assertTrue(command.startswith("ExecStartPre=/usr/bin/python3 /usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/recovery_writer.py "))
        self.assertIn("--preflight-issue-id ${PAPERCLIP_PREFLIGHT_ISSUE_ID}", command)
        self.assertIn("--preflight-agent-id ${PAPERCLIP_PREFLIGHT_AGENT_ID}", command)
        self.assertNotIn("/usr/bin/true", command)
        self.assertNotIn("#", command)

    def test_unit_prechecks_timeout_path(self):
        source = SERVICE.read_text()
        self.assertIn("ExecStartPre=/usr/bin/test -x /usr/bin/timeout", source)

    def test_timeout_start_sec_covers_preflight_and_oneshot(self):
        source = SERVICE.read_text()
        self.assertRegex(source, r"(?m)^TimeoutStartSec=5m$")

    def test_unit_runs_writer_through_the_reviewed_driver(self):
        # The drift guard: the mode bit (--apply) lives in the reviewed
        # driver, never in a unit a hand-edit could relabel. The unit must
        # exec the installed driver, not inline its own writer invocation.
        source = SERVICE.read_text()
        exec_start = re.search(r"(?m)^ExecStart=(.+)$", source).group(1)
        self.assertIn("/usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/run-recovery-writer.sh", exec_start)
        self.assertNotIn("recovery_writer.py --proposals", exec_start)
        self.assertNotIn("--apply", exec_start)

    def test_driver_carries_apply_literally_no_env_mode(self):
        # The other half: the driver's --apply is a literal in reviewed
        # bytes. An env var read for the mode bit here reintroduces the
        # sweep-auto drift through the back door.
        source = DRIVER.read_text()
        apply_lines = [line for line in source.splitlines()
                       if "--apply" in line and not line.strip().startswith("#")]
        self.assertEqual(len(apply_lines), 1)
        self.assertTrue(any(line.strip() == "--apply" for line in source.splitlines()),
                        "expected a literal --apply argv element in the driver")
        for env_name in ("RECOVERY_WRITER_APPLY", "WRITER_APPLY", "APPLY_MODE",
                         "RECOVERY_MODE", "DRY_RUN"):
            self.assertNotIn(env_name, source)

    def test_driver_refuses_empty_detector_output(self):
        # Executed, not textual: a detector that emits nothing must abort
        # the cycle before the writer runs. A text assertion here would
        # match the comment describing the contract ("zero proposals all
        # abort LOUDLY") rather than the contract itself -- measured, not
        # assumed, after a message-only mutant passed the textual form.
        with tempfile.TemporaryDirectory() as td:
            stage = pathlib.Path(td)
            shutil.copyfile(DRIVER, stage / "run-recovery-writer.sh")
            (stage / "run-recovery-writer.sh").chmod(0o755)
            marker = stage / "writer-ran"
            (stage / "recovery_writer.py").write_text(
                "#!/usr/bin/env python3\n"
                f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n"
            )
            (stage / "recovery_writer.py").chmod(0o755)
            env = {**os.environ,
                   "PAPERCLIP_API_URL": "http://127.0.0.1:9",
                   "PAPERCLIP_API_KEY": "dead-key",
                   "PAPERCLIP_RECOVERY_WRITER_DETECTOR_CMD": "true",
                   "PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS": "0"}
            result = subprocess.run(
                [str(stage / "run-recovery-writer.sh")],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0,
                                "driver accepted an empty detector cycle as clean")
            self.assertFalse(marker.exists(),
                             "driver ran the writer on an empty detector cycle")

    def test_repair_cap_is_operator_settable_without_editing_a_root_unit(self):
        # The cap must stay a variable in reviewed bytes, never a baked
        # literal -- in the unit (root-owned, sha256-pinned) or in the
        # driver. A literal anywhere turns "raise the cap" into "re-review
        # the bundle", which is how a needed repair goes unmade.
        source = SERVICE.read_text()
        driver = DRIVER.read_text()
        self.assertIn("--max-repairs", driver)
        self.assertNotRegex(source, r"--max-repairs\s+\d")
        self.assertNotRegex(driver, r"--max-repairs\s+\d")

    def test_unset_repair_cap_defaults_to_zero_not_an_empty_argument(self):
        source = SERVICE.read_text()
        self.assertRegex(source, r"(?m)^Environment=PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS=0$")
        directives = [
            line for line in source.splitlines() if line and not line.startswith("#")
        ]
        default_at = next(
            i for i, line in enumerate(directives)
            if line.startswith("Environment=PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS=")
        )
        env_file_at = next(
            i for i, line in enumerate(directives) if line.startswith("EnvironmentFile=")
        )
        self.assertLess(default_at, env_file_at)

    def test_no_writable_home_path_contradicts_protect_home(self):
        source = SERVICE.read_text()
        self.assertIn("ProtectHome=read-only", source)
        directives = [line for line in source.splitlines() if line.startswith("ReadWritePaths=")]
        self.assertEqual([d for d in directives if "%h" in d], [])

    def test_timer_targets_exact_writer_service(self):
        source = TIMER.read_text()
        self.assertRegex(source, r"(?m)^Unit=paperclip-recovery-writer\.service$")

    def test_timer_retains_interval_jitter_and_persistence(self):
        source = TIMER.read_text()
        for line in ("OnUnitActiveSec=10m", "RandomizedDelaySec=90s", "Persistent=true"):
            self.assertIn(line, source)

    def test_installer_only_requires_executables_it_actually_runs(self):
        source = INSTALLER.read_text()
        service = SERVICE.read_text()
        timer = TIMER.read_text()
        required = re.findall(r"(?m)^for executable in (.+); do$", source)
        self.assertEqual(len(required), 1)
        elsewhere = source.replace(required[0], "", 1)
        for executable in required[0].split():
            invoked = executable in elsewhere or executable in service or executable in timer
            self.assertTrue(invoked, f"{executable} is required but never invoked")

    def test_builder_refuses_a_commit_not_merged_into_the_trusted_line(self):
        source = BUILDER.read_text()
        self.assertIn(
            'git --no-replace-objects merge-base --is-ancestor "$SOURCE_REF" "$TRUSTED_LINE"',
            source,
        )
        self.assertIn('rev-parse -q --verify "$TRUSTED_LINE^{commit}"', source)
        self.assertIn('TRUSTED_LINE="origin/main"', source)

    def test_builder_actually_refuses_to_write_a_bundle_for_an_unmerged_commit(self):
        repo = pathlib.Path(__file__).resolve().parent
        builder_src = repo / "systemd" / "build-recovery-writer-bundle.sh"
        payload = [
            "lib/paperclip_api_allowlist.py",
            "recovery_writer.py",
            "ops/lib/host_pressure.py",
            "systemd/run-recovery-writer.sh",
            "systemd/install-recovery-writer.sh",
            "systemd/paperclip-recovery-writer.service",
            "systemd/paperclip-recovery-writer.timer",
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

            shutil.copyfile(builder_src, work / "systemd" / "build-recovery-writer-bundle.sh")
            (work / "systemd" / "build-recovery-writer-bundle.sh").chmod(0o755)
            for rel in payload:
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_text(f"stub for {rel}\n")

            git("init", "--quiet", "-b", "trusted")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "trusted line")
            trusted = git("rev-parse", "HEAD")

            git("checkout", "--quiet", "-b", "sibling", trusted)
            (work / "DIVERGED").write_text("not on the reviewed line\n")
            git("add", "DIVERGED")
            git("commit", "--quiet", "--no-verify", "-m", "diverged sibling")
            unmerged = git("rev-parse", "HEAD")
            git("checkout", "--quiet", "trusted")

            def run(source_ref, out):
                return subprocess.run(
                    [str(work / "systemd" / "build-recovery-writer-bundle.sh"),
                     "--source-ref", source_ref, "--trusted-line", "trusted",
                     "--output", str(out)],
                    cwd=work, env=env, capture_output=True, text=True,
                )

            bad = pathlib.Path(td) / "bad.tar"
            refused = run(unmerged, bad)
            self.assertNotEqual(refused.returncode, 0,
                                "builder accepted a commit not on the trusted line")
            self.assertFalse(bad.exists(),
                             "builder wrote a bundle for an unmerged commit")

            good = pathlib.Path(td) / "good.tar"
            ok = run(trusted, good)
            self.assertEqual(ok.returncode, 0,
                             f"builder refused the trusted commit: {ok.stderr}")
            self.assertTrue(good.exists(), "builder produced no bundle for a good commit")

    def test_build_script_is_executable_as_the_docs_invoke_it(self):
        builder = ROOT / "systemd" / "build-recovery-writer-bundle.sh"
        self.assertTrue(builder.stat().st_mode & 0o111, "build script is not executable")

    def test_installer_compiles_and_imports_pressure_module_before_claiming_installed(self):
        source = INSTALLER.read_text()
        self.assertIn(
            '/usr/bin/python3 -m py_compile "$RELEASE_DIR/recovery_writer.py" "$RELEASE_DIR/lib/paperclip_api_allowlist.py" "$RELEASE_DIR/ops/lib/host_pressure.py"',
            source,
        )
        self.assertIn(
            '(cd "$RELEASE_DIR" && /usr/bin/python3 recovery_writer.py --help >/dev/null)',
            source,
        )

    def test_installer_verifies_bundle_manifest_before_installing(self):
        source = installer_source()
        self.assertIn('(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)', source)
        check_at = source.index('(cd "$WORK_DIR"')
        release_at = source.index('RELEASE_DIR="/usr/local/libexec/paperclip-recovery-writer/$SOURCE_REF"')
        self.assertLess(check_at, release_at)

    def test_installer_does_not_copy_bundle_manifest_into_release(self):
        source = installer_source()
        self.assertNotIn('"$WORK_DIR/SHA256SUMS" "$RELEASE_DIR', source)

    def test_installer_regenerates_release_scoped_manifest(self):
        source = installer_source()
        line = manifest_generation_line(source)
        self.assertIn('(cd "$RELEASE_DIR"', line)
        self.assertIn("/usr/bin/sha256sum lib/paperclip_api_allowlist.py recovery_writer.py ops/lib/host_pressure.py run-recovery-writer.sh REVISION", line)
        self.assertIn('"$WORK_DIR/SHA256SUMS.release"', line)
        self.assertIn('"$WORK_DIR/SHA256SUMS.release" "$RELEASE_DIR/SHA256SUMS"', source)
        revision_install = '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/REVISION"'
        self.assertLess(source.index(revision_install), source.index(line))

    def test_installer_proves_unit_preflight_before_claiming_installed(self):
        source = installer_source()
        release_check = '(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)'
        self.assertIn(release_check, source)
        self.assertLess(
            source.index(manifest_generation_line(source)),
            source.index(release_check),
        )
        self.assertLess(
            source.index(release_check),
            source.index("printf 'INSTALLED paperclip-recovery-writer"),
        )

    def test_release_manifest_covers_exactly_the_installed_byte_set(self):
        source = installer_source()
        install_lines = [line for line in source.splitlines()
                         if "/usr/bin/install " in line and " -d " not in line]
        installed = set(re.findall(r'"\$RELEASE_DIR/([A-Za-z0-9_./\-]+)"', "\n".join(install_lines)))
        self.assertGreaterEqual(installed, {
            "lib/paperclip_api_allowlist.py",
            "recovery_writer.py", "ops/lib/host_pressure.py",
            "run-recovery-writer.sh", "REVISION", "SHA256SUMS",
        })
        covered = manifest_generation_line(source).split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
        self.assertEqual(set(covered), installed - {"SHA256SUMS"})

    def test_unit_preflight_checks_installed_release_manifest(self):
        source = SERVICE.read_text()
        self.assertIn(
            "WorkingDirectory=/usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@",
            source,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/sha256sum --check --strict "
            "/usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/SHA256SUMS",
            source,
        )

    def test_bundle_manifest_still_covers_payload_units_installer_and_driver(self):
        source = BUILDER.read_text()
        for member in (
            "lib/paperclip_api_allowlist.py",
            "recovery_writer.py",
            "ops/lib/host_pressure.py",
            "systemd/run-recovery-writer.sh",
            "systemd/install-recovery-writer.sh",
            "systemd/paperclip-recovery-writer.service",
            "systemd/paperclip-recovery-writer.timer",
        ):
            self.assertIn(member, source)
        self.assertIn('sha256sum "${files[@]}" REVISION > SHA256SUMS', source)

    def test_old_layout_fails_preflight_and_regenerated_manifest_passes(self):
        payload = [
            "lib/paperclip_api_allowlist.py",
            "recovery_writer.py",
            "ops/lib/host_pressure.py",
            "systemd/run-recovery-writer.sh",
            "systemd/install-recovery-writer.sh",
            "systemd/paperclip-recovery-writer.service",
            "systemd/paperclip-recovery-writer.timer",
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

            shutil.copyfile(BUILDER, work / "systemd" / "build-recovery-writer-bundle.sh")
            (work / "systemd" / "build-recovery-writer-bundle.sh").chmod(0o755)
            for rel in payload:
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_text(f"stub for {rel}\n")

            git("init", "--quiet", "-b", "trusted")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "trusted line")
            trusted = git("rev-parse", "HEAD")

            bundle = pathlib.Path(td) / "bundle.tar"
            built = subprocess.run(
                [str(work / "systemd" / "build-recovery-writer-bundle.sh"),
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
            self.assertIn("systemd/paperclip-recovery-writer.service",
                          (extract / "SHA256SUMS").read_text())

            old = pathlib.Path(td) / "release-old"
            old.mkdir()
            shutil.copyfile(extract / "recovery_writer.py", old / "recovery_writer.py")
            shutil.copyfile(extract / "systemd" / "run-recovery-writer.sh",
                            old / "run-recovery-writer.sh")
            shutil.copyfile(extract / "REVISION", old / "REVISION")
            shutil.copyfile(extract / "SHA256SUMS", old / "SHA256SUMS")
            bad = subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"],
                                 cwd=old, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0,
                                "pre-fix layout unexpectedly passed its own preflight")
            combined = bad.stdout + bad.stderr
            self.assertGreaterEqual(combined.count("FAILED open or read"), 3, combined)

            covered = manifest_generation_line(installer_source()
                        ).split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
            new = pathlib.Path(td) / "release-new"
            (new / "lib").mkdir(parents=True)
            (new / "ops" / "lib").mkdir(parents=True)
            shutil.copyfile(extract / "lib" / "paperclip_api_allowlist.py",
                            new / "lib" / "paperclip_api_allowlist.py")
            shutil.copyfile(extract / "recovery_writer.py", new / "recovery_writer.py")
            shutil.copyfile(extract / "ops" / "lib" / "host_pressure.py",
                            new / "ops" / "lib" / "host_pressure.py")
            shutil.copyfile(extract / "systemd" / "run-recovery-writer.sh",
                            new / "run-recovery-writer.sh")
            shutil.copyfile(extract / "REVISION", new / "REVISION")
            gen = subprocess.run(["sha256sum", *covered],
                                 cwd=new, capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            (new / "SHA256SUMS").write_text(gen.stdout)
            good = subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"],
                                  cwd=new, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0,
                             f"fixed layout failed its own preflight: {good.stdout}{good.stderr}")

    def test_rollback_stops_timer_and_active_service_before_removal(self):
        source = MIGRATION.read_text()
        timer_marker = "disable --now paperclip-recovery-writer.timer"
        service_marker = "systemctl --user stop paperclip-recovery-writer.service"
        removal_marker = "sudo rm -f"
        self.assertIn(timer_marker, source)
        self.assertIn(service_marker, source)
        self.assertIn(removal_marker, source)
        timer = source.index(timer_marker)
        service = source.index(service_marker, timer)
        removal = source.index(removal_marker, service)
        self.assertLess(timer, service)
        self.assertLess(service, removal)

    def test_install_section_runs_observe_only_before_timer(self):
        # The first live cycle must be provably incapable of a write. The
        # install doc must order the hand-run before the timer enable, with
        # the cap still at 0. Presence is asserted with assertIn (a FAIL),
        # never trusted to str.index (a ValueError ERROR the gate cannot
        # attribute) -- the install-order mutant deletes the hand-run line.
        source = DOC.read_text()
        self.assertIn("--max-repairs 0", source)
        self.assertIn("start paperclip-recovery-writer.service", source)
        hand_at = source.index("start paperclip-recovery-writer.service")
        timer_at = source.index("enable --now paperclip-recovery-writer.timer")
        self.assertLess(hand_at, timer_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)