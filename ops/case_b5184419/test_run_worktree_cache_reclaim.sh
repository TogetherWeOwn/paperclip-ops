#!/usr/bin/env bash
# Offline suite for run-worktree-cache-reclaim.sh. Mirrors
# ops/case_b1d141af/test_run_worktree_prune.sh: no container engine, no network, no
# credentials. A fake `podman exec` runs the command it is given on a CLEAN
# environment carrying only the variables named by -e, as a real exec does. The
# reclaim tool is a stub that reports what it was given, plus one end-to-end
# case that runs the REAL modules through the real bootstrap.
set -Eeuo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
WRAPPER="$HERE/run-worktree-cache-reclaim.sh"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/worktree-reclaim-wrapper-test.XXXXXXXX")
trap 'rm -rf "$WORK"' EXIT
failures=0
checks=0

ok() { checks=$((checks + 1)); printf '  ok: %s\n' "$1"; }
fail() { checks=$((checks + 1)); printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }

# ---- fake podman --------------------------------------------------------
FAKE_DOCKER="$WORK/docker"
cat > "$FAKE_DOCKER" <<'PY'
#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\n")
if not argv or argv[0] != "exec":
    raise SystemExit(64)
i, names = 1, []
while i < len(argv) and argv[i].startswith("-"):
    if argv[i] in ("-u", "-e"):
        if argv[i] == "-e":
            names.append(argv[i + 1])
        i += 2
    else:
        i += 1
container, cmd = argv[i], argv[i + 1:]
env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent"}
for n in names:
    if n in os.environ:
        env[n] = os.environ[n]
# Harness-only bridge: the wrapper never forwards the allowlist test hatch into
# the container; mirror the outer value here so the REAL core (section 7) can
# run against an unresolvable base.
if "PAPERCLIP_API_ALLOWLIST_TEST" in os.environ:
    env["PAPERCLIP_API_ALLOWLIST_TEST"] = os.environ["PAPERCLIP_API_ALLOWLIST_TEST"]
os.execvpe(cmd[0], cmd, env)
PY
chmod +x "$FAKE_DOCKER"

# ---- a fake release tree: stub modules, same layout as the real one -----
make_release() {
  local dir="$1" code="${2:-0}"
  mkdir -p "$dir/ops/case_b5184419" "$dir/ops/lib" "$dir/lib"
  cat > "$dir/lib/paperclip_api_allowlist.py" <<'PY'
MARK = "allowlist-imported"
PY
  cp "$REPO/lib/paperclip_api_allowlist.sh" "$dir/lib/paperclip_api_allowlist.sh"
  cat > "$dir/ops/lib/atomic_state.py" <<'PY'
MARK = "lib-imported"
PY
  cat > "$dir/ops/case_b5184419/terminal_worktree_cache_reclaim.py" <<PY
import json, os, stat, sys
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.join(here, "..", "..")
argv = sys.argv[1:]
reg = None
if "--registry" in argv:
    reg = argv[argv.index("--registry") + 1]
print(json.dumps({
    "argv": argv,
    "env": sorted(k for k in os.environ if k.startswith(("PAPERCLIP_", "WORKTREE_"))),
    "dir": here,
    "root": os.path.realpath(root),
    "root_mode": stat.S_IMODE(os.stat(root).st_mode),
    "main": __name__,
    "checked_base": os.environ.get("PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"),
    "registry_text": open(reg).read() if reg else None,
}))
sys.exit($code)
PY
}

REL="$WORK/release"
make_release "$REL" 0
REGDIR="$WORK/etc"; mkdir -p "$REGDIR"
printf '{"entries": ["registry-bytes-marker"]}\n' > "$REGDIR/host-timer-registry.json"

# Runs the wrapper with a minimal environment; trailing NAME=value words are env,
# a literal `--` ends them and the rest is wrapper argv.
run_wrapper() {
  local out="$1"; shift
  local envs=() args=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
  [[ "${1:-}" == "--" ]] && shift
  args=("$@")
  : > "$WORK/docker.log"
  env -i PATH="$PATH" HOME="$WORK" \
    FAKE_DOCKER_LOG="$WORK/docker.log" \
    PAPERCLIP_WORKTREE_PRUNE_ENGINE="$FAKE_DOCKER" \
    PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT="${RELEASE:-$REL}" \
    PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY="${REGISTRY:-$REGDIR/host-timer-registry.json}" \
    "${envs[@]}" bash "$WRAPPER" "${args[@]}" > "$out" 2> "$out.err"
}

