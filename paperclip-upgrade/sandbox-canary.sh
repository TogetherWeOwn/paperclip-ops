#!/usr/bin/env bash
# ===========================================================================
# paperclip-upgrade/sandbox-canary.sh -- Review Bot containment canary
# (case_f5985bd1; CISO concurrence on case_2803833a, parent case_b9cd6a4b).
#
# WHY. Review Bot reviews untrusted diffs with codex `-s read-only` and no
# networkScope. Its ONLY containment is that the shell fails closed: the agent
# container cannot create a user namespace, so every shell call (and its
# network egress) dies in bwrap. The reviewer's process environment holds the
# run token and the GitHub App key names. If a codex upgrade, an image change,
# a seccomp/userns/runtime change lets the sandbox START, the shell works and
# that environment is reachable. Nothing else would notice.
#
# WHAT. Two modes, neither makes a model call or needs a credential.
#
#   (default)  run INSIDE the agent container (as the agent uid):
#     1. codex --version       recorded; a change from the baseline is reported
#     2. codex sandbox -c sandbox_mode="read-only" -- echo canary-ok
#                              MUST exit 1 AND name the bwrap namespace refusal
#                              (the shell cannot start, and for THAT reason)
#     3. the same, running curl against the model front
#                              MUST exit 1 AND name the bwrap namespace refusal
#                              (no egress through the shell)
#     4. curl against the model front, NOT sandboxed
#                              MUST print an HTTP status (401 without a key), so
#                              a pass is not a dead network
#     C. codex sandbox -c sandbox_mode="danger-full-access" -- echo canary-control
#                              MUST exit 0 and print the word. This is the
#                              anti-vacuity control for steps 2-3, see below.
#
#   --adapter-config FILE      run on the host with the Operator's read-back of
#     Review Bot's agent record (JSON; the agent object or its adapterConfig):
#     dangerouslyBypassApprovalsAndSandbox must be exactly false (it defaults
#     to true at creation and overrides -s), dangerouslyBypassSandbox must not
#     be true, and the LAST sandbox flag in extraArgs, in any spelling (`-s X`,
#     `--sandbox X`, `--sandbox=X`, `-sX`), must be read-only, with no bypass
#     flag and no `--full-auto` (which selects workspace-write).
#
# WHY THE CONTROL (C). The check as first written in case_f5985bd1 was
# `codex sandbox linux -- echo canary-ok`. On codex-cli 0.159.2 `codex sandbox`
# takes the command DIRECTLY (usage: `codex sandbox [OPTIONS] [COMMAND]...`);
# there is no `linux` subcommand, so `linux` is parsed as the program to run.
# Measured with the sandbox switched off: `codex sandbox echo x` exits 0 and
# prints x; `codex sandbox linux -- echo x` exits 1 "No such file or directory".
# So the original form exits non-zero whether or not the sandbox starts: it
# would have passed after the very regression it exists to catch. Steps 2-3
# here use the direct form, and C proves that form CAN succeed (full access,
# an echo). If C fails the canary cannot tell open from closed, so the result
# is INCONCLUSIVE, never a pass.
#
# WHY "CLOSED" IS EXIT 1 PLUS THE BWRAP TEXT, NOT ANY NON-ZERO EXIT (CISO, PR
# review of case_f5985bd1, pin from case_644101b9 S2). Measured on 0.159.2: with TMPDIR
# unusable (`TMPDIR=/proc/nonexistent`) the sandboxed step panics with exit 101
# and prints no bwrap text, while the full-access control still exits 0. An
# any-non-zero test prints PASS in that state, and the only thing containing
# Review Bot is then a full or broken tmp dir, which clears the moment tmp works.
# A step is closed only when it exits 1 AND its output names the bwrap namespace
# refusal; any other non-zero exit is INCONCLUSIVE (4), never a pass.
#
# Exit status (callers gate on these, never on printed text):
#   0  PASS          steps 2-3 closed on the bwrap refusal, 4 and C proved the
#                    canary works
#   2  REFUSED       bad usage, a tool is missing, or the input is unreadable
#   3  BREACH        a sandboxed command exited 0 (or the adapterConfig allows
#                    a bypass): the shell can start. DO NOT CONTINUE THE CHANGE.
#   4  INCONCLUSIVE  a step hung, exited non-zero for some other reason than
#                    the bwrap refusal, or the control or positive control
#                    failed: the canary could not prove containment either way
#
# On 3 or 4 the standing action is in docs/runbooks/review-bot-sandbox-canary.md:
# stop the change, `Operator:` card to the CEO to set Review Bot
# runtimeConfig.heartbeat.enabled=false and wakeOnDemand=false, tell the CISO.
#
# Needs (default mode): bash, coreutils (timeout, mktemp), grep, codex, curl.
# Needs (--adapter-config): bash, jq. Writes nothing outside a mktemp dir it
# removes. Prints no environment, no credential and no command output beyond a
# truncated first line of a CLOSED step's stderr.
# ===========================================================================
set -uo pipefail

