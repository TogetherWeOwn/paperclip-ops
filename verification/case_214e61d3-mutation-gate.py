#!/usr/bin/env python3
"""case_214e61d3-mutation-gate.py -- proof that the upstream pipeline suites are not vacuous.

The pre-push linter, the maintainer-memory rules, the reputation pass and the
parallel-gate logic in upstream_gate.sh are tested by three suites:

    test_upstream_lint.py        (the linter, memory, batch policy, git hook)
    test_upstream_reputation.py  (the second-model pass and its receipt)
    test_upstream_gate.sh        (publish, ready, status, audit, watcher)

Green assertions about a gate are worth nothing until somebody breaks the gate
and watches them go red. This stages a COPY of the tools, removes exactly ONE
limb per mutation, runs the suite that claims to cover it, and requires the
NAMED case to fail. "The suite went red somewhere" is not evidence for the case
under test, so the named case must be among the failures.

The unmutated copy is run FIRST in the same staging layout: a mutated suite that
fails is unattributable if staging itself is broken.

    python3 verification/case_214e61d3-mutation-gate.py [--only ID_SUBSTRING] [-j N]

Exit 0 = every mutation was detected by the case that claims to cover it.
Nothing in the real tree is touched; every mutant lives in its own temp copy.
"""
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
COPY = ["upstream_gate.sh", "upstream_lint.py", "upstream_reputation.py", "upstream_prepush_hook.sh",
        "upstream_watch.sh", "test_upstream_gate.sh", "test_upstream_lint.py", "test_upstream_reputation.py",
        "tests/fixtures/upstream-public-template.json"]
SUITES = {
    "lint": ["python3", "-m", "unittest", "test_upstream_lint"],
    "rep": ["python3", "-m", "unittest", "test_upstream_reputation"],
    "gate": ["bash", "./test_upstream_gate.sh"],
}

LINT, REP, GATE = "upstream_lint.py", "upstream_reputation.py", "upstream_gate.sh"
HOOK = "upstream_prepush_hook.sh"

