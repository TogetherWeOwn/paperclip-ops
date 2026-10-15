#!/usr/bin/env bash
set -Eeuo pipefail

workflow=${1:-.github/workflows/ci.yml}
[[ -f "$workflow" ]] || { printf 'FAIL: missing %s\n' "$workflow" >&2; exit 1; }

python3 - "$workflow" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text()
jobs_match = re.search(r"(?ms)^jobs:\n(?P<jobs>.*)\Z", text)
if not jobs_match:
    raise SystemExit("FAIL: jobs block is missing")
jobs_text = jobs_match.group("jobs")
jobs = re.findall(r"(?m)^  ([a-zA-Z0-9_-]+):\n", jobs_text)
if not jobs:
    raise SystemExit("FAIL: no jobs found")
bodies = {}
for name in jobs:
    m = re.search(r"(?ms)^  " + re.escape(name) + r":\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", jobs_text)
    bodies[name] = m.group("body") if m else ""
if "suites" not in bodies:
    raise SystemExit("FAIL: suites job is missing")
body = bodies["suites"]

# case_221a4156 has two halves, and only the first was ever pinned here:
#   1. jobs run on the private self-hosted pool, never a paid fallback;
#   2. "jobs must not receive the Docker socket".
# Half 2 was violated in practice for weeks (every runner's .env exported
# DOCKER_HOST into every job) precisely because nothing checked it. case_10099fe0
# split the fleet by label -- `two-isolated` runners have no DOCKER_HOST, plain
# `two-selfhosted` ones still do, for other repos whose CI needs a daemon -- and
# moved the last job that wanted a container (a `postgres` service) onto an
# ephemeral PostgreSQL cluster it starts itself.
#
# So the invariant this workflow can now hold is the strong one: NO job here
# needs a container daemon, therefore no job may sit on the Docker-enabled pool
# and no job may declare a service container (which would silently require one
# again). Approved included-minutes overflow uses the exact opt-in expression
# below, never a bare hosted label or paid fallback.
ISOLATED = "    runs-on: [self-hosted, two-isolated]\n"

# Owner 2026-10-04: jobs may overflow to GitHub-hosted runners on the org's
# INCLUDED Actions minutes (never paid overage; ops/ci-overflow guard flips
# CI_OVERFLOW_RUNNER back to self-hosted at 45k minutes or any net spend).
# A GitHub-hosted VM is single-use and never sees our host Docker socket, so
# the case_221a4156 isolation property holds. The only accepted form is this exact
# expression: per-job opt-in via vars, default the isolated pool.
def overflow_line(job):
    return ("    runs-on: ${{ fromJSON((contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), '" + job +
            "') && contains(fromJSON(vars.CI_OVERFLOW_EVENTS || '[]'), github.event_name) && "
            "vars.CI_OVERFLOW_RUNNER) || '[\"self-hosted\",\"two-isolated\"]') }}\n")
# case_5236db70: the three lightweight gating jobs are hard-pinned to the larger
# GARM ephemeral pool (not isolated) so they never queue behind heavy suites
# on the saturated isolated pool — even docs-only PRs need change detection
# and ci-ok, which is why these jobs (and only these) may use the ephemeral
# label. Hard-pin, not an overflow expression:
# CI_OVERFLOW_JOBS currently lists these jobs with CI_OVERFLOW_RUNNER
# two-isolated, and any vars-driven expression lets the operator routing
# defeat the fix. Heavy jobs keep the isolated fallback; only these three
# may use the ephemeral label.
LIGHTWEIGHT_EPHEMERAL_JOBS = ("changes", "secret-scan", "ci-ok")
EPHEMERAL = "    runs-on: [self-hosted, two-ephemeral]\n"
# Pipeline review 2026-10-10 (#7): the model-selection-moved guard became a
# step of the always-run ephemeral secret-scan job, so no job requires the
# opt-in overflow route any more. Keep the hook for future opt-in jobs.
OVERFLOW_OPT_IN_JOBS = ()
OVERFLOW_RE = re.compile(
    r"^runs-on: \$\{\{ fromJSON\(\(contains\(fromJSON\(vars\.CI_OVERFLOW_JOBS \|\| '\[\]'\), '[a-z0-9-]+'\) && "
    r"contains\(fromJSON\(vars\.CI_OVERFLOW_EVENTS \|\| '\[\]'\), github\.event_name\) && "
    r"vars\.CI_OVERFLOW_RUNNER\) \|\| '\[\"self-hosted\",\"two-isolated\"\]'\) \}\}$")

