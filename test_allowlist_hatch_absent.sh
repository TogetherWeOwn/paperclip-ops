#!/usr/bin/env bash
# CI gate: the allowlist test hatch (PAPERCLIP_API_ALLOWLIST_TEST) is
# env-only and test-only. It must be absent from production surfaces
# (systemd units, env examples, operator templates, workflows) and must
# never default on in sender code or the shared helpers: hermetic suites
# set it explicitly per invocation, nothing else may.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       %s\n' "$2"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

HATCH="PAPERCLIP_API_ALLOWLIST_TEST"

hdr "hatch absent from production surfaces"
scope_a_hits=""
while IFS= read -r file; do
  if grep -qF "$HATCH" "$file"; then
    scope_a_hits+="${file}"$'\n'
  fi
done < <(
  { find "$HERE/systemd" "$HERE/host-bootstrap" "$HERE/.github" -type f 2>/dev/null;
    find "$HERE" -name '*.env.example' -not -path "$HERE/.paperclip-runtime/*" 2>/dev/null;
    find "$HERE" -name 'OPERATOR.md' -not -path "$HERE/.paperclip-runtime/*" 2>/dev/null; } | sort -u
)
if [[ -z "$scope_a_hits" ]]; then
  ok "no hatch in systemd units, env examples, operator templates, workflows"
else
  bad "no hatch in systemd units, env examples, operator templates, workflows" "$scope_a_hits"
fi

hdr "hatch never defaults on in senders or helpers"
SCOPE_B=(
  "$HERE/lib/paperclip_api_allowlist.py"
  "$HERE/lib/paperclip_api_allowlist.sh"
  "$HERE/agent_error_proposer.py"
  "$HERE/process_lost_probe.py"
  "$HERE/websearch_lane_probe.py"
  "$HERE/recovery_writer.py"
  "$HERE/scripts/model_pin_cleanup.py"
  "$HERE/ops/case_34c2c99f/prune_operator_run_dirs.py"
  "$HERE/ops/case_60e07cc2/project_worktree_regen_prune.py"
  "$HERE/ops/case_af881f62/operator_workspace_reconciler.py"
  "$HERE/ops/case_b1d141af/terminal_worktree_prune.py"
  "$HERE/ops/case_b5184419/terminal_worktree_cache_reclaim.py"
  "$HERE/notify_paperclip_issue.sh"
  "$HERE/db_backup_stall.sh"
  "$HERE/db_backup_stall_monitor.sh"
  "$HERE/red_main_poll.sh"
  "$HERE/sibling_guard.sh"
  "$HERE/scripts/dispatch_deploy_monitor.sh"
  "$HERE/ops/case_4ea7d92e/lib/post_finding.sh"
  "$HERE/ops/case_b1d141af/run-worktree-prune.sh"
)
missing=0
for file in "${SCOPE_B[@]}"; do
  [[ -f "$file" ]] || { bad "scope lists a file that no longer exists" "$file"; missing=1; }
done
[[ "$missing" -eq 0 ]] && ok "all scoped sender/helper files exist"
# Default-on shapes: a direct =1 assignment, a shell :-1} default, a
# mapping .get/.setdefault with a "1" default (literal or via the
# HATCH_VAR constant), or any default for the host-delegated
# CHECKED_BASE bypass (see below). Plain `== "1"` comparisons never match.
# Capture first, then test the capture: piping a producer straight into an
# early-exiting `grep -q` under pipefail lets SIGPIPE read a match as a
# miss (case_c25c4d3f).
hits_b_assign="$(grep -rnE "$HATCH\"?\]?\s*=[^=]" "${SCOPE_B[@]}" 2>/dev/null | grep -vE '==')"
if grep -q . <<<"$hits_b_assign"; then
  bad "no direct hatch assignment in senders/helpers" "$(head -n 5 <<<"$hits_b_assign")"
else
  ok "no direct hatch assignment in senders/helpers"
fi
hits_b_shell="$(grep -rnE "${HATCH}:-[^}]*1" "${SCOPE_B[@]}" 2>/dev/null)"
if grep -q . <<<"$hits_b_shell"; then
  bad "no shell default-on in senders/helpers" "$(head -n 5 <<<"$hits_b_shell")"
else
  ok "no shell default-on in senders/helpers"