BASE=(
  PAPERCLIP_WORKTREE_PRUNE_CONTAINER=paperclip-server
  PAPERCLIP_API_URL=http://paperclip.invalid:3100
  PAPERCLIP_API_KEY=secret-value-never-on-argv
  PAPERCLIP_COMPANY_ID=company-1
  # The host-side verdict runs for real; the suite base is .invalid, so every
  # success path opts into the test hatch explicitly.
  PAPERCLIP_API_ALLOWLIST_TEST=1
)

without() { # BASE minus one NAME
  local skip="$1" item
  for item in "${BASE[@]}"; do
    [[ "$item" == "$skip="* ]] || printf '%s\n' "$item"
  done
}

json_field() { python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$1" "$2"; }

GATED_ARGV='["--registry-gate", "routine-worktree-cache-reclaim", "--registry", "@", "--out-dir", "/paperclip/instances/default/data/worktree-cache-reclaim"]'
gated_argv_ok() { # $1 = result json
  python3 - "$1" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
a = r["argv"]
assert a[:2] == ["--registry-gate", "routine-worktree-cache-reclaim"], a
assert a[2] == "--registry" and a[3] == r["root"] + "/registry/host-timer-registry.json", a
assert a[4:] == ["--out-dir", "/paperclip/instances/default/data/worktree-cache-reclaim"], a
assert "--apply" not in a, a
PY
}

# ---- 1. refusals happen before the engine is ever called --------------------
for name in PAPERCLIP_WORKTREE_PRUNE_CONTAINER PAPERCLIP_API_URL PAPERCLIP_API_KEY PAPERCLIP_COMPANY_ID; do
  mapfile -t partial < <(without "$name")
  rc=0; run_wrapper "$WORK/o1" "${partial[@]}" || rc=$?
  if [[ $rc -eq 2 ]] && grep -q "REFUSED: .*$name" "$WORK/o1.err" && [[ ! -s "$WORK/docker.log" ]]; then
    ok "missing $name refuses with exit 2 and never calls the container engine"
  else
    fail "missing $name: rc=$rc log=$(cat "$WORK/docker.log") err=$(cat "$WORK/o1.err")"
  fi
done

for bad in "a b" "-x" "--privileged" "x;y" '$(id)' ""; do
  rc=0; run_wrapper "$WORK/o2" "${BASE[@]}" "PAPERCLIP_WORKTREE_PRUNE_CONTAINER=$bad" || rc=$?
  if [[ $rc -eq 2 && ! -s "$WORK/docker.log" ]]; then
    ok "container name '$bad' is refused"
  else
    fail "container name '$bad' was accepted: rc=$rc"
  fi
done

for bad in "0" "-1" "abc" "1.5"; do
  rc=0; run_wrapper "$WORK/o3" "${BASE[@]}" "PAPERCLIP_WORKTREE_CACHE_RECLAIM_BOUND_SECONDS=$bad" || rc=$?
  if [[ $rc -eq 2 ]] && grep -q "BOUND_SECONDS" "$WORK/o3.err" && [[ ! -s "$WORK/docker.log" ]]; then
    ok "bound '$bad' is refused"
  else
    fail "bound '$bad' was accepted: rc=$rc"
  fi
done

for bad in "--apply" "--registry-gate" "--out-dir" "x"; do
  rc=0; run_wrapper "$WORK/o3b" "${BASE[@]}" -- "$bad" || rc=$?
  if [[ $rc -eq 2 ]] && grep -q "unknown argument" "$WORK/o3b.err" && [[ ! -s "$WORK/docker.log" ]]; then
    ok "argument '$bad' is refused (only --dry-run is accepted)"
  else
    fail "argument '$bad' was accepted: rc=$rc"
  fi
done
rc=0; run_wrapper "$WORK/o3c" "${BASE[@]}" -- --dry-run --apply || rc=$?
[[ $rc -eq 2 && ! -s "$WORK/docker.log" ]] && ok "two arguments are refused" || fail "two arguments accepted: rc=$rc"

mkdir -p "$WORK/empty-release"
rc=0; RELEASE="$WORK/empty-release" run_wrapper "$WORK/o5" "${BASE[@]}" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "reclaim core is missing" "$WORK/o5.err" && [[ ! -s "$WORK/docker.log" ]]; then
  ok "a release without the reclaim core is refused"
else
  fail "missing core not refused: rc=$rc"
fi

for pair in "ops/lib/atomic_state.py:atomic-state lib is missing" \
            "lib/paperclip_api_allowlist.py:allowlist lib is missing" \
            "lib/paperclip_api_allowlist.sh:shell allowlist lib is missing"; do
  file="${pair%%:*}"; msg="${pair#*:}"
  make_release "$WORK/no-x" 0; rm "$WORK/no-x/$file"
  rc=0; RELEASE="$WORK/no-x" run_wrapper "$WORK/o6" "${BASE[@]}" || rc=$?
  if [[ $rc -eq 2 ]] && grep -q "$msg" "$WORK/o6.err" && [[ ! -s "$WORK/docker.log" ]]; then
    ok "a release without $file is refused"
  else
    fail "missing $file not refused: rc=$rc"
  fi
  rm -rf "$WORK/no-x"
done

rc=0; run_wrapper "$WORK/o7" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_ENGINE="$WORK/no-such-docker" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "container engine is not executable" "$WORK/o7.err"; then
  ok "a missing container engine binary is refused"
else
  fail "missing engine not refused: rc=$rc"
fi

# The registry is mandatory in both modes and must be the reviewed file name.
for mode_args in "" "--dry-run"; do
  rc=0; REGISTRY="$WORK/etc/absent.json" run_wrapper "$WORK/o8" "${BASE[@]}" -- $mode_args || rc=$?
  [[ $rc -eq 2 ]] && grep -q "host-timer-registry.json" "$WORK/o8.err" && [[ ! -s "$WORK/docker.log" ]] \
    && ok "a registry with the wrong name is refused (${mode_args:-gated})" || fail "wrong registry name: rc=$rc"
  rc=0; REGISTRY="$WORK/etc/none/host-timer-registry.json" run_wrapper "$WORK/o8b" "${BASE[@]}" -- $mode_args || rc=$?
  [[ $rc -eq 2 ]] && grep -q "registry is missing or unreadable" "$WORK/o8b.err" && [[ ! -s "$WORK/docker.log" ]] \
    && ok "a missing registry refuses with exit 2 (${mode_args:-gated})" || fail "missing registry: rc=$rc"
done

# ---- 2. the argv the tool receives ---------------------------------------
rc=0; run_wrapper "$WORK/a1" "${BASE[@]}" || rc=$?
[[ $rc -eq 0 ]] && ok "a default run succeeds" || fail "default run rc=$rc err=$(cat "$WORK/a1.err")"
gated_argv_ok "$WORK/a1" && ok "default run is registry-gated: no --apply, the registry decides" \
  || fail "default argv: $(json_field "$WORK/a1" argv)"
python3 - "$WORK/a1" <<'PY' && ok "the container receives the reviewed registry bytes" || fail "registry bytes wrong"
import json, sys
assert "registry-bytes-marker" in json.load(open(sys.argv[1]))["registry_text"]
PY

rc=0; run_wrapper "$WORK/a2" "${BASE[@]}" -- --dry-run || rc=$?
python3 - "$WORK/a2" <<'PY' && ok "--dry-run drops the gate and passes no mode flag" || fail "dry-run argv: $(json_field "$WORK/a2" argv)"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["argv"] == ["--out-dir", "/paperclip/instances/default/data/worktree-cache-reclaim"], r["argv"]
PY

# No env variable can add --apply or alter the gate.
rc=0; run_wrapper "$WORK/a3" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_APPLY=1 APPLY=1 DRY_RUN=0 \
  PAPERCLIP_WORKTREE_PRUNE_ARGS="--apply" || rc=$?
gated_argv_ok "$WORK/a3" && ok "no mode or extra-flag env variable changes the argv" \
  || fail "env-driven argv: $(json_field "$WORK/a3" argv)"

# ---- 3. credentials travel by name, never as a value on argv -------------
if grep -q "secret-value-never-on-argv" "$WORK/docker.log"; then
  fail "the API key value appears on the engine argv"
else
  ok "the API key value never appears on the engine argv"
fi
run_wrapper "$WORK/a1" "${BASE[@]}" || true
python3 - "$WORK/docker.log" <<'PY' && ok "podman exec forwards the API variables plus the checked-base delegation by bare name" || fail "-e forms are wrong"
import json, sys
argv = json.loads(open(sys.argv[1]).readline())
assert argv[:4] == ["exec", "-i", "-u", "node"], argv[:4]
pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == "-e"]
assert sorted(v for _, v in pairs) == ["PAPERCLIP_API_ALLOWLIST_CHECKED_BASE", "PAPERCLIP_API_KEY", "PAPERCLIP_API_URL", "PAPERCLIP_COMPANY_ID"], pairs
assert "paperclip-server" in argv
PY

