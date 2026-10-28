#!/usr/bin/env bash
# Offline suite for run-worktree-prune.sh (case_b1d141af). No container engine, no network, no
# credentials: a fake `podman exec` runs the command it is given on a CLEAN
# environment that carries only the variables named by -e, which is what a real
# container exec does. The pruner itself is replaced by a stub that reports what
# it was given, plus one end-to-end case that runs the REAL modules through the
# real bootstrap and proves they fail closed against an unreachable API.
set -Eeuo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
WRAPPER="$HERE/run-worktree-prune.sh"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/worktree-prune-wrapper-test.XXXXXXXX")
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
log = os.environ["FAKE_DOCKER_LOG"]
with open(log, "a") as fh:
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
# Test-harness-only hatch bridge for section 7: the wrapper deliberately does
# not forward the allowlist test hatch into the container, so mirror the
# outer value only here. Production forwarding still follows ENV_ARGS.
if "PAPERCLIP_API_ALLOWLIST_TEST" in os.environ:
    env["PAPERCLIP_API_ALLOWLIST_TEST"] = os.environ["PAPERCLIP_API_ALLOWLIST_TEST"]
os.execvpe(cmd[0], cmd, env)
PY
chmod +x "$FAKE_DOCKER"

# ---- a fake release tree: stub modules, same layout as the real one -----
make_release() {
  local dir="$1" code="${2:-0}"
  mkdir -p "$dir/ops/case_b1d141af" "$dir/ops/case_b5184419" "$dir/ops/lib" "$dir/lib"
  cat > "$dir/lib/paperclip_api_allowlist.py" <<'PY'
MARK = "allowlist-imported"
PY
  # The host-side verdict runs for real: the wrapper sources this lib.
  cp "$REPO/lib/paperclip_api_allowlist.sh" "$dir/lib/paperclip_api_allowlist.sh"
  cat > "$dir/ops/lib/atomic_state.py" <<'PY'
MARK = "lib-imported"
PY
  cat > "$dir/ops/case_b5184419/terminal_worktree_cache_reclaim.py" <<'PY'
MARK = "core-imported"
PY
  cat > "$dir/ops/case_b1d141af/terminal_worktree_prune.py" <<PY
import json, os, stat, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "case_b5184419"))
import terminal_worktree_cache_reclaim as core
here = os.path.dirname(os.path.abspath(__file__))
print(json.dumps({
    "argv": sys.argv[1:],
    "env": sorted(k for k in os.environ if k.startswith(("PAPERCLIP_", "WORKTREE_"))),
    "core": core.MARK,
    "dir": here,
    "dir_mode": stat.S_IMODE(os.stat(os.path.join(here, "..", "..")).st_mode),
    "main": __name__,
    "checked_base": os.environ.get("PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"),
}))
sys.exit($code)
PY
}

REL="$WORK/release"
make_release "$REL" 0

