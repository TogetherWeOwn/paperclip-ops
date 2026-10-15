#!/usr/bin/env python3
"""Offline acceptance for PR #6: pr_standards alignment landing blockers.

Reproduces, without network, database, or credentials, the four CI reds
seen on the pre-fix head ``db4d362`` (CI run 38682854470 plus the pr-lint
run 38682854483):

* secret-scan red: ``public-owned-paths.txt`` drifted from its generator
  (``Fail if the freeze list drifted from its generator``).
* change detection red: ``scripts/sync-toolkit.sh`` exits 1 because four
  manifest rows pin first-party verification gates at release ``v0.2.2``,
  which never shipped them (``Restore toolkit-vendored files``).
* pr-lint red: the lint job schedules on ``ubuntu-latest`` but its first
  step refuses any runner whose name is not ``garm-*``, so every
  GitHub-hosted pickup fails (``Prove a GARM runner selected this job``).
* ci-ok red: derivative of the secret-scan and change-detection failures
  (``needed job secret-scan ended failure``, ``needed job changes ended
  failure``); no independent aggregator defect was observed.

The first class below guards the PR's own fix (private card-ref gate and
internal detection now follow the ``case_`` convention); it is green on the
pre-fix head and must stay green. The remaining red classes fail on the
pre-fix head by design and go green once the landing change fixes the
underlying gates. Run with ``python3 -m unittest test_pr6_acceptance``
(also wired into ``scripts/verify-fast.sh``). Stdlib only, hermetic, well
under a second apart from the repository's own lint suite it shells out to.
"""

import difflib
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / ".github" / "scripts"

sys.path.insert(0, str(SCRIPTS))
import pr_standards as ps  # noqa: E402

# First-party verification gates that live in this repo. The manifest must
# not route them through any toolkit release: the release never shipped
# them, so the restore step exits 1 on the first such row.
FIRST_PARTY_GATES = [
    "verification/case_9e76f76d-recorded-command.sh",
    "verification/case_9e76f76d-recorded-command.txt",
    "verification/case_46e26fa6-mutation-gate.sh",
    "verification/case_f909ca1f-mutation-gate.sh",
]

WORKFLOWS = [
    ".github/workflows/pr-lint.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/docs-fast-path.yml",
    ".github/workflows/public-safety.yml",
    ".github/workflows/test-guard.yml",
]

# Runner labels served by GitHub-hosted capacity (paid fallback pool).
HOSTED_LABEL_PREFIXES = ("ubuntu-", "windows-", "macos-")


def clean_env():
    """Subprocess env without inherited repo-locating variables."""
    env = dict(os.environ)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(var, None)
    return env


