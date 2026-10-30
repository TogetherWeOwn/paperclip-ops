#!/usr/bin/env python3
"""Offline CLI acceptance for native squash groups; no workflow or network access.

Run: python3 -B -m unittest discover -s .github/scripts -p test_pr_standards_merge_group.py
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pr_standards as ps


SCRIPT = Path(__file__).resolve().with_name("pr_standards.py")
ABSENT = object()
PUBLIC_PR_BODY = """
## Thinking Path

- The standards check validates the squash title before admission
- Missing native evidence currently leaves no commits to validate
- Reading the native event and local squash history closes that gap

## Linked Issues or Issue Description

Reject a queue group when its native evidence cannot be verified.

## What Changed

- Validate the queued squash messages in the local checkout

## Verification

- Offline Git fixtures exercise the command line

## Risks

- Incomplete local history must refuse admission

## Model Used

- None - human-authored

## Checklist

- [x] I searched for duplicate or related PRs and linked them above

## Merge readiness

<!-- readiness:start -->
## Merge readiness (bot-maintained — do not edit)

Head: pending — the handler fills this on the first event.

| Check | State |
|---|---|

Paperclip Review: pending
Mergeable: pending
Updated: pending
<!-- readiness:end -->
"""


def push_commits(*messages):
    return json.dumps([
        {"id": f"{i:040x}", "message": message}
        for i, message in enumerate(messages, 1)
    ])


class NativeSquashGroup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pr-standards-group-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "checkout"
        self.event_path = self.root / "event.json"
        # Do not inherit runner credentials, Git configuration, hooks, or remotes.
        self.process_env = {
            "PATH": os.defpath,
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "Fixture Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
        }
        self.git("init", "--quiet", "--initial-branch=main", "--object-format=sha1",
                 "--template=", str(self.repo), cwd=self.root)
        self.tree = self.git("mktree", input="").stdout.strip()
        self.base = self.commit("Fixture baseline outside queued range", parents=())
        self.set_queue("fix(ci): validate queued squash (#7)")

    def git(self, *args, input=None, cwd=None, check=True):
        return subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}",
             "-c", "commit.gpgsign=false", *args],
            cwd=cwd or self.repo, env=self.process_env, input=input,
            capture_output=True, text=True, check=check, timeout=10,
        )

    def commit(self, message, *, parents):
        args = ["commit-tree", self.tree]
        for parent in parents:
            args.extend(("-p", parent))
        return self.git(*args, input=message + "\n").stdout.strip()

    def set_head(self, head):
        self.head = head
        self.git("update-ref", "HEAD", head)
        self.write_event(self.native_event())

    def set_queue(self, *messages):
        head = self.base
        for message in messages:
            head = self.commit(message, parents=(head,))
        self.set_head(head)

    def native_event(self):
        return {"action": "checks_requested", "merge_group": {
            "base_sha": self.base, "head_sha": self.head,
        }}

    def write_event(self, data):
        self.event_path.write_text(json.dumps(data), encoding="utf-8")

    def run_cli(self, *, cwd=None, **changes):
        checkout = cwd or self.repo
        env = dict(self.process_env, EVENT="merge_group", GITHUB_EVENT_NAME="merge_group",
                   GITHUB_EVENT_PATH=str(self.event_path), GITHUB_SHA=self.head,
                   GITHUB_WORKSPACE=str(checkout),
                   GITHUB_REPOSITORY="TogetherWeOwn/paperclip-ops",
                   REPO_PRIVATE="false", PR_STANDARDS_MODE="error")
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT)], cwd=checkout, env=env,
            capture_output=True, text=True, timeout=10,
        )

    def run_pr(self, **changes):
        env = dict(EVENT="pull_request", GITHUB_EVENT_NAME="pull_request",
                   GITHUB_EVENT_PATH=None, GITHUB_SHA=None,
                   TITLE="fix(ci): verify queued squash messages", BODY=PUBLIC_PR_BODY,
                   AUTHOR="fixture-user", HEAD_REF="fix/standards-fixture")
        env.update(changes)
        return self.run_cli(**env)

    def assert_accepts(self, result):
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertNotIn("::error ", output)
        self.assertIn("PR conventions OK", output)

    def assert_rejects(self, result):
        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("::error ", output)
        self.assertNotIn("PR conventions OK", output)
        self.assertNotIn("Traceback (most recent call last)", output)

    def test_one_valid_squash_passes_without_commits_payload(self):
        self.assert_accepts(self.run_cli())

    def test_multiple_valid_squashes_pass(self):
        self.set_queue("fix(ci): validate first queued squash (#7)",
                       "docs(ci): explain queue validation (#8)")
        self.assert_accepts(self.run_cli())

    def test_valid_native_history_is_not_replaced_by_commits_payload(self):
        for commits in ("not json", push_commits("Update unrelated push payload")):
            with self.subTest(commits=commits):
                self.assert_accepts(self.run_cli(COMMITS=commits))

    def test_squashed_away_original_subjects_are_not_linted(self):
        original = self.commit("WIP original branch work", parents=(self.base,))
        original = self.commit("Finish original branch work", parents=(original,))
        self.git("update-ref", "refs/heads/original-feature", original)
        self.assert_accepts(self.run_cli())

    def test_missing_event_path_cannot_fall_back_to_commits_payload(self):
        for commits in (None, "null", "not json",
                        push_commits("fix(ci): unrelated payload looks valid (#9)")):
            with self.subTest(commits=commits):
                self.assert_rejects(self.run_cli(GITHUB_EVENT_PATH=None, COMMITS=commits))

    def test_empty_event_path_is_an_error(self):
        self.assert_rejects(self.run_cli(GITHUB_EVENT_PATH=""))

    def test_missing_event_file_is_an_error(self):
        self.assert_rejects(self.run_cli(GITHUB_EVENT_PATH=str(self.root / "absent.json")))

    def test_unreadable_event_path_is_an_error(self):
        # A directory cannot be read as event JSON, including when tests run as root.
        self.assert_rejects(self.run_cli(GITHUB_EVENT_PATH=str(self.root)))

    def test_invalid_event_encoding_or_json_is_an_error(self):
        for raw in (b"", b"{", b"\xff"):
            with self.subTest(raw=raw):
                self.event_path.write_bytes(raw)
                self.assert_rejects(self.run_cli())

    def test_event_data_must_be_an_object(self):
        for data in (None, [], [self.native_event()], "checks_requested", 42, True):
            with self.subTest(data=data):
                self.write_event(data)
                self.assert_rejects(self.run_cli())

    def test_merge_group_must_be_present_and_an_object(self):
        for group in (ABSENT, None, [], "group", 42, True):
            with self.subTest(group=repr(group)):
                event = self.native_event()
                if group is ABSENT:
                    event.pop("merge_group")
                else:
                    event["merge_group"] = group
                self.write_event(event)
                self.assert_rejects(self.run_cli())

    def test_only_checks_requested_action_is_accepted(self):
        for action in (ABSENT, None, "", "destroyed", ["checks_requested"]):
            with self.subTest(action=repr(action)):
                event = self.native_event()
                if action is ABSENT:
                    event.pop("action")
                else:
                    event["action"] = action
                self.write_event(event)
                self.assert_rejects(self.run_cli())

    def test_event_shas_must_be_raw_full_nonzero_object_ids(self):
        for field, valid in (("base_sha", self.base), ("head_sha", self.head)):
            values = (ABSENT, None, True, 1, [], {}, "", "0" * 40, "z" * 40,
                      valid[:12], valid[:-1], valid + "0", "a" * 64,
                      " " + valid, valid + " ", valid + "\n", valid + "^0", "HEAD")
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    event = self.native_event()
                    if value is ABSENT:
                        event["merge_group"].pop(field)
                    else:
                        event["merge_group"][field] = value
                    self.write_event(event)
                    self.assert_rejects(self.run_cli())

    def test_runner_sha_must_be_a_raw_full_nonzero_object_id(self):
        for value in (None, "", "0" * 40, "z" * 40, self.head[:12], self.head[:-1],
                      self.head + "0", "a" * 64, " " + self.head,
                      self.head + " ", self.head + "\n", self.head + "^0", "HEAD"):
            with self.subTest(value=value):
                self.assert_rejects(self.run_cli(GITHUB_SHA=value))

    def test_event_head_must_match_runner_sha(self):
        event = self.native_event()
        event["merge_group"]["head_sha"] = self.base
        self.write_event(event)
        self.assert_rejects(self.run_cli())

    def test_runner_sha_must_match_event_head(self):
        self.assert_rejects(self.run_cli(GITHUB_SHA=self.base))

    def test_matching_event_and_runner_shas_must_match_checked_out_head(self):
        other = self.commit("fix(ci): another local tip (#9)", parents=(self.head,))
        self.git("update-ref", "HEAD", other)
        self.assert_rejects(self.run_cli())

    def test_native_evidence_errors_are_fatal_even_in_warn_mode(self):
        self.assert_rejects(self.run_cli(GITHUB_EVENT_PATH=None, PR_STANDARDS_MODE="warn"))
        self.assert_rejects(self.run_cli(GITHUB_SHA=self.base, PR_STANDARDS_MODE="warn"))

    def test_replacement_object_cannot_hide_invalid_queued_squash(self):
        self.set_queue("Update first queued squash", "fix(ci): valid queued tip (#8)")
        original = self.git("rev-parse", "HEAD^").stdout.strip()
        replacement = self.commit("fix(ci): replacement looks valid (#7)", parents=(self.base,))
        self.git("replace", original, replacement)
        self.assertEqual("fix(ci): replacement looks valid (#7)",
                         self.git("show", "-s", "--format=%s", original).stdout.strip())
        self.assertEqual("Update first queued squash",
                         self.git("--no-replace-objects", "show", "-s", "--format=%s",
                                  original).stdout.strip())
        self.assert_rejects(self.run_cli())

    def assert_native_git_boundary(self, key, expected):
        real_run = subprocess.run
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.repo)
        # Neither absent guards nor inherited permissive values may mask removal.
        inherited_guards = {
            "GIT_NO_LAZY_FETCH": "0",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "1",
        }
        for inherited in ({}, inherited_guards):
            with self.subTest(inherited=inherited):
                env = dict(self.process_env, EVENT="merge_group",
                           GITHUB_EVENT_NAME="merge_group", GITHUB_EVENT_PATH=str(self.event_path),
                           GITHUB_SHA=self.head, GITHUB_WORKSPACE=str(self.repo),
                           GITHUB_REPOSITORY="TogetherWeOwn/paperclip-ops",
                           REPO_PRIVATE="false", PR_STANDARDS_MODE="error")
                for name in inherited_guards:
                    env.pop(name, None)
                env.update(inherited)
                calls = []

                def run_git(command, **kwargs):
                    self.assertEqual("git", command[0])
                    actual = kwargs.get("timeout") if key == "timeout" else kwargs.get("env", {}).get(key)
                    self.assertEqual(expected, actual, f"{key} missing from native Git call: {command}")
                    calls.append(command)
                    # Keep the local dependency bounded even under timeout mutants.
                    return real_run(command, **dict(kwargs, timeout=10))

                output = io.StringIO()
                with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(output), \
                        mock.patch.object(ps.subprocess, "run", side_effect=run_git):
                    result = ps.main()
                self.assertEqual(0, result, output.getvalue())
                self.assertIn("PR conventions OK", output.getvalue())
                self.assertNotIn("::error ", output.getvalue())
                self.assertGreaterEqual(len(calls), 4)

    def test_native_git_disables_lazy_fetch_on_every_command(self):
        self.assert_native_git_boundary("GIT_NO_LAZY_FETCH", "1")

    def test_native_git_disallows_protocols_on_every_command(self):
        self.assert_native_git_boundary("GIT_ALLOW_PROTOCOL", "")

    def test_native_git_disables_terminal_prompts_on_every_command(self):
        self.assert_native_git_boundary("GIT_TERMINAL_PROMPT", "0")

    def test_native_git_bounds_every_command_with_timeout(self):
        self.assert_native_git_boundary("timeout", 10)

    def test_empty_base_to_head_range_is_an_error(self):
        self.set_queue()
        self.assert_rejects(self.run_cli())

    def test_missing_base_object_is_an_error(self):
        missing = "f" * 40
        self.assertNotEqual(0, self.git("cat-file", "-e", missing, check=False).returncode)
        event = self.native_event()
        event["merge_group"]["base_sha"] = missing
        self.write_event(event)
        self.assert_rejects(self.run_cli())

    def test_unrelated_resolvable_base_is_an_error(self):
        unrelated = self.commit("fix(ci): unrelated root (#9)", parents=())
        self.assertEqual(1, self.git("merge-base", "--is-ancestor", unrelated, self.head,
                                     check=False).returncode)
        event = self.native_event()
        event["merge_group"]["base_sha"] = unrelated
        self.write_event(event)
        self.assert_rejects(self.run_cli())

    def test_shallow_checkout_without_base_is_an_error(self):
        shallow = self.root / "shallow"
        self.git("clone", "--quiet", "--no-local", "--depth=1",
                 self.repo.as_uri(), str(shallow), cwd=self.root)
        self.assertEqual("true", self.git("rev-parse", "--is-shallow-repository",
                                          cwd=shallow).stdout.strip())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD", cwd=shallow).stdout.strip())
        self.assertNotEqual(0, self.git("cat-file", "-e", self.base,
                                       cwd=shallow, check=False).returncode)
        self.assert_rejects(self.run_cli(cwd=shallow))

    def test_missing_intermediate_history_is_an_error(self):
        self.set_queue("fix(ci): first queued squash (#7)",
                       "fix(ci): second queued squash (#8)")
        middle = self.git("rev-parse", "HEAD^").stdout.strip()
        loose_object = self.repo / ".git" / "objects" / middle[:2] / middle[2:]
        loose_object.rename(self.root / "unavailable-parent-object")
        self.assertEqual(0, self.git("cat-file", "-e", self.base).returncode)
        self.assertEqual(0, self.git("cat-file", "-e", self.head).returncode)
        self.assert_rejects(self.run_cli())

    def test_merge_parent_topology_is_not_a_squash_even_with_valid_subject(self):
        side = self.commit("fix(ci): side branch (#8)", parents=(self.base,))
        merge = self.commit("fix(ci): conventional title on a merge (#9)",
                            parents=(self.head, side))
        self.set_head(merge)
        self.assert_rejects(self.run_cli())

    def test_base_reachable_only_as_second_parent_is_an_error(self):
        side = self.commit("fix(ci): side branch (#8)", parents=(self.base,))
        merge = self.commit("fix(ci): conventional title on a merge (#9)",
                            parents=(self.head, side))
        self.set_head(merge)
        self.assertEqual(0, self.git("merge-base", "--is-ancestor", side, merge).returncode)
        self.assertNotIn(side, self.git("rev-list", "--first-parent", merge).stdout.splitlines())
        event = self.native_event()
        event["merge_group"]["base_sha"] = side
        self.write_event(event)
        self.assert_rejects(self.run_cli())

    def test_no_git_checkout_is_an_error(self):
        self.assert_rejects(self.run_cli(cwd=self.root))

    def test_invalid_non_tip_squash_is_not_hidden_by_valid_tip_or_commits_payload(self):
        self.set_queue("Update first queued squash", "fix(ci): valid queued tip (#8)")
        self.assert_rejects(self.run_cli(COMMITS=push_commits("fix(ci): valid fallback (#9)")))

    def test_invalid_tip_is_an_error_even_in_warn_mode(self):
        self.set_queue("Update queued tip")
        for mode in ("warn", "error"):
            with self.subTest(mode=mode):
                self.assert_rejects(self.run_cli(PR_STANDARDS_MODE=mode))

    def test_all_generated_merge_subjects_cannot_produce_empty_success(self):
        self.set_queue("Merge branch 'first'", "Merge pull request #8 from example/second")
        self.assert_rejects(self.run_cli())

    def test_generated_merge_subject_is_not_skipped_before_valid_tip(self):
        self.set_queue("Merge branch 'first'", "fix(ci): valid queued tip (#8)")
        self.assert_rejects(self.run_cli())

    def test_native_generated_revert_tip_is_rejected_in_both_modes(self):
        self.set_queue('Revert "previous change"')
        for mode in ("warn", "error"):
            with self.subTest(mode=mode):
                result = self.run_cli(PR_STANDARDS_MODE=mode)
                self.assert_rejects(result)
                self.assertIn("::error title=Commit on main::", result.stdout)

    def test_native_generated_revert_is_not_skipped_before_valid_tip_in_both_modes(self):
        self.set_queue('Revert "previous change"', "fix(ci): valid queued tip (#8)")
        for mode in ("warn", "error"):
            with self.subTest(mode=mode):
                result = self.run_cli(PR_STANDARDS_MODE=mode)
                self.assert_rejects(result)
                self.assertIn("::error title=Commit on main::", result.stdout)

    def test_full_non_tip_squash_message_uses_existing_public_reference_severity(self):
        internal_ref = "-".join(("TOG", "999999991"))
        self.set_queue("fix(ci): clean queued subject (#7)\n\nRefs: " + internal_ref,
                       "fix(ci): clean queued tip (#8)")
        for mode in ("warn", "error"):
            with self.subTest(mode=mode):
                result = self.run_cli(PR_STANDARDS_MODE=mode)
                if mode == "error":
                    self.assert_rejects(result)
                else:
                    self.assert_accepts(result)
                    self.assertIn("::warning title=Internal reference::", result.stdout)

    def test_pull_request_uses_title_not_local_commit_subjects(self):
        self.set_queue("WIP original branch commit")
        self.assert_accepts(self.run_pr())

    def test_pull_request_bad_title_stays_fatal_in_warn_mode(self):
        self.assert_rejects(self.run_pr(TITLE="Update stuff", PR_STANDARDS_MODE="warn"))

    def test_pull_request_template_warning_stays_nonfatal(self):
        result = self.run_pr(BODY=PUBLIC_PR_BODY.replace("## Risks", "## Notes"),
                             PR_STANDARDS_MODE="warn")
        self.assert_accepts(result)
        self.assertIn("::warning title=PR template::", result.stdout)

    def test_valid_push_does_not_require_native_group_evidence(self):
        self.assert_accepts(self.run_cli(EVENT="push", GITHUB_EVENT_NAME="push",
                                         GITHUB_EVENT_PATH=None, GITHUB_SHA=None,
                                         COMMITS=push_commits("fix(ci): valid push (#7)")))

    def test_push_bad_header_stays_fatal_in_warn_mode(self):
        self.assert_rejects(self.run_cli(EVENT="push", GITHUB_EVENT_NAME="push",
                                         PR_STANDARDS_MODE="warn",
                                         COMMITS=push_commits("Update stuff")))

    def test_push_generated_merge_and_revert_keep_their_exemption(self):
        self.assert_accepts(self.run_cli(EVENT="push", GITHUB_EVENT_NAME="push",
                                         COMMITS=push_commits("Merge branch 'first'",
                                                              'Revert "previous change"')))

    def test_push_legacy_empty_or_malformed_commits_handling_is_unchanged(self):
        for commits in (None, "", "null", "not json"):
            with self.subTest(commits=commits):
                self.assert_accepts(self.run_cli(EVENT="push", GITHUB_EVENT_NAME="push",
                                                 GITHUB_EVENT_PATH=None, GITHUB_SHA=None,
                                                 COMMITS=commits))

    def test_push_public_reference_severity_is_unchanged(self):
        internal_ref = "-".join(("TOG", "999999991"))
        commits = push_commits("fix(ci): clean push subject (#7)\n\nRefs: " + internal_ref)
        for mode in ("warn", "error"):
            with self.subTest(mode=mode):
                result = self.run_cli(EVENT="push", GITHUB_EVENT_NAME="push",
                                      COMMITS=commits, PR_STANDARDS_MODE=mode)
                if mode == "error":
                    self.assert_rejects(result)
                else:
                    self.assert_accepts(result)
                    self.assertIn("::warning title=Internal reference::", result.stdout)


if __name__ == "__main__":
    unittest.main()
