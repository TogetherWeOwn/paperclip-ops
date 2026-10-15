#!/usr/bin/env bash
# case_1ccef1dc. Offline, hermetic suite for the review-stack kit under ops/case_1ccef1dc/:
#   1. review-bot manifest: least privilege, no webhook, private, registration link in sync
#   2. Greptile config: free-tier guards cannot be loosened, no internal card id in a public file
#   3. build_corpus.py: seed applied exactly once, reviewer sandbox carries no ground truth
#   3c. corpus v2: weakened-test edits, second seeds, survival merge, data invariants
#   4. score.py: recall, false-positive accounting, paired test, bad-input exit code
#   5. observational analyze.py: per-model rows and head-conflict count on a fixture
# No network, no credential, no model call. Asserts on values and exit status, not message text.
set -u
cd "$(dirname "$0")"
K=ops/case_1ccef1dc
FAIL=0
ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; FAIL=1; }
check() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
T=$(mktemp -d "${TMPDIR:-/tmp}/case_c1a76495.XXXXXX"); trap 'rm -rf "$T"' EXIT

# --- 1. review-bot manifest -------------------------------------------------------------
python3 - "$K/review-bot" <<'PY' && ok "manifest: exact least-privilege permission set, private, no webhook/events" || bad "manifest invariants"
import json, sys
m = json.load(open(sys.argv[1] + "/app-manifest.json"))
assert m["default_permissions"] == {"metadata": "read", "pull_requests": "write", "checks": "read", "statuses": "read"}, m["default_permissions"]
assert m["public"] is False
assert m.get("default_events") == []
assert "hook_attributes" not in m
assert not any(k.startswith("organization_") for k in m["default_permissions"])
PY
python3 - "$K/review-bot" <<'PY' && ok "registration link: params equal the manifest, webhook off, org-scoped, no contents" || bad "registration link drift"
import json, subprocess, sys
from urllib.parse import urlparse, parse_qs
d = sys.argv[1]
url = subprocess.run([sys.executable, d + "/registration_url.py"], capture_output=True, text=True, check=True).stdout.strip()
u = urlparse(url)
assert u.scheme == "https" and u.netloc == "github.com"
assert u.path == "/organizations/TogetherWeOwn/settings/apps/new", u.path
q = {k: v[0] for k, v in parse_qs(u.query).items()}
m = json.load(open(d + "/app-manifest.json"))
assert q["webhook_active"] == "false" and q["public"] == "false" and q["name"] == m["name"]
perms = {k: v for k, v in q.items() if k in ("metadata", "pull_requests", "checks", "statuses", "contents", "actions", "administration", "issues", "workflows")}
assert perms == m["default_permissions"], perms
PY

check "README registration link equals the generated one" bash -c \
  "grep -Fxq \"   \$(python3 $K/review-bot/registration_url.py)\" $K/README.md"

# --- 2. Greptile free-tier guards -------------------------------------------------------
python3 - "$K/greptile" <<'PY' && ok "greptile config: base tier, no auto review, no auto-approve, one author, no card id" || bad "greptile guards"
import json, re, sys
raw = open(sys.argv[1] + "/two-web-next.greptile-config.json").read()
c = json.loads(raw)
assert c["effort"] == "base"
assert c["autoReview"] == []
assert c["statusCheck"] is False and c["shouldUpdateDescription"] is False
assert c["autoApprove"] == {"enabled": False}
assert c["includeAuthors"] == ["togetherweown[bot]"]
assert not re.search(r"TOG-\d+", raw), "internal card id in a file destined for a public repo"
PY
check "greptile kit states the zero-spend layers" bash -c "grep -q 'Flex Usage Limit' $K/greptile/KIT.md && grep -q 'No payment method\|no payment method' $K/greptile/KIT.md && grep -q 'Only select repositories' $K/greptile/KIT.md"

# --- 3. build_corpus.py on a throwaway repo ---------------------------------------------
R=$T/repo; mkdir -p "$R/src"
git -C "$R" init -q && git -C "$R" config user.email t@example.invalid && git -C "$R" config user.name t
printf 'line1\nline2\nreturn a > 0 && a <= 9;\nline4\n' > "$R/src/x.ts"; git -C "$R" add -A && git -C "$R" commit -qm base
printf 'line1\nline2\nreturn a > 0 && a <= 9;\nline4\nextra\n' > "$R/src/x.ts"; git -C "$R" add -A && git -C "$R" commit -qm "fix(x): thing (#7)" -m "body text"
SHA=$(git -C "$R" rev-parse --short HEAD)
cat > "$T/bases.json" <<EOF
{"repo": "o/r", "bases": [{"pr": 7, "sha": "$SHA", "area": "x"}]}
EOF
cat > "$T/seeds.json" <<'EOF'
{"seeds": [{"id": "T1", "pr": 7, "file": "src/x.ts", "old": "a > 0 && a <= 9", "new": "a >= 0 && a <= 9",
            "class": "boundary", "severity": "P2", "desc": "UNIQUE-GROUND-TRUTH-MARKER"}]}