# ---- 4. the host-side allowlist verdict renders before anything runs ----
# Missing allowlist file in enforce mode refuses with exit 2 (the earlier
# failure, now caught on the host). The file path is the production default,
# so this case only runs where that file is absent.
if [[ ! -e /etc/paperclip/allowed-api-hosts ]]; then
  rc=0; run_wrapper "$WORK/v0" "${BASE[@]}" PAPERCLIP_API_ALLOWLIST_TEST= PAPERCLIP_API_ALLOWLIST_MODE=enforce || rc=$?
  if [[ $rc -eq 2 ]] && grep -q "host allowlist verdict" "$WORK/v0.err" && [[ ! -s "$WORK/docker.log" ]]; then
    ok "a missing /etc/paperclip/allowed-api-hosts refuses with exit 2 before the engine runs"
  else
    fail "missing allowlist file: rc=$rc err=$(cat "$WORK/v0.err")"
  fi
fi
rc=0; run_wrapper "$WORK/v1" "${BASE[@]}" PAPERCLIP_API_URL=https://board.example.com || rc=$?
if [[ $rc -eq 2 ]] && grep -q "host allowlist verdict" "$WORK/v1.err" && [[ ! -s "$WORK/docker.log" ]]; then
  ok "a hatch-plus-non-test base is refused on the host before the engine runs"
