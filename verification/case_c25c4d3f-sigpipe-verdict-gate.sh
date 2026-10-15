#!/usr/bin/env bash
# =====================================================================================
# case_c25c4d3f-sigpipe-verdict-gate.sh — a matched pattern must never read as a miss
#
# THE DEFECT (observed red on PR #304 head 348f354f, job 104785240882, step 75):
#
#     set -o pipefail
#     if printf '%s' "$out" | grep -qF "FAIL  $case_name"; then ... else FAIL; fi
#
# `grep -q` exits the instant it matches, WITHOUT draining stdin. That closes the read
# end of the pipe while `printf` is still writing, so `printf` takes EPIPE/SIGPIPE and
# exits non-zero. Under `pipefail` the pipeline inherits THAT status, not grep's — so a
# pattern that WAS found is reported as not found, and the gate emits a false verdict.
#
# The broken pipe is itself proof the match happened: `grep -q` only stops reading early
# when it matches. A miss makes grep read to EOF, which can never strand the producer.
#
# It is a race, so it is rare on an idle box and shows up under CI load. Above the pipe
# capacity the producer MUST block mid-write, which makes it deterministic — that is the
# positive control below. Without that control a green here would be unfalsifiable.
#
# THE FIX: drop the pipeline. `grep -qF PAT <<<"$var"` has no producer to strand and no
# pipeline status for pipefail to poison.
#
# case_b2044d98 WIDENED THE SCAN. The first version only matched `printf '%s' "$var" | grep -q`,
# and the same defect kept returning in every other spelling: `echo "$OUT" | grep -q`,
# `some_function | grep -q`, `./tool --help | grep -q`. That last one (test_capture_host_
# evidence.sh:683) cost a review run on at least eight cards: --help writes a 4194-byte
# header through mawk in two 4096-byte chunks, grep -q leaves after the first, awk takes
# SIGPIPE on the second, and pipefail reports the PIPELINE as failed although the text was
# found. 1/400 on an idle box, 59/400 under 24 busy loops. Any producer that writes more than
# once can lose this race, so the scan now flags EVERY pipeline whose non-first stage is an
# early-exiting grep (-q, --quiet, --silent, -l, -m N) in a file that sets pipefail, whatever
# the producer is. Capture the output first (`out="$(cmd)"; grep -q PAT <<<"$out"`), or read
# the file directly (`grep -q PAT file`).
#
# NOT COVERED, on purpose: other early-exiting readers (`| head -n N`, `| sed -n '1p;q'`,
# `| awk '... exit'`). They are overwhelmingly used to trim a failure message printed AFTER
# the verdict was already recorded, where a SIGPIPE changes nothing. case_b2044d98 audited each
# one; the survivors are listed in that PR.
#
#   ./verification/case_c25c4d3f-sigpipe-verdict-gate.sh
#
# Exit 0 = the mechanism is demonstrated, the fixed form is immune, and no verdict site
#          in the tree still routes a variable into an early-exiting grep through a pipe.
# =====================================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || exit 4

pass=0; fail=0
red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ok()  { grn "  PASS  $1"; pass=$((pass+1)); }
no()  { red "  FAIL  $1"; shift; for l in "$@"; do red "        $l"; done; fail=$((fail+1)); }

NEEDLE='FAIL  URL with userinfo is a credential in a URL'
# Needle on line 1, then enough filler to exceed any pipe capacity, so the producer is
# guaranteed to still be writing when an early-exiting consumer walks away.
PAYLOAD="$NEEDLE"$'\n'"$(yes '  PASS  filler ..................................................' \
                        | head -n 60000)"
ITERS=20

echo "=== the mechanism: under pipefail, an early-exiting consumer poisons the status ==="
printf 'payload %d bytes across %d lines; needle on line 1\n\n' \
  "${#PAYLOAD}" "$(($(grep -c '' <<<"$PAYLOAD")))"

# --- POSITIVE CONTROL 1. Consumer-agnostic: `head -c 1` provably stops after one byte.
# If this does not fire, this box cannot exhibit the defect and every green below is
# meaningless — so it is a FAIL, never a skip.
printf '%s' "$PAYLOAD" | head -c 1 >/dev/null
ctl=("${PIPESTATUS[@]}")
if [ "${ctl[0]}" != "0" ]; then
  ok "control: producer stranded by an early-exiting consumer (printf rc=${ctl[0]})"
else
  no "control did NOT fire: printf completed despite the consumer exiting after 1 byte." \
     "This environment cannot exhibit the defect, so this gate proves nothing here." \
     "Do not read a green from this run as evidence."
