#!/usr/bin/env bash
# ===========================================================================
# hook_spawn_guard.sh — do per-prompt/per-tool hooks leak processes?
#
# WHY THIS EXISTS (case_ccf26154). On 2026-10-03 ~1,025 node + ~1,023 python3
# processes from per-prompt hooks OOM-killed the paperclip container (40 GiB).
# The fleet settings at the time ran TWO hooks on every UserPromptSubmit:
# `bash pacer_tick.sh` (which `setsid nohup bash pacer_run.sh &` + `disown`,
# i.e. detached a python3 sampler per prompt) and `node gate-hook.mjs`.
# That is the 1:1 node:python3 pairing in the kernel OOM table. The feed the
# pacer rate-limited against had been stale since 09-04, so its MIN_INTERVAL
# gate never fired and EVERY prompt detached a child. A hook that detaches
# children survives its own death BY DESIGN (setsid escapes the process group
# the hook timeout kills), so at burst prompt rates the orphans accumulate
# faster than they exit.
#
# WHAT IT CHECKS. `check --settings <fleet-settings.json> [--hooks-dir <dir>]
# [--format text|json]`
#   F1 detached-spawn: a hook command — or the script body it points at — that
#      backgrounds work. Detached children outlive the hook timeout kill and
#      are the OOM mechanism above.
#   F2 missing-timeout: a hook entry with no numeric `timeout`. Without one the
#      only bound is the harness default, which is not a reviewed choice.
#
# The checks are extension-aware, because each language detaches differently:
#   .sh        setsid / nohup / disown / a bare `&` that is neither `&&` nor
#              `&>`. Full-line `#` comments are stripped first, so a comment
#              that merely NAMES setsid does not trip the check.
#   .mjs/.js   `detached` (spawn detached:true) / child_process `fork(`. A
#              timer `.unref()` is explicitly NOT flagged: it shortens life.
#              A bare `&` is NOT checked here — in JS it is bitwise-AND or sits
#              inside regex classes like `[^\s;&|()]`, and flagging it fires on
#              the live model-guard (measured 2026-10-03).
#   .py        start_new_session / os.fork / os.setsid / daemon. A plain Popen
#              is NOT flagged: it stays in the hook's process group, so the
#              hook timeout kill still reaps it.
# Anything else (or an unresolvable body) is checked on the command string
# alone. Every approximation above is pinned by the suite, so a future
# "smarter" pattern cannot silently narrow it.
#
# Offline: reads one JSON file and optionally script bodies. No network, no
# credential, no /paperclip read. Exit 0 clean · 2 refused · 3 finding.
#
# OUTPUT. Default `--format text` prints `FINDING <rule> <detail>` lines (or
# `clean`); `--format json` prints one versioned envelope object instead.
# Exits are identical in both formats. Envelope (schema_version 1):
# verdict clean|finding|refused, rows [{rule, location, detail, next_action}],
# summary {findings, detached_spawn, missing_timeout}, error ({code,
# retryable, next_action} or null), next_action. Refusal codes:
# settings_unreadable, settings_unparseable, hooks_dir_unreadable.
# ===========================================================================
set -uo pipefail

ME="$(basename "${BASH_SOURCE[0]}")"
EXIT_OK=0; EXIT_REFUSED=2; EXIT_FINDING=3

die() { printf '%s: %s\n' "$ME" "$*" >&2; exit $EXIT_REFUSED; }

usage() { die "usage: $ME check --settings <file> [--hooks-dir <dir>] [--format text|json]"; }

# JSON string escaping via stdlib only (python3 is already required below).
# One spawn per field; findings are few, so correctness beats cleverness.
json_escape() {
  python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.argv[1])[1:-1])' "$1"
}

ROWS_JSON=""
json_row() { # json_row <rule> <location> <detail> <next_action>
  local frag
  frag="$(printf '{"rule":"%s","location":"%s","detail":"%s","next_action":"%s"}' \
    "$1" "$(json_escape "$2")" "$(json_escape "$3")" "$(json_escape "$4")")"
  if [ -n "$ROWS_JSON" ]; then ROWS_JSON="$ROWS_JSON,$frag"; else ROWS_JSON="$frag"; fi
}

emit_json() { # emit_json <verdict> <top_next_action> — clean/finding envelope
  printf '{"schema_version":1,"tool":"hook_spawn_guard","command":"check","verdict":"%s","rows":[%s],"summary":{"findings":%d,"detached_spawn":%d,"missing_timeout":%d},"error":null,"next_action":"%s"}\n' \
    "$1" "$ROWS_JSON" "$FINDINGS" "$DETACHED_N" "$MISSING_N" "$(json_escape "$2")"
}

