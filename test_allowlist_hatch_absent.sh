#!/usr/bin/env bash
# CI gate: the allowlist test hatch (PAPERCLIP_API_ALLOWLIST_TEST) is
# env-only and test-only. It must be absent from production surfaces
# (systemd units, env examples, operator templates, workflows) and must
# never default on in sender code or the shared helpers: hermetic suites
# set it explicitly per invocation, nothing else may.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       %s\n' "$2"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

HATCH="PAPERCLIP_API_ALLOWLIST_TEST"
CHECKED="PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"

# One entrypoint for the real sender scan and every control fixture. Exit 0
# means absent, 1 means forbidden default, 2 means inspection failed.
check_defaults() {
  python3 - "$@" <<'PYCHECK'
import ast
import pathlib
import re
import sys

kind = sys.argv[1]
key = "PAPERCLIP_API_ALLOWLIST_TEST" if kind == "hatch" else "PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"
violations = []
for filename in sys.argv[2:]:
    path = pathlib.Path(filename)
    try:
        source = path.read_text()
        if path.suffix == ".sh":
            # Keep the old lexical coverage for Python embedded in shell
            # carriers; those files are not standalone Python AST inputs.
            getter = r"(?:\.(?:get|setdefault)|(?:\.)?getenv)\(\s*"
            if kind == "hatch":
                pattern = getter + r"(?:HATCH_VAR|[\"']" + key + r"[\"'])\s*,\s*[\"']?1"
            else:
                pattern = getter + r"[\"']" + key + r"[\"']\s*,\s*(?![\"']{2})[^\s,)]"
            if re.search(pattern, source):
                violations.append(f"{path}: forbidden {kind} embedded default")
            if kind == "checked":
                for match in re.finditer(r"\$\{" + key + r":?-([^}]*)\}", source.replace("\\\n", "")):
                    if match[1] not in ("", "''", '""'):
                        violations.append(f"{path}: nonempty CHECKED_BASE shell default")
            continue
        tree = ast.parse(source, filename=filename)
    except (OSError, UnicodeError, SyntaxError) as error:
        print(f"cannot inspect {path}: {error}", file=sys.stderr)
        sys.exit(2)
    # Support both os.getenv and `from os import getenv [as alias]`.
    getenv_names = {"getenv"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            getenv_names.update(alias.asname or alias.name for alias in node.names if alias.name == "getenv")
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not ((isinstance(func, ast.Attribute) and func.attr in ("get", "setdefault", "getenv"))
                or (isinstance(func, ast.Name) and func.id in getenv_names)):
            continue
        keywords = {kw.arg: kw.value for kw in call.keywords}
        name = call.args[0] if call.args else keywords.get("key")
        if not ((isinstance(name, ast.Constant) and name.value == key)
                or (kind == "hatch" and isinstance(name, ast.Name) and name.id == "HATCH_VAR")):
            continue
        default = call.args[1] if len(call.args) > 1 else keywords.get("default")
        if default is None:
            continue
        # Only provably empty checked-base defaults are safe. Parentheses,
        # multiline calls, expressions, and comments are handled by the AST,
        # not guessed from the first character on a grep line.
        empty = isinstance(default, ast.Constant) and default.value in ("", None)
        unsafe = not empty if kind == "checked" else (
            not isinstance(default, ast.Constant) or default.value in ("1", 1)
        )
        if unsafe:
            violations.append(f"{path}:{call.lineno}: forbidden {kind} default")
print("\n".join(violations), end="\n" if violations else "")
sys.exit(1 if violations else 0)
PYCHECK
}

hdr "hatch absent from production surfaces"
scope_a_hits=""
while IFS= read -r file; do
  if grep -qF "$HATCH" "$file"; then
    scope_a_hits+="${file}"$'\n'
  fi
done < <(
  { find "$HERE/systemd" "$HERE/host-bootstrap" "$HERE/.github" -type f 2>/dev/null;
    find "$HERE" -name '*.env.example' -not -path "$HERE/.paperclip-runtime/*" 2>/dev/null;
    find "$HERE" -name 'OPERATOR.md' -not -path "$HERE/.paperclip-runtime/*" 2>/dev/null; } | sort -u
)
if [[ -z "$scope_a_hits" ]]; then
  ok "no hatch in systemd units, env examples, operator templates, workflows"
else
  bad "no hatch in systemd units, env examples, operator templates, workflows" "$scope_a_hits"
fi

hdr "hatch never defaults on in senders or helpers"
SCOPE_B=(
  "$HERE/lib/paperclip_api_allowlist.py"
  "$HERE/lib/paperclip_api_allowlist.sh"
  "$HERE/agent_error_proposer.py"
  "$HERE/process_lost_probe.py"
  "$HERE/websearch_lane_probe.py"
  "$HERE/recovery_writer.py"
  "$HERE/scripts/model_pin_cleanup.py"
  "$HERE/ops/case_34c2c99f/prune_operator_run_dirs.py"
  "$HERE/ops/case_60e07cc2/project_worktree_regen_prune.py"
  "$HERE/ops/case_af881f62/operator_workspace_reconciler.py"
  "$HERE/ops/case_b1d141af/terminal_worktree_prune.py"
  "$HERE/ops/case_b5184419/terminal_worktree_cache_reclaim.py"
  "$HERE/notify_paperclip_issue.sh"
  "$HERE/db_backup_stall.sh"
  "$HERE/db_backup_stall_monitor.sh"
  "$HERE/red_main_poll.sh"
  "$HERE/sibling_guard.sh"
  "$HERE/scripts/dispatch_deploy_monitor.sh"
  "$HERE/ops/case_4ea7d92e/lib/post_finding.sh"
  "$HERE/ops/case_b1d141af/run-worktree-prune.sh"
)
missing=0
for file in "${SCOPE_B[@]}"; do
  [[ -f "$file" ]] || { bad "scope lists a file that no longer exists" "$file"; missing=1; }
done
[[ "$missing" -eq 0 ]] && ok "all scoped sender/helper files exist"
# Default-on shapes: a direct assignment, a shell :-1} default, a Python
# getter with an enabled/unknown default, or a nonempty fallback for the
# host-delegated CHECKED_BASE bypass. Python calls use ASTs across lines;
# comments and plain `== "1"` comparisons are not fallback expressions.
# Capture first, then test the capture: piping a producer straight into an
# early-exiting `grep -q` under pipefail lets SIGPIPE read a match as a
# miss (case_c25c4d3f).
hits_b_assign="$(grep -rnE "$HATCH\"?\]?\s*=[^=]" "${SCOPE_B[@]}" 2>/dev/null | grep -vE '==')"
if grep -q . <<<"$hits_b_assign"; then
  bad "no direct hatch assignment in senders/helpers" "$(head -n 5 <<<"$hits_b_assign")"
