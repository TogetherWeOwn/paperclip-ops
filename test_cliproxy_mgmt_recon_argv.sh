#!/usr/bin/env bash
# Regression suite for plugins/cliproxy-insight/deploy/cliproxy_mgmt_recon.sh:
# the live management key must never reach curl argv (case_7cbfeac4).
#
# THE BUG THIS EXISTS FOR (case_3d790dcb class, roadmap item 2.15). The recon script
# called `curl ... -H "Authorization: Bearer $KEY" ...` once per candidate
# endpoint, where KEY is the live CLIProxy management key. /proc/<pid>/cmdline
# is world-readable and the host is shared, so any local process could read
# the key for the lifetime of every call by polling /proc. No exploit, no log
# entry. gh_token.sh, notify_paperclip_issue.sh and db_backup_stall.sh already
# carry bearers in 0600 --config files; this call was the odd one out in its
# file. The bearer now travels in one 0600 curl --config file that serves the
# whole run (the key is constant across endpoints) and is removed by the EXIT
# trap, so only the config PATH ever appears in argv.
#
# WHY THIS IS HARDER THAN IT LOOKS. Two traps a naive suite falls into:
#
#   Deleting the Authorization header entirely would pass "no key in argv"
#   with full marks. So every no-leak assertion is paired with a positive
#   assertion that the key DID reach curl, by the config file.
#
#   Grepping the source for `-H "Authorization` proves nothing about what the
#   process actually executed — a helper, an array, an eval, or a future
#   rewrite all defeat it. So this suite asserts on the kernel's own record:
#   a stub `curl` reads its own /proc/<pid>/cmdline, which is byte for byte
#   what a snooping neighbour would have read. Same seam as
#   test_gh_token_argv.sh and the case_74682bfa section of test_db_backup_stall.sh.
#
#   The same world-readable argv surface applies to EVERY child, not just
#   curl: redact_key() once ran `sed -e "s/$KEY/..."` on every 200 response,
#   so the live key sat on sed's cmdline three times per default run. A suite
#   that only watches curl stays green through that leak. So a second shim —
#   a `sed` wrapper on PATH logging its own /proc/<pid>/cmdline — asserts the
#   canary never reaches any child's argv either.
#
# The stub reads /proc/$$/cmdline, not /proc/self/cmdline. `< /proc/self/...`
# inside the stub is opened by a forked child that is about to exec `tr`, so
# self would resolve to tr's argv, not the stub's, and the suite would pass
# for the wrong reason. $$ is this shell's own pid, resolved before the fork.
#
# Offline by construction. Nothing here is real: a throwaway canary key, a
# stub curl that never opens a socket, and fixture bodies. The tool runs under
# `env -i` so an operator's live credentials cannot leak in.
#
# Requires bash and /proc. No node, no network. Works with or without node on
# PATH: the structure dump prints field names and TYPES in both modes, so the
# contract assertions below hold either way.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${RECON_SH:-$HERE/plugins/cliproxy-insight/deploy/cliproxy_mgmt_recon.sh}"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[[ -r /proc/$$/cmdline ]] || { echo "test_cliproxy_mgmt_recon_argv: needs a Linux /proc" >&2; exit 2; }
[[ -x "$TOOL" ]] || { echo "test_cliproxy_mgmt_recon_argv: $TOOL is not executable" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- what "the live key" looks like -------------------------------------------
# Assembled from fragments so the literal canary never appears in this file as
# a whole string: CI greps tracked files for token shapes, and a suite that
# trips the repo's own secret scan is a suite people disable.
CANARY_PRE="mgmtkeycanary"
CANARY="${CANARY_PRE}0000000000000000000001"

# A credential-shaped VALUE living in a fixture body, not the key we send.
# The recon contract is "field names and types only, values never printed",
# so this value must never reach stdout at default verbosity either.
FIXTURE_SECRET="fixture-secret-value-001"

# --- stub curl ----------------------------------------------------------------
# Three jobs: record the argv the kernel sees, record what arrived by config
# file instead, and answer plausibly enough that the recon loop runs to
# completion over all 17 endpoints. It is deliberately permissive — a
# regressed tool must SUCCEED here so the suite catches the leak, rather than
# erroring out and looking like a refusal.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/stubcurl" <<'STUBCURL'
#!/usr/bin/env bash
# $$ not self: see the header of test_cliproxy_mgmt_recon_argv.sh.
{ tr '\0' '\n' < "/proc/$$/cmdline"; printf -- '--END-ARGV--\n'; } >> "$STUB_CMDLOG"

cfg=""; prev=""
for a in "$@"; do
  case "$prev" in --config|-K) cfg="$a" ;; esac
  prev="$a"