# (id, file, old, new, suite, [case names that must go red])
MUTANTS = [
    # ---- linter ---------------------------------------------------------------
    ("lint-dco-off", LINT, 'dco, cla = bool(so.get("dco")), bool(so.get("cla"))',
     'dco, cla = False, bool(so.get("cla"))', "lint",
     ["test_dco_missing_signoff", "test_dco_signoff_must_match_author"]),
    ("lint-cla-evidence-ignored", LINT, '        ev = so.get("cla_evidence")\n        if ev:',
     '        ev = so.get("cla_evidence")\n        if True:', "lint", ["test_cla_without_evidence_fails"]),
    ("lint-cla-coupled-to-dco", LINT, '    if cla:\n        ev = so.get("cla_evidence")',
     '    if cla and not dco:\n        ev = so.get("cla_evidence")', "lint",
     ["test_dco_does_not_skip_independent_cla_requirement"]),
    ("lint-signoff-fetch-failure-overwrites-memory", LINT,
     '        if not is_missing_api_resource(exc):\n            raise Refused("cannot read signoff policy from CONTRIBUTING.md; memory unchanged: %s" % exc)',
     '        if False:\n            raise Refused("cannot read signoff policy from CONTRIBUTING.md; memory unchanged: %s" % exc)',
     "lint", ["test_signoff_policy_fetch_failure_does_not_overwrite_memory"]),
    ("lint-signoff-listing-failure-overwrites-memory", LINT,
     '        except Refused as exc:\n            if is_missing_api_resource(exc):\n                continue\n            raise Refused("cannot read signoff policy paths; memory unchanged: %s" % exc)',
     '        except Refused as exc:\n            if not is_missing_api_resource(exc):\n                continue\n            raise Refused("cannot read signoff policy paths; memory unchanged: %s" % exc)',
     "lint", ["test_signoff_policy_listing_failure_does_not_overwrite_memory"]),
    ("lint-budget-lines", LINT, "if lines > max_lines:", "if lines > max_lines * 1000:", "lint", ["test_too_many_lines"]),
    ("lint-budget-files", LINT, "if nfiles > max_files:", "if nfiles > max_files * 1000:", "lint", ["test_too_many_files"]),
    ("lint-split-request-ignored", LINT,
     'asked = [f for f in mem.get("feedback", [])\n             if f.get("kind") == "maintainer" and f.get("split_request") and f.get("status") != "resolved"]',
     "asked = []", "lint", ["test_maintainer_split_request_lowers_the_budget"]),
    ("lint-budget-counts-any-kind", LINT,
     'if f.get("kind") == "maintainer" and f.get("split_request") and f.get("status") != "resolved"]',
     'if f.get("split_request") and f.get("status") != "resolved"]', "lint",
     ["test_commenter_split_request_does_not_lower_the_budget"]),
    ("lint-tests-always-pass", LINT, "    if not src:\n", "    if True:\n", "lint", ["test_behavior_change_without_test"]),
    ("lint-test-needs-assertions", LINT,
     '        if not any(TEST_ASSERT.search(ln) for ln in code):\n            if dropped:',
     "        if False:\n            if dropped:", "lint", ["test_test_file_without_assertions_does_not_count"]),
    ("lint-empty-test-declaration-counts", LINT,
     '        if not any(TEST_ASSERT.search(ln) for ln in code):\n            if dropped:',
     '        if not any(TEST_ASSERT.search(ln) or re.search(r"\\b(?:it|test|describe)\\s*[.(]", ln) for ln in added_lines(patch)):\n            if dropped:',
     "lint", ["test_empty_test_declaration_is_not_an_assertion"]),
    ("lint-assert-import-counts", LINT,
     r'''    r"\{[ \t]*assert[ \t]+(?![=,}]|as\b|from\b)\S|\b(?=\w*assert)\w+!\s*\(|\bassert\s*<[^\n]*>\s*\(", re.M)''',
     r'''    r"\{[ \t]*assert[ \t]+\S|\b(?=\w*assert)\w+!\s*\(|\bassert\s*<[^\n]*>\s*\(", re.M)''', "lint",
     ["test_assert_identifier_without_assertion_does_not_count"]),
    ("lint-inline-java-assert-ignored", LINT,
     r'''    r"\{[ \t]*assert[ \t]+(?![=,}]|as\b|from\b)\S|\b(?=\w*assert)\w+!\s*\(|\bassert\s*<[^\n]*>\s*\(", re.M)''',
     r'''    r"\b(?=\w*assert)\w+!\s*\(|\bassert\s*<[^\n]*>\s*\(", re.M)''', "lint",
     ["test_inline_java_assertion_counts"]),
    ("lint-test-no-patch-refuses", LINT, "    if unscanned_tests:\n", "    if False:\n", "lint",
     ["test_test_file_without_patch_refuses"]),
    ("lint-test-rename-no-patch", LINT, '            if f.get("status") != "removed" and changed_without_patch(f):',
     '            if f.get("status") != "removed":', "lint",
     ["test_renamed_test_file_without_patch_is_fine"]),
    ("lint-test-same-package", LINT, "        if pkg in t_by_pkg:\n", "        if t_by_pkg:\n", "lint",
     ["test_test_in_other_package_does_not_count"]),
    ("lint-added-line-scan", LINT,
     '        for ln in added_lines(patch):\n            for h in text_hits(ln, CODE_PATTERNS):\n                hits.append("added line in %s holds %r" % (name, h))',
     '        for ln in added_lines(patch):\n            for h in []:\n                hits.append("added line in %s holds %r" % (name, h))', "lint",
     ["test_pap_id_in_added_line", "test_nobody_mode_still_scans_commits_and_diff"]),
    ("lint-control-separated-patch-lines", LINT,
     '    return [ln[:-1] if ln.endswith("\\r") else ln for ln in (patch or "").split("\\n")]',
     '    return (patch or "").splitlines()', "lint",
     ["test_git_mode_scans_control_separators_in_added_lines", "test_assertion_after_unicode_line_separator_counts"]),
    ("lint-scan-every-commit-addition", LINT, '        for f in c.get("added_files", []):',
     '        for f in []:', "lint", ["test_git_mode_scans_added_lines_from_every_commit"]),
    ("lint-scan-double-plus-source-line", LINT, '        if ln.startswith("+"):\n            out.append(ln[1:])',
     '        if ln.startswith("+") and not ln.startswith("+++"):\n            out.append(ln[1:])', "lint",
     ["test_added_source_line_starting_with_two_plus_signs_is_scanned"]),
    ("lint-unknown-binary-source-refuses", LINT,
     '        if item["binary"] and ext not in BINARY_EXT and not GENERATED_PATH.search(name):',
     '        if False:', "lint", ["test_git_mode_refuses_unknown_binary_source"]),
    ("lint-configured-diff-prefixes", LINT,
     'patch_args = ("--no-renames", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",\n                  "--src-prefix=a/", "--dst-prefix=b/")',
     'patch_args = ("--no-renames", "-U0", "--no-color", "--no-ext-diff", "--no-textconv")', "lint",
     ["test_git_mode_ignores_configured_diff_prefixes_and_scans"]),
    ("lint-commit-numstat-no-textconv", LINT,
     'commit_numstat = git(repo_dir, "log", "--reverse", "--no-renames", "--numstat", "--no-textconv", "-z",',
     'commit_numstat = git(repo_dir, "log", "--reverse", "--no-renames", "--numstat", "-z",', "lint",
     ["test_git_numstat_disables_configured_textconv"]),
    ("lint-diff-numstat-no-textconv", LINT,
     'entries = numstat_entries(git(repo_dir, "diff", "--no-renames", "--numstat", "--no-textconv", "-z", rng))',
     'entries = numstat_entries(git(repo_dir, "diff", "--no-renames", "--numstat", "-z", rng))', "lint",
     ["test_git_numstat_disables_configured_textconv"]),
    ("lint-diff-numstat-nul-filenames", LINT,
     'entries = numstat_entries(git(repo_dir, "diff", "--no-renames", "--numstat", "--no-textconv", "-z", rng))',
     'entries = numstat_entries(git(repo_dir, "diff", "--no-renames", "--numstat", "--no-textconv", rng))', "lint",
     ["test_git_mode_preserves_quoted_paths_with_newlines"]),
    ("lint-name-status-nul-filenames", LINT,
     'state = parse_name_status_z(git(repo_dir, "diff", "--no-renames", "--name-status", "-z", rng))',
     'state = parse_name_status_z(git(repo_dir, "diff", "--no-renames", "--name-status", rng))', "lint",
     ["test_git_mode_preserves_quoted_paths_with_newlines"]),
    ("lint-unscannable-refuses", LINT, "    if unscanned:\n", "    if False:\n", "lint", ["test_unscannable_text_file_refuses"]),
    ("lint-token-pattern", LINT, 'r"\\bghp_[A-Za-z0-9]{30,}", ', "", "lint", ["test_real_shaped_token_in_added_line"]),
    ("lint-case-insensitive-prose", LINT,
     'TEXT_PATTERNS = ([re.compile(p, re.I) for p in _ID_AND_HOST]',
     'TEXT_PATTERNS = ([re.compile(p) for p in _ID_AND_HOST]', "lint",
     ["test_case_insensitive_internal_id_in_pr_text"]),
    ("lint-case-insensitive-code", LINT,
     'CODE_PATTERNS = [re.compile(p, re.I) for p in _ID_AND_HOST]',
     'CODE_PATTERNS = [re.compile(p) for p in _ID_AND_HOST]', "lint",
     ["test_case_insensitive_internal_host_in_added_line"]),
    ("lint-negation-guard", LINT, "if label not in NEGATION_PROOF and NEGATOR.search(", "if False and NEGATOR.search(", "lint",
     ["test_negated_claim_is_not_a_claim"]),
    ("lint-negation-excuses-will-never", LINT, "if label not in NEGATION_PROOF and NEGATOR.search(", "if NEGATOR.search(", "lint",
     ["test_negation_does_not_excuse_will_never"]),
    ("lint-ready-needs-all-boxes", LINT, '            if stage == "ready":\n', '            if stage == "ready" and False:\n', "lint",
     ["test_ready_stage_baseline_needs_every_box_ticked"]),
    ("lint-deleted-checkbox", LINT, "            if lost:\n", "            if False:\n", "lint", ["test_deleted_checkbox"]),
    ("lint-placeholder", LINT, "if not content or PLACEHOLDER.search(content):", "if False:", "lint",
     ["test_todo_text_is_a_placeholder"]),
    ("lint-provider-mismatch", LINT, "if provider_fams and id_fams and not (provider_fams & id_fams):", "if False:", "lint",
     ["test_model_used_provider_mismatch"]),
    ("lint-batch-overlap", LINT, "    if overlap and not asked:\n", "    if False:\n", "lint", ["test_overlap_with_our_open_pr_fails"]),
    ("lint-batch-link-check", LINT, "    if split_url and not asked:\n", "    if False:\n", "lint",
     ["test_split_link_must_be_a_public_github_link"]),
    ("lint-ingest-own-comments", LINT, "        if not who or who.lower() in mine:\n", "        if not who:\n", "lint",
     ["test_ingest_records_maintainer_and_greptile_feedback_once"]),
    ("lint-ingest-not-idempotent", LINT,
     '        if fid in have or (kind == "commenter" and "maintainer-%d-%s" % (pr, cid) in have):\n',
     "        if False:\n", "lint", ["test_ingest_records_maintainer_and_greptile_feedback_once"]),
    ("lint-ingest-legacy-id-duplicates", LINT,
     'if fid in have or (kind == "commenter" and "maintainer-%d-%s" % (pr, cid) in have):',
     "if fid in have:", "lint", ["test_legacy_commenter_entry_marked_maintainer_does_not_reappear"]),
    ("lint-ingest-commenter-is-maintainer", LINT,
     'if kind == "maintainer" and (association or "").upper() not in MAINTAINER_ASSOCIATIONS:',
     "if False:", "lint", ["test_unrelated_commenter_cannot_set_maintainer_policy"]),
    ("lint-history-path-unscanned", LINT,
     '            for h in text_hits(name, CODE_PATTERNS):\n                hits.append("path %s in commit %s holds %r" % (name, sha, h))',
     '            for h in []:\n                hits.append("path %s in commit %s holds %r" % (name, sha, h))', "lint",
     ["test_historical_path_with_internal_id_is_scanned_after_deletion",
      "test_git_mode_scans_a_path_added_then_deleted_before_head"]),
    ("lint-zero-count-patchless-passes", LINT,
     'return (f.get("changes") or 0) > 0 or f.get("status") in ("added", "modified")',
     'return (f.get("changes") or 0) > 0', "lint",
     ["test_patchless_zero_count_added_or_modified_file_refuses", "test_patchless_zero_count_test_file_refuses"]),
    ("lint-zero-count-rename-refuses", LINT,
     'return (f.get("changes") or 0) > 0 or f.get("status") in ("added", "modified")', "return True", "lint",
     ["test_patchless_zero_count_rename_is_fine"]),
    ("lint-backtick-literal-per-line", LINT, 'if char == "`" and backtick_literal:', "if False:", "lint",
     ["test_multiline_fixture_text_is_not_an_assertion"]),
    ("lint-triple-quote-per-line", LINT, "opener = next((t for t in triples if line.startswith(t, i)), None)",
     "opener = None", "lint", ["test_multiline_fixture_text_is_not_an_assertion"]),
    ("lint-literal-never-closes", LINT, "                elif line.startswith(multi, i):", "                elif False:", "lint",
     ["test_assertion_after_a_closed_multiline_fixture_counts"]),
    ("lint-open-literal-hunk-counted", LINT, "        if multi is None or creation:\n            out.extend(hunk)", "        if True:\n            out.extend(hunk)", "lint",
     ["test_hunk_that_starts_inside_an_unseen_literal_yields_no_assertion"]),
    ("lint-hunk-literal-state-carried", LINT, "        multi, in_block, hunk = None, False, []",
     "        in_block, hunk = False, []", "lint", ["test_each_hunk_is_read_from_outside_any_literal"]),
    ("lint-balanced-hunk-dropped", LINT, "        if multi is None or creation:\n            out.extend(hunk)",
     "        if creation:\n            out.extend(hunk)", "lint",
     ["test_hunk_inside_a_literal_opened_before_it_is_read_as_code"]),
    ("lint-import-counts-as-assertion", LINT, '    r"(?:^|[;:)]|\\belse)[ \\t]*assert[ \\t]+(?![=,}])\\S|"',
     '    r"\\bassert\\b|"', "lint", ["test_import_only_test_is_not_an_assertion"]),
    ("lint-global-test-ignores-assertion", LINT,
     "if any(any_glob(name, global_dirs) for name in asserted_tests):",
     'if any(any_glob(t["filename"], global_dirs) for t in tst):', "lint",
     ["test_global_test_glob_needs_an_added_assertion"]),
    ("lint-template-fallback-on-unreadable", LINT,
     'if not (is_missing_api_resource(exc) or "is not a file" in str(exc)):', "if False:", "lint",
     ["test_unreadable_primary_template_does_not_select_a_weaker_fallback"]),
    ("gate-lint-load-pair-exit-lost", GATE,
     '    PAIR_SHA="$(fetch_pr_layout "$upstream" "$num" "$PAIR_UDIR")" || exit $EXIT_REFUSED\n',
     '    PAIR_SHA="$(fetch_pr_layout "$upstream" "$num" "$PAIR_UDIR")"\n', "gate",
     ["lint refuses a PR head change during fetch"]),
    ("gate-publish-fetch-exit-lost", GATE,
     'sha="$(fetch_pr_layout "$repo" "$num" "$dir")" || exit $EXIT_REFUSED',
     'sha="$(fetch_pr_layout "$repo" "$num" "$dir")"', "gate",
     ["publish --dry-run refuses a mirror PR change during fetch"]),
    ("gate-provenance-probe-crash-is-legacy", GATE,
     'if [ "$code" -ne 0 ]; then v2_receipt_error="${v2_receipt:-provenance probe exited $code without a verdict}"',
     'if [ "$code" -eq 2 ]; then v2_receipt_error="${v2_receipt:-provenance probe exited $code without a verdict}"', "gate",
     ["a provenance probe that crashes refuses instead of downgrading to legacy"]),
    ("gate-lint-engine-empty-verdict-passes", GATE,
     '  case "$code:$out" in\n    0:PASS*|1:FAIL*) printf \'%s\\n\' "$out"; return 0;;\n    *)   printf \'REFUSED\\t%s\\n\' "${out#REFUSED\t}${out:+ }$(head -c 200 "$WORK/lint.err")"; return 2;;',
     '  case "$code" in\n    0|1) printf \'%s\\n\' "$out"; return 0;;\n    *)   printf \'REFUSED\\t%s\\n\' "${out#REFUSED\t}${out:+ }$(head -c 200 "$WORK/lint.err")"; return 2;;', "gate",
     ["a linter that crashes before a verdict refuses the audit"]),
    ("gate-reputation-engine-empty-verdict-passes", GATE,
     '  case "$code:$out" in\n    0:PASS*|1:FAIL*) printf \'%s\\n\' "$out"; return 0;;\n    *)   printf \'REFUSED\\t%s\\n\' "${out#REFUSED\t}${out:+ }$(head -c 200 "$WORK/rep.err")"; return 2;;',
     '  case "$code" in\n    0|1) printf \'%s\\n\' "$out"; return 0;;\n    *)   printf \'REFUSED\\t%s\\n\' "${out#REFUSED\t}${out:+ }$(head -c 200 "$WORK/rep.err")"; return 2;;', "gate",
     ["a reputation engine that crashes before a verdict refuses the audit"]),
    ("lint-creation-hunk-dropped", LINT, "        if multi is None or creation:\n            out.extend(hunk)",
     "        if multi is None:\n            out.extend(hunk)", "lint",
     ["test_new_test_file_with_a_backtick_regex_keeps_its_assertions"]),
    ("lint-dropped-hunk-silent", LINT, '            if dropped:\n                ambiguous.append(f["filename"])',
     '            if False:\n                ambiguous.append(f["filename"])', "lint",
     ["test_dropped_hunk_is_named_in_the_failure"]),
    ("lint-masked-string-vanishes", LINT, "                quote = char\n                code.append('\"\"')\n",
     "                quote = char\n", "lint", ["test_assertions_after_a_masked_string_still_count"]),
    ("lint-module-extensions-dropped", LINT, '    "cjs", "cts", "js", "jsx",', '    "js", "jsx",', "lint",
     ["test_module_extensions_track_template_literals"]),
    ("lint-split-diff-halves-unchecked", LINT, 'if rest == "a/%s b/%s" % (name, name):', "if True:", "lint",
     ["test_split_diff_refuses_what_it_cannot_attribute"]),
    ("lint-split-diff-path-unchecked", LINT, "                if name != paths[cur]:\n", "                if False:\n", "lint",
     ["test_split_diff_refuses_what_it_cannot_attribute"]),
    ("lint-split-diff-count-unchecked", LINT, "    if cur + 1 != len(paths):\n", "    if False:\n", "lint",
     ["test_split_diff_refuses_what_it_cannot_attribute"]),
    ("lint-empty-blob-refuses", LINT, '    if f.get("sha") in EMPTY_BLOB_SHAS:', "    if False:", "lint",
     ["test_patchless_empty_added_file_is_fine"]),
    ("lint-commit-subject-splitlines", LINT, 'subject = patch_lines(msg)[0] if msg else ""',
     'subject = msg.splitlines()[0] if msg else ""', "lint", ["test_commit_subject_splits_on_lf_only"]),
    ("hook-remote-label-unscrubbed", HOOK, "block \"remote '$remote_label' is not", "block \"remote '$remote_name' is not", "lint",
     ["test_url_used_as_the_remote_name_does_not_echo_userinfo"]),
    ("gate-audit-collect-crash-passes", GATE, '  collect() { # evaluator dir label [hard: 1=vetoes conversion, 0=mergeable transient]\n    local hard="${4:-1}"\n    line="$($1 "$2")"; code=$?\n    # An evaluator answers through its text (PASS/FAIL) and exits 0; any other\n    # exit is "could not measure", never a blank passing item.\n    if [ $code -ne 0 ]; then\n      line="$(emit_refused "$line")"; refused=1\n      [ "$hard" -eq 1 ] && hard_refused=1\n    fi\n    items+=("$3: $line")',
     '  collect() { # evaluator dir label [hard: 1=vetoes conversion, 0=mergeable transient]\n    local hard="${4:-1}"\n    line="$($1 "$2")"; code=$?\n    # An evaluator answers through its text (PASS/FAIL) and exits 0; any other\n    # exit is "could not measure", never a blank passing item.\n    if [ $code -eq 2 ]; then\n      line="$(emit_refused "$line")"; refused=1\n      [ "$hard" -eq 1 ] && hard_refused=1\n    fi\n    items+=("$3: $line")', "gate",
     ["an audit evaluator that crashes refuses instead of passing a blank item"]),
    ("lint-template-not-a-file-refuses", LINT, 'if not (is_missing_api_resource(exc) or "is not a file" in str(exc)):',
     "if not is_missing_api_resource(exc):", "lint", ["test_primary_template_that_is_not_a_file_falls_back"]),
    ("lint-rust-assert-macro-ignored", LINT, "|\\b(?=\\w*assert)\\w+!\\s*\\(|\\bassert\\s*<", "|\\bassert\\s*<", "lint",
     ["test_assertions_after_a_masked_string_still_count"]),
    ("lint-debug-assert-macro-ignored", LINT, "|\\b(?=\\w*assert)\\w+!\\s*\\(|\\bassert\\s*<",
     "|\\bassert\\w*!\\s*\\(|\\bassert\\s*<", "lint", ["test_rust_prefixed_assert_macros_count"]),
    ("lint-go-raw-string-escapes", LINT, '                if line[i] == "\\\\" and not (ext == "go" and multi == "`"):',
     '                if line[i] == "\\\\":', "lint", ["test_assertions_after_a_masked_string_still_count"]),
    ("lint-creation-literal-unflagged", LINT, "        if multi is not None:\n            dropped += 1",
     "        if multi is not None and not creation:\n            dropped += 1", "lint",
     ["test_creation_hunk_that_leaves_a_literal_open_names_the_file"]),
    ("lint-template-fallback-never", LINT,
     'if not (is_missing_api_resource(exc) or "is not a file" in str(exc)):', "if True:", "lint",
     ["test_verified_absent_primary_template_falls_back"]),
    ("lint-branch-id", LINT, 'if re.search(r"(?i)\\bTOG-|\\bPAPA?-?\\d|^(?:agent|run)[-/]", branch):', "if False:", "lint",
     ["test_internal_id_branch"]),
    ("hook-own-org-passthrough", HOOK, "  togetherweown) exit 0;;", "  togetherweown) :;;", "lint",
     ["test_own_org_passes_through_without_any_text"]),
    ("hook-missing-text-allowed", HOOK, "    failed=1; continue\n  fi\n  echo \"$ME: linting", "    continue\n  fi\n  echo \"$ME: linting", "lint",
     ["test_missing_pr_text_blocks"]),
    ("hook-lint-result-ignored", HOOK, "--stage first-push --live || failed=1", "--stage first-push --live || true", "lint",
     ["test_failing_lint_blocks_the_push", "test_changed_upstream_template_blocks"]),
    ("hook-update-base-increment-only", HOOK,
     '  [ -n "$base" ] || { echo "$ME: BLOCKED: cannot resolve the full PR base of \'$branch\' on $slug (fetch $remote_label first)" >&2; failed=1; continue; }',
     '  base="$remote_sha"\n  [ -n "$base" ] || { echo "$ME: BLOCKED: cannot resolve the full PR base of \'$branch\' on $slug (fetch $remote_label first)" >&2; failed=1; continue; }', "lint",
     ["test_update_lints_the_full_pr_range_not_just_the_increment"]),
    ("hook-unresolved-base-falls-back-to-tip", HOOK,
     '  [ -n "$base" ] || { echo "$ME: BLOCKED: cannot resolve the full PR base of \'$branch\' on $slug (fetch $remote_label first)" >&2; failed=1; continue; }',
     '  if [ -z "$base" ] && [ "$remote_sha" != "$ZERO" ]; then base="$remote_sha"; fi\n  [ -n "$base" ] || { echo "$ME: BLOCKED: cannot resolve the full PR base of \'$branch\' on $slug (fetch $remote_label first)" >&2; failed=1; continue; }', "lint",
     ["test_unresolved_base_does_not_fall_back_to_remote_tip"]),
    ("hook-remote-userinfo-echoed", HOOK,
     "repo; the linter cannot read maintainer memory for it",
     "repo ($remote_url); the linter cannot read maintainer memory for it", "lint",
     ["test_non_github_url_userinfo_is_not_echoed"]),
    ("hook-helper-path-unquoted", LINT, "shlex.quote(script)", "script", "lint",
     ["test_install_hook_quotes_helper_path"]),
    # ---- reputation -----------------------------------------------------------
    ("rep-configured-reviewer-family-guard", REP,
     '    for model in REVIEWERS:\n        fams = ul.detect_families(model)\n        if len(fams) != 1:',
     '    for model in REVIEWERS:\n        fams = ul.detect_families(model)\n        if False:', "rep",
     ["test_configured_reviewer_family_guard_refuses"]),
    ("rep-same-family", REP, "        if fam in authors:\n", "        if False:\n", "rep",
     ["test_same_family_request_is_refused", "test_same_family_reviewer_is_refused_before_any_call"]),
    ("rep-text-binding", REP, "if not m_text or m_text.group(1) != now:", "if not m_text:", "rep",
     ["test_receipt_for_edited_text_fails"]),
    ("rep-receipt-family", REP, "if not m_rev or m_rev.group(1) in authors:", "if not m_rev:", "rep",
     ["test_same_family_receipt_fails"]),
    ("rep-author-binding", REP, 'if m_auth and set(m_auth.group(1).split("+")) != authors:', "if False:", "rep",
     ["test_receipt_recording_a_different_author_fails"]),
    ("rep-clean-with-findings", REP, 'if reply["verdict"] == "clean" and not reply["findings"]:',
     'if reply["verdict"] == "clean":', "rep", ["test_clean_verdict_with_findings_cannot_clear"]),
    ("rep-unparseable-reply", REP, 'if obj.get("verdict") not in ("clean", "findings") or not isinstance(obj.get("findings"), list):',
     "if False:", "rep", ["test_unparseable_replies_are_refusals"]),
    ("rep-newest-receipt", REP, "    last = mine[-1]\n", "    last = mine[0]\n", "rep", ["test_newest_receipt_wins"]),
    ("rep-failure-state", REP, 'if last.get("state") != "success" or not desc.startswith("clean"):', "if False:", "rep",
     ["test_failure_state_fails"]),
    ("rep-status-post-failure-passes", REP,
     "(exc, path), file=sys.stderr)\n            return EXIT_REFUSED", "(exc, path), file=sys.stderr)\n            return EXIT_PASS", "rep",
     ["test_receipt_post_failure_is_a_refusal_not_a_silent_pass"]),
    ("rep-checkbox-state-in-hash", REP,
     'hash_body = re.sub(r"(?m)^([ \\t]*-[ \\t]*)\\[[xX ]\\](.*)$", normalize_gate_box, body or "")',
     'hash_body = body or ""', "rep",
     ["test_ticking_a_gate_owned_box_does_not_change_the_hash", "test_receipt_survives_ticking_a_gate_owned_box"]),
    ("rep-bundle-normalizes-visible-checkboxes", REP,
     '(title or "", body or "", "\\n---\\n".join(msgs))',
     '(title or "", re.sub(r"(?m)^(\\s*-\\s*)\\[[xX ]\\]", r"\\1[ ]", body or ""), "\\n---\\n".join(msgs))',
     "rep", ["test_reviewer_bundle_preserves_actual_checkbox_states"]),
    ("rep-offline-seam-posts", REP, '        if getattr(a, "response_file", None) and getattr(a, "post_receipt", False):',
     '        if False:', "rep",
     ["test_response_file_with_post_receipt_refuses"]),
    # ---- receipts (PR comments on the mirror PR) -------------------------------
    ("receipt-author-trust-lint", LINT, '        if ((c.get("user") or {}).get("login") or "").lower() != author.lower():\n            continue\n',
     '        if False:\n            continue\n', "lint", ["test_only_our_login_is_trusted_and_order_is_by_time"]),
    ("receipt-author-trust-rep", LINT, '        if ((c.get("user") or {}).get("login") or "").lower() != author.lower():\n            continue\n',
     '        if False:\n            continue\n', "rep", ["test_receipt_written_by_someone_else_is_ignored"]),
    ("receipt-author-trust-gate", LINT, '        if ((c.get("user") or {}).get("login") or "").lower() != author.lower():\n            continue\n',
     '        if False:\n            continue\n', "gate", ["a receipt comment written by someone else is not trusted"]),
    ("receipt-head-binding-rep", REP, ' and r["head"] == lay["head"]', '', "rep",
     ["test_receipt_for_another_head_is_ignored"]),
    ("receipt-head-binding-gate", REP, ' and r["head"] == lay["head"]', '', "gate",
     ["a receipt for another head does not count"]),
    ("receipt-marker-escape", LINT, '    payload = payload.replace("-", "\\\\u002d").replace(">", "\\\\u003e")  # nothing can close the comment early\n',
     "    pass\n", "lint", ["test_marker_cannot_be_closed_early_and_round_trips"]),
    ("receipt-post-403-ignored", LINT, '        except (urllib.error.URLError, OSError) as exc:\n            raise Refused("could not record the %s receipt',
     '        except (urllib.error.URLError, OSError) as exc:\n            return\n            raise Refused("could not record the %s receipt', "lint",
     ["test_post_receipt_refuses_when_github_says_403"]),
    # ---- gate -----------------------------------------------------------------
    ("gate-fork-check-run-id-order", GATE,
     r'''# Latest run per check name wins (re-runs, queued runs and shared-SHA PRs).
latest = {}
for r in cr:
    name = r.get("name", "?")
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED\tcheck run has no numeric id for %s" % name); sys.exit(2)
    # GitHub IDs order queued runs even when their timestamps are still null.
    key = (run_id, r.get("started_at") or r.get("completed_at") or "")''',
     r'''# Latest run per check name wins (re-runs, queued runs and shared-SHA PRs).
latest = {}
for r in cr:
    name = r.get("name", "?")
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED\tcheck run has no numeric id for %s" % name); sys.exit(2)
    # GitHub IDs order queued runs even when their timestamps are still null.
    key = (r.get("started_at") or r.get("completed_at") or "", run_id)''',
     "gate", ["new queued fork check supersedes old green without timestamps"]),
    ("gate-upstream-check-run-id-order", GATE,
     r'''sha = pr.get("head", {}).get("sha", "")
latest = {}
for r in cr:
    name = r.get("name", "?")
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED\tcheck run has no numeric id for %s" % name); sys.exit(2)
    # GitHub IDs order queued runs even when their timestamps are still null.
    key = (run_id, r.get("started_at") or r.get("completed_at") or "")''',
     r'''sha = pr.get("head", {}).get("sha", "")
latest = {}
for r in cr:
    name = r.get("name", "?")
    run_id = r.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        print("REFUSED\tcheck run has no numeric id for %s" % name); sys.exit(2)
    # GitHub IDs order queued runs even when their timestamps are still null.
    key = (r.get("started_at") or r.get("completed_at") or "", run_id)''',
     "gate", ["new queued upstream check supersedes old green without timestamps"]),
    ("gate-review-check-run-id-order", GATE,
     r'''    return (run_id, r.get("started_at") or r.get("completed_at") or "")''',
     r'''    return (r.get("started_at") or r.get("completed_at") or "", run_id)''',
     "gate", ["new queued Paperclip Review supersedes older 5/5 without timestamps"]),
    ("gate-ready-ignores-reputation", GATE,
     '  line="$(eval_reputation "$vdir" "$mdir/comments.json")"; code=$?\n'
     '  [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }\n'
     '  [ "$code" -eq 0 ] || refuse "reputation evaluator exited $code"\n'
     '  echo "$line"; case "$line" in FAIL*) fail=1;; esac',
     '  line="$(eval_reputation "$vdir" "$mdir/comments.json")"; code=$?\n'
     '  [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }\n'
     '  [ "$code" -eq 0 ] || refuse "reputation evaluator exited $code"\n'
     '  echo "$line"; case "$line" in FAIL*) :;; esac', "gate",
     ["no reputation receipt blocks ready", "a same-family reviewer receipt blocks ready",
      "editing the PR text after the pass invalidates it", "a reputation pass with findings blocks ready"]),
    ("gate-ready-ignores-lint", GATE,
     '  line="$(eval_lint "$vdir")"; code=$?\n  unset LINT_REPO LINT_STAGE LINT_LIVE\n'
     '  [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }\n'
     '  [ "$code" -eq 0 ] || refuse "pre-push linter evaluator exited $code"\n'
     '  echo "$line"; case "$line" in FAIL*) fail=1;; esac',
     '  line="$(eval_lint "$vdir")"; code=$?\n  unset LINT_REPO LINT_STAGE LINT_LIVE\n'
     '  [ "$code" -eq 2 ] && { emit_refused "$line"; return $EXIT_REFUSED; }\n'
     '  [ "$code" -eq 0 ] || refuse "pre-push linter evaluator exited $code"\n'
     '  echo "$line"; case "$line" in FAIL*) :;; esac', "gate",
     ["a behavior change without a test blocks ready (linter)", "an internal id in the diff blocks ready (linter)",
      "an over-budget diff blocks ready (linter)"]),
    ("gate-ready-allows-pending", GATE, 'GATE_ALLOW_PENDING=0 run_gate_a "$mdir" "$repo" "$fixtures"; local gcode=$?',
     'GATE_ALLOW_PENDING=1 run_gate_a "$mdir" "$repo" "$fixtures"; local gcode=$?', "gate", ["ready refuses while fork CI is still running"]),
    ("gate-serial-flag-ignored", GATE, 'if [ "$serial" -eq 1 ]; then GATE_ALLOW_PENDING=0; else GATE_ALLOW_PENDING=1; fi',
     "GATE_ALLOW_PENDING=1", "gate", ["--serial refuses while a gate is still running"]),
    ("gate-publish-lint-ignored", GATE, '  case "$lline" in FAIL*)\n', '  case "$lline" in NEVER*)\n', "gate",
     ["an internal id in an added diff line blocks publish"]),
    ("gate-publish-batch-ignored", GATE,
     """[ "$bcode" -eq 0 ] || { printf 'BATCH POLICY: FAIL — publish refused\\n'; return $EXIT_FAIL; }""", ":", "gate",
     ["overlap with our open PR refuses publish (fewer, complete PRs)", "a non-link split justification is refused"]),
    ("gate-publish-red-allowed", GATE, '''  [ "$gcode" -eq 0 ] || { printf 'GATE A: %s — publish refused\\n' \\''',
     '''  true || { printf 'GATE A: %s — publish refused\\n' \\''', "gate", ["parallel publish still refuses a failed gate"]),
    ("gate-pending-is-red", GATE, 'FAIL*"[pending]"*) echo pending;;', 'FAIL*"[pending]"*) echo red;;', "gate",
     ["Greptile not yet scored is pending (exit 3), not red", "fork CI still running is pending"]),
    ("gate-stale-greptile-red", GATE, "(stale) [pending]", "(stale)", "gate",
     ["a Greptile score on an older head is pending until re-review"]),
    ("gate-tick-everything", GATE,
     '    if label in VERIFIED:', '    if True:', "gate",
     ["an author-declared box left unticked still blocks ready"]),
    ("gate-audit-v2-reputation", GATE,
     '    if [ "$isdraft" != "true" ]; then\n      if [ -f "$mdir/comments.json" ]; then',
     '    if false; then\n      if [ -f "$mdir/comments.json" ]; then', "gate",
     ["a v2 ready PR without the reputation receipt is flagged for conversion"]),
    ("gate-audit-v2-receipt-preserves-provenance", GATE,
     '  elif [ -f "$mdir/comments.json" ]; then\n    v2_receipt="$(eval_v2_receipt "$mdir/comments.json" "$sha")"; code=$?',
     '  elif false; then\n    v2_receipt="$(eval_v2_receipt "$mdir/comments.json" "$sha")"; code=$?', "gate",
     ["a trusted lint receipt preserves v2 enforcement after the body marker is deleted"]),
    ("gate-v2-receipt-requires-trusted-author", GATE,
     'receipts = ul.receipts_from_comments(comments, ul.whoami())',
     'receipts = ul.receipts_from_comments(comments, "stranger")', "gate",
     ["an untrusted v2-looking comment is not pipeline provenance"]),
    ("gate-v2-receipt-binds-head", GATE,
     'and re.search(r"(?:^|\\s)pipeline=v2(?:\\s|$)", r.get("description") or "")',
     'and r.get("head") == sys.argv[2] and re.search(r"(?:^|\\s)pipeline=v2(?:\\s|$)", r.get("description") or "")',
     "gate", ["a trusted stale-head v2 lint receipt preserves provenance"]),
    ("gate-unreadable-v2-provenance-refuses", GATE,
     '  if [ -n "$v2_receipt_error" ]; then\n    items+=("pipeline-provenance: REFUSED $v2_receipt_error"); refused=1; hard_refused=1\n  fi',
     '  if false; then\n    items+=("pipeline-provenance: REFUSED $v2_receipt_error"); refused=1; hard_refused=1\n  fi', "gate",
     ["unreadable markerless provenance refuses instead of downgrading to legacy"]),
    ("gate-markerless-no-mirror-provenance-refuses", GATE,
     '    v2_receipt_error="no matching mirror PR or comments are available; cannot verify markerless pipeline provenance"',
     '    v2_receipt_error=""', "gate",
     ["markerless provenance with no mirror refuses instead of downgrading to legacy"]),
    ("gate-fetch-pr-snapshot-consistency", GATE,
     '    || refuse "PR #$num on $repo changed during fetch; refusing a mixed snapshot"',
     '    || true', "gate", ["audit refuses a PR head change during fetch without mutation"]),
    ("gate-publish-requires-v2-receipt", GATE,
     '|| refuse "could not record the v2 lint receipt on the mirror PR; publish refused"',
     '|| true', "gate", ["a failed v2 provenance receipt refuses publish before gh pr create"]),
    ("gate-audit-legacy-converted", GATE, '  if [ "$v2" -eq 1 ]; then\n    LINT_REPO', '  if true; then\n    LINT_REPO', "gate",
     ["a legacy ready PR without a receipt is NOT converted to draft"]),
    ("gate-lint-result-swallowed", GATE, "    0:PASS*|1:FAIL*) printf '%s\\n' \"$out\"; return 0;;\n    *)   printf 'REFUSED\\t%s\\n' \"${out#REFUSED\t}${out:+ }$(head -c 200 \"$WORK/lint.err\")\"; return 2;;",
     "    0:PASS*|1:FAIL*) printf 'PASS\\tlint ok\\n'; return 0;;\n    *)   printf 'REFUSED\\t%s\\n' \"${out#REFUSED\t}${out:+ }$(head -c 200 \"$WORK/lint.err\")\"; return 2;;",
     "gate", ["a behavior change without a test blocks ready (linter)", "an internal id in the diff blocks ready (linter)"]),
    ("gate-ready-body-newline", GATE, '    jq -j \'.body // ""\' "$vdir/pr.json" > "$WORK/ready_body.md"',
     '    jq -r \'.body\' "$vdir/pr.json" > "$WORK/ready_body.md"', "gate",
     ["ready writes the ticked body byte-for-byte"]),
    ("gate-body-marker", GATE, 'print("- pipeline: upstream-gate v2")', 'print("- pipeline: upstream-gate v1")', "gate",
     ["generated body leaves the CI box open and claims nothing unmeasured"]),
    ("gate-ci-box-ticked-at-publish", GATE, 'print("- [ ] All Paperclip CI gates are green (pending;',
     'print("- [x] All Paperclip CI gates are green (pending;', "gate",
     ["generated body leaves the CI box open and claims nothing unmeasured"]),
]

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def stage():
    d = tempfile.mkdtemp(prefix="case_89afd59b-mut-")
    for rel in COPY:
        dst = os.path.join(d, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), dst)
    return d


