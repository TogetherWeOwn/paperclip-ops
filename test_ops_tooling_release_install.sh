#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSTALLER="$ROOT/scripts/install-ops-tooling-release.sh"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ops-tooling-release-test.XXXXXXXX")
trap 'rm -rf "$WORK"' EXIT
failures=0

ok() { printf '  ok: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }

make_repo() {
  local repo="$1" fixture_mode="${2:-stub}"
  mkdir -p "$repo/lib"
  git -C "$repo" init -q
  git -C "$repo" config user.name test
  git -C "$repo" config user.email test@example.invalid
  cp "$ROOT/ops-tooling-runtime-manifest.txt" "$repo/"
  while read -r mode member; do
    [[ -n "${mode:-}" && "$mode" != \#* ]] || continue
    mkdir -p "$repo/$(dirname "$member")"
    if [[ "$fixture_mode" == real ]]; then
      cp "$ROOT/$member" "$repo/$member"
    else
      case "$member" in
        pacing_verdict.py)
          cat > "$repo/$member" <<'PY'
#!/usr/bin/env python3
import argparse
argparse.ArgumentParser().parse_args()
PY
          ;;
        lib/durable_queue.py)
          cat > "$repo/$member" <<'PY'
#!/usr/bin/env python3
import pathlib
import sys
if len(sys.argv) != 3 or sys.argv[1] != "append":
    raise SystemExit(2)
pathlib.Path(sys.argv[2]).write_bytes(sys.stdin.buffer.read())
PY
          ;;
        *.py) printf '#!/usr/bin/env python3\n' > "$repo/$member";;
        *.sh)
          cat > "$repo/$member" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == template-keys ]]; then printf 'T1\ttools:use\n'; fi
exit 0
SH
          ;;
        *) printf 'fixture\n' > "$repo/$member";;
      esac
    fi
    [[ "$mode" == 0555 ]] && chmod +x "$repo/$member" || chmod -x "$repo/$member"
  done < "$repo/ops-tooling-runtime-manifest.txt"
  git -C "$repo" add .
  git -C "$repo" commit -qm fixture
}

commit_repo() {
  local repo="$1" message="$2"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "$message"
  git -C "$repo" rev-parse HEAD
}

run_install() {
  local repo="$1" base="$2" ref="$3"
  (
    cd "$repo"
    OPS_TOOLING_INSTALL_TEST_MODE=1 \
    OPS_TOOLING_INSTALL_BASE="$base" \
    "$INSTALLER" --source-ref "$ref"
  )
}

expect_refusal() {
  local label="$1" expected="$2"
  shift 2
  local output rc
  set +e
  output=$("$@" 2>&1)
  rc=$?
  set -e
  if [[ $rc -eq 2 && "$output" == *"$expected"* ]]; then
    ok "$label"
  else
    fail "$label (rc=$rc output=$output)"
  fi
}

repo="$WORK/repo"
base="$WORK/opt/paperclip-ops"
make_repo "$repo"
reviewed=$(git -C "$repo" rev-parse HEAD)
mkdir -p "$base"
printf 'legacy bytes\n' > "$base/tool_drift.sh"
legacy_hash=$(sha256sum "$base/tool_drift.sh")

real_repo="$WORK/real-runtime"
real_base="$WORK/real/opt/paperclip-ops"
make_repo "$real_repo" real
real_reviewed=$(git -C "$real_repo" rev-parse HEAD)
if run_install "$real_repo" "$real_base" "$real_reviewed" >/dev/null; then
  ok "current real runtime dependency closure passes installer preflight"
else
  fail "current real runtime dependency closure passes installer preflight"
fi

# case_1926b5a1. The installed release must ship a runnable closeout ledger. The
# live-input producer is the Director's hourly closeout pass (GitHub state in,
# registry.json + snapshot.json out); the plan consumer is the authorized
# Operator (plan.json proposals in, at most one diagnostic action per admitted
# card per pass). These fixtures pin the INSTALLED copy to the refused-clean
# contract and the deterministic producer contract -- never the checkout copy.
installed_ledger="$real_base/current/pr_closeout_ledger.py"
if [[ -f "$installed_ledger" && ! -L "$installed_ledger" ]]; then
  ok "installed release ships pr_closeout_ledger.py as a regular file"
