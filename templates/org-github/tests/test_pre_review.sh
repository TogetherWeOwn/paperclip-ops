#!/usr/bin/env bash
# ===========================================================================
# Offline regression suite for the staged pre-review gate.
#
# Builds THROWAWAY git repos in mktemp, commits fixtures, and runs
# pre-review.sh against them. No network, no credentials, nothing leaves
# the temp directory. Exit status only: PASS/FAIL counters, non-zero on any
# failure. Assertion targets are exit codes and STATUS/BUCKET table rows,
# never human-readable prose beyond the bucket token.
#
# Git comes from the fast-git PATH shadow below: some environments put a
# slow credential-shim `git` on PATH that would also pollute captures.
# ===========================================================================
# Strict mode like the rest of this repo's suites: assertion chains are
# `expect && ok || bad` (a failed expect falls through to `bad`, never
# aborts), and run_check preserves the script's exit code explicitly.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRE="${PRE_REVIEW_SH:-$HERE/../pre-review/pre-review.sh}"
STAGED="$HERE/../pre-review"
GIT="git" # resolved after the fast-git PATH shadow below

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }

[ -x "$PRE" ] || { echo "no executable pre-review.sh at $PRE" >&2; exit 2; }
unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# Fast git first: some environments put a slow credential-shim `git` on
# PATH (~10s per spawn). The staged script calls plain `git`, so shadow it
# with the system git when one exists outside the current PATH.
mkdir -p "$WORK/bin"
export PATH="$WORK/bin:$PATH"
if REALGIT="$(PATH="$(getconf PATH)" command -v git 2>/dev/null)" && [[ -n "$REALGIT" ]]; then
  export REAL_GIT_BIN="$REALGIT"
  cat > "$WORK/bin/git" <<'EOF'
#!/usr/bin/env bash
if [[ "${FAIL_AUTHZ_DIFF:-0}" == 1 && "${1:-}" == --literal-pathspecs && "${2:-}" == diff ]]; then
  exit 73
fi
if [[ "${FAIL_AUTHZ_READ:-0}" == 1 && "${1:-}" == cat-file && "${2:-}" == blob ]]; then
  exit 74
fi
if [[ "${FAIL_AUTHZ_EXIST:-0}" == 1 && "${1:-}" == cat-file && "${2:-}" == -e ]]; then
  exit 73
fi
exec "$REAL_GIT_BIN" "$@"
EOF
  chmod +x "$WORK/bin/git"
fi
# Stop BASH_ENV from re-prepending the credential shim on child shells,
# which would bury the shadow above (BASH_ENV= + real git first).
export BASH_ENV=/dev/null
# Cargo is stubbed so these lockfile checks stay offline and hermetic. The
# status simulates Cargo's --locked result for dependency-neutral/stale cases.
FAKE_CARGO_LOG="$WORK/cargo.log"
export FAKE_CARGO_LOG
cat > "$WORK/bin/cargo" <<'EOF'
#!/usr/bin/env bash
printf 'cwd=%s config=%s cargo_home=%s lock=%s args=%s\n' "$PWD" \
  "$([[ -e .cargo/config.toml ]] && echo present || echo absent)" \
  "${CARGO_HOME:-unset}" \
  "$(grep -E '^version' Cargo.lock 2>/dev/null | head -n 1 || echo missing)" "$*" >> "$FAKE_CARGO_LOG"
if [[ " $* " == *" --locked "* && ! -f Cargo.lock ]]; then
  echo 'error: cannot create the lock file because --locked was passed' >&2
  exit 101
fi
if [[ "${FAKE_CARGO_STATUS:-0}" -ne 0 ]]; then
  echo 'error: lock file needs to be updated but --locked was passed' >&2
  exit "$FAKE_CARGO_STATUS"
fi
EOF
chmod +x "$WORK/bin/cargo"
N=0; REPO_DIR=""; CHANGE_BASE=""

# new_repo: sets REPO_DIR; seeds one conventional base commit. No stdout.
new_repo() {
  N=$((N+1))
  REPO_DIR="$WORK/r$N"
  mkdir -p "$REPO_DIR"
  $GIT -C "$REPO_DIR" init -qb main >&2
  $GIT -C "$REPO_DIR" config user.email t@example.invalid >&2
  $GIT -C "$REPO_DIR" config user.name Test >&2
  $GIT -C "$REPO_DIR" config commit.gpgsign false >&2
  echo "base" > "$REPO_DIR/base.txt"
  $GIT -C "$REPO_DIR" add -A >&2
  $GIT -C "$REPO_DIR" commit -qm "chore(repo): seed fixture" >&2
}

# commit_change <subject>: commits the worktree, sets CHANGE_BASE to HEAD~1.
commit_change() {
  $GIT -C "$REPO_DIR" add -A >&2
  $GIT -C "$REPO_DIR" commit -qm "$1" >&2
  CHANGE_BASE="$($GIT -C "$REPO_DIR" rev-parse HEAD~1)"
}

OUT=""; RC=0
run_check() {
  : > "$FAKE_CARGO_LOG"
  OUT="$(cd "$REPO_DIR" && bash "$PRE" --base "$CHANGE_BASE" "$@" 2>&1)" && RC=0 || RC=$?
}
has_row() { grep -qE "^$1  +$2" <<<"$OUT"; }

# --- 1. clean change passes ---------------------------------------------------
new_repo
echo 'print("hi")' > "$REPO_DIR/app.py"
echo 'def test_app(): pass' > "$REPO_DIR/test_app.py"
echo '# docs' > "$REPO_DIR/notes.md"
commit_change "feat(app): add app with test and doc"
run_check
[[ "$RC" -eq 0 ]] && has_row PASS untested-branch && ok "clean change exits 0" \
  || bad "clean change exits 0 (rc=$RC)"

# --- 2. code without test fails ----------------------------------------------
new_repo
echo 'print("hi")' > "$REPO_DIR/app.py"
commit_change "feat(app): add app without test"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL untested-branch && ok "untested branch fails" \
  || bad "untested branch fails (rc=$RC)"
new_repo
echo 'print("hi")' > "$REPO_DIR/app.py"
echo 'x = 1' > "$REPO_DIR/contest.py"
commit_change "feat(app): add app with lookalike test name"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL untested-branch && ok "lookalike test name fails" \
  || bad "lookalike test name fails (rc=$RC)"

# --- 3. TODO warns, does not fail ---------------------------------------------
new_repo
printf 'x = 1\n# TODO: handle zero\n' > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with todo"
run_check
[[ "$RC" -eq 0 ]] && has_row WARN dead-state && ok "TODO warns only" \
  || bad "TODO warns only (rc=$RC)"

# --- 4. secret fails -----------------------------------------------------------
new_repo
# Fake fixture secret, assembled at runtime so this file holds no secret
# pattern (the staged script must stay clean under its own scan).
FAKE_CORE="abcdefghijklmnopqrstuvwx123456"
printf 'token = "ghp_%s"\n' "$FAKE_CORE" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with token"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "secret fails" \
  || bad "secret fails (rc=$RC)"

# --- 5. conflict markers fail --------------------------------------------------
new_repo
printf '<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> branch\n' > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add conflicted file"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL conflicts-stale && ok "conflict markers fail" \
  || bad "conflict markers fail (rc=$RC)"

# --- 6. shell without set -e fails ---------------------------------------------
new_repo
printf '#!/usr/bin/env bash\nrm -rf "$1"\n' > "$REPO_DIR/clean.sh"
printf 'pass\n' > "$REPO_DIR/test_clean.sh"
commit_change "feat(ops): add cleaner without guard"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL unhandled-errors && ok "unguarded shell fails" \
  || bad "unguarded shell fails (rc=$RC)"
new_repo
printf '#!/usr/bin/env bash\nset -uo pipefail\nrm -rf "$1"\n' > "$REPO_DIR/clean.sh"
printf 'pass\n' > "$REPO_DIR/test_clean.py"
commit_change "feat(ops): add cleaner without errexit"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL unhandled-errors && ok "pipefail without -e fails" \
  || bad "pipefail without -e fails (rc=$RC)"

# --- 7. lockfile pair ------------------------------------------------------------
new_repo
echo '{"name":"x"}' > "$REPO_DIR/package.json"
commit_change "feat(deps): bump manifest without lockfile"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL generated-drift && ok "lockfile drift fails" \
  || bad "lockfile drift fails (rc=$RC)"
new_repo
echo '{"name":"x"}' > "$REPO_DIR/package.json"
echo '{"lock":1}' > "$REPO_DIR/package-lock.json"
commit_change "feat(deps): bump manifest with lockfile"
run_check
has_row FAIL generated-drift && bad "paired lockfile must not fail" \
  || ok "paired lockfile passes"
new_repo
mkdir -p "$REPO_DIR/sub"
echo '{"name":"x"}' > "$REPO_DIR/sub/package.json"
commit_change "feat(deps): bump nested manifest without lockfile"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL generated-drift && ok "nested lockfile drift fails" \
  || bad "nested lockfile drift fails (rc=$RC)"