EOF
check "build_corpus: builds clean and seeded variants" \
  python3 $K/reviewer-eval/build_corpus.py --repo "$R" --bases "$T/bases.json" --seeds "$T/seeds.json" --out "$T/out"
check "build_corpus: seed line anchor is the changed line (3)" \
  python3 -c "import json,sys;m=json.load(open('$T/out/7-T1/meta.json'));sys.exit(0 if m['truth']['line']==3 and m['seeded'] else 1)"
check "build_corpus: seeded diff carries the seeded text, clean diff does not" \
  bash -c "grep -q 'a >= 0' $T/out/view/7-T1/pr.diff && ! grep -q 'a >= 0' $T/out/view/7-clean/pr.diff"
check "build_corpus: reviewer sandbox holds no ground truth, no .git, no meta" \
  bash -c "! grep -rq UNIQUE-GROUND-TRUTH-MARKER $T/out/view && [ ! -e $T/out/view/7-T1/meta.json ] && [ ! -e $T/out/view/7-T1/tree/.git ]"
printf '{"seeds": [{"id": "T2", "pr": 7, "file": "src/x.ts", "old": "NOT-PRESENT", "new": "z", "class": "c", "severity": "P2", "desc": "d"}]}' > "$T/seeds_bad.json"
python3 $K/reviewer-eval/build_corpus.py --repo "$R" --bases "$T/bases.json" --seeds "$T/seeds_bad.json" --out "$T/out_bad" >/dev/null 2>&1
check "build_corpus: a seed whose old text is absent aborts (non-zero)" test $? -ne 0

# --- 3b. make_arena.py and arm_import.py -------------------------------------------------
check "make_arena: opaque ids, map outside the arena, refuses an existing arena" bash -c "
  python3 $K/reviewer-eval/make_arena.py --corpus $T/out --arena $T/arena --map $T/amap.json --seed 5 >/dev/null &&
  python3 -c \"import json;m=json.load(open('$T/amap.json'));assert sorted(m)==['r01','r02'] and sorted(m.values())==['7-T1','7-clean']\" &&
  [ -f $T/arena/r01/pr.diff ] && [ ! -e $T/arena/r01/meta.json ] && ! find $T/arena -name amap.json | grep -q . &&
  ! grep -rq UNIQUE-GROUND-TRUTH-MARKER $T/arena &&
  true"
python3 $K/reviewer-eval/make_arena.py --corpus "$T/out" --arena "$T/arena" --map "$T/amap2.json" --seed 5 >/dev/null 2>&1; RC_EXIST=$?
check "make_arena: an existing arena is refused with exit 2" test $RC_EXIST -eq 2
python3 $K/reviewer-eval/make_arena.py --corpus "$T/out" --arena "$T/arena3" --map "$T/arena3/map.json" --seed 5 >/dev/null 2>&1; RC_INSIDE=$?
check "make_arena: a map inside the arena is refused with exit 2, arena not created" bash -c "[ $RC_INSIDE -eq 2 ] && [ ! -e $T/arena3 ]"
mkdir -p "$T/many/view"; for i in 1 2 3 4 5 6 7 8; do mkdir -p "$T/many/view/v$i"; echo "$i" > "$T/many/view/v$i/pr.diff"; done
python3 $K/reviewer-eval/make_arena.py --corpus "$T/many" --arena "$T/ma5" --map "$T/m5.json" --seed 5 >/dev/null
python3 $K/reviewer-eval/make_arena.py --corpus "$T/many" --arena "$T/ma6" --map "$T/m6.json" --seed 6 >/dev/null
check "make_arena: order is shuffled and depends on the seed" python3 -c "
import json
a=list(json.load(open('$T/m5.json')).values()); b=list(json.load(open('$T/m6.json')).values())
assert a!=sorted(a) and b!=sorted(b) and a!=b, (a,b)"
check "make_arena: same seed reproduces the same map" bash -c "
  python3 $K/reviewer-eval/make_arena.py --corpus $T/out --arena $T/arena2 --map $T/amap_b.json --seed 5 >/dev/null && cmp -s $T/amap.json $T/amap_b.json"