else
  fail "installed release ships pr_closeout_ledger.py as a regular file"
fi

closeout_case="$WORK/closeout-installed"
mkdir -p "$closeout_case"
HEAD_A=$(printf 'a%.0s' {1..40})
MERGE_C=$(printf 'c%.0s' {1..40})
python3 - "$closeout_case" "$HEAD_A" "$MERGE_C" <<'PY'
import json
import pathlib
import sys
case, head, merge = (pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3])
(case / "registry.json").write_text(json.dumps({"entries": [{
    "repo": "two-web-next", "pr": 291, "admission": "admitted", "kind": "standard",
    "authorCard": "case_3a0e31bb", "successorCard": "case_3a0e31bb",
    "reviewerCard": "case_3485df85"}]}))
checks = [{"name": n, "sha": head, "conclusion": "success"}
          for n in ("check", "gitleaks", "pr-lint")]
(case / "snapshot-merged.json").write_text(json.dumps({
    "snapshots": {"two-web-next#291": {
        "headSha": head, "mergeableState": "clean", "merged": True,
        "mergeCommitSha": merge, "mergeUrl": "https://example.invalid/merge",
        "draft": False, "releasePlease": False, "checks": checks,
        "requiredChecks": ["check", "gitleaks", "pr-lint"],
        "companyIndependentReviewRequired": True,
        "companySecurityReviewRequired": False}},
    "reviews": {}, "cards": {}}))
(case / "snapshot-empty.json").write_text(json.dumps(
    {"snapshots": {}, "reviews": {}, "cards": {}}))
(case / "snapshot-owned.json").write_text(json.dumps({
    "snapshots": {}, "reviews": {},
    "cards": {"case_cd6574d9": {"id": "x", "identifier": "case_cd6574d9",
                            "status": "todo"}}}))
PY

run_installed() {
  set +e
  python3 "$installed_ledger" --registry "$1" --snapshot "$2" \
    --ledger-out "$closeout_case/ledger.json" \
    --plan-out "$closeout_case/plan.json" \
    >"$closeout_case/stdout.txt" 2>"$closeout_case/stderr.txt"
  INST_RC=$?
  set -e
}

run_installed "$closeout_case/registry.json" "$closeout_case/snapshot-merged.json"
[[ $INST_RC -eq 0 ]] && ok "installed ledger scores verified MERGED with exit 0" || fail "installed ledger MERGED case (rc=$INST_RC)"
python3 - "$closeout_case/ledger.json" "$closeout_case/plan.json" <<'PY' \
  && ok "installed MERGED record carries proof and an empty plan" \
  || fail "installed MERGED record shape"
import json
import sys
ledger = json.load(open(sys.argv[1]))
plan = json.load(open(sys.argv[2]))
record = ledger["records"]["two-web-next#291"]
assert record["disposition"] == "MERGED", record
assert record["mergeCommitSha"] == "c" * 40, record
assert record["mergeUrl"] == "https://example.invalid/merge", record
assert {a["name"] for a in record["requiredCheckAttempts"]} == {
    "check", "gitleaks", "pr-lint"}, record
assert all(a["conclusion"] == "success" for a in record["requiredCheckAttempts"]), record
assert record["nextCheckKind"] == "recheck", record
assert record["admissionProvenance"] == "first_sight", record
assert {a["name"] for a in record["requiredCheckAttempts"]} == {
    "check", "gitleaks", "pr-lint"}, record
assert record["deadlineDirectorAt"] is not None, record
assert record["deadlineCeoAt"] is not None, record
assert plan["proposals"] == [], plan
assert set(plan) == {"generatedAt", "proposals"}, plan
PY

run_installed "$closeout_case/registry.json" "$closeout_case/snapshot-empty.json"
if [[ $INST_RC -eq 5 && "$(cat "$closeout_case/stderr.txt")" == *"refusing to report a clean board"* ]]; then
  ok "installed ledger refuses empty inputs with exit 5"
else
  fail "installed ledger empty-input refusal (rc=$INST_RC)"
fi

run_installed "$closeout_case/registry.json" "$closeout_case/snapshot-owned.json"
[[ $INST_RC -eq 1 ]] && ok "installed ledger emits an owned proposal with exit 1" || fail "installed ledger owned case (rc=$INST_RC)"
python3 - "$closeout_case/ledger.json" "$closeout_case/plan.json" <<'PY' \
  && ok "installed proposal names the consumer contract fields" \
  || fail "installed proposal shape"
