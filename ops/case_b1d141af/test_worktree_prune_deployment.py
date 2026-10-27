#!/usr/bin/env python3
# case_b1d141af: deployment contract for the daily worktree pruner. Offline, no
# credentials, no container engine. Pins (1) the unit's bounds and the mode-bit
# rule, (2) that the bundle builder, the installer and the release manifest
# agree on one byte set (the case_b4863264 shape), and (3) the mechanism itself:
# a real bundle built from a real git commit is installed by the real installer
# into a temp root, and the installed wrapper resolves all four reviewed modules
# from the installed layout.

from __future__ import annotations

import io
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE = ROOT / "systemd" / "paperclip-worktree-prune.service"
TIMER = ROOT / "systemd" / "paperclip-worktree-prune.timer"
ENV_EXAMPLE = ROOT / "systemd" / "worktree-prune.env.example"
BUILDER = ROOT / "systemd" / "build-worktree-prune-bundle.sh"
INSTALLER = ROOT / "systemd" / "install-worktree-prune.sh"
WRAPPER = ROOT / "ops" / "case_b1d141af" / "run-worktree-prune.sh"

PAYLOAD = [
    "lib/paperclip_api_allowlist.py",
    "lib/paperclip_api_allowlist.sh",
    "ops/case_b1d141af/run-worktree-prune.sh",
    "ops/case_b1d141af/terminal_worktree_prune.py",
    "ops/case_b5184419/terminal_worktree_cache_reclaim.py",
    "ops/lib/atomic_state.py",
    "systemd/install-worktree-prune.sh",
    "systemd/paperclip-worktree-prune.service",
    "systemd/paperclip-worktree-prune.timer",
    "systemd/worktree-prune.env.example",
]
# What the installer puts in the release directory (bundle-scope systemd/* is
# verified but never installed).
INSTALLED = [
    "lib/paperclip_api_allowlist.py",
    "lib/paperclip_api_allowlist.sh",
    "ops/lib/atomic_state.py",
    "ops/case_b5184419/terminal_worktree_cache_reclaim.py",
    "ops/case_b1d141af/run-worktree-prune.sh",
    "ops/case_b1d141af/terminal_worktree_prune.py",
    "REVISION",
]


def active_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def seconds(value: str) -> int:
    m = re.fullmatch(r"(\d+)\s*(s|m|min|h)?", value.strip())
    assert m, f"unparseable duration {value!r}"
    return int(m.group(1)) * {"s": 1, None: 1, "m": 60, "min": 60, "h": 3600}[m.group(2)]


