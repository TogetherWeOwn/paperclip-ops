#!/usr/bin/env python3
"""Area matcher for the org change-detection standard (case_6b39c80f/case_a99daa6c).

Reads a NUL-separated list of changed files from
``git diff --name-only --no-renames -z`` and decides, per area, whether anything
changed, whether the change forces a full run, and whether the change is
docs-only. --no-renames exposes both sides of a rename; -z preserves Unicode,
whitespace and control characters without Git's pathname quoting.
Source: https://git-scm.com/docs/git-diff

Pure stdlib (fnmatch). Shared reference implementation: the reusable
``changes.yml`` workflow embeds the same matching rules inline so the workflow
stays self-contained (pinned checkout only). Tests execute both implementations.
fnmatch is not globstar: **/package.json does not match root package.json.
Paths outside every area conservatively force a full run; docs_only=false alone
would not select any heavy suite. Invoke with -I to isolate stdlib imports.
Sources: https://docs.python.org/3/library/fnmatch.html
         https://docs.python.org/3/using/cmdline.html#cmdoption-I

Usage (enable pipefail so a failed diff cannot be read as an empty change):
    set -o pipefail
    git diff --name-only --no-renames -z <base> HEAD | python3 -I detect_changes.py \
        --areas-json '{"web":["web/**","src/web/**"],"bot":["bot/**"],"docs":["**.md","docs/**"]}' \
        --full-run-json '["package.json","**/package.json","package-lock.json","**/package-lock.json","npm-shrinkwrap.json","**/npm-shrinkwrap.json","pnpm-lock.yaml","**/pnpm-lock.yaml","yarn.lock","**/yarn.lock","Gemfile","**/Gemfile","Gemfile.lock","**/Gemfile.lock","Cargo.toml","**/Cargo.toml","Cargo.lock","**/Cargo.lock","go.mod","**/go.mod","go.sum","**/go.sum","pyproject.toml","**/pyproject.toml","poetry.lock","**/poetry.lock","uv.lock","**/uv.lock","requirements*.txt","**/requirements*.txt","requirements*.in","**/requirements*.in","setup.py","**/setup.py","setup.cfg","**/setup.cfg","Pipfile","**/Pipfile","Pipfile.lock","**/Pipfile.lock","composer.json","**/composer.json","composer.lock","**/composer.lock","pnpm-workspace.yaml","**/pnpm-workspace.yaml","go.work","**/go.work","go.work.sum","**/go.work.sum","tsconfig*.json","**/tsconfig*.json","jsconfig*.json","**/jsconfig*.json",".cargo/**","**/.cargo/**","rust-toolchain*","**/rust-toolchain*",".npmrc","**/.npmrc",".yarnrc*","**/.yarnrc*","Makefile*","**/Makefile*","CMakeLists.txt","**/CMakeLists.txt",".github/**","shared/**","common/**","build/**","Dockerfile*","**/Dockerfile*","docker-compose*.yml","**/docker-compose*.yml"]' \
        --docs-areas-json '["docs"]'

Output (stdout, JSON):
    {"areas": {"web": true, "bot": false, "docs": true},
     "full_run": false, "docs_only": false}
"""

import argparse
import fnmatch
import json
import os
import sys


def parse_args(argv):
    p = argparse.ArgumentParser(description="Match changed files to CI areas.")
    p.add_argument("--areas-json", required=True,
                   help="JSON object mapping area name to list of globs.")
    p.add_argument("--full-run-json", required=True,
                   help="JSON array of globs that force a full run.")
    p.add_argument("--docs-areas-json", default='["docs"]',
                   help="JSON array of area names that count as docs-only.")
    p.add_argument("--files", default=None,
                   help="File with NUL-terminated changed paths (default: stdin).")
    return p.parse_args(argv)


def match_any(path, globs):
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    areas = json.loads(args.areas_json)
    full_run_globs = json.loads(args.full_run_json)
    docs_areas = set(json.loads(args.docs_areas_json))

    if args.files:
        with open(args.files, "rb") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.buffer.read()
    if raw and not raw.endswith(b"\0"):
        raise ValueError("changed paths must be NUL-terminated")
    changed = [os.fsdecode(path) for path in raw.split(b"\0") if path]

    # Unclassified paths must not silently skip every heavy suite.
    area_globs = [glob for globs in areas.values() for glob in globs]
    full_run = any(match_any(path, full_run_globs)
                   or not match_any(path, area_globs) for path in changed)
    area_hit = {
        area: any(match_any(path, globs) for path in changed)
        for area, globs in areas.items()
    }
    hit_areas = {area for area, hit in area_hit.items() if hit}
    docs_globs = [glob for area, globs in areas.items() if area in docs_areas
                  for glob in globs]
    docs_only = (
        bool(changed)
        and not full_run
        and hit_areas <= docs_areas
        and all(match_any(path, docs_globs) for path in changed)
    )

    print(json.dumps(
        {"areas": area_hit, "full_run": full_run, "docs_only": docs_only},
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