check "make_arena: the map write goes through write_json_atomic, no plain-open writer" bash -c "
  grep -q 'from atomic_state import write_json_atomic' $K/reviewer-eval/make_arena.py &&
  grep -q 'write_json_atomic(a.map, mapping)' $K/reviewer-eval/make_arena.py &&
  ! grep -n 'open(' $K/reviewer-eval/make_arena.py"
mkdir -p "$T/readonly" && printf 'previous-bytes\n' > "$T/readonly/amap.json" && chmod 555 "$T/readonly"
python3 $K/reviewer-eval/make_arena.py --corpus "$T/out" --arena "$T/arena_ro" --map "$T/readonly/amap.json" --seed 5 >/dev/null 2>&1; RC_RO=$?
check "make_arena: a failing map write exits non-zero and leaves previous bytes" bash -c "[ $RC_RO -ne 0 ] && cmp -s $T/readonly/amap.json <(printf 'previous-bytes\n')"
chmod 755 "$T/readonly"
mkdir -p "$T/raw"
printf 'prose before {"verdict":"CHANGES","findings":[{"file":"src/x.ts","line":3,"severity":"P2","title":"t"}]}' > "$T/raw/r01.json"
printf 'not json at all' > "$T/raw/r02.json"
INV=$(python3 $K/reviewer-eval/arm_import.py --map "$T/amap.json" --raw "$T/raw" --arm a --out "$T/imp")
check "arm_import: parses embedded JSON, counts the unparseable as invalid" bash -c "
  [ '$INV' = 1 ] && python3 -c \"
import json,glob
fs={f.split('/')[-1]:json.load(open(f)) for f in glob.glob('$T/imp/a/*.json')}
inv=[k for k,v in fs.items() if v.get('invalid')]
ok=[k for k,v in fs.items() if v['verdict']=='CHANGES' and len(v['findings'])==1]
assert len(fs)==2 and len(inv)==1 and len(ok)==1 and fs[inv[0]]['verdict']=='APPROVE'\""

# --- 3c. corpus v2: weakened tests, second seeds, survival merge --------------------------
V=$K/reviewer-eval/v2
R2=$T/repo2; mkdir -p "$R2/src" "$R2/test"
git -C "$R2" init -q && git -C "$R2" config user.email t@example.invalid && git -C "$R2" config user.name t
printf 'export const ok = (a: number) => a > 0 && a <= 9;\nexport const q = (b: number) => b + 1;\n' > "$R2/src/x.ts"
printf 'expect(ok(9)).toBe(true);\nexpect(q(1)).toBe(2);\n' > "$R2/test/x.test.ts"
git -C "$R2" add -A && git -C "$R2" commit -qm base
printf 'export const ok = (a: number) => a > 0 && a <= 9;\nexport const q = (b: number) => b + 1;\n// pr\n' > "$R2/src/x.ts"
git -C "$R2" add -A && git -C "$R2" commit -qm "fix(x): thing (#7)" -m "body text"
SHA2=$(git -C "$R2" rev-parse --short HEAD)
mkdir -p "$T/ev2" "$T/ws2"
printf '{"repo": "o/r", "bases": [{"pr": 7, "sha": "%s", "area": "x"}]}' "$SHA2" > "$T/ev2/bases.json"
cat > "$T/ev2/seeds.json" <<'EOF'
{"seeds": [{"id": "S01", "pr": 7, "file": "src/x.ts", "old": "a > 0 && a <= 9", "new": "a >= 0 && a <= 9",
            "class": "boundary", "severity": "P2", "desc": "UNIQUE-SEED-MARKER"},
           {"id": "S01b", "pr": 7, "file": "src/x.ts", "old": "b + 1", "new": "b + 3",
            "class": "unit", "severity": "P2", "desc": "UNIQUE-SEED-B-MARKER"}]}