fi

# --- POSITIVE CONTROL 2. The real construct: grep -q matched, yet the pipeline is red.
lost=0; matched_but_red=0
for ((i = 0; i < ITERS; i++)); do
  printf '%s' "$PAYLOAD" | grep -qF "$NEEDLE"
  # PIPESTATUS must be copied by the VERY next command: any other command, including
  # a plain `st=$?` assignment, overwrites it with its own one-element status.
  ps=("${PIPESTATUS[@]}")
  redp=0; for s in "${ps[@]}"; do [ "$s" = "0" ] || redp=1; done   # what pipefail yields
  # grep itself matched; only the producer's EPIPE can have made the pipeline red.
  [ "${ps[1]}" = "0" ] && [ "$redp" = "1" ] && matched_but_red=$((matched_but_red+1))
  [ "${ps[0]}" = "0" ] || lost=$((lost+1))
done
if [ "$matched_but_red" -gt 0 ]; then
  ok "old form: grep MATCHED but the pipeline read red in $matched_but_red/$ITERS runs"
else
  # Some greps (e.g. ugrep) drain stdin instead of exiting on match. Say so loudly:
  # a green from an environment that cannot lose the race is not evidence of a fix.
  no "the grep here never stranded the producer ($lost/$ITERS) — control 2 is inert." \
     "grep: $(grep --version 2>&1 | head -1)" \
     "GNU grep -q exits on match and DOES strand it; that is what CI runs."
fi

# --- POSITIVE CONTROL 3. The case_b2044d98 shape: an EXTERNAL producer that writes twice.
# A builtin printf of one variable is the lucky case; a real tool (awk, a script) emits its
# output in several write(2)s, so the consumer can match chunk 1 and leave before chunk 2.
# The sleep makes the loss deterministic instead of a coin flip.
lost3=0
for ((i = 0; i < ITERS; i++)); do
  bash -c 'printf "%s\n" "$1"; sleep 0.05; printf "second chunk\n"' _ "$NEEDLE" | grep -qF "$NEEDLE"
  ps=("${PIPESTATUS[@]}")
  [ "${ps[1]}" = "0" ] && [ "${ps[0]}" != "0" ] && lost3=$((lost3+1))
done
if [ "$lost3" -gt 0 ]; then
  ok "multi-write external producer: grep MATCHED but the producer was stranded in $lost3/$ITERS runs"
else
  no "control 3 is inert: a two-write producer was never stranded ($lost3/$ITERS)." \
     "This environment cannot exhibit the case_b2044d98 shape, so the scan below proves nothing here."
fi

echo
echo "=== the fix: a herestring has no producer to strand ==="
bad=0
for ((i = 0; i < ITERS; i++)); do
  grep -qF "$NEEDLE" <<<"$PAYLOAD" || bad=$((bad+1))
done
if [ "$bad" = "0" ]; then
  ok "fixed form: the match is reported $ITERS/$ITERS times"
else
  no "fixed form lost the match in $bad/$ITERS runs"
fi

# ...and it must still be capable of reporting a genuine miss, or it is vacuous.
if grep -qF "a string that is definitely not in the payload" <<<"$PAYLOAD"; then
  no "fixed form reported a match for a pattern that is not present"
else
  ok "fixed form still reports a genuine miss as a miss"
fi

# Capture-first is the same fix for an external producer: the command runs to completion
# inside $( ), so nothing is left writing when grep leaves.
bad3=0
for ((i = 0; i < ITERS; i++)); do
  cap="$(bash -c 'printf "%s\n" "$1"; sleep 0.05; printf "second chunk\n"' _ "$NEEDLE")"
  grep -qF "$NEEDLE" <<<"$cap" || bad3=$((bad3+1))
done
if [ "$bad3" = "0" ]; then
  ok "capture-first form: the match is reported $ITERS/$ITERS times for a multi-write producer"
else
  no "capture-first form lost the match in $bad3/$ITERS runs"
fi

echo
echo "=== no pipeline in the tree feeds an early-exiting grep under pipefail ==="

# Operator scripts (not test code) that still carry the construct. Each one's producer
# emits a single line or a tiny single write and then exits (`find ... -print -quit`,
# `podman ps`, a printf builtin of two tokens), so none of them can lose this race, and
# case_b2044d98 was bounded to test scripts. They are listed so the exception is visible and
# cannot grow silently: scan() fails if an entry stops matching (a stale exemption), and
# anything not listed here fails the gate.
EXEMPT="capture_host_render.sh github-runner/verify.sh omniroute/case_d06b6dba-register-teamclaude.sh ops/case_4ea7d92e/git_remote_credential_sweep_check.sh"

