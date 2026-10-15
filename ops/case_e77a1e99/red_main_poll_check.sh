#!/usr/bin/env bash
# ===========================================================================
# red_main_poll_check.sh — host-cron wrapper (case_e77a1e99) for red_main_poll.sh.
#
# Runs unattended under systemd --user with NO agent-run context: no
# $PAPERCLIP_API_KEY (a run-scoped JWT that dies with the heartbeat that
# minted it). Auth comes from a durable AGENT API key supplied via
# EnvironmentFile as PAPERCLIP_AGENT_API_KEY (see ops/case_e77a1e99/INSTALL.md
# §1). Same pattern as ops/case_4ea7d92e/*_check.sh.
#
# READ-ONLY TIMER. This wrapper never writes to the board: the deployed API
# rejects every runless agent comment write with HTTP 403
# (cross_issue_influence_run_context_required — live 2026-10-05 and
# 2026-10-06), so a timer tick that POSTs cannot deliver, ever. Findings are
# recorded as proposal files under RED_MAIN_STATE_DIR/proposals/ and printed
# to stdout (the unit journal), at a bounded cadence. Board delivery belongs
# to a RUNFUL routine — the CEO hourly watchdog routine, which files the
# incident cards from the handoff and posts `filed:` mirrors with its run JWT
# through the same comment-only helper. See ops/case_e77a1e99/INSTALL.md §0.
#
# WHAT IT RECORDS, EXACTLY. It never creates an INCIDENT card
# (`red-main:v1:…` is the CEO routine's to file) and never touches one. It
# never creates ANY card and never posts ANY comment. One proposal per
# finding, first line TAG TITLE:
#
#   [red-main-poll]        drafts for the operator (an untracked red)
#   [red-main-poll-gap]    coverage gap: a repo or the board was unobservable
#   [red-main-poll-error]  the poller refused or errored
#
# Three tags, so a gap never rides on a draft proposal (and a filed draft
# never swallows an outstanding gap).
#
# THE HANDOFF. Every tick also rewrites RED_MAIN_HANDOFF_DIR/red-mains.json
# (watchdog.red-main-handoff.v1): the poller's snapshot entries and exit code,
# stamped with measuredAt. The CEO routine reads that one file through the
# offline builder (watchdog/snapshot_builder.py --red-mains-handoff). It is a
# local file write, never a board write, and it is the only path from this host
# to the routine.
# See docs/red-main-poll.md and the decision in docs/decisions/.
#
# The bridge key is bound to the rollup
# parent, which is exactly the key's whole reach; the poller's bounded board
# read (parent plus comments) still runs every tick, so a routine-posted
# `filed:` mirror silences the next draft the same way a posted draft would.
#
# CADENCE IS BOUNDED. An unbounded timer would rewrite the same proposal
# every tick for as long as a red persists. Per finding kind this wrapper
# therefore keeps a state file (digest of what the finding says plus the
# time it was recorded) and records:
#   * nothing while the digest is unchanged and RED_MAIN_REPOST_SECONDS
#     (default 86400, one day) has not passed;
#   * a changed digest only after RED_MAIN_CHANGE_FLOOR_SECONDS (default 3600)
#     since the last record of that kind.
# The digest of a draft is the SET OF DEDUPE KEYS in it, not its text: the
# head SHA in the body moves with every commit while the incident is the same.
# A clean tick (poller exit 0: green, or every red already tracked) clears the
# state, so a red that goes green and red again is recorded afresh. Every
# finding tick sweeps the proposals and stamps of kinds it did not see, so
# a stale error/gap file never outlives the tick that stopped seeing it.
# State lives in RED_MAIN_STATE_DIR (default ~/.local/state/paperclip-case_1a1b67a4).
#
# EXIT CONTRACT. 0: clean tick, nothing outstanding (proposals cleared).
# 1: one or more proposals are outstanding — the unit is FAILED so the red
# is visible in the unit state, not a clean tick. Delivery itself is the
# CEO routine's: a failed unit here means "look at the proposals", never a
# phantom board write. A silent 403 is the one outcome this must never have,
# and with no POST path there is no POST to be refused.
# ===========================================================================
set -Eeuo pipefail

