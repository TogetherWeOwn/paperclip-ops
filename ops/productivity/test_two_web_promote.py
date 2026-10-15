#!/usr/bin/env python3
"""Offline: Cloudflare-derived live identity must win over release metadata. No host, network or credential."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from two_web_promote import (
    LiveIdentityError,
    decide_promotion,
    derive_live_prod_identity,
    main,
    rollback_command,
)

# Synthetic fixture identifiers only (zero-padded, not production secrets).
# RELEASE_COMMIT is the fixture tag value the old flow read as live; the
# Cloudflare deployment below serves a different version/commit. This is a
# standalone offline port: no release-sorting helper exists in this repo to
# execute, so the regression is fixture-level (served commit wins over the
# tag value), not a run of old code.
RELEASE_COMMIT = "e360cca2" + "0" * 32
ACTIVE_VERSION = "eced969f" + "0" * 32
ACTIVE_COMMIT = "15423dc9" + "0" * 32
ACTIVE_BUILD = "b1" + "0" * 30
SMOKE_CMD = "./run-smoke.sh"


def deployments(version_id=ACTIVE_VERSION, percentage=100):
    return {"result": {"deployments": [{"versions": [{"version_id": version_id, "percentage": percentage}]}]}}


def builds(version_id=ACTIVE_VERSION, commit=ACTIVE_COMMIT):
    return {
        "result": {
            "builds": {
                version_id: {
                    "build_uuid": ACTIVE_BUILD,
                    "build_trigger_metadata": {"commit_hash": commit},
                }
            }
        }
    }


def write_json(tmpdir, name, payload):
    path = Path(tmpdir) / name
    if isinstance(payload, str):
        path.write_text(payload)
    else:
        path.write_text(json.dumps(payload))
    return str(path)


def run_cli(deployments_payload, builds_payload, extra=()):
    with tempfile.TemporaryDirectory() as tmpdir:
        dep = write_json(tmpdir, "deployments.json", deployments_payload)
        bld = write_json(tmpdir, "builds.json", builds_payload)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["--deployments", dep, "--builds", bld, *extra])
        return rc, out.getvalue(), err.getvalue()


class LiveIdentity(unittest.TestCase):
    def test_active_version_wins_over_release_tag(self):
        # Fixture-level regression: the tag value differs from the served
        # version. The helper must return the Cloudflare identity (or
        # refuse), never the tag commit.
        identity = derive_live_prod_identity(deployments(), builds())
        self.assertEqual(identity["commit"], ACTIVE_COMMIT)
        self.assertEqual(identity["version_id"], ACTIVE_VERSION)
        self.assertNotEqual(identity["commit"], RELEASE_COMMIT)

    def test_decision_and_rollback_use_live_identity(self):
        identity = derive_live_prod_identity(deployments(), builds())
        self.assertEqual(decide_promotion(identity["commit"], ACTIVE_COMMIT), "already-live")
        self.assertEqual(decide_promotion(identity["commit"], RELEASE_COMMIT), "promote")
        expected = [
            "scripts/dispatch_deploy.sh",
            "rollback",
            "--commit",
            ACTIVE_COMMIT,
            "--smoke-cmd",
            SMOKE_CMD,
        ]
        self.assertEqual(rollback_command(identity["commit"], SMOKE_CMD), expected)
        self.assertNotIn(RELEASE_COMMIT, rollback_command(identity["commit"], SMOKE_CMD))

    def test_rollback_matches_dispatch_cli(self):
        cmd = rollback_command(ACTIVE_COMMIT, SMOKE_CMD)
        self.assertEqual(cmd[0], "scripts/dispatch_deploy.sh")
        self.assertIn("rollback", cmd)
        self.assertIn("--smoke-cmd", cmd)
        self.assertIn(ACTIVE_COMMIT, cmd)

    def test_rollback_requires_commit_and_smoke_cmd(self):
        with self.assertRaises(LiveIdentityError):
            rollback_command("", SMOKE_CMD)
        with self.assertRaises(LiveIdentityError):
            rollback_command(ACTIVE_COMMIT, "")

    def test_decide_requires_both_commits(self):
        with self.assertRaises(LiveIdentityError):
            decide_promotion("", ACTIVE_COMMIT)
        with self.assertRaises(LiveIdentityError):
            decide_promotion(ACTIVE_COMMIT, "")

    def test_unknown_version_mapping_refuses(self):
        with self.assertRaisesRegex(LiveIdentityError, "no known build mapping"):
            derive_live_prod_identity(deployments(), builds(version_id="other" + "0" * 35))

    def test_build_without_commit_refuses(self):
        bad = {"result": {"builds": {ACTIVE_VERSION: {"build_uuid": ACTIVE_BUILD}}}}
        with self.assertRaisesRegex(LiveIdentityError, "no commit hash"):
            derive_live_prod_identity(deployments(), bad)

    def test_non_object_metadata_refuses(self):
        bad = {
            "result": {
                "builds": {
                    ACTIVE_VERSION: {
                        "build_uuid": ACTIVE_BUILD,
                        "build_trigger_metadata": "not-an-object",
                    }
                }
            }
        }
        with self.assertRaisesRegex(LiveIdentityError, "non-object build metadata"):
            derive_live_prod_identity(deployments(), bad)

    def test_mixed_versions_refuse_without_silent_pick(self):
        mixed = {
            "result": {
                "deployments": [
                    {
                        "versions": [
                            {"version_id": ACTIVE_VERSION, "percentage": 50},
                            {"version_id": "f" * 40, "percentage": 50},
                        ]
                    }
                ]
            }
        }
        with self.assertRaisesRegex(LiveIdentityError, "mixed-version"):
            derive_live_prod_identity(mixed, builds())

    def test_staged_zero_percent_does_not_block(self):
        # Cloudflare smoke-test flow: new version staged at 0% next to the
        # old one at 100%. Only the serving version is live.
        staged = {
            "result": {
                "deployments": [
                    {
                        "versions": [
                            {"version_id": ACTIVE_VERSION, "percentage": 100},
                            {"version_id": "f" * 40, "percentage": 0},
                        ]
                    }
                ]
            }
        }
        identity = derive_live_prod_identity(staged, builds())
        self.assertEqual(identity["version_id"], ACTIVE_VERSION)
        self.assertEqual(identity["commit"], ACTIVE_COMMIT)

    def test_all_zero_percent_refuses(self):
        parked = {
            "result": {
                "deployments": [
                    {
                        "versions": [
                            {"version_id": ACTIVE_VERSION, "percentage": 0},
                            {"version_id": "f" * 40, "percentage": 0},
                        ]
                    }
                ]
            }
        }
        with self.assertRaisesRegex(LiveIdentityError, "no serving version"):
            derive_live_prod_identity(parked, builds())

    def test_duplicate_serving_entries_refuse(self):
        dup = {
            "result": {
                "deployments": [
                    {
                        "versions": [
                            {"version_id": ACTIVE_VERSION, "percentage": 50},
                            {"version_id": ACTIVE_VERSION, "percentage": 50},
                        ]
                    }
                ]
            }
        }
        with self.assertRaisesRegex(LiveIdentityError, "mixed-version"):
            derive_live_prod_identity(dup, builds())

    def test_malformed_entries_refuse(self):
        with self.assertRaisesRegex(LiveIdentityError, "not an object"):
            derive_live_prod_identity(
                {"result": {"deployments": [{"versions": [ACTIVE_VERSION, {"version_id": "f" * 40, "percentage": 50}]}]}},
                builds(),
            )
        with self.assertRaisesRegex(LiveIdentityError, "no version_id"):
            derive_live_prod_identity(
                {"result": {"deployments": [{"versions": [{"percentage": 50}, {"version_id": ACTIVE_VERSION, "percentage": 50}]}]}},
                builds(),
            )
        with self.assertRaisesRegex(LiveIdentityError, "not an object"):
            derive_live_prod_identity(
                {"result": {"deployments": [{"versions": [{"version_id": ACTIVE_VERSION, "percentage": 50}, "garbage"]}]}},
                builds(),
            )

    def test_string_percentage_refuses(self):
        bad = {
            "result": {
                "deployments": [
                    {"versions": [{"version_id": ACTIVE_VERSION, "percentage": "100"}]}
                ]
            }
        }
        with self.assertRaisesRegex(LiveIdentityError, "non-numeric percentage"):
            derive_live_prod_identity(bad, builds())

    def test_bool_percentage_refuses(self):
        bad = {
            "result": {
                "deployments": [
                    {"versions": [{"version_id": ACTIVE_VERSION, "percentage": True}]}
                ]
            }
        }
        with self.assertRaisesRegex(LiveIdentityError, "non-numeric percentage"):
            derive_live_prod_identity(bad, builds())

    def test_percentages_must_total_100(self):
        short = {
            "result": {
                "deployments": [{"versions": [{"version_id": ACTIVE_VERSION, "percentage": 50}]}]
            }
        }
        with self.assertRaisesRegex(LiveIdentityError, "total"):
            derive_live_prod_identity(short, builds())

    def test_list_form_builds(self):
        listed = {
            "result": {
                "builds": [
                    {
                        "version_id": ACTIVE_VERSION,
                        "build_uuid": ACTIVE_BUILD,
                        "build_trigger_metadata": {"commit_hash": ACTIVE_COMMIT},
                    }
                ]
            }
        }
        identity = derive_live_prod_identity(deployments(), listed)
        self.assertEqual(identity["commit"], ACTIVE_COMMIT)

    def test_empty_deployments_refuse(self):
        with self.assertRaisesRegex(LiveIdentityError, "no deployments"):
            derive_live_prod_identity({"result": {"deployments": []}}, builds())

    def test_empty_versions_refuse(self):
        with self.assertRaisesRegex(LiveIdentityError, "no versions"):
            derive_live_prod_identity({"result": {"deployments": [{"versions": []}]}}, builds())

    def test_main_success_prints_identity(self):
        rc, out, _ = run_cli(deployments(), builds())
        self.assertEqual(rc, 0)
        identity = json.loads(out)
        self.assertEqual(identity["version_id"], ACTIVE_VERSION)
        self.assertEqual(identity["commit"], ACTIVE_COMMIT)

    def test_main_candidate_already_live(self):
        rc, out, _ = run_cli(deployments(), builds(), extra=("--candidate", ACTIVE_COMMIT))
        self.assertEqual(rc, 0)
        self.assertIn("already-live", out)

    def test_main_candidate_promote(self):
        rc, out, _ = run_cli(deployments(), builds(), extra=("--candidate", RELEASE_COMMIT))
        self.assertEqual(rc, 0)
        self.assertIn("promote", out)

    def test_main_refusal_exits_2(self):
        mixed = {
            "result": {
                "deployments": [
                    {
                        "versions": [
                            {"version_id": ACTIVE_VERSION, "percentage": 50},
                            {"version_id": "f" * 40, "percentage": 50},
                        ]
                    }
                ]
            }
        }
        rc, _, err = run_cli(mixed, builds())
        self.assertEqual(rc, 2)
        self.assertIn("refused:", err)

    def test_main_string_percentage_refuses_rc_2(self):
        bad = {
            "result": {
                "deployments": [
                    {"versions": [{"version_id": ACTIVE_VERSION, "percentage": "100"}]}
                ]
            }
        }
        rc, _, err = run_cli(bad, builds())
        self.assertEqual(rc, 2)
        self.assertIn("refused:", err)

    def test_main_unreadable_input_exits_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dep = write_json(tmpdir, "deployments.json", "{not json")
            bld = write_json(tmpdir, "builds.json", builds())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["--deployments", dep, "--builds", bld])
            self.assertEqual(rc, 2)
            self.assertIn("refused:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