import json
import sys
ledger = json.load(open(sys.argv[1]))
plan = json.load(open(sys.argv[2]))
record = ledger["records"]["two-web-next#291"]
assert record["disposition"] == "UNKNOWN_CHECKS", record
assert record["nextActor"] == "DevOps CI steward", record
assert len(plan["proposals"]) == 1, plan
proposal = plan["proposals"][0]
assert proposal["prKey"] == "two-web-next#291", proposal
assert proposal["reason"] == "owner_unmapped", proposal
assert proposal["mutation"] == "none", proposal
assert proposal["owner"] == "Director", proposal
assert proposal["recommendedAction"] == "map author/successor/reviewer card in the registry", proposal
assert proposal["nextCheckAt"] == record["nextCheckAt"], proposal
assert proposal["nextCheckAt"] is not None, proposal
PY

if run_install "$repo" "$base" "$reviewed" >/dev/null; then
  ok "reviewed commit installs"
else
  fail "reviewed commit installs"
fi
[[ "$(readlink "$base/current")" == "releases/$reviewed" ]] && ok "current is a relative atomic release pointer" || fail "current pointer target"
[[ "$(stat -c '%u:%g' "$base/current")" == "$(id -u):$(id -g)" ]] && ok "current pointer has the expected owner" || fail "current pointer owner"
[[ "$(sha256sum "$base/tool_drift.sh")" == "$legacy_hash" ]] && ok "legacy top-level file is preserved" || fail "legacy top-level file was changed"
for marker in tool_drift.sh capability_gate.sh org_request_queue.sh quota_brake.sh; do
  [[ -x "$base/current/$marker" && ! -L "$base/current/$marker" ]] || fail "marker missing: $marker"
done
ok "all four broker markers are regular executables"

if run_install "$repo" "$base" "$reviewed" >/dev/null; then
  ok "repeat installation of the same pin is idempotent"
else
  fail "repeat installation of the same pin is idempotent"
fi

expect_refusal "branch refs are refused" "must be a 40-hex commit" run_install "$repo" "$WORK/branch-base" main

missing_repo="$WORK/missing"
make_repo "$missing_repo"
rm "$missing_repo/lib/durable_queue.py"
missing=$(commit_repo "$missing_repo" missing)
expect_refusal "missing manifest member is refused" "missing manifest member lib/durable_queue.py" run_install "$missing_repo" "$WORK/missing-base" "$missing"
[[ ! -e "$WORK/missing-base/current" ]] && ok "missing member publishes no current pointer" || fail "missing member partially published current"

escape_repo="$WORK/escape"
make_repo "$escape_repo"
rm "$escape_repo/lib/pcsql.sh"
ln -s ../../outside "$escape_repo/lib/pcsql.sh"
escape=$(commit_repo "$escape_repo" symlink)
expect_refusal "symlink/path escape is refused" "not regular" run_install "$escape_repo" "$WORK/escape-base" "$escape"
[[ ! -e "$WORK/escape-base/current" ]] && ok "path escape publishes no current pointer" || fail "path escape partially published current"

preflight_repo="$WORK/preflight"
make_repo "$preflight_repo"
printf 'if then\n' >> "$preflight_repo/quota_brake.sh"
preflight=$(commit_repo "$preflight_repo" broken)
expect_refusal "failed syntax preflight is refused" "shell syntax preflight failed" run_install "$preflight_repo" "$WORK/preflight-base" "$preflight"
[[ ! -e "$WORK/preflight-base/releases/$preflight" && ! -e "$WORK/preflight-base/current" ]] \
  && ok "failed preflight publishes neither release nor current" || fail "failed preflight left partial publication"

runtime_repo="$WORK/runtime-preflight"
make_repo "$runtime_repo"
cat > "$runtime_repo/lib/durable_queue.py" <<'PY'
#!/usr/bin/env python3
raise SystemExit(0)
PY
chmod +x "$runtime_repo/lib/durable_queue.py"
runtime_preflight=$(commit_repo "$runtime_repo" runtime-broken)
expect_refusal "failed runtime preflight is refused" "runtime preflight produced no durable queue row" run_install "$runtime_repo" "$WORK/runtime-preflight-base" "$runtime_preflight"
[[ ! -e "$WORK/runtime-preflight-base/releases/$runtime_preflight" && ! -e "$WORK/runtime-preflight-base/current" ]] \
  && ok "runtime preflight failure publishes nothing" || fail "runtime preflight failure left partial publication"

