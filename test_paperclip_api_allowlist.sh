#!/usr/bin/env bash
# Hermetic suite for the shared shell API host allowlist
# (lib/paperclip_api_allowlist.sh, warn-first shadow). Mirrors
# test_paperclip_api_allowlist_py.py: exact-origin match, attacker cases,
# fail-closed file behavior, shadow WARNING + counter, hatch and bridge
# matrices, mode handling, and the --check dry-run. Fully offline: fixture
# host files, env -i, no network, no credentials.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       %s\n' "$2"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_pc_allowlist.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
RUN_ENV=(env -i "PATH=/usr/bin:/bin")

# run_check <base> <file> [extra-env...]: sources the lib and calls the
# check; prints "<rc> <count> <stderr>" for assertions.
run_check() {
  local base="$1" file="$2"; shift 2
  "${RUN_ENV[@]}" "$@" bash -c '
    source "$1"; PAPERCLIP_ALLOWLIST_SHADOW_COUNT=0
    errfile="$(mktemp)"; trap "rm -f $errfile" EXIT
    paperclip_allowlist_check "$2" "suite_probe" "$3" 2>"$errfile"; rc=$?
    printf "%s %s %s" "$rc" "$PAPERCLIP_ALLOWLIST_SHADOW_COUNT" "$(cat "$errfile")"
  ' probe "$HERE/lib/paperclip_api_allowlist.sh" "$base" "$file"
}

fixture() {
  local name="$1"; shift
  printf '%s\n' "$@" > "$WORK/$name"
  printf '%s' "$WORK/$name"
}

GOOD="$(fixture good-hosts 'https://board.example.invalid' 'https://port.example.invalid:8443')"
MISSING="$WORK/definitely-not-here"

hdr "match and normalization"
out="$(run_check "https://board.example.invalid" "$GOOD")"
[[ "$out" == "0 0 "* ]] && ok "exact match allows silently" || bad "exact match allows silently" "$out"
out="$(run_check "https://BOARD.Example.Invalid" "$GOOD")"
[[ "$out" == "0 0 "* ]] && ok "case-insensitive host matches" || bad "case-insensitive host matches" "$out"
for base in "https://board.example.invalid:443" "https://board.example.invalid/" "https://board.example.invalid:443/"; do
  out="$(run_check "$base" "$GOOD")"
  [[ "$out" == "0 0 "* ]] && ok "matches $base" || bad "matches $base" "$out"
done
out="$(run_check "https://port.example.invalid:8443" "$GOOD")"
[[ "$out" == "0 0 "* ]] && ok "port-bearing entry matches" || bad "port-bearing entry matches" "$out"

hdr "attacker cases warn in shadow, refuse in enforce"
for base in "https://board.example.invalid.evil.invalid" "https://evilboard.example.invalid" \
            "https://board.example.invalid:8443" "https://port.example.invalid:9443" \
            "https://board.example.invalid./" "https://board.example.invalid/evil"; do
  out="$(run_check "$base" "$GOOD")"
  if [[ "$out" == "0 1 WARNING allowlist_shadow base_host="*" sender=suite_probe "* ]]; then
    ok "shadow warns $base"
  else
    bad "shadow warns $base" "$out"
  fi
  out="$(run_check "$base" "$GOOD" PAPERCLIP_API_ALLOWLIST_MODE=enforce)"
  [[ "$out" == "1 0 refusing: allowlist base not allowlisted "* ]] \
    && ok "enforce refuses $base" || bad "enforce refuses $base" "$out"
done
out="$(run_check "https://board.example.invalid/evil" "$GOOD")"
[[ "$out" == *"reason=path-not-matched"* ]] && ok "path reason pinned" || bad "path reason pinned" "$out"
out="$(run_check "https://board.example.invalid" "$GOOD" PAPERCLIP_API_ALLOWLIST_MODE=enforce)"
[[ "$out" == "0 0 "* ]] && ok "enforce still allows matches" || bad "enforce still allows matches" "$out"
out="$(run_check "https://user@board.example.invalid/" "$GOOD")"
[[ "$out" == "1 0 refusing: allowlist base is not a clean http(s) base"* ]] \
  && ok "userinfo refuses in warn mode" || bad "userinfo refuses in warn mode" "$out"