done

out=""; dump=""; prev=""
for a in "$@"; do
  case "$prev" in -o) out="$a" ;; -D|--dump-header) dump="$a" ;; esac
  prev="$a"
done

url=""
for a in "$@"; do case "$a" in http://*|https://*) url="$a" ;; esac; done

if [[ -n "$cfg" ]]; then
  printf '%s\t%s\n' "$(stat -c '%a' "$cfg")" "$cfg" >> "$STUB_CFGLOG"
  # The key the tool believes it is sending, so the suite can prove the config
  # path carries the real thing and not a placeholder.
  sed -n 's/^header = "Authorization: Bearer \(.*\)"$/BEARER\t\1/p' "$cfg" >> "$STUB_CFGLOG"
fi
printf 'URL\t%s\n' "$url" >> "$STUB_CFGLOG"

ep="${url#http://stub.invalid}"; ep="${ep#https://stub.invalid}"
case "$ep" in
  /v0/management/auth-files|/v0/management/api-keys|/v0/management/config)
    code=200
    printf '{"email":"op@example.invalid","account":"stub-acct","path":"/stub","id":"stub-id","auth_index":0,"project_id":"stub-proj","id_token":"%s","apiKey":"%s"}\n' \
      "$STUB_ID_TOKEN" "$STUB_API_KEY" > "$out"
    ;;
  *)
    code=404
    printf '{"error":"not found"}\n' > "$out"
    ;;
esac
if [[ -n "$dump" ]]; then
  printf 'HTTP/1.1 %s stub\r\nContent-Type: application/json\r\n\r\n' "$code" > "$dump"
fi
printf '%s' "$code"
STUBCURL
chmod +x "$WORK/bin/stubcurl"

# --- stub sed -----------------------------------------------------------------
# Logs the kernel's argv record for every `sed` the tool spawns, then execs the
# real sed. Placed first on PATH for the recon run only, so the harness's own
# seds (outside env -i) are not logged. A redactor that execs the key (e.g.
# sed -e "s/$KEY/.../") lands here byte for byte.
cat > "$WORK/bin/sed" <<'STUBSED'
#!/usr/bin/env bash
{ tr '\0' '\n' < "/proc/$$/cmdline"; printf -- '--END-ARGV--\n'; } >> "$STUB_SEDLOG"
exec /usr/bin/sed "$@"
STUBSED
chmod +x "$WORK/bin/sed"

CMDLOG="$WORK/cmdline.log"
CFGLOG="$WORK/config.log"
SEDLOG="$WORK/sed-cmdline.log"
TOOLTMP="$WORK/tooltmp"