# Cargo's own locked metadata check distinguishes a dependency-neutral
# manifest edit from one that needs a Cargo.lock change.
new_repo
printf 'Cargo.lock\n' > "$REPO_DIR/.gitignore"
mkdir -p "$REPO_DIR/cfg"
printf 'build.rustc = "/tmp/untrusted-compiler-wrapper"\n' > "$REPO_DIR/cfg/config.toml"
ln -s cfg "$REPO_DIR/.cargo"
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[lib]\ncrate-type = ["rlib"]\n' > "$REPO_DIR/Cargo.toml"
$GIT -C "$REPO_DIR" add -A >&2
$GIT -C "$REPO_DIR" commit -qm "chore(cargo): seed manifest"
CHANGE_BASE="$($GIT -C "$REPO_DIR" rev-parse HEAD)"
printf 'version = 3\n' > "$REPO_DIR/Cargo.lock"
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[lib]\ncrate-type = ["rlib", "cdylib"]\n' > "$REPO_DIR/Cargo.toml"
commit_change "chore(cargo): add crate type"
FAKE_CARGO_STATUS=0 run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL generated-drift \
  && $GIT -C "$REPO_DIR" check-ignore -q Cargo.lock \
  && grep -q 'metadata --offline --format-version 1 --manifest-path .*Cargo.toml' "$FAKE_CARGO_LOG" \
  && ! grep -q 'metadata --locked' "$FAKE_CARGO_LOG" \
  && grep -Eq -- '--manifest-path /[^[:space:]]+/Cargo.toml' "$FAKE_CARGO_LOG" \
  && grep -q '^cwd=' "$FAKE_CARGO_LOG" \
  && ! grep -Fq "cwd=$REPO_DIR config=" "$FAKE_CARGO_LOG" \
  && ! grep -Fq -- "--manifest-path $REPO_DIR/" "$FAKE_CARGO_LOG" \
  && ! grep -q 'cargo_home=.*/cargo-home' "$FAKE_CARGO_LOG" \
  && grep -q 'config=absent' "$FAKE_CARGO_LOG" \
  && grep -q 'build.rustc="rustc"' "$FAKE_CARGO_LOG" \
  && grep -q 'build.rustc-wrapper=""' "$FAKE_CARGO_LOG" \
  && ok "dependency-neutral Cargo manifest uses isolated committed snapshot" \
  || bad "dependency-neutral Cargo manifest passes with ignored lockfile (rc=$RC)"

new_repo
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n' > "$REPO_DIR/Cargo.toml"
printf 'version = 3\n' > "$REPO_DIR/Cargo.lock"
$GIT -C "$REPO_DIR" add -A >&2
$GIT -C "$REPO_DIR" commit -qm "chore(cargo): seed tracked lockfile"
CHANGE_BASE="$($GIT -C "$REPO_DIR" rev-parse HEAD)"
printf 'version = 4\n' > "$REPO_DIR/Cargo.lock"
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\ntokio = "1"\n' > "$REPO_DIR/Cargo.toml"
$GIT -C "$REPO_DIR" add Cargo.toml >&2
$GIT -C "$REPO_DIR" commit -qm "feat(cargo): add dependency without saved lock update"
FAKE_CARGO_STATUS=1 run_check
if [[ "$RC" -eq 1 ]] && has_row FAIL generated-drift \
  && grep -q 'metadata --locked --offline --format-version 1' "$FAKE_CARGO_LOG" \
  && grep -q 'lock=version = 3' "$FAKE_CARGO_LOG" \
  && grep -q '^version = 4$' "$REPO_DIR/Cargo.lock"; then
  ok "Cargo metadata validates committed lock, not unsaved worktree lock"
else
  bad "Cargo metadata validates committed lock, not unsaved worktree lock (rc=$RC)"
fi

new_repo
printf '[package]\nname = "x"\nversion = "0.1.0"\n' > "$REPO_DIR/Cargo.toml"
$GIT -C "$REPO_DIR" add -A >&2
$GIT -C "$REPO_DIR" commit -qm "chore(cargo): seed manifest"
CHANGE_BASE="$($GIT -C "$REPO_DIR" rev-parse HEAD)"
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n' > "$REPO_DIR/Cargo.toml"
commit_change "feat(cargo): add dependency"
FAKE_CARGO_STATUS=1 run_check
[[ "$RC" -eq 1 ]] && has_row FAIL generated-drift \
  && grep -q 'metadata --offline --format-version 1' "$FAKE_CARGO_LOG" \
  && ! grep -q 'metadata --locked' "$FAKE_CARGO_LOG" \
  && ok "Cargo dependency change without lockfile fails" \
  || bad "Cargo dependency change without lockfile fails (rc=$RC)"

# A non-ASCII path must still detect its tracked lockfile and require --locked.
new_repo
$GIT -C "$REPO_DIR" config core.quotepath true
mkdir -p "$REPO_DIR/crätes"
printf '[package]\nname = "x"\nversion = "0.1.0"\n' > "$REPO_DIR/crätes/Cargo.toml"
printf 'version = 3\n' > "$REPO_DIR/crätes/Cargo.lock"
$GIT -C "$REPO_DIR" add -A >&2
$GIT -C "$REPO_DIR" commit -qm "chore(cargo): seed unicode manifest and lockfile"
CHANGE_BASE="$($GIT -C "$REPO_DIR" rev-parse HEAD)"
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n' > "$REPO_DIR/crätes/Cargo.toml"
commit_change "feat(cargo): add dependency under unicode path"
FAKE_CARGO_STATUS=1 run_check
[[ "$RC" -eq 1 ]] && has_row FAIL generated-drift \
  && grep -q 'metadata --locked --offline --format-version 1' "$FAKE_CARGO_LOG" \
  && ok "non-ASCII Cargo path keeps tracked lockfile check locked" \
  || bad "non-ASCII Cargo path keeps tracked lockfile check locked (rc=$RC)"

new_repo
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[lib]\ncrate-type = ["rlib"]\n' > "$REPO_DIR/Cargo.toml"
printf 'version = 3\n' > "$REPO_DIR/Cargo.lock"
$GIT -C "$REPO_DIR" add -A >&2
$GIT -C "$REPO_DIR" commit -qm "chore(cargo): seed manifest and lockfile"
CHANGE_BASE="$($GIT -C "$REPO_DIR" rev-parse HEAD)"
printf '[package]\nname = "x"\nversion = "0.1.0"\n\n[lib]\ncrate-type = ["rlib", "cdylib"]\n' > "$REPO_DIR/Cargo.toml"
printf 'version = 4\n' > "$REPO_DIR/Cargo.lock"
commit_change "chore(cargo): update manifest and lockfile"
FAKE_CARGO_STATUS=0 run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL generated-drift \
  && ok "paired Cargo manifest and lockfile pass" \
  || bad "paired Cargo manifest and lockfile pass (rc=$RC)"

# --- 8. commit subject ------------------------------------------------------------
new_repo
echo hi > "$REPO_DIR/other.txt"
commit_change "wip stuff."
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL spec-mismatch && ok "bad subject fails" \
  || bad "bad subject fails (rc=$RC)"

# --- 9. PR body sections -----------------------------------------------------------
new_repo
echo 'print(1)' > "$REPO_DIR/a.py"
printf 'def test_a(): pass\n' > "$REPO_DIR/test_a.py"
commit_change "feat(a): add a"
printf '## What\n\nDid things.\n\n## Risks\n\n' > "$WORK/body.md"
run_check --pr-body "$WORK/body.md"
[[ "$RC" -eq 1 ]] && has_row FAIL spec-mismatch && ok "empty body section fails" \
  || bad "empty body section fails (rc=$RC)"
printf '## What\n\nDid things.\n\n## Risks\n\nNone.\n' > "$WORK/body-full.md"
run_check --pr-body "$WORK/body-full.md" --pr-title "feat(a): add a"
[[ "$RC" -eq 0 ]] && ok "full body and title pass" \
  || bad "full body and title pass (rc=$RC)"
run_check --pr-title "oops no type here"
[[ "$RC" -eq 1 ]] && ok "bad title fails" \
  || bad "bad title fails (rc=$RC)"
run_check --pr-title "feat(a): add a."
[[ "$RC" -eq 1 ]] && ok "trailing-period title fails" \
  || bad "trailing-period title fails (rc=$RC)"
run_check --pr-title "feat(a): $(printf 'x%.0s' {1..95})"
[[ "$RC" -eq 1 ]] && ok "overlong title fails" \
  || bad "overlong title fails (rc=$RC)"

# --- 10. public patterns -----------------------------------------------------------
new_repo
printf 'see internal ref ABC-123 for context\n' > "$REPO_DIR/a.py"
printf 'def test_a(): pass\n' > "$REPO_DIR/test_a.py"
commit_change "feat(a): add a"
PRE_REVIEW_PRIVATE_PATTERNS='ABC-[0-9]+' run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "private pattern fails when configured" \
  || bad "private pattern fails when configured (rc=$RC)"
run_check
has_row SKIP authz && ok "pattern scan skips when unconfigured" \
  || bad "pattern scan skips when unconfigured"
PRE_REVIEW_PRIVATE_PATTERNS='' run_check --public
has_row SKIP authz && ok "public scan without patterns skips" \
  || bad "public scan without patterns skips (rc=$RC)"

# --- 11. CI logic ------------------------------------------------------------------
new_repo
mkdir -p "$REPO_DIR/.github/workflows"
printf 'name: x\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n' > "$REPO_DIR/.github/workflows/x.yml"
commit_change "ci(x): add workflow without timeout"
run_check
[[ "$RC" -eq 0 ]] && has_row WARN ci-logic && ok "missing timeout warns" \
  || bad "missing timeout warns (rc=$RC)"
new_repo
mkdir -p "$REPO_DIR/.github/workflows"
printf 'name: x\non:\n  pull_request_target:\n    types: [opened]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: echo hi\n' > "$REPO_DIR/.github/workflows/x.yml"
commit_change "ci(x): add target workflow"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL ci-logic && ok "pull_request_target fails" \
  || bad "pull_request_target fails (rc=$RC)"
new_repo
mkdir -p "$REPO_DIR/.github/workflows"
printf 'name: x\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n        with:\n          persist-credentials: false\n' > "$REPO_DIR/.github/workflows/x.yml"
commit_change "ci(x): add pinned workflow"
run_check
[[ "$RC" -eq 0 ]] && ! has_row WARN authz && ! has_row WARN perf && ok "pinned checkout without creds passes" \
  || bad "pinned checkout without creds passes (rc=$RC)"
