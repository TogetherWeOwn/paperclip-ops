#!/usr/bin/env python3
"""Fixture-driven tests for warn_first_inventory.py (case_b871a1bb).

The inventory is a read-only mirror of the PR #881 WARN-first rules
(16 vendored ast-grep rules + 5 semgrep rules). These tests pin it against
a fixture tree with known violations, so a vacuous implementation (one
that always reports zero, skips files, or writes into the tree) fails.

Tricky mirrors are pinned to the vendored rule fixtures from the case_db71b0ff
branch (`sg-rules/tests/*-test.yml`), not to author-written shapes: the
invalid case must fire and the valid case must not. That is what catches
drift once #881 lands.

Style: stdlib unittest, fixture builders, one behavior per test. No
network, no clock, no writes outside a temp dir. The module under test is
loaded via importlib so no import sits below module-level code.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "warn_first_inventory.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "warn_first_inventory", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_module()

# Isolate temp fixtures from any enclosing checkout (e.g. TMPDIR under a
# worktree): `git rev-parse` honours GIT_CEILING_DIRECTORIES, so point the
# ceiling at the system temp dir. Intentional `git init` repos inside tmp
# still resolve (their `.git` sits below the ceiling), but an outer
# checkout above the ceiling no longer leaks in.
os.environ["GIT_CEILING_DIRECTORIES"] = tempfile.gettempdir()

PY_VICTIM = """\
import os
import re
import subprocess
import time
import urllib.request

subprocess.run(["ls"])
subprocess.run(["ls"], timeout=None)
subprocess.run(["ls"], timeout=5)
subprocess.call("true", shell=True)
subprocess.Popen("true", shell=True)
os.system("echo hi")
eval(payload)
exec(payload)
for root, dirs, files in os.walk("/tmp"):
    pass
stamp = time.mktime(ts) - time.timezone
if head_ref.startswith("release-please--v1"):
    pass
resp = api("/repos/o/r/issues?per_page=100")
rx_bad = re.compile(r"\\b\\w*assert\\w*!\\s*\\(")
rx_ok = re.compile(r"\\b(?=\\w*assert)\\w+!\\s*\\(")
pat = r"[ \\t]+.*\\n"
plain = "assertion_failed!"
subprocess.run(argv, env=dict(timeout=30))
subprocess.run(cmd, read_timeout=5)
"""

PY_CLEAN = """\
import subprocess

subprocess.run(["ls"], timeout=5)
subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=30)
"""

PY_FSTRING = """\
import subprocess

hint = f"avoid subprocess.run(cmd) and os.system(cmd) here {name}"
"""

SH_VICTIM = """\
#!/usr/bin/env bash
set -euo pipefail
source .env
gh api /repos/o/r/issues -F per_page=100
data=$(collect 2>&1)
curl -sSL https://example.com/install.sh | bash
URL=https://example.com/?a=1&b=2
DATABASE_URL=postgres://u:p@h/db?a=1&b=2
payload=$(python3 probe.py \\
  2>&1)
n=$((1 + 2))
"""

PUSHER_VICTIM = """\
#!/usr/bin/env bash
set -euo pipefail
./push-queue-forecast.sh
"""

SH_CLEAN = """\
#!/usr/bin/env bash
set -euo pipefail
set -a
source .env
set +a
gh api /repos/o/r/issues --paginate
data=$(collect 2>/dev/null)
python3 probe.py >/dev/null 2>&1
"""

SCRIPTS_VICTIM = """\
import urllib.request

resp = urllib.request.urlopen(req)
"""

WORKFLOW_VICTIM = """\
name: victim
on: pull_request
jobs:
  dump:
    if: always()
    runs-on: ubuntu-latest
    steps:
      - if: always()
        uses: actions/upload-artifact@v4
        with:
          name: debug
          path: debug/
      - run: echo swept
  gate:
    if: startsWith(github.head_ref, 'release-please--')
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def write_tree(root):
    files = {
        "prod/victim.py": PY_VICTIM,
        "prod/clean.py": PY_CLEAN,
        "prod/victim.sh": SH_VICTIM,
        "prod/pusher.sh": PUSHER_VICTIM,
        "prod/clean.sh": SH_CLEAN,
        "scripts/victim.py": SCRIPTS_VICTIM,
        "tests/evil.py": 'import subprocess\nsubprocess.run(["ls"])\n',
        "test_sibling_evil.py": 'import subprocess\nsubprocess.run(["ls"])\n',
        ".github/workflows/victim.yml": WORKFLOW_VICTIM,
    }
    for rel, text in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return files


