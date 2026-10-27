"""Temp-repo fixtures for the orphan-suite ratchet.

Each case builds a miniature tracked repo (git init + git add, so only
tracked files count) and runs verification/orphan_suites.check against it.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from verification import orphan_suites as ratchet


WORKFLOW = """\
name: CI
on:
  pull_request:
jobs:
  suites:
    runs-on: ubuntu-latest
    steps:
      - name: placeholder suite
        run: {runs}
"""

VERIFY_FAST = """\
#!/usr/bin/env bash
set -euo pipefail
{body}
if [[ "$MODE" == "parse" ]]; then
  exit 0
fi
"""


# Every temp repo make_repo creates, removed by tearDownModule: CI runs
# this suite on persistent runners, so un-removed .git trees accumulate.
CREATED_REPOS = []


def tearDownModule():
    for repo in CREATED_REPOS:
        shutil.rmtree(repo, ignore_errors=True)


def make_repo(files):
    """Create a temp dir with the given {relpath: content} tracked by git."""
    tmp = Path(tempfile.mkdtemp(prefix="orphan-ratchet-"))
    CREATED_REPOS.append(tmp)
    (tmp / ".github" / "workflows").mkdir(parents=True)
    (tmp / "scripts").mkdir(exist_ok=True)
    (tmp / "verification").mkdir(exist_ok=True)
    for rel, content in files.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True,
                   capture_output=True)
    return tmp


def repo_with(runs="echo nothing wired", verify_body="echo ok",
              allowlist=None, suites=None):
    files = {
        ".github/workflows/ci.yml": WORKFLOW.format(runs=runs),
        "scripts/verify-fast.sh": VERIFY_FAST.format(body=verify_body),
    }
    for name in (suites or []):
        files[name] = "#!/usr/bin/env bash\necho fixture suite\n"
    if allowlist is not None:
        files["verification/orphan_suites_allowlist.json"] = (
            json.dumps(allowlist, indent=2) + "\n")
    return make_repo(files)


def repo_with_steps(step_yaml, verify_body="echo ok",
                    allowlist=None, suites=None):
    """Temp repo whose ci.yml step uses a block scalar (multi-line runs)."""
    files = {
        ".github/workflows/ci.yml": (
            "name: CI\non:\n  pull_request:\njobs:\n  suites:\n"
            "    runs-on: ubuntu-latest\n    steps:\n" + step_yaml),
        "scripts/verify-fast.sh": VERIFY_FAST.format(body=verify_body),
    }
    for name in (suites or []):
        files[name] = "#!/usr/bin/env bash\necho fixture suite\n"
    if allowlist is not None:
        files["verification/orphan_suites_allowlist.json"] = (
            json.dumps(allowlist, indent=2) + "\n")
    return make_repo(files)


class OrphanRatchetTest(unittest.TestCase):
    def test_orphan_fails_and_names_the_file(self):
        root = repo_with(suites=["test_alpha.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.sh", errors[0])

    def test_suite_wired_through_ci_yml_passes(self):
        root = repo_with(runs="./test_alpha.sh", suites=["test_alpha.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_unrelated_shell_segment_is_not_parsed(self):
        segments = [("scripts/run_unrelated.sh",
                     "unrelated() { echo ready; }\nunrelated\n")]
        counts = ({"test_alpha.sh": 1}, {"test_alpha": 1})
        with mock.patch.object(ratchet, "_function_bodies",
                               wraps=ratchet._function_bodies) as function_bodies:
            self.assertFalse(ratchet.is_wired(
                "test_alpha.sh", segments, [], counts))
            function_bodies.assert_not_called()

    def test_suite_wired_through_verify_fast_unittest_list_passes(self):
        root = repo_with(
            verify_body="(cd ops/productivity && python3 -B -m unittest "
                        "test_beta test_gamma)",
            suites=["ops/productivity/test_beta.py",
                    "ops/productivity/test_gamma.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_exempt_suite_passes(self):
        root = repo_with(suites=["test_alpha.sh"])
        allow = {"test_alpha.sh": "needs a database, not run offline"}
        self.assertEqual(ratchet.check(root, allow), [])

    def test_stale_exemption_for_now_wired_suite_fails(self):
        root = repo_with(runs="./test_alpha.sh", suites=["test_alpha.sh"])
        allow = {"test_alpha.sh": "needs a database, not run offline"}
        errors = ratchet.check(root, allow)
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.sh", errors[0])

    def test_exemption_for_deleted_file_fails(self):
        root = repo_with()
        allow = {"test_gone.sh": "host-only, never wired"}
        errors = ratchet.check(root, allow)
        self.assertEqual(len(errors), 1)
        self.assertIn("test_gone.sh", errors[0])

    def test_untracked_suite_is_ignored(self):
        root = repo_with()
        (root / "test_scratch.sh").write_text("echo not tracked\n")
        self.assertEqual(ratchet.check(root, {}), [])

    def test_case_arm_mention_does_not_wire(self):
        body = ("case \"$1\" in\n"
                "  test_alpha.sh) return 0 ;;\n"
                "esac")
        root = repo_with(verify_body=body, suites=["test_alpha.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.sh", errors[0])

    def test_parenthesised_unittest_invocation_wires(self):
        root = repo_with(
            verify_body="(cd ops/productivity && python3 -m unittest "
                        "test_beta)",
            suites=["ops/productivity/test_beta.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_empty_exemption_reason_fails(self):
        root = repo_with(suites=["test_alpha.sh"])
        errors = ratchet.check(root, {"test_alpha.sh": ""})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.sh", errors[0])

    def test_suite_only_in_full_tier_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="echo nothing wired"),
            "scripts/verify-fast.sh": (
                "#!/usr/bin/env bash\nset -euo pipefail\n"
                "echo parse tier\n"
                "if [[ \"$MODE\" == \"parse\" ]]; then\n"
                "  exit 0\n"
                "fi\n"
                "python3 -m unittest test_alpha\n"),
            "test_alpha.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.py", errors[0])

    def test_discover_pattern_wires_only_its_directory(self):
        root = repo_with(
            runs="python3 -m unittest discover -s a -p 'test_x.py'",
            suites=["a/test_x.py", "b/test_x.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("b/test_x.py", errors[0])

    def test_suite_only_in_ast_parse_list_is_orphan(self):
        root = repo_with(
            verify_body="python3 -c 'import ast, sys; "
                        "[ast.parse(open(p, \"rb\").read(), p) "
                        "for p in sys.argv[1:]]' test_alpha.py",
            suites=["test_alpha.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.py", errors[0])

    def test_suite_only_on_ast_parse_continuation_is_orphan(self):
        root = repo_with(
            verify_body="python3 -c 'import ast, sys; "
                        "[ast.parse(open(p).read(), p) "
                        "for p in sys.argv[1:]]' \\\n"
                        "  test_alpha.py",
            suites=["test_alpha.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.py", errors[0])

    def test_suite_only_in_classifier_table_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 .github/scripts/ci_changes.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            ".github/scripts/ci_changes.py": (
                "AREA = {\n"
                "    \"core\": [\"test_alpha.sh\"],\n"
                "}\n"),
            "test_alpha.sh": "#!/usr/bin/env bash\necho fixture suite\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_alpha.sh", errors[0])

    def test_suite_wired_through_helper_script_passes(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash scripts/run_gate.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "scripts/run_gate.sh": (
                "#!/usr/bin/env bash\n"
                "python3 -m unittest test_alpha\n"),
            "test_alpha.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_suite_only_in_ci_run_helper_docstring_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                '"""Helper. Tests: python3 -m unittest test_zz_probe."""\n'
                "print('impl')\n"),
            "ops/zzprobe/test_zz_probe.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_probe.py", errors[0])

    def test_suite_only_in_ast_parse_listed_helper_docstring_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="echo nothing wired"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(
                body="python3 -c 'import ast, sys; "
                     "[ast.parse(open(p, \"rb\").read(), p) "
                     "for p in sys.argv[1:]]' ops/zzprobe/zz_impl.py"),
            "ops/zzprobe/zz_impl.py": (
                '"""Helper. Tests: python3 -m unittest test_zz_probe."""\n'
                "print('impl')\n"),
            "ops/zzprobe/test_zz_probe.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_probe.py", errors[0])

    def test_suite_only_in_ci_run_helper_echo_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "echo \"reminder: also run ./test_zz_probe2.sh by hand\"\n"),
            "ops/zzprobe/test_zz_probe2.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_probe2.sh", errors[0])

    def test_suite_only_in_path_qualified_ast_parse_list_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="echo nothing wired"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(
                body="python3 -c 'import ast, sys; "
                     "[ast.parse(open(p, \"rb\").read(), p) "
                     "for p in sys.argv[1:]]' ops/sub/test_alpha.py"),
            "ops/sub/test_alpha.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("ops/sub/test_alpha.py", errors[0])

    def test_suite_only_on_py_compile_continuation_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="echo nothing wired"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(
                body="python3 -m py_compile \\\n"
                     "  ops/sub/test_alpha.py"),
            "ops/sub/test_alpha.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("ops/sub/test_alpha.py", errors[0])

    def test_suite_only_in_shell_heredoc_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "usage() { cat <<EOF\n"
                "usage: run ./test_zz_a.sh before merging\n"
                "EOF\n"
                "}\n"
                "usage\n"),
            "ops/zzprobe/test_zz_a.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_a.sh", errors[0])

    def test_suite_only_in_python_print_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "print(\"usage: python3 test_zz_b.py to verify\")\n"),
            "ops/zzprobe/test_zz_b.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_b.py", errors[0])

    def test_suite_only_in_echo_with_quoted_separator_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "echo \"reminder: run ./test_zz_c.sh by hand; not automated\"\n"),
            "ops/zzprobe/test_zz_c.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_c.sh", errors[0])

    def test_suite_only_in_trailing_comment_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "true  # tests: ./test_zz_d.sh\n"),
            "ops/zzprobe/test_zz_d.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_d.sh", errors[0])

    def test_suite_piped_into_runner_wires(self):
        root = repo_with(
            runs="echo payload | bash ./test_zz_f.sh",
            suites=["test_zz_f.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_suite_after_cp_and_chain_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="cp ops/zzprobe/fix.json /tmp/fix.json"
                     " && bash ./test_zz_h.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/fix.json": "{}\n",
            "test_zz_h.sh": "#!/usr/bin/env bash\necho fixture suite\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_parse_checked_helper_with_real_run_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="echo nothing wired"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(
                body="python3 -c 'import ast, sys; "
                     "[ast.parse(open(p).read(), p) "
                     "for p in sys.argv[1:]]' ops/zzprobe/zz_impl.py"),
            "ops/zzprobe/zz_impl.py": (
                "import os\n"
                "os.system(\"bash ./test_zz_x.sh\")\n"),
            "ops/zzprobe/test_zz_x.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_x.sh", errors[0])

    def test_suite_only_on_cp_line_is_orphan(self):
        root = repo_with(
            runs="cp ./test_zz_g.sh /tmp/staged.sh",
            suites=["test_zz_g.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_g.sh", errors[0])

    def test_suite_only_in_printf_reminder_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "printf 'reminder: run ./test_zz_i.sh by hand\\n'\n"),
            "ops/zzprobe/test_zz_i.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_i.sh", errors[0])

    def test_suite_after_echo_chain_wires(self):
        root = repo_with(
            runs="echo done && bash ./test_zz_j.sh",
            suites=["test_zz_j.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_var_prefixed_two_hop_chain_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="./test_zz_outer.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "test_zz_outer.sh": (
                "#!/usr/bin/env bash\n"
                "bash \"$ROOT/test_zz_mid.sh\"\n"),
            "test_zz_mid.sh": (
                "#!/usr/bin/env bash\n"
                "python3 -B \"$ROOT/test_zz_leaf.py\"\n"),
            "test_zz_leaf.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_braced_var_prefixed_hop_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash \"${ROOT}/test_zz_mid2.sh\""),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "test_zz_mid2.sh": (
                "#!/usr/bin/env bash\n"
                "python3 \"${ROOT}/test_zz_leaf2.py\"\n"),
            "test_zz_leaf2.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_env_prefixed_invocation_wires(self):
        root = repo_with(
            runs="PAPERCLIP_RUN_SCRATCH_DIR=\"$TMP\" "
                 "python3 -B -m unittest test_zz_y",
            suites=["test_zz_y.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_die_message_does_not_wire(self):
        root = repo_with(
            runs="[ -n \"$X\" ] || die \"set X, then run ./test_zz_e.sh\"",
            suites=["test_zz_e.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_e.sh", errors[0])

    def test_then_echo_does_not_wire(self):
        root = repo_with(
            runs="if [ -z \"$X\" ]; then echo \"reminder: run "
                 "./test_zz_f.sh\"; fi",
            suites=["test_zz_f.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_f.sh", errors[0])

    def test_brace_echo_does_not_wire(self):
        root = repo_with(
            runs="cmd || { echo \"see ./test_zz_g.sh\" >&2; exit 1; }",
            suites=["test_zz_g.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_g.sh", errors[0])

    def test_case_pattern_echo_does_not_wire(self):
        root = repo_with(
            runs="-h) echo \"tests live in ./test_zz_h.sh\";;",
            suites=["test_zz_h.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_h.sh", errors[0])

    def test_test_exists_check_does_not_wire(self):
        root = repo_with(
            runs="test -x ./test_zz_m.sh && echo ok",
            suites=["test_zz_m.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_m.sh", errors[0])

    def test_hint_assignment_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "HINT=\"run ./test_zz_n.sh\"\n"),
            "ops/zzprobe/test_zz_n.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_n.sh", errors[0])

    def test_quoted_separator_after_name_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "echo \"heads up; run ./test_zz_q.sh by hand\"\n"),
            "ops/zzprobe/test_zz_q.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_q.sh", errors[0])

    def test_quoted_and_after_name_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "echo \"heads up && run ./test_zz_q2.sh by hand\"\n"),
            "ops/zzprobe/test_zz_q2.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_q2.sh", errors[0])

    def test_py_logging_call_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import logging\n"
                "logging.warning(\"run ./test_zz_s.sh\")\n"),
            "ops/zzprobe/test_zz_s.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_s.sh", errors[0])

    def test_py_stderr_write_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import sys\n"
                "sys.stderr.write(\"run ./test_zz_t.sh\\n\")\n"),
            "ops/zzprobe/test_zz_t.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_t.sh", errors[0])

    def test_py_usage_assignment_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "USAGE = \"python3 test_zz_u.py to verify\"\n"),
            "ops/zzprobe/test_zz_u.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_u.py", errors[0])

    def test_py_raise_systemexit_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "raise SystemExit(\"run python3 test_zz_v.py\")\n"),
            "ops/zzprobe/test_zz_v.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_v.py", errors[0])

    def test_py_multiline_print_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "print(\n"
                "    \"usage: python3 test_zz_w.py to verify\")\n"),
            "ops/zzprobe/test_zz_w.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_w.py", errors[0])

    def test_stdin_heredoc_run_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "bash -s <<'EOS'\n"
                "./test_zz_r.sh\n"
                "EOS\n"),
            "ops/zzprobe/test_zz_r.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_subshell_invocation_wires(self):
        root = repo_with(
            runs="(cd ops/zzprobe && ./test_zz_k.sh)",
            suites=["ops/zzprobe/test_zz_k.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_command_substitution_invocation_wires(self):
        root = repo_with(
            runs="out=$(./test_zz_l.sh)",
            suites=["test_zz_l.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_suite_var_assignment_then_bash_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "SUITE=\"$ROOT/test_zz_m2.sh\"\n"
                "bash \"$SUITE\"\n"),
            "test_zz_m2.sh": "#!/usr/bin/env bash\necho fixture suite\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_generic_wrapper_with_test_argument_wires(self):
        root = repo_with(
            runs="run-suite test ./test_zz_n2.sh",
            suites=["test_zz_n2.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_longer_path_does_not_wire_suffix_suite(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 mcp/test/test_zz_x.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "test/test_zz_x.py": "import unittest\n",
            "mcp/test/test_zz_x.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test/test_zz_x.py", errors[0])

    def test_tar_manifest_array_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/bundle.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/bundle.sh": (
                "#!/usr/bin/env bash\n"
                "files=(\n"
                "  ops/zzprobe/test_zz_o.sh\n"
                ")\n"
                "tar cf bundle.tar \"${files[@]}\"\n"),
            "ops/zzprobe/test_zz_o.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_o.sh", errors[0])

    def test_grep_mention_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "grep -q test_zz_p.sh README.md\n"),
            "ops/zzprobe/test_zz_p.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_p.sh", errors[0])

    def test_multiline_echo_usage_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "echo \"usage:\n"
                "  run ./test_zz_q3.sh\n"
                "\"\n"),
            "ops/zzprobe/test_zz_q3.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_q3.sh", errors[0])

    def test_read_prompt_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "read -p \"run ./test_zz_r2.sh now? \" ans\n"),
            "ops/zzprobe/test_zz_r2.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_r2.sh", errors[0])

    def test_redirected_echo_does_not_wire(self):
        root = repo_with(
            runs=">&2 echo \"see ./test_zz_s2.sh\"",
            suites=["test_zz_s2.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_s2.sh", errors[0])

    def test_py_argparse_epilog_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import argparse\n"
                "argparse.ArgumentParser(\n"
                "    epilog=\"run python3 test_zz_t2.py\")\n"),
            "ops/zzprobe/test_zz_t2.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_t2.py", errors[0])

    def test_py_sys_exit_message_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import sys\n"
                "sys.exit(\"run test_zz_u2.py\")\n"),
            "ops/zzprobe/test_zz_u2.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_u2.py", errors[0])

    def test_py_string_tuple_helper_not_followed(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "SAMPLES = (\"ops/zzprobe/bundle.sh\",)\n"
                "print(SAMPLES)\n"),
            "ops/zzprobe/bundle.sh": (
                "#!/usr/bin/env bash\n"
                "./test_zz_v2.sh\n"),
            "ops/zzprobe/test_zz_v2.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_v2.sh", errors[0])

    def test_working_directory_relative_run_wires(self):
        root = repo_with(
            runs="./test/test_zz_wd.sh",
            suites=["ops/zzprobe/test/test_zz_wd.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_dirname_substitution_script_arg_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "exec python3 \"$(dirname \"$0\")/test_zz_dn.py\"\n"),
            "ops/zzprobe/test_zz_dn.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_bash_c_code_string_under_wrapper_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "check \"atomic\" bash -c \\\n"
                "  \"(cd ops && python3 -m unittest test_zz_bc)\"\n"),
            "ops/test_zz_bc.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_py_load_tests_from_name_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_audit.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_audit.py": (
                "import unittest\n"
                "loader = unittest.TestLoader()\n"
                "suite = loader.loadTestsFromName(\"test_zz_lt\")\n"),
            "ops/zzprobe/test_zz_lt.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_discover_in_grep_mention_does_not_wire(self):
        root = repo_with(
            runs="grep -q \"python3 -m unittest discover -s ops/zz -p "
                 "'test_*.py'\" README.md",
            suites=["ops/zz/test_zz.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_discover_in_die_message_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "die \"hint: python3 -m unittest discover -s ops/zz "
                "-p 'test_*.py'\"\n"),
            "ops/zz/test_zz.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_discover_in_py_assert_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import unittest\n"
                "assert ok, \"python3 -m unittest discover -s ops/zz "
                "-p 'test_*.py'\"\n"),
            "ops/zz/test_zz.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_discover_in_py_usage_assignment_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "USAGE = \"python3 -m unittest discover -s ops "
                "-p 'test_*.py'\"\n"),
            "ops/test_zz.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_discover_in_shell_assignment_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "HINT=\"python3 -m unittest discover -s ops "
                "-p 'test_*.py'\"\n"),
            "ops/test_zz.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_discover_with_cd_prefix_does_not_wire(self):
        root = repo_with(
            runs="(cd other && python3 -m unittest discover -s ops/zz "
                 "-p 'test_*.py')",
            suites=["ops/zz/test_zz.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_discover_with_verbose_flag_wires(self):
        root = repo_with(
            runs="python3 -m unittest discover -v -s ops/zz -p 'test_*.py'",
            suites=["ops/zz/test_zz.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_discover_with_double_quoted_pattern_wires(self):
        root = repo_with(
            runs='python3 -m unittest discover -s ops/zz -p "test_*.py"',
            suites=["ops/zz/test_zz.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_discover_with_reordered_flags_wires(self):
        root = repo_with(
            runs="python3 -m unittest discover -p 'test_*.py' -s ops/zz",
            suites=["ops/zz/test_zz.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_discover_dot_start_dir_wires_root_suite(self):
        root = repo_with(
            runs="python3 -m unittest discover -s . -p 'test_*.py'",
            suites=["test_zz.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_unlisted_lint_commands_do_not_wire(self):
        root = repo_with(
            runs="python3 -m mypy ops/zza/test_zza.py && black --check "
                 "ops/zzb/test_zzb.py && semgrep scan --config r "
                 "ops/zzc/test_zzc.py",
            suites=["ops/zza/test_zza.py", "ops/zzb/test_zzb.py",
                    "ops/zzc/test_zzc.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 3)
        self.assertIn("test_zza.py", errors[0])
        self.assertIn("test_zzb.py", errors[1])
        self.assertIn("test_zzc.py", errors[2])

    def test_inspector_helper_commands_do_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "sha1sum ops/zz/test_zz_sha.sh\n"
                "nl ops/zz/test_zz_nl.sh\n"
                "rsync -a ops/zz/test_zz_rsync.sh dest/\n"
                "scp ops/zz/test_zz_scp.sh host:/tmp/\n"
                "command -v ops/zz/test_zz_cmd.sh\n"),
            "ops/zz/test_zz_sha.sh": "#!/usr/bin/env bash\necho fixture\n",
            "ops/zz/test_zz_nl.sh": "#!/usr/bin/env bash\necho fixture\n",
            "ops/zz/test_zz_rsync.sh": "#!/usr/bin/env bash\necho fixture\n",
            "ops/zz/test_zz_scp.sh": "#!/usr/bin/env bash\necho fixture\n",
            "ops/zz/test_zz_cmd.sh": "#!/usr/bin/env bash\necho fixture\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 5)

    def test_export_assignment_does_not_wire(self):
        root = repo_with(
            runs="export SUITE=ops/zz/test_zz.sh",
            suites=["ops/zz/test_zz.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.sh", errors[0])

    def test_non_runner_subprocess_lists_do_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import subprocess\n"
                "subprocess.run([\"chmod\", \"+x\", "
                "\"ops/zz/test_zz_pm.py\"])\n"
                "subprocess.run([\"shellcheck\", "
                "\"ops/zz/test_zz_sc.py\"])\n"),
            "ops/zz/test_zz_pm.py": "import unittest\n",
            "ops/zz/test_zz_sc.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 2)

    def test_unknown_command_does_not_wire(self):
        root = repo_with(
            runs="lint-scan ./test_zz_unk.sh",
            suites=["test_zz_unk.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_unk.sh", errors[0])

    def test_for_shellcheck_loop_does_not_wire(self):
        root = repo_with(
            runs='for f in ops/zz/test_zz.sh; do shellcheck "$f"; done',
            suites=["ops/zz/test_zz.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.sh", errors[0])

    def test_for_bash_loop_wires(self):
        root = repo_with(
            runs='for s in ops/zz/test_zz.sh; do bash "$s"; done',
            suites=["ops/zz/test_zz.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_bash_lc_cluster_wires(self):
        root = repo_with(
            runs="bash -lc 'cd ops/zz && ./test_zz.sh'",
            suites=["ops/zz/test_zz.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_bash_o_pipefail_wires(self):
        root = repo_with(
            runs="bash -o pipefail ops/zz/test_zz.sh",
            suites=["ops/zz/test_zz.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_python_W_error_wires(self):
        root = repo_with(
            runs="python3 -W error ops/zz/test_zz.py",
            suites=["ops/zz/test_zz.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_python_X_dev_wires(self):
        root = repo_with(
            runs="python3 -X dev ops/zz/test_zz.py",
            suites=["ops/zz/test_zz.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_case_arm_in_function_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "run() {\n"
                "  case \"$1\" in\n"
                "    test_zz.sh) echo hit ;;\n"
                "  esac\n"
                "}\n"),
            "ops/zzprobe/test_zz.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.sh", errors[0])

    def test_grep_in_function_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "run() {\n"
                "  grep -q test_zz.sh README.md\n"
                "}\n"),
            "ops/zzprobe/test_zz.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.sh", errors[0])

    def test_bash_n_in_function_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "run() {\n"
                "  bash -n ./test_zz.sh\n"
                "}\n"),
            "ops/zzprobe/test_zz.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.sh", errors[0])

    def test_run_in_function_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "run() {\n"
                "  ./test_zz.sh\n"
                "}\n"),
            "ops/zzprobe/test_zz.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_pytest_module_wires(self):
        root = repo_with(
            runs="python3 -m pytest ops/zz/test_zz.py",
            suites=["ops/zz/test_zz.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_py_suite_outside_os_system_is_orphan(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import os\n"
                "TARGET = \"./test_zz_os.py\"\n"
                "os.system(\"echo hi\")\n"),
            "ops/zzprobe/test_zz_os.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_os.py", errors[0])

    def test_captured_substitution_run_wires(self):
        root = repo_with(
            runs='out="$({ ./test_zz_o.sh; } 2>&1)"; echo "$out"',
            suites=["test_zz_o.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_make_repo_registers_cleanup(self):
        root = repo_with()
        self.assertIn(root, CREATED_REPOS)

    def test_verify_fast_without_parse_marker_fails_closed(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="echo nothing wired"),
            "scripts/verify-fast.sh": (
                "#!/usr/bin/env bash\nset -euo pipefail\necho ok\n"),
        })
        with self.assertRaises(RuntimeError):
            ratchet.check(root, {})

    def test_unscoped_discover_does_not_wire_nested_suite(self):
        root = repo_with(
            runs="python3 -m unittest discover -p 'test_*.py'",
            suites=["ops/zz/test_zz.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_dot_discover_does_not_wire_nested_suite(self):
        root = repo_with(
            runs="python3 -m unittest discover -s . -p 'test_*.py'",
            suites=["test_zz.py", "ops/zz/test_zz.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("ops/zz/test_zz.py", errors[0])

    def test_discover_start_dir_wires_direct_child_not_grandchild(self):
        root = repo_with(
            runs="python3 -m unittest discover -s ops/zz -p 'test_*.py'",
            suites=["ops/zz/test_a.py", "ops/zz/sub/test_b.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("ops/zz/sub/test_b.py", errors[0])

    def test_discover_wires_nested_package_suite(self):
        root = repo_with(
            runs="python3 -m unittest discover -s ops -p 'test_*.py'",
            suites=["ops/pkg/test_p.py", "ops/pkg/__init__.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_discover_without_s_resolves_cd_prefix(self):
        root = repo_with(
            runs="(cd other && python3 -m unittest discover -p 'test_*.py')",
            suites=["ops/zz/test_zz.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz.py", errors[0])

    def test_discover_without_s_wires_suite_under_cd_prefix(self):
        root = repo_with(
            runs="(cd other && python3 -m unittest discover -p 'test_*.py')",
            suites=["other/test_cd.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_working_directory_scopes_discover(self):
        root = repo_with_steps(
            "      - name: probe\n"
            "        working-directory: other\n"
            "        run: python3 -m unittest discover -p 'test_*.py'\n",
            suites=["other/test_wd.py", "ops/zz/test_zz.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("ops/zz/test_zz.py", errors[0])

    def test_working_directory_after_run_scopes_discover(self):
        root = repo_with_steps(
            "      - name: probe\n"
            "        run: python3 -m unittest discover -p 'test_*.py'\n"
            "        working-directory: other\n",
            suites=["other/test_zz_wd_after.py",
                    "test_zz_wd_after_root.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_wd_after_root.py", errors[0])

    def test_working_directory_after_long_run_body_scopes_discover(self):
        body = "".join(f"          echo line {n}\n" for n in range(30))
        root = repo_with_steps(
            "      - name: probe\n"
            "        run: |\n" + body +
            "          python3 -m unittest discover -p 'test_*.py'\n"
            "        working-directory: other\n",
            suites=["other/test_zz_wd_long.py",
                    "test_zz_wd_long_root.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_wd_long_root.py", errors[0])

    def test_job_defaults_working_directory_fails_closed(self):
        root = make_repo({
            ".github/workflows/ci.yml": (
                "name: CI\non:\n  pull_request:\njobs:\n  suites:\n"
                "    runs-on: ubuntu-latest\n"
                "    defaults:\n"
                "      run:\n"
                "        working-directory: pkg\n"
                "    steps:\n"
                "      - name: probe\n"
                "        run: bash ./test_zz_jd.sh\n"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "test_zz_jd.sh": "#!/usr/bin/env bash\necho fixture\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_jd.sh", errors[0])

    def test_step_working_directory_overrides_job_defaults(self):
        root = make_repo({
            ".github/workflows/ci.yml": (
                "name: CI\non:\n  pull_request:\njobs:\n  suites:\n"
                "    runs-on: ubuntu-latest\n"
                "    defaults:\n"
                "      run:\n"
                "        working-directory: pkg\n"
                "    steps:\n"
                "      - name: probe\n"
                "        working-directory: .\n"
                "        run: bash ./test_zz_jd2.sh\n"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "test_zz_jd2.sh": "#!/usr/bin/env bash\necho fixture\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_echo_backslash_continuation_does_not_wire(self):
        root = repo_with_steps(
            "      - name: probe\n"
            "        run: |\n"
            "          echo \"note: proof lives in\" \\\n"
            "            ./test_zz_bs_cont.sh\n",
            suites=["test_zz_bs_cont.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_bs_cont.sh", errors[0])

    def test_env_dash_u_invocation_wires(self):
        root = repo_with(
            runs="env -u BASH_ENV ./test_zz_env.sh",
            suites=["test_zz_env.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_nice_dash_n_invocation_wires(self):
        root = repo_with(
            runs="nice -n 19 bash ./test_zz_nice.sh",
            suites=["test_zz_nice.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_env_dash_i_invocation_wires(self):
        root = repo_with(
            runs="env -i ./test_zz_ei.sh",
            suites=["test_zz_ei.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_sudo_dash_n_invocation_wires(self):
        root = repo_with(
            runs="sudo -n bash ./test_zz_sn.sh",
            suites=["test_zz_sn.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_sudo_dash_u_invocation_wires(self):
        root = repo_with(
            runs="sudo -u runner bash ./test_zz_sudo.sh",
            suites=["test_zz_sudo.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_bare_pytest_with_file_arg_wires(self):
        root = repo_with(
            runs="pytest -q ops/zz/test_zz_pyt.py",
            suites=["ops/zz/test_zz_pyt.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_uv_run_python_wires(self):
        root = repo_with(
            runs="uv run python ops/zz/test_zz_uv.py",
            suites=["ops/zz/test_zz_uv.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_coverage_run_wires(self):
        root = repo_with(
            runs="coverage run ops/zz/test_zz_cov.py",
            suites=["ops/zz/test_zz_cov.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_coverage_report_does_not_wire(self):
        root = repo_with(
            runs="coverage report ops/zz/test_zz_cov2.py",
            suites=["ops/zz/test_zz_cov2.py"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_cov2.py", errors[0])

    def test_variable_command_wires(self):
        root = repo_with(
            runs='"$PY" ops/zz/test_zz_dpy.py',
            suites=["ops/zz/test_zz_dpy.py"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_source_invocation_wires(self):
        root = repo_with(
            runs="source ./test_zz_src.sh",
            suites=["test_zz_src.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_multiline_for_loop_wires(self):
        root = repo_with_steps(
            "      - name: probe\n"
            "        run: |\n"
            "          for t in ops/zz/test_zz.sh ops/zz/test_b.sh; do\n"
            '            bash "$t"\n'
            "          done\n",
            suites=["ops/zz/test_zz.sh", "ops/zz/test_b.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_for_loop_with_case_body_wires(self):
        root = repo_with_steps(
            "      - name: probe\n"
            "        run: |\n"
            "          for t in ops/zz/test_zz_c.sh; do\n"
            '            case "$t" in\n'
            '              *.sh) bash "$t";;\n'
            "            esac\n"
            "          done\n",
            suites=["ops/zz/test_zz_c.sh"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_function_forwarding_args_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "run_suite() { bash \"$@\"; }\n"
                "run_suite ops/zzprobe/test_zz_fn.py\n"),
            "ops/zzprobe/test_zz_fn.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_function_first_arg_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "deploy() {\n"
                "  bash \"$1\"\n"
                "}\n"
                "deploy ops/zzprobe/test_zz_fn1.py\n"),
            "ops/zzprobe/test_zz_fn1.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_function_inspector_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "lint() { shellcheck \"$1\"; }\n"
                "lint ops/zzprobe/test_zz_fn2.sh\n"),
            "ops/zzprobe/test_zz_fn2.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_fn2.sh", errors[0])

    def test_multiline_sha1sum_loop_does_not_wire(self):
        root = repo_with_steps(
            "      - name: probe\n"
            "        run: |\n"
            "          for f in ops/zz/test_zz_h.sh; do\n"
            '            sha1sum "$f"\n'
            "          done\n",
            suites=["ops/zz/test_zz_h.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_h.sh", errors[0])

    def test_unknown_runner_loop_does_not_wire(self):
        root = repo_with(
            runs='for f in ops/zz/test_zz_u.sh; do ./run.sh "$f"; done',
            suites=["ops/zz/test_zz_u.sh"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_u.sh", errors[0])

    def test_bash_n_subprocess_list_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import subprocess\n"
                "subprocess.run([\"bash\", \"-n\", "
                "\"ops/zzprobe/test_zz_n.py\"])\n"),
            "ops/zzprobe/test_zz_n.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_n.py", errors[0])

    def test_mypy_module_subprocess_list_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import subprocess\n"
                "subprocess.run([\"python3\", \"-m\", \"mypy\", "
                "\"ops/zzprobe/test_zz_my.py\"])\n"),
            "ops/zzprobe/test_zz_my.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_my.py", errors[0])

    def test_sys_executable_subprocess_list_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import subprocess\n"
                "import sys\n"
                "subprocess.run([sys.executable, "
                "\"ops/zzprobe/test_zz_se.py\"])\n"),
            "ops/zzprobe/test_zz_se.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_bash_subprocess_list_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import subprocess\n"
                "subprocess.run([\"bash\", "
                "\"ops/zzprobe/test_zz_bl.sh\"])\n"),
            "ops/zzprobe/test_zz_bl.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_os_system_shellcheck_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import os\n"
                "os.system(\"shellcheck ./test_zz_sc.py\")\n"),
            "ops/zzprobe/test_zz_sc.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_sc.py", errors[0])

    def test_os_system_bash_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import os\n"
                "os.system(\"bash ./test_zz_sb.py\")\n"),
            "ops/zzprobe/test_zz_sb.py": "import unittest\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_subprocess_string_shellcheck_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import subprocess\n"
                "subprocess.run(\"shellcheck ./test_zz_ss.py\", "
                "shell=True)\n"),
            "ops/zzprobe/test_zz_ss.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_ss.py", errors[0])

    def test_subprocess_count_mention_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 ops/zzprobe/zz_impl.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_impl.py": (
                "import subprocess\n"
                "subprocess.check_output([\"git\", \"ls-files\"]).count(\n"
                "    \"test_zz_cc.py\")\n"),
            "ops/zzprobe/test_zz_cc.py": "import unittest\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_cc.py", errors[0])

    def test_var_assignment_then_inspector_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "SUITE=ops/zzprobe/test_zz_vi.sh\n"
                "shellcheck \"$SUITE\"\n"),
            "ops/zzprobe/test_zz_vi.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_vi.sh", errors[0])

    def test_unused_var_assignment_does_not_wire(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="bash ops/zzprobe/zz_helper.sh"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "ops/zzprobe/zz_helper.sh": (
                "#!/usr/bin/env bash\n"
                "SUITE=ops/zzprobe/test_zz_vu.sh\n"
                "echo done\n"),
            "ops/zzprobe/test_zz_vu.sh": (
                "#!/usr/bin/env bash\necho fixture suite\n"),
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_vu.sh", errors[0])

    def test_root_flag_uses_target_allowlist(self):
        root = repo_with(runs="./test_alpha.sh", suites=["test_alpha.sh"],
                         allowlist={})
        self.assertEqual(ratchet.main(["--root", str(root)]), 0)

    def test_shell_tokens_split_newlines(self):
        tokens = ratchet._shell_tokens('\n  bash "$t"\n')
        self.assertEqual(
            [(text, start, end) for text, start, end in tokens],
            [("bash", 3, 7), ("$t", 8, 12)])


def repo_with_npm(script_map, package_dir="pkg", run="npm test",
                  allowlist=None, suites=None):
    """Temp repo whose ci.yml step runs npm in ``package_dir``."""
    files = {
        ".github/workflows/ci.yml": (
            "name: CI\non:\n  pull_request:\njobs:\n  suites:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: package suite\n"
            f"        working-directory: {package_dir}\n"
            f"        run: {run}\n"),
        "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
        f"{package_dir}/package.json": json.dumps(
            {"scripts": script_map}, indent=2) + "\n",
    }
    for name in (suites or []):
        files[name] = "import test from 'node:test';\n"
    if allowlist is not None:
        files["verification/orphan_suites_allowlist.json"] = (
            json.dumps(allowlist, indent=2) + "\n")
    return make_repo(files)


class OrphanRatchetJsTest(unittest.TestCase):
    """JS/TS spec wiring: node --test, npm scripts, vitest run."""

    def test_spec_orphan_fails_and_names_the_file(self):
        root = repo_with(suites=["pkg/test/zz_alpha.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("pkg/test/zz_alpha.test.mjs", errors[0])

    def test_test_prefix_mjs_suite_is_listed(self):
        root = repo_with(suites=["test_zz_alpha.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("test_zz_alpha.mjs", errors[0])

    def test_node_test_single_file_wires(self):
        root = repo_with(
            runs="node --test pkg/test/zz_alpha.test.mjs",
            suites=["pkg/test/zz_alpha.test.mjs"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_node_test_second_file_wires(self):
        root = repo_with(
            runs="node --test hooks/zz_one.test.mjs "
                 "hooks/zz_two.test.mjs",
            suites=["hooks/zz_one.test.mjs", "hooks/zz_two.test.mjs"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_node_test_reporter_flag_wires(self):
        root = repo_with(
            runs="node --test --test-reporter=tap pkg/test/zz_alpha.test.mjs",
            suites=["pkg/test/zz_alpha.test.mjs"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_node_check_does_not_wire(self):
        root = repo_with(
            runs="node --check pkg/test/zz_alpha.test.mjs",
            suites=["pkg/test/zz_alpha.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_alpha.test.mjs", errors[0])

    def test_node_test_name_pattern_value_does_not_wire(self):
        # The --test-name-pattern value is a test-name regex, not a file
        # target, even when it spells a suite basename. (The trailing
        # `other` target keeps the run from going bare, which would
        # cover the directory on its own.)
        root = repo_with(
            runs='node --test --test-name-pattern "zz_alpha.test.mjs" '
                 'other',
            suites=["pkg/test/zz_alpha.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_alpha.test.mjs", errors[0])

    def test_node_test_flag_after_script_does_not_run(self):
        # `node runner.js --test` passes --test to the script; the test
        # runner never starts, so nothing wires.
        root = repo_with(
            runs="node scripts/zz_runner.js --test",
            suites=["zz_probe.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_probe.test.mjs", errors[0])

    def test_node_file_direct_wires(self):
        root = repo_with(
            runs="node pkg/test/zz_alpha.test.mjs",
            suites=["pkg/test/zz_alpha.test.mjs"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_node_test_glob_wires_only_its_directory(self):
        # Shell globs do not cross `/`: the nested suite stays an orphan.
        root = repo_with(
            runs="cd pkg && node --test 'test/*.test.mjs'",
            suites=["pkg/test/zz_a.test.mjs",
                    "pkg/test/zz_b.test.mjs",
                    "pkg/test/nested/zz_c.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("pkg/test/nested/zz_c.test.mjs", errors[0])

    def test_node_test_bare_directory_skips_specs(self):
        # A bare directory covers *.test.{mjs,cjs,js} only: specs are
        # outside the node runner's default file matching.
        root = repo_with(
            runs="node --test pkg/test",
            suites=["pkg/test/zz_a.test.mjs",
                    "pkg/test/zz_b.spec.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_b.spec.mjs", errors[0])

    def test_bare_node_test_wires_root_test_files(self):
        root = repo_with(
            runs="node --test",
            suites=["zz_a.test.mjs", "zz_b.spec.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_b.spec.mjs", errors[0])

    def test_npm_test_resolves_package_script(self):
        root = repo_with_npm(
            {"test": "node --test test/zz_a.test.mjs test/zz_b.test.mjs"},
            suites=["pkg/test/zz_a.test.mjs",
                    "pkg/test/zz_b.test.mjs",
                    "pkg/test/zz_c.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("pkg/test/zz_c.test.mjs", errors[0])

    def test_npm_test_without_package_json_wires_nothing(self):
        root = make_repo({
            ".github/workflows/ci.yml": (
                "name: CI\non:\n  pull_request:\njobs:\n  suites:\n"
                "    runs-on: ubuntu-latest\n    steps:\n"
                "      - name: package suite\n"
                "        working-directory: pkg\n"
                "        run: npm test\n"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "pkg/test/zz_a.test.mjs": "import test from 'node:test';\n",
        })
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_a.test.mjs", errors[0])

    def test_working_directory_after_run_scopes_npm(self):
        root = make_repo({
            ".github/workflows/ci.yml": (
                "name: CI\non:\n  pull_request:\njobs:\n  suites:\n"
                "    runs-on: ubuntu-latest\n    steps:\n"
                "      - name: package suite\n"
                "        run: npm test\n"
                "        working-directory: pkg\n"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "pkg/package.json": json.dumps(
                {"scripts": {
                    "test": "node --test test/zz_after.test.mjs"}},
                indent=2) + "\n",
            "pkg/test/zz_after.test.mjs":
                "import test from 'node:test';\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_npm_run_check_does_not_wire(self):
        root = repo_with_npm(
            {"check": "node --check test/zz_a.test.mjs"},
            run="npm run check",
            suites=["pkg/test/zz_a.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_a.test.mjs", errors[0])

    def test_npm_test_glob_script_wires(self):
        root = repo_with_npm(
            {"test": "node --test test/*.test.mjs"},
            suites=["pkg/test/zz_a.test.mjs",
                    "pkg/test/nested/zz_b.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("pkg/test/nested/zz_b.test.mjs", errors[0])

    def test_npm_test_nested_run_script_wires(self):
        root = repo_with_npm(
            {"test": "npm run zz_unit",
             "zz_unit": "node --test test/zz_a.test.mjs"},
            suites=["pkg/test/zz_a.test.mjs"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_echo_npm_test_does_not_wire(self):
        root = repo_with_npm(
            {"test": "node --test test/zz_a.test.mjs"},
            run="echo \"run npm test by hand\"",
            suites=["pkg/test/zz_a.test.mjs"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_a.test.mjs", errors[0])

    def test_vitest_run_wires_specs(self):
        root = repo_with_npm(
            {"test": "vitest run"},
            suites=["pkg/tests/zz_a.spec.ts",
                    "pkg/tests/zz_b.test.ts"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_npx_vitest_run_wires_specs(self):
        root = repo_with_npm(
            {"test": "npx vitest run"},
            suites=["pkg/tests/zz_a.spec.ts"])
        self.assertEqual(ratchet.check(root, {}), [])

    def test_vitest_run_with_filter_wires_only_it(self):
        root = repo_with_npm(
            {"test": "vitest run tests/zz_a.spec.ts"},
            suites=["pkg/tests/zz_a.spec.ts",
                    "pkg/tests/zz_b.spec.ts"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("pkg/tests/zz_b.spec.ts", errors[0])

    def test_vitest_without_run_does_not_wire(self):
        # Bare `vitest` starts watch mode; it runs no suite files.
        root = repo_with_npm(
            {"test": "vitest"},
            suites=["pkg/tests/zz_a.spec.ts"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_a.spec.ts", errors[0])

    def test_grep_vitest_does_not_wire(self):
        root = repo_with_npm(
            {"test": "vitest run"},
            run="grep -r vitest pkg | head",
            suites=["pkg/tests/zz_a.spec.ts"])
        errors = ratchet.check(root, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_a.spec.ts", errors[0])

    def test_stale_exemption_for_js_suite_fails(self):
        root = repo_with(
            runs="node --test pkg/test/zz_a.test.mjs",
            suites=["pkg/test/zz_a.test.mjs"])
        allow = {"pkg/test/zz_a.test.mjs": "needs a browser, not run in CI"}
        errors = ratchet.check(root, allow)
        self.assertEqual(len(errors), 1)
        self.assertIn("zz_a.test.mjs", errors[0])

    def test_js_exemption_passes(self):
        root = repo_with(suites=["pkg/test/zz_a.test.mjs"])
        allow = {"pkg/test/zz_a.test.mjs": "needs a browser, not run in CI"}
        self.assertEqual(ratchet.check(root, allow), [])

    def test_py_subprocess_node_test_wires(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(
                runs="python3 scripts/zz_js_helper.py"),
            "scripts/verify-fast.sh": VERIFY_FAST.format(body="echo ok"),
            "scripts/zz_js_helper.py": (
                "import subprocess\n"
                "subprocess.run([\"node\", \"--test\", "
                "\"test/zz_a.test.mjs\"], check=True)\n"),
            "test/zz_a.test.mjs": "import test from 'node:test';\n",
        })
        self.assertEqual(ratchet.check(root, {}), [])

    def test_glob_star_does_not_cross_slash(self):
        self.assertTrue(ratchet._glob_match(
            "pkg/test/a.test.mjs", "pkg/test/*.test.mjs"))
        self.assertFalse(ratchet._glob_match(
            "pkg/test/nested/a.test.mjs", "pkg/test/*.test.mjs"))

    def test_glob_double_star_crosses_slash(self):
        self.assertTrue(ratchet._glob_match(
            "pkg/test/nested/a.test.mjs", "pkg/**/a.test.mjs"))
        self.assertFalse(ratchet._glob_match(
            "pkg/test/b.test.mjs", "pkg/**/a.test.mjs"))

    def test_join_target_dotdot_escape_fails_closed(self):
        self.assertIsNone(ratchet._join_target("pkg", "../../zz_x.test.mjs"))
        self.assertIsNone(ratchet._join_target("", "/etc/zz_x.test.mjs"))
        # A `..` that stays inside the root resolves normally.
        self.assertEqual(ratchet._join_target("pkg", "../zz_x.test.mjs"),
                         "zz_x.test.mjs")
        self.assertEqual(
            ratchet._join_target("pkg", "./test/zz_x.test.mjs"),
            "pkg/test/zz_x.test.mjs")


class OrphanStructuredHardeningTest(unittest.TestCase):
    """Regression pins for structured-outcome safeguards.

    Each test kills a one-line revert mutant that the paired
    acceptance suite leaves green: CR filename decoding, the Git
    timeout, the broad CLI exception boundary, and UTF-8 allowlist
    reading. They live here (not in the frozen acceptance file) so
    the contract stays immutable.
    """

    def test_carriage_return_filename_preserved(self):
        name = "test_cr\rbreak.sh"
        root = repo_with(suites=[name], allowlist={})
        self.assertIn(name, ratchet.tracked_files(root))
        self.assertIn(name, ratchet.tracked_suites(root))
        _, findings = ratchet._analyze(root, {})
        self.assertIn(("orphan-suite", name), findings)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ratchet.main(["--json", "--root", str(root)])
        self.assertEqual(code, 1)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["outcome"], "findings")
        self.assertIn(name, [entry["path"] for entry in obj["findings"]])

    def test_git_calls_are_timeout_bounded(self):
        root = repo_with(suites=["test_alpha.sh"], allowlist={})
        with mock.patch.object(ratchet.subprocess, "run",
                               wraps=subprocess.run) as spy:
            ratchet.tracked_files(root)
        self.assertTrue(spy.called)
        for call in spy.call_args_list:
            _, kwargs = call
            self.assertEqual(kwargs.get("timeout"), 30)
        with mock.patch.object(
                ratchet.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="git",
                                                      timeout=30)):
            with self.assertRaises(ratchet.CheckFailure) as ctx:
                ratchet._git(root, "ls-files", "-z")
            self.assertEqual(ctx.exception.code, "git-failed")

    def test_missing_verify_fast_is_analysis_failed(self):
        root = make_repo({
            ".github/workflows/ci.yml": WORKFLOW.format(runs="echo x"),
            "verification/orphan_suites_allowlist.json": "{}\n",
            "test_alpha.sh": "#!/usr/bin/env bash\necho suite\n",
        })
        self.assertFalse((root / "scripts/verify-fast.sh").exists())
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ratchet.main(["--json", "--root", str(root)])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "")
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["outcome"], "unmeasurable")
        self.assertFalse(obj["check_complete"])
        self.assertIsNone(obj["suite_count"])
        self.assertEqual(obj["diagnostic"]["code"], "analysis-failed")

    def test_utf8_allowlist_under_ascii_locale(self):
        reason = "café – needs database"
        root = repo_with(suites=["test_alpha.sh"],
                         allowlist={"test_alpha.sh": reason})
        allow_path = root / "verification/orphan_suites_allowlist.json"
        allow_path.write_bytes(
            json.dumps({"test_alpha.sh": reason},
                       ensure_ascii=False).encode("utf-8") + b"\n")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": tempfile.mkdtemp(prefix="orphan-ascii-home-"),
            "LC_ALL": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        proc = subprocess.run(
            [sys.executable, "-B",
             str(ROOT / "verification" / "orphan_suites.py"),
             "--json", "--root", str(root)],
            capture_output=True, timeout=30, env=env)
        self.assertEqual(proc.stderr.decode("utf-8"), "")
        obj = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(obj["outcome"], "clean")
        self.assertTrue(obj["check_complete"])


if __name__ == "__main__":
    unittest.main()
