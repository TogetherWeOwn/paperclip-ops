#!/usr/bin/env bash
# ===========================================================================
# dlq_redrive.sh — standalone DLQ redrive CLI working against a local fixture
# file. No broker, no database, no network, no credential (case_c7b3c6ac).
# ---------------------------------------------------------------------------
# THE GAP THIS EXISTS FOR. S17 (case_821856da) defines a durable outbox / consumer /
# DLQ, but every operation against it needs the broker. There is no way to
# inspect, redrive or purge dead entries offline — against a captured fixture,
# in CI, or on a box that cannot reach the broker. This tool is that way: the
# whole DLQ is a JSONL file, one entry per line, and every verb below works on
# that file alone.
#
# FIXTURE FORMAT (one JSON object per line; blank lines ignored):
#   {"id":"m1","topic":"billing.invoiced","payload":{...},"error":"timeout",
#    "failed_at":"2026-09-20T04:00:00Z","attempts":5,"status":"dead"}
# `status` is one of dead | redriven | purged. Every other key is carried
# through untouched; `list` renders id, status, attempts, topic, failed_at,
# error and ignores the rest.
#
# THREE RULES THIS FILE ENCODES, EACH BECAUSE THE OBVIOUS VERSION IS WRONG.
#
# 1. EVERY MUTATION CARRIES A REASON, FROM A REGISTRY IN THIS FILE.
#    `org_request_queue.sh` requires a reason on every decision — approvals
#    included — because the record has to answer "why", not only "who". A DLQ
#    redrive that takes a free-text `--note` gets `--note "retry"` on every
#    invocation and the audit trail says nothing. So the reason is REQUIRED and
#    must be a member of the registry below, namespaced by action: a redrive
#    reason claims the entry deserves another attempt, a purge reason claims it
#    never will. `duplicate` as a redrive reason is refused, not reinterpreted.
#
# 2. PURGED IS TERMINAL.
#    A purged entry was deliberately deleted by an operator. Letting `redrive`
#    resurrect it turns purge into a suggestion. `redrive` on a purged id is
#    refused with cause `already_purged`; only `dead` entries redrive, and only
#    `dead` entries purge in bulk. A redriven entry purges only by explicit
#    `--id`: bulk purge never sweeps up entries another operator already
#    decided to retry.
#
# 3. A CHECK THAT MEASURED NOTHING MUST NOT EXIT GREEN.
#    Missing or unreadable fixture, or missing jq, is exit 5 (UNMEASURED) —
#    never 0. An existing fixture that legitimately holds zero entries lists
#    quietly at exit 0: the measurement succeeded, the set is empty. But
#    `--all` matching zero entries is exit 3 (`no_match`): a typo'd `--topic`
#    that silently does nothing is the same failure shape as the dormant
#    roster `queue_liveness.sh` was built to catch.
#
# EXIT CODES — distinct so a caller can tell the four apart.
#   0  ok
#   2  refused (bad usage, unknown reason, malformed fixture, duplicate id)
#   3  not eligible (id unknown, wrong state, --all matched nothing)
#   5  UNMEASURED: could not read the fixture or jq is absent. NOT green.
#
# MACHINE CONTRACT. Default stdout is stable TSV; stderr is human prose.
#   list              id \t status \t attempts \t topic \t failed_at \t error
#   redrive / purge   <new-status> \t id \t reason        (one line per entry)
#   any refusal       refused \t <cause> \t <detail>
# --json on list/redrive/purge emits exactly one JSON document, schema_version=1:
#   list:     {schema_version, entries:[{id,status,attempts,topic,failed_at,error}]}
#             Field values keep their JSON types; missing/null values become "".
#   mutation: {schema_version, outcomes:[{status,id,reason,preview}]}
#   refusal:  {schema_version, error:{cause,next_action}} (no fixture content).
# Causes and exit codes are identical in both formats; next_action is fixed per
# cause. JSON never includes payload or arbitrary extra fixture keys.
# Mutations rewrite the fixture atomically (temp file + mv under flock) and
# append one JSON record per entry to <fixture>.journal.jsonl — the audit
# trail, not stdout. --dry-run uses the same validation/selection but writes
# NOTHING (including no lock/temp files); preview=true marks every outcome.
# Without --json, previews add a leading `preview` TSV column; real TSV stays
# unchanged. A preview reads a snapshot, not a reservation against later writes.
# Synthetic: redrive --id m1 --reason transient_upstream --dry-run --json
#   {"schema_version":1,"outcomes":[{"status":"redriven","id":"m1",
#     "reason":"transient_upstream","preview":true}]}
# ===========================================================================
set -uo pipefail

