#!/usr/bin/env bash
# upstream_prepush_hook.sh -- git pre-push hook: lint before the first push to
# any repo outside TogetherWeOwn (case_214e61d3).
#
# Install into a clone with:  upstream_lint.py install-hook --repo-dir <clone>
#
# What it does: for every branch a push would create or update on a non-own
# remote, it runs the upstream pre-push linter (upstream_lint.py lint) over the
# commits that push adds, the remote branch name that will become public, and the
# PR text you will use. The text is read from the clone's git dir:
#
#     <git-dir>/upstream-pr/<remote-branch>/title.txt
#     <git-dir>/upstream-pr/<remote-branch>/body.md
#
# so the lint measures the words that will go public, not a placeholder. Fail
# closed: a missing title or body, an unknown remote host, an unresolvable base
# or a linter refusal all block the push. TogetherWeOwn remotes pass straight
# through (this repo's own verify-fast hook and the mirror gate govern those).
# Bypassing it with `git push --no-verify` is a policy violation.
set -uo pipefail
ME="$(basename "${BASH_SOURCE[0]}")"
SELFDIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
remote_name="${1:-}"
remote_url="${2:-}"
ZERO="0000000000000000000000000000000000000000"
GIT="${GIT_BIN:-git}"

block() { echo "$ME: BLOCKED: $*" >&2; exit 1; }
# git passes the URL as the remote name when pushing to a URL; never echo userinfo.
remote_label="$(printf '%s' "$remote_name" | sed -E 's#(://)[^/@]*@#\1#')"

[ -n "$remote_url" ] || block "no remote URL given by git (cannot tell which repo this push targets)"

# owner/repo from any GitHub URL form.
slug=""
case "$remote_url" in
  https://github.com/*|http://github.com/*) slug="${remote_url#*github.com/}";;
  git@github.com:*)                         slug="${remote_url#git@github.com:}";;
  ssh://git@github.com/*)                   slug="${remote_url#ssh://git@github.com/}";;
  https://*@github.com/*)                   slug="${remote_url#*@github.com/}";;
esac
slug="${slug%.git}"; slug="${slug%/}"
if [ -z "$slug" ] || ! [[ "$slug" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  block "remote '$remote_label' is not a recognizable github.com repo; the linter cannot read maintainer memory for it"
fi
owner="${slug%%/*}"
case "$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')" in
  togetherweown) exit 0;;
esac

gitdir="$("$GIT" rev-parse --git-dir)" || block "not inside a git repository"
failed=0
while read -r local_ref local_sha remote_ref remote_sha; do
  [ -n "${local_ref:-}" ] || continue
  [ "$local_sha" = "$ZERO" ] && continue              # deleting a branch: nothing to lint
  case "$remote_ref" in refs/heads/*) ;; *) continue;; esac   # tags etc.
  branch="${remote_ref#refs/heads/}"
  # Base: the full PR range, base..HEAD, every time. Linting only the pushed
  # increment (remote_sha..local_sha) lets split pushes evade the diff budget
  # and the same-package test gate. If the fork point is not available, refuse;
  # the remote tip is not proof of the PR base.
  base=""
  for cand in "refs/remotes/$remote_name/HEAD" "refs/remotes/$remote_name/main" "refs/remotes/$remote_name/master"; do
    if "$GIT" rev-parse -q --verify "$cand" >/dev/null 2>&1; then
      base="$("$GIT" merge-base "$local_sha" "$cand" 2>/dev/null)" && [ -n "$base" ] && break
    fi
  done
  [ -n "$base" ] || { echo "$ME: BLOCKED: cannot resolve the full PR base of '$branch' on $slug (fetch $remote_label first)" >&2; failed=1; continue; }
  tdir="$gitdir/upstream-pr/$branch"
  if [ ! -s "$tdir/title.txt" ] || [ ! -s "$tdir/body.md" ]; then
    echo "$ME: BLOCKED: no PR text for '$branch'. Write the title to $tdir/title.txt and the body (the upstream template, filled) to $tdir/body.md, then push again." >&2
    failed=1; continue
  fi
  echo "$ME: linting '$branch' for $slug ($("$GIT" rev-list --count "$base..$local_sha") commit(s) from ${base:0:10})"
  python3 "$SELFDIR/upstream_lint.py" lint --repo "$slug" --repo-dir "$PWD" \
    --git-range "$base..$local_sha" --branch "$branch" \
    --title "$(head -n1 "$tdir/title.txt")" --body-file "$tdir/body.md" \
    --stage first-push --live || failed=1
done
[ "$failed" -eq 0 ] || block "pre-push lint failed for $slug; fix the items above (the push did not happen)"
exit 0