# The poller and CI reader are generic code whose source of truth is the
# PUBLIC toolkit clone at TOOLKIT_DIR (pinned per INSTALL.md §3). This
# wrapper, the units and the install runbook stay here because they carry
# host paths and install scope. The toolkit scripts must come from that
# clone -- never from a stale copy of them elsewhere.
TOOLKIT_DIR="${RED_MAIN_TOOLKIT_DIR:-$HOME/paperclip-operator-toolkit}"

POLL="${TOOLKIT_DIR}/red_main_poll.sh"
TAG_DRAFT="[red-main-poll]"
TAG_GAP="[red-main-poll-gap]"
TAG_ERROR="[red-main-poll-error]"
: "${case_1a1b67a4_REPOS:?case_1a1b67a4_REPOS must be set (EnvironmentFile) -- see ops/case_e77a1e99/INSTALL.md §2}"
: "${RED_MAIN_ROLLUP_PARENT_ID:?RED_MAIN_ROLLUP_PARENT_ID must be set (EnvironmentFile) -- the rollup card this bridge key is bound to, see §1}"
: "${RED_MAIN_HANDOFF_DIR:?RED_MAIN_HANDOFF_DIR must be set (EnvironmentFile) -- a directory under ~/.local/share/paperclip, see ops/case_e77a1e99/INSTALL.md §0}"

STATE_DIR="${RED_MAIN_STATE_DIR:-${XDG_STATE_HOME:-${HOME:?HOME or RED_MAIN_STATE_DIR must be set}/.local/state}/paperclip-case_1a1b67a4}"
PROPOSAL_DIR="${STATE_DIR}/proposals"
REPOST_SECONDS="${RED_MAIN_REPOST_SECONDS:-86400}"
CHANGE_FLOOR_SECONDS="${RED_MAIN_CHANGE_FLOOR_SECONDS:-3600}"
[[ "$REPOST_SECONDS" =~ ^[0-9]+$ && "$CHANGE_FLOOR_SECONDS" =~ ^[0-9]+$ ]] \
  || { echo "red_main_poll_check: RED_MAIN_REPOST_SECONDS and RED_MAIN_CHANGE_FLOOR_SECONDS must be whole seconds" >&2; exit 1; }

# Only the API URL and the credential and parent bindings are exported to the
# toolkit poller. The bounded branch is forced by RED_MAIN_ROLLUP_PARENT_ID
# (required above), so a company id left in the environment is never read.
export PAPERCLIP_API_URL
export PAPERCLIP_AGENT_API_KEY
export RED_MAIN_API_KEY="${PAPERCLIP_AGENT_API_KEY:?PAPERCLIP_AGENT_API_KEY must be set (EnvironmentFile)}"
export GH_CI_STATUS_BIN="${TOOLKIT_DIR}/gh_ci_status.sh"
# The bounded board read needs the rollup parent: the poller reads the bound
# thread (parent plus comments) instead of the company list. No writer is
# sourced and no comment alias is exported: this timer has no POST path by
# construction, so the run gate has nothing to refuse.
export RED_MAIN_ROLLUP_PARENT_ID
# INCIDENT_SOURCE_CMD unset: the poller reads the board directly.
export GH_TOKEN
GH_TOKEN="$(${GH_TOKEN_CMD:?GH_TOKEN_CMD must be set (EnvironmentFile)})"
[[ -n "$GH_TOKEN" ]] || { echo "red_main_poll_check: GH_TOKEN_CMD printed nothing" >&2; exit 1; }

