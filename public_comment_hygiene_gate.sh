#!/usr/bin/env bash
# ===========================================================================
# Public-comment hygiene gate.  case_99462e9c (child of case_d5ca14da).
#
# THE FAILURE THIS STOPS. Review automation posted issue comments carrying
# internal card IDs onto PUBLIC repos (two-bot-next#398: six comments citing
# case_fa4fcb64 plus one citing case_207ab624; two-bot-next#129: one comment citing
# case_da9d73c5 and case_96a40721), violating GITHUB_STANDARD_20261003: no TOG-/PAP-
# IDs in any public title, body, comment or branch name. The in-repo `pr-lint`
# check (`.github/scripts/pr_standards.py`) enforces that rule for PR titles
# and bodies only -- comment text goes through no gate at all, which is the
# hole these eight comments fell through.
#
# WHAT THIS IS. An offline pre-post check: hand it the exact text you are
# about to post (and, for PR work, the branch name) plus the repo visibility,
# and it fails when a public post would carry an internal ID. Private repos
# always pass -- IDs belong there (`Refs: case_1cab5f7f`). Run it before every
# `gh api .../comments` POST/PATCH, every review-submit body, and every push
# of a branch destined for a public repo. It never touches the network and
# never needs a credential, so there is no reason to skip it.
#
# WHAT THIS IS NOT. It cannot scrub what is already posted (that needs a
# GitHub-admin write; see the case_99462e9c Operator card), and it does not scan
# live threads -- re-scanning posted threads is a `gh api ... | grep` one-
# liner recorded on the card, not this script's job.
#
# Prefix set matches `pr-lint` (`INTERNAL_ID_PREFIXES`, default TOG|PAP|PAPA)
# so the two gates never disagree about what an "internal ID" is.
#
# Exit codes:
#   0  PASS     -- safe to post (private repo, or public with no internal IDs).
#   1  VIOLATION-- public text carries at least one internal ID. Do not post.
#   2  usage / internal error (missing file, bad visibility, unreadable or
#        non-regular input, or a scanner failure). Never report PASS after a
#        scan failure: grep exit 1 (no match) is the only clean signal.
#
# JSON contract (--json; requires jq): one document on stdout, no text output.
# --help always prints text help instead of scanning, even with --json.
# An omitted visibility is echoed as "" on usage errors (context defaults).
#   {"schema_version":1,"verdict":"pass|violation|error",
#    "visibility":"<argument>","context":"<label>",
#    "findings":[{"source":"body","line":2,"matched":["<ID>"]},
#                {"source":"branch","matched":["<ID>"]}],
#    "truncated":0,
#    "error":{"code":"ok","retryable":false,"next_action":"post"}}
# Findings contain one matched ID each, in scan order (body, then branch),
# capped at 50 total. truncated counts omitted ID occurrences, including repeats.
# Neither body text nor the branch name is included. Private repos skip scans.
# Errors use verdict error (exit 2), empty findings and a stable error.code:
# unknown_argument, missing_value, invalid_visibility, missing_body_file,
# body_not_readable, body_not_regular, body_scan_failed, branch_scan_failed,
# json_encoding_failed (jq unavailable/failed; metadata is escaped by Bash).
# next_action is post, redact_ids, fix_arguments, fix_body_file, fix_scanner
# or fix_serializer; retryable is always false (correct input/tool to retry).
# A binary match without line/ID data is body_scan_failed, never a PASS.
# Synthetic example (a body ID on line 2):
#   {"schema_version":1,"verdict":"violation","visibility":"public",
#    "context":"review","findings":[{"source":"body","line":2,
#    "matched":["EXAMPLE-123"]}],"truncated":0,
#    "error":{"code":"ok","retryable":false,"next_action":"redact_ids"}}
# ===========================================================================

set -uo pipefail

VISIBILITY=""
BODY_FILE=""
BRANCH=""
CONTEXT="comment"
JSON=0
FINDINGS=()
FINDING_CAP=50
TOTAL_HITS=0
TRUNCATED=0

# Detect the output mode and metadata even when a usage error precedes them.
# Values of other options are not flags (--context --json is just a label).
ARGS=("$@")
for ((i=0; i<${#ARGS[@]}; i++)); do
  case "${ARGS[i]}" in
    --visibility) VISIBILITY="${ARGS[i+1]:-}"; i=$((i+1)) ;;
    --visibility=*) VISIBILITY="${ARGS[i]#*=}" ;;
    --context) CONTEXT="${ARGS[i+1]:-}"; i=$((i+1)) ;;
    --context=*) CONTEXT="${ARGS[i]#*=}" ;;
    --body-file|--branch) i=$((i+1)) ;;
    --json) JSON=1 ;;
  esac
