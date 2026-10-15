#!/usr/bin/env bash
# Prove the case_b4863264 host-db-backup deployment assertions are load-bearing.
#
# Every test in test_host_db_backup_deployment.py must have at least one
# mutant here that it catches. That completeness check is not decoration: the
# sibling case_d2b472ea gate rotted once when a code change left a mutant pointing
# at bytes that no longer existed while new assertions arrived with no mutant
# at all. `mutate` fails closed on a target it cannot find, so a stale mutant
# is caught -- but an uncovered assertion is invisible without the coverage
# check at the bottom. Do not add an assertion without adding its mutant.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

SUITE=test_host_db_backup_deployment.py

mkdir -p "$STAGE/systemd"
reset_stage() {
  cp -p "$ROOT/$SUITE" "$STAGE/"
  cp -p "$ROOT/systemd/paperclip-host-db-backup.service" "$STAGE/systemd/"
  cp -p "$ROOT/systemd/paperclip-host-db-backup.timer" "$STAGE/systemd/"
  cp -p "$ROOT/systemd/install-host-db-backup.sh" "$STAGE/systemd/"
  cp -p "$ROOT/systemd/build-host-db-backup-bundle.sh" "$STAGE/systemd/"
}
reset_stage

run_suite() {
  (cd "$STAGE" && python3 -m unittest "$SUITE")
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

# Shared verdict for every mutation style: the suite must go red, must still
# run the same number of tests (a mutant that makes the suite collapse or skip
# proves nothing), and must fail in the NAMED test rather than some other one.
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
  bash -n "$STAGE/systemd/install-host-db-backup.sh"
  bash -n "$STAGE/systemd/build-host-db-backup-bundle.sh"
  local output rc=0
  output="$(run_suite 2>&1)" || rc=$?
  assert_caught "$name" "$expected" "$rc" "$output"
}

# A mode-only mutation. `mutate` rewrites bytes and cannot express this one.
mutate_mode() {
  local name="$1" file="$2" mode="$3" expected="$4"
  reset_stage
  chmod "$mode" "$STAGE/$file"
  local output rc=0
  output="$(run_suite 2>&1)" || rc=$?
  assert_caught "$name" "$expected" "$rc" "$output"
}

# case_b4863264 itself: the release manifest must not be the bundle manifest
# copied verbatim -- the bundle lists systemd/* paths that are never
# installed, which is the FAILED-open-or-read the unit died on.
mutate copy-bundle-manifest-verbatim systemd/install-host-db-backup.sh \
  '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/REVISION"' \
  '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$WORK_DIR/SHA256SUMS" "$RELEASE_DIR/"' \
  test_installer_does_not_copy_bundle_manifest_into_release
mutate manifest-regeneration-dropped systemd/install-host-db-backup.sh \
  '(cd "$RELEASE_DIR" && /usr/bin/sha256sum host_db_backup.sh REVISION > "$WORK_DIR/SHA256SUMS.release")' \
  'true' \
  test_installer_regenerates_release_scoped_manifest
mutate release-check-dropped systemd/install-host-db-backup.sh \
  '(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)' \
  'true' \
  test_installer_proves_unit_preflight_before_claiming_installed
# The bundle check must come BEFORE the install, not merely exist: after the
# install is too late to refuse on. This mutant moves the check rather than
# deleting it, so only the ordering assertion catches it.
mutate bundle-check-after-install systemd/install-host-db-backup.sh \
  '(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)
RELEASE_DIR="/usr/local/libexec/paperclip-host-db-backup/$SOURCE_REF"' \
  'RELEASE_DIR="/usr/local/libexec/paperclip-host-db-backup/$SOURCE_REF"
(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)' \
  test_installer_verifies_bundle_manifest_before_installing
# A file installed into the release dir but missing from the regenerated
# manifest breaks the exact-byte-set invariant from the other side.
mutate installs-uncovered-extra-file systemd/install-host-db-backup.sh \
  'bash -n "$RELEASE_DIR/host_db_backup.sh"' \
  '/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/EXTRA"
bash -n "$RELEASE_DIR/host_db_backup.sh"' \
  test_release_manifest_covers_exactly_the_installed_byte_set
# A path covered by the regenerated manifest but absent from the release dir
# is the original case_b4863264 shape; the executed test must catch it by RUNNING
# the generation command, not by reading it.
mutate manifest-covers-absent-path systemd/install-host-db-backup.sh \
  '(cd "$RELEASE_DIR" && /usr/bin/sha256sum host_db_backup.sh REVISION > "$WORK_DIR/SHA256SUMS.release")' \
  '(cd "$RELEASE_DIR" && /usr/bin/sha256sum host_db_backup.sh REVISION systemd/ghost > "$WORK_DIR/SHA256SUMS.release")' \
  test_old_layout_fails_preflight_and_regenerated_manifest_passes
mutate unit-preflight-dropped systemd/paperclip-host-db-backup.service \
  'ExecStartPre=/usr/bin/sha256sum --check --strict /usr/local/libexec/paperclip-host-db-backup/@SOURCE_REF@/SHA256SUMS' \
  'ExecStartPre=/usr/bin/true' \
  test_unit_preflight_checks_installed_release_manifest
# The fix must narrow the RELEASE manifest, never the BUNDLE one: dropping a
# covered member "fixes" the preflight by ceasing to verify a reviewed file.
mutate bundle-stops-covering-unit systemd/build-host-db-backup-bundle.sh \
  '  systemd/paperclip-host-db-backup.service
' '' \
  test_bundle_manifest_still_covers_payload_units_and_installer
mutate installer-requires-uninvoked-binary systemd/install-host-db-backup.sh \
  'for executable in /usr/bin/install ' 'for executable in /usr/sbin/runuser /usr/bin/install ' \
  test_installer_only_requires_executables_it_actually_runs

mutate_mode build-script-not-executable systemd/build-host-db-backup-bundle.sh 0644 \
  test_build_script_is_executable_as_the_docs_invoke_it

# Completeness. Every test in the suite must be named by at least one mutant
# above. Without this, an assertion added with no mutant is indistinguishable
# from an assertion that cannot fail -- which is how the sibling gate rotted.
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
