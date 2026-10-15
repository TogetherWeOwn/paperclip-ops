#!/usr/bin/env bash
# ===========================================================================
# test_case_f277272d_queue_wait_time_slo.sh — offline suite for the case_1555b1d1 queue
# wait-time SLO definition (ops/case_1555b1d1/queue-wait-time-slo.md).
#
# Pins the SLO table, endpoint shape/consistency, config non-collision,
# secret-free bundle and sibling dedupe boundary. Each mutant must change its
# fixture and reject with exit 1 after the identical healthy path exits 0.
# Setup failures are never mutant kills. No network or credentials.
# ===========================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOC="$HERE/ops/case_1555b1d1/queue-wait-time-slo.md"
BUNDLE="$HERE/ops/case_1555b1d1"
CFG="$HERE/ops/case_658bf6b9/gatus-config.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d "${PAPERCLIP_RUN_SCRATCH_DIR:-${TMPDIR:-/tmp}}/queue-slo.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT

[[ -f "$DOC" ]] || { echo "REFUSE: $DOC not found"; exit 3; }
[[ -f "$CFG" ]] || { echo "REFUSE: $CFG not found"; exit 3; }

# Checkers: 0 healthy, 1 measured violation, 2 setup/read failure.
# === 1+2. SLO table, endpoint shape, table↔snippet consistency ===============
slo_check() {  # <doc>
  python3 - "$1" <<'PY'
import re, sys
try:
    text = open(sys.argv[1]).read()
except (OSError, UnicodeError) as exc:
    print("REFUSE: cannot read SLO document", file=sys.stderr)
    sys.exit(2)
errs = []
def section(a, b):
    try:
        return text.split(a, 1)[1].split(b, 1)[0]
    except IndexError:
        return None
t = section("<!-- SLO-TABLE -->", "<!-- /SLO-TABLE -->")
if t is None:
    errs.append("SLO-TABLE markers missing")
rows = {}
if t is not None:
    for m in re.finditer(r"\|\s*`([a-z]+)`\s*\|\s*(\d+)m\s*\|\s*(\d+)m\s*\|", t):
        rows[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    if set(rows) != {"isolated", "selfhosted", "ephemeral"}:
        errs.append("table classes %s != isolated/selfhosted/ephemeral" % sorted(rows))
    for c, (w, p) in rows.items():
        if not 0 < w < p:
            errs.append("%s: need 0<warn<page, got %d/%d" % (c, w, p))
e = section("<!-- SLO-ENDPOINTS -->", "<!-- /SLO-ENDPOINTS -->")
if e is None:
    errs.append("SLO-ENDPOINTS markers missing")
names = re.findall(r"-\s*name:\s*(ci-wait-time-[a-z]+)", e or "")
if sorted(names) != ["ci-wait-time-ephemeral", "ci-wait-time-isolated",
                     "ci-wait-time-selfhosted"]:
    errs.append("endpoint names %s" % sorted(names))
if "\t" in (e or ""):
    errs.append("tab inside YAML snippet (Gatus YAML forbids tabs)")
blocks = re.split(r"(?=-\s*name:\s*ci-wait-time-)", e or "")
for b in blocks:
    m = re.match(r"-\s*name:\s*(ci-wait-time-([a-z]+))", b)
    if not m:
        continue
    name, cls = m.group(1), m.group(2)
    for need in ('group: platform', 'token: "${GATUS_EXTERNAL_TOKEN}"',
                 "interval: 10m"):
        if need not in b:
            errs.append("%s: missing %r" % (name, need))
    if b.count("- type: discord") != 1 or b.count("- type: custom") != 1:
        errs.append("%s: need exactly one discord + one custom alert" % name)
    if cls in rows:
        w, p = rows[cls]
        if ("warn %dm / page %dm" % (w, p)) not in b:
            errs.append("%s: description does not carry warn %dm / page %dm"
                        % (name, w, p))
if errs:
    print("ERRORS:")
    for x in errs:
        print(" - " + x)
    sys.exit(1)
print("SLO-OK")
PY
}

# === 3. NO COLLISION with gatus-config.yaml ===================================
collision_ok() {  # <cfg> <doc>; variable fixture comes first for both paths
  local cfg="$1" doc="$2" names n rc
  [[ -f "$doc" && -r "$doc" && -f "$cfg" && -r "$cfg" ]] || return 2
  if names="$(grep -o 'ci-wait-time-[a-z]*' "$doc")"; then :
  else rc=$?; [[ "$rc" -eq 1 ]] && return 1; return 2; fi
  while read -r n; do
    [[ -n "$n" ]] || continue
    if grep -q "^[[:space:]]*-[[:space:]]name:[[:space:]]$n[[:space:]]*$" "$cfg"; then
      return 1
    else
      rc=$?; [[ "$rc" -eq 1 ]] || return 2
    fi
    [[ "$n" != "ci-queue-age" && "$n" != "garm-pool" ]] || return 1
  done <<<"$names"
  return 0
}

# === 4. NO SECRETS in the new bundle ==========================================
secrets_ok() {  # <dir>; do not print matching fixture contents
  local dir="$1" rc
  [[ -d "$dir" && -r "$dir" ]] || return 2
  if grep -rEn 'discord\.com/api/webhooks|Bearer [A-Za-z0-9+/=_.-]{8,}|token:[[:space:]]*"[^$]|webhook/alert/[0-9a-f-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_' "$dir" >"$WORK/secrets.out"; then
    if grep -v 'env-indirected token' "$WORK/secrets.out" >/dev/null; then return 1
    else rc=$?; [[ "$rc" -eq 1 ]] || return 2; fi
  else
    rc=$?; [[ "$rc" -eq 1 ]] || return 2
  fi
  return 0
}

# === 5. DEDUPE boundary names siblings + their metrics ========================
dedupe_ok() {  # <doc>
  local region rc needle
  [[ -f "$1" && -r "$1" ]] || return 2
  region="$(awk '/Dedupe against sibling/,/^## 2\./' "$1")" || return 2
  [[ -n "$region" ]] || return 1
  for needle in case_60a899d5 case_c542accd case_d61d5dad runtime "provider operation"; do
    if grep -qF "$needle" <<<"$region"; then :
    else rc=$?; [[ "$rc" -eq 1 ]] && return 1; return 2; fi
  done
  return 0
}

healthy() {  # <label> <checker> <args...>
  local label="$1" rc; shift
  if "$@" >"$WORK/check.out" 2>&1; then ok "$label"
  else rc=$?; bad "$label" "checker exit=$rc"; fi
}
healthy "1+2 table, endpoint shape and consistency" slo_check "$DOC"
healthy "3 no collision with gatus-config.yaml" collision_ok "$CFG" "$DOC"
healthy "4 no secrets in bundle" secrets_ok "$BUNDLE"
healthy "5 dedupe boundary names siblings and metrics" dedupe_ok "$DOC"

# === Positive controls: changed fixture + healthy 0 + measured rejection 1 ===
mutation_result() {  # <before-file> <after-file> <checker> <healthy> <mutated> [args...]
  local before="$1" after="$2" checker="$3" good="$4" changed="$5" rc
  shift 5
  if "$checker" "$good" "$@" >"$WORK/control.out" 2>&1; then :
  else printf 'healthy control did not pass\n'; return 2; fi
  if cmp -s "$before" "$after"; then
    printf 'fixture did not change\n'; return 2
  else
    rc=$?; [[ "$rc" -eq 1 ]] || { printf 'cannot compare fixtures\n'; return 2; }
  fi
  if "$checker" "$changed" "$@" >"$WORK/mutant.out" 2>&1; then
    printf 'mutant survived\n'; return 1
  else
    rc=$?
    [[ "$rc" -eq 1 ]] || { printf 'setup error exit=%s, not a kill\n' "$rc"; return 2; }
  fi
  printf 'healthy=0 changed=yes mutant=1\n'
  return 0
}
mutant() {  # <label> <mutation_result args...>
  local label="$1" result rc; shift
  if result="$(mutation_result "$@")"; then ok "mutant $label killed ($result)"
  else rc=$?; bad "mutant $label not proven" "exit=$rc $result"; fi
}

cp "$DOC" "$WORK/m1.md"
python3 - "$WORK/m1.md" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p).read()
t, count = re.subn(r"(\|\s*`isolated`\s*\|\s*)40m(\s*\|\s*)60m(\s*\|)",
                   r"\g<1>60m\g<2>40m\g<3>", t)