EOF
cat > "$T/ws2/v2_seeds.py" <<'EOF'
V2_SEEDS = [dict(id="T1", pr=7, file="src/x.ts", old="// pr", new="// pr2", klass="off-by-one", severity="P1", desc="UNIQUE-T-MARKER")]
EOF
cat > "$T/ws2/v2_weakened.py" <<'EOF'
V2_WEAKENED = [
 dict(seed="S01", file="test/x.test.ts", old="expect(ok(9)).toBe(true);", new="// UNIQUE-WEAKEN-MARKER", note="drop the boundary case"),
 dict(seed="S01b", file="test/x.test.ts", old="expect(q(1)).toBe(2);", new="// second weakening folds to S01", note="subkey fold"),
]
EOF
cat > "$T/ws2/v2_weakened_T.py" <<'EOF'
V2_WEAKENED_T = [dict(seed="T1", file="test/x.test.ts", old="expect(q(1)).toBe(2);", new="it.skip('q', () => {});", note="skip the q case")]
EOF
check "build_v2: clean + v1 seed + T seed variants, corpus tagged v2" bash -c "
  python3 $V/build_v2.py --repo $R2 --ws3 $T/ws2 --eval-dir $T/ev2 --out $T/out2 >/dev/null &&
  python3 -c \"
import json
r=json.load(open('$T/out2/corpus-report.json'))
assert sorted(e['variant'] for e in r)==['7-S01','7-S01b','7-T1','7-clean'], r
assert json.load(open('$T/out2/7-S01/meta.json'))['corpus']=='v2'\""
check "build_v2: weakened edits (incl. S01b folded to S01) land in the seeded diff only" bash -c "
  grep -q 'UNIQUE-WEAKEN-MARKER' $T/out2/7-S01/pr.diff && grep -q 'second weakening folds' $T/out2/7-S01/pr.diff &&
  grep -q 'a >= 0' $T/out2/7-S01/pr.diff &&
  ! grep -q 'UNIQUE-WEAKEN-MARKER' $T/out2/7-clean/pr.diff && ! grep -q 'UNIQUE-WEAKEN-MARKER' $T/out2/7-T1/pr.diff"
check "build_v2: weakened_test recorded in meta for S01 (two edits) and T1 (one)" python3 -c "
import json
a=json.load(open('$T/out2/7-S01/meta.json'))['weakened_test']; b=json.load(open('$T/out2/7-T1/meta.json'))['weakened_test']
assert [x['file'] for x in a]==['test/x.test.ts','test/x.test.ts'] and [x['file'] for x in b]==['test/x.test.ts'], (a,b)"
check "build_v2: a 4-char seed id (S01b) does not inherit the edits of S01 and says so" python3 -c "
import json
b=json.load(open('$T/out2/7-S01b/meta.json'))
assert b['weakened_test']==[{'file':None,'note':'no contradicting test'}] and b['seeded'], b
assert 'UNIQUE-WEAKEN-MARKER' not in open('$T/out2/7-S01b/pr.diff').read()"
check "build_v2: weakening_visible is true only where the edit adds a skip" bash -c "
  python3 -c \"
import json
f=lambda v: json.load(open('$T/out2/'+v+'/meta.json'))['weakening_visible']
assert f('7-S01') is False and f('7-clean') is False and f('7-S01b') is False and f('7-T1') is True
\" "
check "build_v2: reviewer sandbox holds no ground truth, weakening note, meta or .git" bash -c "
  ! grep -rqE 'UNIQUE-(SEED|SEED-B|T)-MARKER|drop the boundary case|subkey fold' $T/out2/view &&
  [ ! -e $T/out2/view/7-S01/meta.json ] && [ ! -e $T/out2/view/7-S01/tree/.git ] && [ -f $T/out2/view/7-S01/tree/test/x.test.ts ]"
cp "$T/ws2/v2_weakened.py" "$T/ws2/v2_weakened.py.ok"
sed -i 's/old="expect(q(1)).toBe(2);"/old="NOT-PRESENT-ANYWHERE"/' "$T/ws2/v2_weakened.py"
python3 $V/build_v2.py --repo "$R2" --ws3 "$T/ws2" --eval-dir "$T/ev2" --out "$T/out2b" >/dev/null 2>&1
check "build_v2: a weakening whose anchor is absent aborts (non-zero)" test $? -ne 0
cat > "$T/ws2/v2_weakened.py" <<'EOF'
V2_WEAKENED = [dict(seed="S01", file="test/x.test.ts", old="expect(", new="// twice", note="ambiguous anchor")]
EOF
python3 $V/build_v2.py --repo "$R2" --ws3 "$T/ws2" --eval-dir "$T/ev2" --out "$T/out2c" >/dev/null 2>&1
check "build_v2: a weakening whose anchor matches twice aborts (non-zero)" test $? -ne 0
python3 - "$V" "$K/reviewer-eval/seeds.json" "$K/reviewer-eval/bases.json" <<'PY' && ok "v2 data: ids unique, every edit has distinct file/old/new, every key maps to a seed on a base PR" || bad "v2 data invariants"
import json, sys
sys.path.insert(0, sys.argv[1])
from v2_seeds import V2_SEEDS
from v2_weakened import V2_WEAKENED
from v2_weakened_T import V2_WEAKENED_T
v1 = json.load(open(sys.argv[2]))["seeds"]
prs = {b["pr"] for b in json.load(open(sys.argv[3]))["bases"]}
ids = [s["id"] for s in v1] + [s["id"] for s in V2_SEEDS]
assert len(ids) == len(set(ids)), "duplicate seed id"
assert len(V2_SEEDS) == 12 and all(s["pr"] in prs for s in V2_SEEDS)
for s in V2_SEEDS:
    assert s["file"] and s["old"] and s["new"] and s["old"] != s["new"], s["id"]