else
  fail "host verdict refusal wrong: rc=$rc err=$(cat "$WORK/v1.err")"
fi
rc=0; run_wrapper "$WORK/v4" "${BASE[@]}" \
  PAPERCLIP_API_URL=https://evil.example.invalid \
  PAPERCLIP_API_ALLOWLIST_TEST= PAPERCLIP_API_ALLOWLIST_MODE=enforce || rc=$?
if [[ $rc -eq 2 ]] && grep -q "host allowlist verdict" "$WORK/v4.err" && [[ ! -s "$WORK/docker.log" ]]; then
  ok "an enforce-refused (mismatched) base is refused on the host before the engine runs"
else
  fail "enforce verdict refusal wrong: rc=$rc log=$(cat "$WORK/docker.log") err=$(cat "$WORK/v4.err")"
fi
rc=0; run_wrapper "$WORK/v2" "${BASE[@]}" || rc=$?
python3 - "$WORK/v2" <<'PY' && ok "the container receives the checked base verbatim as the delegation" || fail "delegation value wrong"
import json, sys
assert json.load(open(sys.argv[1]))["checked_base"] == "http://paperclip.invalid:3100"
PY
rc=0; run_wrapper "$WORK/v3" "${BASE[@]}" PAPERCLIP_API_ALLOWLIST_CHECKED_BASE=https://evil.example.com || rc=$?
python3 - "$WORK/v3" <<'PY' && ok "an outer CHECKED_BASE preset is overwritten with the checked base" || fail "outer preset survived"
import json, sys
assert json.load(open(sys.argv[1]))["checked_base"] == "http://paperclip.invalid:3100"
PY

# ---- 5. the code that runs in the container -----------------------------
python3 - "$WORK/v2" <<'PY' && ok "the bootstrap runs the tool as __main__ from a 0700 temp dir that is removed afterwards" || fail "bootstrap run is wrong"
import json, os, sys
r = json.load(open(sys.argv[1]))
assert r["main"] == "__main__", r["main"]
assert r["dir"].endswith("/ops/case_b5184419"), r["dir"]
assert r["root_mode"] == 0o700, oct(r["root_mode"])
assert not os.path.exists(r["root"]), "temp dir not removed: " + r["root"]
PY
run_wrapper "$WORK/b1" "${BASE[@]}" || true
python3 - "$WORK/docker.log" <<'PY' && ok "the container command is bounded by timeout inside the exec" || fail "no in-container bound"
import json, sys
argv = json.loads(open(sys.argv[1]).readline())
i = argv.index("paperclip-server")
assert argv[i + 1:i + 7] == ["timeout", "--signal=TERM", "--kill-after=30s", "1500", "python3", "-c"], argv[i + 1:i + 7]
PY

