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

The first class below guards the PR's own fix (private card-ref gate accepts
a tracker card ref or a ``case_`` ref; public detection flags standalone
``case_`` refs and tracker ids but stays clean on tracked ``case_`` paths
and documentation placeholders); it is green on the pre-fix head and must
stay green. The remaining red classes fail on the pre-fix head by design and
go green once the landing change fixes the underlying gates.

After the hosted public-CI restoration landed on main and was merged into
this branch, three contracts were reconciled with the merged implementation
(the public-CI suite in ``.github/scripts/test_public_ci.py`` pins the same
shapes): the repo-split freeze guards apply only to a private source
repository while the secret and privacy scans stay unconditional; the four
gate destinations are retained in the manifest, remapped to public toolkit
source paths restored under the default pin; and the disk-pressure
preflight is advisory on ephemeral runners, not a runner-pool refusal. The
lint-suite guard keeps the lint test helper synthesizing tracker ids for
every evaluation while the private gate accepts either shape, so both the
tracker and ``case_`` positives stay green. Run with ``python3 -m unittest test_pr6_acceptance``
(also wired into ``scripts/verify-fast.sh``). Stdlib only, hermetic, well
under a second apart from the repository's own lint suite it shells out to.
"""

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

# Verification gates whose destinations in this repo carry case_ names.
# The merged public CI retains them in the manifest, remapped to the public
# source paths the toolkit actually ships; a row that instead routed a
# destination to its own case_ path in a toolkit release would fail restore,
# because no release ever shipped the gates under those names.
REMAPPED_GATE_SOURCES = {
    "verification/case_9e76f76d-recorded-command.sh":
        "verification/recorded-monitor-command-gate.sh",
    "verification/case_9e76f76d-recorded-command.txt":
        "verification/recorded-monitor-command.txt",
    "verification/case_46e26fa6-mutation-gate.sh":
        "verification/plugin-deploy-drift-detector-mutation-gate.sh",
    "verification/case_f909ca1f-mutation-gate.sh":
        "verification/grant-attribution-resolver-mutation-gate.sh",
}
FIRST_PARTY_GATES = list(REMAPPED_GATE_SOURCES)

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


def pinned_tags():
    """Release labels with an immutable row in the pins file."""
    tags = set()
    for line in (ROOT / "vendor" / "toolkit-pins.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        tags.add(line.split()[0])
    return tags


def default_toolkit_tag():
    """The sync script's default label (rows without an explicit tag)."""
    text = (ROOT / "scripts" / "sync-toolkit.sh").read_text()
    match = re.search(r"TOOLKIT_TAG:-([^}]+)}", text)
    assert match, "default toolkit tag not found in sync-toolkit.sh"
    return match.group(1)


def ci_step(name, workflow=".github/workflows/ci.yml"):
    """Body of one named step in a workflow, failing closed if absent."""
    text = (ROOT / workflow).read_text()
    marker = "      - name: " + name + "\n"
    assert marker in text, "workflow step missing: %s" % (name,)
    return text.split(marker, 1)[1].split("      - ", 1)[0]


