#!/usr/bin/env python3
# case_0bf02bac: the home-PC guard timer reconciles CI_nodecf677d86_RUNNER with
# home-PC runner liveness. The installer must install the reviewed bytes,
# the release manifest must cover exactly the installed byte set (the
# case_b4863264 shape), and the unit must bound every start. These tests pin the
# installer bytes, the unit's end of the contract, and the mechanism itself,
# executed against a real built bundle, with the pre-fix layout reproduced
# as the failing control.

from __future__ import annotations

import http.server
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SERVICE = ROOT / "systemd" / "paperclip-ci-node93f4bccf-guard.service"
TIMER = ROOT / "systemd" / "paperclip-ci-node93f4bccf-guard.timer"
RUNNER = ROOT / "systemd" / "run-ci-node93f4bccf-guard.sh"
INSTALLER = ROOT / "systemd" / "install-ci-node93f4bccf-guard.sh"
BUILDER = ROOT / "systemd" / "build-ci-node93f4bccf-guard-bundle.sh"


def installer_source() -> str:
    return INSTALLER.read_text()


def manifest_generation_line(source: str) -> str:
    lines = [
        line for line in source.splitlines()
        if "sha256sum " in line and " > " in line and "--check" not in line
    ]
    assert len(lines) == 1, f"expected one manifest-generation line, got {lines}"
    return lines[0]