# Runs the wrapper with a minimal environment; EXTRA is "NAME=value ..." words.
run_wrapper() {
  local out="$1"; shift
  : > "$WORK/docker.log"
  env -i PATH="$PATH" HOME="$WORK" \
    FAKE_DOCKER_LOG="$WORK/docker.log" \
    PAPERCLIP_WORKTREE_PRUNE_ENGINE="$FAKE_DOCKER" \
    PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT="${RELEASE:-$REL}" \
    "$@" bash "$WRAPPER" > "$out" 2> "$out.err"
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

for bad in "-1" "abc" "1.5" "1 2" "0x10"; do
  rc=0; run_wrapper "$WORK/o3" "${BASE[@]}" "PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE=$bad" || rc=$?
  if [[ $rc -eq 2 ]] && grep -q "MAX_REMOVE" "$WORK/o3.err" && [[ ! -s "$WORK/docker.log" ]]; then
    ok "cap '$bad' is refused"
  else
    fail "cap '$bad' was accepted: rc=$rc"
  fi
done

rc=0; run_wrapper "$WORK/o4" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_BOUND_SECONDS=0 || rc=$?
[[ $rc -eq 2 && ! -s "$WORK/docker.log" ]] && ok "a zero bound is refused" || fail "zero bound accepted: rc=$rc"

mkdir -p "$WORK/empty-release"
rc=0; RELEASE="$WORK/empty-release" run_wrapper "$WORK/o5" "${BASE[@]}" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "pruner module is missing" "$WORK/o5.err" && [[ ! -s "$WORK/docker.log" ]]; then
  ok "a release without the pruner module is refused"
else
  fail "missing module not refused: rc=$rc"
fi

make_release "$WORK/no-core" 0; rm "$WORK/no-core/ops/case_b5184419/terminal_worktree_cache_reclaim.py"
rc=0; RELEASE="$WORK/no-core" run_wrapper "$WORK/o6" "${BASE[@]}" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "reclaim core is missing" "$WORK/o6.err"; then
  ok "a release without the reclaim core is refused"
else
  fail "missing core not refused: rc=$rc"
fi

make_release "$WORK/no-lib" 0; rm "$WORK/no-lib/ops/lib/atomic_state.py"
rc=0; RELEASE="$WORK/no-lib" run_wrapper "$WORK/o6b" "${BASE[@]}" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "atomic-state lib is missing" "$WORK/o6b.err"; then
  ok "a release without the atomic-state lib is refused"
else
  fail "missing lib not refused: rc=$rc"
fi

make_release "$WORK/no-allowlist" 0; rm "$WORK/no-allowlist/lib/paperclip_api_allowlist.py"
rc=0; RELEASE="$WORK/no-allowlist" run_wrapper "$WORK/o6c" "${BASE[@]}" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "allowlist lib is missing" "$WORK/o6c.err"; then
  ok "a release without the allowlist lib is refused"
else
  fail "missing allowlist lib not refused: rc=$rc"
fi

make_release "$WORK/no-sh-allowlist" 0; rm "$WORK/no-sh-allowlist/lib/paperclip_api_allowlist.sh"
rc=0; RELEASE="$WORK/no-sh-allowlist" run_wrapper "$WORK/o6d" "${BASE[@]}" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "shell allowlist lib is missing" "$WORK/o6d.err" && [[ ! -s "$WORK/docker.log" ]]; then
  ok "a release without the shell allowlist lib is refused"
else
  fail "missing shell allowlist lib not refused: rc=$rc"
fi

# Default engine selection (no PAPERCLIP_WORKTREE_PRUNE_ENGINE): the first executable of the
# candidate list wins, podman first. Marker-writing shims show which one actually ran.
# Keep candidate paths whitespace-free; the inherited TMPDIR may contain spaces.
SHIMS=$(mktemp -d /tmp/worktree-prune-engine-test.XXXXXXXX)
trap 'rm -rf "$WORK" "$SHIMS"' EXIT
for name in podman docker; do
  printf '#!/bin/sh\n: > "%s/ran-%s"\nexec "%s" "$@"\n' "$WORK" "$name" "$FAKE_DOCKER" > "$SHIMS/shim-$name"
  chmod +x "$SHIMS/shim-$name"
done
CANDIDATES="$SHIMS/shim-podman $SHIMS/shim-docker"
rm -f "$WORK"/ran-*
rc=0; run_wrapper "$WORK/e1" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_ENGINE= \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE_CANDIDATES="$CANDIDATES" || rc=$?
if [[ $rc -eq 0 && -e "$WORK/ran-podman" && ! -e "$WORK/ran-docker" ]]; then
  ok "default engine is podman when both are installed"
else
  fail "podman-first selection wrong: rc=$rc err=$(cat "$WORK/e1.err")"
fi
rm -f "$WORK"/ran-*
rc=0; run_wrapper "$WORK/e2" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_ENGINE= \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE_CANDIDATES="$SHIMS/no-such-podman $SHIMS/shim-docker" || rc=$?
if [[ $rc -eq 0 && -e "$WORK/ran-docker" && ! -e "$WORK/ran-podman" ]]; then
  ok "a docker-only host falls back to docker"
else
  fail "docker fallback wrong: rc=$rc err=$(cat "$WORK/e2.err")"
fi
rm -f "$WORK"/ran-*
rc=0; run_wrapper "$WORK/e3" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_ENGINE= \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE_CANDIDATES="$SHIMS/no-such-podman $SHIMS/no-such-docker" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "container engine is not executable: $SHIMS/no-such-docker" "$WORK/e3.err" \
    && [[ ! -s "$WORK/docker.log" ]]; then
  ok "a host with neither engine refuses before anything runs"
else
  fail "neither-engine not refused: rc=$rc err=$(cat "$WORK/e3.err")"
fi
rm -f "$WORK"/ran-*
rc=0; run_wrapper "$WORK/e4" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_ENGINE="$SHIMS/shim-docker" \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE_CANDIDATES="$SHIMS/shim-podman" || rc=$?
if [[ $rc -eq 0 && -e "$WORK/ran-docker" && ! -e "$WORK/ran-podman" ]]; then
  ok "an explicit engine override wins over the candidate list"
else
  fail "override precedence wrong: rc=$rc err=$(cat "$WORK/e4.err")"
fi

rc=0; run_wrapper "$WORK/o7" "${BASE[@]}" \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE="$WORK/no-such-docker" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "container engine is not executable" "$WORK/o7.err"; then
  ok "a missing container engine binary is refused"
else
  fail "missing docker not refused: rc=$rc err=$(cat "$WORK/o7.err")"
fi

# ---- 2. the argv the pruner receives -------------------------------------
rc=0; run_wrapper "$WORK/a1" "${BASE[@]}" || rc=$?
[[ $rc -eq 0 ]] && ok "a default run succeeds" || fail "default run rc=$rc err=$(cat "$WORK/a1.err")"
[[ "$(json_field "$WORK/a1" argv)" == '["--apply", "--max-remove", "0"]' ]] \
  && ok "default cap is 0: --apply is literal, --max-remove 0 (observe-only)" \
  || fail "default argv: $(json_field "$WORK/a1" argv)"

rc=0; run_wrapper "$WORK/a2" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE=37 || rc=$?
[[ "$(json_field "$WORK/a2" argv)" == '["--apply", "--max-remove", "37"]' ]] \
  && ok "the env file's cap is the only thing that changes the argv" \
  || fail "capped argv: $(json_field "$WORK/a2" argv)"

# No env variable can drop --apply or add a flag: the mode bit is reviewed bytes.
rc=0; run_wrapper "$WORK/a3" "${BASE[@]}" PAPERCLIP_WORKTREE_PRUNE_APPLY=0 DRY_RUN=1 APPLY=0 \
  PAPERCLIP_WORKTREE_PRUNE_ARGS="--min-age-hours 0" || rc=$?
[[ "$(json_field "$WORK/a3" argv)" == '["--apply", "--max-remove", "0"]' ]] \
  && ok "no mode or extra-flag env variable changes the argv" \
  || fail "env-driven argv: $(json_field "$WORK/a3" argv)"

# Positional arguments to the wrapper are ignored, not forwarded.
: > "$WORK/docker.log"
rc=0
env -i PATH="$PATH" HOME="$WORK" FAKE_DOCKER_LOG="$WORK/docker.log" \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE="$FAKE_DOCKER" PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT="$REL" \
  "${BASE[@]}" bash "$WRAPPER" --max-remove 9999 --min-age-hours 0 > "$WORK/a4" 2>/dev/null || rc=$?
[[ "$(json_field "$WORK/a4" argv)" == '["--apply", "--max-remove", "0"]' ]] \
  && ok "positional arguments to the wrapper are not forwarded" \
  || fail "positional argv leaked: $(json_field "$WORK/a4" argv)"

# ---- 3. credentials travel by name, never as a value on argv -------------
if grep -q "secret-value-never-on-argv" "$WORK/docker.log"; then
  fail "the API key value appears on the engine argv"
else
  ok "the API key value never appears on the engine argv"
fi
python3 - "$WORK/docker.log" <<'PY' && ok "podman exec forwards the API variables plus the checked-base delegation by bare name" || fail "-e forms are wrong"
import json, sys
argv = json.loads(open(sys.argv[1]).readline())
assert argv[:4] == ["exec", "-i", "-u", "node"], argv[:4]
pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == "-e"]
assert sorted(v for _, v in pairs) == ["PAPERCLIP_API_ALLOWLIST_CHECKED_BASE", "PAPERCLIP_API_KEY", "PAPERCLIP_API_URL", "PAPERCLIP_COMPANY_ID"], pairs
assert "PAPERCLIP_API_ALLOWLIST_TEST" not in [v for _, v in pairs], "hatch must never be forwarded"
assert "paperclip-server" in argv
PY
# The inner env carries the forwarded four plus the hatch, which the fake
# engine mirrors in (harness-only bridge for section 7; never on the -e argv).
python3 - "$WORK/a1" <<'PY' && ok "the container sees the forwarded variables (hatch mirrored by the harness only)" || fail "inner env wrong"
import json, sys
env = json.load(open(sys.argv[1]))["env"]
assert env == ["PAPERCLIP_API_ALLOWLIST_CHECKED_BASE", "PAPERCLIP_API_ALLOWLIST_TEST", "PAPERCLIP_API_KEY", "PAPERCLIP_API_URL", "PAPERCLIP_COMPANY_ID"], env
PY