if count != 1:
    sys.exit("REFUSE: M1 must swap exactly one isolated table row")
open(p, "w").write(t)
PY
mutant "M1 warn/page swap" "$DOC" "$WORK/m1.md" slo_check "$DOC" "$WORK/m1.md"

cp "$DOC" "$WORK/m2.md"
python3 - "$WORK/m2.md" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p).read()
a, b = "<!-- SLO-ENDPOINTS -->", "<!-- /SLO-ENDPOINTS -->"
head, rest = t.split(a, 1)
body, tail = rest.split(b, 1)
blocks = re.split(r"(?=  - name: ci-wait-time-)", body)
removed = [x for x in blocks if x.startswith("  - name: ci-wait-time-selfhosted\n")]
if len(removed) != 1:
    sys.exit("REFUSE: M2 must drop exactly one selfhosted endpoint")
body = "".join(x for x in blocks if x not in removed)
open(p, "w").write(head + a + body + b + tail)
PY
mutant "M2 dropped endpoint" "$DOC" "$WORK/m2.md" slo_check "$DOC" "$WORK/m2.md"

cp "$CFG" "$WORK/m3.yaml"
printf '\n  - name: ci-wait-time-isolated\n    group: platform\n' >> "$WORK/m3.yaml"
mutant "M3 colliding endpoint in config" "$CFG" "$WORK/m3.yaml" collision_ok "$CFG" "$WORK/m3.yaml" "$DOC"