# case_0bf02bac (owner 2026-10-04): a job could opt in to the owner's home-PC
# runners (label two-node93f4bccf) while they were online. The old long mutation job
# and model-selection suite moved to toolkit CI; the targeted private mutation
# gate remains here on the isolated pool. No job may route to home-PC runners.
# Keep the expression shape below so the check rejects any job that carries a
# home-PC key (the runners are persistent, not fresh VMs per job, and never
# fork-safe without the same-repo guard).
nodecf677d86_JOBS = ()
# Same-repo guard, not the `fork` flag: the flag asks "is the head a fork of
# anything", not "is this a foreign PR". On push/merge_group (no pull_request
# object) the event-name check short-circuits first, so no null dereference.
FORK_GUARD = ("(github.event_name != 'pull_request' || "
              "github.event.pull_request.head.repo.full_name == github.repository)")
def node93f4bccf_line(job):
    return ("    runs-on: ${{ fromJSON((contains(fromJSON(vars.CI_nodecf677d86_JOBS || '[]'), '" + job +
            "') && " + FORK_GUARD + " && "
            "vars.CI_nodecf677d86_RUNNER) || (contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), '" + job +
            "') && contains(fromJSON(vars.CI_OVERFLOW_EVENTS || '[]'), github.event_name) && "
            "vars.CI_OVERFLOW_RUNNER) || '[\"self-hosted\",\"two-isolated\"]') }}\n")
nodecf677d86_RE = re.compile(
    r"^runs-on: \$\{\{ fromJSON\(\(contains\(fromJSON\(vars\.CI_nodecf677d86_JOBS \|\| '\[\]'\), '[a-z0-9-]+'\) && "
    r"\(github\.event_name != 'pull_request' \|\| "
    r"github\.event\.pull_request\.head\.repo\.full_name == github\.repository\) && "
    r"vars\.CI_nodecf677d86_RUNNER\) \|\| \(contains\(fromJSON\(vars\.CI_OVERFLOW_JOBS \|\| '\[\]'\), '[a-z0-9-]+'\) && "
    r"contains\(fromJSON\(vars\.CI_OVERFLOW_EVENTS \|\| '\[\]'\), github\.event_name\) && "
    r"vars\.CI_OVERFLOW_RUNNER\) \|\| '\[\"self-hosted\",\"two-isolated\"\]'\) \}\}$")

# C6 cutover removed the only shared routing key (case_c45ee603: the
# model-selection impact check used to share the mutants' key). Every
# remaining job carries exactly its own key; a foreign key fails the
# exact-expression checks below.
def is_routed(name, body):
    if name in LIGHTWEIGHT_EPHEMERAL_JOBS:
        return EPHEMERAL in body
    if ISOLATED in body or overflow_line(name) in body:
        return True
    if name in nodecf677d86_JOBS and node93f4bccf_line(name) in body:
        return True
    return False

unisolated = sorted(n for n, b in bodies.items() if not is_routed(n, b))
# Match the expression tokens, not the bare name: comments may name the
# variable (a step comment explaining the guard does), while only a runs-on
# expression routes a job.
node93f4bccf_users = sorted(n for n, b in bodies.items() if "vars.CI_nodecf677d86" in b)
# A `services:` block is the quiet way back to needing a daemon.
with_services = sorted(n for n, b in bodies.items() if re.search(r"(?m)^    services:\s*$", b))

def uses_ephemeral_label(b):
    return any("two-ephemeral" in l and l.strip().startswith("runs-on:") for l in b.splitlines())
non_lightweight_ephemeral = sorted(n for n, b in bodies.items() if n not in LIGHTWEIGHT_EPHEMERAL_JOBS and uses_ephemeral_label(b))