def git(args):
    """Run git inside this checkout; fail closed when git cannot answer."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError("git unavailable for offline check: %r" % (exc,))
    if proc.returncode != 0:
        raise AssertionError(
            "git %s failed: %s" % (" ".join(args), proc.stderr.strip())
        )
    return proc.stdout


def freeze_base_ref():
    """Base tree the freeze recipe diffs against (CI uses the PR base SHA)."""
    candidates = [os.environ.get("PR6_FREEZE_BASE", ""), "origin/main", "HEAD"]
    for ref in candidates:
        if not ref:
            continue
        try:
            git(["rev-parse", "--verify", "--quiet", ref + "^{tree}"])
            return ref
        except AssertionError:
            continue
    raise AssertionError("no base tree resolvable for freeze recipe")


def regen_owned():
    """Replicate the secret-scan freeze generator byte for byte."""
    manifest = (ROOT / "vendor" / "toolkit-files.txt").read_text().splitlines()
    dests = sorted(
        d
        for d in {line.split("|")[0] for line in manifest if line}
        if not d.startswith("plugins/model-selection/")
    )
    base = freeze_base_ref()
    base_files = set(git(["ls-tree", "-r", "--name-only", base]).splitlines())
    head_files = set(git(["ls-files"]).splitlines())
    survivors = base_files & head_files
    return sorted(set(dests) - survivors)


def committed_owned():
    """The committed freeze list minus comments and blanks."""
    return sorted(
        line
        for line in (ROOT / "public-owned-paths.txt").read_text().splitlines()
        if line and not line.startswith("#")
    )


def manifest_rows():
    """Parsed (dest, src, tag-or-None) manifest rows."""
    rows = []
    for line in (ROOT / "vendor" / "toolkit-files.txt").read_text().splitlines():
        if not line:
            continue
        parts = line.split("|")
        assert len(parts) <= 3, "malformed manifest row: %r" % (line,)
        dest = parts[0]
        assert dest, "empty manifest dest: %r" % (line,)
        rows.append((dest, parts[1] if len(parts) > 1 else dest,
                     parts[2] if len(parts) > 2 else None))
    return rows


class PrStandardsAlignment(unittest.TestCase):
    """The PR's own fix: card-ref detection follows the case_ convention."""

    def test_private_card_gate_uses_case_refs(self):
        """A legacy tracker id alone must not satisfy the private gate."""
        env = {
            "EVENT": "pull_request",
            "TITLE": "fix(scope): do a thing",
            "HEAD_REF": "fix/short-slug",
            "REPO_PRIVATE": "true",
            "REQUIRE_CARD_REF": "true",
            "PR_STANDARDS_MODE": "warn",
        }
        legacy = "Refs: %s-%d" % ("TO" + "G", 123)
        for body, want in (
            ("just some words", True),
            (legacy, True),
            ("Refs: case_1cab5f7f", False),
        ):
            env["BODY"] = body
            got = any(
                f.title == "Card reference"
                for f in ps.check_pull_request(dict(env))
            )
            self.assertEqual(
                got, want, "body %r: Card reference finding=%s" % (body, got)
            )

    def test_internal_hits_flags_case_refs_and_new_patterns(self):
        """case_ refs, documentation IPs and example hosts are internal."""
        hits = ps.internal_hits("see case_1cab5f7f here", "TOG|PAP|PAPA")
        self.assertTrue(
            any("case_1cab5f7f" in h for h in hits),
            "card ref not flagged: %r" % (hits,),
        )
        for sample, label in (
            ("server 192.0.2.7 up", "documentation IP"),
            ("server 198.51.100.9 up", "documentation IP"),
            ("server 203.0.113.4 up", "documentation IP"),
            ("host foo.example.invalid down", "example.invalid"),
        ):
            self.assertTrue(
                any(label in h
                    for h in ps.internal_hits(sample, "TOG|PAP|PAPA")),
                "%r: %s not flagged" % (sample, label),
            )

    def test_internal_hits_look_alikes_stay_clean(self):
        """Version strings and out-of-range addresses are not internal."""
        # Fragments are concatenated so no tracker-shaped token appears in
        # this public file (the hygiene self-check below forbids them).
        sample = (
            "CV" + "E-2026-1234 and GP" + "T-4 on 172.32.0.1 ok"
        )
        hits = ps.internal_hits(sample, "TOG|PAP|PAPA")
        self.assertEqual(hits, [], "look-alikes flagged: %r" % (hits,))

    def test_pr_standards_suite_green(self):
        """The repository's own lint suite passes on this tree."""
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "test_pr_standards"],
            cwd=str(SCRIPTS),
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=300,
        )
        tail = (proc.stderr + proc.stdout).strip().splitlines()[-3:]
        self.assertEqual(
            proc.returncode, 0, "lint suite red: %r" % (tail,)
        )


class FreezeListDrift(unittest.TestCase):
    """secret-scan red: the committed freeze list matches its generator."""

    def test_freeze_list_matches_generator(self):
        """Regenerated owned-paths equal public-owned-paths.txt."""
        want = regen_owned()
        got = committed_owned()
        self.assertEqual(
            got, want,
            "freeze list drifted; re-run the command in the file header:\n"
            + "".join(difflib.unified_diff(
                got, want, fromfile="committed", tofile="regen", lineterm=""
            ))[:2000],
        )


class ToolkitRestore(unittest.TestCase):
    """change-detection red: every manifest row restores from its pin."""

    def test_first_party_gates_tracked(self):
        """The four gates exist in the tree (sync skips tracked paths)."""
        missing = [p for p in FIRST_PARTY_GATES if not (ROOT / p).is_file()]
        self.assertEqual(missing, [], "first-party gates missing: %r" % (missing,))

    def test_no_manifest_row_for_first_party_gates(self):
        """No manifest row may route a first-party gate anywhere."""
        rows = [dest for dest, _, _ in manifest_rows()]
        bad = [p for p in FIRST_PARTY_GATES if p in rows]
        self.assertEqual(
            bad, [],
            "manifest rows restore exits 1 on (release never shipped them): %r"
            % (bad,),
        )

    def test_manifest_pins_resolve(self):
        """Every explicit release tag has a row in the pins file."""
        pinned = set()
        for line in (ROOT / "vendor" / "toolkit-pins.txt").read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            pinned.add(line.split()[0])
        tags = {tag for _, _, tag in manifest_rows() if tag}
        self.assertEqual(
            sorted(tags - pinned), [],
            "manifest tags without a pin: %r" % (sorted(tags - pinned),),
        )

    def test_manifest_rows_well_formed(self):
        """Rows are dest[|src[|tag]] with unique dests."""
        rows = manifest_rows()
        dests = [dest for dest, _, _ in rows]
        self.assertEqual(
            len(dests), len(set(dests)), "duplicate manifest dests"
        )


