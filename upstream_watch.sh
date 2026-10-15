#!/usr/bin/env bash
# upstream_watch.sh -- one read-only watcher tick for one upstream PR (case_214e61d3).
#
# The host upstream-pr-watcher timer calls this instead of `upstream_gate.sh
# audit --json` alone. It prints ONE JSON line the watcher records per head:
#
#   {"repo":"O/R","pr":N,"head":"<sha>","draft":bool,"audit":{...audit --json...},
#    "gates":{...status --json...},"unseen_feedback":K,"exit":0|1|2}
#
#   audit            the watcher backstop record (a non-draft PR whose head has
#                    no passing record is "convert to draft now"; v2 PRs also
#                    need lint and the reputation receipt)
#   gates            the three parallel gates (fork CI, our Paperclip Review,
#                    Greptile) on that head: green, pending or red each
#   unseen_feedback  maintainer and Greptile comments not yet in the repo's
#                    maintainer memory. K > 0 means the owning agent should run
#                    `upstream_lint.py memory ingest --repo O/R --pr N` and ship
#                    the memory file in a small PR.
#
# It never mutates anything: no --enforce, no memory write, no status post.
# Acting on the record is the owning agent's job (doer-finishes rule).
#
# EXIT: the audit exit code (0 pass, 1 fail, 2 refused). A tick that could not
# measure is never reported as a pass.
#
# Usage: upstream_watch.sh --upstream-repo O/R --upstream-pr N [--fork-repo O/R] [--me LOGIN]
set -uo pipefail
ME="$(basename "${BASH_SOURCE[0]}")"
SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

upstream="" num="" fork="" me="${UPSTREAM_AUTHOR_LOGIN:-}"
while [ $# -gt 0 ]; do case "$1" in
  --upstream-repo) upstream="${2:-}"; shift 2;;
  --upstream-pr) num="${2:-}"; shift 2;;
  --fork-repo) fork="${2:-}"; shift 2;;
  --me) me="${2:-}"; shift 2;;
  *) echo "$ME: unknown option '$1'" >&2; exit 4;;
esac; done
[ -n "$upstream" ] && [ -n "$num" ] || { echo "$ME: needs --upstream-repo O/R --upstream-pr N" >&2; exit 4; }

work="$(mktemp -d "${TMPDIR:-/tmp}/upstream_watch.XXXXXXXX")" || exit 2
trap 'rm -rf "$work"' EXIT
forkarg=(); [ -n "$fork" ] && forkarg=(--fork-repo "$fork")

"$SELFDIR/upstream_gate.sh" audit --upstream-pr "$num" --upstream-repo "$upstream" "${forkarg[@]}" --json \
  > "$work/audit.json" 2> "$work/audit.err"; acode=$?
"$SELFDIR/upstream_gate.sh" status --upstream-pr "$num" --upstream-repo "$upstream" "${forkarg[@]}" --json \
  > "$work/status.json" 2> "$work/status.err"; scode=$?
python3 "$SELFDIR/upstream_lint.py" memory ingest --repo "$upstream" --pr "$num" --dry-run ${me:+--me "$me"} \
  > "$work/ingest.txt" 2> "$work/ingest.err"; icode=$?

python3 - "$upstream" "$num" "$acode" "$scode" "$icode" "$work" <<'PYEOF'
import json, os, re, sys
repo, num, acode, scode, icode, work = sys.argv[1:7]
def load(name, code):
    try:
        v = json.load(open(os.path.join(work, name)))
        v["exit"] = int(code)
        return v
    except Exception:
        err = open(os.path.join(work, name.replace(".json", ".err"))).read()[:300]
        return {"error": err or "no output", "exit": int(code)}
audit, gates = load("audit.json", acode), load("status.json", scode)
unseen = None
if icode == "0":
    m = re.search(r"(\d+) new", open(os.path.join(work, "ingest.txt")).read())
    unseen = int(m.group(1)) if m else None
out = {"repo": repo, "pr": int(num), "head": audit.get("head") or gates.get("head"),
       "draft": audit.get("draft", gates.get("draft")), "audit": audit, "gates": gates,
       "unseen_feedback": unseen, "exit": int(acode)}
print(json.dumps(out))
PYEOF
exit "$acode"