checks = {
    "required check name is unchanged": "    name: suites\n" in body,
    "suites runs on the isolated pool": ISOLATED in body or overflow_line("suites") in body,
    "every job runs on a private pool (isolated, hard-pinned ephemeral, overflow, node93f4bccf)": not unisolated,
    "only the three lightweight gating jobs use the ephemeral label": not non_lightweight_ephemeral,
    # sudoers/privilege contracts must run where sudo is absent: never overflow.
    "privilege-suites never leaves the isolated pool": ISOLATED in bodies.get("privilege-suites", ""),
    "private-mutation-gates stays on the isolated pool":
        ISOLATED in bodies.get("private-mutation-gates", ""),
    # case_0bf02bac: the home-PC runners are persistent, not fresh VMs per job.
    # No job may route there, and the label travels only inside the exact
    # expression above.
    "no job may route to the home PC": (
        all(n in nodecf677d86_JOBS for n in node93f4bccf_users)
        and all(node93f4bccf_line(n) in bodies.get(n, "") for n in nodecf677d86_JOBS)),
    "privilege-suites never routes to the home PC": "vars.CI_nodecf677d86" not in bodies.get("privilege-suites", ""),
    "home-PC label travels only inside the exact expression":
        re.search(r"(?m)^    runs-on: \[self-hosted, two-node93f4bccf\]\s*$", text) is None,
    "no job declares a service container": not with_services,
    "Docker-enabled pool is unused by this workflow":
        re.search(r"(?m)^    runs-on: \[self-hosted, two-selfhosted\]\s*$", text) is None,
    "paid fallback is absent": re.search(r"(?m)^    runs-on: ubuntu-latest\s*$", text) is None,
    "no runs-on value outside the private pools": all(
        l.strip() in ("runs-on: [self-hosted, two-isolated]", "runs-on: [self-hosted, two-ephemeral]") or OVERFLOW_RE.match(l.strip()) or nodecf677d86_RE.match(l.strip())
        for l in text.splitlines() if l.strip().startswith("runs-on:")),
    # case_bec120d3: the two-isolated label spans the Coolify box and the node7b1ff338
    # isolated VM. case_e41d7023: the static fleets are no longer registered, so
    # the proof must also accept the GARM prefix. Narrowing to any subset
    # strands live runners, so all three are pinned here as guard lines, not
    # bare substrings: a comment that merely NAMES a fleet must not satisfy
    # this check (a deletion mutant keeps the comment while removing the
    # guard).
    "runner identity is a positive control": (
        all(value in body for value in ("RUNNER_NAME", "RUNNER_OS", "RUNNER_ARCH"))
        and all(re.search(p, body) is not None for p in (
            r'"\$\{RUNNER_NAME:-\}" == coolify-vps-\*',
            r'"\$\{RUNNER_NAME:-\}" == ci-node7b1ff338-iso-\*',
            r'"\$\{RUNNER_NAME:-\}" == garm-\*'))),
    # case_d0cad227: cancelling the in-progress run on main left only 7 of the last
    # 29 completed main commits fully verified. Only a pull request run may be
    # cancelled by a newer one.
    "a push to main never cancels the previous main run": re.search(
        r"(?m)^  cancel-in-progress: \$\{\{ github\.event_name == 'pull_request' \}\}\s*$", text) is not None
        and re.search(r"(?m)^  cancel-in-progress: true\s*$", text) is None,
    # case_ee446431: the checkout ref is SHA-pinned (upstream U8), so the anchor
    # requires the 40-hex form, not the floating @v4 tag. The trailing
    # `[^\n]*` covers the `# v4` version comment. Dependabot bumps keep the
    # shape; any future unpinning goes red here.
    "checkout cleans the workspace": re.search(
        r"(?ms)- uses: actions/checkout@[0-9a-f]{40}[^\n]*\n\s+with:\n\s+clean: true", body) is not None,
}
checks.update({
    # case_5236db70: lightweight gating jobs are hard-pinned to the ephemeral
    # pool. Any vars-driven expression lets operator routing defeat the fix.
    f"{job} hard-pinned to the ephemeral pool":
        EPHEMERAL in bodies.get(job, "")
    for job in LIGHTWEIGHT_EPHEMERAL_JOBS
})
checks.update({
    # Jobs listed in OVERFLOW_OPT_IN_JOBS keep the opt-in overflow route
    # with isolated fallback.
    f"{job} supports job/event opt-in overflow with isolated fallback":
        overflow_line(job) in bodies.get(job, "")
    for job in OVERFLOW_OPT_IN_JOBS
})
failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("PASS" if ok else "FAIL") + ": " + name)
if unisolated:
    print("  jobs not on a private pool: " + ", ".join(unisolated))
if non_lightweight_ephemeral:
    print("  non-lightweight jobs on ephemeral: " + ", ".join(non_lightweight_ephemeral))
if with_services:
    print("  jobs declaring a service container: " + ", ".join(with_services))
if failed:
    raise SystemExit(1)
PY
