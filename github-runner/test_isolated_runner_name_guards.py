"""Public runner attestation and private-canary predicate refusal proofs.

Fully offline: exercises the exact public guard predicate from
.github/workflows/ci.yml (hosted attestation) and the optional private
.github/workflows/garm-canary.yml (GARM prefix tripwire) in disposable
`bash -c` subprocesses with a controlled RUNNER_NAME/RUNNER_ENVIRONMENT
only. No host, no network, no credentials, no runner. Run from repo root:

python3 -B -m unittest github-runner.test_isolated_runner_name_guards -v

Auto-discovered by the existing offline-suite step:
python3 -m unittest discover -s github-runner -p 'test_isolated_*.py'

If a guard is edited, the anchor assertions fail first by design: update the
inline snippet copies AND the fixture matrix together, and keep
verification/case_221a4156-selfhosted-runner-gate.sh green.
"""
import os
import pathlib
import subprocess
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_CANARY = _ROOT / ".github" / "workflows" / "garm-canary.yml"

# Public CI requires the platform-provided hosted-environment attestation.
# A runner name alone must never admit private infrastructure.
STATIC_GUARD = '[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]'
STATIC_ANCHORS = ('"${RUNNER_ENVIRONMENT:-}" == github-hosted',)
CANARY_GUARD = 'case "${RUNNER_NAME:-}" in garm-*) ;; *) exit 1 ;; esac'
CANARY_ANCHOR = 'garm-*) ;;'


def run_guard(guard, runner_name=None, unset=False, runner_environment=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if not unset:
        env["RUNNER_NAME"] = runner_name
    if runner_environment is not None:
        env["RUNNER_ENVIRONMENT"] = runner_environment
    result = subprocess.run(["bash", "-c", guard], env=env,
                            capture_output=True, text=True, timeout=10)
    return result.returncode


class RunnerNameGuardTest(unittest.TestCase):
    def test_anchors_present(self):
        ci = _CI.read_text()
        for anchor in STATIC_ANCHORS:
            self.assertIn(anchor, ci, "static guard anchor moved: " + anchor)

    @unittest.skipUnless(_CANARY.is_file(),
                         "Public snapshot excludes the private live GARM canary workflow; offline predicate tests still run")
    def test_private_canary_anchor_present(self):
        canary = _CANARY.read_text()
        self.assertIn(CANARY_ANCHOR, canary, "canary guard anchor moved")
        # The canary tripwire must stay fail-closed on unset: no bare
        # `case "$RUNNER_NAME"` that crashes under `set -u` instead of
        # reaching the ::error arm.
        self.assertNotIn('case "$RUNNER_NAME"', canary)

    def test_public_guard_refuses_private_fleets_without_attestation(self):
        # Historical fleet names are negative controls, not an allowlist.
        for name in ("coolify-vps-6", "coolify-vps-1",
                     "ci-node7b1ff338-iso-1", "ci-node7b1ff338-iso-7",
                     "garm-two-abc123", "garm-old-xyz", "garm-1"):
            with self.subTest(runner=name):
                self.assertNotEqual(run_guard(STATIC_GUARD, name), 0)

    def test_static_guard_refuses_mislabeled_runner(self):
        # Foreign statics, case variants and empty/unset must all fail.
        for name in ("static-runner", "garm", " garm-two-1", "two-selfhosted-1", "", "GARM-TWO-1",
                     "coolify-vps", "ci-node7b1ff338-iso", " coolify-vps-6"):
            with self.subTest(runner=name):
                self.assertNotEqual(run_guard(STATIC_GUARD, name), 0)
        self.assertNotEqual(run_guard(STATIC_GUARD, unset=True), 0)
        # Outside the github-hosted overflow environment a mislabeled name
        # is refused even when some environment value is present.
        for env in ("self-hosted", ""):
            with self.subTest(env=env):
                self.assertNotEqual(
                    run_guard(STATIC_GUARD, "static-runner",
                              runner_environment=env), 0)

    def test_static_guard_overflow_env_admits_any_name(self):
        # CI overflow mode: a github-hosted environment passes the name
        # allowlist regardless of RUNNER_NAME, including empty.
        for name in ("static-runner", "garm-two-abc123", ""):
            with self.subTest(runner=name):
                self.assertEqual(
                    run_guard(STATIC_GUARD, name,
                              runner_environment="github-hosted"), 0)

    def test_canary_tripwire_admits_garm_refuses_static(self):
        for name in ("garm-two-abc123", "garm-node575e13db-xyz", "garm-1"):
            with self.subTest(runner=name):
                self.assertEqual(run_guard(CANARY_GUARD, name), 0)
        for name in ("coolify-vps-6", "ci-node7b1ff338-iso-1",
                     "static-runner", "", "GARM-TWO-1"):
            with self.subTest(runner=name):
                self.assertNotEqual(run_guard(CANARY_GUARD, name), 0)
        self.assertNotEqual(run_guard(CANARY_GUARD, unset=True), 0)

    def test_garm_names_do_not_bypass_public_isolation(self):
        # A private canary name predicate is not public runner attestation.
        for name in ("garm-two-abc123", "garm-old-xyz"):
            with self.subTest(runner=name):
                self.assertNotEqual(run_guard(STATIC_GUARD, name), 0)
                self.assertEqual(run_guard(CANARY_GUARD, name), 0)
        for name in ("coolify-vps-6", "ci-node7b1ff338-iso-1"):
            with self.subTest(runner=name):
                self.assertNotEqual(run_guard(CANARY_GUARD, name), 0)



if __name__ == "__main__":
    unittest.main()