done

# The error envelope must remain usable even when jq itself is unavailable.
# Encode only the two caller-provided metadata strings with Bash in that path.
json_string() {
  local value="$1" char escaped i LC_ALL=C
  printf '"'
  for ((i=0; i<${#value}; i++)); do
    char="${value:i:1}"
    case "$char" in
      \"|\\) printf '\\%s' "$char" ;;
      [[:cntrl:]]) printf -v escaped '\\u%04x' "'$char"; printf '%s' "$escaped" ;;
      *) printf '%s' "$char" ;;
    esac
  done
  printf '"'
}

emit_json() {
  local verdict="$1" code="$2" action="$3" document
  # Each retained hit is a source/line/ID triple. jq escapes argument values;
  # no raw body text or branch name ever reaches the serializer.
  if ! document="$(jq -cn --arg verdict "$verdict" --arg visibility "$VISIBILITY" \
    --arg context "$CONTEXT" --arg code "$code" --arg action "$action" \
    --argjson truncated "$TRUNCATED" --args '
      {schema_version: 1, verdict: $verdict, visibility: $visibility,
       context: $context,
       findings: [$ARGS.positional as $hits | range(0; $hits | length; 3) as $i |
         {source: $hits[$i], matched: [$hits[$i+2]]} +
         (if $hits[$i] == "body" then {line: ($hits[$i+1] | tonumber)} else {} end)],
       truncated: $truncated,
       error: {code: $code, retryable: false, next_action: $action}}
    ' -- "${FINDINGS[@]+"${FINDINGS[@]}"}" 2>/dev/null)"; then
    # Never expose tool diagnostics, and retain metadata without needing jq.
    printf '{"schema_version":1,"verdict":"error","visibility":'
    json_string "$VISIBILITY"
    printf ',"context":'
    json_string "$CONTEXT"
    printf '%s\n' ',"findings":[],"truncated":0,"error":{"code":"json_encoding_failed","retryable":false,"next_action":"fix_serializer"}}'
    exit 2
  fi
  printf '%s\n' "$document"
}

fail() {
  local code="$1" action="$2" message="$3"
  if (( JSON )); then
    FINDINGS=(); TRUNCATED=0
    emit_json error "$code" "$action"
  else
    echo "$message" >&2
  fi
  exit 2
}

record_hit() {
  TOTAL_HITS=$((TOTAL_HITS+1))
  if (( TOTAL_HITS <= FINDING_CAP )); then
    FINDINGS+=("$1" "$2" "$3")
  else
    TRUNCATED=$((TRUNCATED+1))
  fi
}

usage() {
  cat <<'EOF'
Usage: public_comment_hygiene_gate.sh --visibility public|private --body-file FILE [--branch NAME] [--context LABEL] [--json]

  Fails when text destined for a PUBLIC repo carries an internal card ID
  (case_1cab5f7f, case_05ee029d, ...). Private repos always pass.

  --visibility   public|private (required)
  --body-file    file holding the exact text to be posted (required)
  --branch       branch the work will ship on; checked too when public
  --context      label used in failure lines (default: comment)
  --json         one JSON document on stdout instead of text (requires jq)

JSON schema_version 1: verdict pass|violation|error, visibility, context,
findings [{source: body|branch, line: body-only integer, matched: [ID]}],
truncated (omitted hits), error {code, retryable: false, next_action}.
The findings cap is 50 ID occurrences (body then branch); repeats count too.
No body text or full branch name is included. Private repos skip scanning.
Errors have empty findings and stable codes (see the header contract).
json_encoding_failed / fix_serializer means jq is unavailable or failed.
Missing visibility is echoed as "" on usage errors. --help always prints text.
Example: {"schema_version":1,"verdict":"violation","visibility":"public","context":"review","findings":[{"source":"body","line":2,"matched":["EXAMPLE-123"]}],"truncated":0,"error":{"code":"ok","retryable":false,"next_action":"redact_ids"}}

Exit: 0 pass, 1 violation, 2 usage/internal error.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --visibility) VISIBILITY="${2:-}"; shift 2 || fail missing_value fix_arguments "ERROR: --visibility needs a value" ;;
    --visibility=*) VISIBILITY="${1#*=}"; shift ;;
    --body-file) BODY_FILE="${2:-}"; shift 2 || fail missing_value fix_arguments "ERROR: --body-file needs a value" ;;
    --body-file=*) BODY_FILE="${1#*=}"; shift ;;
    --branch) BRANCH="${2:-}"; shift 2 || fail missing_value fix_arguments "ERROR: --branch needs a value" ;;
    --branch=*) BRANCH="${1#*=}"; shift ;;
    --context) CONTEXT="${2:-}"; shift 2 || fail missing_value fix_arguments "ERROR: --context needs a value" ;;
    --context=*) CONTEXT="${1#*=}"; shift ;;
    --json) shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if (( JSON )); then fail unknown_argument fix_arguments ""; fi
      echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$VISIBILITY" in
  public|private) ;;
  *) fail invalid_visibility fix_arguments "ERROR: --visibility must be public|private, got '${VISIBILITY:-}'" ;;