mkdir -p "$WORK/m4bundle"
cp "$DOC" "$WORK/m4bundle/queue-wait-time-slo.md"
printf '\nwebhook-url: "https://discord.com/api/webhooks/123/abc"\n' >> "$WORK/m4bundle/queue-wait-time-slo.md"
mutant "M4 webhook secret" "$DOC" "$WORK/m4bundle/queue-wait-time-slo.md" secrets_ok "$BUNDLE" "$WORK/m4bundle"

cp "$DOC" "$WORK/m5.md"
sed -i '/case_60a899d5/d; /case_c542accd/d; /case_d61d5dad/d' "$WORK/m5.md"
mutant "M5 stripped dedupe rows" "$DOC" "$WORK/m5.md" dedupe_ok "$DOC" "$WORK/m5.md"

cp "$DOC" "$WORK/m6.md"
python3 - "$WORK/m6.md" <<'PY'
import sys
p = sys.argv[1]
t = open(p).read()
old = "two-isolated pickup p95 above SLO (warn 40m / page 60m)"
if t.count(old) != 2:
    sys.exit("REFUSE: M6 must change exactly two isolated descriptions")
open(p, "w").write(t.replace(old, "two-isolated pickup p95 above SLO (warn 10m / page 20m)"))
PY
mutant "M6 table/snippet number drift" "$DOC" "$WORK/m6.md" slo_check "$DOC" "$WORK/m6.md"

# === Harness regressions: setup failures/no-op fixtures cannot turn green ====
expect_status() {  # <expected> <label> <command...>
  local expected="$1" label="$2" rc; shift 2
  if "$@" >"$WORK/regression.out" 2>&1; then rc=0; else rc=$?; fi
  if [[ "$rc" -eq "$expected" ]]; then ok "$label (exit=$rc)"
  else bad "$label" "expected=$expected actual=$rc"; fi
}
survivor_check() { return 0; }
broken_control() { return 1; }
setup_error_check() { [[ "$1" == "$DOC" ]] && return 0; return 2; }
command_error_check() { [[ "$1" == "$DOC" ]] && return 0; return 127; }
old_child_path() { bash -c '[[ "$(queue_slo_missing_checker "$1")" == SLO-OK ]]' _ "$1"; }
expect_status 2 "unchanged fixture refused" mutation_result "$DOC" "$DOC" slo_check "$DOC" "$DOC"
expect_status 1 "surviving mutant refused" mutation_result "$DOC" "$WORK/m1.md" survivor_check "$DOC" "$WORK/m1.md"
expect_status 2 "failed healthy control refused" mutation_result "$DOC" "$WORK/m1.md" broken_control "$DOC" "$WORK/m1.md"
expect_status 2 "legacy unavailable child checker refused" mutation_result "$DOC" "$WORK/m1.md" old_child_path "$DOC" "$WORK/m1.md"
expect_status 2 "missing checker refused" mutation_result "$DOC" "$WORK/m1.md" queue_slo_missing_checker "$DOC" "$WORK/m1.md"
expect_status 2 "mutant setup exit 2 refused" mutation_result "$DOC" "$WORK/m1.md" setup_error_check "$DOC" "$WORK/m1.md"
expect_status 2 "mutant command exit 127 refused" mutation_result "$DOC" "$WORK/m1.md" command_error_check "$DOC" "$WORK/m1.md"
expect_status 2 "unreadable mutant refused" mutation_result "$DOC" "$WORK/m1.md" slo_check "$DOC" "$WORK/missing.md"
expect_status 2 "missing comparison fixture refused" mutation_result "$DOC" "$WORK/missing.md" slo_check "$DOC" "$WORK/m1.md"
expect_status 2 "missing config refused" collision_ok "$WORK/missing.yaml" "$DOC"
expect_status 2 "missing secret bundle refused" secrets_ok "$WORK/missing-bundle"
expect_status 2 "missing dedupe document refused" dedupe_ok "$WORK/missing.md"

printf 'pass=%d fail=%d\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