# ---- 4. the kill switch variable is forwarded only when set -------------
rc=0; run_wrapper "$WORK/k1" "${BASE[@]}" WORKTREE_PRUNE_DISABLED=1 || rc=$?
if python3 - "$WORK/k1" "$WORK/docker.log" <<'PY'
import json, sys
env = json.load(open(sys.argv[1]))["env"]
argv = json.loads(open(sys.argv[2]).readline())
assert "WORKTREE_PRUNE_DISABLED" in env, env
assert argv.count("WORKTREE_PRUNE_DISABLED") == 1
PY
then ok "WORKTREE_PRUNE_DISABLED reaches the pruner when the env file sets it"
else fail "kill switch not forwarded"; fi
python3 - "$WORK/a1" <<'PY' && ok "an unset kill switch is not invented" || fail "kill switch leaked"
import json, sys
assert "WORKTREE_PRUNE_DISABLED" not in json.load(open(sys.argv[1]))["env"]
PY

# ---- 4b. the host-side allowlist verdict renders before anything runs ----
# Hatch + non-test base: the verdict refuses. Hermetic: the hatch refusal
# precedes the host-file read, so no /etc state is consulted.
rc=0; run_wrapper "$WORK/v1" "${BASE[@]}" PAPERCLIP_API_URL=https://board.example.com || rc=$?
if [[ $rc -eq 2 ]] && grep -q "host allowlist verdict" "$WORK/v1.err" && [[ ! -s "$WORK/docker.log" ]]; then
  ok "a hatch-plus-non-test base is refused on the host before the engine runs"