class RunnerProofConsistency(unittest.TestCase):
    """pr-lint red: a job must be able to pass where it is scheduled."""

    @staticmethod
    def job_chunks(text):
        """Split workflow text into (job id, chunk) after the jobs: key."""
        chunks, cur, started = [], None, False
        for line in text.splitlines():
            if re.match(r"^jobs:\s*$", line):
                started = True
                continue
            if not started:
                continue
            m = re.match(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$", line)
            if m:
                cur = [m.group(1), []]
                chunks.append(cur)
                continue
            if cur is not None:
                cur[1].append(line)
        return [(name, "\n".join(body)) for name, body in chunks]

    @staticmethod
    def runs_on_labels(chunk):
        """Every runs-on value of one job chunk (inline or flow list)."""
        labels = []
        for m in re.finditer(r"(?m)^\s*runs-on:\s*(.+?)\s*(?:#.*)?$", chunk):
            val = m.group(1).strip()
            if val.startswith("["):
                labels.extend(
                    v.strip().strip("'\"")
                    for v in val.strip("[]").split(",")
                )
            else:
                labels.append(val.strip().strip("'\""))
        return labels

    def test_hosted_jobs_have_no_hard_runner_refusal(self):
        """No hosted-scheduled step may hard-refuse hosted runners."""
        offenders = []
        for rel in WORKFLOWS:
            text = (ROOT / rel).read_text()
            for job, chunk in self.job_chunks(text):
                labels = self.runs_on_labels(chunk)
                if not any(
                    label.startswith(HOSTED_LABEL_PREFIXES) for label in labels
                ):
                    continue
                steps = re.split(r"(?m)^      - \w", chunk)[1:]
                for step in steps:
                    if (
                        "RUNNER_NAME" in step
                        and "exit 1" in step
                        and "github-hosted" not in step
                    ):
                        first = next(
                            (
                                line.strip()
                                for line in step.splitlines()
                                if line.strip()
                            ),
                            "?",
                        )
                        offenders.append("%s/%s: %s" % (rel, job, first[:80]))
        self.assertEqual(
            offenders,
            [],
            "steps that fail on the pool they schedule on: %r" % (offenders,),
        )

    def test_pr_lint_still_lints(self):
        """The lint job still runs its unit suites and script checks."""
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        for needle in (
            "test_pr_standards.py",
            "test_ci_policy.py",
            "test_action_pins.py",
            ".github/scripts/pr_standards.py",
            "ci_policy.py",
            "action_pins.py",
        ):
            self.assertIn(needle, text, "lint coverage lost: %s" % needle)


class CiOkGraph(unittest.TestCase):
    """ci-ok red is derivative; the aggregator graph itself stays intact."""

    def test_ci_ok_needs_name_defined_jobs(self):
        """Every job ci-ok waits on is defined in the same workflow."""
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        jobs = set()
        in_jobs = False
        for line in text.splitlines():
            if re.match(r"^jobs:\s*$", line):
                in_jobs = True
                continue
            if in_jobs:
                m = re.match(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$", line)
                if m:
                    jobs.add(m.group(1))
        needs = set()
        for m in re.finditer(r"(?m)^\s*needs:\s*\[(.+?)\]", text):
            needs.update(v.strip().strip("'\"") for v in m.group(1).split(","))
        for m in re.finditer(
            r"(?m)^\s*needs:\s*$\n((?:^      - .+\n)+)", text
        ):
            for line in m.group(1).splitlines():
                needs.add(line.split("-", 1)[1].strip().strip("'\""))
        self.assertTrue(jobs, "no jobs parsed from ci.yml")
        self.assertEqual(
            sorted(needs - jobs), [],
            "ci-ok waits on undefined jobs: %r" % (sorted(needs - jobs),),
        )


class PublicText(unittest.TestCase):
    def test_no_tracker_ids_in_this_suite(self):
        """Public text carries no tracker ids (hygiene gate)."""
        text = Path(__file__).read_text()
        hits = re.findall(r"\b[A-Z]{2,}-\d+", text)
        self.assertEqual(hits, [], "tracker ids in public text: %r" % (hits,))


if __name__ == "__main__":
    unittest.main()