class UnitContract(unittest.TestCase):
    def setUp(self):
        self.service = SERVICE.read_text()
        self.lines = active_lines(self.service)

    def option(self, key):
        return [ln.split("=", 1)[1] for ln in self.lines if ln.startswith(key + "=")]

    def test_oneshot_with_no_self_retry(self):
        self.assertEqual(self.option("Type"), ["oneshot"])
        self.assertEqual(self.option("Restart"), [])

    def test_bounds_nest_in_order(self):
        wrapper_bound = int(re.search(r'BOUND_SECONDS="\$\{PAPERCLIP_WORKTREE_PRUNE_BOUND_SECONDS:-(\d+)\}"',
                                      WRAPPER.read_text()).group(1))
        exec_start = self.option("ExecStart")
        self.assertEqual(len(exec_start), 1)
        m = re.match(r"/usr/bin/timeout --signal=TERM --kill-after=(\S+) (\S+) ", exec_start[0])
        self.assertIsNotNone(m, exec_start[0])
        host_timeout = seconds(m.group(2))
        unit_bound = seconds(self.option("TimeoutStartSec")[0])
        self.assertLess(wrapper_bound, host_timeout, "in-container bound must fire before the host timeout")
        self.assertLess(host_timeout + seconds(m.group(1)), unit_bound,
                        "host timeout plus kill-after must fit inside TimeoutStartSec")

    def test_mode_bit_is_not_in_the_unit_or_env(self):
        # --apply lives in the reviewed wrapper's argv; nothing the operator
        # edits (unit environment, env file) can carry it.
        for text in (self.service, ENV_EXAMPLE.read_text()):
            self.assertNotIn("APPLY", "\n".join(active_lines(text)).upper())

    def test_default_cap_is_observe_only_and_env_file_overrides(self):
        env_lines = [ln for ln in self.lines if ln.startswith(("Environment=", "EnvironmentFile="))]
        self.assertEqual(env_lines[0], "Environment=PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE=0")
        self.assertEqual(env_lines[1], "EnvironmentFile=/etc/paperclip/worktree-prune.env")

    def test_example_ships_the_cap_at_zero(self):
        self.assertIn("PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE=0", active_lines(ENV_EXAMPLE.read_text()))

    def test_preflight_checks_installed_release_manifest_and_tools(self):
        base = "/usr/local/libexec/paperclip-worktree-prune/@SOURCE_REF@"
        pre = self.option("ExecStartPre")
        self.assertIn(f"/usr/bin/sha256sum --check --strict {base}/SHA256SUMS", pre)
        self.assertIn(f"/usr/bin/test -x {base}/ops/case_b1d141af/run-worktree-prune.sh", pre)
        self.assertIn("/usr/bin/test -x /usr/bin/timeout", pre)
        self.assertIn("/bin/sh -c '/usr/bin/test -x /usr/bin/podman || /usr/bin/test -x /usr/bin/docker'", pre)

    def test_exec_start_runs_the_installed_wrapper(self):
        self.assertIn(
            "/usr/local/libexec/paperclip-worktree-prune/@SOURCE_REF@/ops/case_b1d141af/run-worktree-prune.sh",
            self.option("ExecStart")[0])

    def test_sandbox_and_no_host_writes(self):
        for want in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict",
                     "ProtectHome=read-only", "UMask=0077"):
            self.assertIn(want, self.lines)
        self.assertEqual(self.option("ReadWritePaths"), [], "the host writes nothing; state lives in the container")

    def test_no_secret_on_any_command_line(self):
        for ln in self.lines:
            if ln.startswith(("ExecStart=", "ExecStartPre=")):
                self.assertNotIn("KEY", ln.upper())

    def test_timer_is_daily_and_catches_up(self):
        timer = TIMER.read_text()
        self.assertRegex(timer, r"(?m)^OnCalendar=\*-\*-\* \d\d:\d\d:00$")
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=paperclip-worktree-prune.service", timer)
        self.assertNotRegex(timer, r"(?m)^OnUnitActiveSec=")

    def test_env_example_is_nonsecret_with_a_loud_placeholder(self):
        text = ENV_EXAMPLE.read_text()
        self.assertIn("PAPERCLIP_API_KEY=REPLACE_ME_ON_THE_HOST", text)
        self.assertNotRegex(text, r"(?i)(ghp_|ghs_|github_pat_|eyJ[A-Za-z0-9_-]{20,}|pcp_[A-Za-z0-9]{12,})")
        listed = "\n".join(active_lines(text))
        for var in ("PAPERCLIP_WORKTREE_PRUNE_CONTAINER", "PAPERCLIP_API_URL",
                    "PAPERCLIP_COMPANY_ID", "PAPERCLIP_API_KEY", "PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE"):
            self.assertIn(var + "=", listed, var)

    def test_every_variable_the_wrapper_requires_is_in_the_example(self):
        wrapper = WRAPPER.read_text()
        required = set(re.findall(r'\[\[ -n "\$\{(PAPERCLIP_[A-Z_]+):-\}" \]\]', wrapper))
        required |= {"PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE"}
        listed = "\n".join(active_lines(ENV_EXAMPLE.read_text()))
        for var in sorted(required):
            self.assertIn(var + "=", listed, f"{var} is required by the wrapper but absent from the env template")


