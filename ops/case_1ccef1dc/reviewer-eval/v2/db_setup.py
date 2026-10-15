#!/usr/bin/env python3
"""Create and migrate one test database per base PR on the sanctioned test host.

    db_setup.py --corpus corpus-v2 --template 'postgres://agent_test@agent-testdb:5432/two_web_next_tog<card>_pr{pr}'

For every base PR in corpus-report.json: create the database if it does not exist (through the
`agent_test` role the host sanctions for exactly this, never another credential), then run the PR's
own `npm run db:migrate` from its clean tree against it. Per-PR databases keep each PR on the
migration set of its era; main's later migrations add append-only audit triggers that make older
tests red on a clean tree. Only the sanctioned test database is accepted (see survival_all.py).
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from survival_all import sanctioned, url_for  # noqa: E402

CREATE = r"""
import postgres from "postgres";
const name = process.argv[1];
const sql = postgres({ host: "agent-testdb", port: 5432, user: "agent_test", password: () => "", database: "two_web_next", max: 1, connect_timeout: 10 });
try {
  const ex = await sql`select 1 from pg_database where datname = ${name}`;
  if (!ex.length) await sql.unsafe(`create database ${name}`);
  console.log(ex.length ? "exists" : "created");
} finally { await sql.end(); }
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--template", required=True)
    a = ap.parse_args()
    sanctioned(a.template.replace("{pr}", "0"))
    corpus = os.path.abspath(a.corpus)
    prs = sorted({e["pr"] for e in json.load(open(os.path.join(corpus, "corpus-report.json")))})
    env0 = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env0["NODE_ENV"] = "development"
    for pr in prs:
        url = url_for(a.template, pr)
        name = url.rsplit("/", 1)[1]
        tree = os.path.join(corpus, f"{pr}-clean", "tree")
        r = subprocess.run(["node", "--input-type=module", "-e", CREATE, name], cwd=tree,
                           capture_output=True, text=True, env=env0, timeout=60)
        if r.returncode != 0:
            print(f"pr {pr}: create failed: {r.stderr[-300:]}", file=sys.stderr)
            return 1
        m = subprocess.run(["npm", "run", "db:migrate"], cwd=tree, capture_output=True, text=True,
                           env={**env0, "DATABASE_URL": url}, timeout=300)
        ok = m.returncode == 0 and "migrations applied successfully" in (m.stdout + m.stderr)
        print(f"pr {pr}: {name} {r.stdout.strip()} migrate={'ok' if ok else 'FAILED'}")
        if not ok:
            print((m.stdout + m.stderr)[-400:], file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
