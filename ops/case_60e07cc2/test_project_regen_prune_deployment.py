#!/usr/bin/env python3
# case_60e07cc2: deployment contract for the hourly project regen pruner.
# Offline, no credentials, no host writes. Pins (1) the unit's bounds, the
# unprivileged user and the mode-bit rule, (2) that the bundle builder, the
# installer and the release manifest agree on one byte set, and (3) the
# mechanism itself: a real bundle built from a real git commit is installed
# by the real installer into a temp root, and the installed tool resolves
# its reclaim import from the installed layout.

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE = ROOT / "systemd" / "paperclip-project-regen-prune.service"
TIMER = ROOT / "systemd" / "paperclip-project-regen-prune.timer"
ENV_EXAMPLE = ROOT / "systemd" / "project-regen-prune.env.example"
BUILDER = ROOT / "systemd" / "build-project-regen-prune-bundle.sh"
INSTALLER = ROOT / "systemd" / "install-project-regen-prune.sh"
TOOL = ROOT / "ops" / "case_60e07cc2" / "project_worktree_regen_prune.py"

PAYLOAD = [
    "lib/paperclip_api_allowlist.py",
    "ops/case_60e07cc2/project_worktree_regen_prune.py",
    "ops/case_b5184419/terminal_worktree_cache_reclaim.py",
    "ops/lib/atomic_state.py",
    "systemd/install-project-regen-prune.sh",
    "systemd/paperclip-project-regen-prune.service",
    "systemd/paperclip-project-regen-prune.timer",
    "systemd/project-regen-prune.env.example",
]
# What the installer puts in the release directory (bundle-scope systemd/* is
# verified but never installed).
INSTALLED = [
    "lib/paperclip_api_allowlist.py",
    "ops/lib/atomic_state.py",
    "ops/case_b5184419/terminal_worktree_cache_reclaim.py",
    "ops/case_60e07cc2/project_worktree_regen_prune.py",
    "REVISION",
]
RELEASE_BASE = "/usr/local/libexec/paperclip-project-regen-prune/@SOURCE_REF@"


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
        exec_start = self.option("ExecStart")
        self.assertEqual(len(exec_start), 1)
        m = re.match(r"/usr/bin/timeout --signal=TERM --kill-after=(\S+) (\S+) /usr/bin/python3 ", exec_start[0])
        self.assertIsNotNone(m, exec_start[0])
        tool_timeout = seconds(m.group(2))
        unit_bound = seconds(self.option("TimeoutStartSec")[0])
        self.assertLess(tool_timeout + seconds(m.group(1)), unit_bound,
                        "tool timeout plus kill-after must fit inside TimeoutStartSec")

    def test_apply_is_literal_in_the_unit_and_absent_from_env(self):
        # --apply lives literally in these reviewed unit bytes; the env file
        # (the only thing the operator edits) carries the volume knob only.
        self.assertIn("--apply", self.option("ExecStart")[0])
        env_text = "\n".join(active_lines(ENV_EXAMPLE.read_text()))
        self.assertNotIn("APPLY", env_text.upper())
        for ln in self.lines:
            if ln.startswith("Environment="):
                self.assertNotIn("APPLY", ln.upper())

    def test_default_cap_is_observe_only_and_env_file_overrides(self):
        env_lines = [ln for ln in self.lines if ln.startswith(("Environment=", "EnvironmentFile="))]
        self.assertEqual(env_lines[0], "Environment=PROJECT_REGEN_PRUNE_MAX_REMOVE=0")
        self.assertEqual(env_lines[1], "EnvironmentFile=/etc/paperclip/project-regen-prune.env")

    def test_example_ships_the_cap_at_zero(self):
        self.assertIn("PROJECT_REGEN_PRUNE_MAX_REMOVE=0", active_lines(ENV_EXAMPLE.read_text()))

    def test_preflight_checks_installed_release_manifest_and_tools(self):
        pre = self.option("ExecStartPre")
        self.assertIn(f"/usr/bin/sha256sum --check --strict {RELEASE_BASE}/SHA256SUMS", pre)
        self.assertIn("/usr/bin/test -x /usr/bin/python3", pre)
        self.assertIn("/usr/bin/test -x /usr/bin/timeout", pre)
        self.assertIn("/usr/bin/test -d /var/lib/paperclip/project-regen-prune", pre)

    def test_exec_start_runs_the_installed_tool(self):
        start = self.option("ExecStart")[0]
        self.assertIn(f"{RELEASE_BASE}/ops/case_60e07cc2/project_worktree_regen_prune.py", start)
        self.assertIn("--max-remove ${PROJECT_REGEN_PRUNE_MAX_REMOVE}", start)

    def test_unprivileged_user_and_host_write_sandbox(self):
        # The tool deletes under /home and writes manifests: ProtectHome
        # must NOT be read-only, and the user must NOT be root (root running
        # git inside agent-writable repos is a code-execution path).
        self.assertEqual(self.option("User"), ["ubuntu"])
        self.assertNotIn("User=root", self.lines)
        for want in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict",
                     "UMask=0077"):
            self.assertIn(want, self.lines)
        self.assertNotIn("ProtectHome=read-only", self.lines)

    def test_host_writes_are_allowlisted_under_strict(self):
        # Error pin: ProtectSystem=strict mounts everything read-only at
        # start, so the manifest dir and the pruned projects root must be
        # allowlisted — without ReadWritePaths even the first cap-0 run
        # fails on the manifest write.
        self.assertIn("ProtectSystem=strict", self.lines)
        paths = self.option("ReadWritePaths")
        self.assertIn("/var/lib/paperclip/project-regen-prune", paths)
        self.assertIn(
            "/home/ubuntu/stacks/paperclip/data/instances/default/projects",
            paths)

    def test_no_secret_on_any_command_line(self):
        for ln in self.lines:
            if ln.startswith(("ExecStart=", "ExecStartPre=")):
                for needle in ("KEY", "TOKEN", "PASSWORD"):
                    self.assertNotIn(needle, ln.upper())

    def test_timer_is_hourly_at_37_without_catch_up(self):
        timer = TIMER.read_text()
        self.assertRegex(timer, r"(?m)^OnCalendar=\*:37$")
        self.assertIn("Persistent=false", timer)
        self.assertIn("Unit=paperclip-project-regen-prune.service", timer)
        self.assertNotRegex(timer, r"(?m)^OnUnitActiveSec=")

    def test_env_example_is_nonsecret_with_a_loud_placeholder(self):
        text = ENV_EXAMPLE.read_text()
        self.assertIn("PAPERCLIP_API_KEY=REPLACE_ME_ON_THE_HOST", text)
        self.assertNotRegex(text, r"(?i)(ghp_|ghs_|github_pat_|eyJ[A-Za-z0-9_-]{20,}|pcp_[A-Za-z0-9]{12,})")
        listed = "\n".join(active_lines(text))
        for var in ("PAPERCLIP_API_URL", "PAPERCLIP_COMPANY_ID",
                    "PAPERCLIP_API_KEY", "PROJECT_REGEN_PRUNE_MAX_REMOVE"):
            self.assertIn(var + "=", listed, var)

    def test_every_variable_the_tool_requires_is_in_the_example(self):
        listed = "\n".join(active_lines(ENV_EXAMPLE.read_text()))
        for var in ("PAPERCLIP_API_URL", "PAPERCLIP_COMPANY_ID", "PAPERCLIP_API_KEY"):
            self.assertIn(var + "=", listed, f"{var} is required by the tool but absent from the env template")