scan() { # scan <dir> [self] [exempt] -- one "file:line: text" per offending site; rc 1 = hits, 2 = stale list
  SCANDIR="$1" SCANSELF="${2:-}" SCANEXEMPT="${3:-}" python3 - <<'PY'
# This gate's own file is the one place the construct must survive: the header quotes the
# defective lines and the positive controls above deliberately RUN them. Skipping it is not
# a carve-out for production code -- the planted-violation controls below prove the scanner
# still fires, and they do it on files that are not exempt.
import os, re, subprocess, pathlib, sys

root = pathlib.Path(os.environ["SCANDIR"])
selfpath = os.environ.get("SCANSELF") or None
exempt = set(os.environ.get("SCANEXEMPT", "").split())

def tracked():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.split("\0")
    except Exception:
        out = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    return [f for f in out if f]

def shell_kind(path):
    """'script' (has a shell shebang), 'lib' (a .sh with none: sourced, so it runs under
    its caller's pipefail), or None (not shell)."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return None
    if re.match(rb"#!\s*(/usr/bin/env\s+)?(/\S*/)?(ba)?sh\b", first):
        return "script"
    return "lib" if path.suffix == ".sh" else None

HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

def logical_lines(text):
    """Yield (first_line_no, joined_text). Heredoc bodies are skipped (they are another
    program's source). A line ending in a backslash, `|`, `&&` or `||` joins the next."""
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        start, buf, tags = i + 1, "", []
        while i < n:
            raw = lines[i]
            i += 1
            if raw.strip().startswith("#") and not buf:
                break
            tags += [m.group(2) for m in HEREDOC.finditer(raw)]
            if raw.endswith("\\"):
                buf += raw[:-1] + " "
                continue
            buf += raw
            if re.search(r"(?<!\|)\|\s*$|&&\s*$|\|\|\s*$", re.sub(r"\s+#.*$", "", raw)):
                buf += " "
                continue
            break
        for tag in tags:
            j = i
            while j < n and lines[j].strip() != tag:
                j += 1
            if j < n:           # an unterminated tag is not a heredoc; skip nothing
                i = j + 1
        if buf.strip():
            yield start, buf

def stages(line):
    """Split a logical line into [(operator_before, text)], honouring quotes. Pipes
    inside $( ) still count: the pipeline there is a pipeline."""
    out, cur, op, q, i, n = [], [], None, None, 0, len(line)
    def cut(new_op):
        nonlocal cur, op
        out.append((op, "".join(cur)))
        cur, op = [], new_op
    while i < n:
        c = line[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                cur.append(line[i + 1]); i += 1
            elif c == q:
                q = None
        elif c == "\\" and i + 1 < n:
            cur += [c, line[i + 1]]; i += 1
        elif c in "'\"":
            q = c; cur.append(c)
        elif c == "#" and (i == 0 or line[i - 1].isspace()):
            break
        elif c == "|":
            if line[i:i + 2] == "||":
                cut("||"); i += 1
            elif line[i:i + 2] == "|&":
                cut("|"); i += 1
            else:
                cut("|")
        elif c == "&" and line[i:i + 2] == "&&":
            cut("&&"); i += 1
        elif c == "&" and (line[i - 1:i] in (">", "<") or line[i + 1:i + 2] == ">"):
            cur.append(c)
        elif c == ";" or (c in "(){}" and not (c == "(" and line[i - 1:i] == "$")
                          and not (c in "{}" and (line[i - 1:i] == "$" or line[i + 1:i + 2].isalnum()
                                                  or line[i - 1:i].isalnum()))):
            cut(";")
        else:
            cur.append(c)
        i += 1
    cut(None)
    return out

def words(s):
    ws, cur, q = [], "", None
    for c in s:
        if q:
            cur += c
            if c == q:
                q = None
        elif c in "'\"":
            q = c; cur += c
        elif c.isspace():
            if cur:
                ws.append(cur); cur = ""
        else:
            cur += c
    if cur:
        ws.append(cur)
    return ws

LEAD = re.compile(r"^(?:(?:if|elif|while|until|then|do|else|!|command|exec|time|builtin)$|[A-Za-z_]\w*=\S*$)")
GREPS = {"grep", "egrep", "fgrep", "/usr/bin/grep", "/bin/grep"}

def early_grep(stage):
    """True if this stage is a grep that can stop reading before EOF: -q/--quiet/--silent,
    -l, or -m N / --max-count. Every other grep drains stdin and cannot strand a producer."""
    ws = words(stage)
    k = 0
    while k < len(ws) and LEAD.match(ws[k]):
        k += 1
    if k >= len(ws) or ws[k] not in GREPS:
        return False
    rest, skip, j = ws[k + 1:], False, 0
    while j < len(rest):
        w = rest[j]; j += 1
        if skip:
            skip = False
            continue
        if w == "--":
            break
        if w in ("--quiet", "--silent", "--files-with-matches") or w.startswith("--max-count"):
            return True
        if w.startswith("--") or not w.startswith("-") or w == "-":
            continue
        for ci, c in enumerate(w[1:], 1):
            if c in "qlm":
                return True
            if c in "efABCdD":      # takes an argument: the rest of the word, or the next
                if ci == len(w) - 1:
                    skip = True
                break
    return False

def subshells(line):
    """Yield the text inside every balanced $( ... ) -- including ones inside double
    quotes, which stages() treats as opaque -- and inside those, recursively."""
    i, n, sq = 0, len(line), False
    while i < n:
        c = line[i]
        if sq:
            sq = c != "'"
        elif c == "\\":
            i += 1
        elif c == "'":
            sq = True
        elif line[i:i + 2] == "$(" and line[i:i + 3] != "$((":
            depth, j, q = 1, i + 2, None
            while j < n and depth:
                d = line[j]
                if q:
                    if d == "\\" and q == '"':
                        j += 1
                    elif d == q:
                        q = None
                elif d == "\\":
                    j += 1
                elif d in "'\"":
                    q = d
                elif d == "(":
                    depth += 1
                elif d == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                inner = line[i + 2:j - 1]
                yield inner
                yield from subshells(inner)
        i += 1

def has_early_pipe(line):
    return any(op == "|" and early_grep(seg) for op, seg in stages(line))

def scan_text(text):
    if not re.search(r"\bpipefail\b", text):
        return None
    hits = []
    for ln, l in logical_lines(text):
        if "|" not in l:
            continue
        if has_early_pipe(l) or any(has_early_pipe(x) for x in subshells(l)):
            hits.append((ln, l.strip()))
    return hits

total, stale = 0, []
seen_exempt = {}
for f in tracked():
    p = root / f
    if not p.is_file() or f == selfpath:
        continue
    kind = shell_kind(p)
    if kind is None:
        continue
    try:
        text = p.read_text()
    except Exception:
        continue
    hits = scan_text(text)
    if hits is None:
        if kind == "script":
            continue
        hits = scan_text("set -o pipefail\n" + text)   # a sourced lib inherits its caller's
        hits = [(ln - 1, l) for ln, l in (hits or [])]
    if f in exempt:
        seen_exempt[f] = len(hits)
        continue
    for ln, l in hits:
        print(f"{f}:{ln}: {l[:100]}")
        total += 1
for f in sorted(exempt):
    if not seen_exempt.get(f):
        stale.append(f)
if selfpath and selfpath not in tracked():
    print(f"SELF-EXCLUSION STALE: {selfpath} is not in the scanned file list")
    sys.exit(2)
if stale:
    for f in stale:
        print(f"EXEMPTION STALE: {f} no longer has a flagged site (or is not tracked) -- remove it from the list")
    sys.exit(2)
sys.exit(1 if total else 0)
PY
}

SELF_REL="${BASH_SOURCE[0]#./}"; SELF_REL="verification/$(basename "$SELF_REL")"
hits="$(scan "$ROOT" "$SELF_REL" "$EXEMPT")"; scan_rc=$?
if [ "$scan_rc" = "2" ]; then
  no "the self-exclusion or the exemption list no longer matches the tree:" "$hits"
elif [ "$scan_rc" = "0" ]; then
  ok "zero pipefail-poisoned verdict sites in the tree (this file and the listed operator scripts excepted)"
else
  n=$(grep -c '' <<<"$hits")
  no "$n site(s) still pipe a producer into an early-exiting grep under pipefail:" \
     "capture first: out=\"\$(cmd)\"; grep -q PAT <<<\"\$out\"" "$(sed 's/^/        /' <<<"$hits")"
fi

# --- The scanner must be able to FAIL, in every spelling. A scan that cannot go red has
# measured nothing. Each case is a throwaway git repo holding one planted file.
probe="$(mktemp -d)"; trap 'rm -rf "$probe"' EXIT
plant() { # plant <name> <expect: hit|clean> <file body...>
  local name="$1" want="$2" body="$3" d="$probe/$1"
  mkdir -p "$d"
  ( cd "$d" && git init -q . && git config user.email a@b && git config user.name a )
  printf '%s\n' "$body" > "$d/planted.sh"
  ( cd "$d" && git add -A && git commit -qm p )
  scan "$d" >/dev/null; local rc=$?
  if [ "$want" = hit ] && [ "$rc" = 1 ]; then ok "scanner control: $name is detected"
  elif [ "$want" = clean ] && [ "$rc" = 0 ]; then ok "scanner control: $name is correctly NOT flagged"
  else no "scanner control: $name expected $want, scan rc=$rc"; fi
}
HDR=$'#!/usr/bin/env bash\nset -euo pipefail'
plant "printf of a variable"           hit   "$HDR"$'\nif printf \'%s\' "$out" | grep -qF "needle"; then :; fi'
plant "echo of a variable"             hit   "$HDR"$'\nif echo "$OUT" | grep -q "needle"; then :; fi'
plant "external tool (the :683 shape)" hit   "$HDR"$'\n./tool --help 2>/dev/null | grep -q \'READ-ONLY\' && echo ok'
plant "shell function producer"        hit   "$HDR"$'\nparse_push | grep -qi "success=true" && echo ok'
plant "negated pipeline"               hit   "$HDR"$'\nif ! ./tool | grep -q needle; then :; fi'
plant "grep -m 1 reader"               hit   "$HDR"$'\n./tool | grep -m 1 needle >/dev/null'
plant "-l reader inside a cluster"     hit   "$HDR"$'\n./tool | grep -il needle >/dev/null'
plant "continuation line"              hit   "$HDR"$'\nif ./tool \\\n    | grep -q needle; then :; fi'
plant "pipe at end of line"            hit   "$HDR"$'\nif ./tool |\n    grep -q needle; then :; fi'
plant "command-prefixed grep"          hit   "$HDR"$'\nprintf x | command grep -qIE "pat"'
plant "env-prefixed grep"              hit   "$HDR"$'\nprintf x | LC_ALL=C grep -q pat'
plant "pipeline inside \$( )"          hit   "$HDR"$'\nv="$(./tool | grep -q needle && echo y)"'
plant "second stage of a chain"        hit   "$HDR"$'\n./tool | sed s/a/b/ | grep -q needle'
plant "sourced lib (no shebang)"       hit   $'foo() { ./tool | grep -q needle; }'
plant "herestring (the fix)"           clean "$HDR"$'\nout="$(./tool --help)"\ngrep -q needle <<<"$out"'
plant "grep -q reading a file"         clean "$HDR"$'\ngrep -q needle file.txt'
plant "grep -q as the FIRST stage"     clean "$HDR"$'\ngrep -q needle file.txt | cat'
plant "draining grep (-c, no -q)"      clean "$HDR"$'\n./tool | grep -c needle'
plant "-m inside a quoted pattern"     clean "$HDR"$'\n./tool | grep -E "a -m b" >/dev/null'
plant "-e takes the next word as pattern" clean "$HDR"$'\n./tool | grep -e -q >/dev/null'
plant "same line without pipefail"     clean $'#!/usr/bin/env bash\nset -eu\n./tool | grep -q needle'
plant "pattern only in a comment"      clean "$HDR"$'\n# ./tool | grep -q needle'
plant "pipe inside a heredoc body"     clean "$HDR"$'\ncat >stub.sh <<\'EOF\'\n./tool | grep -q needle\nEOF\ngrep -q x <<<"$y"'
plant "herestring is not a heredoc"    hit   "$HDR"$'\ngrep -q x <<<"foo"\n./tool | grep -q needle'

# A stale exemption must be reported, not silently honoured.
d="$probe/stale-exempt"; mkdir -p "$d"
( cd "$d" && git init -q . && git config user.email a@b && git config user.name a )
printf '#!/usr/bin/env bash\nset -euo pipefail\ngrep -q x <<<"$y"\n' > "$d/op.sh"
( cd "$d" && git add -A && git commit -qm p )
scan "$d" "" "op.sh" >/dev/null; rc=$?
if [ "$rc" = 2 ]; then ok "scanner control: an exemption that no longer matches is reported stale"
else no "scanner control: a stale exemption was honoured (rc=$rc)"; fi

echo
if [ "$fail" = "0" ]; then
  grn "sigpipe verdict gate: $pass passed, 0 failed"
else
  red "sigpipe verdict gate: $pass passed, $fail FAILED"
fi
exit $((fail > 0 ? 1 : 0))