def sub(i):
    return i[:3] if len(i) == 4 and i[3:].isalpha() else i
for w in V2_WEAKENED + V2_WEAKENED_T:
    assert sub(w["seed"]) in ids, w["seed"]
    if w["file"] is None:  # a recorded decision not to weaken: explicit, never an empty edit
        assert w["old"] is None and w["new"] is None and w["note"].startswith("NO-EDIT"), w
        continue
    assert w["file"].startswith("test/") and w["old"] and w["new"] and w["old"] != w["new"], w
assert all(sub(w["seed"]).startswith("S") for w in V2_WEAKENED) and all(sub(w["seed"]).startswith("T") for w in V2_WEAKENED_T)
PY
mkdir -p "$T/sv"; cp -r "$T/out2/7-S01" "$T/out2/7-S01b" "$T/out2/7-T1" "$T/out2/7-clean" "$T/sv/"; cp "$T/out2/corpus-report.json" "$T/sv/"
printf '[{"variant":"7-S01","seed":"S01","exit":0,"ci_silent":true},{"variant":"7-S01b","seed":"S01b","exit":0,"ci_silent":true},{"variant":"7-T1","seed":"T1","exit":0,"ci_silent":true}]' > "$T/nodb.json"
printf '[{"variant":"7-S01","seed":"S01","exit":1,"ci_silent":false},{"variant":"7-S01b","seed":"S01b","exit":0,"ci_silent":true},{"variant":"7-T1","seed":"T1","exit":0,"ci_silent":true}]' > "$T/withdb.json"
python3 $V/merge_survival.py --corpus "$T/sv" --nodb "$T/nodb.json" --db "$T/withdb.json" >/dev/null
check "merge_survival: silent only if green with AND without a database; clean untouched" python3 -c "
import json
a=json.load(open('$T/sv/7-S01/meta.json')); b=json.load(open('$T/sv/7-T1/meta.json')); c=json.load(open('$T/sv/7-clean/meta.json'))
assert a['ci_silent'] is False and a['survival']['no_database']['silent'] is True and a['survival']['with_database']['silent'] is False, a['survival']
assert b['ci_silent'] is True and 'ci_silent' not in c"
check "merge_survival: a DB-red variant whose clean baseline is red too stays silent (unknown, recorded)" bash -c "
  mkdir -p $T/sv2 && cp -r $T/sv/7-S01 $T/sv/7-S01b $T/sv/7-T1 $T/sv/7-clean $T/sv/corpus-report.json $T/sv2/ &&
  printf '[{\"variant\":\"7-S01\",\"seed\":\"S01\",\"exit\":1,\"ci_silent\":false,\"baseline_exit\":1},{\"variant\":\"7-S01b\",\"seed\":\"S01b\",\"exit\":0,\"ci_silent\":true,\"baseline_exit\":0},{\"variant\":\"7-T1\",\"seed\":\"T1\",\"exit\":0,\"ci_silent\":true,\"baseline_exit\":0}]' > $T/withdb2.json &&
  python3 $V/merge_survival.py --corpus $T/sv2 --nodb $T/nodb.json --db $T/withdb2.json >/dev/null &&
  python3 -c \"