ME="$(basename "${BASH_SOURCE[0]}")"

EXIT_OK=0; EXIT_REFUSED=2; EXIT_NOT_ELIGIBLE=3; EXIT_UNMEASURED=5

FIXTURE="${DLQ_FIXTURE:-./dlq.jsonl}"
ACTOR="${DLQ_ACTOR:-operator}"
JSON=0
ENTRIES=()

# Discover the output mode before parsing, so even an earlier invalid argument
# gets a JSON refusal. Flag values named --json are data, not output switches.
scan_output_mode() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) JSON=1; shift;;
      --fixture|--status|--topic|--reason|--id|--before)
        if [[ $# -ge 2 ]]; then shift 2; else shift; fi;;
      *) shift;;
    esac
  done
}
scan_output_mode "$@"

# --- reason registry ---------------------------------------------------------
# Namespaced by action. There is deliberately no --reason-text flag: see rule 1.
REDRIVE_REASONS="transient_upstream consumer_bugfix_deployed payload_corrected redeliver_anyway"
PURGE_REASONS="poison_unrecoverable duplicate expired_stale test_fixture"

reason_description() {
  case "$1" in
    transient_upstream)       printf 'the downstream that timed out is healthy again; retry is expected to succeed';;
    consumer_bugfix_deployed) printf 'the consumer bug that poisoned this entry is fixed and deployed';;
    payload_corrected)        printf 'the payload was corrected out of band since the failure';;
    redeliver_anyway)         printf 'operator override: retry despite none of the above holding';;
    poison_unrecoverable)     printf 'the entry can never succeed and would only poison the consumer again';;
    duplicate)                printf 'a duplicate of an entry that was already processed';;
    expired_stale)            printf 'too old for redelivery to mean anything';;
    test_fixture)             printf 'test data, not real traffic';;
    *)                        printf 'unknown reason';;
  esac
}

# --- output ------------------------------------------------------------------
human() { printf '%s: %s\n' "$ME" "$*" >&2; }

# Fixed vocabulary only: JSON refusals never interpolate untrusted details.
next_action() {
  case "$1" in
    bad_usage|invalid_status) printf 'check_usage';;
    reason_required|reason_not_for_action|unknown_reason) printf 'choose_registered_reason';;
    fixture_unreadable) printf 'provide_readable_fixture';;
    malformed_fixture|duplicate_id) printf 'repair_fixture';;
    already_purged|already_redriven|not_found|no_match) printf 'select_eligible_entries';;
    jq_missing) printf 'install_jq';;
  esac
}

# refuse <exit> <cause> <detail>: one machine verdict, prose on stderr.
refuse() {
  local exit="$1" cause="$2" detail="${3:-}"
  if [[ "$JSON" == "1" ]]; then
    # No jq dependency here: even jq_missing must be a parseable envelope.
    printf '{"schema_version":1,"error":{"cause":"%s","next_action":"%s"}}\n' \
      "$cause" "$(next_action "$cause")"
    human "$cause"
  else
    printf 'refused\t%s\t%s\n' "$cause" "$detail"
    human "$cause${detail:+ — $detail}"
  fi
  exit "$exit"
}

need_jq() {
  command -v jq >/dev/null 2>&1 || \
    refuse "$EXIT_UNMEASURED" "jq_missing" "jq is required"
}

need_fixture() {
  [[ -r "$FIXTURE" && -f "$FIXTURE" ]] || \
    refuse "$EXIT_UNMEASURED" "fixture_unreadable" "$FIXTURE"
}