else
  fail "host verdict refusal wrong: rc=$rc err=$(cat "$WORK/v1.err")"
fi
# The forwarded delegation is the checked base, byte for byte ...
python3 - "$WORK/a1" <<'PY' && ok "the container receives the checked base verbatim as the delegation" || fail "delegation value wrong"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["checked_base"] == "http://paperclip.invalid:3100", r["checked_base"]
PY
# ... even when the outer environment presets a different value.
rc=0; run_wrapper "$WORK/v3" "${BASE[@]}" PAPERCLIP_API_ALLOWLIST_CHECKED_BASE=https://evil.example.com || rc=$?
python3 - "$WORK/v3" <<'PY' && ok "an outer CHECKED_BASE preset is overwritten with the checked base" || fail "outer preset survived"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["checked_base"] == "http://paperclip.invalid:3100", r["checked_base"]
PY
# ---- 5. the code that runs in the container -----------------------------
python3 - "$WORK/a1" <<'PY' && ok "the bootstrap runs the pruner as __main__ and its sibling core imports" || fail "bootstrap run is wrong"
import json, os, sys
r = json.load(open(sys.argv[1]))
assert r["main"] == "__main__", r["main"]
assert r["core"] == "core-imported", r["core"]
assert r["dir"].endswith("/ops/case_b1d141af"), r["dir"]
assert r["dir_mode"] == 0o700, oct(r["dir_mode"])
assert not os.path.exists(r["dir"]), "temp dir not removed: " + r["dir"]
PY
python3 - "$WORK/docker.log" <<'PY' && ok "the container command is bounded by timeout inside the exec" || fail "no in-container bound"
import json, sys
argv = json.loads(open(sys.argv[1]).readline())
i = argv.index("paperclip-server")
assert argv[i + 1:i + 7] == ["timeout", "--signal=TERM", "--kill-after=30s", "1500", "python3", "-c"], argv[i + 1:i + 7]
assert argv[-3:] == ["--apply", "--max-remove", "0"], argv[-3:]
PY