class BundleContract(unittest.TestCase):
    def test_builder_installer_and_tests_agree_on_the_payload(self):
        builder = BUILDER.read_text()
        listed = re.search(r"files=\(\n(.*?)\n\)", builder, re.S).group(1).split()
        self.assertEqual(listed, PAYLOAD)
        self.assertEqual(listed, sorted(listed), "payload is kept sorted")
        installer = INSTALLER.read_text()
        required = re.search(r"for source in (.+?); do", installer).group(1).split()
        self.assertEqual(required, PAYLOAD)

    def test_release_manifest_covers_exactly_the_installed_bytes(self):
        installer = INSTALLER.read_text()
        gen = [ln for ln in installer.splitlines() if "sha256sum " in ln and " > " in ln and "--check" not in ln]
        self.assertEqual(len(gen), 1)
        covered = gen[0].split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
        self.assertEqual(covered, INSTALLED)

    def test_installer_verifies_the_bundle_before_installing(self):
        installer = INSTALLER.read_text()
        check = installer.index('/usr/bin/sha256sum --check --strict SHA256SUMS)')
        self.assertLess(check, installer.index('RELEASE_DIR="$DEST'))
        self.assertLess(installer.index("bash -n"), installer.index('RELEASE_DIR="$DEST'))
        # No code path force-installs over an existing release.
        self.assertIn('[[ ! -e "$RELEASE_DIR" ]] || refuse', installer)

    def test_installer_never_enables_or_starts_anything(self):
        for line in active_lines(INSTALLER.read_text()):
            if line.lstrip().startswith("printf"):
                continue
            self.assertNotRegex(line, r"systemctl|--apply|git worktree", line)

    def test_scripts_are_executable(self):
        for path in (BUILDER, INSTALLER, WRAPPER):
            self.assertTrue(path.stat().st_mode & 0o111, f"{path.name} is not executable")