for code in 1 2 3; do
  make_release "$WORK/rel$code" "$code"
  rc=0; RELEASE="$WORK/rel$code" run_wrapper "$WORK/x$code" "${BASE[@]}" || rc=$?
  [[ $rc -eq $code ]] && ok "the tool's exit $code propagates through podman exec" || fail "exit $code became $rc"
done
RELEASE="$WORK/rel3" run_wrapper "$WORK/x3b" "${BASE[@]}" || true
python3 - "$WORK/x3b" <<'PY' && ok "the temp dir is removed even when the tool exits nonzero" || fail "temp dir leaked on failure"
import json, os, sys
assert not os.path.exists(json.load(open(sys.argv[1]))["root"])
PY

# ---- 6. the bootstrap accepts only the reviewed members ------------------
python3 - "$WRAPPER" "$WORK/bootstrap.py" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r"^BOOTSTRAP='\n(.*?)\n'$", text, re.S | re.M)
assert m, "BOOTSTRAP literal not found"
open(sys.argv[2], "w").write(m.group(1))
PY
python3 - "$WORK" <<'PY'
import io, os, sys, tarfile
work = sys.argv[1]
def build(name, entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for ent in entries:
            info = tarfile.TarInfo(ent[0])
            if ent[1] == "file":
                info.size = len(ent[2])
                tar.addfile(info, io.BytesIO(ent[2]))
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = ent[2]
                tar.addfile(info)
    open(os.path.join(work, name), "wb").write(buf.getvalue())
good = [("lib/paperclip_api_allowlist.py", "file", b"MARK = 'x'\n"),
        ("ops/lib/atomic_state.py", "file", b"MARK = 'x'\n"),
        ("registry/host-timer-registry.json", "file", b"{}\n"),
        ("ops/case_b5184419/terminal_worktree_cache_reclaim.py", "file", b"print('ran')\n")]
build("tar_good", good)
build("tar_extra", good + [("ops/evil.py", "file", b"print('evil')\n")])
build("tar_missing", good[:1])
build("tar_noregistry", good[:2] + good[3:])
build("tar_traversal", [("../escape.py", "file", b"x"), good[1]])
build("tar_symlink", good[:3] + [("ops/case_b5184419/terminal_worktree_cache_reclaim.py", "symlink", "/etc/passwd")])
open(os.path.join(work, "tar_garbage"), "wb").write(b"not a tar at all")
PY
before=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'wtreclaim-*' 2>/dev/null | wc -l)
rc=0; python3 -c "$(cat "$WORK/bootstrap.py")" < "$WORK/tar_good" > "$WORK/b_good" 2>&1 || rc=$?
[[ $rc -eq 0 && "$(cat "$WORK/b_good")" == "ran" ]] && ok "the bootstrap runs a well-formed reviewed set" || fail "good tar: rc=$rc $(cat "$WORK/b_good")"
for bad in tar_extra tar_missing tar_noregistry tar_traversal tar_symlink tar_garbage; do
  rc=0; python3 -c "$(cat "$WORK/bootstrap.py")" < "$WORK/$bad" > "$WORK/b_$bad" 2>&1 || rc=$?
  if [[ $rc -ne 0 ]] && ! grep -q "^ran$" "$WORK/b_$bad" && ! grep -q "^evil$" "$WORK/b_$bad"; then
    ok "the bootstrap refuses $bad"
  else
    fail "the bootstrap accepted $bad: rc=$rc $(cat "$WORK/b_$bad")"
  fi
done
after=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'wtreclaim-*' 2>/dev/null | wc -l)
[[ "$before" == "$after" ]] && ok "no wtreclaim-* temp dir survives a refusal" || fail "temp dirs leaked: $before -> $after"
[[ ! -e "$WORK/escape.py" && ! -e "$(dirname "$WORK")/escape.py" ]] && ok "a traversal member wrote nothing outside the temp dir" || fail "traversal wrote outside"

# ---- 7. the REAL core through the REAL bootstrap -------------------------
# The base is unreachable, so the core stops at its fresh evaluation and
# removes nothing; what this proves is that the shipped bytes import, accept the
# bundled registry and fail closed. --dry-run keeps it write-free by construction.
rc=0
env -i PATH="$PATH" HOME="$WORK" FAKE_DOCKER_LOG="$WORK/docker.log" \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE="$FAKE_DOCKER" PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT="$REPO" \
  PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY="$REGDIR/host-timer-registry.json" \
  PAPERCLIP_WORKTREE_PRUNE_CONTAINER=paperclip-server PAPERCLIP_API_URL=http://127.0.0.1:1 \
  PAPERCLIP_API_ALLOWLIST_TEST=1 PAPERCLIP_API_KEY=k PAPERCLIP_COMPANY_ID=c \
  bash "$WRAPPER" --dry-run > "$WORK/real.out" 2> "$WORK/real.err" || rc=$?
