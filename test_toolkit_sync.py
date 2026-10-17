"""Cold, credential-free controls for this repo's selected vendor contract."""
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
TOOLKIT = json.loads((ROOT / "vendor/offline-ci-contract.json").read_text())["toolkit"]
TAG = TOOLKIT["tag"]
COMMIT = TOOLKIT["commit"]
FIX_COMMIT = "d0324d0e5dd5a604a375e63a45eb5aa00e507059"
FIX_ARCHIVE_SHA256 = "390032e7032e9041e82497b4fa90be086fea12df791653c0d5e6818a83d7f791"
FIX_GUARD_SHA256 = "40fdea4eebad62c5ed20293fdb5913f3664daac06dae6584b757f6bf2272bb76"
# sibling_guard.sh is tracked in this repo (toolkit-tracked allowlist
# survivor, canonical for the default-branch resolution fix), so the guard
# subject below is the tracked bytes, no longer the pinned toolkit copy.
# FIX_GUARD_SHA256 still records the pinned toolkit bytes at FIX_COMMIT for
# the pin-history assertions above.


class ToolkitSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_SCRATCH_DIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "vendor").mkdir()
        (self.root / "bin").mkdir()
        for name in ("sync-toolkit.sh", "check-toolkit-vendored-tracked.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.root / "scripts" / name)
        (self.root / "vendor/toolkit-files.txt").write_text("old-name.txt|canonical-name.txt\n")
        (self.root / "vendor/toolkit-tracked-allowlist.txt").write_text("# no tracked fixture\n")
        # A real tarball exercises checksum validation, mapped-source lookup,
        # extraction and copying. Only the download is replaced, never a gate.
        self.archive = self.root / "fixture.tar.gz"
        self.pack({"LICENSE": b"fixture license\n", "canonical-name.txt": b"tag bytes\n"})
        curl = self.root / "bin/curl"
        curl.write_text("""#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${TOOLKIT_CURL_ARGV_LOG:-}" ]]; then
  printf '%s\\0' "$@" >> "$TOOLKIT_CURL_ARGV_LOG"
fi
url= dest= retry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) dest=$2; shift 2 ;;
    --retry) retry=$2; shift 2 ;;
    --retry-max-time|--retry-delay|--connect-timeout|--max-time) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
if [[ "${TOOLKIT_CURL_FAIL_FIRST:-0}" == 1 && ! -f "$dest.failed" ]]; then
  touch "$dest.failed"
  echo "curl: (22) HTTP response code said error" >&2
  [[ "$retry" -gt 0 ]] || exit 22
fi
source=$TOOLKIT_FIXTURE_ARCHIVE
if [[ "${url##*/}" == "${TOOLKIT_FIXTURE_LEGACY_COMMIT:-unset}.tar.gz" ]]; then
  source=$TOOLKIT_FIXTURE_LEGACY_ARCHIVE
fi
cp "$source" "$dest"
""")
        curl.chmod(0o755)
        self.env = dict(os.environ)
        for key in ("TOOLKIT_TAG", "TOOLKIT_COMMIT", "TOOLKIT_SHA256", "TOOLKIT_URL",
                    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "BASH_ENV"):
            self.env.pop(key, None)
        self.env.update(PATH=f"{self.root / 'bin'}:/usr/bin:/bin",
                        TOOLKIT_FIXTURE_ARCHIVE=str(self.archive), TMPDIR=str(self.root))

    def pack(self, files):
        with tarfile.open(self.archive, "w:gz") as archive:
            for path, content in files.items():
                member = tarfile.TarInfo(f"paperclip-operator-toolkit-{COMMIT}/{path}")
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
        checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        (self.root / "vendor/toolkit-pins.txt").write_text(f"{TAG} {COMMIT} {checksum}\n")

    def run_sync(self, *args):
        return subprocess.run(["/usr/bin/bash", "scripts/sync-toolkit.sh", *args],
                              cwd=self.root, env=self.env, capture_output=True, text=True,
                              timeout=20)

    def legacy_fixture(self):
        commit = "7c4b39a2157ceb122ae50c7f554cc43c6891c829"
        archive_path = self.root / "legacy-fixture.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for path, content in {"LICENSE": b"fixture license\n",
                                  "private-needle.txt": b"pre-scrub bytes\n"}.items():
                member = tarfile.TarInfo(f"paperclip-operator-toolkit-{commit}/{path}")
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
        checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        pins = self.root / "vendor/toolkit-pins.txt"
        pins.write_text(pins.read_text() + f"v0.2.2 {commit} {checksum}\n")
        manifest = self.root / "vendor/toolkit-files.txt"
        manifest.write_text(manifest.read_text() +
                            "private-consumer.txt|private-needle.txt|v0.2.2\n")
        self.env.update(TOOLKIT_FIXTURE_LEGACY_COMMIT=commit,
                        TOOLKIT_FIXTURE_LEGACY_ARCHIVE=str(archive_path))
        return pins, checksum

    def test_explicit_private_exception_keeps_pre_scrub_bytes(self):
        self.legacy_fixture()
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "old-name.txt").read_bytes(), b"tag bytes\n")
        self.assertEqual((self.root / "private-consumer.txt").read_bytes(),
                         b"pre-scrub bytes\n")

    def test_failed_default_never_falls_back_to_legacy(self):
        pins, _ = self.legacy_fixture()
        rows = pins.read_text().splitlines()
        rows[0] = f"{TAG} {COMMIT} {'0' * 64}"
        pins.write_text("\n".join(rows) + "\n")
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"checksum mismatch for commit {COMMIT}", result.stderr)
        self.assertFalse((self.root / "old-name.txt").exists())
        self.assertFalse((self.root / "private-consumer.txt").exists())

    def test_exception_checksum_is_verified_before_any_copy(self):
        pins, checksum = self.legacy_fixture()
        pins.write_text(pins.read_text().replace(checksum, "0" * 64))
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse((self.root / "old-name.txt").exists())
        self.assertFalse((self.root / "private-consumer.txt").exists())

    def test_unknown_exception_pin_refuses(self):
        manifest = self.root / "vendor/toolkit-files.txt"
        manifest.write_text(manifest.read_text() + "private.txt|needle.txt|unknown\n")
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no immutable pin", result.stderr)
        self.assertFalse((self.root / "old-name.txt").exists())

    def test_default_pin_restores_alias_bytes_in_a_cold_tree(self):
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "old-name.txt").read_bytes(), b"tag bytes\n")

    def test_unknown_tag_refuses_without_restoring(self):
        self.assertNotEqual(self.run_sync("--tag", "v-does-not-exist").returncode, 0)
        self.assertFalse((self.root / "old-name.txt").exists())

    def test_wrong_checksum_refuses_without_restoring(self):
        (self.root / "vendor/toolkit-pins.txt").write_text(f"{TAG} {COMMIT} {'0' * 64}\n")
        self.assertNotEqual(self.run_sync().returncode, 0)
        self.assertFalse((self.root / "old-name.txt").exists())

    def test_missing_mapped_source_refuses(self):
        self.pack({"LICENSE": b"fixture license\n"})
        self.assertNotEqual(self.run_sync().returncode, 0)
        self.assertFalse((self.root / "old-name.txt").exists())

    def test_missing_license_refuses(self):
        self.pack({"canonical-name.txt": b"tag bytes\n"})
        self.assertNotEqual(self.run_sync().returncode, 0)
        self.assertFalse((self.root / "old-name.txt").exists())

    def test_archive_download_carries_explicit_time_and_retry_bounds(self):
        # A slow or briefly unavailable archive endpoint must remain bounded,
        # while transient GitHub 5xx responses get a small retry window.
        log = self.root / "curl-argv.log"
        self.env.update(TOOLKIT_CURL_ARGV_LOG=str(log))
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = log.read_bytes().split(b"\0")
        self.assertIn(b"--max-time", argv)
        self.assertIn(b"--connect-timeout", argv)
        self.assertIn(b"--retry", argv)
        self.assertIn(b"--retry-max-time", argv)

    def test_transient_archive_failure_retries_before_restoring(self):
        curl = self.root / "bin/curl"
        curl.write_text(curl.read_text().replace(
            'cp "$source" "$dest"',
            'if [[ "${TOOLKIT_CURL_FAIL_FIRST:-0}" == 1 && ! -f "$dest.failed" ]]; then\n'
            '  touch "$dest.failed"\n'
            '  echo "curl: (22) HTTP response code said error" >&2\n'
            '  exit 22\n'
            'fi\ncp "$source" "$dest"'))
        curl.chmod(0o755)
        self.env["TOOLKIT_CURL_FAIL_FIRST"] = "1"
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "old-name.txt").read_bytes(), b"tag bytes\n")

    def test_timed_out_download_fails_without_restoring(self):
        # case_e107fefc: curl exit 28 (operation timed out, i.e. what a hung
        # endpoint produces once --max-time fires) fails the sync loudly and
        # restores nothing, never a partial tree that reads as synced.
        curl = self.root / "bin/curl"
        curl.write_text("#!/usr/bin/env bash\necho 'curl: (28) Operation timed out' >&2\nexit 28\n")
        curl.chmod(0o755)
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "old-name.txt").exists())

    def test_selected_immutable_pin_contains_body_attribution_fix(self):
        # Pin/default drift must not silently restore the old false-positive guard.
        self.assertEqual(COMMIT, FIX_COMMIT,
                         "selected toolkit pin predates the body-attribution fix")
        self.assertEqual(TOOLKIT["sha256"], FIX_ARCHIVE_SHA256)
        pins = [line.split() for line in (ROOT / "vendor/toolkit-pins.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")]
        self.assertEqual([row for row in pins if row[0] == TAG],
                         [[TAG, FIX_COMMIT, FIX_ARCHIVE_SHA256]])

    def prepare_guard(self):
        fixture = ROOT / "sibling_guard.sh"
        self.assertTrue(fixture.is_file(), "sibling_guard.sh is tracked in this repo")
        subject = fixture.read_bytes()
        # Tracked survivor: allowlisted here, absent from the restored-input
        # closure there — sync must restore around it in a real checkout.
        allowlist = (ROOT / "vendor/toolkit-tracked-allowlist.txt").read_text().splitlines()
        self.assertIn("sibling_guard.sh", allowlist)
        contract = json.loads((ROOT / "vendor/offline-ci-contract.json").read_text())
        self.assertNotIn("sibling_guard.sh", contract["restored_files_sha256"])
        # Repack only the needed immutable bytes, with a fixture checksum. The
        # real archive checksum is asserted separately, not relabelled as this tar.
        self.pack({"LICENSE": b"fixture license\n", "sibling_guard.sh": subject})
        entries = [line for line in (ROOT / "vendor/toolkit-files.txt").read_text().splitlines()
                   if line.split("|", 1)[0] == "sibling_guard.sh"]
        self.assertEqual(entries, ["sibling_guard.sh"],
                         "guard must consume the selected default, not an old compatibility pin")
        (self.root / "vendor/toolkit-files.txt").write_text(entries[0] + "\n")
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.guard = self.root / "sibling_guard.sh"
        self.assertEqual(self.guard.read_bytes(), subject)

    def test_sync_restores_tracked_guard_subject_bytes(self):
        self.prepare_guard()
        subject = (ROOT / "sibling_guard.sh").read_bytes()
        self.assertEqual(hashlib.sha256(self.guard.read_bytes()).hexdigest(),
                         hashlib.sha256(subject).hexdigest(),
                         "sync must restore the guard subject bytes unchanged")

    def prepare_guard_world(self):
        self.prepare_guard()
        self.pulls = []
        self.pulls_status = 200
        self.runs_status = 200
        self.runs_body = [{"runId": "run-me", "agentId": "agent-me", "status": "running",
                           "adapterType": "claude_local", "finishedAt": None,
                           "startedAt": "2026-08-25T10:00:00.000Z",
                           "createdAt": "2026-08-25T10:00:00.000Z"}]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/repos/stub/acceptance/pulls":
                    code, body = owner.pulls_status, owner.pulls
                elif path in ("/api/issues/CASE-1001", "/api/issues/case-clear"):
                    code, body = 200, {"id": "case-clear", "identifier": "CASE-1001"}
                elif path == "/api/companies/stub-company/issues":
                    code, body = 200, [{"id": "case-clear", "identifier": "CASE-1001"}]
                elif path == "/api/issues/case-clear/runs":
                    code, body = owner.runs_status, owner.runs_body
                else:
                    code, body = 404, {"error": "unexpected fixture route"}
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(stop_server)
        base = f"http://127.0.0.1:{server.server_port}"
        # Node may be installed by setup-node/NVM rather than in /usr/bin.
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the real guard")
        guard_bin = self.root / "guard-bin"
        guard_bin.mkdir()
        (guard_bin / "node").symlink_to(node)
        # Do not inherit live credentials, broker/shim settings or toolkit overrides.
        self.guard_env = {
            "PATH": f"{guard_bin}:/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "suite", "GIT_AUTHOR_EMAIL": "suite@example.invalid",
            "GIT_COMMITTER_NAME": "suite", "GIT_COMMITTER_EMAIL": "suite@example.invalid",
            "PAPERCLIP_API_URL": base, "PAPERCLIP_API_KEY": "stub-key",
            "PAPERCLIP_COMPANY_ID": "stub-company", "PAPERCLIP_AGENT_ID": "agent-me",
            "PAPERCLIP_RUN_ID": "run-me", "GH_API_URL": base, "GH_TOKEN": "stub-gh-token",
        }
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        origin = self.root / "origin.git"
        self.git("init", "--quiet", "--bare", "-b", "main", str(origin))
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "credential.helper", "")
        (self.checkout / "file.txt").write_text("seed\n")
        self.git("add", "file.txt")
        self.git("commit", "--quiet", "-m", "seed")
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "--quiet", "-u", "origin", "main")
        self.git("checkout", "--quiet", "-b", "acceptance-work")
        (self.checkout / "file.txt").write_text("seed\nnew work\n")
        self.git("commit", "--quiet", "-am", "unlanded work")
        # This real commit is deliberately not on origin/main: prepush must not
        # pass/refuse because of the already-landed detector instead of the PR.
        self.head_sha = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        return subprocess.run(["/usr/bin/git", *args], cwd=self.checkout,
                              env=self.guard_env, capture_output=True, text=True,
                              check=True, timeout=10)

    def run_guard_case(self, body="", *, head="different-work", title="unrelated work",
                       phase="start", expected=0):
        self.pulls = [{"number": 77, "title": title, "body": body,
                       "state": "open", "draft": False,
                       "head": {"ref": head, "sha": self.head_sha if head == "acceptance-work"
                                else "a" * 40},
                       "base": {"ref": "main"}}]
        return self.run_guard(phase=phase, expected=expected)

    def run_guard(self, *, phase="start", expected=0):
        result = subprocess.run(["/usr/bin/bash", str(self.guard), "CASE-1001",
                                 "--repo=stub/acceptance", f"--phase={phase}"],
                                cwd=self.checkout, env=self.guard_env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_no_pr_is_clear_with_all_detectors_running(self):
        self.prepare_guard_world()
        self.run_guard()

    def test_different_head_related_body_mention_is_not_ownership(self):
        self.prepare_guard_world()
        for phase in ("start", "prepush"):
            with self.subTest(phase=phase):
                self.run_guard_case("Related: CASE-1001\nRefs: CASE-1002\n", phase=phase)

    def test_different_head_procedure_body_mention_is_not_ownership(self):
        self.prepare_guard_world()
        for phase in ("start", "prepush"):
            with self.subTest(phase=phase):
                self.run_guard_case("Procedure: run ./sibling_guard.sh CASE-1001 before pushing.\n",
                                    phase=phase)

    def test_different_head_mid_sentence_refs_is_not_ownership(self):
        self.prepare_guard_world()
        for phase in ("start", "prepush"):
            with self.subTest(phase=phase):
                self.run_guard_case("Related work (see Refs: CASE-1001 for procedure).\n",
                                    phase=phase)

    def test_different_head_matching_refs_line_is_a_duplicate(self):
        self.prepare_guard_world()
        for phase in ("start", "prepush"):
            with self.subTest(phase=phase):
                self.run_guard_case("Description\nRefs: CASE-1001\n", phase=phase, expected=1)

    def test_title_and_branch_attribution_still_refuse_duplicates(self):
        self.prepare_guard_world()
        for kwargs in ({"title": "fix CASE-1001"}, {"head": "CASE-1001-other"}):
            with self.subTest(kwargs=kwargs):
                self.run_guard_case(expected=1, **kwargs)

    def test_same_head_own_and_unclaimed_prs_remain_clear(self):
        self.prepare_guard_world()
        for body in ("Refs: CASE-1001\n", "Refs: CASE-1001, CASE-1002\n",
                     "no refs line", "Related (see Refs: CASE-1002)"):
            for phase in ("start", "prepush"):
                with self.subTest(body=body, phase=phase):
                    self.run_guard_case(body, head="acceptance-work", phase=phase)

    def test_same_head_foreign_claim_refuses_only_at_prepush(self):
        self.prepare_guard_world()
        self.run_guard_case("Refs: CASE-1002\n", head="acceptance-work", phase="start")
        self.run_guard_case("Refs: CASE-1002\n", head="acceptance-work",
                            phase="prepush", expected=1)

    def test_pr_api_errors_are_indeterminate_not_clear(self):
        self.prepare_guard_world()
        for status, body in ((403, {"message": "denied"}), (200, "not json")):
            with self.subTest(status=status, body=body):
                self.pulls_status = status
                self.pulls = body
                self.run_guard(expected=3)

    def test_run_api_errors_are_indeterminate_not_clear(self):
        self.prepare_guard_world()
        for status, body in ((500, {"error": "failed"}), (200, "not json")):
            with self.subTest(status=status, body=body):
                self.runs_status, self.runs_body = status, body
                self.run_guard_case(expected=3)


if __name__ == "__main__":
    unittest.main()