import json
a=json.load(open('$T/sv2/7-S01/meta.json'))
assert a['ci_silent'] is True and a['survival']['with_database']['silent'] is None and a['survival']['with_database']['baseline_exit']==1, a['survival']\""
refused() { python3 $V/survival_all.py --dir "$T/sv" --database-url "$1" 2>&1 >/dev/null | grep -q '^refusing: --database-url'; }
check "survival_all: a CI-service --database-url is refused before any process runs" refused "postgres://postgres:ci@localhost:5432/postgres"
check "survival_all: a non-test host is refused too" refused "postgres://person-b7262c29@example.invalid:5432/two_web_next"
check "survival_all: a production-looking database name on the test host is refused" refused "postgres://agent_test@agent-testdb:5432/production"
check "survival_all: a password in the URL is refused" refused "postgres://agent_test:secret@agent-testdb:5432/two_web_next"
check "survival_all: a {pr} template is expanded per base PR and still has to be sanctioned" python3 - "$V" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import survival_all as s
assert s.url_for("postgres://agent_test@agent-testdb:5432/two_web_next_x_pr{pr}", 114) == "postgres://agent_test@agent-testdb:5432/two_web_next_x_pr114"
try:
    s.url_for("postgres://agent_test@agent-testdb:5432/production_{pr}", 114)
    raise AssertionError("production-looking name accepted")
except SystemExit:
    pass
PY
refused_setup() { python3 $V/db_setup.py --corpus "$T/no-such-corpus" --template "$1" 2>&1 >/dev/null | grep -q '^refusing: --database-url'; }
check "db_setup: a non-sanctioned template is refused before any database is touched" refused_setup "postgres://postgres:ci@localhost:5432/postgres"
check "db_setup: a production-looking name is refused too" refused_setup "postgres://agent_test@agent-testdb:5432/production_{pr}"
printf '[{"variant":"7-S01","seed":"S01","exit":0,"ci_silent":true}]' > "$T/partial.json"
python3 $V/merge_survival.py --corpus "$T/sv" --nodb "$T/partial.json" >/dev/null 2>&1
check "merge_survival: a seeded variant with no survival row is refused (exit 2)" test $? -eq 2

# --- 3d. build_v2 atomic writes (case_a896464a): no plain-open writer, ENOSPC keeps bytes --
check "build_v2 atomic: helper unit + no-plain-open + failure-preserves-bytes" bash -c \
  "(cd $V && python3 -m unittest test_build_v2_atomic)"