class CiHomepcGuardDeploymentContract(unittest.TestCase):
    def test_installer_verifies_bundle_manifest_before_installing(self):
        source = installer_source()
        self.assertIn('(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)', source)
        check_at = source.index('(cd "$WORK_DIR"')
        release_at = source.index('RELEASE_DIR="/usr/local/libexec/paperclip-ci-node93f4bccf-guard/$SOURCE_REF"')
        self.assertLess(check_at, release_at)

    def test_installer_does_not_copy_bundle_manifest_into_release(self):
        source = installer_source()
        self.assertNotIn('"$WORK_DIR/REVISION" "$WORK_DIR/SHA256SUMS" "$RELEASE_DIR/"', source)
        self.assertNotIn('"$WORK_DIR/SHA256SUMS" "$RELEASE_DIR', source)

    def test_installer_regenerates_release_scoped_manifest(self):
        source = installer_source()
        line = manifest_generation_line(source)
        self.assertIn('(cd "$RELEASE_DIR"', line)
        self.assertIn("/usr/bin/sha256sum ci_node93f4bccf_guard.py", line)
        self.assertIn('"$WORK_DIR/SHA256SUMS.release" "$RELEASE_DIR/SHA256SUMS"', source)

    def test_installer_proves_unit_preflight_before_claiming_installed(self):
        source = installer_source()
        release_check = '(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)'
        self.assertIn(release_check, source)
        self.assertLess(source.index(manifest_generation_line(source)), source.index(release_check))
        self.assertLess(source.index(release_check), source.index("printf 'INSTALLED paperclip-ci-node93f4bccf-guard"))

    def test_release_manifest_covers_exactly_the_installed_byte_set(self):
        source = installer_source()
        # Installed bytes: $RELEASE_DIR/<name> targets plus WORK_DIR files
        # the installer copies by basename (REVISION travels as
        # "$WORK_DIR/REVISION" "$RELEASE_DIR/").
        installed = set(re.findall(r'"\$RELEASE_DIR/([A-Za-z0-9_.\-]+)"', source))
        installed |= set(re.findall(r'"\$WORK_DIR/([A-Za-z0-9_.\-]+)" "\$RELEASE_DIR/"', source))
        installed |= set(re.findall(r'"\$WORK_DIR/([A-Za-z0-9_.\-]+)" "\$RELEASE_DIR', source))
        # The runner installs flat (systemd/ prefix stripped at install).
        # Only the install-target line counts: manifest paths
        # (SHA256SUMS.release) and cleanup lines (__pycache__ removal) are
        # not installed bytes.
        installed |= {pathlib.PurePath(m).name
                      for m in re.findall(r'"\$WORK_DIR/(systemd/run-ci-node93f4bccf-guard\.sh)" "\$RELEASE_DIR/"', source)}
        # Not installed bytes: the release manifest is written under its own
        # name (never listed as a source), and __pycache__ is removed, not installed.
        installed -= {"SHA256SUMS.release", "__pycache__"}
        self.assertGreaterEqual(
            installed, {"ci_node93f4bccf_guard.py", "test_ci_node93f4bccf_guard.py",
                        "run-ci-node93f4bccf-guard.sh", "REVISION", "SHA256SUMS"})
        covered = manifest_generation_line(source).split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
        self.assertEqual(set(covered), installed - {"SHA256SUMS"})

    def test_unit_preflight_checks_installed_release_manifest(self):
        source = SERVICE.read_text()
        self.assertIn(
            "ExecStartPre=/usr/bin/sha256sum --check --strict "
            "/usr/local/libexec/paperclip-ci-node93f4bccf-guard/@SOURCE_REF@/SHA256SUMS",
            source,
        )

    def test_timer_fires_within_the_two_minute_offline_bound(self):
        timer = TIMER.read_text()
        m = re.search(r"(?m)^OnUnitActiveSec=(.+)$", timer)
        self.assertIsNotNone(m, "timer has no OnUnitActiveSec")
        value = m.group(1).strip()
        self.assertEqual(value, "60s")
        self.assertIn("RandomizedDelaySec=0", timer)
        self.assertIn("AccuracySec=1s", timer)
        service = SERVICE.read_text()
        self.assertIn("--kill-after=5s 45s /usr/bin/bash", service)
        self.assertIn("TimeoutStartSec=55s", service)
        self.assertLess(60 + 1 + 55, 120)
        self.assertIn("Persistent=false", timer)

    def test_runner_never_carries_the_credential_on_argv(self):
        source = RUNNER.read_text()
        self.assertIn('CREDENTIALS_DIRECTORY', source)
        self.assertIn('GH_TOKEN="$(cat "$CREDENTIAL_PATH")"', source)
        self.assertNotIn("GH_TOKEN=", source.replace('GH_TOKEN="$(cat "$CREDENTIAL_PATH")"', ""))

    def test_credential_is_a_system_scope_unit(self):
        # The encrypted credential lives in /etc/credstore.encrypted, which
        # only a system unit can load; a user unit cannot. The installer
        # must target /etc/systemd/system, matching the quota-controller.
        source = installer_source()
        self.assertIn("/etc/systemd/system/paperclip-ci-node93f4bccf-guard.service", source)
        self.assertIn("/etc/systemd/system/paperclip-ci-node93f4bccf-guard.timer", source)

    def test_bundle_manifest_still_covers_payload_units_and_installer(self):
        source = BUILDER.read_text()
        for member in (
            "ci_node93f4bccf_guard.py",
            "test_ci_node93f4bccf_guard.py",
            "systemd/run-ci-node93f4bccf-guard.sh",
            "systemd/install-ci-node93f4bccf-guard.sh",
            "systemd/paperclip-ci-node93f4bccf-guard.service",
            "systemd/paperclip-ci-node93f4bccf-guard.timer",
            "systemd/ci-node93f4bccf-guard.env.example",
        ):
            self.assertIn(member, source)
        self.assertIn('sha256sum "${files[@]}" REVISION > SHA256SUMS', source)

    def test_installer_only_requires_executables_it_actually_runs(self):
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
        self.assertTrue(BUILDER.stat().st_mode & 0o111, "build script is not executable")

    def test_old_layout_fails_preflight_and_regenerated_manifest_passes(self):
        payload = [
            "ci_node93f4bccf_guard.py",
            "systemd/install-ci-node93f4bccf-guard.sh",
            "systemd/paperclip-ci-node93f4bccf-guard.service",
            "systemd/paperclip-ci-node93f4bccf-guard.timer",
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

            shutil.copyfile(BUILDER, work / "systemd" / "build-ci-node93f4bccf-guard-bundle.sh")
            (work / "systemd" / "build-ci-node93f4bccf-guard-bundle.sh").chmod(0o755)
            for rel in payload:
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_text(f"stub for {rel}\n")
            (work / "test_ci_node93f4bccf_guard.py").write_text("stub\n")
            (work / "systemd" / "run-ci-node93f4bccf-guard.sh").write_text("stub\n")
            (work / "systemd" / "ci-node93f4bccf-guard.env.example").write_text("stub\n")

            git("init", "--quiet", "-b", "trusted")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "trusted line")
            trusted = git("rev-parse", "HEAD")

            bundle = pathlib.Path(td) / "bundle.tar"
            built = subprocess.run(
                [str(work / "systemd" / "build-ci-node93f4bccf-guard-bundle.sh"),
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
            self.assertIn("systemd/paperclip-ci-node93f4bccf-guard.service",
                          (extract / "SHA256SUMS").read_text())

            old = pathlib.Path(td) / "release-old"
            old.mkdir()
            shutil.copyfile(extract / "ci_node93f4bccf_guard.py", old / "ci_node93f4bccf_guard.py")
            shutil.copyfile(extract / "REVISION", old / "REVISION")
            shutil.copyfile(extract / "SHA256SUMS", old / "SHA256SUMS")
            bad = subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"],
                                 cwd=old, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0,
                                "pre-fix layout unexpectedly passed its own preflight")

            covered = manifest_generation_line(installer_source()
                        ).split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
            # Layout the full installed byte set the way the fixed installer
            # does: every covered name must resolve in the release dir, or
            # the generation command itself fails -- which is the assertion.
            new = pathlib.Path(td) / "release-new"
            new.mkdir()
            for name in covered:
                src = extract / name
                if not src.is_file():
                    # run-ci-node93f4bccf-guard.sh installs flat (no systemd/ prefix).
                    alt = extract / "systemd" / name
                    self.assertTrue(alt.is_file(), f"bundle lacks {name} and systemd/{name}")
                    src = alt
                shutil.copyfile(src, new / name)
            shutil.copyfile(extract / "REVISION", new / "REVISION")
            gen = subprocess.run(["sha256sum", *covered, "REVISION"],
                                 cwd=new, capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            (new / "SHA256SUMS").write_text(gen.stdout)
            good = subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"],
                                  cwd=new, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0,
                             f"fixed layout failed its own preflight: {good.stdout}{good.stderr}")


class InstalledService(unittest.TestCase):
    """Run the real installer and rendered commands only inside a temp root.

    No systemctl, host paths or real GitHub: installer substitutions redirect
    destination prefixes and root ownership/uid checks. Payload bytes, modes,
    manifests, wrapper and interpreter are the shipped ones.
    """

    def setUp(self):
        td = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR"))
        self.addCleanup(td.cleanup)
        self.root = pathlib.Path(td.name)
        work = self.root / "repo"
        work.mkdir()
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        payload = ["ci_node93f4bccf_guard.py", "test_ci_node93f4bccf_guard.py",
                   "systemd/run-ci-node93f4bccf-guard.sh", "systemd/install-ci-node93f4bccf-guard.sh",
                   "systemd/paperclip-ci-node93f4bccf-guard.service", "systemd/paperclip-ci-node93f4bccf-guard.timer",
                   "systemd/ci-node93f4bccf-guard.env.example", "systemd/build-ci-node93f4bccf-guard-bundle.sh"]
        for rel in payload:
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dest)
        for args in (["git", "init", "--quiet", "-b", "trusted"],
                     ["git", "add", "-A"], ["git", "commit", "--quiet", "--no-verify", "-m", "fixture"]):
            self.run_ok(args, cwd=work)
        ref = self.run_ok(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
        bundle = self.root / "bundle.tar"
        self.run_ok(["bash", str(work / "systemd/build-ci-node93f4bccf-guard-bundle.sh"),
                     "--source-ref", ref, "--trusted-line", "trusted", "--output", str(bundle)], cwd=work)
        source = installer_source()
        prefixes = ("/usr/local/libexec/paperclip-ci-node93f4bccf-guard", "/etc/paperclip",
                    "/var/lib/paperclip/ci-node93f4bccf-guard", "/etc/credstore.encrypted", "/etc/systemd/system")
        for prefix in prefixes:
            source = source.replace(prefix, str(self.root) + prefix)
            (self.root / prefix.lstrip("/")).mkdir(parents=True, exist_ok=True)
        source = source.replace('[[ $EUID -eq 0 ]]', 'true')
        source = source.replace('-o root -g root', f'-o {os.getuid()} -g {os.getgid()}')
        installer = self.root / "installer.sh"
        installer.write_text(source)
        self.run_ok(["bash", str(installer), "--source-ref", ref, "--bundle", str(bundle)])
        self.release = self.root / "usr/local/libexec/paperclip-ci-node93f4bccf-guard" / ref
        self.assertEqual(stat.S_IMODE((self.release / "ci_node93f4bccf_guard.py").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE((self.release / "run-ci-node93f4bccf-guard.sh").stat().st_mode), 0o555)
        self.service = (self.root / "etc/systemd/system/paperclip-ci-node93f4bccf-guard.service").read_text()
        # The template in the tar has host paths, so redirect its destinations
        # in this test copy, after the actual installer renders the revision.
        for prefix in prefixes:
            self.service = self.service.replace(prefix, str(self.root) + prefix)
        self.cwd = pathlib.Path(re.search(r"(?m)^WorkingDirectory=(.+)$", self.service).group(1))
        self.preflights = [shlex.split(line.split("=", 1)[1]) for line in self.service.splitlines()
                           if line.startswith("ExecStartPre=")]
        self.command = shlex.split(re.search(r"(?m)^ExecStart=(.+)$", self.service).group(1))
        credentials = self.root / "credentials"
        credentials.mkdir()
        (credentials / "ci-node93f4bccf-guard-gh-token").write_text("synthetic-offline-token")
        self.env["CREDENTIALS_DIRECTORY"] = str(credentials)
        self.variable = None
        self.fail_observation = False
        self.truncate_observation = False
        self.calls = []
        test = self

        class API(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, payload=None):
                self.send_response(code)
                self.end_headers()
                if payload is not None:
                    self.wfile.write(json.dumps(payload).encode())

            def do_GET(self):
                test.calls.append("GET")
                if self.headers.get("Authorization") != "Bearer synthetic-offline-token":
                    return self.reply(401)
                if "/actions/runners?" in self.path:
                    if test.fail_observation:
                        return self.reply(503)
                    if test.truncate_observation:
                        # Promise 500 bytes, send 10, close: IncompleteRead on the client.
                        self.send_response(200)
                        self.send_header("Content-Length", "500")
                        self.end_headers()
                        self.wfile.write(b'{"runners"')
                        self.close_connection = True
                        return
                    return self.reply(200, {"runners": [{"name": "node93f4bccf-r1", "status": "online",
                                        "busy": False, "labels": [{"name": "two-node93f4bccf"}]}]})
                if "/actions/variables/" in self.path:
                    return self.reply(404) if test.variable is None else self.reply(200, {"value": test.variable})
                return self.reply(404)

            def do_POST(self):
                test.calls.append("POST")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.variable = body["value"]
                self.reply(201, {})

            def do_DELETE(self):
                test.calls.append("DELETE")
                test.variable = None
                self.reply(204)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.env["nodecf677d86_API_URL"] = f"http://127.0.0.1:{server.server_port}"

    def run_command(self, command, cwd=None):
        return subprocess.run(command, cwd=cwd or self.root, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def run_ok(self, command, cwd=None):
        result = self.run_command(command, cwd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_installed_service_preflights_and_real_wrapper_start(self):
        self.assertEqual(self.cwd, self.release)
        for command in self.preflights:
            self.run_ok(command, self.cwd)
        result = self.run_ok(self.command, self.cwd)
        self.assertEqual(self.variable, '["self-hosted","two-node93f4bccf"]')
        self.assertIn("POST", self.calls)
        self.assertNotIn("synthetic-offline-token", result.stdout + result.stderr)
        self.fail_observation = True
        result = self.run_command(self.command, self.cwd)
        self.assertEqual(result.returncode, 3)
        self.assertIsNone(self.variable)
        self.assertIn("DELETE", self.calls)

    def test_installed_service_clears_the_route_when_the_runner_body_is_cut_short(self):
        self.run_ok(self.command, self.cwd)
        self.assertEqual(self.variable, '["self-hosted","two-node93f4bccf"]')
        self.calls.clear()
        self.truncate_observation = True
        result = self.run_command(self.command, self.cwd)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIsNone(self.variable)
        self.assertIn("DELETE", self.calls)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("synthetic-offline-token", result.stdout + result.stderr)

    def test_old_executable_bit_preflight_rejects_installed_read_only_python(self):
        command = next(c for c in self.preflights if c[-1].endswith("/ci_node93f4bccf_guard.py"))
        self.run_ok(command, self.cwd)
        old = ["-x" if arg == "-r" else arg for arg in command]
        self.assertNotEqual(self.run_command(old, self.cwd).returncode, 0)

    def test_old_python_interpreter_cannot_execute_bash_wrapper(self):
        old = self.command.copy()
        old[old.index("/usr/bin/bash")] = "/usr/bin/python3"
        self.assertNotEqual(self.run_command(old, self.cwd).returncode, 0)
        self.assertEqual(self.calls, [])

    def test_manifest_requires_release_working_directory_and_detects_tamper(self):
        command = next(c for c in self.preflights if c[0] == "/usr/bin/sha256sum")
        self.run_ok(command, self.cwd)
        self.assertNotEqual(self.run_command(command, self.root).returncode, 0)
        payload = self.release / "ci_node93f4bccf_guard.py"
        payload.chmod(0o644)
        with payload.open("a") as out:
            out.write("\n# tampered\n")
        self.assertNotEqual(self.run_command(command, self.cwd).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
