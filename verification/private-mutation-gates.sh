#!/usr/bin/env bash
# Targeted mutation gates for private integration suites whose generic
# dependencies now run mutation coverage in paperclip-operator-toolkit.
set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

for command in bash cmp cp jq node python3 sed tail timeout; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'ERROR: required command is missing: %s\n' "$command" >&2
    exit 1
  }
done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
rc=0

# Only an assertion-failed suite (exit 1) proves a mutant was caught. The
# watchdog itself can also exit 1 when its child fails to launch, so the suite
# wrapper remaps that child status to 42 before the watchdog reports it.
mutation_run_is_unattributable() {
  local status="$1"
  (( status != 42 ))
}

write_mutation_suite_wrapper() {
  local dir="$1" suite="$2" wrapper
  wrapper="$dir/.mutation-suite-wrapper.sh"
  cat >"$wrapper" <<EOF || return 1
#!/usr/bin/env bash
if "./$suite" "\$@"; then
  suite_rc=0
else
  suite_rc=\$?
fi
if (( suite_rc == 1 )); then
  exit 42
fi
if (( suite_rc == 42 )); then
  exit 43
fi
exit "\$suite_rc"
EOF
  chmod +x "$wrapper"
}

# Request-record integration: these assertions exercise the private queue
# against the toolkit-owned record helper. Verify one passing baseline before
# applying the three defects the old gate assigned to this suite.
request_root="$TMP/request-record"
baseline="$request_root/baseline"
mkdir -p "$baseline/lib" || exit 1
if ! cp org_request_queue.sh org_provisioner.sh org_access_review.sh \
  test_responsible_leader.sh test_request_record_integrity.sh \
  test_decision_notify.sh test_leader_notify.sh "$baseline/" \
  || ! cp lib/*.sh lib/*.py "$baseline/lib/"; then
  printf 'ERROR: could not stage request-record baseline\n' >&2
  exit 1
fi
if ! write_mutation_suite_wrapper "$baseline" "test_request_record_integrity.sh"; then
  printf 'ERROR: could not stage request-record suite wrapper\n' >&2
  exit 1
fi

request_suite() {
  local dir="$1" output="$2" limit="${MUTATION_SUITE_TIMEOUT:-150}"
  python3 "$dir/lib/mutation_watchdog.py" "$dir" \
    .mutation-suite-wrapper.sh "$limit" >"$output" 2>&1
}

baseline_output="$request_root/baseline.out"
if request_suite "$baseline" "$baseline_output"; then
  :
else
  base_rc=$?
  printf 'ERROR: unmutated request-record suite failed (rc=%s)\n' "$base_rc" >&2
  tail -20 "$baseline_output" >&2
  exit 1
fi

request_mutation() {
  local label="$1"; shift
  local mut="$request_root/$label" expr target mut_rc
  mkdir -p "$mut" || return 1
  cp -a "$baseline/." "$mut/" || return 1
  for expr in "$@"; do
    target="$mut/org_request_queue.sh"
    if [[ "$expr" == lib:* ]]; then
      target="$mut/lib/reqrecord.sh"
      expr="${expr#lib:}"
    fi
    if ! sed -i "$expr" "$target"; then
      printf 'ERROR: %s mutation could not be applied\n' "$label" >&2
      rm -rf "$mut"
      return 1
    fi
  done
  if ! bash -n "$mut/org_request_queue.sh" "$mut/lib/reqrecord.sh"; then
    printf 'ERROR: %s mutation broke parsing\n' "$label" >&2
    rm -rf "$mut"
    return 1
  fi
  if cmp -s "$baseline/org_request_queue.sh" "$mut/org_request_queue.sh" \
    && cmp -s "$baseline/lib/reqrecord.sh" "$mut/lib/reqrecord.sh"; then
    printf 'ERROR: %s mutation changed no target bytes\n' "$label" >&2
    rm -rf "$mut"
    return 1
  fi
  if request_suite "$mut" "$mut/.mutant-output"; then
    printf 'ERROR: request-record suite passed against %s\n' "$label" >&2
    rm -rf "$mut"
    return 1
  else
    mut_rc=$?
  fi
  if mutation_run_is_unattributable "$mut_rc"; then
    printf 'ERROR: %s mutation run did not report a test assertion failure (rc=%s)\n' \
      "$label" "$mut_rc" >&2
    rm -rf "$mut"
    return 1
  fi
  printf '  ok  %s\n' "$label"
  rm -rf "$mut"
}

request_mutation "unreasoned-approval" \
  's/\[\[ -n "$reason" \]\] || die "every decision/[[ -n "$reason" || "$decision" == "approved" ]] || die "every decision/' || rc=1
request_mutation "never-expires" \
  's/^reap_expired() {/reap_expired() { return 0;/' \
  's/expired "$exp" \&\& rec=/false \&\& rec=/' || rc=1
request_mutation "ambiguous-request-id" \
  'lib:s/\[\[ "$n" -le 1 \]\]/[[ "$n" -le 99 ]]/' || rc=1

# Private notification transport: the test uses a local curl stub, never a
# network or credential. Each mutant must be rejected by the unmutated suite.
notify_root="$TMP/notify-transport"
notify_baseline="$notify_root/baseline"
mkdir -p "$notify_baseline" "$notify_baseline/lib" || exit 1
if ! cp notify_paperclip_issue.sh test_notify_transport.sh "$notify_baseline/"; then
  printf 'ERROR: could not stage notification baseline\n' >&2
  exit 1
fi
if ! cp lib/paperclip_api_allowlist.sh "$notify_baseline/lib/"; then
  printf 'ERROR: could not stage notification allowlist helper\n' >&2
  exit 1
fi
chmod +x "$notify_baseline/notify_paperclip_issue.sh" \
  "$notify_baseline/test_notify_transport.sh" || exit 1
if ! write_mutation_suite_wrapper "$notify_baseline" "test_notify_transport.sh"; then
  printf 'ERROR: could not stage notification suite wrapper\n' >&2
  exit 1
fi

notify_suite() {
  # The multibyte oversized-body baseline exceeds 60s on isolated runners.
  local dir="$1" output="$2" limit="${NOTIFY_MUTATION_TIMEOUT:-180}"
  NOTIFY_SH="$dir/notify_paperclip_issue.sh" \
    timeout "$limit" "$dir/.mutation-suite-wrapper.sh" >"$output" 2>&1
}

notify_baseline_output="$notify_root/baseline.out"
if notify_suite "$notify_baseline" "$notify_baseline_output"; then
  :
else
  base_rc=$?
  printf 'ERROR: unmutated notification transport suite failed (rc=%s)\n' "$base_rc" >&2
  tail -20 "$notify_baseline_output" >&2
  exit 1
fi

notify_mutation() {
  local label="$1"; shift
  local mut="$notify_root/$label" expr mut_rc
  mkdir -p "$mut" || return 1
  cp -a "$notify_baseline/." "$mut/" || return 1
  for expr in "$@"; do
    if ! sed -i "$expr" "$mut/notify_paperclip_issue.sh"; then
      printf 'ERROR: %s mutation could not be applied\n' "$label" >&2
      rm -rf "$mut"
      return 1
    fi
  done
  if ! bash -n "$mut/notify_paperclip_issue.sh"; then
    printf 'ERROR: %s mutation broke parsing\n' "$label" >&2
    rm -rf "$mut"
    return 1
  fi
  if cmp -s "$notify_baseline/notify_paperclip_issue.sh" \
    "$mut/notify_paperclip_issue.sh"; then
    printf 'ERROR: %s mutation changed no target bytes\n' "$label" >&2
    rm -rf "$mut"
    return 1
  fi
  if notify_suite "$mut" "$mut/.mutant-output"; then
    printf 'ERROR: notification transport suite passed against %s\n' "$label" >&2
    rm -rf "$mut"
    return 1
  else
    mut_rc=$?
  fi
  if mutation_run_is_unattributable "$mut_rc"; then
    printf 'ERROR: %s mutation run did not report a test assertion failure (rc=%s)\n' \
      "$label" "$mut_rc" >&2
    rm -rf "$mut"
    return 1
  fi
  printf '  ok  %s\n' "$label"
  rm -rf "$mut"
}

notify_mutation "notify-credential-on-argv" \
  's|^RESP="\$(curl --config "\$CFG"|RESP="$(curl --config "$CFG" -H "Authorization: Bearer $PAPERCLIP_API_KEY"|' || rc=1
notify_mutation "notify-unvalidated-issue-id" \
  's|^if \[\[ ! "\$ISSUE" =~ \^\[A-Za-z0-9\]\[A-Za-z0-9_-\]\*\$ \]\]; then|if false; then|' || rc=1
notify_mutation "notify-fence-not-measured" \
  's|^FENCE_LEN=\$(( \$(longest_tick_run "\$BODY") + 1 )).*$|FENCE_LEN=3|' || rc=1
notify_mutation "notify-renders-status-only" \
  's/^STATUS=.*$/STATUS="$(jq -r '\''\.status'\'' <<<"$PAYLOAD")"/' || rc=1
notify_mutation "notify-leader-uses-requester-fallback" \
  's/^if \[\[ -z "$ISSUE" && "$AUDIENCE" == "requester" \]\]; then/if [[ -z "$ISSUE" ]]; then/' || rc=1
notify_mutation "notify-oversized-comment-refused" \
  's/^if (( MSG_BYTES > MAX_COMMENT_BYTES )); then/if false; then/' || rc=1

# The endpoint-preflight source is restored from the pinned public toolkit;
# this private suite is not part of that repository, so keep its 11 targeted
# assertion-killing mutants here.
preflight_root="$TMP/endpoint-preflight"
preflight_baseline="$preflight_root/baseline"
mkdir -p "$preflight_baseline/lib" || exit 1
if ! cp agent_endpoint_preflight.sh test_agent_endpoint_preflight.sh \
  "$preflight_baseline/" \
  || ! cp lib/mutation_watchdog.py "$preflight_baseline/lib/"; then
  printf 'ERROR: could not stage endpoint-preflight baseline\n' >&2
  exit 1
fi
if ! write_mutation_suite_wrapper "$preflight_baseline" \
  "test_agent_endpoint_preflight.sh"; then
  printf 'ERROR: could not stage endpoint-preflight suite wrapper\n' >&2
  exit 1
fi

preflight_suite() {
  local dir="$1" output="$2" limit="${PREFLIGHT_MUTATION_TIMEOUT:-180}"
  python3 "$dir/lib/mutation_watchdog.py" "$dir" \
    .mutation-suite-wrapper.sh "$limit" >"$output" 2>&1
}

preflight_baseline_output="$preflight_root/baseline.out"
if preflight_suite "$preflight_baseline" "$preflight_baseline_output"; then
  :
else
  base_rc=$?
  printf 'ERROR: unmutated endpoint-preflight suite failed (rc=%s)\n' "$base_rc" >&2
  tail -25 "$preflight_baseline_output" >&2
  exit 1
fi

preflight_mutation() {
  local label="$1"; shift
  local mut="$preflight_root/$label" expr mut_rc
  mkdir -p "$mut" || return 1
  cp -a "$preflight_baseline/." "$mut/" || return 1
  for expr in "$@"; do
    if ! sed -i "$expr" "$mut/agent_endpoint_preflight.sh"; then
      printf 'ERROR: %s mutation could not be applied\n' "$label" >&2
      rm -rf "$mut"
      return 1
    fi
  done
  if cmp -s "$preflight_baseline/agent_endpoint_preflight.sh" \
    "$mut/agent_endpoint_preflight.sh"; then
    printf 'ERROR: %s mutation changed no target bytes\n' "$label" >&2
    rm -rf "$mut"
    return 1
  fi
  if ! bash -n "$mut/agent_endpoint_preflight.sh"; then
    printf 'ERROR: %s mutation broke parsing\n' "$label" >&2
    rm -rf "$mut"
    return 1
  fi
  if preflight_suite "$mut" "$mut/.mutant-output"; then
    printf 'ERROR: endpoint-preflight suite passed against %s\n' "$label" >&2
    rm -rf "$mut"
    return 1
  else
    mut_rc=$?
  fi
  if mutation_run_is_unattributable "$mut_rc"; then
    printf 'ERROR: %s mutation run did not report a test assertion failure (rc=%s)\n' \
      "$label" "$mut_rc" >&2
    rm -rf "$mut"
    return 1
  fi
  printf '  ok  %s\n' "$label"
  rm -rf "$mut"
}

preflight_mutation "unreachable-reads-ok" \
  's/verdict "unreachable" 5/verdict "unreachable" 0/g' || rc=1
preflight_mutation "hang-reads-ok" \
  's/verdict "timeout" 6/verdict "timeout" 0/' || rc=1
preflight_mutation "cache-creation-check-deleted" \
  's/"\$CACHE_CREATE" -le 0/"$CACHE_CREATE" -le -1/' || rc=1
preflight_mutation "cache-replay-check-deleted" \
  's/"\$CACHE_REPLAY_READ" -le 0/"$CACHE_REPLAY_READ" -le -1/' || rc=1
preflight_mutation "payg-not-flagged" \
  's/^\[\[ "\$RESP_ID" == gen-\* \]\]/[[ "$RESP_ID" == nevermatch-* ]]/' \
  's|^\[\[ "\$RESP_MODEL" == \*/\* \]\]|[[ "$RESP_MODEL" == *@@@* ]]|' || rc=1
preflight_mutation "wrong-dialect-accepted" \
  's/^  anthropic) ;;$/  anthropic|openai|error|*) ;;/' || rc=1
preflight_mutation "retries-on-auth-failure" \
  's|^CURL_EXIT=0$|curl -sS -o /dev/null -H "@$HDRS" -d "$REQ" --max-time "$TIMEOUT" "$BASE/v1/messages" >/dev/null 2>\&1\nCURL_EXIT=0|' || rc=1
preflight_mutation "dead-model-lane-reads-ok" \
  's/verdict "model-unavailable" 8/verdict "model-unavailable" 0/' || rc=1
preflight_mutation "model-lane-reads-as-outage" \
  's/if \[\[ "\$CCODE" == 200 \]\]; then/if [[ "$CCODE" == 999 ]]; then/' || rc=1
preflight_mutation "client-identity-not-sent" \
  's/^CLIENT_IDENTITY=1$/CLIENT_IDENTITY=0/' || rc=1
preflight_mutation "combo-output-budget-too-small" \
  's/"max_tokens":16/"max_tokens":1/g' || rc=1

printf 'private mutation gates: %s\n' "$([[ $rc -eq 0 ]] && printf PASS || printf FAIL)"
exit "$rc"