class InstallMechanism(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        td = pathlib.Path(cls.tmp.name)
        cls.td = td
        cls.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                   "GIT_CONFIG_GLOBAL": str(td / "gitconfig"), "GIT_CONFIG_SYSTEM": os.devnull}
        cls.env.pop("BASH_ENV", None)
        cls.repo = td / "repo"
        cls.repo.mkdir()
        for rel in PAYLOAD + ["systemd/build-worktree-prune-bundle.sh"]:
            dst = cls.repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, dst)
            dst.chmod((ROOT / rel).stat().st_mode & 0o777)
        cls.git("init", "--quiet", "-b", "trusted")
        cls.git("add", "-A")
        cls.git("commit", "--quiet", "--no-verify", "-m", "reviewed")
        cls.ref = cls.git("rev-parse", "HEAD")
        cls.bundle = td / "bundle.tar"
        built = cls.build(cls.ref, cls.bundle)
        assert built.returncode == 0, built.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def git(cls, *args):
        r = subprocess.run(["git", *args], cwd=cls.repo, env=cls.env, capture_output=True, text=True)
        assert r.returncode == 0, f"git {args[0]}: {r.stderr}"
        return r.stdout.strip()

    @classmethod
    def build(cls, ref, out, trusted="trusted"):
        return subprocess.run(
            [str(cls.repo / "systemd" / "build-worktree-prune-bundle.sh"),
             "--source-ref", ref, "--trusted-line", trusted, "--output", str(out)],
            cwd=cls.repo, env=cls.env, capture_output=True, text=True)

    def install(self, root, bundle=None, ref=None, mode="1", extra_env=None):
        env = {**self.env, "WORKTREE_PRUNE_INSTALL_ROOT": str(root)}
        if mode is not None:
            env["WORKTREE_PRUNE_INSTALL_TEST_MODE"] = mode
        env.update(extra_env or {})
        return subprocess.run(
            [str(INSTALLER), "--source-ref", self.ref if ref is None else ref, "--bundle", str(bundle or self.bundle)],
            env=env, capture_output=True, text=True)

    def fresh_root(self):
        return pathlib.Path(tempfile.mkdtemp(dir=self.td))

    def release(self, root):
        return root / "usr/local/libexec/paperclip-worktree-prune" / self.ref

    def test_install_lays_out_the_release_and_the_manifest_verifies(self):
        root = self.fresh_root()
        r = self.install(root)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        rel = self.release(root)
        modes = {p: (rel / p).stat().st_mode & 0o777 for p in INSTALLED}
        self.assertEqual(modes["ops/case_b1d141af/run-worktree-prune.sh"], 0o555)
        for name in ("ops/case_b1d141af/terminal_worktree_prune.py",
                     "ops/case_b5184419/terminal_worktree_cache_reclaim.py", "REVISION", "SHA256SUMS"):
            self.assertEqual((rel / name).stat().st_mode & 0o777, 0o444, name)
        self.assertEqual((rel / "REVISION").read_text().strip(), self.ref)
        # Only the installed byte set is in the release: no installer, no unit sources.
        found = sorted(str(p.relative_to(rel)) for p in rel.rglob("*") if p.is_file())
        self.assertEqual(found, sorted(INSTALLED + ["SHA256SUMS"]))
        check = subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=rel,
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_units_are_rendered_to_the_reviewed_ref(self):
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        service = (root / "etc/systemd/system/paperclip-worktree-prune.service").read_text()
        self.assertNotIn("@SOURCE_REF@", service)
        self.assertIn(self.ref, service)
        exec_path = re.search(r"(?m)^ExecStart=\S+ \S+ \S+ \S+ (\S+)$", service).group(1)
        self.assertTrue((root / exec_path.lstrip("/")).is_file(), exec_path)
        self.assertEqual((root / "etc/systemd/system/paperclip-worktree-prune.timer").read_text(),
                         TIMER.read_text())

    def test_env_file_is_created_0600_and_never_overwritten(self):
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        env_file = root / "etc/paperclip/worktree-prune.env"
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(env_file.read_text(), ENV_EXAMPLE.read_text())
        env_file.write_text("PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE=7\n")
        env_file.chmod(0o600)
        # A second release pin keeps the operator's file.
        (self.repo / "systemd" / "worktree-prune.env.example").write_text("# changed\n")
        self.git("commit", "--quiet", "--no-verify", "-am", "second")
        ref2 = self.git("rev-parse", "HEAD")
        bundle2 = self.td / "bundle2.tar"
        self.assertEqual(self.build(ref2, bundle2).returncode, 0)
        r = self.install(root, bundle=bundle2, ref=ref2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(env_file.read_text(), "PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE=7\n")
        self.assertIn("PRESERVED", r.stdout)

    def test_env_file_with_loose_mode_is_refused(self):
        root = self.fresh_root()
        (root / "etc/paperclip").mkdir(parents=True)
        env_file = root / "etc/paperclip/worktree-prune.env"
        env_file.write_text("x=1\n")
        env_file.chmod(0o644)
        r = self.install(root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("expected 600", r.stderr)
        # Refused before any release byte or unit was written.
        self.assertFalse((root / "usr").exists())
        self.assertFalse((root / "etc/systemd").exists())
        self.assertEqual(env_file.read_text(), "x=1\n")

    def test_existing_release_is_a_refusal_not_an_overwrite(self):
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        marker = self.release(root) / "ops/case_b1d141af/terminal_worktree_prune.py"
        before = marker.read_bytes()
        r = self.install(root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(marker.read_bytes(), before)

    def test_tampered_bundle_installs_nothing(self):
        root = self.fresh_root()
        tampered = self.td / "tampered.tar"
        buf = io.BytesIO()
        with tarfile.open(self.bundle) as src, tarfile.open(fileobj=buf, mode="w") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read()
                if m.name == "ops/case_b1d141af/terminal_worktree_prune.py":
                    data += b"\n# injected\n"
                    m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
        tampered.write_bytes(buf.getvalue())
        r = self.install(root, bundle=tampered)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.release(root).exists())
        self.assertFalse((root / "etc/systemd/system/paperclip-worktree-prune.service").exists())

    def test_ref_must_match_the_bundle_revision(self):
        root = self.fresh_root()
        r = self.install(root, ref="0" * 40)
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not match", r.stderr)
        self.assertFalse((root / "usr").exists())

    def test_ref_must_be_a_full_commit_hash(self):
        for ref in ("main", "abc123", "G" * 40, ""):
            r = self.install(self.fresh_root(), ref=ref)
            self.assertEqual(r.returncode, 2, ref)

    def test_a_hostile_ref_cannot_steer_the_install_path(self):
        # A bundle whose REVISION and manifest agree with a path-shaped --source-ref
        # passes every integrity check, so only the 40-hex rule stops the install
        # from landing outside the release directory.
        stage = self.td / "hostile"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        with tarfile.open(self.bundle) as tar:
            tar.extractall(stage, filter="data")
        hostile = "../../../escaped"
        (stage / "REVISION").write_text(hostile + "\n")
        sums = subprocess.run(["sha256sum", *PAYLOAD, "REVISION"], cwd=stage, capture_output=True, text=True, check=True)
        (stage / "SHA256SUMS").write_text(sums.stdout)
        bundle = self.td / "hostile.tar"
        with tarfile.open(bundle, "w") as tar:
            for name in [*PAYLOAD, "REVISION", "SHA256SUMS"]:
                tar.add(stage / name, arcname=name)
        root = self.fresh_root()
        r = self.install(root, bundle=bundle, ref=hostile)
        self.assertEqual(r.returncode, 2)
        self.assertIn("40-hex", r.stderr)
        self.assertEqual(list(root.iterdir()), [])
        self.assertFalse((root.parent / "escaped").exists())

    def test_production_mode_refuses_a_test_root_in_the_source(self):
        # The root-only branch cannot execute without root, so pin its text: the
        # test-only install root must be refused outright in production mode.
        installer = INSTALLER.read_text()
        prod = installer.split("  0)\n", 1)[1].split("    OWNER=(-o root -g root)", 1)[0]
        self.assertIn('[[ $EUID -eq 0 ]] || refuse', prod)
        self.assertIn('[[ -z "${WORKTREE_PRUNE_INSTALL_ROOT:-}" ]] || refuse', prod)

    def test_production_mode_refuses_a_test_root(self):
        root = self.fresh_root()
        r = self.install(root, mode=None)
        self.assertEqual(r.returncode, 2)
        self.assertRegex(r.stderr, r"must run as root|test-only")
        self.assertFalse((root / "usr").exists())

    def test_builder_refuses_a_commit_that_is_not_on_the_trusted_line(self):
        self.git("checkout", "--quiet", "-b", "unreviewed")
        try:
            (self.repo / "stray.txt").write_text("x\n")
            self.git("add", "stray.txt")
            self.git("commit", "--quiet", "--no-verify", "-m", "unreviewed")
            stray = self.git("rev-parse", "HEAD")
        finally:
            self.git("checkout", "--quiet", "trusted")
        r = self.build(stray, self.td / "stray.tar")
        self.assertEqual(r.returncode, 2)
        self.assertIn("never merged by review", r.stderr)
        self.assertFalse((self.td / "stray.tar").exists())

    def test_installed_wrapper_finds_all_four_modules_from_the_installed_layout(self):
        # Run the INSTALLED wrapper (not the repo copy) with a fake engine that
        # captures the tar it is streamed: all four reviewed modules, nothing else.
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        wrapper = self.release(root) / "ops/case_b1d141af/run-worktree-prune.sh"
        engine = self.td / "engine.py"
        capture = self.td / "captured.tar"
        engine.write_text(f"#!/usr/bin/env python3\nimport sys\nopen({str(capture)!r}, 'wb').write(sys.stdin.buffer.read())\n")
        engine.chmod(0o755)
        env = {"PATH": os.environ["PATH"], "HOME": str(self.td),
               "PAPERCLIP_WORKTREE_PRUNE_ENGINE": str(engine),
               "PAPERCLIP_WORKTREE_PRUNE_CONTAINER": "paperclip",
               "PAPERCLIP_API_URL": "http://x.invalid", "PAPERCLIP_API_KEY": "k",
               "PAPERCLIP_COMPANY_ID": "c",
               # The installed wrapper renders the host verdict for real; the
               # fixture base is .invalid, so opt into the hatch explicitly.
               "PAPERCLIP_API_ALLOWLIST_TEST": "1"}
        r = subprocess.run([str(wrapper)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with tarfile.open(capture) as tar:
            self.assertEqual(sorted(tar.getnames()),
                             ["lib/paperclip_api_allowlist.py",
                              "ops/case_b1d141af/terminal_worktree_prune.py",
                              "ops/case_b5184419/terminal_worktree_cache_reclaim.py",
                              "ops/lib/atomic_state.py"])


if __name__ == "__main__":
    unittest.main()