else
  ok "no direct hatch assignment in senders/helpers"
fi
hits_b_shell="$(grep -rnE "${HATCH}:-[^}]*1" "${SCOPE_B[@]}" 2>/dev/null)"
if grep -q . <<<"$hits_b_shell"; then
  bad "no shell default-on in senders/helpers" "$(head -n 5 <<<"$hits_b_shell")"
else
  ok "no shell default-on in senders/helpers"
fi
if hits_b_map="$(check_defaults hatch "${SCOPE_B[@]}")"; then
  ok "no mapping default-on in senders/helpers"
else
  bad "no mapping default-on in senders/helpers" "$hits_b_map"
fi
# Only the checked host wrapper may set a delegated verdict. A nonempty
# fallback can self-delegate by making checked_base equal the forwarded base.
if hits_b_checked="$(check_defaults checked "${SCOPE_B[@]}")"; then
  ok "no CHECKED_BASE default in senders/helpers"
else
  bad "no CHECKED_BASE default in senders/helpers" "$hits_b_checked"
fi

hdr "positive controls (the production checks detect real violations)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_hatch_absent.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
printf 'Environment=%s=1\n' "$HATCH" > "$WORK/fake.service"
if grep -qF "$HATCH" "$WORK/fake.service"; then
  ok "scope-A grep catches a unit file violation"
else
  bad "scope-A grep catches a unit file violation"
fi
printf '%s=1\n' "$HATCH" > "$WORK/assign.sh"
printf 'echo "${%s:-1}"\n' "$HATCH" > "$WORK/default.sh"
printf 'x = env.get("%s", "1")\n' "$HATCH" > "$WORK/getmap.py"
printf 'x = env.get(HATCH_VAR, "1")\n' > "$WORK/getconst.py"
grep -qE "$HATCH\"?\]?\s*=[^=]" "$WORK/assign.sh" \
  && ok "assignment grep catches =1" || bad "assignment grep catches =1"
grep -qE "${HATCH}:-[^}]*1" "$WORK/default.sh" \
  && ok "default grep catches :-1}" || bad "default grep catches :-1}"