class PrStandardsAlignment(unittest.TestCase):
    """The PR's own fix: private gate accepts tracker or case_, public flags standalone refs."""

    def test_private_card_gate_accepts_tracker_or_case_ref(self):
        """A tracker id or a case_ ref satisfies the private gate; missing both fails."""
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
            (legacy, False),
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

    def test_internal_hits_flags_standalone_refs(self):
        """Standalone case_ refs and tracker ids are internal."""
        hits = ps.internal_hits("see case_1cab5f7f here", "TOG|PAP|PAPA")
        self.assertTrue(
            any("case_1cab5f7f" in h for h in hits),
            "card ref not flagged: %r" % (hits,),
        )
        legacy = "%s-%d" % ("TO" + "G", 999999991)
        hits = ps.internal_hits("see " + legacy + " here", "TOG|PAP|PAPA")
        self.assertTrue(
            any("ticket id" in h for h in hits),
            "tracker id not flagged: %r" % (hits,),
        )

    def test_internal_hits_paths_and_placeholders_stay_clean(self):
        """Tracked case_ paths and documentation placeholders are not internal.

        Keeps pr_standards.py consistent with public_safety.py, which exempts
        the RFC 5737 ranges and only flags tracker-shaped ids: a public PR
        must be able to name the files it changes and use example addresses.
        """
        for sample in (
            "see verification/case_c89b5362-drift-pin-mutation-gate.sh here",
            "server 192.0.2.7 up",
            "server 198.51.100.9 up",
            "server 203.0.113.4 up",
            "host foo.example.invalid down",
        ):
            hits = ps.internal_hits(sample, "TOG|PAP|PAPA")
            self.assertEqual(hits, [], "%r flagged: %r" % (sample, hits))

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
    """secret-scan red, as merged: freeze guards are private-only.

    The former private source repository regenerated the freeze list on
    every run and refused drift. The merged public CI deliberately scopes
    the repo-split guards to private repositories (the public repository
    must be able to own these paths), while the actual secret and privacy
    scans stay unconditional. Applying the former drift recipe to the
    public tree would therefore fail a guard CI no longer runs here.
    """

    def test_freeze_guards_apply_only_to_private_repositories(self):
        """Each repo-split guard step is gated on repository privacy."""
        for name in (
            "Fail if the moved plugin path is touched",
            "Fail if the freeze list drifted from its generator",
            "Fail if a toolkit-owned path is touched",
        ):
            step = ci_step(name)
            self.assertIn(
                "        if: github.event.repository.private\n", step,
                "freeze guard not private-only: %s" % (name,),
            )
        # The generator recipe itself stays in the private guard, so the
        # private contract still regenerates and compares byte for byte.
        drift = ci_step("Fail if the freeze list drifted from its generator")
        self.assertIn("vendor/toolkit-files.txt", drift)
        self.assertIn("public-owned-paths.txt", drift)
        self.assertIn("cmp -s", drift)

    def test_secret_and_privacy_scans_stay_unconditional(self):
        """Scoping the freeze guards must not scope the security scans."""
        scan = ci_step("Scan tracked files for committed secrets")
        self.assertIn("./verification/secret-scan.sh", scan)
        self.assertNotRegex(
            scan, r"(?m)^\s*if:", "secret scan gained a condition"
        )
        safety = (ROOT / ".github/workflows/public-safety.yml").read_text()
        self.assertIn("gitleaks", safety)
        self.assertIn("public_safety.py", safety)
        # The history scan fails closed: merge-base range on PRs,
        # before..HEAD on pushes, no narrowed fallback (see the scratch-repo
        # pin in .github/scripts/test_public_ci.py).
        self.assertNotIn('--log-opts="--all"', safety)
        self.assertNotIn('RANGE="-1 HEAD"', safety)
        self.assertIn("git merge-base origin/main HEAD", safety)
        self.assertIn("github.event.before", safety)

    def test_freeze_list_entries_are_manifest_destinations(self):
        """Committed and regenerated lists name only manifest dests."""
        dests = {
            dest
            for dest, _, _ in manifest_rows()
            if not dest.startswith("plugins/model-selection/")
        }
        committed = committed_owned()
        self.assertEqual(
            sorted(set(committed) - dests), [],
            "freeze list names non-manifest paths: %r"
            % (sorted(set(committed) - dests),),
        )
        regen = regen_owned()
        self.assertEqual(
            sorted(set(regen) - dests), [],
            "generator names non-manifest paths: %r"
            % (sorted(set(regen) - dests),),
        )