fi
hits_b_map="$(grep -rnE "\.(get|setdefault)\(\s*(HATCH_VAR|[\"']$HATCH[\"'])\s*,\s*[\"']?1" "${SCOPE_B[@]}" 2>/dev/null)"
if grep -q . <<<"$hits_b_map"; then
  bad "no mapping default-on in senders/helpers" "$(head -n 5 <<<"$hits_b_map")"
else
  ok "no mapping default-on in senders/helpers"
fi
# The host-delegated verdict (PAPERCLIP_API_ALLOWLIST_CHECKED_BASE) skips
# the in-container file check on exact equality with the forwarded base.
# It is set only by the wrapper after its host check; no scoped file may
# give it a default, or a sender could self-delegate a verdict.
CHECKED="PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"
hits_b_checked="$(grep -rnE "\.(get|setdefault)\(\s*([\"']$CHECKED[\"'])\s*,\s*([A-Za-z0-9_]|\"[^\"]|'[^'])|${CHECKED}:?-[^}]" "${SCOPE_B[@]}" 2>/dev/null)"
if grep -q . <<<"$hits_b_checked"; then
  bad "no CHECKED_BASE default in senders/helpers" "$(head -n 5 <<<"$hits_b_checked")"
else
  ok "no CHECKED_BASE default in senders/helpers"
fi

hdr "positive controls (the greps detect real violations)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_hatch_absent.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
printf 'Environment=%s=1\n' "$HATCH" > "$WORK/fake.service"
if grep -qF "$HATCH" "$WORK/fake.service"; then
  ok "scope-A grep catches a unit file violation"
else
  bad "scope-A grep catches a unit file violation"
fi
printf '%s=1\n' "$HATCH" > "$WORK/assign.sh"
printf 'echo "${%s:-1}"\n' "$HATCH" > "$WORK/default.sh"
printf 'x = env.get("%s", "1")\n' "$HATCH" > "$WORK/getmap.py"
printf 'x = env.get(HATCH_VAR, "1")\n' > "$WORK/getconst.py"
grep -qE "$HATCH\"?\]?\s*=[^=]" "$WORK/assign.sh" \
  && ok "assignment grep catches =1" || bad "assignment grep catches =1"
grep -qE "${HATCH}:-[^}]*1" "$WORK/default.sh" \
  && ok "default grep catches :-1}" || bad "default grep catches :-1}"
printf 'x = env.setdefault("%s", "1")\n' "$HATCH" > "$WORK/setdefault.py"
for fixture in "$WORK/getmap.py" "$WORK/getconst.py" "$WORK/setdefault.py"; do
  grep -qE "\.(get|setdefault)\(\s*(HATCH_VAR|[\"']$HATCH[\"'])\s*,\s*[\"']?1" "$fixture" \
    && ok "mapping grep catches default in ${fixture##*/}" \
    || bad "mapping grep catches default in ${fixture##*/}"
done
printf 'x = env.get("%s", base)\n' "$CHECKED" > "$WORK/checked_default.py"
printf 'echo "${%s:-$PAPERCLIP_API_URL}"\n' "$CHECKED" > "$WORK/checked_default.sh"
printf 'echo "${%s-$PAPERCLIP_API_URL}"\n' "$CHECKED" > "$WORK/checked_default_unset.sh"
for fixture in "$WORK/checked_default.py" "$WORK/checked_default.sh" "$WORK/checked_default_unset.sh"; do
  grep -qE "\.(get|setdefault)\(\s*([\"']$CHECKED[\"'])\s*,\s*([A-Za-z0-9_]|\"[^\"]|'[^'])|${CHECKED}:?-[^}]" "$fixture" \
    && ok "CHECKED_BASE grep catches default in ${fixture##*/}" \
    || bad "CHECKED_BASE grep catches default in ${fixture##*/}"
done
printf 'x = env.get("%s", "")\n' "$CHECKED" > "$WORK/checked_legit.py"
hits_checked_legit="$(grep -E "\.(get|setdefault)\(\s*([\"']$CHECKED[\"'])\s*,\s*([A-Za-z0-9_]|\"[^\"]|'[^'])|${CHECKED}:?-[^}]" "$WORK/checked_legit.py")"
if grep -q . <<<"$hits_checked_legit"; then
  bad "empty CHECKED_BASE default does not trip the gate"
else
  ok "empty CHECKED_BASE default does not trip the gate"
fi
# The legitimate comparison shape must NOT trip the assignment grep.
printf 'if [[ "${%s:-}" == "1" ]]; then\n' "$HATCH" > "$WORK/legit.sh"
printf 'if env.get(HATCH_VAR, "") == "1":\n' > "$WORK/legit.py"
hits_legit="$(grep -E "$HATCH\"?\]?\s*=[^=]" "$WORK/legit.sh" "$WORK/legit.py" | grep -vE '==')"
if grep -q . <<<"$hits_legit"; then
  bad "legitimate comparisons do not trip the gate"
else
  ok "legitimate comparisons do not trip the gate"
fi

printf '\nhatch-absent gate: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