new_repo
mkdir -p "$REPO_DIR/.github/workflows"
printf 'name: x\non: [push]\njobs:\n    build:\n        runs-on: ubuntu-latest\n        steps:\n            - run: echo hi\n' > "$REPO_DIR/.github/workflows/x.yml"
commit_change "ci(x): add indented workflow without timeout"
run_check
[[ "$RC" -eq 0 ]] && has_row WARN ci-logic && ok "indented job without timeout warns" \
  || bad "indented job without timeout warns (rc=$RC)"
new_repo
echo x > "$REPO_DIR/f.txt"
commit_change "chore(repo): touch base"
mkdir -p "$REPO_DIR/.github/workflows"
printf 'name: x\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: sleep 30\n      - run: git clo'"ne https://example.invalid/r.git"'\n' > "$REPO_DIR/.github/workflows/slow.yml"
# slow.yml stays untracked; both perf WARNs must still fire on it.
OUT="$(cd "$REPO_DIR" && bash "$PRE" --base "$CHANGE_BASE" 2>&1)" && RC=0 || RC=$?
has_row WARN perf && grep -q "sleep in added workflow steps" <<<"$OUT" \
  && ok "untracked workflow sleep warns" \
  || bad "untracked workflow sleep warns (rc=$RC)"
# Match on "--depth", not the warn title: the title itself names the bare
# clone command and would trip the scanner's self-check on this file.
grep -q "use --depth" <<<"$OUT" && ok "untracked full clone warns" \
  || bad "untracked full clone warns (rc=$RC)"

# --- 12. docs broken link -------------------------------------------------------------
new_repo
printf 'print(1)\n' > "$REPO_DIR/a.py"
printf 'def test_a(): pass\n' > "$REPO_DIR/test_a.py"
printf 'see [missing](./nope.md)\n' > "$REPO_DIR/notes.md"
commit_change "docs(a): add doc with broken link"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL docs-drift && ok "broken doc link fails" \
  || bad "broken doc link fails (rc=$RC)"

# --- 13. perf / error extras ---------------------------------------------------------
new_repo
printf 'set -euo pipefail\ncurl https://example.invalid/x\n' > "$REPO_DIR/fetch.sh"
printf 'pass\n' > "$REPO_DIR/test_fetch.sh"
commit_change "feat(ops): add fetcher"
run_check
has_row WARN perf && ok "curl without retry warns" \
  || bad "curl without retry warns"
new_repo
printf 'try:\n    f()\nexcept:\n    pass\n' > "$REPO_DIR/a.py"
printf 'def test_a(): pass\n' > "$REPO_DIR/test_a.py"
commit_change "feat(a): add bare except"
run_check
has_row WARN unhandled-errors && ok "bare except warns" \
  || bad "bare except warns"
new_repo
printf 'const f = () => {};\ntry { g(); } catch(e) {}\n' > "$REPO_DIR/a.js"
printf 'test\n' > "$REPO_DIR/a.test.js"
commit_change "feat(a): add empty catch"
run_check
has_row WARN unhandled-errors && ok "empty catch warns" \
  || bad "empty catch warns"

# --- 14. stale base and dirty tree warn -----------------------------------------------------
new_repo
$GIT -C "$REPO_DIR" checkout -qb feature >&2
echo 'print(2)' > "$REPO_DIR/a.py"
printf 'def test_a(): pass\n' > "$REPO_DIR/test_a.py"
commit_change "feat(a): add a on branch"
$GIT -C "$REPO_DIR" checkout -q main >&2
echo more >> "$REPO_DIR/base.txt"
$GIT -C "$REPO_DIR" commit -qam "chore(repo): advance main" >&2
$GIT -C "$REPO_DIR" update-ref refs/remotes/origin/main main >&2
$GIT -C "$REPO_DIR" checkout -q feature >&2
run_check
has_row WARN conflicts-stale && ok "stale base warns" \
  || bad "stale base warns"
echo dirty > "$REPO_DIR/dirty.txt"
run_check
has_row WARN conflicts-stale && ok "dirty tree warns" \
  || bad "dirty tree warns"

# --- 15. untracked files are reviewed ---------------------------------------------------
new_repo
printf 'def test_sneaky(): pass\n' > "$REPO_DIR/test_sneaky.py"
commit_change "feat(a): add sneaky test"
FAKE_CORE="abcdefghijklmnopqrstuvwx123456"
printf 'token = "ghp_%s"\n' "$FAKE_CORE" > "$REPO_DIR/sneaky.py"
# sneaky.py stays untracked; the secret must still fail the scan.
OUT="$(cd "$REPO_DIR" && bash "$PRE" --base "$CHANGE_BASE" 2>&1)" && RC=0 || RC=$?
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "untracked secret fails" \
  || bad "untracked secret fails (rc=$RC)"
new_repo
printf 'def test_binary(): pass\n' > "$REPO_DIR/test_binary.py"
commit_change "test(auth): seed binary scanning fixture"
printf 'x\000token_payload\000' > "$REPO_DIR/binary.dat"
OUT="$(cd "$REPO_DIR" && bash "$PRE" --base "$CHANGE_BASE" 2>&1)" && RC=0 || RC=$?
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "untracked binary fails closed" \
  || bad "untracked binary fails closed (rc=$RC)"

# --- 16. self-scan: the script stays clean under its own detectors -----------
new_repo
cp "$PRE" "$REPO_DIR/pre-review-copy.sh"
printf 'pass\n' > "$REPO_DIR/test_copy.py"
commit_change "feat(a): vendor scanner copy"
run_check
[[ "$RC" -eq 0 ]] && ! grep -q "debug leftover" <<<"$OUT" \
  && ! has_row FAIL authz && ! grep -q "persist-credentials: true added" <<<"$OUT" \
  && ok "scanner copy stays clean" \
  || bad "scanner copy stays clean (rc=$RC)"

# --- 17. table shape + public-safe staging ---------------------------------------------------
new_repo
echo x > "$REPO_DIR/f.txt"
commit_change "chore(repo): touch"
run_check
grep -qE '^STAT +BUCKET +DETAIL' <<<"$(head -n 1 <<<"$OUT")" && ok "table header shape" \
  || bad "table header shape"
grep -qE '^FAIL=[0-9]+ WARN=[0-9]+' <<<"$(tail -n 1 <<<"$OUT")" && ok "summary line shape" \
  || bad "summary line shape"
# No exclusion list: any match fails, so no crafted line can bypass it.
if grep -rEn 'TOG-[0-9]+|PAP-[0-9]+|deploymentexample|agent://' "$STAGED/pre-review.sh" "$STAGED/RUBRIC.md" "$STAGED/AGENT-RULE.md" "$STAGED/README.md"; then
  bad "staged shipping files must stay public-safe"
else
  ok "staged shipping files public-safe"
fi

# --- 18. reviewer round 2: workspaces, sql, paren links, flag values -----------
# Workspace: nested manifest with the ROOT lockfile passes both directions.
new_repo
mkdir -p "$REPO_DIR/sub"
echo '{"name":"x"}' > "$REPO_DIR/sub/package.json"
echo '{"lock":1}' > "$REPO_DIR/package-lock.json"
commit_change "feat(deps): bump workspace manifest and root lockfile"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL generated-drift \
  && ok "workspace manifest plus root lockfile passes" \
  || bad "workspace manifest plus root lockfile passes (rc=$RC)"
# Lock-only update (audit-fix): a live but untouched manifest is not drift.
new_repo
echo '{"name":"x"}' > "$REPO_DIR/package.json"
echo '{"lock":1}' > "$REPO_DIR/package-lock.json"
commit_change "feat(deps): seed manifest and lockfile"
echo '{"lock":2}' > "$REPO_DIR/package-lock.json"
commit_change "fix(deps): audit-fix lockfile only"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL generated-drift \
  && ok "lock-only update passes" \
  || bad "lock-only update passes (rc=$RC)"
# Dangling lockfile (no manifest at its side or above) still fails.
new_repo
mkdir -p "$REPO_DIR/sub"
echo '{"lock":1}' > "$REPO_DIR/sub/pnpm-lock.yaml"
commit_change "feat(deps): add dangling lockfile"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL generated-drift && ok "dangling lockfile fails" \
  || bad "dangling lockfile fails (rc=$RC)"
# .sql counts as code: a migration-only change must FAIL untested-branch.
new_repo
echo 'CREATE TABLE t (id INT);' > "$REPO_DIR/0003_new.sql"
commit_change "feat(db): add migration without test"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL untested-branch && ok "migration-only change fails" \
  || bad "migration-only change fails (rc=$RC)"
# Parenthesized doc path: a link to the existing ./api_(v2).md must not fail.
new_repo
printf 'print(1)\n' > "$REPO_DIR/a.py"
printf 'def test_a(): pass\n' > "$REPO_DIR/test_a.py"
echo '# api' > "$REPO_DIR/api_(v2).md"
printf 'see [api](./api_(v2).md)\n' > "$REPO_DIR/notes.md"
commit_change "docs(a): link parenthesized path"
run_check
! has_row FAIL docs-drift && ok "parenthesized link passes" \
  || bad "parenthesized link passes (rc=$RC)"
# A flag with a missing value exits 2 with usage, never a silent abort.
run_check --base
[[ "$RC" -eq 2 ]] && grep -q "missing value" <<<"$OUT" \
  && ok "missing flag value exits 2" \
  || bad "missing flag value exits 2 (rc=$RC)"

# --- 19. gate fixes: code-like values pass authz, merges skip spec --------
# A parsed-filename value (method-call RHS) is code, not a credential.
new_repo
printf 'token = match.group(1)\n' > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): parse filename token"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "method-call value passes authz" \
  || bad "method-call value passes authz (rc=$RC)"
# A long callee name (12+ chars before the paren) is still a call, not a
# credential: the bare-value check must anchor at the end of the value.
new_repo
printf 'token = create_access_token(data)\n' > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): mint token via helper"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "long call value passes authz" \
  || bad "long call value passes authz (rc=$RC)"