# BASH_SOURCE is unset when drain.sh pipes this in with `docker exec -i ... bash -s`.
ME="${BASH_SOURCE[0]:-sandbox-canary.sh}"; ME="${ME##*/}"
EXIT_PASS=0; EXIT_REFUSED=2; EXIT_BREACH=3; EXIT_INCONCLUSIVE=4

BASELINE_VERSION="${SANDBOX_CANARY_BASELINE_VERSION:-codex-cli 0.159.2}"
PROBE_URL="${SANDBOX_CANARY_URL:-http://cliproxy:8317/v1/models}"
STEP_TIMEOUT="${SANDBOX_CANARY_TIMEOUT_SECS:-30}"
ACTION_DOC="docs/runbooks/review-bot-sandbox-canary.md"

say()    { printf '%s\n' "$*"; }
refuse() { printf 'canary: REFUSED: %s\n' "$*" >&2; exit "$EXIT_REFUSED"; }

usage() {
  cat <<'USAGE'
usage: sandbox-canary.sh                         run in the agent container
       sandbox-canary.sh --adapter-config FILE   check the Operator's read-back
                                                 of Review Bot's agent record
USAGE
}

ADAPTER_FILE=""
while (($#)); do
  case "$1" in
    --adapter-config) ADAPTER_FILE="${2:?--adapter-config needs a file}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; refuse "unknown argument: $1" ;;
  esac
done

TMPD=""
trap '[[ -n "$TMPD" ]] && rm -rf -- "$TMPD"' EXIT

pointer() { say "canary: ACTION: stop the change; Operator card to the CEO to set Review Bot heartbeat.enabled=false and wakeOnDemand=false; tell the CISO. See $ACTION_DOC"; }

# ---------------------------------------------------------------------------
# --adapter-config: the read-back half (host side, jq only)
# ---------------------------------------------------------------------------
adapter_check() {
  local f="$1" bypass sandbox_alias last_sandbox bad_flags verdict=pass
  command -v jq >/dev/null || refuse "jq is required for --adapter-config"
  [[ -f "$f" && ! -L "$f" && -r "$f" ]] || refuse "adapter config is not a readable regular file: $f"
  jq -e 'type == "object"' "$f" >/dev/null 2>&1 || refuse "adapter config is not a JSON object"

  # Accept the whole agent record or just its adapterConfig. The redacted API
  # view is enough: extraArgs entries ending url/api are masked, `-s` pairs are not.
  local sel='(if (.adapterConfig | type) == "object" then .adapterConfig else . end)'
  bypass="$(jq -r "$sel | .dangerouslyBypassApprovalsAndSandbox | if . == false then \"false\" elif . == null then \"absent\" else (type + \":\" + tostring) end" "$f")"
  sandbox_alias="$(jq -r "$sel | .dangerouslyBypassSandbox | if . == null then \"absent\" else (type + \":\" + tostring) end" "$f")"
  # Every spelling of the sandbox flag counts and the LAST one wins: `-s X`,
  # `--sandbox X`, `--sandbox=X` and the attached `-sX` / `-s=X`. A `-s` with no
  # value reads as "null", which is not read-only.
  last_sandbox="$(jq -r "$sel | (.extraArgs // []) | if type != \"array\" then \"invalid\" else
      . as \$a | [range(0; length) as \$i | (\$a[\$i] | tostring) as \$x
        | if \$x == \"-s\" or \$x == \"--sandbox\" then (\$a[\$i + 1] | tostring)
          elif (\$x | startswith(\"--sandbox=\")) then (\$x | sub(\"^--sandbox=\"; \"\"))
          elif (\$x | test(\"^-s.\")) then (\$x | sub(\"^-s=?\"; \"\"))
          else empty end]
        | if length == 0 then \"none\" else .[-1] end end" "$f")"
  bad_flags="$(jq -r "$sel | (.extraArgs // []) | if type != \"array\" then \"invalid\" else
      [.[] | tostring | select(. == \"--dangerously-bypass-approvals-and-sandbox\" or . == \"--yolo\"
        or . == \"--full-auto\"
        or (test(\"sandbox_mode\") and (test(\"read-only\") | not)))] | length end" "$f")"

  say "adapter: dangerouslyBypassApprovalsAndSandbox=$bypass dangerouslyBypassSandbox=$sandbox_alias lastSandboxFlag=$last_sandbox bypassFlagsInExtraArgs=$bad_flags"
  [[ "$bypass" == false ]] || { say "adapter: BREACH: dangerouslyBypassApprovalsAndSandbox is '$bypass' (must be exactly false; it overrides -s)"; verdict=breach; }
  [[ "$sandbox_alias" == absent || "$sandbox_alias" == boolean:false ]] || { say "adapter: BREACH: dangerouslyBypassSandbox is '$sandbox_alias' (must be absent or false)"; verdict=breach; }
  [[ "$last_sandbox" == "read-only" ]] || { say "adapter: BREACH: the last sandbox flag in extraArgs is '$last_sandbox' (must be -s read-only)"; verdict=breach; }
  [[ "$bad_flags" == 0 ]] || { say "adapter: BREACH: extraArgs carries a bypass, --full-auto or non-read-only sandbox_mode flag"; verdict=breach; }

  if [[ "$verdict" == pass ]]; then say "canary: PASS (adapterConfig)"; exit "$EXIT_PASS"; fi
  say "canary: BREACH (adapterConfig)"; pointer; exit "$EXIT_BREACH"
}
[[ -z "$ADAPTER_FILE" ]] || adapter_check "$ADAPTER_FILE"

# ---------------------------------------------------------------------------
# default mode: in the agent container
# ---------------------------------------------------------------------------
[[ "$PROBE_URL" =~ ^https?://[A-Za-z0-9._:/-]+$ ]] || refuse "SANDBOX_CANARY_URL is not a plain http(s) URL"
[[ "$STEP_TIMEOUT" =~ ^[0-9]+$ && "$STEP_TIMEOUT" -ge 1 ]] || refuse "SANDBOX_CANARY_TIMEOUT_SECS must be a positive integer"
for t in codex curl timeout mktemp grep; do command -v "$t" >/dev/null || refuse "$t is not on PATH"; done
TMPD="$(mktemp -d)" || refuse "cannot create a temp dir"

# step NAME CMD...  ->  STEP_RC, STEP_OUT (file). stdin is /dev/null: when this
# script is piped into `docker exec -i ... bash -s`, a child that read stdin
# would eat the rest of the script.
step() {
  local name="$1"; shift
  STEP_OUT="$TMPD/$name.out"
  timeout --kill-after=5 "$STEP_TIMEOUT" "$@" </dev/null >"$STEP_OUT" 2>&1
  STEP_RC=$?
}
hung() { case "$1" in 124|137) return 0 ;; *) return 1 ;; esac; }  # timeout(1) codes
first_line() { head -n 1 "$1" 2>/dev/null | tr -cd '[:print:]' | cut -c1-120; }

BREACH=0; INCONCLUSIVE=0

step version codex --version
VERSION="$(first_line "$STEP_OUT")"
[[ "$STEP_RC" == 0 && -n "$VERSION" ]] || refuse "codex --version failed (rc=$STEP_RC)"
if [[ "$VERSION" == "$BASELINE_VERSION" ]]; then
  say "canary: step=version result=baseline version='$VERSION'"
else
  say "canary: step=version result=CHANGED version='$VERSION' baseline='$BASELINE_VERSION' (record it on the change card)"
fi

# C: the invocation shape can succeed. Without this, 2-3 could "fail closed"
# for a reason that has nothing to do with the sandbox (see the header).
step control codex sandbox -c 'sandbox_mode="danger-full-access"' -- echo canary-control
if [[ "$STEP_RC" == 0 && "$(head -n 1 "$STEP_OUT")" == canary-control ]]; then
  say "canary: step=control result=ok (the sandbox command form can succeed)"
else
  say "canary: step=control result=FAILED rc=$STEP_RC (steps 2-3 cannot tell open from closed on this codex)"
  INCONCLUSIVE=1
fi

# bwrap's refusal when the kernel will not give it a user namespace, e.g.
# "bwrap: No permissions to create a new namespace, likely because ...".
bwrap_namespace_refusal() { grep -Eiq 'bwrap:.*namespace' "$1" 2>/dev/null; }

classify_closed() { # NAME DESC: report step 2/3 from STEP_RC / STEP_OUT
  local name="$1" desc="$2"
  if [[ "$STEP_RC" == 0 ]]; then
    say "canary: step=$name result=BREACH rc=0 ($desc ran: the sandbox started)"
    BREACH=1
  elif hung "$STEP_RC"; then
    say "canary: step=$name result=INCONCLUSIVE rc=$STEP_RC (hung past ${STEP_TIMEOUT}s; not a clean fail-closed)"
    INCONCLUSIVE=1
  elif [[ "$STEP_RC" == 1 ]] && bwrap_namespace_refusal "$STEP_OUT"; then
    say "canary: step=$name result=closed rc=$STEP_RC stderr0='$(first_line "$STEP_OUT")'"
  else
    say "canary: step=$name result=INCONCLUSIVE rc=$STEP_RC (non-zero, but not exit 1 with the bwrap namespace refusal; something other than the userns denial stopped it) stderr0='$(first_line "$STEP_OUT")'"
    INCONCLUSIVE=1
  fi
}

step shell codex sandbox -c 'sandbox_mode="read-only"' -- echo canary-ok
classify_closed shell "echo canary-ok"

step egress codex sandbox -c 'sandbox_mode="read-only"' -- curl -sS --max-time 5 "$PROBE_URL"
classify_closed egress "curl through the sandbox"

step netctl curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$PROBE_URL"
CODE="$(head -n 1 "$STEP_OUT" | tr -cd '0-9' | cut -c1-3)"
if [[ "$STEP_RC" == 0 && "$CODE" =~ ^[1-5][0-9][0-9]$ ]]; then
  say "canary: step=network-control result=ok http=$CODE"
else
  say "canary: step=network-control result=FAILED rc=$STEP_RC http='$CODE' (a dead network would make step 3 pass for free)"
  INCONCLUSIVE=1
fi

if ((BREACH)); then say "canary: BREACH"; pointer; exit "$EXIT_BREACH"; fi
if ((INCONCLUSIVE)); then say "canary: INCONCLUSIVE"; pointer; exit "$EXIT_INCONCLUSIVE"; fi
say "canary: PASS"
exit "$EXIT_PASS"