class ToolkitRestore(unittest.TestCase):
    """change-detection red: every manifest row restores from its pin."""

    def test_gate_files_present_after_restore(self):
        """The four gates exist in a prepared tree.

        Tracked gates are always present; vendored ones arrive via
        ``scripts/sync-toolkit.sh``, which verify-fast and CI run before
        this suite. On a fresh unrestored checkout this fails by design:
        restore first instead of treating the gap as a code defect.
        """
        missing = [p for p in FIRST_PARTY_GATES if not (ROOT / p).is_file()]
        self.assertEqual(missing, [], "first-party gates missing: %r" % (missing,))

    def test_gate_rows_remap_to_public_sources_under_a_pin(self):
        """Each retained gate restores from its public toolkit source.

        The merged public CI kept these destinations in the manifest and
        remapped them to source paths the toolkit ships, instead of the
        pre-merge proposal to drop the rows. A row self-routing a case_
        destination (source equal to dest, or an explicit old release pin)
        is the shape restore used to exit 1 on, so it must not return.
        """
        rows = {dest: (src, tag) for dest, src, tag in manifest_rows()}
        pinned = pinned_tags()
        default = default_toolkit_tag()
        self.assertIn(
            default, pinned, "default toolkit tag has no pin: %s" % default
        )
        for dest, want_src in REMAPPED_GATE_SOURCES.items():
            self.assertIn(dest, rows, "gate row dropped from manifest: %s" % dest)
            src, tag = rows[dest]
            self.assertEqual(
                src, want_src,
                "%s restores from %r, want public source %r" % (dest, src, want_src),
            )
            self.assertNotEqual(src, dest, "gate row self-routes: %s" % dest)
            effective = tag if tag else default
            self.assertIn(
                effective, pinned,
                "%s restores under unpinned tag %s" % (dest, effective),
            )

    def test_manifest_pins_resolve(self):
        """Every explicit release tag has a row in the pins file."""
        pinned = pinned_tags()
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

    @staticmethod
    def hard_runner_refusal(step):
        """True when a step fails runners outside a private fleet.

        A runner-pool refusal keys its failure on runner identity alone, so
        every hosted pickup dies in it. A disk-pressure style safety check
        is not a refusal: runners outside the persistent fleets leave
        through an advisory ``exit 0`` before the failing branch, and that
        branch is conditioned on the measured pressure, not identity.
        Steps that explicitly handle the hosted pool are likewise exempt.
        """
        if "RUNNER_NAME" not in step or "exit 1" not in step:
            return False
        if "github-hosted" in step:
            return False
        non_fleet = re.search(r"RUNNER_NAME[^\n]*!=", step)
        if (
            non_fleet
            and "exit 0" in step
            and non_fleet.start() < step.index("exit 0") < step.index("exit 1")
        ):
            return False
        return True

    def test_detector_rejects_a_garm_only_refusal(self):
        """A synthetic identity-only refusal is still flagged."""
        step = (
            "ame: Prove a GARM runner selected this job\n"
            "        run: |\n"
            '          if [[ "${RUNNER_NAME:-}" != garm-* ]]; then\n'
            '            echo "::error::requires a self-hosted runner"\n'
            "            exit 1\n"
            "          fi\n"
        )
        self.assertTrue(self.hard_runner_refusal(step))

    def test_detector_permits_an_advisory_safety_check(self):
        """A synthetic disk-pressure shape is not a pool refusal."""
        step = (
            "ame: runner disk-pressure preflight\n"
            "        run: |\n"
            '          out="$(./runner_disk_pressure.sh)"; rc=$?\n'
            '          if [[ "${RUNNER_NAME:-}" != coolify-vps-* ]]; then\n'
            '            echo "advisory only; not failing."\n'
            "            exit 0\n"
            "          fi\n"
            "          if [[ $rc -eq 1 ]]; then\n"
            '            echo "::error title=Runner disk pressure::$out"\n'
            "            exit 1\n"
            "          fi\n"
        )
        self.assertFalse(self.hard_runner_refusal(step))

    def test_disk_pressure_preflight_is_advisory_on_hosted(self):
        """The real preflight lets hosted runners through with exit 0."""
        step = ci_step("runner disk-pressure preflight")
        self.assertIn("advisory only on", step)
        self.assertIn("exit 0", step)
        self.assertFalse(self.hard_runner_refusal(step))

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
                    if self.hard_runner_refusal(step):
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