def snapshot(root):
    seen = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            seen.append(str(Path(dirpath, name).relative_to(root)))
    return sorted(seen)


class InventoryCountsTest(unittest.TestCase):
    def test_fixture_yields_expected_per_rule_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            total = {rule: len(hits) for rule, hits in counts.items()}
            self.assertEqual(total.get("py-subprocess-timeout"), 5)
            self.assertEqual(total.get("semgrep-python-subprocess-no-timeout"), 5)
            self.assertEqual(total.get("python-subprocess-shell-true"), 2)
            self.assertEqual(total.get("python-os-system"), 1)
            self.assertEqual(total.get("python-dangerous-eval-exec"), 2)
            self.assertEqual(total.get("py-oswalk-onerror"), 1)
            self.assertEqual(total.get("py-utc-mktime"), 1)
            self.assertEqual(total.get("py-release-branch-exemption"), 1)
            self.assertEqual(total.get("py-github-first-page-api"), 1)
            self.assertEqual(total.get("py-redos-assert-token"), 1)
            self.assertEqual(total.get("py-redos-yaml-indent"), 1)
            self.assertEqual(total.get("py-default-redirect-opener"), 1)
            self.assertEqual(total.get("bash-gh-first-page-only"), 1)
            self.assertEqual(total.get("bash-json-stderr-merge"), 2)
            self.assertEqual(total.get("bash-curl-pipe-shell"), 1)
            self.assertEqual(total.get("bash-unquoted-url-ampersand"), 1)
            self.assertEqual(total.get("bash-unquoted-db-url-ampersand"), 1)
            self.assertEqual(total.get("bash-source-env-export"), 1)
            self.assertEqual(total.get("bash-pusher-missing-env-load"), 1)
            # Hidden dirs are out of gate scope: the gate never scans them.
            self.assertEqual(total.get("gha-artifact-always-unscrubbed"), 0)
            self.assertEqual(total.get("gha-branch-name-trust"), 0)

    def test_hits_carry_exact_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            timeout_hits = counts["py-subprocess-timeout"]
            locations = sorted(
                (hit[0], hit[1]) for hit in timeout_hits
            )
            self.assertEqual(
                locations,
                [
                    ("prod/victim.py", 7),
                    ("prod/victim.py", 8),
                    ("prod/victim.py", 10),
                    ("prod/victim.py", 25),
                    ("prod/victim.py", 26),
                ],
            )

    def test_vendored_timeout_fixtures(self):
        # sg-rules/tests/py-subprocess-timeout-test.yml: Popen is exempt,
        # nested env=dict(timeout=30) and timeout=None are violations.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prod").mkdir(parents=True, exist_ok=True)
            (root / "prod" / "t.py").write_text(
                "import subprocess\n"
                "subprocess.run(argv, timeout=30, check=True)\n"
                "subprocess.Popen(argv)\n"
                "subprocess.run(argv, env=dict(timeout=30))\n"
                "subprocess.check_output(argv, timeout=None)\n"
            )
            hits = mod.scan_file(
                "prod/t.py", (root / "prod" / "t.py").read_text()
            )
            timeout_lines = sorted(
                line for rule, line, _ in hits if rule == "py-subprocess-timeout"
            )
            self.assertEqual(timeout_lines, [4, 5])

    def test_vendored_redos_fixtures(self):
        # The invalid pair fires; the valid pair and a plain runtime
        # string with assert...! do not.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prod").mkdir(parents=True, exist_ok=True)
            (root / "prod" / "r.py").write_text(
                "import re\n"
                "bad = re.compile(r\"\\b\\w*assert\\w*!\\s*\\(\")\n"
                "ok = re.compile(r\"\\b(?=\\w*assert)\\w+!\\s*\\(\")\n"
                "msg = \"assertion_failed!\"\n"
                "indent_bad = r\"[ \\t]+.*\\n\"\n"
                "indent_ok = r\"^  suites:\\n((?:(?: {4,}.*)?\\n)+)\"\n"
            )
            hits = mod.scan_file(
                "prod/r.py", (root / "prod" / "r.py").read_text()
            )
            assert_lines = sorted(
                line for rule, line, _ in hits if rule == "py-redos-assert-token"
            )
            indent_lines = sorted(
                line for rule, line, _ in hits if rule == "py-redos-yaml-indent"
            )
            self.assertEqual(assert_lines, [2])
            self.assertEqual(indent_lines, [5])

    def test_popen_shell_true_fires(self):
        # The semgrep shell-true rule matches subprocess.$FUNC, so Popen
        # with shell=True must fire too.
        hits = mod.scan_file(
            "prod/p.py",
            "import subprocess\nsubprocess.Popen(\"true\", shell=True)\n",
        )
        shell_lines = [
            line for rule, line, _ in hits if rule == "python-subprocess-shell-true"
        ]
        self.assertEqual(shell_lines, [2])

    def test_fstring_text_is_not_scanned_as_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prod").mkdir(parents=True, exist_ok=True)
            (root / "prod" / "f.py").write_text(PY_FSTRING)
            counts = mod.scan_tree(root)
            every_hit = [
                hit for hits in counts.values() for hit in hits
            ]
            self.assertEqual(every_hit, [])

    def test_stderr_merge_attributes_opening_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            locations = sorted(
                (hit[0], hit[1]) for hit in counts["bash-json-stderr-merge"]
            )
            # Single-line hit stays; the multi-line hit lands on the line
            # where $( opens; `$((...))` arithmetic is not a substitution.
            self.assertEqual(
                locations,
                [("prod/victim.sh", 5), ("prod/victim.sh", 9)],
            )

    def test_process_substitution_is_not_a_heredoc(self):
        # `< <(...)` must not register `allowlist_ids` as a heredoc
        # delimiter swallowing the rest of the file: the top-level
        # redirect is not a hit, but a later real hit still fires.
        text = (
            "#!/usr/bin/env bash\n"
            "while read -r id; do echo \"$id\"; done < <(allowlist_ids)\n"
            "fetch \"$url\" >/dev/null 2>&1\n"
            "v=$(run 2>&1)\n"
        )
        hits = mod.scan_file("prod/p.sh", text)
        merge = [
            (rule, line) for rule, line, _ in hits
            if rule == "bash-json-stderr-merge"
        ]
        self.assertEqual(merge, [("bash-json-stderr-merge", 4)])

    def test_heredoc_body_inside_substitution_counts(self):
        # The gate regexes node text: a redirect in a heredoc body that
        # sits inside $( ) belongs to the command_substitution node, so
        # it is a hit at the opening line — without disturbing state.
        text = (
            "#!/usr/bin/env bash\n"
            "out=$(cat <<'PY'\n"
            "print(\"x 2>&1 y\")\n"
            "PY\n"
            ")\n"
            "echo \"$out\"\n"
        )
        hits = mod.scan_file("prod/p.sh", text)
        merge = [
            (rule, line) for rule, line, _ in hits
            if rule == "bash-json-stderr-merge"
        ]
        self.assertEqual(merge, [("bash-json-stderr-merge", 2)])

    def test_heredoc_body_at_top_level_not_a_hit(self):
        text = (
            "#!/usr/bin/env bash\n"
            "cat <<'PY'\n"
            "print(\"x 2>&1 y\")\n"
            "PY\n"
        )
        hits = mod.scan_file("prod/p.sh", text)
        self.assertEqual(
            [h for h in hits if h[0] == "bash-json-stderr-merge"], []
        )

    def test_brace_default_substitution_closes(self):
        # In ${X:-...$(...)...} the inner ) pops despite outer braces,
        # so a later top-level redirect is not flagged.
        text = (
            "#!/usr/bin/env bash\n"
            "OUT=\"${X:-$HERE/run-$(date -u +%s)}\"\n"
            "probe \"$OUT\" >/dev/null 2>&1\n"
        )
        hits = mod.scan_file("prod/p.sh", text)
        self.assertEqual(
            [h for h in hits if h[0] == "bash-json-stderr-merge"], []
        )

    def test_single_quote_inside_dollar_paren_resets_context(self):
        # A ' inside "$(...)" opens single quotes and a backtick inside
        # those is literal: no stuck state may flag the next line.
        text = (
            "#!/usr/bin/env bash\n"
            "x=\"$(jq --arg r \"$r\" '{a:\"`b`\"}' <<<\"$in\")\"\n"
            "probe \"$x\" >/dev/null 2>&1\n"
        )
        hits = mod.scan_file("prod/p.sh", text)
        self.assertEqual(
            [h for h in hits if h[0] == "bash-json-stderr-merge"], []
        )

    def test_hash_inside_double_quotes_is_not_a_comment(self):
        # The Postgres #>> operator must not truncate the line: the
        # redirect stays inside the substitution and is a hit.
        text = (
            "#!/usr/bin/env bash\n"
            "res=\"$(psql -c \"SELECT a #>> '{b}'\" 2>&1)\"\n"
        )
        hits = mod.scan_file("prod/p.sh", text)
        merge = [
            (rule, line) for rule, line, _ in hits
            if rule == "bash-json-stderr-merge"
        ]
        self.assertEqual(merge, [("bash-json-stderr-merge", 2)])

    def test_hidden_dirs_out_of_scope_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            default_counts = mod.scan_tree(Path(tmp))
            every_file = {
                hit[0] for hits in default_counts.values() for hit in hits
            }
            self.assertNotIn(".github/workflows/victim.yml", every_file)
            wide_counts = mod.scan_tree(Path(tmp), include_hidden=True)
            self.assertEqual(
                len(wide_counts["gha-artifact-always-unscrubbed"]), 1
            )
            self.assertEqual(len(wide_counts["gha-branch-name-trust"]), 1)

    def test_out_of_scope_rules_never_recommended(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            named = mod.recommend(counts)
            self.assertNotIn("gha-artifact-always-unscrubbed", named)
            self.assertNotIn("gha-branch-name-trust", named)
            wide = mod.build_report(
                Path(tmp), counts, include_hidden=True
            )
            self.assertNotIn("gha-artifact-always-unscrubbed", wide["recommend"])
            self.assertNotIn("gha-branch-name-trust", wide["recommend"])
            self.assertEqual(
                set(wide["out_of_gate_scope"]), mod.HIDDEN_ONLY_RULES
            )
            self.assertEqual(
                wide["by_rule"]["gha-artifact-always-unscrubbed"]["origin"],
                "ast-grep (advisory hidden-scope mirror)",
            )

    def test_incomplete_scan_reports_skips_and_suppresses_recommendations(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            original_read = mod.read_text

            def unreadable(root, rel):
                if rel == "prod/clean.py":
                    return None
                return original_read(root, rel)

            with mock.patch.object(mod, "read_text", unreadable):
                counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertIn("prod/clean.py", report["skipped"])
            self.assertEqual(report["recommend"], [])
            self.assertIn("incomplete scan", mod.format_text(report))
            self.assertNotIn("flips next (zero hits)", mod.format_text(report))

    def test_unreadable_file_via_permission_error_reports_skipped(self):
        # Hits the real `except OSError` arm in scan_tree (not the
        # NUL/binary None arm): PermissionError is an OSError, so the
        # path lands in skipped, flips are suppressed, and main exits 2.
        # Deterministic as root via mock (chmod 000 still reads as root).
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            real_read_bytes = Path.read_bytes

            def denied(self, *args, **kwargs):
                if str(self).endswith(os.path.join("prod", "clean.py")):
                    raise PermissionError(13, "denied")
                return real_read_bytes(self, *args, **kwargs)

            with mock.patch.object(Path, "read_bytes", denied):
                counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertTrue(
                any("prod/clean.py" in entry for entry in report["skipped"])
            )
            self.assertEqual(report["recommend"], [])
            self.assertIn("incomplete scan", mod.format_text(report))
            with mock.patch.object(
                mod, "scan_tree", return_value=counts
            ):
                with mock.patch(
                    "sys.stdout", new_callable=io.StringIO
                ) as output:
                    self.assertEqual(mod.main(["--root", tmp]), 2)
                self.assertIn("incomplete scan", output.getvalue())

    def test_walk_error_reports_skipped_and_suppresses_flips(self):
        # Hits the real os.walk onerror arm: the stub invokes whatever
        # onerror list_files wired up, so deleting `onerror=` or emptying
        # record_walk_error leaves skipped empty and fails this test.
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            real_walk = mod.os.walk

            def fake_walk(top, *args, **kwargs):
                onerror = kwargs.get("onerror")
                self.assertIsNotNone(
                    onerror, "os.walk must be wired with onerror="
                )
                onerror(PermissionError(13, "denied", "/x/unreadable-dir"))
                yield from real_walk(top, *args, **kwargs)

            with mock.patch.object(mod.os, "walk", side_effect=fake_walk):
                counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertTrue(report["skipped"])
            self.assertEqual(report["recommend"], [])
            self.assertIn("incomplete scan", mod.format_text(report))

    def test_git_ls_files_missing_binary_fails_closed(self):
        # proc is None (git binary missing / OSError): a checkout must
        # fail closed, not fall back to a filesystem walk.
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            with mock.patch.object(
                mod.subprocess,
                "run",
                side_effect=FileNotFoundError(2, "git"),
            ):
                counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertEqual(report["total"], 0)
            self.assertTrue(report["skipped"])
            self.assertEqual(report["recommend"], [])

    def test_subdir_root_scans_tracked_files_only(self):
        # --root below the checkout top stays tracked-only: untracked
        # files under the subdir must not count.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub" / "deep").mkdir(parents=True, exist_ok=True)
            (root / "sub" / "tracked.py").write_text(
                "import subprocess\n"
                "subprocess.run([\"ls\"], timeout=5)\n"
            )
            (root / "sub" / "untracked.py").write_text(
                "import subprocess\nsubprocess.run([\"ls\"])\n"
            )
            (root / "sub" / "deep" / "untracked2.py").write_text(
                "import subprocess\nsubprocess.run([\"ls\"])\n"
            )
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            subprocess.run(
                ["git", "add", "sub/tracked.py"], cwd=tmp, check=True
            )
            counts = mod.scan_tree(root / "sub")
            self.assertEqual(len(counts["py-subprocess-timeout"]), 0)
            every_file = {
                hit[0] for hits in counts.values() for hit in hits
            }
            self.assertNotIn("untracked.py", every_file)
            self.assertNotIn("deep/untracked2.py", every_file)

    def test_non_ascii_tracked_path_is_found(self):
        # git ls-files -z keeps non-ASCII names intact where the quoted
        # text form would drop them (false clean zero).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = "café.py"
            (root / name).write_text(
                "import subprocess\nsubprocess.run([\"ls\"])\n"
            )
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            counts = mod.scan_tree(root)
            timeout_hits = counts["py-subprocess-timeout"]
            self.assertEqual(len(timeout_hits), 1)
            self.assertEqual(timeout_hits[0][0], name)

    def test_cli_returns_nonzero_for_incomplete_scan(self):
        counts = mod.InventoryCounts()
        counts.skipped.append("locked.py")
        with mock.patch.object(mod, "scan_tree", return_value=counts):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                result = mod.main(["--root", "."])
        self.assertEqual(result, 2)
        self.assertIn("incomplete scan", output.getvalue())
        self.assertNotIn("flips next (zero hits)", output.getvalue())

    def test_git_ls_files_failure_does_not_fall_back_to_tree_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)

            def _run(args, **kwargs):
                if "rev-parse" in args:
                    return subprocess.CompletedProcess(
                        args, 0, "true\n", ""
                    )
                return subprocess.CompletedProcess(args, 1, "", "failed")

            with mock.patch.object(
                mod.subprocess, "run", side_effect=_run
            ):
                counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertEqual(report["total"], 0)
            self.assertTrue(report["skipped"])
            self.assertEqual(report["recommend"], [])

    def test_untracked_dir_inside_checkout_fails_closed(self):
        # --root on an untracked (or ignored) dir inside a checkout must
        # not report a false clean: ls-files exits 0 empty, so the scan
        # records a skip, suppresses flips, and exits nonzero.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            (root / "tracked.py").write_text(
                "import subprocess\nsubprocess.run([\"ls\"], timeout=5)\n"
            )
            subprocess.run(["git", "add", "tracked.py"], cwd=tmp, check=True)
            out = root / "build" / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "a.py").write_text(
                "import subprocess\nsubprocess.run([\"ls\"])\n"
            )
            (out / "s.sh").write_text(
                "#!/usr/bin/env bash\necho $(cat x 2>&1)\n"
            )
            counts = mod.scan_tree(out)
            report = mod.build_report(out, counts)
            self.assertEqual(report["total"], 0)
            self.assertTrue(report["skipped"])
            self.assertTrue(
                any("untracked or ignored" in entry for entry in report["skipped"])
            )
            self.assertEqual(report["recommend"], [])
            self.assertIn("incomplete scan", mod.format_text(report))

    def test_rev_parse_dubious_ownership_fails_closed(self):
        # rev-parse rc 128 "dubious ownership" with a .git marker must fail
        # closed (skipped, no flips, exit 2), never fall back to a walk.
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)

            def _run(args, **kwargs):
                if "rev-parse" in args:
                    return subprocess.CompletedProcess(
                        args,
                        128,
                        "",
                        "fatal: detected dubious ownership "
                        "in repository at '/x'",
                    )
                return subprocess.CompletedProcess(
                    args, 128, "", "fatal: detected dubious ownership"
                )

            with mock.patch.object(
                mod.subprocess, "run", side_effect=_run
            ):
                counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertEqual(report["total"], 0)
            self.assertTrue(report["skipped"])
            self.assertEqual(report["recommend"], [])
            with mock.patch.object(
                mod.subprocess, "run", side_effect=_run
            ):
                with mock.patch(
                    "sys.stdout", new_callable=io.StringIO
                ):
                    self.assertEqual(mod.main(["--root", tmp]), 2)

    def test_dangling_git_pointer_fails_closed(self):
        # A .git gitdir pointer git cannot use (and an empty .git dir)
        # must fail closed: the untracked vendored file is never counted.
        for layout in ("dangling", "empty-dir"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if layout == "dangling":
                    (root / ".git").write_text(
                        "gitdir: /nonexistent/.git/worktrees/x\n"
                    )
                else:
                    (root / ".git").mkdir()
                (root / "a.py").write_text(
                    "import subprocess\nsubprocess.run([\"ls\"])\n"
                )
                vend = root / "node_modules"
                vend.mkdir(exist_ok=True)
                (vend / "vend.py").write_text(
                    "import subprocess\nsubprocess.run([\"ls\"])\n"
                )
                counts = mod.scan_tree(root)
                report = mod.build_report(root, counts)
                self.assertEqual(report["total"], 0, layout)
                self.assertTrue(report["skipped"], layout)
                self.assertTrue(
                    any(".git" in entry for entry in report["skipped"]),
                    layout,
                )
                self.assertEqual(report["recommend"], [], layout)
                every_file = {
                    hit[0] for hits in counts.values() for hit in hits
                }
                self.assertNotIn("node_modules/vend.py", every_file, layout)

    def test_has_filesystem_files_walk_error_assumes_files(self):
        # The existence probe must assume files on walk errors: a no-op
        # _onerror would report a false clean for an untracked dir whose
        # only content sits under an unreadable subdirectory.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_walk(top, *args, **kwargs):
                onerror = kwargs.get("onerror")
                self.assertIsNotNone(
                    onerror, "existence probe must wire onerror="
                )
                onerror(PermissionError(13, "denied", str(root)))
                return iter([])

            with mock.patch.object(
                mod.os, "walk", side_effect=fake_walk
            ):
                self.assertTrue(mod._has_filesystem_files(root))

    def test_rev_parse_false_without_marker_scans_tree(self):
        # rc 0 "false" with no .git marker is a plain tree: the scan
        # walks the filesystem. Dropping the stdout == "true" check
        # would misroute this into the fail-closed branch (0 hits).
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)

            def _run(args, **kwargs):
                if "rev-parse" in args:
                    return subprocess.CompletedProcess(args, 0, "false\n", "")
                return subprocess.CompletedProcess(args, 128, "", "nope")

            with mock.patch.object(
                mod.subprocess, "run", side_effect=_run
            ):
                counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertEqual(
                len(counts["py-subprocess-timeout"]), 5
            )
            self.assertFalse(report["skipped"])
            self.assertTrue(report["total"] > 0)

    def test_has_git_marker_honours_ceiling(self):
        # The ceiling directory's own .git must not count: git never
        # examines the ceiling dir itself when ascending. Deleting the
        # ceiling break (or emptying _ceiling_dirs) must fail this.
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(os.path.realpath(tmp))
            (outer / ".git").mkdir()
            inner = outer / "a" / "b"
            inner.mkdir(parents=True)
            for ceiling, expected in (
                (str(outer / "a"), False),
                (str(outer), False),
                ("", True),
            ):
                with mock.patch.dict(
                    os.environ, {"GIT_CEILING_DIRECTORIES": ceiling}
                ):
                    self.assertEqual(
                        mod._has_git_marker(inner), expected, ceiling
                    )

    def test_workflow_artifact_checks_each_step_mapping(self):
        text = (
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - if: always()\n"
            "        uses: actions/upload-artifact@v4\n"
            "        with:\n"
            "          name: failure-bundle\n"
            "      - run: echo swept\n"
        )
        hits = mod.scan_file(".github/workflows/ci.yml", text)
        self.assertEqual(
            [(rule, line) for rule, line, _ in hits
             if rule == "gha-artifact-always-unscrubbed"],
            [("gha-artifact-always-unscrubbed", 5)],
        )

    def test_test_paths_are_out_of_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            every_file = {
                hit[0] for hits in counts.values() for hit in hits
            }
            self.assertNotIn("tests/evil.py", every_file)
            self.assertNotIn("test_sibling_evil.py", every_file)

    def test_clean_files_report_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            every_file = {
                hit[0] for hits in counts.values() for hit in hits
            }
            self.assertNotIn("prod/clean.py", every_file)
            self.assertNotIn("prod/clean.sh", every_file)

    def test_scan_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            before = snapshot(tmp)
            mod.scan_tree(Path(tmp))
            mod.scan_tree(Path(tmp))
            self.assertEqual(snapshot(tmp), before)

    def test_recommendation_names_only_zero_hit_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "prod" / "clean.py"
            clean.parent.mkdir(parents=True, exist_ok=True)
            clean.write_text(PY_CLEAN)
            counts = mod.scan_tree(Path(tmp))
            named = mod.recommend(counts)
            self.assertTrue(named)
            totals = {rule: len(hits) for rule, hits in counts.items()}
            for rule in named:
                self.assertIn(rule, totals)
                self.assertEqual(totals[rule], 0)

    def test_recommendation_empty_when_every_rule_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp), include_hidden=True)
            self.assertEqual(
                mod.recommend(counts, include_hidden=True), []
            )

    def test_report_marks_origin_and_unique_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            self.assertEqual(
                report["by_rule"]["semgrep-python-subprocess-no-timeout"][
                    "origin"
                ],
                "semgrep (intended semantics, see header)",
            )
            self.assertEqual(
                report["by_rule"]["py-subprocess-timeout"]["origin"],
                "ast-grep",
            )
            # The timeout pair fires on the same call sites: the summed
            # total double-counts, unique_locations does not.
            timeout_sites = {
                (rel, line) for rel, line, _ in counts["py-subprocess-timeout"]
            }
            semgrep_sites = {
                (rel, line)
                for rel, line, _ in counts["semgrep-python-subprocess-no-timeout"]
            }
            self.assertEqual(timeout_sites, semgrep_sites)
            self.assertLess(report["unique_locations"], report["total"])
            self.assertIn(
                "gha-artifact-always-unscrubbed", report["out_of_gate_scope"]
            )

    def test_json_report_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            counts = mod.scan_tree(Path(tmp))
            report = mod.build_report(Path(tmp), counts)
            parsed = json.loads(json.dumps(report))
            self.assertEqual(parsed["root"], str(Path(tmp)))
            self.assertIn("py-subprocess-timeout", parsed["by_rule"])
            self.assertTrue(parsed["total"] > 0)

    def test_cli_exits_zero_and_prints_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp)
            proc = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--root", tmp],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("py-subprocess-timeout", proc.stdout)


if __name__ == "__main__":
    unittest.main()
