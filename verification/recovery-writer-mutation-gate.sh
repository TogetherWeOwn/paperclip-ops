#!/usr/bin/env bash
# Prove the recovery-writer deployment assertions are load-bearing.
#
# Every test in test_recovery_writer_deployment.py must have at least one
# mutant here that it catches. The gate polices its own coverage, so an
# assertion added without a mutant is a red, not a silent gap.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

SUITE=test_recovery_writer_deployment.py

mkdir -p "$STAGE/systemd" "$STAGE/docs"
reset_stage() {
  cp -p "$ROOT/$SUITE" "$STAGE/"
  cp -p "$ROOT/systemd/paperclip-recovery-writer.service" "$STAGE/systemd/"
  cp -p "$ROOT/systemd/paperclip-recovery-writer.timer" "$STAGE/systemd/"
  cp -p "$ROOT/systemd/run-recovery-writer.sh" "$STAGE/systemd/"
  cp -p "$ROOT/systemd/install-recovery-writer.sh" "$STAGE/systemd/"
  cp -p "$ROOT/systemd/build-recovery-writer-bundle.sh" "$STAGE/systemd/"
  cp -p "$ROOT/docs/recovery-writer.md" "$STAGE/docs/"
  cp -p "$ROOT/docs/recovery-writer-migration.md" "$STAGE/docs/"
}
reset_stage

run_suite() {
  (cd "$STAGE" && python3 -m unittest -v "$SUITE")
}

ran_count() {
  printf '%s\n' "$1" | grep -oE 'Ran [0-9]+ tests' | grep -oE '[0-9]+' | tail -1 || true
}

baseline="$(run_suite 2>&1)" || {
  printf 'FAIL: unmutated deployment suite is already red\n%s\n' "$baseline" >&2
  exit 1
}
baseline_count="$(ran_count "$baseline")"
[[ -n "$baseline_count" && "$baseline_count" -gt 0 ]] || { echo 'FAIL: baseline ran zero tests' >&2; exit 1; }

covered=()

assert_caught() {
  local name="$1" expected="$2" rc="$3" output="$4"
  local count
  count="$(ran_count "$output")"
  if [[ "$rc" -eq 0 || "$count" != "$baseline_count" || "$output" != *"FAIL: $expected"* ]]; then
    printf 'FAIL: %s was not caught by %s\n%s\n' "$name" "$expected" "$output" >&2
    exit 1
  fi
  covered+=("$expected")
  printf 'PASS: %s\n' "$name"
}

mutate() {
  local name="$1" file="$2" old="$3" new="$4" expected="$5"
  reset_stage
  OLD="$old" NEW="$new" FILE="$STAGE/$file" python3 - <<'PY'
import os, pathlib, sys
path = pathlib.Path(os.environ["FILE"])
source = path.read_text()
old = os.environ["OLD"]
count = source.count(old)
if count != 1:
    print(f"mutation target appears {count} times, expected exactly 1", file=sys.stderr)
    raise SystemExit(1)
path.write_text(source.replace(old, os.environ["NEW"]))
PY
  bash -n "$STAGE/systemd/install-recovery-writer.sh"
  bash -n "$STAGE/systemd/build-recovery-writer-bundle.sh"
  bash -n "$STAGE/systemd/run-recovery-writer.sh"
  local output rc=0
  output="$(run_suite 2>&1)" || rc=$?
  assert_caught "$name" "$expected" "$rc" "$output"
}

mutate_mode() {
  local name="$1" file="$2" mode="$3" expected="$4"
  reset_stage
  chmod "$mode" "$STAGE/$file"
  local output rc=0
  output="$(run_suite 2>&1)" || rc=$?
  assert_caught "$name" "$expected" "$rc" "$output"
}

# --- unit: the writer runs through the reviewed driver, not inline ---
mutate driver-bypassed systemd/paperclip-recovery-writer.service \
  'ExecStart=/usr/bin/timeout --signal=TERM --kill-after=15s 4m /usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/run-recovery-writer.sh' \
  'ExecStart=/usr/bin/timeout --signal=TERM --kill-after=15s 4m /usr/bin/python3 /usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/recovery_writer.py --proposals x --apply' \
  test_unit_runs_writer_through_the_reviewed_driver