hdr "The live management key never reaches curl argv (case_7cbfeac4)"
if [[ -r /proc/$$/cmdline ]]; then
  # Positive control: prove the /proc detector fires on a real argv leak, or
  # every no-leak result below is meaningless.
  : > "$CMDLOG"; : > "$CFGLOG"
  STUB_CMDLOG="$CMDLOG" STUB_CFGLOG="$CFGLOG" \
  STUB_ID_TOKEN="x" STUB_API_KEY="y" \
    "$WORK/bin/stubcurl" -sS --max-time 15 -H "Authorization: Bearer $CANARY" \
    http://stub.invalid/v0/management/auth-files >/dev/null 2>&1
  if grep -qF "$CANARY" "$CMDLOG"; then
    ok "positive control: an argv-borne bearer IS detected in /proc"
  else
    bad "positive control BLIND: a bearer on argv was not seen in /proc -- every argv result below is meaningless"
  fi

  # Positive control for the sed shim: prove an argv-borne key in a sed child
  # IS detected, or every no-leak result below for non-curl children is blind.
  : > "$SEDLOG"
  STUB_SEDLOG="$SEDLOG" PATH="$WORK/bin:/usr/bin:/bin" \
    sed -e "s/$CANARY/<KEY-REDACTED>/g" <<<"probe" >/dev/null 2>&1
  if grep -qF "$CANARY" "$SEDLOG"; then
    ok "positive control: an argv-borne key in sed IS detected in /proc"
  else
    bad "positive control BLIND: a key on sed argv was not seen in /proc -- every non-curl argv result below is meaningless"
  fi

  # Full recon run against the stub: the canary is the live key.
  : > "$CMDLOG"; : > "$CFGLOG"; : > "$SEDLOG"
  rm -rf "$TOOLTMP"; mkdir -p "$TOOLTMP"
  OUT="$(env -i PATH="$WORK/bin:/usr/bin:/bin" TMPDIR="$TOOLTMP" \
    CLIPROXY_MGMT_KEY="$CANARY" RECON_BASE="http://stub.invalid" RECON_SLEEP=0 \
    RECON_CURL="$WORK/bin/stubcurl" \
    STUB_CMDLOG="$CMDLOG" STUB_CFGLOG="$CFGLOG" STUB_SEDLOG="$SEDLOG" \
    STUB_ID_TOKEN="$FIXTURE_SECRET" STUB_API_KEY="$FIXTURE_SECRET" \
    "$TOOL" 2>"$WORK/tool.err")"
  RC=$?
  (( RC == 0 )) && ok "recon still exits 0 against the stub" || bad "expected exit 0, got $RC ($(cat "$WORK/tool.err")"

  calls="$(grep -c -- '--END-ARGV--' "$CMDLOG")"
  if [[ "$calls" == "17" ]]; then
    ok "all 17 candidate endpoints were probed through the stub"
  else
    bad "expected 17 curl calls, saw ${calls:-0} -- the loop did not run as measured"
  fi

  if grep -qF "$CANARY" "$CMDLOG"; then
    bad "the live management key reached curl's argv"
  else
    ok "the live management key never appears in /proc/<curl>/cmdline"
  fi

  if [[ -s "$SEDLOG" ]]; then
    if grep -qF "$CANARY" "$SEDLOG"; then
      bad "the live management key reached a non-curl child's argv (see sed cmdline log)"
    else
      ok "the live management key never appears in any non-curl child's argv"
    fi
  else
    # No sed children at all is also a pass: nothing to leak through. Record
    # it distinctly so a future sed reintroduction is visible as a new call,
    # not as a silent absence.
    ok "no sed child was spawned with the key on argv (no sed argv observed)"
  fi

  if grep -qF "$CANARY" "$CFGLOG"; then
    ok "the bearer still reached curl by config file (recon is really authenticated, not silently unauthenticated)"
  else
    bad "no bearer reached curl at all -- recon is now unauthenticated, not fixed"
  fi

  modes="$(cut -f1 "$CFGLOG" | grep -E '^[0-7]{3,4}$' | sort -u)"
  if [[ -n "$modes" && "$modes" == "600" ]]; then
    ok "the config file was mode 600 while curl held it"
  else
    bad "config file modes were '${modes:-none observed}', expected exactly 600"
  fi

  leftover="$(find "$TOOLTMP" -mindepth 1 2>/dev/null)"
  if [[ -z "$leftover" ]]; then
    ok "no config file survived the run"
  else
    bad "recon left files in TMPDIR: $(tr '\n' ' ' <<<"$leftover")"
  fi

  # Output contract: structure only. Endpoint markers, statuses and the reading
  # guide are all present; neither the key nor any secret-valued body field is
  # printed at default verbosity.
  for marker in "=== GET /v0/management/auth-files ===" "=== GET /usage ===" \
      "status  200" "status  404" "DID ANY ROUTE THAT 404"; do
    if grep -qF "$marker" <<<"$OUT"; then
      ok "report still contains '$marker'"
    else
      bad "report lost '$marker' -- output contract changed"
    fi
  done
  if grep -qF "$CANARY" <<<"$OUT"; then
    bad "report prints the live key"
  else
    ok "report never prints the live key"
  fi
  if grep -qF "$FIXTURE_SECRET" <<<"$OUT"; then
    bad "report prints a secret-valued body field"
  else
    ok "report prints field names and types only, never secret values"
  fi

  # Source guard: no live bearer may be placed on a curl command line in this
  # file again. (The --config writer line carries the header NAME by necessity;
  # what is forbidden is handing it to curl as argv via -H.)
  if grep -q -- '-H "Authorization: Bearer' "$TOOL"; then
    bad "source still passes Authorization: Bearer to curl on argv"
  else
    ok "source passes no Authorization: Bearer to curl on argv"
  fi

  # Source guard: the live key must never be interpolated into a sed command
  # line either (the case_7cbfeac4 follow-up: redact_key() was sed -e "s/$KEY/..").
  # Comment lines are excluded so this documents the old defect without tripping.
  stripped="$(grep -v '^[[:space:]]*#' "$TOOL")"
  if grep -qE 'sed.*\$(\{)?KEY' <<<"$stripped"; then
    bad "source interpolates the live key into a sed command line"
  else
    ok "source interpolates the live key into no sed command line"
  fi
else
  bad "no Linux /proc -- argv assertions unverified"
fi

printf '\n\033[1mTOTAL\033[0m  %d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 )) || exit 1
