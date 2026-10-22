#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEFAULT_TARGET=/paperclip/plugin-packages-root/dispatch-src
readonly REF_CURRENT=refs/dispatch-deploy/current
readonly REF_ROLLBACK=refs/dispatch-deploy/rollback
readonly SCHEMA_VERSION=2
readonly JSON_SCHEMA_VERSION=1
readonly DEFAULT_SMOKE_TIMEOUT=10
readonly SMOKE_TIMEOUT_MIN=1
readonly SMOKE_TIMEOUT_MAX=600
readonly SMOKE_KILL_GRACE=2
readonly SMOKE_TAIL_BYTES=8192
readonly LOCK_WAIT_MAX=5

usage() {
  cat <<'USAGE'
Usage:
  dispatch_deploy.sh deploy   --commit <40-hex-commit> --smoke-cmd <cmd> [--smoke-timeout <1..600>] [--reason <text>] [--json] [--dry-run]
  dispatch_deploy.sh rollback [--commit <40-hex-commit>] --smoke-cmd <cmd> [--smoke-timeout <1..600>] [--reason <text>] [--json] [--dry-run]
  dispatch_deploy.sh status [--json]
  dispatch_deploy.sh --help | -h

Manages the live in-place checkout at /paperclip/plugin-packages-root/dispatch-src.

deploy:
  Accepts only an explicit 40-hex commit already present in the target's object
  store (it never fetches). Refuses unless the working tree is clean and the
  ref names a real commit object. Stages the commit into a throwaway worktree
  and runs --smoke-cmd there BEFORE moving live HEAD; a failing or
  over-deadline smoke refuses the deploy and live HEAD, anchor refs and the
  deployment record are provably untouched. On success it anchors both the new
  and previous SHA against `git gc` via refs/dispatch-deploy/*, moves live
  HEAD, verifies the result, and atomically records deployedSha/previousSha
  in <target>.deployment.json.

rollback:
  Same staged-smoke-then-move flow, defaulting the target commit to the
  anchored refs/dispatch-deploy/rollback SHA (override with --commit).

status:
  Read-only. Refuses (exit 2) if the deployment record is absent or corrupt,
  or if a recorded SHA is no longer a reachable commit object. Exits 1 if live
  HEAD or the anchor refs have drifted from the record, 0 if not.

Smoke timeout:
  Every smoke runs under a finite deadline, default 10s, override with
  --smoke-timeout 1..600. Past the deadline the wrapper terminates the whole
  smoke process group, escalating to a forced kill within 2s. Retained smoke
  output is bounded to the last 8KB; structured errors carry a stable reason
  and never embed raw smoke output.

Cleanup:
  The invocation's own smoke worktree, its registration and its capture file
  are removed on success, failure, timeout and catchable INT/TERM. Only this
  invocation's temporary paths are ever removed. A failed cleanup is reported
  as a nonzero cleanup failure, never as success.

Lock:
  deploy and rollback take one shared per-target transaction lock before
  reading live HEAD or selecting the implicit rollback SHA, then revalidate
  the clean-tree and commit guards under that lock. A contended lock waits up
  to 5s for the holder, then refuses (exit 2); different targets never block
  each other.

--json:
  Print one versioned JSON object (schemaVersion 1) on stdout with command,
  stage, exitCode, target, retryable and next_action (plus deployedSha/
  previousSha on success and a stable error code on failure). Human text
  stays on stderr. Exit meanings are unchanged.

--dry-run:
  Print a static zero-write plan and exit 0: no lock, worktree, ref or
  record is created and the smoke command never runs. (status takes no
  --dry-run, --commit, --smoke-cmd or --smoke-timeout.)

Exit codes: 0 ok (or drift-free status, or dry-run plan), 1 status drift
  detected, 2 refused (bad flags, dirty tree, unknown object, smoke failure,
  smoke timeout, lock contention, cleanup failure, bad record, or a torn
  post-deploy verification).

Examples:
  deploy a pinned commit with a 30s smoke budget:
    scripts/dispatch_deploy.sh deploy --commit <40-hex> --smoke-cmd './smoke.sh' --smoke-timeout 30
  preview a rollback, then run it:
    scripts/dispatch_deploy.sh rollback --smoke-cmd './smoke.sh' --dry-run
    scripts/dispatch_deploy.sh rollback --smoke-cmd './smoke.sh'
  machine-readable status for agents:
    scripts/dispatch_deploy.sh status --json
USAGE
}

# Stable snake_case error code for a refusal message. Every refuse() message
# below maps here so --json outcomes stay versioned without duplicating the
# human text at each call site.
refuse_code_for_message() {
  case "$1" in
    "DISPATCH_DEPLOY_TARGET is test-only") printf 'test_only_violation';;
    "DISPATCH_DEPLOY_TEST_MODE"*) printf 'invalid_test_mode';;
    "DISPATCH_DEPLOY_TARGET must be set"*) printf 'missing_test_target';;
    "required command missing"*) printf 'missing_command';;
    "target is not a git checkout"*) printf 'not_a_checkout';;
    "commit must be a 40-hex commit") printf 'invalid_commit';;
    "commit is not an available object"*) printf 'unknown_object';;
    "ref does not name a commit object directly"*) printf 'not_a_commit';;
    "--smoke-cmd is required") printf 'missing_smoke_cmd';;
    "deploy requires --commit") printf 'missing_commit';;
    "target commit is already deployed"*) printf 'already_deployed';;
    "unknown argument"*) printf 'unknown_argument';;
    "unknown subcommand"*) printf 'unknown_subcommand';;
    "status takes no "*) printf 'misplaced_flag';;
    "option --"*) printf 'missing_option_value';;
    "--smoke-timeout must be"*) printf 'invalid_smoke_timeout';;
    "could not stage "*) printf 'worktree_stage_failed';;
    "smoke check timeout"*) printf 'smoke_timeout';;
    "smoke check failed"*) printf 'smoke_failed';;
    "interrupted by "*) printf 'interrupted';;
    "cleanup failed"*) printf 'cleanup_failed';;
    "could not acquire transaction lock"*) printf 'lock_contention';;
    "cannot create "*) printf 'internal_error';;
    "target working tree is not clean"*) printf 'dirty_tree';;
    "no anchored rollback SHA"*) printf 'no_rollback_anchor';;
    "no deployment record"*) printf 'no_record';;
    "deployment record is corrupt"*) printf 'corrupt_record';;
    "recorded deployedSha is no longer"*) printf 'pin_gone';;
    "recorded previousSha is no longer"*) printf 'pin_gone';;
    "target HEAD is not resolvable"*) printf 'unresolvable_head';;
    "post-deploy HEAD verification failed"*) printf 'post_deploy_verification_failed';;
    *) printf 'refused';;
  esac
}

refuse_retryable_for_code() {
  case "$1" in
    smoke_failed|smoke_timeout|lock_contention|dirty_tree|interrupted|cleanup_failed|worktree_stage_failed) printf 'true';;
    *) printf 'false';;
  esac
}

refuse_next_action_for_code() {
  case "$1" in
    invalid_smoke_timeout) printf 'pass --smoke-timeout 1..600 or omit it for the 10s default';;
    unknown_argument|unknown_subcommand|misplaced_flag|missing_option_value|missing_commit|missing_smoke_cmd) printf 'correct the flags and retry (see --help)';;
    invalid_commit|unknown_object|not_a_commit) printf 'pass an explicit 40-hex commit already present in the target object store';;
    not_a_checkout) printf 'point the target at a git checkout (the test-only override requires DISPATCH_DEPLOY_TEST_MODE=1)';;
    test_only_violation|invalid_test_mode|missing_test_target) printf 'unset DISPATCH_DEPLOY_TARGET outside test mode, or set it with DISPATCH_DEPLOY_TEST_MODE=1';;
    missing_command) printf 'install the missing command and retry';;
    dirty_tree) printf 'restore a clean working tree, then retry';;
    already_deployed) printf 'no action: the desired commit is already live';;
    lock_contention) printf 'retry after the in-flight transaction finishes';;
    smoke_failed) printf 'fix the smoke failure, then retry';;
    smoke_timeout) printf 'investigate the hung smoke command, then retry';;
    interrupted) printf 're-run after confirming live state with status';;
    cleanup_failed) printf 'remove the stale smoke worktree registration manually, then retry';;
    worktree_stage_failed) printf 'check git worktree state and disk space, then retry';;
    no_record|corrupt_record) printf 'deploy once to establish the record, then use status';;
    pin_gone) printf 'redeploy a reachable commit to re-anchor the record';;
    no_rollback_anchor) printf 'pass an explicit --commit to rollback';;
    unresolvable_head) printf 'inspect the target checkout, then retry';;
    post_deploy_verification_failed) printf 'inspect HEAD, anchors and record immediately; do not retry blindly';;
    internal_error) printf 'check disk space and temp directory permissions, then retry';;
    *) printf 'correct the issue and retry';;
  esac
}

# One versioned JSON outcome on stdout. Never embeds raw smoke output.
# Args: exit_code stage err_code err_msg retryable next_action deployed previous commit dry_run
json_outcome() {
  python3 - "$JSON_SCHEMA_VERSION" "${COMMAND:-unknown}" "$1" "$2" "${TARGET:-}" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10:-}" <<'PY'
import json
import sys
(schema, command, exit_code, stage, target, error_code, error_message,
 retryable, next_action, deployed, previous, commit, dry_run) = sys.argv[1:14]
outcome = {
    "schemaVersion": int(schema),
    "command": command,
    "stage": stage,
    "exitCode": int(exit_code),
    "target": target or None,
    "retryable": retryable == "true",
    "next_action": next_action,
}
if error_code:
    outcome["error"] = {"code": error_code, "message": error_message}
if deployed:
    outcome["deployedSha"] = deployed
if previous:
    outcome["previousSha"] = previous
if commit:
    outcome["commit"] = commit
if dry_run == "true":
    outcome["dryRun"] = True
print(json.dumps(outcome, sort_keys=True))
PY
}

refuse() {
  local message="$1" code retryable next_action
  code=$(refuse_code_for_message "$message")
  retryable=$(refuse_retryable_for_code "$code")
  next_action=$(refuse_next_action_for_code "$code")
  printf 'REFUSED: %s\n' "$message" >&2
  if [[ "${JSON_MODE:-0}" == "1" ]] && command -v python3 >/dev/null 2>&1; then
    json_outcome 2 "${STAGE:-validate}" "$code" "$message" "$retryable" "$next_action" "" "" "" ""
  fi
  exit 2
}

log() {
  if [[ "${JSON_MODE:-0}" == "1" ]]; then
    printf '%s\n' "$1" >&2
  else
    printf '%s\n' "$1"
  fi
}

is_forty_hex() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

require_clean_tree() {
  [[ -z "$(git -C "$TARGET" status --porcelain 2>&1)" ]] || refuse "target working tree is not clean: $TARGET"
}

require_commit_object() {
  local sha="$1" type
  type=$(git -C "$TARGET" cat-file -t "$sha" 2>/dev/null) || refuse "commit is not an available object in the target's store: $sha"
  [[ "$type" == commit ]] || refuse "ref does not name a commit object directly: $sha"
}

atomic_write() {
  local destination="$1" content="$2" tmp
  tmp=$(mktemp "$(dirname "$destination")/.tmp.XXXXXXXX")
  printf '%s' "$content" > "$tmp"
  mv -T -- "$tmp" "$destination"
}

# Remove ONLY this invocation's smoke worktree registration and directory.
# Returns 0 when nothing remains, 1 when its own registration survives.
# Never touches another worktree and never runs a repo-wide prune.
remove_owned_worktree() {
  [[ -n "${SMOKE_DIR:-}" ]] || return 0
  local dir="$SMOKE_DIR" list
  case "$dir" in
    "${TMPDIR:-/tmp}"/dispatch-smoke-wt.*) ;;
    *) return 1 ;;
  esac
  git -C "$TARGET" worktree remove --force "$dir" 2>/dev/null || rm -rf -- "$dir" 2>/dev/null || true
  list=$(git -C "$TARGET" worktree list --porcelain 2>/dev/null || true)
  if [[ "$list" == *"worktree $dir"* ]]; then
    return 1
  fi
  SMOKE_DIR=""
  return 0
}

acquire_transaction_lock() {
  if [[ -n "${LOCK_HELD:-}" ]]; then
    return 0
  fi
  STAGE="lock"
  exec {LOCK_FD}>"$LOCK_FILE" || refuse "cannot create transaction lock file: $LOCK_FILE"
  # NOTE: never add 2>/dev/null to these bare exec closes: a redirection on
  # a command-less exec persists for the rest of the shell and would swallow
  # every later diagnostic, including this function's own refusal.
  SECONDS=0
  while ! flock -n "$LOCK_FD" 2>/dev/null; do
    if (( SECONDS >= LOCK_WAIT_MAX )); then
      exec {LOCK_FD}>&- || true
      unset LOCK_FD
      refuse "could not acquire transaction lock for $TARGET within ${LOCK_WAIT_MAX}s: another deploy or rollback holds it"
    fi
    sleep 0.1 2>/dev/null || true
  done
  LOCK_HELD=1
  STAGE="validate"
}

release_transaction_lock() {
  if [[ -n "${LOCK_FD:-}" ]]; then
    exec {LOCK_FD}>&- || true
    unset LOCK_FD
  fi
  unset LOCK_HELD
}

# Stage $1 into a throwaway worktree and run the smoke command there with a
# finite deadline. The smoke and its deadline keeper each run in their own
# process group: past the deadline the keeper terminates the smoke group and
# escalates to a forced kill, so a stuck or noisy child can neither hang the
# wrapper nor grow captured memory (output is read back bounded). Sets
# SMOKE_RC / SMOKE_TIMED_OUT / SMOKE_OUTPUT and removes the owned worktree
# before returning; SMOKE_CLEANUP_FAILED records a surviving registration.
run_smoke() {
  local sha="$1" smoke_cmd="$2" stage_dir
  SMOKE_TIMED_OUT=0
  SMOKE_RC=""
  SMOKE_OUTPUT=""
  SMOKE_CLEANUP_FAILED=0
  SMOKE_OUT=$(mktemp "${TMPDIR:-/tmp}/dispatch-smoke-out.XXXXXXXX") \
    || refuse "cannot create smoke capture file"
  SMOKE_FLAG=$(mktemp "${TMPDIR:-/tmp}/dispatch-smoke-flag.XXXXXXXX") \
    || refuse "cannot create smoke flag file"
  stage_dir=$(mktemp -d "${TMPDIR:-/tmp}/dispatch-smoke-wt.XXXXXXXX") \
    || refuse "cannot create smoke worktree parent"
  rmdir "$stage_dir"
  SMOKE_DIR="$stage_dir"
  git -C "$TARGET" worktree add --detach --quiet "$SMOKE_DIR" "$sha" \
    || { remove_owned_worktree || true; refuse "could not stage $sha into a throwaway worktree for smoke checks"; }
  set -m
  ( cd "$SMOKE_DIR" && bash -c "$smoke_cmd" >"$SMOKE_OUT" 2>&1 ) &
  SMOKE_PID=$!
  SMOKE_PGID=$SMOKE_PID
  (
    sleep "$SMOKE_TIMEOUT" || exit 0
    printf 'fired' >"$SMOKE_FLAG" 2>/dev/null || true
    kill -TERM -- "-$SMOKE_PGID" 2>/dev/null || true
    sleep "$SMOKE_KILL_GRACE" || true
    kill -KILL -- "-$SMOKE_PGID" 2>/dev/null || true
  ) &
  KILLER_PID=$!
  KILLER_PGID=$KILLER_PID
  set +m
  SMOKE_RUNNING=1
  set +e
  wait "$SMOKE_PID"
  SMOKE_RC=$?
  set -e
  SMOKE_RUNNING=0
  kill -KILL -- "-$KILLER_PGID" 2>/dev/null || true
  wait "$KILLER_PID" 2>/dev/null || true
  KILLER_PID=""
  KILLER_PGID=""
  if [[ "$(cat "$SMOKE_FLAG" 2>/dev/null)" == "fired" ]]; then
    SMOKE_TIMED_OUT=1
  fi
  kill -KILL -- "-$SMOKE_PGID" 2>/dev/null || true
  SMOKE_OUTPUT=$(tail -c "$SMOKE_TAIL_BYTES" "$SMOKE_OUT" 2>/dev/null || true)
  rm -f -- "$SMOKE_OUT" "$SMOKE_FLAG" 2>/dev/null || true
  SMOKE_OUT=""
  SMOKE_FLAG=""
  remove_owned_worktree || SMOKE_CLEANUP_FAILED=1
}

do_deploy() {
  local target_sha="$1" smoke_cmd="$2" reason="$3"
  is_forty_hex "$target_sha" || refuse "commit must be a 40-hex commit"
  [[ -n "$smoke_cmd" ]] || refuse "--smoke-cmd is required"
  acquire_transaction_lock
  require_clean_tree
  require_commit_object "$target_sha"

  local previous_sha
  previous_sha=$(git -C "$TARGET" rev-parse HEAD)

  if [[ "$previous_sha" == "$target_sha" ]]; then
    refuse "target commit is already deployed: $target_sha"
  fi

  STAGE="smoke"
  run_smoke "$target_sha" "$smoke_cmd"
  if [[ "$SMOKE_CLEANUP_FAILED" == "1" ]]; then
    refuse "cleanup failed: owned smoke worktree $SMOKE_DIR could not be removed; live HEAD is unchanged at $previous_sha"
  fi
  if [[ "$SMOKE_TIMED_OUT" == "1" ]]; then
    refuse "smoke check timeout: deadline ${SMOKE_TIMEOUT}s exceeded; live HEAD is unchanged at $previous_sha"
  fi
  if [[ "$SMOKE_RC" -ne 0 ]]; then
    printf '%s\n' "$SMOKE_OUTPUT" >&2
    refuse "smoke check failed (exit $SMOKE_RC); live HEAD is unchanged at $previous_sha"
  fi

  # Revalidate the guards under the lock: the smoke window is the last
  # moment an out-of-band change could have landed before live mutation.
  STAGE="mutate"
  require_clean_tree
  require_commit_object "$target_sha"

  # Anchor both endpoints against gc BEFORE moving live HEAD, so a crash
  # between here and the checkout below can never leave the new commit
  # collectible.
  MUTATING=1
  git -C "$TARGET" update-ref "$REF_ROLLBACK" "$previous_sha"
  git -C "$TARGET" update-ref "$REF_CURRENT" "$target_sha"

  git -C "$TARGET" checkout --quiet --detach "$target_sha"

  local resulting_head
  resulting_head=$(git -C "$TARGET" rev-parse HEAD)
  [[ "$resulting_head" == "$target_sha" ]] || refuse "post-deploy HEAD verification failed: expected $target_sha, got $resulting_head"
  require_clean_tree
  MUTATING=0

  STAGE="record"
  local deployed_at smoke_tail
  deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  smoke_tail=$(printf '%s' "$SMOKE_OUTPUT" | tail -c 2000)

  local record
  record=$(python3 - "$SCHEMA_VERSION" "$TARGET" "$target_sha" "$previous_sha" "$deployed_at" "$reason" "$smoke_cmd" "$SMOKE_RC" "$smoke_tail" "$SMOKE_TIMEOUT" <<'PY'
import json
import sys

(schema_version, path, deployed_sha, previous_sha, deployed_at, reason,
 smoke_cmd, smoke_rc, smoke_tail, smoke_timeout) = sys.argv[1:11]
print(json.dumps({
    "schemaVersion": int(schema_version),
    "path": path,
    "deployedSha": deployed_sha,
    "previousSha": previous_sha,
    "deployedAt": deployed_at,
    "reason": reason,
    "rollbackCommand": f"scripts/dispatch_deploy.sh rollback --smoke-cmd '{smoke_cmd}'",
    "smoke": {"cmd": smoke_cmd, "exitCode": int(smoke_rc),
              "outputTail": smoke_tail, "timeoutSec": int(smoke_timeout)},
}, indent=2, sort_keys=True) + "\n")
PY
  )
  atomic_write "$RECORD" "$record"

  STAGE="done"
  release_transaction_lock
  log "DEPLOYED dispatch-src to $target_sha"
  log "previous: $previous_sha"
  log "record:   $RECORD"
  if [[ "$JSON_MODE" == "1" ]]; then
    json_outcome 0 "done" "" "" "false" "no action: deploy complete" "$target_sha" "$previous_sha" "" ""
  fi
}

do_rollback() {
  local override_sha="$1" smoke_cmd="$2" reason="$3" rollback_sha
  [[ -n "$smoke_cmd" ]] || refuse "--smoke-cmd is required"
  if [[ -n "$override_sha" ]]; then
    is_forty_hex "$override_sha" || refuse "commit must be a 40-hex commit"
  fi
  acquire_transaction_lock
  if [[ -n "$override_sha" ]]; then
    rollback_sha="$override_sha"
  else
    rollback_sha=$(git -C "$TARGET" for-each-ref --format='%(objectname)' "$REF_ROLLBACK")
    [[ -n "$rollback_sha" ]] || refuse "no anchored rollback SHA and no --commit given ($REF_ROLLBACK is unset)"
  fi
  do_deploy "$rollback_sha" "$smoke_cmd" "$reason"
}

read_record() {
  [[ -f "$RECORD" ]] || refuse "no deployment record at $RECORD"
  python3 - "$RECORD" <<'PY' 2>/dev/null || refuse "deployment record is corrupt or missing required fields: $RECORD"
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
for field in ("schemaVersion", "deployedSha", "previousSha", "deployedAt"):
    if field not in data:
        raise SystemExit(1)
for field in ("deployedSha", "previousSha"):
    value = data[field]
    if not isinstance(value, str) or len(value) != 40:
        raise SystemExit(1)
print(data["deployedSha"])
print(data["previousSha"])
PY
}

do_status() {
  local record_output recorded_current recorded_rollback
  STAGE="status"
  # A refusal inside the capture still prints its JSON to stdout, which lands
  # in $record_output; re-emit it so --json stays a single object on stdout.
  record_output=$(read_record) || {
    if [[ -n "$record_output" ]]; then
      printf '%s\n' "$record_output"
    fi
    exit 2
  }
  recorded_current=$(printf '%s' "$record_output" | sed -n '1p')
  recorded_rollback=$(printf '%s' "$record_output" | sed -n '2p')

  local drift=0

  local live_head
  live_head=$(git -C "$TARGET" rev-parse HEAD 2>/dev/null) || refuse "target HEAD is not resolvable: $TARGET"

  local current_type rollback_type
  current_type=$(git -C "$TARGET" cat-file -t "$recorded_current" 2>/dev/null || true)
  rollback_type=$(git -C "$TARGET" cat-file -t "$recorded_rollback" 2>/dev/null || true)
  [[ "$current_type" == commit ]] || refuse "recorded deployedSha is no longer a reachable commit object (pin gone): $recorded_current"
  [[ "$rollback_type" == commit ]] || refuse "recorded previousSha is no longer a reachable commit object (pin gone): $recorded_rollback"

  local anchor_current anchor_rollback
  anchor_current=$(git -C "$TARGET" for-each-ref --format='%(objectname)' "$REF_CURRENT")
  anchor_rollback=$(git -C "$TARGET" for-each-ref --format='%(objectname)' "$REF_ROLLBACK")

  log "record:          $RECORD"
  log "recorded current: $recorded_current"
  log "recorded rollback: $recorded_rollback"
  log "live HEAD:        $live_head"
  log "anchor current:   ${anchor_current:-<unset>}"
  log "anchor rollback:  ${anchor_rollback:-<unset>}"

  if [[ "$live_head" != "$recorded_current" ]]; then
    log "DRIFT: live HEAD does not match the recorded deployedSha (stray checkout, pin itself is still reachable)"
    drift=1
  fi
  if [[ "$anchor_current" != "$recorded_current" ]]; then
    log "DRIFT: $REF_CURRENT does not match the recorded deployedSha"
    drift=1
  fi
  if [[ "$anchor_rollback" != "$recorded_rollback" ]]; then
    log "DRIFT: $REF_ROLLBACK does not match the recorded previousSha"
    drift=1
  fi

  if [[ "$drift" -eq 0 ]]; then
    log "OK: live HEAD, recorded state, and anchor refs agree"
  fi
  if [[ "$JSON_MODE" == "1" ]]; then
    if [[ "$drift" -eq 0 ]]; then
      json_outcome 0 "done" "" "" "false" "no action: live state matches the deployment record" "$recorded_current" "$recorded_rollback" "" ""
    else
      json_outcome 1 "done" "drift" "drift detected: live HEAD or anchor refs differ from the deployment record" "false" "inspect the live checkout, then redeploy to reconcile" "$recorded_current" "$recorded_rollback" "" ""
    fi
  fi
  exit "$drift"
}

emit_dry_run_plan() {
  local kind="$1" commit="$2" smoke="$3"
  if [[ "$kind" == "deploy" ]]; then
    log "DRY-RUN plan: deploy $commit"
  elif [[ -n "$commit" ]]; then
    log "DRY-RUN plan: rollback to explicit commit $commit"
  else
    log "DRY-RUN plan: rollback to implicit $REF_ROLLBACK (resolved under the transaction lock at run time)"
  fi
  log "  target:  $TARGET"
  if [[ -n "$commit" ]]; then
    log "  commit:  $commit"
  fi
  log "  smoke:   $smoke (deadline ${SMOKE_TIMEOUT}s; NOT executed)"
  log "  lock:    $LOCK_FILE (not acquired)"
  log "  writes:  none (no lock, worktree, ref or record changes)"
  if [[ "$JSON_MODE" == "1" ]]; then
    json_outcome 0 "plan" "" "" "false" "re-run without --dry-run to execute" "" "" "$commit" "true"
  fi
}

on_interrupt() {
  local sig="$1" cleanup_failed=0
  trap '' INT TERM
  [[ -n "${KILLER_PGID:-}" ]] && kill -KILL -- "-$KILLER_PGID" 2>/dev/null || true
  [[ -n "${KILLER_PID:-}" ]] && kill -KILL -- "$KILLER_PID" 2>/dev/null || true
  if [[ "${SMOKE_RUNNING:-0}" == "1" && -n "${SMOKE_PGID:-}" ]]; then
    kill -TERM -- "-$SMOKE_PGID" 2>/dev/null || true
    sleep 0.5 2>/dev/null || true
    kill -KILL -- "-$SMOKE_PGID" 2>/dev/null || true
    if [[ -n "${SMOKE_PID:-}" ]]; then
      wait "$SMOKE_PID" 2>/dev/null || true
    fi
    SMOKE_RUNNING=0
  fi
  remove_owned_worktree || cleanup_failed=1
  if [[ -n "${SMOKE_OUT:-}" ]]; then rm -f -- "$SMOKE_OUT" 2>/dev/null || true; SMOKE_OUT=""; fi
  if [[ -n "${SMOKE_FLAG:-}" ]]; then rm -f -- "$SMOKE_FLAG" 2>/dev/null || true; SMOKE_FLAG=""; fi
  if [[ "${MUTATING:-0}" == "1" ]]; then
    refuse "interrupted by $sig during live mutation; inspect HEAD, anchor refs and the deployment record before retrying"
  fi
  if [[ "$cleanup_failed" == "1" ]]; then
    refuse "interrupted by $sig; cleanup failed: owned smoke worktree could not be removed; inspect worktree registrations before retrying"
  fi
  refuse "interrupted by $sig; smoke stopped and owned worktree removed, live HEAD unchanged"
}

exit_cleanup() {
  [[ -n "${KILLER_PGID:-}" ]] && kill -KILL -- "-$KILLER_PGID" 2>/dev/null || true
  [[ -n "${KILLER_PID:-}" ]] && kill -KILL -- "$KILLER_PID" 2>/dev/null || true
  if [[ "${SMOKE_RUNNING:-0}" == "1" && -n "${SMOKE_PGID:-}" ]]; then
    kill -KILL -- "-$SMOKE_PGID" 2>/dev/null || true
  fi
  if [[ -n "${LOCK_FD:-}" ]]; then
    exec {LOCK_FD}>&- || true
  fi
  remove_owned_worktree >/dev/null 2>&1 || true
  [[ -n "${SMOKE_OUT:-}" ]] && rm -f -- "$SMOKE_OUT" 2>/dev/null || true
  [[ -n "${SMOKE_FLAG:-}" ]] && rm -f -- "$SMOKE_FLAG" 2>/dev/null || true
}

# ---- globals (traps may fire before the transaction starts) ----
JSON_MODE=0
DRY_RUN=0
STAGE="validate"
COMMAND="unknown"
SMOKE_TIMEOUT="$DEFAULT_SMOKE_TIMEOUT"
SMOKE_DIR=""; SMOKE_OUT=""; SMOKE_FLAG=""
SMOKE_PID=""; SMOKE_PGID=""; SMOKE_RUNNING=0
KILLER_PID=""; KILLER_PGID=""
SMOKE_RC=""; SMOKE_TIMED_OUT=0; SMOKE_OUTPUT=""; SMOKE_CLEANUP_FAILED=0
MUTATING=0
TARGET=""; RECORD=""; LOCK_FILE=""

trap 'on_interrupt SIGINT' INT
trap 'on_interrupt SIGTERM' TERM
trap exit_cleanup EXIT

SUBCOMMAND="${1:-}"
[[ -n "$SUBCOMMAND" ]] || { usage; exit 2; }
shift || true
COMMAND="$SUBCOMMAND"

COMMIT=""; COMMIT_GIVEN=0
SMOKE_CMD=""; SMOKE_GIVEN=0
SMOKE_TIMEOUT_RAW=""; TIMEOUT_GIVEN=0
JSON_MODE=0
DRY_RUN=0
REASON="dispatch_deploy.sh $SUBCOMMAND"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) [[ $# -ge 2 ]] || refuse "option --commit requires a value"; COMMIT="$2"; COMMIT_GIVEN=1; shift 2;;
    --smoke-cmd) [[ $# -ge 2 ]] || refuse "option --smoke-cmd requires a value"; SMOKE_CMD="$2"; SMOKE_GIVEN=1; shift 2;;
    --smoke-timeout) [[ $# -ge 2 ]] || refuse "option --smoke-timeout requires a value"; SMOKE_TIMEOUT_RAW="$2"; TIMEOUT_GIVEN=1; shift 2;;
    --reason) [[ $# -ge 2 ]] || refuse "option --reason requires a value"; REASON="$2"; shift 2;;
    --json) JSON_MODE=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    --help|-h) usage; exit 0;;
    *) refuse "unknown argument: $1";;
  esac
done

if [[ "$TIMEOUT_GIVEN" == "1" ]]; then
  if [[ "$SMOKE_TIMEOUT_RAW" =~ ^[0-9]+$ ]] && (( 10#$SMOKE_TIMEOUT_RAW >= SMOKE_TIMEOUT_MIN && 10#$SMOKE_TIMEOUT_RAW <= SMOKE_TIMEOUT_MAX )); then
    SMOKE_TIMEOUT="$SMOKE_TIMEOUT_RAW"
  else
    refuse "--smoke-timeout must be an integer 1..600 (got '$SMOKE_TIMEOUT_RAW')"
  fi
fi

case "$SUBCOMMAND" in
  --help|-h) usage; exit 0;;
esac

TEST_MODE="${DISPATCH_DEPLOY_TEST_MODE:-0}"
case "$TEST_MODE" in
  0)
    [[ -z "${DISPATCH_DEPLOY_TARGET:-}" ]] || refuse "DISPATCH_DEPLOY_TARGET is test-only"
    TARGET="$DEFAULT_TARGET"
    ;;
  1)
    TARGET="${DISPATCH_DEPLOY_TARGET:-}"
    [[ -n "$TARGET" ]] || refuse "DISPATCH_DEPLOY_TARGET must be set in test mode"
    ;;
  *) refuse "DISPATCH_DEPLOY_TEST_MODE must be 0 or 1";;
esac
RECORD="$TARGET.deployment.json"
LOCK_FILE="${TARGET}.dispatch-deploy.lock"

for command in git python3 mktemp mv date flock; do
  command -v "$command" >/dev/null || refuse "required command missing: $command"
done

[[ -d "$TARGET/.git" || -f "$TARGET/.git" ]] || refuse "target is not a git checkout: $TARGET"

case "$SUBCOMMAND" in
  deploy)
    [[ -n "$COMMIT" ]] || refuse "deploy requires --commit"
    is_forty_hex "$COMMIT" || refuse "commit must be a 40-hex commit"
    [[ -n "$SMOKE_CMD" ]] || refuse "--smoke-cmd is required"
    if [[ "$DRY_RUN" == "1" ]]; then
      emit_dry_run_plan deploy "$COMMIT" "$SMOKE_CMD"
      exit 0
    fi
    do_deploy "$COMMIT" "$SMOKE_CMD" "$REASON"
    ;;
  rollback)
    [[ -n "$SMOKE_CMD" ]] || refuse "--smoke-cmd is required"
    if [[ -n "$COMMIT" ]]; then
      is_forty_hex "$COMMIT" || refuse "commit must be a 40-hex commit"
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
      emit_dry_run_plan rollback "$COMMIT" "$SMOKE_CMD"
      exit 0
    fi
    do_rollback "$COMMIT" "$SMOKE_CMD" "$REASON"
    ;;
  status)
    if [[ "$COMMIT_GIVEN" == "1" || "$SMOKE_GIVEN" == "1" || "$TIMEOUT_GIVEN" == "1" || "$DRY_RUN" == "1" ]]; then
      refuse "status takes no --commit, --smoke-cmd, --smoke-timeout or --dry-run"
    fi
    do_status
    ;;
  *)
    refuse "unknown subcommand: $SUBCOMMAND"
    ;;
esac