# --- 4. score.py -------------------------------------------------------------------------
C=$T/c; F=$T/f; mkdir -p "$C/1-S1" "$C/2-S2" "$C/3-clean" "$F/good" "$F/noisy"
meta() { printf '{"variant":"%s","seeded":%s,"ci_silent":%s,"truth":%s}' "$1" "$2" "$4" "$3" > "$C/$1/meta.json"; }
meta 1-S1 true '{"file":"src/a.ts","line":50,"severity":"P1","class":"c","desc":"d"}' true
meta 2-S2 true '{"file":"src/b.ts","line":10,"severity":"P2","class":"c","desc":"d"}' false
meta 3-clean false null null
f() { printf '{"verdict":"%s","findings":%s}' "$3" "$4" > "$F/$1/$2.json"; }
f good 1-S1 CHANGES '[{"file":"src/a.ts","line":55,"severity":"P1","title":"t"}]'
f good 2-S2 CHANGES '[{"file":"src/b.ts","line":9,"severity":"P2","title":"t"}]'
f good 3-clean APPROVE '[]'
f noisy 1-S1 CHANGES '[{"file":"src/a.ts","line":500,"severity":"P1","title":"far"}]'
f noisy 2-S2 CHANGES '[{"file":"src/b.ts","line":10,"severity":"P2","title":"t"}]'
f noisy 3-clean CHANGES '[{"file":"src/z.ts","line":1,"severity":"P0","title":"invented"}]'
python3 $K/reviewer-eval/score.py --corpus "$C" --findings "$F" > "$T/score.json"; RC=$?
check "score: exits 0 on a well-formed corpus" test $RC -eq 0
python3 - "$T/score.json" <<'PY' && ok "score: recall, near-miss, clean-FP candidates and pairing computed" || bad "score values"
import json, sys
d = json.load(open(sys.argv[1]))["arms"]
g, n = d["good"], d["noisy"]
assert (g["seeded_hits"], g["seeded_n"], g["seeded_hits_at_P0P1"]) == (2, 2, 1)
assert (g["ci_silent_n"], g["ci_silent_hits"], n_silent := d["noisy"]["ci_silent_hits"]) == (1, 1, 0)
assert g["clean_changes_verdicts"] == 0 and g["clean_p0p1_findings_unadjudicated"] == 0
assert (n["seeded_hits"], n["near_misses_to_adjudicate"]) == (1, 1)
assert n["clean_changes_verdicts"] == 1 and n["clean_p0p1_findings_unadjudicated"] == 1
p = json.load(open(sys.argv[1]))["paired"]["good vs noisy"]
assert (p["only_first"], p["only_second"]) == (1, 0) and p["mcnemar_exact_p"] == 1.0
PY
C3=$T/c3; F3=$T/f3; mkdir -p "$C3/1-a" "$C3/2-b" "$C3/3-c" "$C3/4-d" "$F3/x"
m3() { printf '{"variant":"%s","seeded":true,"ci_silent":%s,%s"truth":{"file":"src/a.ts","line":10,"severity":"P1","class":"c","desc":"d"}}' "$1" "$2" "$3" > "$C3/$1/meta.json"; }
m3 1-a true '"weakening_visible":false,'
m3 2-b true '"weakening_visible":true,'
m3 3-c false '"weakening_visible":false,'
m3 4-d true ''
f3() { printf '{"verdict":"CHANGES","findings":[{"file":"src/a.ts","line":%s,"severity":"P1","title":"t"}]}' "$2" > "$F3/x/$1.json"; }
f3 1-a 10; f3 2-b 900; f3 3-c 10; f3 4-d 900
python3 $K/reviewer-eval/score.py --corpus "$C3" --findings "$F3" > "$T/score3.json"
python3 - "$T/score3.json" <<'PY' && ok "score: stealth-silent excludes visible weakening; a v1 meta (no flag) counts as stealth" || bad "stealth partition"
import json, sys
x = json.load(open(sys.argv[1]))["arms"]["x"]
assert (x["ci_silent_n"], x["ci_silent_hits"]) == (3, 1), x
assert (x["stealth_silent_n"], x["stealth_silent_hits"]) == (2, 1), x
assert (x["seeded_n"], x["seeded_hits"]) == (4, 2), x
PY
C4=$T/c4; F4=$T/f4; mkdir -p "$C4/1-a" "$C4/2-b" "$C4/3-c" "$C4/4-d" "$C4/5-e" "$C4/6-clean" "$C4/7-f" "$F4/x"
m4() { printf '{"variant":"%s","seeded":true,"ci_silent":%s,"weakening_visible":%s,"truth":{"id":"%s","file":"src/a.ts","line":10,"severity":"P1","class":"c","desc":"d"}}' "$1" "$2" "$3" "$1" > "$C4/$1/meta.json"; }
m4 1-a false false; m4 2-b true true; m4 3-c true false; m4 4-d true false; m4 5-e true false; m4 7-f true false
printf '{"variant":"6-clean","seeded":false,"truth":null}' > "$C4/6-clean/meta.json"
f4() { printf '{"verdict":"%s","findings":%s}' "$2" "$3" > "$F4/x/$1.json"; }
f4 1-a CHANGES '[{"file":"src/a.ts","line":10,"severity":"P0","title":"t"}]'
f4 2-b CHANGES '[{"file":"src/a.ts","line":12,"severity":"P1","title":"t"}]'
f4 3-c APPROVE '[{"file":"src/a.ts","line":11,"severity":"P2","title":"t"}]'
f4 4-d CHANGES '[{"file":"src/a.ts","line":10,"severity":"P1","title":"t"}]'
f4 5-e APPROVE '[]'
f4 7-f CHANGES '[{"file":"src/a.ts","line":500,"severity":"P1","title":"far from the seed"}]'
f4 6-clean CHANGES '[{"file":"src/z.ts","line":1,"severity":"P1","title":"t"}]'
python3 $K/reviewer-eval/v2/tier_report.py --corpus "$C4" --findings "$F4" --arm x > "$T/tier.json"; RC4=$?
check "tier_report: exits 0 on a well-formed corpus" test $RC4 -eq 0
python3 - "$T/tier.json" <<'PY' && ok "tier_report: tiers split by CI-visibility and visible weakening; P2-only find is a hit, not a gate hit" || bad "tier_report values"
import json, sys
d = json.load(open(sys.argv[1]))
t = d["tiers"]
assert (t["ci-visible"]["n"], t["ci-visible"]["hits"], t["ci-visible"]["gate_hits"]) == (1, 1, 1), t["ci-visible"]
assert (t["visible-weakening"]["n"], t["visible-weakening"]["hits"], t["visible-weakening"]["gate_hits"]) == (1, 1, 1), t["visible-weakening"]
assert (t["stealth-silent"]["n"], t["stealth-silent"]["hits"], t["stealth-silent"]["gate_hits"]) == (4, 2, 1), t["stealth-silent"]
assert (t["all seeded"]["n"], t["all seeded"]["hits"], t["all seeded"]["gate_hits"]) == (6, 4, 3), t["all seeded"]
assert d["clean"] == {"n": 1, "changes": 1, "p0p1_findings": 1}, d["clean"]
PY
python3 $K/reviewer-eval/v2/tier_report.py --corpus "$T/nope" --findings "$F4" --arm x >/dev/null 2>&1
check "tier_report: unreadable input exits 2" test $? -eq 2
mkdir -p "$T/subs"
python3 - "$T/subs" <<'PY'
import json, sys
d = sys.argv[1]
A, R = "/tmp/arena-t", "/tmp/raw-t"
def agent(name, rid, calls):
    with open(f"{d}/agent-{name}.jsonl", "w") as fh:
        fh.write(json.dumps({"message": {"role": "user", "content": f"Review {A}/{rid} using {A}/PROMPT.md"}}) + "\n")
        fh.write(json.dumps({"message": {"role": "assistant", "content": [{"type": "tool_use", "name": n, "input": i} for n, i in calls]}}) + "\n")