if [[ $rc -ne 0 && $rc -ne 126 && $rc -ne 127 ]] && grep -q "^mode: dry-run" "$WORK/real.err" \
    && grep -q "URLError" "$WORK/real.err"; then
  ok "the real core via the real bootstrap starts, reports mode: dry-run and stops nonzero on an unreachable API (rc=$rc)"
else
  fail "real modules: rc=$rc out=$(head -c 400 "$WORK/real.out") err=$(head -c 600 "$WORK/real.err")"
fi
rc=0
env -i PATH="$PATH" HOME="$WORK" FAKE_DOCKER_LOG="$WORK/docker.log" \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE="$FAKE_DOCKER" PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT="$REPO" \
  PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY="$REGDIR/host-timer-registry.json" \
  PAPERCLIP_WORKTREE_PRUNE_CONTAINER=paperclip-server PAPERCLIP_API_URL=http://127.0.0.1:1 \
  PAPERCLIP_API_ALLOWLIST_TEST=1 PAPERCLIP_API_KEY=k PAPERCLIP_COMPANY_ID=c \
  bash "$WRAPPER" > "$WORK/real2.out" 2> "$WORK/real2.err" || rc=$?
# The fixture registry has no matching entry: the gate must dry-run, never apply.
if grep -q "^mode: dry-run" "$WORK/real2.err" && ! grep -q "^mode: apply" "$WORK/real2.err"; then
  ok "the real gate reads the bundled registry and dry-runs when the entry is not an active writer"
else
  fail "real gated run: rc=$rc err=$(head -c 600 "$WORK/real2.err")"
fi

# An active-writer entry is the ONLY way the gated run chooses apply; the API is
# unreachable here, so the tool stops at its fresh evaluation and deletes nothing.
mkdir -p "$WORK/etc-active"
printf '{"entries": [{"id": "routine-worktree-cache-reclaim", "state": "active", "role": "writer", "writes": ["fs:worktree-dep-caches"]}]}\n' \
  > "$WORK/etc-active/host-timer-registry.json"
rc=0
env -i PATH="$PATH" HOME="$WORK" FAKE_DOCKER_LOG="$WORK/docker.log" \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE="$FAKE_DOCKER" PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT="$REPO" \
  PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY="$WORK/etc-active/host-timer-registry.json" \
  PAPERCLIP_WORKTREE_PRUNE_CONTAINER=paperclip-server PAPERCLIP_API_URL=http://127.0.0.1:1 \
  PAPERCLIP_API_ALLOWLIST_TEST=1 PAPERCLIP_API_KEY=k PAPERCLIP_COMPANY_ID=c \
  bash "$WRAPPER" > "$WORK/real3.out" 2> "$WORK/real3.err" || rc=$?
if [[ $rc -ne 0 ]] && grep -q "^mode: apply (registry entry" "$WORK/real3.err" && grep -q "URLError" "$WORK/real3.err"; then
  ok "an active-writer registry entry selects apply, and an unreachable API stops it before any delete"
else
  fail "active registry run: rc=$rc err=$(head -c 400 "$WORK/real3.err")"
fi

# ---- 8. static properties of the wrapper bytes ---------------------------
exec_lines=$(grep -v '^[[:space:]]*#' "$WRAPPER")
if grep -qE -- '--apply|--force|git[[:space:]]+worktree' <<<"$exec_lines"; then
  fail "the wrapper carries --apply, a force flag or runs git worktree itself"
else
  ok "the wrapper never passes --apply (the registry decides) and never runs git itself"
fi
grep -q 'PAPERCLIP_API_KEY=' <<<"$exec_lines" && fail "the wrapper assigns the API key" || ok "the wrapper never assigns or echoes the API key"
grep -q 'PAPERCLIP_API_ALLOWLIST_TEST' <<<"$exec_lines" && fail "the wrapper references the allowlist test hatch" || ok "the wrapper never references the allowlist test hatch"

printf '%d checks, %d failure(s)\n' "$checks" "$failures"
[[ $failures -eq 0 ]]
