#!/usr/bin/env bash
# Fixtures for the org change-detection standard (case_a99daa6c).
# Exercises scripts/detect_changes.py with the four required cases:
# docs-only, area-only, lockfile, and .github changes, plus a git-level case
# that a rename into docs still lists the deleted source path.
set -euo pipefail
cd "$(dirname "$0")/.."

AREAS='{"web":["web/**"],"bot":["bot/**"],"docs":["**.md","docs/**"]}'
# Exercise the caller's actual patterns, not a fixture-specific copy.
FULL="$(python3 -I - <<'PY'
import re
from pathlib import Path
text = Path('workflow-templates/starter-ci.yml').read_text()
print(re.search(r"full-run-globs-json: '([^']+)'", text).group(1))
PY
)"
DOCS='["docs"]'

check() { # name, changed-files..., then ---, then expected JSON
  local name="$1"; shift
  local files=()
  while [ "${1:-}" != "---" ]; do files+=("$1"); shift; done
  shift
  local expected="$1"
  local got
  got="$(printf '%s\0' "${files[@]}" | python3 -I scripts/detect_changes.py --areas-json "$AREAS" --full-run-json "$FULL" --docs-areas-json "$DOCS")"
  if [ "$got" = "$expected" ]; then
    echo "PASS $name"
  else
    echo "FAIL $name"
    echo "  got:      $got"
    echo "  expected: $expected"
    exit 1
  fi
}

check "docs-only skips heavy jobs" \
  README.md docs/guide.md --- \
  '{"areas": {"bot": false, "docs": true, "web": false}, "docs_only": true, "full_run": false}'

check "area-only runs just that area" \
  web/app.js --- \
  '{"areas": {"bot": false, "docs": false, "web": true}, "docs_only": false, "full_run": false}'

check "lockfile runs everything" \
  web/app.js package-lock.json --- \
  '{"areas": {"bot": false, "docs": false, "web": true}, "docs_only": false, "full_run": true}'

check ".github change runs everything" \
  .github/workflows/ci.yml --- \
  '{"areas": {"bot": false, "docs": false, "web": false}, "docs_only": false, "full_run": true}'

check "shared code runs everything" \
  shared/util.py --- \
  '{"areas": {"bot": false, "docs": false, "web": false}, "docs_only": false, "full_run": true}'

check "unclassified file conservatively runs everything" \
  notes/scratch.txt --- \
  '{"areas": {"bot": false, "docs": false, "web": false}, "docs_only": false, "full_run": true}'

check "unmatched deleted code plus docs runs everything" \
  tools/run.py docs/run.md --- \
  '{"areas": {"bot": false, "docs": true, "web": false}, "docs_only": false, "full_run": true}'

for manifest in package.json package-lock.json npm-shrinkwrap.json pnpm-lock.yaml yarn.lock \
  Gemfile Gemfile.lock Cargo.toml Cargo.lock go.mod go.sum pyproject.toml \
  poetry.lock uv.lock requirements.txt requirements-dev.txt requirements.in \
  setup.py setup.cfg Pipfile Pipfile.lock composer.json composer.lock; do
  for prefix in '' nested/; do
    check "dependency $prefix$manifest runs everything" "$prefix$manifest" --- \
      '{"areas": {"bot": false, "docs": false, "web": false}, "docs_only": false, "full_run": true}'
  done
done

for config in pnpm-workspace.yaml go.work go.work.sum tsconfig.json .cargo/config.toml; do
  check "workspace/build $config runs everything" "$config" --- \
    '{"areas": {"bot": false, "docs": false, "web": false}, "docs_only": false, "full_run": true}'
  check "workspace/build web/$config runs everything" "web/$config" --- \
    '{"areas": {"bot": false, "docs": false, "web": true}, "docs_only": false, "full_run": true}'
done

# Git-level case: the diff command the reusable workflow runs. With rename
# detection on (force it, whatever the host's git config says), moving a code
# file into docs lists only the destination unless --no-renames is passed, and
# the change then looks docs-only.
HERE="$(pwd)"
TMP="$(mktemp -d "${PAPERCLIP_RUN_SCRATCH_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}}/detect-changes.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
(
  # cd alone does not isolate Git when callers set GIT_DIR/GIT_WORK_TREE.
  # Source: https://git-scm.com/docs/git-rev-parse#Documentation/git-rev-parse.txt---local-env-vars
  GIT_LOCAL_ENV="$(git rev-parse --local-env-vars)"
  while IFS= read -r var; do unset "$var"; done <<< "$GIT_LOCAL_ENV"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  cd "$TMP"
  # --template overrides GIT_TEMPLATE_DIR; an empty directory copies no hooks.
  # Source: https://git-scm.com/docs/git-init#_template_directory
  mkdir "$TMP/empty-template"
  git init -q --template="$TMP/empty-template" .
  git config user.email ci@example.invalid
  git config user.name ci
  git config commit.gpgsign false
  git config diff.renames true
  mkdir web docs
  printf 'export const one = 1;\nexport const two = 2;\nexport const three = 3;\nexport const four = 4;\n' > web/app.js
  git add -A
  git commit -q -m base
  git mv web/app.js docs/app.md
  git commit -q -m move
  got="$(git diff --name-only --no-renames -z HEAD~1 HEAD | python3 -I "$HERE/scripts/detect_changes.py" --areas-json "$AREAS" --full-run-json "$FULL" --docs-areas-json "$DOCS")"
  expected='{"areas": {"bot": false, "docs": true, "web": true}, "docs_only": false, "full_run": false}'
  if [ "$got" = "$expected" ]; then
    echo "PASS rename into docs is not docs-only"
  else
    echo "FAIL rename into docs is not docs-only"
    echo "  got:      $got"
    echo "  expected: $expected"
    exit 1
  fi
  # Negative control: without the flag the same diff hides the source path.
  hidden="$(git diff --name-only -z HEAD~1 HEAD | python3 -I "$HERE/scripts/detect_changes.py" --areas-json "$AREAS" --full-run-json "$FULL" --docs-areas-json "$DOCS")"
  case "$hidden" in
    *'"docs_only": true'*) echo "PASS control: plain git diff hides the renamed source (why --no-renames is required)" ;;
    *) echo "FAIL control: expected plain git diff to look docs-only here"; echo "  got: $hidden"; exit 1 ;;
  esac
)

echo "All change-detection fixtures pass."