# Enforce wiring pin (placed after the bound assertion because run_wrapper
# clears docker.log, which that assertion reads): deleting the wrapper's
# paperclip_allowlist_check call must fail this case — the wrapper would
# run the engine against the evil base.
rc=0; run_wrapper "$WORK/v4" "${BASE[@]}" \
  PAPERCLIP_API_URL=https://evil.example.invalid \
  PAPERCLIP_API_ALLOWLIST_TEST= PAPERCLIP_API_ALLOWLIST_MODE=enforce || rc=$?
if [[ $rc -eq 2 ]] && grep -q "host allowlist verdict" "$WORK/v4.err" && [[ ! -s "$WORK/docker.log" ]]; then
  ok "an enforce-refused base is refused on the host before the engine runs"
else
  fail "enforce verdict refusal wrong: rc=$rc log=$(cat "$WORK/docker.log") err=$(cat "$WORK/v4.err")"
fi

for code in 1 2 3; do
  make_release "$WORK/rel$code" "$code"
  rc=0; RELEASE="$WORK/rel$code" run_wrapper "$WORK/x$code" "${BASE[@]}" || rc=$?
  [[ $rc -eq $code ]] && ok "the pruner's exit $code propagates through podman exec" \
    || fail "exit $code became $rc"
done

# A removal failure must stay loud: the temp dir is still cleaned up.
RELEASE="$WORK/rel3" run_wrapper "$WORK/x3b" "${BASE[@]}" || true
python3 - "$WORK/x3b" <<'PY' && ok "the temp dir is removed even when the pruner exits nonzero" || fail "temp dir leaked on failure"
import json, os, sys
assert not os.path.exists(json.load(open(sys.argv[1]))["dir"])
PY

# ---- 6. the bootstrap accepts only the reviewed quad --------------------
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
            kind = ent[1]
            if kind == "file":
                data = ent[2]
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = ent[2]
                tar.addfile(info)
    open(os.path.join(work, name), "wb").write(buf.getvalue())
good = [("lib/paperclip_api_allowlist.py", "file", b"MARK = 'x'\n"),
        ("ops/lib/atomic_state.py", "file", b"MARK = 'x'\n"),
        ("ops/case_b5184419/terminal_worktree_cache_reclaim.py", "file", b"MARK = 'x'\n"),
        ("ops/case_b1d141af/terminal_worktree_prune.py", "file", b"print('ran')\n")]