# shellcheck disable=SC2206
REPOS=( $case_1a1b67a4_REPOS )
[[ "${#REPOS[@]}" -gt 0 ]] || { echo "red_main_poll_check: case_1a1b67a4_REPOS is empty" >&2; exit 1; }
# A repo listed twice would yield two identical rows, so two cards.
mapfile -t REPOS < <(printf '%s\n' "${REPOS[@]}" | awk '!seen[$0]++')

(umask 077; mkdir -p "$STATE_DIR" "$PROPOSAL_DIR")
(umask 022; mkdir -p "$RED_MAIN_HANDOFF_DIR") \
  || { echo "red_main_poll_check: cannot create RED_MAIN_HANDOFF_DIR" >&2; exit 1; }

body_file="$(mktemp)"
err_file="$(mktemp)"
trap 'rm -f "$body_file" "$err_file"' EXIT

# RECORDED counts the proposals recorded this tick: a non-zero count fails
# the unit (exit 1) so an outstanding finding is never a clean tick.
# SEEN_KINDS lists the finding kinds observed this tick (recorded or held):
# kinds with no finding are swept below, so a stale error/gap proposal from
# an earlier tick never outlives the tick that stopped seeing it.
RECORDED=0
SEEN_KINDS=""

# record_if_due <kind> <tag> <title> <digest>
# Records $body_file as a proposal file unless the cadence bound says the last
# record of this kind already said the same thing recently. A recorded
# proposal is printed to stdout (the unit journal) for the operator; a held
# one is noted on stderr.
record_if_due() {
  local kind="$1" tag="$2" title="$3" digest="$4"
  local state="${STATE_DIR}/${kind}.last" now last_ts="" last_digest="" age
  SEEN_KINDS+=" ${kind}"
  now="$(date +%s)"
  if [[ -r "$state" ]]; then
    read -r last_ts last_digest < "$state" || true
    if [[ "$last_ts" =~ ^[0-9]+$ ]]; then
      age=$(( now - last_ts ))
      if [[ "$age" -ge 0 ]]; then
        if [[ "$last_digest" == "$digest" && "$age" -lt "$REPOST_SECONDS" ]]; then
          echo "red_main_poll_check: ${kind} unchanged since ${age}s ago (repost every ${REPOST_SECONDS}s); not recording" >&2
          RECORDED=$((RECORDED+1))
          return 0
        fi
        if [[ "$last_digest" != "$digest" && "$age" -lt "$CHANGE_FLOOR_SECONDS" ]]; then
          echo "red_main_poll_check: ${kind} changed but last record was ${age}s ago (floor ${CHANGE_FLOOR_SECONDS}s); not recording" >&2
          RECORDED=$((RECORDED+1))
          return 0
        fi
      fi
    fi
  fi
  rm -f "${PROPOSAL_DIR}/${kind}-"*.md
  {
    printf '# %s %s\n' "$tag" "$title"
    printf 'recorded: %s kind: %s digest: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$kind" "$digest"
    cat "$body_file"
  } > "${PROPOSAL_DIR}/${kind}-${digest}.md"
  printf '%s %s\n' "$now" "$digest" > "$state"
  printf '%s %s\n\n' "$tag" "$title"
  cat "$body_file"
  RECORDED=$((RECORDED+1))
}

# body_digest -> sha1 of the whole body: right for findings whose text IS the
# finding (a gap lists which repos were blind; an error quotes the refusal).
body_digest() { sha1sum < "$body_file" | cut -c1-40; }

# drafts_digest -> sha1 over the SET of dedupe keys in the drafts. The text
# carries the head SHA, which changes with every commit on a red main; the set
# of (repo, signature) keys is what decides whether a proposal is re-recorded.
drafts_digest() {
  local keys
  keys="$(grep -oE 'red-main:v1:[^] `]+' "$body_file" | LC_ALL=C sort -u || true)"
  if [[ -n "$keys" ]]; then printf '%s\n' "$keys" | sha1sum | cut -c1-40; else body_digest; fi
}

