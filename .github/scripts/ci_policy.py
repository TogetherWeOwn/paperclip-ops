#!/usr/bin/env python3
"""CI change-gating policy lint (case_21bcd078, Phase 2 of the case_6b39c80f standard).

Warns, and once every repo child complies fails, when a workflow has heavy
jobs without change gating or uses workflow-level `on: pull_request: paths:`
on a workflow producing a required check.

Scope: PR-triggered workflows that produce required checks. Out of scope and
skipped: workflows with no `pull_request`/`pull_request_target` trigger
(schedule, push-to-main and dispatch-only full-run paths need no gating), and
workflows carrying a `ci-policy: non-required` marker comment (live audits,
canaries and other deliberately non-required workflows that run on their own
file only).

Heuristics (static; see the card for the historical-duration half):
  heavy job = `timeout-minutes` >= 10, or missing (GitHub defaults to 6h).
  gated job = a job-level `if:` naming change detection (`needs.*outputs`,
  the `github.event` draft/event-name latches) or a lifecycle guard
  (`failure()`, `always()`, `cancelled()`); or the job publishes `outputs:`
  (it IS the detector). Light jobs (< 10 min) need no gating.

ci-ok completeness (case_b1c5ef61): in a workflow that defines a job named `ci-ok`,
every other job must be directly in `ci-ok`'s `needs:`, or carry
`# ci-ok: exempt <reason>` on the job: on the header line, in the job body, or
in the comment lines directly above the header. A job outside the aggregator
can go red and still merge. Coverage is direct only: a job reached through
another job's `needs:` is NOT covered, because when its dependency fails GitHub
skips the dependent job, `ci-ok` never sees the failure in its `needs` context,
and a `ci-ok` that accepts `skipped` passes. A marker without a reason is a
finding too. Workflows with no `ci-ok` job are skipped. Known limit: coverage
is structural, so a directly needed job that runs under `always()` is counted
as covered even though its failure would not propagate.

Environment contract:
  CI_POLICY_MODE        "warn" | "error" (default warn)
  HEAVY_TIMEOUT_MINUTES heavy threshold in minutes (default 10)

Usage: ci_policy.py [--json] [workflow-file ...] (default: .github/workflows/*.yml).
Without `--json` the output is annotation/`note:` prose. With `--json` stdout is
exactly one `ci-policy-verdicts.v1` object instead (per-finding `rule`, `path`,
`job`, `line`, stable `code`, `retryable`, `next_action`; `unmeasurable` inputs;
`coverage.limits`; contract in docs/ci-policy-verdicts.md); exit is 2 when any
input could not be measured. Runs inside the existing required `pr-lint` job, so adopting it in warn mode
adds no new required check and needs no ruleset change. Stdlib only.

Fail-flip record: `CI_POLICY_MODE` is `error` in the owning workflow since
case_5406e427 (every heavy job change-gated, canaries marked non-required). The
local copy stays until the org home carries the lint (case_b57a367e owns that
move); keep the two in sync until then.
"""
import glob
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HEAVY_DEFAULT = 10
EXEMPT = re.compile(r"ci-policy:\s*non-required\b", re.I)
GATED = re.compile(
    r"needs\.[a-zA-Z0-9_-]+\.outputs|github\.event|failure\(\)|always\(\)|cancelled\(\)"
)
JOB_HEADER = re.compile(r"(?m)^  ([A-Za-z0-9_-]+):\n")
TIMEOUT = re.compile(r"(?m)^    timeout-minutes:\s*(\d+)\s*$")
JOB_IF = re.compile(r"(?m)^    if:\s*(.+?)\s*$")
OUTPUTS = re.compile(r"(?m)^    outputs:\s*$")
TOP_KEY = re.compile(r"(?m)^[A-Za-z][^:\n]*:")
JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):[ \t]*(?:#.*)?$")
CI_OK = "ci-ok"
CI_OK_EXEMPT = re.compile(r"(?:^|\s)#\s*ci-ok:\s*exempt\b(?P<reason>.*)$", re.I)
NEEDS_KEY = re.compile(r"^    needs:[ \t]*(?P<val>.*?)[ \t]*$")


@dataclass
class Finding:
    level: str  # "error" | "warning"
    title: str
    message: str
    file: str = ""  # set with `line` to anchor the annotation on the job
    line: int = 0
    # Machine-readable identity for `--json`; never rendered in the prose output.
    rule: str = ""
    code: str = ""
    path: str = ""
    job: str = ""
    job_line: int = 0


def on_section(text):
    """Return the `on:` trigger block, or '' when absent."""
    # [ \t], never \s: a greedy \s* eats the newline and the indentation,
    # so a block-form `on:` would misread as the flow value `pull_request:`.
    m = re.search(r"(?m)^on:[ \t]*(?P<val>\[.*?\]|\S.*?)?[ \t]*$", text)
    if not m:
        return ""
    if (m.group("val") or "").strip():
        return m.group("val")  # flow form: `on: [push, pull_request]`
    start = m.end()
    tail = text[start:]
    end = TOP_KEY.search(tail)
    return tail[: end.start()] if end else tail


def has_pr_trigger(on_text):
    return "pull_request" in on_text


def has_workflow_paths(on_text):
    """Workflow-level `paths:` under `pull_request`/`pull_request_target`."""
    lines = on_text.splitlines()
    in_pr, pr_indent = False, 0
    for line in lines:
        m = re.match(r"^(\s*)([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        indent, key = len(m.group(1)), m.group(2)
        # Relative indents: the block is already cut at top-level keys, so
        # the trigger names sit at its shallowest level, whatever that is.
        if key in ("pull_request", "pull_request_target") and (not in_pr or indent <= pr_indent):
            in_pr, pr_indent = True, indent
        elif in_pr and indent <= pr_indent:
            in_pr = False
        if in_pr and key == "paths" and indent > pr_indent:
            return True
    return False


def split_jobs(text):
    """Map job name -> body for the `jobs:` block."""
    m = re.search(r"(?ms)^jobs:\n(?P<jobs>.*)\Z", text)
    if not m:
        return {}
    jobs_text = m.group("jobs")
    bodies = {}
    for name in JOB_HEADER.findall(jobs_text):
        jm = re.search(
            r"(?ms)^  " + re.escape(name) + r":\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            jobs_text,
        )
        bodies[name] = jm.group("body") if jm else ""
    return bodies


@dataclass
class Job:
    name: str
    line: int  # 1-based line of the `name:` header
    header: str
    body: list  # lines under the header, minus comments that lead the next job
    leading: list  # comment lines directly above the header


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def job_blocks(text):
    """Line-anchored jobs of the `jobs:` block, in file order.

    Unlike split_jobs this keeps line numbers, tolerates a trailing comment on
    the header, and hands a comment that sits directly above a job (indent <= 2,
    no blank line between) to that job rather than to the one before it.
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^jobs:[ \t]*(#.*)?$", ln)), None)
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^[A-Za-z]", lines[i]):
            end = i
            break
    heads = [i for i in range(start + 1, end) if JOB_KEY.match(lines[i])]
    jobs = []
    for n, h in enumerate(heads):
        stop = heads[n + 1] if n + 1 < len(heads) else end
        jobs.append([JOB_KEY.match(lines[h]).group(1), h, stop])
    out = []
    for n, (name, h, stop) in enumerate(jobs):
        body_end = stop
        # Comments directly above the NEXT header are its marker, not our body.
        while body_end > h + 1 and lines[body_end - 1].lstrip().startswith("#") \
                and _indent(lines[body_end - 1]) <= 2 and n + 1 < len(jobs):
            body_end -= 1
        leading, j = [], h
        while j - 1 > start and lines[j - 1].lstrip().startswith("#") and _indent(lines[j - 1]) <= 2:
            j -= 1
            leading.append(lines[j])
        out.append(Job(name, h + 1, lines[h], lines[h + 1:body_end], leading))
    return out


def job_needs(job):
    """Names in the job's `needs:` (scalar, flow list or block list)."""
    names = []
    for i, ln in enumerate(job.body):
        m = NEEDS_KEY.match(ln)
        if not m:
            continue
        val = re.sub(r"\s#.*$", "", m.group("val")).strip()
        if val.startswith("["):
            while "]" not in val and i + 1 < len(job.body):  # flow list over several lines
                i += 1
                val += " " + re.sub(r"\s#.*$", "", job.body[i].strip())
            names = re.findall(r"[A-Za-z0-9_-]+", val.split("]")[0])
        elif val:
            names = re.findall(r"^[A-Za-z0-9_-]+$", val)
        else:
            for nxt in job.body[i + 1:]:
                bm = re.match(r"^\s+-\s*([A-Za-z0-9_-]+)\s*(?:#.*)?$", nxt)
                if bm:
                    names.append(bm.group(1))
                elif nxt.strip() and not nxt.lstrip().startswith("#"):
                    break
        break
    return names


def ci_ok_exempt(job):
    """(has_marker, reason) from the first `# ci-ok: exempt <reason>` on the job."""
    for ln in [job.header, *job.body, *job.leading]:
        m = CI_OK_EXEMPT.search(ln)
        if m:
            return True, m.group("reason").strip(" \t-\u2013\u2014:")
    return False, ""


def check_ci_ok_needs(path, text, mode):
    """Every job must be reachable from `ci-ok` `needs:` or exempt (case_b1c5ef61)."""
    jobs = job_blocks(text)
    byname = {j.name: j for j in jobs}
    if CI_OK not in byname:
        return [], [f"{path}: no `{CI_OK}` job; needs-completeness rule skipped"]
    # Direct needs only: a job reached through another needed job is skipped, not
    # failed, when its dependency goes red, so ci-ok would not see the failure.
    covered = {name for name in job_needs(byname[CI_OK]) if name in byname}
    out = []
    for job in jobs:
        if job.name == CI_OK or job.name in covered:
            continue
        exempt, reason = ci_ok_exempt(job)
        if exempt and reason:
            continue
        if exempt:
            msg = (f"{path}:{job.line}: job `{job.name}` has `# ci-ok: exempt` with no "
                   "reason. Say why it sits outside the required gate.")
        else:
            msg = (f"{path}:{job.line}: job `{job.name}` is not in `{CI_OK}` `needs:` "
                   "directly (a job reached only through another job's `needs:` is "
                   "skipped, not failed, when its dependency fails), so it can go "
                   "red and still merge. "
                   f"Add it to `{CI_OK}` `needs:` (and to any per-job list the gate "
                   "reads), or mark the job `# ci-ok: exempt <reason>`.")
        out.append(Finding(
            mode, "ci-ok needs", msg, path, job.line, "ci-ok-coverage",
            "ci-ok-exempt-no-reason" if exempt else "ci-ok-needs-missing",
            path, job.name, job.line,
        ))
    return out, []


def has_on_key(text):
    """True when a top-level `on:` key exists (the lint cannot scope a file without one)."""
    return re.search(r"(?m)^on:", text) is not None


def job_line(text, name, blocks):
    """1-based line of the job header; `split_jobs` and `job_blocks` may disagree on odd headers."""
    for job in blocks:
        if job.name == name:
            return job.line
    m = re.search(r"(?m)^  " + re.escape(name) + r":\n", text)
    return text.count("\n", 0, m.start()) + 1 if m else 1


def lint_file(path, mode, heavy_after, facts=None):
    """Findings and notes for one workflow.

    `facts`, when given, receives the scope decision for the JSON report:
    `scope` is "unparseable", "no-pull-request-trigger", "non-required" or
    "linted", and `no_ci_ok` is set when the ci-ok rule was skipped.
    """
    out, notes = [], []
    facts = {} if facts is None else facts
    text = Path(path).read_text(encoding="utf-8")
    on_text = on_section(text)
    if not has_on_key(text):
        facts["scope"] = "unparseable"
    if not has_pr_trigger(on_text):
        notes.append(f"{path}: no pull_request trigger; full-run path, out of scope")
        facts.setdefault("scope", "no-pull-request-trigger")
        return out, notes
    if EXEMPT.search(text):
        notes.append(f"{path}: marked ci-policy non-required; out of scope")
        facts["scope"] = "non-required"
        return out, notes
    facts["scope"] = "linted"
    if has_workflow_paths(on_text):
        out.append(Finding(
            mode, "Change gating",
            f"{path}: workflow-level `on: pull_request: paths:` silences "
            "required checks on PRs outside it. Gate at job level instead "
            "(`needs:` + `if:`, aggregated by `ci-ok`), or mark the workflow "
            "`ci-policy: non-required` with a reason when it truly produces "
            "no required check.",
            rule="workflow-paths", code="workflow-level-paths", path=path,
        ))
    blocks = job_blocks(text)
    for name, body in split_jobs(text).items():
        tm = TIMEOUT.search(body)
        timeout = int(tm.group(1)) if tm else None
        heavy = timeout is None or timeout >= heavy_after
        if not heavy:
            continue
        if OUTPUTS.search(body):
            continue  # the detector publishes outputs; it is the gate
        im = JOB_IF.search(body)
        if im and GATED.search(im.group(1)):
            continue
        why = "has no `timeout-minutes` (GitHub defaults to 6h)" if timeout is None else (
            f"`timeout-minutes: {timeout}` with no change-gating `if:`")
        out.append(Finding(
            mode, "Change gating",
            f"{path}: heavy job `{name}` {why}. Run it only when its area "
            "changed (`needs:` the detector, `if:` on its outputs), or record "
            "why it always runs.",
            rule="change-gating",
            code="heavy-job-no-timeout" if timeout is None else "heavy-job-ungated",
            path=path, job=name, job_line=job_line(text, name, blocks),
        ))
    f, n = check_ci_ok_needs(path, text, mode)
    out.extend(f)
    notes.extend(n)
    facts["no_ci_ok"] = bool(n)
    return out, notes


def evaluate(files, mode="warning", heavy_after=HEAVY_DEFAULT):
    findings, notes = [], []
    for path in files:
        f, n = lint_file(path, mode, heavy_after)
        findings.extend(f)
        notes.extend(n)
    return findings, notes


def _escape(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_prop(text):
    return _escape(str(text)).replace(":", "%3A").replace(",", "%2C")


def annotation(f):
    where = f"file={_escape_prop(f.file)},line={f.line}," if f.file and f.line else ""
    return f"::{f.level} {where}title={_escape_prop(f.title)}::{_escape(f.message)}"


SCHEMA = "ci-policy-verdicts.v1"

# code -> next_action: a closed vocabulary, no prose and no workflow content.
FINDING_ACTIONS = {
    "heavy-job-no-timeout": "gate_job_on_change_detection",
    "heavy-job-ungated": "gate_job_on_change_detection",
    "workflow-level-paths": "move_paths_filter_to_job_level",
    "ci-ok-needs-missing": "add_job_to_ci_ok_needs",
    "ci-ok-exempt-no-reason": "add_exempt_reason",
}
UNMEASURABLE_ACTIONS = {
    "input-missing": "fix_input_path",
    "input-invalid-encoding": "fix_input_encoding",
    "workflow-unparseable": "fix_workflow_syntax",
    "no-inputs-discovered": "provide_workflow_files",
}
LIMITS = {
    "ci-ok-always-structural": (
        "A job directly listed in the ci-ok needs: that runs under always() counts as "
        "covered even though its failure would not propagate; the lint is structural."),
    "skipped-no-pull-request-trigger": (
        "Workflows with no pull_request or pull_request_target trigger are out of scope "
        "and were not linted."),
    "skipped-non-required-marker": (
        "Workflows carrying a ci-policy: non-required marker are out of scope and were "
        "not linted."),
    "ci-ok-rule-skipped-no-ci-ok-job": (
        "The ci-ok completeness rule does not apply to workflows without a ci-ok job."),
}
SCOPE_LIMIT = {
    "no-pull-request-trigger": "skipped-no-pull-request-trigger",
    "non-required": "skipped-non-required-marker",
}
USAGE = "usage: ci_policy.py [--json] [workflow-file ...]"
HELP = USAGE + """

Lint PR-triggered GitHub workflows for change gating and ci-ok completeness.
Default: annotations and notes on stdout. Exit 0 or 1 (1 only in error mode).

  --json    print exactly one `ci-policy-verdicts.v1` JSON object on stdout:
            outcome clean|findings|unmeasurable, totals, findings (rule, path,
            job, line, code, retryable, next_action), unmeasurable inputs and
            coverage.limits. Exit 2 when any input could not be measured.
  -h, --help  print this help (plain text, exit 0).

Environment: CI_POLICY_MODE warn|error (default warn), HEAVY_TIMEOUT_MINUTES.
Default files: .github/workflows/*.yml
"""


def _unmeasurable(path, code):
    return {"path": path, "code": code, "retryable": False,
            "next_action": UNMEASURABLE_ACTIONS[code]}


def verdict_report(files, mode, heavy_after, discovered=False):
    """The `ci-policy-verdicts.v1` object; rule, location and code only, never content."""
    files = sorted(set(files))
    findings, unmeasurable = [], []
    linted = skipped = 0
    skip_paths = {code: [] for code in SCOPE_LIMIT.values()}
    no_ci_ok = []
    for path in files:
        facts = {}
        try:
            found, _ = lint_file(path, mode, heavy_after, facts)
        except UnicodeDecodeError:
            unmeasurable.append(_unmeasurable(path, "input-invalid-encoding"))
            continue
        except OSError:
            unmeasurable.append(_unmeasurable(path, "input-missing"))
            continue
        scope = facts.get("scope")
        if scope == "unparseable":
            unmeasurable.append(_unmeasurable(path, "workflow-unparseable"))
        elif scope in SCOPE_LIMIT:
            skipped += 1
            skip_paths[SCOPE_LIMIT[scope]].append(path)
        else:
            linted += 1
            if facts.get("no_ci_ok"):
                no_ci_ok.append(path)
        for f in found:
            findings.append({
                "rule": f.rule, "path": f.path, "job": f.job or None,
                "line": f.job_line or None, "code": f.code, "retryable": False,
                "next_action": FINDING_ACTIONS[f.code],
            })
    findings.sort(key=lambda r: (r["path"], r["job"] or "", r["rule"], r["code"]))
    requested = len(files)
    if not files and discovered:
        unmeasurable.append(_unmeasurable(None, "no-inputs-discovered"))
    unmeasurable.sort(key=lambda u: (u["path"] or "", u["code"]))
    limit_paths = {"ci-ok-always-structural": [], **skip_paths,
                   "ci-ok-rule-skipped-no-ci-ok-job": no_ci_ok}
    limits = [{"code": code, "description": LIMITS[code], "paths": paths}
              for code, paths in limit_paths.items()
              if code == "ci-ok-always-structural" or paths]
    outcome = ("unmeasurable" if unmeasurable or not requested
               else "findings" if findings else "clean")
    return {
        "schema": SCHEMA,
        "mode": "error" if mode == "error" else "warn",
        "outcome": outcome,
        "totals": {"requested": requested, "linted": linted, "skipped": skipped,
                   "unmeasurable": len([u for u in unmeasurable if u["path"] is not None]),
                   "findings": len(findings)},
        "findings": findings,
        "unmeasurable": unmeasurable,
        "coverage": {"limits": limits},
    }


def main(env=None, argv=None):
    env = dict(os.environ) if env is None else env
    mode = "error" if env.get("CI_POLICY_MODE", "warn").strip().lower() == "error" else "warning"
    try:
        heavy_after = int(env.get("HEAVY_TIMEOUT_MINUTES") or HEAVY_DEFAULT)
    except ValueError:
        heavy_after = HEAVY_DEFAULT
    args = list(argv[1:] if argv is not None else sys.argv[1:])
    as_json, paths = False, []
    for i, arg in enumerate(args):
        if arg == "--":
            paths.extend(args[i + 1:])
            break
        if arg in ("-h", "--help"):
            print(HELP, end="")
            return 0
        if arg == "--json":
            as_json = True
        elif arg.startswith("-") and arg != "-":
            print(f"{USAGE}\nci_policy.py: error: unrecognized option: {arg}", file=sys.stderr)
            return 2
        else:
            paths.append(arg)
    files = paths or sorted(glob.glob(os.path.join(".github", "workflows", "*.yml")))
    if as_json:
        report = verdict_report(files, mode, heavy_after, discovered=not paths)
        print(json.dumps(report, indent=2, sort_keys=False))
        if report["outcome"] == "unmeasurable":
            return 2
        return 1 if mode == "error" and report["findings"] else 0
    findings, notes = evaluate(files, mode, heavy_after)
    for f in findings:
        print(annotation(f))
    for n in notes:
        print(f"note: {n}")
    if any(f.level == "error" for f in findings):
        return 1
    print("CI policy OK" if not findings else "CI policy OK (warnings above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