usage() {
  cat <<'EOF'
dlq_redrive.sh — list, redrive and purge DLQ entries in a local JSONL fixture (case_c7b3c6ac)

  list    [--fixture F] [--status S] [--topic T] [--json]
  redrive (--id ID | --all [--topic T] [--before TS]) --reason CODE [--fixture F] [--json] [--dry-run]
  purge   (--id ID | --all [--topic T] [--before TS]) --reason CODE [--fixture F] [--json] [--dry-run]
  reasons

--json: one schema_version=1 document, with entries (list), outcomes (mutations),
        or error:{cause,next_action} (refusals). No payload fields are emitted.
--dry-run: redrive/purge preview only; identical rules, no fixture/journal/lock/tmp
           writes. JSON outcomes have preview=true; TSV adds a preview column.
           A preview is a snapshot, not a reservation. Real TSV is unchanged.
Example (synthetic id):
  dlq_redrive.sh redrive --id m1 --reason transient_upstream --dry-run --json
  {"schema_version":1,"outcomes":[{"status":"redriven","id":"m1","reason":"transient_upstream","preview":true}]}

Redrive reasons: transient_upstream, consumer_bugfix_deployed, payload_corrected, redeliver_anyway
Purge reasons:   poison_unrecoverable, duplicate, expired_stale, test_fixture

Environment:
  DLQ_FIXTURE   fixture file (default ./dlq.jsonl)
  DLQ_ACTOR     name recorded in the journal (default "operator")

Exits: 0 ok · 2 refused · 3 not eligible · 5 unmeasured (never green)
EOF
}

# --- fixture loading ---------------------------------------------------------
# Validate the whole snapshot into memory, so previews create no files at all.
# Runs in the CALLER's shell: a refusal must exit this process, not a subshell.
load_entries() {
  ENTRIES=()
  local lineno=0 line entry id status
  declare -A seen=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    entry="$(jq -c -s -e 'if length == 1 and (.[0] | type == "object") then .[0] else error("invalid entry") end' <<<"$line" 2>/dev/null)" || \
      refuse "$EXIT_REFUSED" "malformed_fixture" "$FIXTURE line $lineno is not JSON"
    id="$(jq -r '.id // ""' <<<"$entry")"
    status="$(jq -r '.status // ""' <<<"$entry")"
    [[ -n "$id" && -n "$status" ]] || \
      refuse "$EXIT_REFUSED" "malformed_fixture" "$FIXTURE line $lineno is missing id/status"
    case "$status" in
      dead|redriven|purged) ;;
      *) refuse "$EXIT_REFUSED" "malformed_fixture" \
           "$FIXTURE line $lineno has unknown status '$status'";;
    esac
    [[ -z "${seen[$id]:-}" ]] || \
      refuse "$EXIT_REFUSED" "duplicate_id" "id '$id' appears more than once"
    seen[$id]="$lineno"
    ENTRIES+=("$entry")
  done < "$FIXTURE"
}

entry_field() { jq -r "$2 // \"\"" <<<"$1"; }