# Literals still fail: quoted, bare and dotted-quoted forms. Fixture
# secrets are assembled at runtime so this file holds no secret pattern.
new_repo
FAKE_CORE="abcdefghijklmnopqrstuvwx123456"
printf 'token = "ghp_%s"\n' "$FAKE_CORE" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with token"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "quoted literal still fails" \
  || bad "quoted literal still fails (rc=$RC)"
new_repo
BARE="supersecret123456"
printf 'password = %s\n' "$BARE" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with bare password"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "bare literal still fails" \
  || bad "bare literal still fails (rc=$RC)"
new_repo
DOTTED="correct.horse.battery.staple.99"
printf 'password = "%s"\n' "$DOTTED" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with dotted password"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "dotted literal still fails" \
  || bad "dotted literal still fails (rc=$RC)"
# Bare key/value text inside a quoted string is still a credential: the
# value boundary must include the closing quote, or this line passes authz.
new_repo
QUOTED_EMBED="supersecret123456"
printf 'cfg = "password: %s"\n' "$QUOTED_EMBED" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with embedded password"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "quoted-embedded bare value still fails" \
  || bad "quoted-embedded bare value still fails (rc=$RC)"
# Merge subjects are generated by git and pushed history cannot be
# rewritten: a true merge must not fail spec-mismatch, while a bad
# non-merge subject still must.
new_repo
$GIT -C "$REPO_DIR" checkout -qb feature >&2
printf 'print(1)\n' > "$REPO_DIR/a.py"
printf 'def test_a(): pass\n' > "$REPO_DIR/test_a.py"
$GIT -C "$REPO_DIR" add -A >&2
$GIT -C "$REPO_DIR" commit -qm "feat(a): add a on branch" >&2
$GIT -C "$REPO_DIR" checkout -q main >&2
PRE_MERGE="$($GIT -C "$REPO_DIR" rev-parse HEAD)"
$GIT -C "$REPO_DIR" merge --no-ff --no-edit feature >&2
CHANGE_BASE="$PRE_MERGE"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL spec-mismatch && ok "merge subject passes" \
  || bad "merge subject passes (rc=$RC)"
new_repo
echo hi > "$REPO_DIR/other.txt"
commit_change "oops not conventional"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL spec-mismatch && ok "bad non-merge subject still fails" \
  || bad "bad non-merge subject still fails (rc=$RC)"

# --- 20. gate fixes: Rust type and getter shapes pass authz -------------------
# Struct fields with generic types hold no literal: only the shape name
# plus angle brackets is committed. Fixture lines mirror real auth code.
new_repo
printf 'pub password: Option<String>,\n' > "$REPO_DIR/auth.rs"
printf 'pub token: Option<String>,\n' >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): add credential fields"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "rust option fields pass authz" \
  || bad "rust option fields pass authz (rc=$RC)"
# Runtime getter chains resolve at runtime: no literal is committed.
new_repo
printf 'let token = creds.token.clone().unwrap_or_default();\n' > "$REPO_DIR/auth.rs"
printf 'let raw_password = creds.password.clone().unwrap_or_default();\n' >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): read credentials at runtime"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "rust getter chains pass authz" \
  || bad "rust getter chains pass authz (rc=$RC)"
# Long generic names hold no literal: the `<` after a 12+ type-path run is a
# type argument list, not a credential boundary.
new_repo
printf 'password: %s<%s>\n' CredentialEnvelope String > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): add credential envelope field"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "long generic type passes authz" \
  || bad "long generic type passes authz (rc=$RC)"
# Turbofish deserialisation paths hold no literal: `::from_str::<T>` after a
# credential-named binding is a type-parameterised call, not a value.
new_repo
printf 'let token = serde_json::from_str::<TokenResponse>\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): parse token response"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "turbofish path passes authz" \
  || bad "turbofish path passes authz (rc=$RC)"
# A bare secret glued to a bracket is left unflagged: `<` is neither in
# the bare-value run nor a trailing boundary (pre-review.sh section 7).
# Split across adjacent strings so this file never holds the shape.
new_repo
BARE_GLUED="hunter2""hunter2"
printf 'password = %s<\n' "$BARE_GLUED" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): add password default"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "bare secret glued to bracket passes authz" \
  || bad "bare secret glued to bracket passes authz (rc=$RC)"
# A quoted value containing a bracket is still a literal: `<` is allowed
# inside the quoted run.
new_repo
QUOTED_BRACKET='ab<'"cdefghijklmno"
printf 'token = "%s"\n' "$QUOTED_BRACKET" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add token default"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "quoted bracket value still fails authz" \
  || bad "quoted bracket value still fails authz (rc=$RC)"
# Rust type detection is structural and file-scoped: prefixed fields, nested
# generic wrappers, restricted visibility and trailing comments remain clean.
new_repo
printf 'pub(crate) access_token: Arc<Option<String>>, // runtime-owned\n' > "$REPO_DIR/auth.rs"
printf 'pub raw_%s: %s<%s>,\n' password CredentialEnvelope String >> "$REPO_DIR/auth.rs"
printf 'pub password: CredentialEnvelope <Vec<HashMap<String, String>>>,\n' >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): add wrapped credential fields"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
  && ok "nested Rust credential types pass authz" \
  || bad "nested Rust credential types pass authz (rc=$RC)"
# Generic arguments must themselves be recognized type syntax; arbitrary
# credential-shaped contents are not excused by a type-shaped wrapper.
new_repo
printf 'pub password: SecretString<super_secret_value_12345>,\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan ambiguous generic arguments"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "credential-shaped Rust generic argument fails authz" \
  || bad "credential-shaped Rust generic argument fails authz (rc=$RC)"
new_repo
printf 'pub password: Option<supermansecret99>,\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): reject credential-shaped short generic argument"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "credential-shaped short generic argument fails authz" \
  || bad "credential-shaped short generic argument fails authz (rc=$RC)"
for credential_field in TOKEN SECRET Token Secret; do
  new_repo
  printf 'pub %s: Option<supermansecret99>,\n' "$credential_field" > "$REPO_DIR/auth.rs"
  printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
  commit_change "test(auth): reject mixed-case generic credential field"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "generic credential field fails regardless of case: $credential_field" \
    || bad "generic credential field fails regardless of case: $credential_field (rc=$RC)"
done
new_repo
printf 'pub password /*c*/ : Option<supermansecret99>,\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): reject commented generic credential field"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "comment between credential field and colon fails closed" \
  || bad "comment between credential field and colon fails closed (rc=$RC)"
# A one-segment function turbofish is runtime code too.
new_repo
printf 'let raw_token = parse::<TokenResponse>();\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): parse one-segment token type"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
  && ok "single-segment turbofish passes authz" \
  || bad "single-segment turbofish passes authz (rc=$RC)"
# Numeric suffixes are valid identifiers in Rust types and must not make
# runtime type syntax look like a credential literal.
new_repo
printf 'pub password: %s<%s>,\n' CredentialEnvelope2 String > "$REPO_DIR/auth.rs"
printf 'pub raw_%s: %s,\n' password VeryLongPasswordType >> "$REPO_DIR/auth.rs"
printf 'pub access_token: crate::auth::%s<%s>,\n' CredentialEnvelope String >> "$REPO_DIR/auth.rs"
printf 'pub ref_%s: &%s,\n' password VeryLongType >> "$REPO_DIR/auth.rs"
printf 'pub raw_%s: [%s; 32],\n' secret VeryLongType >> "$REPO_DIR/auth.rs"
printf '// a generic type note, with no credential-shaped value\n' >> "$REPO_DIR/auth.rs"
printf 'let password: %s<%s> = get_password();\n' CredentialEnvelope String >> "$REPO_DIR/auth.rs"
printf 'let token = parse::<Token2>();\n' >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): use numbered credential types"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
  && ok "numbered Rust types pass authz" \
  || bad "numbered Rust types pass authz (rc=$RC)"
# Long lowercase identifiers remain suspicious rather than being treated as types.
new_repo
printf 'pub password: %s,\n' secrettypewithlongname > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): reject lowercase Rust credential-shaped value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "lowercase Rust credential-shaped value fails" \
  || bad "lowercase Rust credential-shaped value fails (rc=$RC)"
# Common secret-wrapper and hash types are declarations, not credentials.
new_repo
printf 'pub password: %s,\n' SecretString > "$REPO_DIR/auth.rs"
printf 'pub raw_password: %s,\n' PasswordHash >> "$REPO_DIR/auth.rs"
printf 'pub secret: %s,\n' SensitiveString >> "$REPO_DIR/auth.rs"
printf 'pub password: %s,\n' crate::secrecy::SecretString >> "$REPO_DIR/auth.rs"
printf 'pub PASSWORD: Option<String>,\n' >> "$REPO_DIR/auth.rs"
printf 'let password: %s = get_password();\n' SecretWrapper >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): support standard credential wrapper types"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
  && ok "Rust wrapper types and initialized bindings pass authz" \
  || bad "Rust wrapper types and initialized bindings pass authz (rc=$RC)"
# Attribute access is code, not a bare credential value.
new_repo
printf 'password = %s\n' config.password > "$REPO_DIR/app.py"
printf 'pub password: %s,\n' self.password > "$REPO_DIR/auth.rs"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): allow dotted runtime field access"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
  && ok "dotted runtime field access passes authz" \
  || bad "dotted runtime field access passes authz (rc=$RC)"
# Lowercase alpha strings before angle suffixes are not generic types.
new_repo
FIXTURE_VALUE="xxmyhostname"
FIXTURE_SUFFIX="local"
printf 'password: %s<%s>\n' "$FIXTURE_VALUE" "$FIXTURE_SUFFIX" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): reject lowercase credential with angle suffix"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "lowercase Rust angle credential still fails" \
  || bad "lowercase Rust angle credential still fails (rc=$RC)"
