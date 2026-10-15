#!/usr/bin/env python3
"""Run vitest related <seedfile> on every seeded v2 variant tree, no DB.

Usage: survival_all.py --dir corpus-v2 [--only 114-S15,114-T06] [--out survival-report.json]
                       [--database-url postgres://agent_test@agent-testdb:5432/two_web_next_tog<card>_pr{pr}]
Writes the report: {variant, seed, file, exit, ci_silent, tail[, baseline_exit]}.
A variant is CI-silent iff exit == 0 (with the weakened tests applied).
With --only, merges into the existing report instead of overwriting.

--database-url runs the same command with DATABASE_URL set, so database-gated suites run. Only
the sanctioned test database is accepted (host agent-testdb, user agent_test, no password, a
two_web_next* name); anything else is refused before a process starts. Each variant is then also
run on the CLEAN tree of its base PR, and `baseline_exit` is recorded: a database run that is red
on the clean tree says nothing about the seed (merge_survival treats it as unknown).
"""
import argparse, json, os, re, subprocess, sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402


def sanctioned(url):
    u = urlparse(url)
    ok = (u.scheme in ("postgres", "postgresql") and u.hostname == "agent-testdb" and u.username == "agent_test"
          and not u.password and (u.port in (None, 5432)) and re.fullmatch(r"/two_web_next[a-z0-9_]*", u.path or "")
          and not u.query)
    if not ok:
        raise SystemExit("refusing: --database-url must be the sanctioned test database (agent-testdb, agent_test, two_web_next*)")


def url_for(template, pr):
    """`{pr}` in --database-url becomes the base PR number, so each PR runs on a database migrated
    with ITS OWN migration set (main's later migrations, e.g. append-only audit triggers, break
    older tests and would make every clean baseline red). The result must still be sanctioned."""
    url = template.replace("{pr}", str(int(pr)))
    sanctioned(url)
    return url


def vitest_related(tree, f, env):
    p = subprocess.run(["npx", "vitest", "related", f, "--run", "--reporter=dot"],
                       cwd=tree, capture_output=True, text=True, timeout=900, env=env)
    return p.returncode, (p.stdout + p.stderr)[-600:]

def write_report(out_path, out):
    """Persist the survival report atomically; fail loudly, never truncate.

    The report goes through a same-directory temp file plus fsync plus atomic
    rename, so an ENOSPC mid-write leaves the previous output in place. A
    failing write names the path and errno on stderr and exits non-zero.
    """
    try:
        write_json_atomic(out_path, out)
    except OSError as exc:
        print("survival_all: write failed: %s: errno=%s"
              % (out_path, getattr(exc, "errno", "?")), file=sys.stderr)
        raise SystemExit(2)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default="survival-report.json")
    ap.add_argument("--database-url", default=None)
    a = ap.parse_args()
    d = os.path.abspath(a.dir)
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["NODE_ENV"] = "test"
    if a.database_url:
        sanctioned(a.database_url.replace("{pr}", "0"))
    baseline = {}
    rep = json.load(open(os.path.join(d, "corpus-report.json")))
    only = set(a.only.split(",")) if a.only else None
    prev = {}
    if only:
        try:
            for r in json.load(open(os.path.join(d, a.out))):
                prev[r["variant"]] = r
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    out = []
    for e in rep:
        if not e["seed"]:
            continue
        v = e["variant"]
        if only and v not in only:
            continue
        m = json.load(open(os.path.join(d, v, "meta.json")))
        f = m["truth"]["file"]
        tree = os.path.join(d, v, "tree")
        if a.database_url:
            env["DATABASE_URL"] = url_for(a.database_url, m["pr"])
        code, tail = vitest_related(tree, f, env)
        row = {"variant": v, "seed": e["seed"], "file": f, "exit": code,
               "ci_silent": code == 0, "tail": tail}
        if a.database_url:
            key = (m["pr"], f)
            if key not in baseline:
                baseline[key] = vitest_related(os.path.join(d, f"{m['pr']}-clean", "tree"), f, env)[0]
            row["baseline_exit"] = baseline[key]
        out.append(row)
        print(json.dumps({k: row[k] for k in ("variant", "seed", "exit", "ci_silent")}), flush=True)
    if only:
        prev.update({r["variant"]: r for r in out})
        seeded = [e["variant"] for e in rep if e["seed"]]
        out = [prev[v] for v in seeded if v in prev]
    write_report(os.path.join(d, a.out), out)
    sil = sum(1 for r in out if r["ci_silent"])
    print(f"SILENT: {sil}/{len(out)}")
if __name__ == "__main__":
    main()