hdr "fail-closed file behavior"
out="$(run_check "https://board.example.invalid" "$MISSING")"
[[ "$out" == "1 0 refusing: allowlist file missing or unreadable: "* ]] \
  && ok "missing file refuses" || bad "missing file refuses" "$out"
out="$(run_check "https://board.example.invalid" "$MISSING" PAPERCLIP_API_ALLOWLIST_MODE=enforce)"
[[ "$out" == 1* ]] && ok "missing file refuses in enforce" || bad "missing file refuses in enforce" "$out"
mkdir -p "$WORK/hosts-dir"
out="$(run_check "https://board.example.invalid" "$WORK/hosts-dir")"
[[ "$out" == "1 0 refusing: allowlist file missing or unreadable: "* ]] \
  && ok "directory as file refuses" || bad "directory as file refuses" "$out"
lineno=0
for badline in "http://board.example.invalid" "https://board.example.invalid/evil" \
               "https://user@board.example.invalid/" "https://board.example.invalid./" \
               "notaurl" "https://board.example.invalid # inline comment"; do
  lineno=$((lineno + 1))
  f="$(fixture "bad-$lineno" "https://board.example.invalid" "$badline")"
  out="$(run_check "https://board.example.invalid" "$f")"
  if [[ "$out" == "1 0 refusing: allowlist file $f:2: malformed origin "* ]]; then
    ok "malformed line refuses: $badline"
  else
    bad "malformed line refuses: $badline" "$out"
  fi
done
f="$(fixture comments '# operator hosts' '' '   ' 'https://board.example.invalid' 'https://board.example.invalid  ')"
out="$(run_check "https://board.example.invalid" "$f")"
[[ "$out" == "0 0 "* ]] && ok "comments/blanks/duplicates tolerated" || bad "comments/blanks/duplicates tolerated" "$out"
: > "$WORK/empty-hosts"
out="$(run_check "https://board.example.invalid" "$WORK/empty-hosts")"
[[ "$out" == "0 1 WARNING allowlist_shadow "* ]] && ok "empty file matches nothing" || bad "empty file matches nothing" "$out"

hdr "hatch matrix"
for base in "http://127.0.0.1:9/api" "https://127.0.0.2/" "http://127.255.255.255/" \
            "http://localhost:3000/api" "https://localhost/" "http://[::1]:9/api" \
            "http://board.invalid/api" "https://deep.sub.board.invalid/"; do
  out="$(run_check "$base" "$MISSING" PAPERCLIP_API_ALLOWLIST_TEST=1)"
  [[ "$out" == "0 0 "* ]] && ok "hatch allows $base" || bad "hatch allows $base" "$out"
done
for base in "https://board.example.com" "http://128.0.0.1/" "http://127.0.0.300/" \
            "http://localhost./" "http://invalid/" "https://board.example.invalid.com/"; do
  out="$(run_check "$base" "$GOOD" PAPERCLIP_API_ALLOWLIST_TEST=1)"
  [[ "$out" == "1 0 refusing: allowlist test hatch with non-test base "* ]] \
    && ok "hatch refuses $base" || bad "hatch refuses $base" "$out"
done
out="$(run_check "http://127.0.0.1:9/" "$GOOD" PAPERCLIP_API_ALLOWLIST_TEST=true)"
[[ "$out" == "0 1 WARNING allowlist_shadow "* ]] && ok "hatch is exact value 1" || bad "hatch is exact value 1" "$out"
out="$(run_check "http://127.0.0.1:9/" "$GOOD")"
[[ "$out" == "0 1 WARNING allowlist_shadow "* ]] && ok "no hatch means file path for loopback" || bad "no hatch means file path for loopback" "$out"

hdr "bridge matrix"
for base in "http://127.0.0.1:38035" "http://127.0.0.1:1/" "http://127.0.0.1:65535/api"; do
  out="$(run_check "$base" "$MISSING" PAPERCLIP_API_BRIDGE_MODE=queue_v1)"
  [[ "$out" == "0 0 "* ]] && ok "bridge allows $base" || bad "bridge allows $base" "$out"