# Trailing comments on a type field remain in the secret scan.
new_repo
FAKE_AKIA="AKIA""IOSFODNN7EXAMPLE"
FIXTURE_VALUE="abcdefghijklmnop"
printf 'pub password: String, // %s\n' "$FAKE_AKIA" > "$REPO_DIR/auth.rs"
printf 'pub raw_secret: String, // password: %s\n' "$FIXTURE_VALUE" >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan Rust trailing comments"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "Rust type comments still detect credentials" \
  || bad "Rust type comments still detect credentials (rc=$RC)"
# Type-shaped text in a Rust comment remains part of the scan, not a type exemption.
new_repo
printf '// password: %s<%s> in a comment\n' CredentialEnvelope String > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): retain Rust comment match spans"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "type-shaped Rust comment remains scanned" \
  || bad "type-shaped Rust comment remains scanned (rc=$RC)"
# Escaped configuration text is not Rust type syntax and must stay detectable.
new_repo
printf 'const CFG: &str = "password: \\\"AbcdefghijklType\\\"";\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan escaped Rust configuration"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "escaped Rust configuration fails authz" \
  || bad "escaped Rust configuration fails authz (rc=$RC)"
# Prose in a Rust comment is not a quoted credential literal.
new_repo
printf 'pub password: Option<String>, // note \\"runtime-owned credential value\\"\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): ignore quoted Rust comment prose"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
  && ok "quoted Rust comment prose passes authz" \
  || bad "quoted Rust comment prose passes authz (rc=$RC)"
# A CamelCase value ending in generic-looking syntax outside Rust is not a type.
new_repo
printf 'password: %s<%s>\n' Abcdefghijkl String > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "test(auth): scan non-Rust angle values"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "non-Rust angle value still fails authz" \
  || bad "non-Rust angle value still fails authz (rc=$RC)"
# A quoted value in Rust source remains a credential literal, not a type.
new_repo
FIXTURE_VALUE="Q7m2V9z4L6p8R1s3"
printf 'let password = "%s";\n' "$FIXTURE_VALUE" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): reject literal Rust credential"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "quoted Rust credential still fails" \
  || bad "quoted Rust credential still fails (rc=$RC)"
# Typed Rust bindings and string constructors still expose the literal value.
new_repo
FIXTURE_VALUE="Q7m2V9z4L6p8R1s3"
printf 'let password: String = "%s";\n' "$FIXTURE_VALUE" > "$REPO_DIR/auth.rs"
printf 'let password = String::from("%s");\n' "$FIXTURE_VALUE" >> "$REPO_DIR/auth.rs"
printf 'let password: Option<String> = Some("%s");\n' "$FIXTURE_VALUE" >> "$REPO_DIR/auth.rs"
printf 'const PASSWORD: &str = "%s";\n' "$FIXTURE_VALUE" >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): reject typed Rust string credentials"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "typed Rust string credentials still fail" \
  || bad "typed Rust string credentials still fail (rc=$RC)"
new_repo
FIXTURE_VALUE="abcdefghijklmnop"
printf 'let password: String = %s;\n' "$FIXTURE_VALUE" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): detect typed binding value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "credential-shaped typed binding value fails" \
  || bad "credential-shaped typed binding value fails (rc=$RC)"
new_repo
printf 'let password: SecretString =foo.hunter2password12345;\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan credential in typed initializer"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "credential in dotted typed initializer fails" \
  || bad "credential in dotted typed initializer fails (rc=$RC)"
new_repo
printf 'let password: SecretString =foo::hunter2password12345;\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan credential in typed path initializer"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "credential in path typed initializer fails" \
  || bad "credential in path typed initializer fails (rc=$RC)"
new_repo
printf 'let token: String =abcdefghijklm;\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan adjacent typed initializer value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "adjacent typed initializer value fails" \
  || bad "adjacent typed initializer value fails (rc=$RC)"
new_repo
printf 'let token: String = abcdefghijkl==;\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan padded typed initializer value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "padded typed initializer value fails" \
  || bad "padded typed initializer value fails (rc=$RC)"
new_repo
PADDED_VALUE="abcdefghijkl=="
printf 'token=%s;\n' "$PADDED_VALUE" > "$REPO_DIR/config.txt"
commit_change "test(auth): scan padded non-Rust value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "padded non-Rust value fails" \
  || bad "padded non-Rust value fails (rc=$RC)"
new_repo
printf 'password: abcdefghijklmnop.\n' > "$REPO_DIR/config.txt"
commit_change "test(auth): scan sentence-terminated value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "sentence-terminated value fails" \
  || bad "sentence-terminated value fails (rc=$RC)"
new_repo
printf 'password("abcdefghijklmnop")\n' > "$REPO_DIR/config.txt"
commit_change "test(auth): scan credential passed as call argument"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "credential passed to named call fails" \
  || bad "credential passed to named call fails (rc=$RC)"
new_repo
printf 'password("abcdefghijkl==")\n' > "$REPO_DIR/config.txt"
commit_change "test(auth): scan padded credential call argument"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "padded credential call argument fails" \
  || bad "padded credential call argument fails (rc=$RC)"
# Base64 padding after an unquoted call value is part of the value, not a
# boundary. The value rides in a variable so this file holds no adjacent
# keyword/value shape.
new_repo
UNQUOTED_PADDED_CALL="QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
printf 'x = token(%s)\n' "$UNQUOTED_PADDED_CALL" > "$REPO_DIR/config.txt"
commit_change "test(auth): scan unquoted padded credential call argument"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "unquoted padded credential call argument fails" \
  || bad "unquoted padded credential call argument fails (rc=$RC)"
# The same padding tolerance applies to the parenthesized assignment form:
# `password = (<base64>==)` is a literal, and whitespace before the closer
# is not a boundary escape either.
new_repo
UNQUOTED_PADDED_PAREN="QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
printf 'password = (%s)\n' "$UNQUOTED_PADDED_PAREN" > "$REPO_DIR/config.txt"
commit_change "test(auth): scan unquoted padded parenthesized value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "unquoted padded parenthesized value fails" \
  || bad "unquoted padded parenthesized value fails (rc=$RC)"
new_repo
printf 'password = (%s )\n' "$UNQUOTED_PADDED_PAREN" > "$REPO_DIR/config.txt"
commit_change "test(auth): scan padded parenthesized value with space"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "padded parenthesized value with space fails" \
  || bad "padded parenthesized value with space fails (rc=$RC)"
new_repo
printf 'password("abcdefghijklmnop");\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan Rust credential call argument"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "Rust credential call argument fails" \
  || bad "Rust credential call argument fails (rc=$RC)"
for fixture in \
  $'password:\tabcdefghijklmnop' \
  $'password\t=\tabcdefghijklmnop' \
  $'password:\t"secretvalue12345"' \
  $'api_key\t=\tsecretvalue12345' \
  'api_key=secretvalue12345' \
  'api_key:secretvalue12345'; do
  new_repo
  printf '%s\n' "$fixture" > "$REPO_DIR/config.txt"
  commit_change "test(auth): scan tab-separated credential value"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "tab-separated credential fails: ${fixture//$'\t'/<TAB>}" \
    || bad "tab-separated credential fails: ${fixture//$'\t'/<TAB>} (rc=$RC)"
done
# Credential-shaped bare Rust values are not whitelisted as type declarations.
new_repo
FIXTURE_VALUE="Q7m2V9z4L6p8R1s3"
printf 'pub password: %s,\n' "$FIXTURE_VALUE" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): reject bare Rust credential value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "bare Rust credential value still fails" \
  || bad "bare Rust credential value still fails (rc=$RC)"
new_repo
FIXTURE_VALUE="Abcdefghijklmnop"
printf 'pub password: %s,\n' "$FIXTURE_VALUE" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): reject CamelCase Rust credential"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "CamelCase Rust credential still fails" \
  || bad "CamelCase Rust credential still fails (rc=$RC)"
new_repo
FIXTURE_VALUE="SecretValueLong"
printf 'pub password: %s,\n' "$FIXTURE_VALUE" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): reject title case Rust credential"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "title-case Rust credential still fails" \
  || bad "title-case Rust credential still fails (rc=$RC)"
for rust_credential in xxmyhostname myuserdataxx somealiasxxx AbcdefghName AbcdefghData MyCredentials; do
  new_repo
  printf 'pub password: %s,\n' "$rust_credential" > "$REPO_DIR/auth.rs"
  printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
  commit_change "test(auth): reject suffix-shaped Rust value"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "suffix-shaped Rust value fails: $rust_credential" \
    || bad "suffix-shaped Rust value fails: $rust_credential (rc=$RC)"
done
new_repo
FAKE_AKIA="AKIA""IOSFODNN7EXAMPLE"
printf 'pub token: %s,\n' "$FAKE_AKIA" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): reject cloud credential identifier"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "cloud credential identifier still fails" \
  || bad "cloud credential identifier still fails (rc=$RC)"
# Regression controls: raw scalars containing angle brackets and call-argument
# literals (including an embedded config string) must remain detectable.
FIXTURE_VALUE="Q7m2V9z4L6p8R1s3"
for fixture_format in \
  'password:%s' \
  'password: %s<local>' \
  'password: %s<123>' \
  'password: %s>' \
  'password: <%s>' \
  'password: (%s)' \
  'configure(password: %s)' \
  'cfg = "configure(password: %s)"'; do
  fixture="$(printf "$fixture_format" "$FIXTURE_VALUE")"
  new_repo
  printf '%s\n' "$fixture" > "$REPO_DIR/app.py"
  printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
  commit_change "feat(app): add synthetic credential fixture"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "credential shape still fails: $fixture" \
    || bad "credential shape still fails: $fixture (rc=$RC)"