esac
if [[ -z "$BODY_FILE" ]]; then fail missing_body_file fix_arguments "ERROR: --body-file is required"; fi
if [[ ! -r "$BODY_FILE" ]]; then fail body_not_readable fix_body_file "ERROR: body file not readable: $BODY_FILE"; fi
if [[ ! -f "$BODY_FILE" ]]; then fail body_not_regular fix_body_file "ERROR: body file is not a regular file: $BODY_FILE"; fi

# Private repos carry IDs by policy. Nothing to check; the file read above is
# the only validation a private post needs.
if [[ "$VISIBILITY" == "private" ]]; then
  if (( JSON )); then emit_json pass ok post; fi
  exit 0
fi

PREFIXES="${INTERNAL_ID_PREFIXES:-TOG|PAP|PAPA}"
# \b keeps TOGETHER / PAPER trails out of the match; the suite pins both sides.
PATTERN="\b(${PREFIXES})-[0-9]+\b"

VIOLATIONS=0

# grep exit 0 = match, 1 = no match, >1 = execution error. An error must
# never read as "no matches" (the old `|| true` did exactly that), so each
# scan below branches on the exit code explicitly and exits 2 on error.
BODY_HITS=""; body_rc=0
BODY_HITS="$(grep -nE -o "$PATTERN" "$BODY_FILE" 2>/dev/null)" || body_rc=$?
if (( body_rc > 1 )); then
  fail body_scan_failed fix_scanner "ERROR: body scan failed (exit $body_rc); refusing to report PASS: $BODY_FILE"
fi
# A binary grep match can return 0 with no line/ID output. Refuse that
# incomplete scan in JSON mode rather than inventing an empty finding. Keep
# the historical text rendering unchanged.
if (( JSON && body_rc == 0 )) && [[ -z "$BODY_HITS" ]]; then
  fail body_scan_failed fix_scanner ""
fi
if (( body_rc == 0 )); then
  VIOLATIONS=1
  while IFS= read -r hit; do
    if (( JSON )); then
      record_hit body "${hit%%:*}" "${hit#*:}"
    else
      echo "VIOLATION [$CONTEXT body line ${hit%%:*}]: carries internal ID ${hit#*:}" >&2
    fi
  done <<<"$BODY_HITS"
fi

if [[ -n "$BRANCH" ]]; then
  BRANCH_HIT=""; branch_rc=0
  BRANCH_HIT="$(printf '%s' "$BRANCH" | grep -E -o "$PATTERN" 2>/dev/null)" || branch_rc=$?
  if (( branch_rc > 1 )); then
    fail branch_scan_failed fix_scanner "ERROR: branch scan failed (exit $branch_rc); refusing to report PASS"
  fi
  if (( branch_rc == 0 )); then
    VIOLATIONS=1
    if (( JSON )); then
      while IFS= read -r hit; do record_hit branch "" "$hit"; done <<<"$BRANCH_HIT"
    else
      echo "VIOLATION [$CONTEXT branch '$BRANCH']: carries internal ID $BRANCH_HIT" >&2
    fi
  fi
fi

if [[ "$VIOLATIONS" -ne 0 ]]; then
  if (( JSON )); then
    emit_json violation ok redact_ids
  else
    echo "VERDICT: VIOLATION -- public $CONTEXT carries internal ID(s); do not post. Link the Paperclip card in the private board thread/work-product instead." >&2
  fi
  exit 1
fi
if (( JSON )); then emit_json pass ok post; fi
exit 0