# --- list --------------------------------------------------------------------
# need_arg <flag> <remaining...>: flags taking a value must HAVE one. Without
# this, a trailing bare `--reason` dies on `set -u` with an uncontracted
# exit 1 instead of the refused exit 2 the contract promises.
need_arg() {
  [[ $# -ge 2 && -n "${2:-}" ]] || \
    refuse "$EXIT_REFUSED" "bad_usage" "flag '$1' needs a value"
}

cmd_list() {
  local status_filter="" topic_filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --fixture) need_arg "$@"; FIXTURE="$2"; shift 2;;
      --status)  need_arg "$@"; status_filter="$2"; shift 2;;
      --topic)   need_arg "$@"; topic_filter="$2"; shift 2;;
      --json)    shift;;
      -h|--help) usage; exit "$EXIT_OK";;
      *) refuse "$EXIT_REFUSED" "bad_usage" "list: unknown flag '$1'";;
    esac
  done
  need_jq; need_fixture
  if [[ -n "$status_filter" ]]; then
    case "$status_filter" in
      dead|redriven|purged) ;;
      *) refuse "$EXIT_REFUSED" "invalid_status" \
           "status filter '$status_filter' is not dead|redriven|purged";;
    esac
  fi
  load_entries
  local entry id status attempts topic failed_at error
  local -a json_entries=()
  for entry in "${ENTRIES[@]}"; do
    id="$(entry_field "$entry" '.id')"
    status="$(entry_field "$entry" '.status')"
    [[ -z "$status_filter" || "$status" == "$status_filter" ]] || continue
    topic="$(entry_field "$entry" '.topic')"
    [[ -z "$topic_filter" || "$topic" == "$topic_filter" ]] || continue
    attempts="$(entry_field "$entry" '.attempts')"
    failed_at="$(entry_field "$entry" '.failed_at')"
    error="$(entry_field "$entry" '.error')"
    if [[ "$JSON" == "1" ]]; then
      json_entries+=("$(jq -c '{id:(.id // ""),status:(.status // ""),
        attempts:(.attempts // ""),topic:(.topic // ""),
        failed_at:(.failed_at // ""),error:(.error // "")}' <<<"$entry")")
    else
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$id" "$status" "$attempts" "$topic" "$failed_at" "$error"
    fi
  done
  if [[ "$JSON" == "1" ]]; then
    printf '%s\n' "${json_entries[@]}" | jq -cs '{schema_version:1,entries:.}'
  fi
}

# --- reasons -----------------------------------------------------------------
cmd_reasons() {
  [[ $# -eq 0 ]] || refuse "$EXIT_REFUSED" "bad_usage" "reasons takes no flags"
  local r
  for r in $REDRIVE_REASONS; do printf 'redrive\t%s\t%s\n' "$r" "$(reason_description "$r")"; done
  for r in $PURGE_REASONS;   do printf 'purge\t%s\t%s\n'   "$r" "$(reason_description "$r")"; done
}

# --- mutations ---------------------------------------------------------------
# check_reason <action> <reason>: registry membership, namespaced by action.
check_reason() {
  local action="$1" reason="$2" allowed r
  [[ -n "$reason" ]] || refuse "$EXIT_REFUSED" "reason_required" \
    "$action requires --reason (see '$ME reasons')"
  if [[ "$action" == "redrive" ]]; then allowed="$REDRIVE_REASONS"; else allowed="$PURGE_REASONS"; fi
  for r in $allowed; do [[ "$r" == "$reason" ]] && return 0; done
  for r in $REDRIVE_REASONS $PURGE_REASONS; do
    [[ "$r" == "$reason" ]] && refuse "$EXIT_REFUSED" "reason_not_for_action" \
      "'$reason' is a reason for the other action, not for $action"
  done
  refuse "$EXIT_REFUSED" "unknown_reason" \
    "'$reason' is not a registered $action reason (see '$ME reasons')"
}

# mutate <action> <reason> <selector...> — shared redrive/purge engine.
# Selector: either "--id ID" or "--all [--topic T] [--before TS]".
mutate() {
  local action="$1"; shift
  local reason="" sel_id="" sel_all=0 topic_filter="" before="" dry_run=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --reason)  need_arg "$@"; reason="$2"; shift 2;;
      --id)      need_arg "$@"; sel_id="$2"; shift 2;;
      --all)     sel_all=1; shift;;
      --topic)   need_arg "$@"; topic_filter="$2"; shift 2;;
      --before)  need_arg "$@"; before="$2"; shift 2;;
      --fixture) need_arg "$@"; FIXTURE="$2"; shift 2;;
      --json)    shift;;
      --dry-run) dry_run=1; shift;;
      -h|--help) usage; exit "$EXIT_OK";;
      *) refuse "$EXIT_REFUSED" "bad_usage" "$action: unknown flag '$1'";;
    esac
  done
  need_jq; need_fixture
  check_reason "$action" "$reason"
  if [[ -n "$sel_id" && "$sel_all" == "1" ]]; then
    refuse "$EXIT_REFUSED" "bad_usage" "$action: --id and --all are mutually exclusive"
  fi
  if [[ -z "$sel_id" && "$sel_all" == "0" ]]; then
    refuse "$EXIT_REFUSED" "bad_usage" "$action: give --id ID or --all"
  fi
  if [[ -n "$sel_id" && (-n "$topic_filter" || -n "$before") ]]; then
    refuse "$EXIT_REFUSED" "bad_usage" "$action: --topic/--before only filter --all"
  fi

  local new_status reason_key
  if [[ "$action" == "redrive" ]]; then new_status="redriven"; reason_key="redrive_reason";
  else new_status="purged"; reason_key="purge_reason"; fi
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  local lock="$FIXTURE.lock" tmp="" journal
  journal="$FIXTURE.journal.jsonl"
  # Real writes retain the same lock/atomic-replace contract. Previews never
  # open a lock or create a temp; cleanup is a no-op and validation is in memory.
  cleanup() { if [[ -n "$tmp" ]]; then rm -f "$tmp"; fi; }
  if [[ "$dry_run" == "0" ]]; then
    tmp="$(mktemp "$(dirname "$FIXTURE")/.dlq_mutate.XXXXXX")"
    trap cleanup EXIT
    exec 9>"$lock"
    flock 9
  fi
  load_entries

  local entry id status topic failed_at matched=0
  local -a matched_ids=() outcomes=()
  for entry in "${ENTRIES[@]}"; do
    id="$(entry_field "$entry" '.id')"
    status="$(entry_field "$entry" '.status')"
    local take=0
    if [[ -n "$sel_id" ]]; then
      [[ "$id" == "$sel_id" ]] && take=1
    else
      # Bulk: only dead entries are swept; redriven ones need an explicit --id.
      # Non-matching entries are CARRIED THROUGH below, never dropped: every
      # line read is appended to the rewrite, taken or not.
      if [[ "$status" == "dead" ]]; then
        take=1
        topic="$(entry_field "$entry" '.topic')"
        [[ -z "$topic_filter" || "$topic" == "$topic_filter" ]] || take=0
        if [[ "$take" == "1" && -n "$before" ]]; then
          failed_at="$(entry_field "$entry" '.failed_at')"
          [[ -n "$failed_at" && "$failed_at" < "$before" ]] || take=0
        fi
      fi
    fi
    if [[ "$take" == "1" ]]; then
      if [[ -n "$sel_id" ]]; then
        # Single-id eligibility, pinned per state (rule 2: purged is terminal).
        if [[ "$action" == "redrive" ]]; then
          case "$status" in
            dead) ;;
            redriven) cleanup; refuse "$EXIT_NOT_ELIGIBLE" "already_redriven" "id '$id' is already redriven";;
            purged)   cleanup; refuse "$EXIT_NOT_ELIGIBLE" "already_purged" "id '$id' is purged; purged is terminal";;
          esac
        else
          case "$status" in
            dead|redriven) ;;
            purged) cleanup; refuse "$EXIT_NOT_ELIGIBLE" "already_purged" "id '$id' is already purged";;
          esac
        fi
      fi
      if [[ "$dry_run" == "0" ]]; then
        entry="$(jq -c --arg st "$new_status" --arg rk "$reason_key" --arg rs "$reason" \
          --arg ts "$ts" --arg actor "$ACTOR" \
          '.status=$st | .[$rk]=$rs | .decided_at=$ts | .decided_by=$actor' <<<"$entry")"
      fi
      matched_ids+=("$id")
      matched=$((matched + 1))
    fi
    if [[ "$dry_run" == "0" ]]; then printf '%s\n' "$entry" >> "$tmp"; fi
  done

  if [[ -n "$sel_id" && "$matched" == "0" ]]; then
    cleanup
    refuse "$EXIT_NOT_ELIGIBLE" "not_found" "id '$sel_id' is not in $FIXTURE"
  fi
  if [[ "$matched" == "0" ]]; then
    cleanup
    refuse "$EXIT_NOT_ELIGIBLE" "no_match" "no dead entries matched the filter"
  fi

  if [[ "$dry_run" == "0" ]]; then
    mv "$tmp" "$FIXTURE"
    trap - EXIT
  fi
  local mid preview=false
  [[ "$dry_run" == "1" ]] && preview=true
  for mid in "${matched_ids[@]}"; do
    if [[ "$dry_run" == "0" ]]; then
      jq -c -n --arg ts "$ts" --arg actor "$ACTOR" --arg act "$action" \
        --arg id "$mid" --arg rs "$reason" \
        '{ts:$ts,actor:$actor,action:$act,id:$id,reason:$rs}' >> "$journal"
    fi
    if [[ "$JSON" == "1" ]]; then
      outcomes+=("$(jq -cn --arg st "$new_status" --arg id "$mid" \
        --arg rs "$reason" --argjson preview "$preview" \
        '{status:$st,id:$id,reason:$rs,preview:$preview}')")
    else
      [[ "$dry_run" == "1" ]] && printf 'preview\t'
      printf '%s\t%s\t%s\n' "$new_status" "$mid" "$reason"
    fi
  done
  if [[ "$JSON" == "1" ]]; then
    printf '%s\n' "${outcomes[@]}" | jq -cs '{schema_version:1,outcomes:.}'
  fi
  human "$action: $matched entr$( [[ "$matched" == "1" ]] && printf 'y' || printf 'ies' ) ($reason), preview=$preview"
  if [[ "$dry_run" == "0" ]]; then exec 9>&-; fi
}

case "${1:-}" in
  list)    shift; cmd_list "$@";;
  redrive) shift; mutate redrive "$@";;
  purge)   shift; mutate purge "$@";;
  reasons) shift; cmd_reasons "$@";;
  -h|--help|help|'') usage; exit "$EXIT_OK";;
  *) usage >&2
     refuse "$EXIT_REFUSED" "bad_usage" "unknown subcommand";;
esac