done
# Credential material need not contain digits; delimiter-edge detection must
# still reject alphabetic values.
ALPHA_VALUE="abcdefghijklmnop"
for fixture_format in 'password:%s' 'Password=%s' 'password: %s<local>' 'configure(password: %s)'; do
  fixture="$(printf "$fixture_format" "$ALPHA_VALUE")"
  new_repo
  printf '%s\n' "$fixture" > "$REPO_DIR/app.py"
  printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
  commit_change "feat(app): reject alphabetic credential fixture"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "alphabetic credential shape still fails: $fixture" \
    || bad "alphabetic credential shape still fails: $fixture (rc=$RC)"
done
# Cloud and private-key literals still fail. Split across adjacent
# strings so this file never holds a contiguous scanner pattern; bash
# concatenates them at runtime into the real fixture.
new_repo
FAKE_AKIA="AKIA""IOSFODNN7EXAMPLE"
printf 'key = "%s"\n' "$FAKE_AKIA" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with key"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "akia literal still fails" \
  || bad "akia literal still fails (rc=$RC)"
new_repo
FAKE_PAT="github_pat_""abcdefghijklmnopqrstuvwxyz"
printf 'token = "%s"\n' "$FAKE_PAT" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add synthetic PAT"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "GitHub PAT literal still fails" \
  || bad "GitHub PAT literal still fails (rc=$RC)"
new_repo
FAKE_XOX="xoxb-""abcdefghijklmnop"
printf 'token = "%s"\n' "$FAKE_XOX" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add synthetic chat token"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "chat token literal still fails" \
  || bad "chat token literal still fails (rc=$RC)"
new_repo
API_VALUE="abcdefghijklmnop"
printf 'api_key = "%s"\n' "$API_VALUE" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add synthetic API key"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "API key literal still fails" \
  || bad "API key literal still fails (rc=$RC)"
new_repo
KEY_HEAD="-----BEGIN RSA ""PRIVATE KEY-----"
printf -- "%s\n" "$KEY_HEAD" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with key header"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "private key still fails" \
  || bad "private key still fails (rc=$RC)"

# Credential families inside Rust-shaped lines and multiline literals must never
# be removed by type recognition before the strong-pattern scan.
new_repo
FAKE_PAT="ghp_""AAAAAAAAAAAAAAAAAAAAAAAA"
printf 'pub token: Option</* %s */String,\n' "$FAKE_PAT" > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): detect token inside Rust generic"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "strong token in Rust generic comment fails" \
  || bad "strong token in Rust generic comment fails (rc=$RC)"
new_repo
printf 'const CFG: &str = r#"\npassword: AbcdefghijklType\n"#;\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan Rust raw configuration"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "password value in Rust raw string fails" \
  || bad "password value in Rust raw string fails (rc=$RC)"
new_repo
printf 'const CFG: &str = r##"\npassword: CredentialEnvelope<String>,\n"##;\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan Rust raw type-shaped configuration"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "type-shaped content in Rust raw string fails" \
  || bad "type-shaped content in Rust raw string fails (rc=$RC)"
new_repo
printf 'const CFG: &str = r##"embedded quote: "\npassword: CredentialEnvelope<String>,\n"##;\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): retain Rust raw string delimiter hashes"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "internal quote does not end Rust raw string" \
  || bad "internal quote does not end Rust raw string (rc=$RC)"
new_repo
printf '/* outer /* nested */ comment\npassword: CredentialEnvelope<String>,\n*/\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan nested Rust comments"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "type-shaped content in nested Rust comment fails" \
  || bad "type-shaped content in nested Rust comment fails (rc=$RC)"
new_repo
FIXTURE_VALUE="Q7m2V9z4L6p8R1s3"
NEWLINE_NAME=$'line\nbreak.txt'
printf 'password: %s\n' "$FIXTURE_VALUE" > "$REPO_DIR/$NEWLINE_NAME"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "test(auth): scan newline filename"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "credential in newline filename fails" \
  || bad "credential in newline filename fails (rc=$RC)"
new_repo
FIXTURE_VALUE="Q7m2V9z4L6p8R1s3"
printf 'password: %s\n' "$FIXTURE_VALUE" > "$REPO_DIR/:(literal)payload.txt"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "test(auth): scan pathspec magic filename"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "credential in magic filename fails" \
  || bad "credential in magic filename fails (rc=$RC)"
# Per-file diff failures must fail the gate rather than silently omit content.
new_repo
printf 'password: %s\n' "$FIXTURE_VALUE" > "$REPO_DIR/payload.txt"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "test(auth): fail closed on per-file diff errors"
FAIL_AUTHZ_DIFF=1 run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "per-file diff error fails closed" \
  || bad "per-file diff error fails closed (rc=$RC)"
# A committed Rust source read failure must fail closed before any lexical exemption.
new_repo
printf 'pub password: Option<String>,\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): fail closed on Rust context read errors"
FAIL_AUTHZ_READ=1 run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "Rust lexical context read error fails closed" \
  || bad "Rust lexical context read error fails closed (rc=$RC)"
# A HEAD existence-query failure on a present file must fail closed, not pass
# as a deletion. ls-tree still reports the file, so the mismatch is an error.
new_repo
printf 'password: %s\n' "$FIXTURE_VALUE" > "$REPO_DIR/payload.txt"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "test(auth): fail closed on HEAD existence query errors"
FAIL_AUTHZ_EXIST=1 run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && grep -q "cannot collect added lines" <<<"$OUT" \
  && ok "HEAD existence query error fails closed" \
  || bad "HEAD existence query error fails closed (rc=$RC)"
# An untracked name holding `=` must still be scanned: awk would otherwise
# read the operand as a variable assignment and see no content.
new_repo
printf 'def test_eqname(): pass\n' > "$REPO_DIR/test_eqname.py"
commit_change "test(auth): seed equals-sign filename fixture"
FIXTURE_VALUE="Q7m2V9z4L6p8R1s3"
printf 'password: %s\n' "$FIXTURE_VALUE" > "$REPO_DIR/config=prod.env"
# config=prod.env stays untracked; the credential must still fail the scan
# by detection (not by a collection failure that masks the content).
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ! grep -q "cannot collect added lines" <<<"$OUT" \
  && ok "credential in equals-sign filename fails" \
  || bad "credential in equals-sign filename fails (rc=$RC)"
# Deleting a binary file adds no content and must not fail collection.
new_repo
printf 'def test_gone(): pass\n' > "$REPO_DIR/test_gone.py"
printf 'x\000token_payload\000' > "$REPO_DIR/gone.dat"
commit_change "test(auth): seed deleted-binary fixture"
$GIT -C "$REPO_DIR" rm -q gone.dat >&2
commit_change "test(auth): delete binary fixture"
run_check
[[ "$RC" -eq 0 ]] && ok "deleted binary passes" \
  || bad "deleted binary passes (rc=$RC)"

# --- Review round 2: ordinary code passes, literals still fail ---------------
# A Rust fn signature with a generic return type holds no literal: the `<` in
# the return position sits outside the parameter's own type position.
new_repo
printf 'pub fn new(token: AuthToken) -> Result<Self, AuthError> {\n}\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): add constructor signature"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "rust fn signature passes authz" \
  || bad "rust fn signature passes authz (rc=$RC)"
# An inline struct body of recognized types holds no literal either.
new_repo
printf 'struct Creds { token: String, secret: Vec<u8> }\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): add inline credential struct"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "inline struct body passes authz" \
  || bad "inline struct body passes authz (rc=$RC)"
# A comment mentioning only a type shape is not a credential.
new_repo
printf '// token: Option<String> for auth\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "feat(auth): note token type"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "type-mention comment passes authz" \
  || bad "type-mention comment passes authz (rc=$RC)"
# But a non-type generic argument in the same position still fails closed.
# Fixture is literal here (as in earlier generic rows): the scanner shape
# under test is the angle-bracket argument, not a real credential.
new_repo
printf 'fn get(token: Option<supermansecret99>) {\n}\n' > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): reject inline generic credential"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "inline generic credential still fails" \
  || bad "inline generic credential still fails (rc=$RC)"
# A typed constructor parameter is a declaration, not a value. The type name
# rides in a variable so this file holds no adjacent keyword/value shape.
new_repo
CTOR_TYPE="AccessTokenValue"
printf 'constructor(private readonly token: %s) {}\n' "$CTOR_TYPE" > "$REPO_DIR/app.ts"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add typed client constructor"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "typed constructor param passes authz" \
  || bad "typed constructor param passes authz (rc=$RC)"
# Identifier and dotted-path call arguments are references, not literals.
new_repo
IDENT_ARG="secret_name_value"
printf 'get_secret(%s)\n' "$IDENT_ARG" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): read secret by name"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "identifier call arg passes authz" \
  || bad "identifier call arg passes authz (rc=$RC)"
new_repo
PATH_ARG="user.passwordHash"
printf 'verify_password(%s)\n' "$PATH_ARG" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): verify password hash"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "dotted call arg passes authz" \
  || bad "dotted call arg passes authz (rc=$RC)"
# But a literal keyword argument still fails. Neutral variable name so this
# file holds no adjacent keyword/value shape (see earlier fixture rows).
new_repo
KWARG_FIXTURE="hunter2secretvalue99"
printf 'verify(password=%s)\n' "$KWARG_FIXTURE" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "test(app): reject literal keyword argument"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "literal keyword argument still fails" \
  || bad "literal keyword argument still fails (rc=$RC)"
# A long receiver followed by a method call is code, not a credential.
new_repo
RECV_OBJ="credentialsStore"
RECV_METHOD="getTokenValue"
printf 'const token = %s.%s()[1];\n' "$RECV_OBJ" "$RECV_METHOD" > "$REPO_DIR/app.js"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): read cached token"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "method-chain receiver passes authz" \
  || bad "method-chain receiver passes authz (rc=$RC)"
# A dot before whitespace still terminates a value (sentence terminator).
new_repo
SENTENCE_FIXTURE="abcdefghijklmnop"
printf 'password: %s. # rotate quarterly\n' "$SENTENCE_FIXTURE" > "$REPO_DIR/config.txt"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "test(app): scan sentence-terminated value"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "sentence dot-space value still fails" \
  || bad "sentence dot-space value still fails (rc=$RC)"