class BundleContract(unittest.TestCase):
    def test_builder_installer_and_tests_agree_on_the_payload(self):
        builder = BUILDER.read_text()
        listed = re.search(r"files=\(\n(.*?)\n\)", builder, re.S).group(1).split()
        self.assertEqual(listed, PAYLOAD)
        self.assertEqual(listed, sorted(listed), "payload is kept sorted")
        installer = INSTALLER.read_text()
        required = re.search(r"for source in (.+?); do", installer).group(1).split()
        self.assertEqual(required, PAYLOAD)
        here = pathlib.Path(__file__).read_text()
        for member in PAYLOAD:
            self.assertIn(member, here, f"deployment suite forgot payload member {member}")

    def test_release_manifest_covers_exactly_the_installed_bytes(self):
        installer = INSTALLER.read_text()
        gen = [ln for ln in installer.splitlines() if "sha256sum " in ln and " > " in ln and "--check" not in ln]
        self.assertEqual(len(gen), 1)
        covered = gen[0].split("sha256sum ", 1)[1].split(" > ", 1)[0].split()
        self.assertEqual(covered, INSTALLED)

    def test_installer_verifies_the_bundle_before_installing(self):
        installer = INSTALLER.read_text()
        check = installer.index("/usr/bin/sha256sum --check --strict SHA256SUMS)")
        self.assertLess(check, installer.index('RELEASE_DIR="$DEST'))
        self.assertLess(installer.index("Python syntax preflight failed"), installer.index('RELEASE_DIR="$DEST'))
        # No code path force-installs over an existing release.
        self.assertIn('[[ ! -e "$RELEASE_DIR" ]] || refuse', installer)

    def test_installer_never_enables_or_starts_anything(self):
        for line in active_lines(INSTALLER.read_text()):
            if line.lstrip().startswith("printf"):
                continue
            self.assertNotRegex(line, r"systemctl|--apply|git worktree", line)

    def test_scripts_are_executable(self):
        for path in (BUILDER, INSTALLER):
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
        for rel in PAYLOAD + ["systemd/build-project-regen-prune-bundle.sh"]:
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
            [str(cls.repo / "systemd" / "build-project-regen-prune-bundle.sh"),
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
        return root / "usr/local/libexec/paperclip-project-regen-prune" / self.ref

    def test_install_lays_out_the_release_and_the_manifest_verifies(self):
        root = self.fresh_root()
        r = self.install(root)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        rel = self.release(root)
        for name in ("ops/case_60e07cc2/project_worktree_regen_prune.py",
                     "ops/case_b5184419/terminal_worktree_cache_reclaim.py", "REVISION"):
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
        service = (root / "etc/systemd/system/paperclip-project-regen-prune.service").read_text()
        self.assertIn(f"/usr/local/libexec/paperclip-project-regen-prune/{self.ref}/", service)
        self.assertNotIn("@SOURCE_REF@", service)
        timer = (root / "etc/systemd/system/paperclip-project-regen-prune.timer").read_text()
        self.assertIn("OnCalendar=*:37", timer)

    def test_state_dir_is_created_for_the_service_user(self):
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        state = root / "var/lib/paperclip/project-regen-prune"
        self.assertTrue(state.is_dir(), "unit ExecStartPre tests this dir; install must create it")

    def test_env_file_is_created_0600_and_never_overwritten(self):
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        env_file = root / "etc/paperclip/project-regen-prune.env"
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        # A pre-provisioned env file survives a fresh install untouched.
        root2 = self.fresh_root()
        env_dir = root2 / "etc/paperclip"
        env_dir.mkdir(parents=True)
        provisioned = env_dir / "project-regen-prune.env"
        provisioned.write_text("OPERATOR-EDITED\n")
        os.chmod(provisioned, 0o600)
        self.assertEqual(self.install(root2).returncode, 0)
        self.assertEqual(provisioned.read_text(), "OPERATOR-EDITED\n")

    def test_env_file_with_loose_mode_is_refused(self):
        root = self.fresh_root()
        env_dir = root / "etc/paperclip"
        env_dir.mkdir(parents=True)
        loose = env_dir / "project-regen-prune.env"
        loose.write_text("x=1\n")
        os.chmod(loose, 0o644)
        r = self.install(root)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.release(root).exists(), "a refused install lays out nothing")

    def test_existing_release_is_a_refusal_not_an_overwrite(self):
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        second = self.install(root)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr + second.stdout)

    def test_tampered_bundle_installs_nothing(self):
        root = self.fresh_root()
        raw = self.bundle.read_bytes()
        tampered = self.td / "tampered.tar"
        # Flip a byte deep in the tar (past headers) to break the checksum.
        blob = bytearray(raw)
        blob[len(blob) // 2] ^= 0x01
        tampered.write_bytes(bytes(blob))
        r = self.install(root, bundle=tampered)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.release(root).exists())

    def test_ref_must_match_the_bundle_revision(self):
        root = self.fresh_root()
        r = self.install(root, ref="0" * 40)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match", r.stderr + r.stdout)

    def test_ref_must_be_a_full_commit_hash(self):
        root = self.fresh_root()
        r = self.install(root, ref="abc123")
        self.assertNotEqual(r.returncode, 0)

    def test_a_hostile_ref_cannot_steer_the_install_path(self):
        root = self.fresh_root()
        evil = "..%2f.." + "0" * 34
        r = self.install(root, ref=evil)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((root / "usr/local/libexec/paperclip-project-regen-prune" / evil).exists())

    def test_production_mode_refuses_a_test_root_in_the_source(self):
        # The root-only branch cannot execute without root, so pin its text:
        # the test-only install root must be refused outright in production.
        installer = INSTALLER.read_text()
        prod = installer.split("  0)\n", 1)[1].split("    OWNER=(-o root -g root)", 1)[0]
        self.assertIn("[[ $EUID -eq 0 ]] || refuse", prod)
        self.assertIn('[[ -z "${WORKTREE_PRUNE_INSTALL_ROOT:-}" ]] || refuse', prod)

    def test_production_mode_refuses_a_test_root(self):
        root = self.fresh_root()
        r = self.install(root, mode=None)
        self.assertEqual(r.returncode, 2)
        self.assertRegex(r.stderr, r"must run as root|test-only")
        self.assertFalse((root / "usr").exists())

    def test_builder_refuses_a_commit_that_is_not_on_the_trusted_line(self):
        self.git("checkout", "--quiet", "-b", "side")
        (self.repo / "extra").write_text("x")
        self.git("add", "-A")
        self.git("commit", "--quiet", "--no-verify", "-m", "side work")
        side = self.git("rev-parse", "HEAD")
        out = self.td / "side.tar"
        built = self.build(side, out)
        self.assertNotEqual(built.returncode, 0)
        self.assertIn("never merged by review", built.stderr + built.stdout)
        self.git("checkout", "--quiet", "trusted")

    def test_installed_tool_resolves_its_reclaim_import(self):
        root = self.fresh_root()
        self.assertEqual(self.install(root).returncode, 0)
        rel = self.release(root)
        probe = subprocess.run(
            ["python3", "-c",
             "import sys; sys.path.insert(0, 'ops/case_60e07cc2'); "
             "import project_worktree_regen_prune as t; "
             "print(t.to_host('/paperclip/a'), t.REGEN_NAMES[0])"],
            cwd=rel, capture_output=True, text=True, env=self.env)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(
            probe.stdout.strip(),
            "/home/ubuntu/stacks/paperclip/data/a node_modules")


if __name__ == "__main__":
    unittest.main(verbosity=2)