emit_json_refused() { # emit_json_refused <code> <next_action>
  local esc
  esc="$(json_escape "$2")"
  printf '{"schema_version":1,"tool":"hook_spawn_guard","command":"check","verdict":"refused","rows":[],"summary":{"findings":0,"detached_spawn":0,"missing_timeout":0},"error":{"code":"%s","retryable":false,"next_action":"%s"},"next_action":"%s"}\n' \
    "$1" "$esc" "$esc"
}

refuse() { # refuse <code> <message> — exit 2 in both formats (fail closed)
  local next_action=""
  case "$1" in
    settings_unreadable) next_action="point --settings at a readable fleet-settings JSON file" ;;
    settings_unparseable) next_action="fix the JSON syntax in the fleet-settings file" ;;
    hooks_dir_unreadable) next_action="point --hooks-dir at a readable hooks directory" ;;
  esac
  if [ -n "$2" ]; then printf '%s: %s\n' "$ME" "$2" >&2; fi
  if [ "$FORMAT" = "json" ]; then emit_json_refused "$1" "$next_action"; fi
  exit $EXIT_REFUSED
}

SETTINGS=""; HOOKS_DIR=""; FORMAT="text"
CMD="${1:-}"; shift 2>/dev/null || true
[ "$CMD" = "check" ] || usage
while [ $# -gt 0 ]; do
  case "$1" in
    --settings) SETTINGS="${2:-}"; shift 2 ;;
    --hooks-dir) HOOKS_DIR="${2:-}"; shift 2 ;;
    --format)
      if [ $# -lt 2 ]; then usage; fi
      case "${2:-}" in
        text|json) FORMAT="$2"; shift 2 ;;
        *) usage ;;
      esac
      ;;
    *) usage ;;
  esac
done
[ -n "$SETTINGS" ] || usage
[ -f "$SETTINGS" ] || refuse settings_unreadable "not a file: $SETTINGS"
[ -z "$HOOKS_DIR" ] || [ -d "$HOOKS_DIR" ] || refuse hooks_dir_unreadable "not a directory: $HOOKS_DIR"
# An unreadable hooks dir must refuse, never silently skip bodies: body
# findings would go missing and a dirty tree would read clean (fail closed).
if [ -n "$HOOKS_DIR" ] && { [ ! -r "$HOOKS_DIR" ] || [ ! -x "$HOOKS_DIR" ]; }; then
  refuse hooks_dir_unreadable "unreadable directory: $HOOKS_DIR"
fi

command -v python3 >/dev/null 2>&1 || die "python3 is required (stdlib json only)"

# --- enumerate hook entries: \x1f-separated (a unit separator, because bash
# treats TAB as collapsible IFS whitespace and would swallow an empty
# timeout field, shifting the command into its slot). Newlines and \x1f
# inside a command are flattened to spaces so one entry is one line. ---
ENTRIES="$(python3 - "$SETTINGS" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except OSError as e:
    print("unreadable settings: %s" % e, file=sys.stderr)
    sys.exit(10)
except ValueError as e:
    print("unreadable settings: %s" % e, file=sys.stderr)
    sys.exit(11)
if not isinstance(doc, dict):
    print("unreadable settings: top-level JSON must be an object", file=sys.stderr)
    sys.exit(11)
hooks = doc.get("hooks")
if not isinstance(hooks, dict):
    sys.exit(0)
for event, groups in hooks.items():
    if not isinstance(groups, list):
        continue
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            continue
        for h in (g.get("hooks") or []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "") if isinstance(h.get("command"), str) else ""
            cmd = cmd.replace("\x1f", " ").replace("\n", " ")
            print("%s\x1f%s\x1f%s\x1f%s" % (
                event, i,
                h.get("timeout") if isinstance(h.get("timeout"), int) else "",
                cmd))
PY
)"
PY_RC=$?
if [ "$PY_RC" -eq 11 ]; then
  refuse settings_unparseable ""
elif [ "$PY_RC" -ne 0 ]; then
  refuse settings_unreadable ""
fi