# Reading a key from the environment is the recommended practice, not a
# secret. Object and leaf ride in separate variables so no single line here
# pairs the keyword with a value shape.
new_repo
ENV_OBJ="process.env"
ENV_LEAF="OPENAI_API_KEY"
printf 'const client = new OpenAI({ apiKey: %s.%s });\n' "$ENV_OBJ" "$ENV_LEAF" > "$REPO_DIR/app.js"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): read key from environment"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz && ok "environment key lookup passes authz" \
  || bad "environment key lookup passes authz (rc=$RC)"
# A raw string closing on a line with a lifetime must not mark that line as
# plain code: string content is never exempt from secret detection.
new_repo
RAW_FIXTURE="hunter2saltysecret"
printf 'const CFG: &str = r#"\n' > "$REPO_DIR/auth.rs"
# The opening `[` makes the array-type exemption reachable, so only the lexer
# (not the bare-value branch) decides this row: with the lifetime wrongly
# restoring plain-code status, the array span would exempt the match.
printf 'password: [%s; 32];"#; fn f<'"'"'a>() {}\n' "$RAW_FIXTURE" >> "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan raw-string close with lifetime"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "raw-string close with lifetime still fails" \
  || bad "raw-string close with lifetime still fails (rc=$RC)"
# Dotted unquoted api-key values are credentials (SendGrid SG.<seg>.<seg>
# shape); only a known environment/config lookup prefix is exempt. Every
# value rides in variables so no line here pairs the keyword with a value.
SEG_A="abcdefghijklmnopqrstuv"
SEG_B="wxyz0123456789abcdefghijkl"
DOTTED_KEYS=("SG.${SEG_A}.${SEG_B}" "sk.${SEG_A}" "key.${SEG_A}" "abc.def.${SEG_A}")
KEY_NAMES=(api_key api-key apikey)
KEY_SEPS=(': ' ' = ')
for key_name in "${KEY_NAMES[@]}"; do
  for key_sep in "${KEY_SEPS[@]}"; do
    for dotted in "${DOTTED_KEYS[@]}"; do
      new_repo
      printf '%s%s%s\n' "$key_name" "$key_sep" "$dotted" > "$REPO_DIR/config.yml"
      commit_change "test(auth): scan unquoted dotted api key"
      run_check
      [[ "$RC" -eq 1 ]] && has_row FAIL authz \
        && ok "unquoted dotted api key fails: ${key_name}${key_sep}${dotted:0:6}" \
        || bad "unquoted dotted api key fails: ${key_name}${key_sep}${dotted:0:6} (rc=$RC)"
    done
  done
done
new_repo
printf 'pub %s%s%s\n' "api_key" ": " "SG.${SEG_A}.${SEG_B}," > "$REPO_DIR/auth.rs"
printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
commit_change "test(auth): scan dotted api key in Rust"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "unquoted dotted api key in Rust fails" \
  || bad "unquoted dotted api key in Rust fails (rc=$RC)"
new_repo
printf '%s: "%s.%s"\n' "api_key" "process" "env.${ENV_LEAF}" > "$REPO_DIR/config.yml"
commit_change "test(auth): scan quoted lookup-shaped api key"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz \
  && ok "quoted lookup-shaped api key still fails" \
  || bad "quoted lookup-shaped api key still fails (rc=$RC)"