build("tar_good", good)
build("tar_extra", good + [("ops/evil.py", "file", b"print('evil')\n")])
build("tar_missing", good[:1])
build("tar_traversal", [("../escape.py", "file", b"x"), good[1]])
build("tar_symlink", [good[0], ("ops/case_b1d141af/terminal_worktree_prune.py", "symlink", "/etc/passwd")])
open(os.path.join(work, "tar_garbage"), "wb").write(b"not a tar at all")
PY
# The bootstrap temp dir is a child of TMPDIR; a traversal member would land beside it.
rm -f "${TMPDIR:-/tmp}/escape.py" "$(dirname "$WORK")/escape.py"
before=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'wtprune-*' 2>/dev/null | wc -l)
rc=0; python3 -c "$(cat "$WORK/bootstrap.py")" < "$WORK/tar_good" > "$WORK/b_good" 2>&1 || rc=$?
[[ $rc -eq 0 && "$(cat "$WORK/b_good")" == "ran" ]] && ok "the bootstrap runs a well-formed reviewed quad" || fail "good tar: rc=$rc $(cat "$WORK/b_good")"
for bad in tar_extra tar_missing tar_traversal tar_symlink tar_garbage; do
  rc=0; python3 -c "$(cat "$WORK/bootstrap.py")" < "$WORK/$bad" > "$WORK/b_$bad" 2>&1 || rc=$?
  if [[ $rc -ne 0 ]] && ! grep -q "^ran$" "$WORK/b_$bad" && ! grep -q "^evil$" "$WORK/b_$bad"; then
    ok "the bootstrap refuses $bad"
  else
    fail "the bootstrap accepted $bad: rc=$rc $(cat "$WORK/b_$bad")"
  fi
done
after=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'wtprune-*' 2>/dev/null | wc -l)
[[ "$before" == "$after" ]] && ok "no wtprune-* temp dir survives a refusal" || fail "temp dirs leaked: $before -> $after"
[[ ! -e "$WORK/escape.py" && ! -e "$(dirname "$WORK")/escape.py" ]] && ok "a traversal member wrote nothing outside the temp dir" || fail "traversal wrote outside"

# ---- 7. the REAL modules through the REAL bootstrap fail closed ---------
rc=0
env -i PATH="$PATH" HOME="$WORK" FAKE_DOCKER_LOG="$WORK/docker.log" \
  PAPERCLIP_WORKTREE_PRUNE_ENGINE="$FAKE_DOCKER" PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT="$REPO" \
  PAPERCLIP_WORKTREE_PRUNE_CONTAINER=paperclip-server PAPERCLIP_API_URL=http://127.0.0.1:1 \
  PAPERCLIP_API_ALLOWLIST_TEST=1 \
  PAPERCLIP_API_KEY=k PAPERCLIP_COMPANY_ID=c WORKTREE_PRUNE_DISABLED=1 \
  bash "$WRAPPER" > "$WORK/real.out" 2> "$WORK/real.err" || rc=$?
if [[ $rc -eq 2 ]] && grep -q "URLError" "$WORK/real.err" && grep -q "KILL-SWITCH" "$WORK/real.err"; then
  ok "the real pruner via the real bootstrap exits 2 (API unreachable) and removes nothing"
else
  fail "real modules: rc=$rc out=$(head -c 400 "$WORK/real.out") err=$(head -c 400 "$WORK/real.err")"
fi

# ---- 8. static properties of the wrapper bytes ---------------------------
if grep -qE -- '--force|git[[:space:]]+worktree' <<<"$(grep -v '^[[:space:]]*#' "$WRAPPER")"; then
  fail "the wrapper carries a force flag or runs git worktree itself"
else
  ok "the wrapper carries no force flag and never runs git itself"
fi
[[ "$(grep -c -- '--apply' <(grep -v '^[[:space:]]*#' "$WRAPPER"))" == 1 ]] \
  && ok "--apply appears exactly once in the executable lines (a literal on argv)" \
  || fail "--apply count changed"
grep -q 'PAPERCLIP_API_KEY=' <<<"$(grep -v '^[[:space:]]*#' "$WRAPPER")" \
  && fail "the wrapper assigns the API key" || ok "the wrapper never assigns or echoes the API key"

printf '%d checks, %d failure(s)\n' "$checks" "$failures"
[[ $failures -eq 0 ]]