unsafe_manifest_repo="$WORK/unsafe-manifest"
make_repo "$unsafe_manifest_repo"
printf '0444 ../escape\n' >> "$unsafe_manifest_repo/ops-tooling-runtime-manifest.txt"
unsafe_manifest=$(commit_repo "$unsafe_manifest_repo" unsafe-manifest)
expect_refusal "manifest path traversal is refused" "reviewed runtime manifest is invalid" run_install "$unsafe_manifest_repo" "$WORK/unsafe-manifest-base" "$unsafe_manifest"

bad_current_base="$WORK/bad-current/opt/paperclip-ops"
mkdir -p "$bad_current_base/current"
expect_refusal "non-symlink current is refused before release publication" "current exists and is not a symlink" run_install "$repo" "$bad_current_base" "$reviewed"
[[ ! -e "$bad_current_base/releases/$reviewed" ]] && ok "invalid current leaves no published release" || fail "invalid current left a partial release"

partial_base="$WORK/partial/opt/paperclip-ops"
mkdir -p "$partial_base/releases/$reviewed"
printf partial > "$partial_base/releases/$reviewed/tool_drift.sh"
expect_refusal "partial existing release is refused" "existing release path is not" run_install "$repo" "$partial_base" "$reviewed"
[[ ! -e "$partial_base/current" ]] && ok "partial existing release cannot publish current" || fail "partial release was published"

chmod 0775 "$base/releases/$reviewed"
expect_refusal "mutable release directory mode is refused" "mutable ownership/mode" run_install "$repo" "$base" "$reviewed"
chmod 0755 "$base/releases/$reviewed"

set +e
ownership_output=$(
  cd "$repo"
  OPS_TOOLING_INSTALL_TEST_MODE=1 \
  OPS_TOOLING_INSTALL_BASE="$base" \
  OPS_TOOLING_INSTALL_EXPECT_UID=$(( $(id -u) + 1 )) \
  OPS_TOOLING_INSTALL_EXPECT_GID="$(id -g)" \
  "$INSTALLER" --source-ref "$reviewed" 2>&1
)
ownership_rc=$?
set -e
if [[ $ownership_rc -eq 2 && "$ownership_output" == *"mutable ownership/mode"* ]]; then
  ok "wrong release ownership is refused"
else
  fail "wrong release ownership is refused (rc=$ownership_rc output=$ownership_output)"
fi

changed_repo="$WORK/changed"
make_repo "$changed_repo"
printf '# changed bytes\n' >> "$changed_repo/tool_drift.sh"
changed=$(commit_repo "$changed_repo" changed)
different_release="$WORK/different/opt/paperclip-ops/releases/$changed"
mkdir -p "$different_release"
cp -a "$base/releases/$reviewed/." "$different_release/"
chmod 0644 "$different_release/REVISION" "$different_release/SHA256SUMS" "$different_release/tool_drift.sh"
printf '%s\n' "$changed" > "$different_release/REVISION"
printf '# valid but not reviewed for this pin\n' >> "$different_release/tool_drift.sh"
chmod 0555 "$different_release/tool_drift.sh"
(
  cd "$different_release"
  sha256sum capability_gate.sh lib/durable_queue.py lib/notify_exec.py lib/pcsql.sh lib/provisioning_policy.sh lib/reqrecord.sh org_provisioner.sh org_request_queue.sh pacing_verdict.py pr_closeout_ledger.py queue_liveness.sh quota_brake.sh quota_brake_exempt.txt tool_drift.sh .release-manifest REVISION > SHA256SUMS
)
chmod 0444 "$different_release/REVISION" "$different_release/SHA256SUMS"
expect_refusal "different byte set at pinned release path is refused" "different byte set" run_install "$changed_repo" "$WORK/different/opt/paperclip-ops" "$changed"

if (( failures != 0 )); then
  printf '%s ops-tooling release installer tests failed\n' "$failures" >&2
  exit 1
fi
printf 'ops-tooling release installer tests passed\n'