agent("good", "r01", [
    ("Read", {"file_path": A + "/PROMPT.md"}),
    ("Read", {"file_path": A + "/r01/tree/src/x.ts"}),
    ("Grep", {"pattern": "a", "path": A + "/r01/tree"}),
    ("Glob", {"pattern": "**/*.ts", "path": A + "/r01"}),
    ("Bash", {"command": "cd " + A + "/r01/tree && grep -rn '\"/about\"' src | head"}),
    ("Write", {"file_path": R + "/r01.json", "content": "{}"})])
agent("bad", "r02", [
    ("Read", {"file_path": A + "/r03/pr.diff"}),
    ("Grep", {"pattern": "a"}),
    ("Bash", {"command": "ls /tmp"}),
    ("Bash", {"command": "cat package.json"}),
    ("Write", {"file_path": "/tmp/elsewhere.json", "content": "{}"})])
PY
python3 $K/reviewer-eval/v2/containment_audit.py --subagents "$T/subs" --arena /tmp/arena-t --raw /tmp/raw-t > "$T/audit.json"
python3 - "$T/audit.json" <<'PY' && ok "containment_audit: a clean agent passes (route strings in grep are not paths); a sibling read, a pathless grep, ls /tmp, a pathless bash and a stray write are each flagged" || bad "containment audit values"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["agents"] == 2 and d["tool_calls"] == 11, d
v = [(x["id"], x["call"].split(" ")[0]) for x in d["violations"]]
assert sorted(v) == sorted([("r02", "Read"), ("r02", "Grep"), ("r02", "Bash"), ("r02", "Bash"), ("r02", "Write")]), v
PY
python3 $K/reviewer-eval/v2/containment_audit.py --subagents "$T/no-such-dir" --arena /tmp/arena-t --raw /tmp/raw-t >/dev/null 2>&1
check "containment_audit: an unreadable directory exits 2" test $? -eq 2
python3 $K/reviewer-eval/score.py --corpus "$T/does-not-exist" --findings "$F" >/dev/null 2>&1
check "score: unreadable input exits 2" test $? -eq 2

# --- 5. observational analyzer -------------------------------------------------------------
python3 $K/reviewer-eval/observational/analyze.py $K/reviewer-eval/observational/fixtures/dataset.json > "$T/obs.json"
python3 - "$T/obs.json" <<'PY' && ok "observational: per-model rows and same-head conflict count" || bad "observational values"
import json, sys
d = json.load(open(sys.argv[1]))
m = {r["model"]: r for r in d["models"]}
assert (m["model-a"]["verdicts"], m["model-a"]["changes"], m["model-a"]["of_which_later_approved_at_new_sha"]) == (2, 1, 1)
assert d["agreement"] == {"card_sha_pairs": 3, "conflicting_verdicts": 1}
PY

# --- 6. closed-book public-corpus harness: fail-closed audit and bundle gates ----------------
check "blinded review harness: offline trace, path, memory and bundle negative controls" \
  env PYTHONDONTWRITEBYTECODE=1 python3 ops/case_a84d4bc9/test_harness.py
check "contained leaf executor: binding, admission, tool, transport and seal controls" \
  env PYTHONDONTWRITEBYTECODE=1 python3 ops/case_a84d4bc9/test_leaf.py

[ "$FAIL" -eq 0 ] && echo "PASS" || { echo "FAILED"; exit 1; }