def run_suite(d, suite):
    env = dict(os.environ)
    if os.path.exists("/usr/bin/git"):
        env.setdefault("GIT_BIN", "/usr/bin/git")
    proc = subprocess.run(SUITES[suite], cwd=d, env=env, capture_output=True, text=True, errors="replace")
    return proc.returncode, ANSI.sub("", proc.stdout + proc.stderr)


def failed_cases(suite, out):
    if suite == "gate":
        return [ln.split("FAIL", 1)[1].strip() for ln in out.splitlines() if re.match(r"\s+FAIL\s", ln)]
    return re.findall(r"^(?:FAIL|ERROR): (\w+) ", out, re.M)


def baseline(suite):
    d = stage()
    try:
        rc, out = run_suite(d, suite)
        ran = re.search(r"Ran (\d+) tests", out) or re.search(r"(\d+) passed", out)
        ok = rc == 0 and ran is not None and int(ran.group(1)) > 0
        return suite, ok, (ran.group(1) if ran else "0"), out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def mutate(m):
    mid, rel, old, new, suite, cases = m
    d = stage()
    try:
        path = os.path.join(d, rel)
        src = open(path, encoding="utf-8").read()
        if src.count(old) != 1:
            return mid, False, "mutation target appears %d times, expected exactly 1" % src.count(old)
        open(path, "w", encoding="utf-8").write(src.replace(old, new))
        rc, out = run_suite(d, suite)
        failed = failed_cases(suite, out)
        if not cases:
            return mid, rc == 0, "no case required (control mutation); suite rc=%d" % rc
        missing = [c for c in cases if not any(c == f or c in f for f in failed)]
        if missing:
            return mid, False, "NOT DETECTED by: %s (suite rc=%d, %d case(s) failed: %s)" % (
                "; ".join(missing), rc, len(failed), ", ".join(failed[:4]))
        return mid, True, "detected by %d named case(s)" % len(cases)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    only, jobs = None, max(1, min(4, os.cpu_count() or 2))
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--only":
            only = args.pop(0)
        elif a == "-j":
            jobs = int(args.pop(0))
        else:
            print("usage: case_214e61d3-mutation-gate.py [--only ID_SUBSTRING] [-j N]", file=sys.stderr)
            return 4
    print("=== baseline: the UNMUTATED copy passes in this same staging layout ===")
    bad = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for suite, ok, n, out in pool.map(baseline, SUITES):
            if ok:
                print("  PASS  baseline %s: green, %s assertions" % (suite, n))
            else:
                print("  FAIL  baseline %s is ALREADY RED or ran nothing; every mutation below would be unattributable" % suite)
                print("\n".join(l for l in out.splitlines() if "FAIL" in l or "Error" in l)[:2000])
                bad += 1
    if bad:
        return 1
    todo = [m for m in MUTANTS if not only or only in m[0]]
    print("\n=== %d mutations, %d at a time ===" % (len(todo), jobs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for mid, ok, why in pool.map(mutate, todo):
            print("  %s  %-34s %s" % ("PASS" if ok else "FAIL", mid, why))
            bad += 0 if ok else 1
    print("\nsummary: %d mutation(s), %d undetected" % (len(todo), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