LOOKUP_PREFIXES=("process.env." "os.environ.get" "import.meta.env." "ENV." "settings." "config.")
for lookup_prefix in "${LOOKUP_PREFIXES[@]}"; do
  new_repo
  printf '%s = %s%s\n' "api_key" "$lookup_prefix" "OPENAI_API_KEY" > "$REPO_DIR/app.py"
  printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
  commit_change "feat(app): read key from lookup"
  run_check
  [[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
    && ok "api key lookup passes authz: ${lookup_prefix}" \
    || bad "api key lookup passes authz: ${lookup_prefix} (rc=$RC)"
done
# A lookup exempts only its own value. `os.environ` needs its dot, and a
# quoted 12+ literal riding behind the lookup (|| ?? or, second call
# argument) is a credential. Name-only calls and empty/short defaults pass.
# Templates carry @K@ (key name), @E@ (env leaf) and @F@ (fallback) so no
# source line here pairs the keyword with a value shape.
TPL_KEY="api_key"
TPL_CAMEL="apiKey"
TPL_LEAF="OPENAI_API_KEY"
render_tpl() {
  local out="$1"
  out="${out//@K@/$TPL_KEY}"; out="${out//@C@/$TPL_CAMEL}"
  out="${out//@E@/$TPL_LEAF}"; out="${out//@F@/$2}"
  printf '%s\n' "$out"
}
for fallback_shape in \
  '@K@ = os.environ@F@~~backupkey123' \
  'const c = { @C@: process.env.@E@ || "@F@" };~~fallbackliteral12' \
  "const c = { @C@: process.env.@E@ ?? '@F@' };~~fallbackliteral12" \
  '@K@ = process.env.@E@ or "@F@"~~fallbackliteral12' \
  '@K@ = os.environ.get("@E@", "@F@")~~fallbackliteral12' \
  '@K@ = os.getenv("@E@", "@F@")~~fallbackliteral12' \
  '@K@ = os.environ.get("@E@", default="@F@")~~fallbackliteral12' \
  '@K@ = os.getenv("@E@", default="@F@")~~fallbackliteral12' \
  '@K@ = os.getenv("@E@", f"@F@")~~fallbackliteral12' \
  '@K@ = ENV.fetch("@E@", "@F@")~~fallbackliteral12' \
  '@K@ = config.get("credentials", "openai_api_key") or "@F@"~~fallbackliteral12' \
  '@K@ = config.get("credentials", "openai_api_key") || "@F@"~~fallbackliteral12' \
  '@K@ = config.get("credentials", "openai_api_key") ?? "@F@"~~fallbackliteral12' \
  '@K@ = settings.get("credentials", "openai_api_key") or "@F@"~~fallbackliteral12' \
  '@K@ = os.environ.get("A", os.environ.get("B", "@F@"))~~fallbackliteral12' \
  '@K@ = os.environ.get("A", os.getenv("B", "@F@"))~~fallbackliteral12' \
  '@K@ = os.environ.setdefault("@E@", "@F@")~~fallbackliteral12' \
  '@K@ = os.environ.pop("@E@", "@F@")~~fallbackliteral12' \
  '@K@ = os.environ.get("A", os.environ.setdefault("B", "@F@"))~~fallbackliteral12' \
  '@K@ = os.environ.get("A", os.environ.pop("B", "@F@"))~~fallbackliteral12' \
  '@K@ = config.get("credentials", "openai_api_key") or ("@F@")~~fallbackliteral12' \
  '@K@ = os.environ.get ("@E@", "@F@")~~fallbackliteral12' \
  '@K@ = os.getenv ("@E@", "@F@")~~fallbackliteral12' \
  '@K@ = config.get("credentials", "openai_api_key") or "@F@"~~abcdefghijkl' \
  '@K@ = os.environ.get("A", config.get("section", "key")) or "@F@"~~fallbackliteral12' \
  '@K@ = os.environ.get("@E@", ("@F@"))~~fallbackliteral12'; do
  new_repo
  render_tpl "${fallback_shape%%~~*}" "${fallback_shape##*~~}" > "$REPO_DIR/app.py"
  printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
  commit_change "test(app): scan literal behind lookup"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "literal behind api key lookup fails: ${fallback_shape%%~~*}" \
    || bad "literal behind api key lookup fails: ${fallback_shape%%~~*} (rc=$RC)"
done
for lookup_shape in \
  '@K@ = os.environ.get("@E@")' \
  '@K@ = os.environ.get("@E@", None)' \
  '@K@ = os.environ.get("@E@", "")' \
  '@K@ = config.get("section", "openai_api_key")' \
  '@K@ = settings.get("section", "openai_api_key")' \
  '@K@ = config.get("credentials", "openai_api_key")' \
  '@K@ = config.get("credentials", "openai_api_key") or "short"' \
  '@K@ = config.get("credentials", "openai_api_key") or "abcdefghijk"' \
  '@K@ = config.get("credentials", "openai_api_key") or fallback_variable' \
  '@K@ = os.environ.get("A", os.environ.get("LONG_ENVIRONMENT_NAME"))' \
  '@K@ = os.environ.get("A", config.get("section", "openai_api_key"))' \
  '@K@ = os.environ.setdefault("LONG_ENVIRONMENT_NAME")' \
  '@K@ = os.environ.pop("LONG_ENVIRONMENT_NAME", None)' \
  '@K@ = config.get("section", "or fallbackliteral12")' \
  '@K@ = config.get("section", "|| fallbackliteral12")' \
  '@K@ = config.get("section", "os.getenv(X, fallbackliteral12)")' \
  '@K@ = config.get("credentials", "openai_api_key"); other = os.getenv("B", "fallbackliteral12")' \
  '@K@ = config.get("credentials", "openai_api_key"); other = x or "fallbackliteral12"' \
  'const c = { @C@: config.get("section", "openai_api_key"), other: os.getenv("B", "fallbackliteral12") };' \
  'const c = { @C@: process.env.@E@, baseURL: "https://api.openai.com/v1" };' \
  'const c = { @C@: process.env.@E@, "x-long-header-name": 1 };'; do
  new_repo
  render_tpl "$lookup_shape" "" > "$REPO_DIR/app.py"
  printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
  commit_change "feat(app): read key from lookup"
  run_check
  [[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
    && ok "api key lookup shape passes authz: $lookup_shape" \
    || bad "api key lookup shape passes authz: $lookup_shape (rc=$RC)"
done
# Ordinary Rust credential-typed declarations are not secrets, whatever type
# the generic argument names; only literal-shaped arguments flag. One fixture
# per shape so a regression names the shape.
for rust_decl in \
  'pub token: Option<Token>,' \
  'token: Arc<RwLock<String>>,' \
  'token: Zeroizing<String>,' \
  'password: impl AsRef<[u8]>,' \
  'fn login(password: impl AsRef<[u8]>) -> Result<Self> {' \
  'secret: Box<[u8]>,' \
  'secret: Vec<[u8; 32]>,' \
  "pub session_token: Cow<'a, str>," \
  "token: Option<&'a str>," \
  'pub access_token: Option<AccessToken>,' \
  'token: Option<AuthenticationToken>,' \
  'token: Box<dyn Fn(String) + Send>,' \
  'token: Option<Vec<u8>>,' \
  'token: std::sync::Arc<tokio::sync::RwLock<String>>,' \
  'token: Option<serde_json_helpers::TokenValue>,' \
  'token: Result<Vec<u8>, Box<dyn Error + Send + Sync>>,' \
  'fn f(token: Option<Arc<Mutex<HashMap<String, Token>>>>) -> Result<()> {' \
  'token: Box<dyn Fn(&str) -> Result<(), anyhow::Error> + Send>,' \
  "token: PhantomData<fn(&'static str) -> ()>," \
  'pub token: Option<Token>, // authentication tokens are rotated regularly' \
  'pub client_secret: Secret<String>,' \
  'fn eat(&mut self, token: &Token) -> Result<(), ParseError> {' \
  'pub colon_token: Option<Token![:]>,' \
  'pub semi_token: Option<Token![;]>,' \
  'token: Result<String, !>,' \
  'token: MyBuffer<{ 32 }>,' \
  'token: GenericArray<u8, { 16 }>,'; do
  new_repo
  printf '%s\n' "$rust_decl" > "$REPO_DIR/auth.rs"
  printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
  commit_change "feat(auth): add credential-typed declaration"
  run_check
  [[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
    && ok "typed Rust declaration passes authz: $rust_decl" \
    || bad "typed Rust declaration passes authz: $rust_decl (rc=$RC)"
done
# A literal in any position of the generic list fails, not only the first.
GENERIC_LITERAL="hunter2saltysecret"
for generic_shape in \
  'password: HashMap<String, %s>,' \
  'secret: Result<Vec<u8>, %s>,' \
  'token: Option<Vec<(String, %s)>>,'; do
  new_repo
  printf "$generic_shape\n" "$GENERIC_LITERAL" > "$REPO_DIR/auth.rs"
  printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
  commit_change "test(auth): scan literal in later generic argument"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "literal in later generic argument fails: $generic_shape" \
    || bad "literal in later generic argument fails: $generic_shape (rc=$RC)"
done
# Literal-shaped generic arguments still fail: lowercase or digit-bearing
# runs of 12+, and arguments with characters outside type syntax.
for literal_arg in "supermansecret99" "Q7m2V9z4L6p8R1s3" "abcdefghijklmnop" "abc/defghijklmnop==" '"supermansecret99"' "aB3/dE5/gH7/jK9" "ab.cd.ef.gh.ij"; do
  new_repo
  printf 'token: Vec<%s>,\n' "$literal_arg" > "$REPO_DIR/auth.rs"
  printf 'mod test_auth {}\n' > "$REPO_DIR/test_auth.rs"
  commit_change "test(auth): scan literal generic argument"
  run_check
  [[ "$RC" -eq 1 ]] && has_row FAIL authz \
    && ok "literal generic argument fails: $literal_arg" \
    || bad "literal generic argument fails: $literal_arg (rc=$RC)"
done
# A submodule pointer bump adds no text in the superproject and must pass.
new_repo
SUBSRC="$WORK/subsrc$N"
$GIT init -qb main "$SUBSRC" >&2
$GIT -C "$SUBSRC" config user.email t@example.invalid >&2
$GIT -C "$SUBSRC" config user.name Test >&2
$GIT -C "$SUBSRC" config commit.gpgsign false >&2
echo v1 > "$SUBSRC/f.txt"
$GIT -C "$SUBSRC" add -A >&2
$GIT -C "$SUBSRC" commit -qm "feat(sub): v1" >&2
$GIT -C "$REPO_DIR" -c protocol.file.allow=always submodule add -q "$SUBSRC" sub >&2
printf 'print("hi")\n' > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): track upstream module"
echo v2 > "$SUBSRC/f.txt"
$GIT -C "$SUBSRC" commit -qam "feat(sub): v2" >&2
SHA2="$($GIT -C "$SUBSRC" rev-parse HEAD)"
$GIT -c protocol.file.allow=always -C "$REPO_DIR/sub" fetch -q origin >&2
$GIT -C "$REPO_DIR/sub" checkout -q "$SHA2" >&2
$GIT -C "$REPO_DIR" add sub >&2
commit_change "feat(app): bump upstream module"
run_check
[[ "$RC" -eq 0 ]] && ! has_row FAIL authz \
  && ok "submodule bump passes authz" \
  || bad "submodule bump passes authz (rc=$RC)"

# --- 21. typed and subscript credential assignments ---------------------------
# Each invocation gets a fresh repo with test/doc plumbing: another bucket
# must not mask a missed literal or make a clean control fail. Fixture source
# is static scanner input only; none of the Python/TypeScript is executed.
check_authz_fixture() {
  local expected_rc="$1" file="$2" label="$3" source="$4"
  new_repo
  printf '%s\n' "$source" > "$REPO_DIR/$file"
  printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
  printf '# Static scanner fixture\n' > "$REPO_DIR/notes.md"
  commit_change "test(authz): add isolated assignment fixture"
  run_check
  if [[ "$expected_rc" -eq 1 ]]; then
    if [[ "$RC" -eq 1 ]] && has_row FAIL authz; then
      ok "$label"
    else
      bad "$label (expected exit=1 and FAIL authz, rc=$RC)"
    fi
  else
    if [[ "$RC" -eq 0 ]] && ! has_row FAIL authz; then
      ok "$label"
    else
      bad "$label (expected exit=0 and no FAIL authz, rc=$RC)"
    fi
  fi
  printf '    observed %s: exit=%s\n' "$label" "$RC"
  grep -E '^(PASS|FAIL|WARN|SKIP)  +authz([[:space:]]|$)' <<<"$OUT" || true
}

# Synthetic literals are assembled here, never committed as credential values
# in this suite. Exercise both quoting styles and ordinary spaces/tabs.
R1_LITERAL="synthetic""123" # exactly 12 characters, the minimum rejected length
R1_SHORT="short-""r1-12" # 11 characters, below the 12-character threshold
for quote in "'" '"'; do
  for spacing in spaces tabs; do
    pad=" "; subpad=""
    if [[ "$spacing" == tabs ]]; then pad=$' \t '; subpad="$pad"; fi
    for shape in python-api-key python-password typescript-api-key environ-store header-store; do
      file=app.py
      case "$shape" in
        python-api-key) format="api_key:${pad}str${pad}=${pad}${quote}%s${quote}" ;;
        python-password) format="password:${pad}str${pad}=${pad}${quote}%s${quote}" ;;
        typescript-api-key)
          file=app.ts
          format="const apiKey:${pad}string${pad}=${pad}${quote}%s${quote};" ;;
        environ-store)
          format="os.environ${subpad}[${subpad}${quote}X_API_KEY${quote}${subpad}]${pad}=${pad}${quote}%s${quote}" ;;
        header-store)
          format="headers${subpad}[${subpad}${quote}x-api-key${quote}${subpad}]${pad}=${pad}${quote}%s${quote}" ;;
      esac
      check_authz_fixture 1 "$file" "R1 literal $shape quote=$quote $spacing" "${format//%s/$R1_LITERAL}"
      if [[ "$spacing" == spaces ]]; then
        check_authz_fixture 0 "$file" "R1 short $shape quote=$quote" "${format//%s/$R1_SHORT}"
      fi
    done
  done

  # Lookup key names are not values, including section/key overloads. These
  # controls remain required even on runners with inherited lookup false reds.
  while IFS='|' read -r label file format; do
    check_authz_fixture 0 "$file" "R1 control $label quote=$quote" "${format//%s/$quote}"
  done <<'EOF'
getenv|app.py|api_key = os.getenv(%sX_API_KEY%s)
environ-get|app.py|api_key = os.environ.get(%sX_API_KEY%s)
config-get|app.py|api_key = config.get(%sapi_key%s)
settings-get|app.py|password = settings.get(%spassword%s)
settings-long-key|app.py|password = settings.get(%sDATABASE_PASSWORD%s)
config-long-key|app.py|api_key = config.get(%sSERVICE_API_KEY%s)
config-section-key|app.py|api_key = config.get(%sauth%s, %sapi_key%s)
settings-section-key|app.py|password = settings.get(%sauth%s, %spassword%s)
typed-api-key-runtime|app.py|api_key: str = os.getenv(%sX_API_KEY%s)
typed-password-runtime|app.py|password: str = settings.get(%spassword%s)
environ-runtime-store|app.py|os.environ[%sX_API_KEY%s] = os.getenv(%sX_API_KEY%s)
header-runtime-store|app.py|headers[%sx-api-key%s] = settings.get(%sapi_key%s)
EOF
  for key in content-type mode; do
    check_authz_fixture 0 app.py "R1 control noncredential-key=$key quote=$quote" \
      "$(printf 'headers[%s%s%s] = %s%s%s\n' "$quote" "$key" "$quote" "$quote" "$R1_LITERAL" "$quote")"
  done
done
check_authz_fixture 0 app.ts "R1 control typed-typescript-runtime" \
  'const apiKey: string = process.env.X_API_KEY;'
for declaration in 'api_key: str' 'password: str' 'declare const apiKey: string;'; do
  file=app.py
  if [[ "$declaration" == declare* ]]; then file=app.ts; fi
  check_authz_fixture 0 "$file" "R1 control annotation-only $declaration" "$declaration"
done

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