# --- driver: --apply stops being a literal (the sweep-auto drift returns) ---
mutate driver-apply-from-env systemd/run-recovery-writer.sh \
  'ARGS=(
  --apply' \
  'ARGS=(
  ${RECOVERY_WRITER_APPLY:+--apply}' \
  test_driver_carries_apply_literally_no_env_mode
# Behavioral, not textual: the mutant keeps the message and drops the
# refusal exit, so the writer runs on a cycle that measured nothing. The
# executed test catches it by the marker the writer leaves behind.
mutate driver-empty-detector-accepted systemd/run-recovery-writer.sh \
  "printf 'REFUSED: detector emitted zero proposals -- measured nothing, writer not run\n' >&2
  exit 1" \
  "printf 'no proposals this cycle -- continuing clean\n' >&2
  exit 0" \
  test_driver_refuses_empty_detector_output
mutate checkout-path systemd/paperclip-recovery-writer.service \
  'WorkingDirectory=/usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@' \
  'WorkingDirectory=${PAPERCLIP_TOOLING_DIR}' \
  test_unit_executes_versioned_installed_release_not_checkout
mutate checksum systemd/paperclip-recovery-writer.service \
  'ExecStartPre=/usr/bin/sha256sum --check --strict /usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/SHA256SUMS' \
  'ExecStartPre=/usr/bin/true' \
  test_unit_verifies_release_hashes_before_credential_preflight
mutate timeout systemd/paperclip-recovery-writer.service \
  'TimeoutStartSec=5m' 'TimeoutStartSec=infinity' \
  test_timeout_start_sec_covers_preflight_and_oneshot
mutate timer-target systemd/paperclip-recovery-writer.timer \
  'Unit=paperclip-recovery-writer.service' 'Unit=paperclip-recovery-wrong.service' \
  test_timer_targets_exact_writer_service
mutate timer-jitter systemd/paperclip-recovery-writer.timer \
  'RandomizedDelaySec=90s' 'RandomizedDelaySec=0s' \
  test_timer_retains_interval_jitter_and_persistence
mutate credential-preflight systemd/paperclip-recovery-writer.service \
  'ExecStartPre=/usr/bin/python3 /usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@/recovery_writer.py --check-service-credential --preflight-issue-id ${PAPERCLIP_PREFLIGHT_ISSUE_ID} --preflight-agent-id ${PAPERCLIP_PREFLIGHT_AGENT_ID}' \
  'ExecStartPre=/usr/bin/true # --check-service-credential' \
  test_unit_runs_exact_read_only_credential_preflight
mutate runtime-path-precheck systemd/paperclip-recovery-writer.service \
  'ExecStartPre=/usr/bin/test -x /usr/bin/timeout' \
  'ExecStartPre=/usr/bin/test -e /usr/bin/timeout' \
  test_unit_prechecks_timeout_path
mutate writable-home systemd/paperclip-recovery-writer.service \
  'UMask=0077' \
  'ReadWritePaths=%h/.local/state/paperclip
UMask=0077' \
  test_no_writable_home_path_contradicts_protect_home
mutate repair-cap-default-dropped systemd/paperclip-recovery-writer.service \
  'Environment=PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS=0
' '' \
  test_unset_repair_cap_defaults_to_zero_not_an_empty_argument
mutate repair-cap-default-after-envfile systemd/paperclip-recovery-writer.service \
  'Environment=PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS=0
EnvironmentFile=%h/.config/paperclip/recovery-writer.env' \
  'EnvironmentFile=%h/.config/paperclip/recovery-writer.env
Environment=PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS=0' \
  test_unset_repair_cap_defaults_to_zero_not_an_empty_argument
mutate immutable-bundle systemd/build-recovery-writer-bundle.sh \
  'git --no-replace-objects archive --format=tar "$SOURCE_REF"' 'git archive --format=tar HEAD' \
  test_unprivileged_builder_archives_installer_and_payload_from_reviewed_commit
mutate builder-accepts-any-reachable-commit systemd/build-recovery-writer-bundle.sh \
  'git --no-replace-objects merge-base --is-ancestor "$SOURCE_REF" "$TRUSTED_LINE"' 'true' \
  test_builder_refuses_a_commit_not_merged_into_the_trusted_line
mutate builder-trusts-head-not-the-merged-line systemd/build-recovery-writer-bundle.sh \
  'TRUSTED_LINE="origin/main"' 'TRUSTED_LINE="HEAD"' \
  test_builder_refuses_a_commit_not_merged_into_the_trusted_line
mutate builder-ancestry-check-present-but-toothless systemd/build-recovery-writer-bundle.sh \
  '  || refuse "$SOURCE_REF is not an ancestor of $TRUSTED_LINE; it was never merged by review. Build a commit that is on that line, or pass --trusted-line if the reviewed line is elsewhere."' '  || true' \
  test_builder_actually_refuses_to_write_a_bundle_for_an_unmerged_commit
mutate root-checkout systemd/install-recovery-writer.sh \
  '/usr/bin/tar --extract --file "$BUNDLE" --directory "$WORK_DIR" --no-same-owner --no-same-permissions' \
  'git archive HEAD | /usr/bin/tar -x --directory "$WORK_DIR"' \
  test_installer_never_reads_git_or_mutable_checkout_as_root
mutate installer-literal-commit systemd/install-recovery-writer.sh \
  '[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]]' '[[ -n "$SOURCE_REF" ]]' \
  test_installer_requires_literal_commit_and_reviewed_bundle
mutate installer-root-user-manager systemd/install-recovery-writer.sh \
  "printf 'INSTALLED paperclip-recovery-writer from %s for user %s (uid %s)\\n'" \
  "systemctl --user daemon-reload
printf 'INSTALLED paperclip-recovery-writer from %s for user %s (uid %s)\\n'" \
  test_installer_never_targets_root_user_manager
mutate installer-existence-not-executable systemd/install-recovery-writer.sh \
  '[[ -x "$executable" ]]' '[[ -e "$executable" ]]' \
  test_installer_checks_every_hardcoded_runtime_executable
mutate installer-requires-uninvoked-binary systemd/install-recovery-writer.sh \
  'for executable in /usr/bin/install ' 'for executable in /usr/sbin/runuser /usr/bin/install ' \
  test_installer_only_requires_executables_it_actually_runs
mutate installer-bundle-check-deleted systemd/install-recovery-writer.sh \
  '(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)' \
  ': bundle-scope check removed' \
  test_installer_verifies_bundle_manifest_before_installing
# Reintroduces the exact pre-fix defect line the test forbids.
mutate installer-copies-bundle-manifest systemd/install-recovery-writer.sh \
  '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/REVISION"' \
  '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$WORK_DIR/SHA256SUMS" "$RELEASE_DIR/"' \
  test_installer_does_not_copy_bundle_manifest_into_release
# Re-bakes the cap into the reviewed driver: "raise the cap" becomes
# "re-review the bundle", so a needed repair goes unmade.
mutate driver-repair-cap-reliteralised systemd/run-recovery-writer.sh \
  '  --max-repairs "$MAX_REPAIRS"' \
  '  --max-repairs 3' \
  test_repair_cap_is_operator_settable_without_editing_a_root_unit
mutate installer-release-manifest-covers-pressure-module systemd/install-recovery-writer.sh \
  '/usr/bin/sha256sum lib/paperclip_api_allowlist.py recovery_writer.py ops/lib/host_pressure.py run-recovery-writer.sh REVISION' \
  '/usr/bin/sha256sum lib/paperclip_api_allowlist.py recovery_writer.py run-recovery-writer.sh REVISION' \
  test_installer_regenerates_release_scoped_manifest
mutate installer-import-smoke-test-removed systemd/install-recovery-writer.sh \
  '(cd "$RELEASE_DIR" && /usr/bin/python3 recovery_writer.py --help >/dev/null)' \
  ': release import smoke test removed' \
  test_installer_compiles_and_imports_pressure_module_before_claiming_installed
mutate installer-release-check-deleted systemd/install-recovery-writer.sh \
  '(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)' \
  ': release preflight proof removed' \
  test_installer_proves_unit_preflight_before_claiming_installed
mutate installer-revision-renamed systemd/install-recovery-writer.sh \
  '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/REVISION"' \
  '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/REVISION.bak"' \
  test_release_manifest_covers_exactly_the_installed_byte_set
mutate unit-preflight-unpinned-dir systemd/paperclip-recovery-writer.service \
  'WorkingDirectory=/usr/local/libexec/paperclip-recovery-writer/@SOURCE_REF@' \
  'WorkingDirectory=/usr/local/libexec/paperclip-recovery-writer' \
  test_unit_preflight_checks_installed_release_manifest
mutate builder-manifest-drops-timer systemd/build-recovery-writer-bundle.sh \
  '  systemd/paperclip-recovery-writer.timer' '' \
  test_bundle_manifest_still_covers_payload_units_installer_and_driver
mutate builder-manifest-narrowed systemd/build-recovery-writer-bundle.sh \
  'sha256sum "${files[@]}" REVISION > SHA256SUMS' \
  'sha256sum recovery_writer.py REVISION > SHA256SUMS' \
  test_old_layout_fails_preflight_and_regenerated_manifest_passes

mutate rollback docs/recovery-writer-migration.md \
  'systemctl --user stop paperclip-recovery-writer.service' ':' \
  test_rollback_stops_timer_and_active_service_before_removal
mutate install-order docs/recovery-writer.md \
  'systemctl --user start paperclip-recovery-writer.service' \
  'systemctl --user enable --now paperclip-recovery-writer.timer # first, skip the hand run' \
  test_install_section_runs_observe_only_before_timer

mutate_mode build-script-not-executable systemd/build-recovery-writer-bundle.sh 0644 \
  test_build_script_is_executable_as_the_docs_invoke_it

# Completeness. Every test in the suite must be named by at least one mutant
# above. Without this, an assertion added with no mutant is indistinguishable
# from an assertion that cannot fail.
reset_stage
UNCOVERED="$(COVERED="$(printf '%s\n' "${covered[@]}")" SUITE_PATH="$STAGE/$SUITE" python3 - <<'PY'
import os, pathlib, re
covered = set(os.environ["COVERED"].split())
names = re.findall(r"(?m)^    def (test_\w+)\(", pathlib.Path(os.environ["SUITE_PATH"]).read_text())
assert names, "found no test methods to check coverage against"
print("\n".join(sorted(set(names) - covered)))
PY
)"
if [[ -n "$UNCOVERED" ]]; then
  printf 'FAIL: deployment assertions with no mutant proving they are load-bearing:\n%s\n' "$UNCOVERED" >&2
  exit 1
fi

printf 'PASS: %s deployment mutations detected, covering all %s assertions\n' \
  "${#covered[@]}" "$baseline_count"