FINDINGS=0; DETACHED_N=0; MISSING_N=0
finding() { # finding <rule> <location> <detail> <next_action> <text_detail>
  FINDINGS=$((FINDINGS+1))
  case "$1" in
    detached-spawn) DETACHED_N=$((DETACHED_N+1)) ;;
    missing-timeout) MISSING_N=$((MISSING_N+1)) ;;
  esac
  if [ "$FORMAT" = "json" ]; then
    json_row "$1" "$2" "$3" "$4"
  else
    printf 'FINDING %s %s\n' "$1" "$5"
  fi
}

# A bare & that backgrounds: neither `&&` (list operator), `&>` (redirect to
# file) nor `>&` (fd duplication, as in `2>&1`). ERE has no lookbehind, so the
# (possibly empty) neighbours are captured and the first char re-anchored.
has_background_op() {
  local t="$1"
  [[ "$t" =~ (^|[^&>])\&([^&>]|$) ]]
}

checked_bodies=""
scan_body() {
  local path="$1" where="$2" ext body code
  ext="${path##*.}"
  body="$(cat "$path" 2>/dev/null)" || return 0
  # Full-line comments name tools without using them; strip them first.
  code="$(grep -vE '^[[:space:]]*#' <<<"$body")"
  case "$ext" in
    sh)
      if grep -qwE 'setsid|nohup|disown' <<<"$code" || has_background_op "$code"; then
        finding "detached-spawn" "$path" \
          "body detaches (setsid/nohup/bare-&/disown)" \
          "remove detached spawning from the hook body" \
          "$where body $path detaches (setsid/nohup/bare-&/disown)"
      fi
      ;;
    mjs|js|cjs)
      if grep -qE 'detached|fork[[:space:]]*\(' <<<"$code"; then
        finding "detached-spawn" "$path" \
          "body detaches (detached/fork)" \
          "remove detached spawning from the hook body" \
          "$where body $path detaches (detached/fork)"
      fi
      ;;
    py)
      if grep -qE 'start_new_session|os\.fork|os\.setsid|daemon' <<<"$code"; then
        finding "detached-spawn" "$path" \
          "body detaches (fork/setsid/daemon)" \
          "remove detached spawning from the hook body" \
          "$where body $path detaches (fork/setsid/daemon)"
      fi
      ;;
  esac
}

check_body() {
  local cmd="$1" path=""
  # Prefer an absolute script path named in the command; fall back to a
  # basename lookup under --hooks-dir so fixtures stay hermetic.
  if [[ "$cmd" =~ (/[^[:space:]]+\.(sh|mjs|js|cjs|py)) ]]; then
    path="${BASH_REMATCH[1]}"
    [ -f "$path" ] || path=""
  fi
  if [ -z "$path" ] && [ -n "$HOOKS_DIR" ]; then
    local base
    base="$(basename "$(awk '{print $1}' <<<"$cmd")" 2>/dev/null)"
    [ -n "$base" ] && [ -f "$HOOKS_DIR/$base" ] && path="$HOOKS_DIR/$base"
  fi
  [ -n "$path" ] || return 0
  case "$checked_bodies" in *"|$path|"*) return 0;; esac
  checked_bodies="$checked_bodies|$path|"
  scan_body "$path" "$2"
}

while IFS=$'\x1f' read -r event idx timeout cmd; do
  [ -n "$cmd" ] || continue
  where="$event[$idx] ${cmd:0:80}"
  if [[ "$cmd" =~ setsid || "$cmd" =~ nohup || "$cmd" =~ disown ]] || has_background_op "$cmd"; then
    finding "detached-spawn" "${event}[$idx]" \
      "command detaches (setsid/nohup/bare-&/disown)" \
      "remove detached spawning from the hook command" \
      "$where command detaches (setsid/nohup/bare-&/disown)"
  fi
  if ! [[ "$timeout" =~ ^[0-9]+$ ]]; then
    finding "missing-timeout" "${event}[$idx]" \
      "has no numeric hook timeout" \
      "add a numeric timeout to the hook entry" \
      "$where has no numeric hook timeout"
  fi
  check_body "$cmd" "$where"
done <<<"$ENTRIES"

if [ "$FINDINGS" -gt 0 ]; then
  printf '%s: %d finding(s)\n' "$ME" "$FINDINGS" >&2
  if [ "$FORMAT" = "json" ]; then
    emit_json "finding" "resolve the $FINDINGS finding(s): remove detached spawning, add numeric hook timeouts"
  fi
  exit $EXIT_FINDING
fi
if [ "$FORMAT" = "json" ]; then
  emit_json "clean" ""
else
  echo "$ME: clean"
fi