done
for base in "https://127.0.0.1:38035/" "http://127.0.0.2:38035/" "http://127.0.0.1/" "https://evil.example.com/"; do
  out="$(run_check "$base" "$GOOD" PAPERCLIP_API_BRIDGE_MODE=queue_v1)"
  [[ "$out" == "0 1 WARNING allowlist_shadow "* ]] && ok "marker widens nothing: $base" || bad "marker widens nothing: $base" "$out"
done
out="$(run_check "https://board.example.invalid" "$GOOD" PAPERCLIP_API_BRIDGE_MODE=queue_v1)"
[[ "$out" == "0 0 "* ]] && ok "listed host matches with marker set" || bad "listed host matches with marker set" "$out"
out="$(run_check "http://127.0.0.1:38035/" "$GOOD" PAPERCLIP_API_BRIDGE_MODE=1)"
[[ "$out" == "0 1 WARNING allowlist_shadow "* ]] && ok "wrong marker value ignored" || bad "wrong marker value ignored" "$out"

hdr "mode handling and counter file"
out="$(run_check "https://evil.example.com" "$GOOD" PAPERCLIP_API_ALLOWLIST_MODE=disabled)"
[[ "$out" == "1 0 refusing: allowlist unknown PAPERCLIP_API_ALLOWLIST_MODE=disabled "* ]] \
  && ok "unknown mode refuses" || bad "unknown mode refuses" "$out"
out="$(run_check "https://evil.example.com" "$GOOD" PAPERCLIP_API_ALLOWLIST_MODE="")"
[[ "$out" == "0 1 WARNING allowlist_shadow "* ]] && ok "empty mode is warn" || bad "empty mode is warn" "$out"
out="$("${RUN_ENV[@]}" PAPERCLIP_API_ALLOWLIST_COUNTER_FILE="$WORK/shadow.count" bash -c '
    source "$1"; PAPERCLIP_ALLOWLIST_SHADOW_COUNT=0
    paperclip_allowlist_check "https://evil.example.com" "suite_probe" "$2" 2>/dev/null
    paperclip_allowlist_check "https://evil.example.com" "suite_probe" "$2" 2>/dev/null
    printf "%s %s" "$PAPERCLIP_ALLOWLIST_SHADOW_COUNT" "$(cat "$3")"
  ' probe "$HERE/lib/paperclip_api_allowlist.sh" "$GOOD" "$WORK/shadow.count")"
[[ "$out" == "2 2" ]] && ok "counter var and file increment" || bad "counter var and file increment" "$out"

hdr "--check dry-run"
out="$("${RUN_ENV[@]}" "$HERE/lib/paperclip_api_allowlist.sh" --check "https://board.example.invalid" --file "$GOOD" 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 0 && "$out" == ALLOW* ]] && ok "allow rc 0" || bad "allow rc 0" "rc=$rc out=$out"
out="$("${RUN_ENV[@]}" "$HERE/lib/paperclip_api_allowlist.sh" --check "https://evil.example.com" --file "$GOOD" 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 0 && "$out" == ALLOW* ]] && grep -q "allowlist_shadow" "$WORK/err.txt" \
  && ok "shadow allows with warning" || bad "shadow allows with warning" "rc=$rc out=$out"
out="$("${RUN_ENV[@]}" "$HERE/lib/paperclip_api_allowlist.sh" --check "https://evil.example.com" --file "$GOOD" --mode enforce 2>/dev/null)"; rc=$?
[[ "$rc" -eq 1 && "$out" == REFUSE* ]] && ok "enforce refuses rc 1" || bad "enforce refuses rc 1" "rc=$rc out=$out"
f="$(fixture malformed "https://board.example.invalid" "garbage{{{")"
out="$("${RUN_ENV[@]}" "$HERE/lib/paperclip_api_allowlist.sh" --check "https://board.example.invalid" --file "$f" 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 1 && "$out" == REFUSE* ]] && grep -q "malformed origin" "$WORK/err.txt" \
  && ok "malformed file refuses rc 1" || bad "malformed file refuses rc 1" "rc=$rc out=$out"
out="$("${RUN_ENV[@]}" "$HERE/lib/paperclip_api_allowlist.sh" --bogus 2>/dev/null)"; rc=$?
[[ "$rc" -eq 2 ]] && ok "usage error rc 2" || bad "usage error rc 2" "rc=$rc"

printf '\nallowlist shell: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