# record_gap -> a coverage-gap finding built from the poller's stderr.
record_gap() {
  {
    printf 'red-main poll could not fully observe the repo set.\n\n'
    printf 'Unobservable polling is not green main. Resolve before trusting silence.\n\n'
    printf '```\n%s\n```\n' "$err"
  } > "$body_file"
  record_if_due gap "$TAG_GAP" "red-main poll coverage gap" "$(body_digest)"
}

# write_handoff: rewrites the one envelope the CEO routine reads
# (watchdog.red-main-handoff.v1). Every tick rewrites it, so measuredAt is the
# freshness signal; the builder treats a stale one as unknown.
write_handoff() {
  local snap_out snap_rc=0 measured_at rows tmp
  snap_out="$(mktemp)" || return 1
  measured_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  "$POLL" snapshot "${REPOS[@]}" >"$snap_out" 2>/dev/null || snap_rc=$?
  if jq -e '.redMains | type == "array"' "$snap_out" >/dev/null 2>&1; then
    rows="$(jq -c '.redMains' "$snap_out")"
  else
    # No array means nothing was measured: unknown, never a clean tick.
    rows='[]'
    snap_rc=3
  fi
  rm -f "$snap_out"
  tmp="$(mktemp "${RED_MAIN_HANDOFF_DIR}/.red-mains.XXXXXX")" || return 1
  if ! jq -cn --arg at "$measured_at" --argjson rc "$snap_rc" \
      --argjson rows "$rows" \
      '{schema: "watchdog.red-main-handoff.v1", measuredAt: $at, exitCode: $rc,
        redMains: $rows}' > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 0644 "$tmp" && mv -f "$tmp" "${RED_MAIN_HANDOFF_DIR}/red-mains.json" \
    || { rm -f "$tmp"; return 1; }
}

rc=0
out="$("$POLL" propose "${REPOS[@]}" 2>"$err_file")" || rc=$?
err="$(cat "$err_file")"
write_handoff \
  || { echo "red_main_poll_check: cannot write the handoff envelope under RED_MAIN_HANDOFF_DIR" >&2; exit 1; }

case "$rc" in
  0)
    # Fully observed, nothing untracked. Green or fully-tracked: silent, and
    # the cadence state and outstanding proposals are cleared so the next red
    # is recorded afresh.
    rm -f "${STATE_DIR}"/*.last "${PROPOSAL_DIR}"/*.md
    exit 0
    ;;
  1)
    # Untracked red(s): drafts for the operator. The poller's exit 1 means
    # drafts dominate, so a blind repo beside them rides only on stderr: it is
    # recorded as its own gap finding, never dropped behind the drafts.
    printf '%s\n' "$out" > "$body_file"
    record_if_due draft "$TAG_DRAFT" "red-main drafts for the operator" "$(drafts_digest)"
    if grep -q '^UNKNOWN ' <<<"$err"; then record_gap; fi
    ;;
  3)
    # Blind spot: repos unobservable or board unreadable. A finding, not clean.
    record_gap
    ;;
  *)
    {
      printf 'red-main poller refused or errored (exit %s).\n\n' "$rc"
      printf '```\n%s\n%s\n```\n' "$err" "$out"
    } > "$body_file"
    record_if_due error "$TAG_ERROR" "red-main poller error" "$(body_digest)"
    ;;
esac

# Sweep kinds this tick did not see: a stale proposal (or cadence stamp)
# from an earlier tick must not outlive the tick that stopped seeing it —
# e.g. an error file lingering while only drafts recur. Held kinds count
# as seen (the finding persists; only its re-record was deferred).
for _sweep_kind in draft gap error; do
  if [[ "$SEEN_KINDS" != *" ${_sweep_kind}"* ]]; then
    rm -f "${STATE_DIR}/${_sweep_kind}.last" "${PROPOSAL_DIR}/${_sweep_kind}-"*.md
  fi
done

# Proposals outstanding: fail the unit so the red is visible in the unit
# state. (RECORDED is always non-zero here: every branch above records.)
exit 1  # outstanding-findings: never exit clean with proposals on disk
