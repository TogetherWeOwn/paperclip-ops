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
if REALGIT="$(PATH="$(getconf PATH)" command -v git 2>/dev/null)" && [[ -n "$REALGIT" ]]; then
  ln -sf "$REALGIT" "$WORK/bin/git"
  export PATH="$WORK/bin:$PATH"
fi
# Stop BASH_ENV from re-prepending the credential shim on child shells,
# which would bury the shadow above (BASH_ENV= + real git first).
export BASH_ENV=/dev/null
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
# A bare literal passed as a call argument is still a credential.
# Assembled via a variable reference so this file holds no literal.
new_repo
CALL_PW="hunter2""hunter2123"
printf 'login(name, password = %s)\n' "$CALL_PW" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): log in with literal"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "call-arg literal still fails" \
  || bad "call-arg literal still fails (rc=$RC)"
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
KEY_HEAD="-----BEGIN RSA ""PRIVATE KEY-----"
printf -- "%s\n" "$KEY_HEAD" > "$REPO_DIR/app.py"
printf 'def test_app(): pass\n' > "$REPO_DIR/test_app.py"
commit_change "feat(app): add app with key header"
run_check
[[ "$RC" -eq 1 ]] && has_row FAIL authz && ok "private key still fails" \
  || bad "private key still fails (rc=$RC)"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