printf 'x = env.setdefault("%s", "1")\n' "$HATCH" > "$WORK/setdefault.py"
printf "python3 - <<'PY'\nx = env.setdefault(\"%s\", \"1\")\nPY\n" "$HATCH" > "$WORK/setdefault.sh"
printf 'from os import getenv as read_env\nx = read_env("%s", ("1"))\n' "$HATCH" > "$WORK/getenv_alias.py"
for fixture in "$WORK/getmap.py" "$WORK/getconst.py" "$WORK/setdefault.py" "$WORK/setdefault.sh" "$WORK/getenv_alias.py"; do
  check_defaults hatch "$fixture" > "$WORK/hits"; rc=$?
  [[ "$rc" -eq 1 ]] && ok "mapping check catches default in ${fixture##*/}" \
    || bad "mapping check catches default in ${fixture##*/}" "exit $rc: $(cat "$WORK/hits")"
done
# Rejection corpus is data only. Every file goes through the exact check
# called with SCOPE_B above, so weakening that check must break a control.
python3 - "$WORK" "$CHECKED" <<'PYFIXTURES'
from pathlib import Path
import sys
work, key = Path(sys.argv[1]), sys.argv[2]
for getter in ('env.get', 'env.setdefault', 'os.getenv', 'getenv'):
    for default in ('base', '(base)', 'base or ""', 'str(base)', '"base"', '"".join([base])'):
        for layout in ('{getter}("{key}", {default})', '{getter}(\n    "{key}",\n    {default},\n)'):
            index = len(list(work.glob('reject_*.py')))
            (work / f'reject_{index}.py').write_text('x = ' + layout.format(getter=getter, key=key, default=default) + '\n')
for i, getter in enumerate(('env.get', 'env.setdefault', 'os.getenv', 'getenv')):
    (work / f'allow_{i}.py').write_text(f'x = {getter}("{key}", "")\n')
(work / 'allow_parenthesized.py').write_text(f'x = env.get(\n"{key}", (""),\n)\n')
(work / 'reject_keyword.py').write_text(f'x = os.getenv(key="{key}", default=base)\n')
PYFIXTURES
printf "python3 - <<'PY'\nx = env.get(\"%s\", base)\nPY\n" "$CHECKED" > "$WORK/reject_embedded.sh"
printf 'echo "${%s:-$PAPERCLIP_API_URL}"\n' "$CHECKED" > "$WORK/reject_default.sh"
printf 'echo "${%s-$PAPERCLIP_API_URL}"\n' "$CHECKED" > "$WORK/reject_unset.sh"
printf 'echo "${%s:-}"\n' "$CHECKED" > "$WORK/allow_empty.sh"
for fixture in "$WORK"/reject_*; do
  check_defaults checked "$fixture" > "$WORK/hits"; rc=$?
  [[ "$rc" -eq 1 ]] && ok "CHECKED_BASE check catches default in ${fixture##*/}" \
    || bad "CHECKED_BASE check catches default in ${fixture##*/}" "exit $rc: $(cat "$WORK/hits")"
done
for fixture in "$WORK"/allow_*; do
  if hits="$(check_defaults checked "$fixture")"; then
    ok "empty CHECKED_BASE default accepted in ${fixture##*/}"
  else
    bad "empty CHECKED_BASE default accepted in ${fixture##*/}" "$hits"
  fi
done
printf 'x = env.get(\n' > "$WORK/malformed.py"
for fixture in "$WORK/malformed.py" "$WORK/missing.py"; do
  check_defaults checked "$fixture" > "$WORK/hits" 2>&1; rc=$?
  [[ "$rc" -eq 2 ]] && ok "uninspectable source fails closed in ${fixture##*/}" \
    || bad "uninspectable source fails closed in ${fixture##*/}" "exit $rc"
done
# The legitimate comparison shape must NOT trip the assignment grep.
printf 'if [[ "${%s:-}" == "1" ]]; then\n' "$HATCH" > "$WORK/legit.sh"
printf 'if env.get(HATCH_VAR, "") == "1":\n' > "$WORK/legit.py"
hits_legit="$(grep -E "$HATCH\"?\]?\s*=[^=]" "$WORK/legit.sh" "$WORK/legit.py" | grep -vE '==')"
if grep -q . <<<"$hits_legit"; then
  bad "legitimate comparisons do not trip the gate"
else
  ok "legitimate comparisons do not trip the gate"
fi

printf '\nhatch-absent gate: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
