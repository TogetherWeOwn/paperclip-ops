#!/usr/bin/env python3
"""Offline: Cloudflare-derived live identity must win over release metadata. No host, network or credential."""
import unittest

from two_web_promote import (
    LiveIdentityError,
    decide_promotion,
    derive_live_prod_identity,
    rollback_command,
)

# Synthetic fixture identifiers only (zero-padded, not production secrets).
# The release tag points at one commit while Cloudflare serves another.
RELEASE_COMMIT = "e360cca2" + "0" * 32
ACTIVE_VERSION = "eced969f" + "0" * 32
ACTIVE_COMMIT = "15423dc9" + "0" * 32
ACTIVE_BUILD = "b1" + "0" * 30


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


def release_tag_payload():
    """What the old release-sorting helper read: newest tag -> RELEASE_COMMIT."""
    return {"tag": "v2026.10.15", "commit": RELEASE_COMMIT}


class LiveIdentity(unittest.TestCase):
    def test_active_version_wins_over_release_tag(self):
        # Regression: the release tag differs from the served version. The
        # promoter must return the Cloudflare identity (or refuse), never the
        # release commit. The old release-sorting helper returned
        # release_tag_payload()["commit"] here and fails this assertion.
        identity = derive_live_prod_identity(deployments(), builds())
        self.assertEqual(identity["commit"], ACTIVE_COMMIT)
        self.assertEqual(identity["version_id"], ACTIVE_VERSION)
        self.assertNotEqual(identity["commit"], release_tag_payload()["commit"])

    def test_decision_and_rollback_use_live_identity(self):
        identity = derive_live_prod_identity(deployments(), builds())
        self.assertEqual(decide_promotion(identity["commit"], ACTIVE_COMMIT), "already-live")
        self.assertEqual(decide_promotion(identity["commit"], RELEASE_COMMIT), "promote")
        self.assertEqual(rollback_command(identity["commit"]), ["deploy", "--commit", ACTIVE_COMMIT])
        self.assertNotIn(RELEASE_COMMIT, rollback_command(identity["commit"]))

    def test_unknown_version_mapping_refuses(self):
        with self.assertRaisesRegex(LiveIdentityError, "no known build mapping"):
            derive_live_prod_identity(deployments(), builds(version_id="other" + "0" * 35))

    def test_build_without_commit_refuses(self):
        bad = {"result": {"builds": {ACTIVE_VERSION: {"build_uuid": ACTIVE_BUILD}}}}
        with self.assertRaisesRegex(LiveIdentityError, "no commit hash"):
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

    def test_empty_deployments_refuse(self):
        with self.assertRaisesRegex(LiveIdentityError, "no deployments"):
            derive_live_prod_identity({"result": {"deployments": []}}, builds())

    def test_empty_versions_refuse(self):
        with self.assertRaisesRegex(LiveIdentityError, "no versions"):
            derive_live_prod_identity({"result": {"deployments": [{"versions": []}]}}, builds())


if __name__ == "__main__":
    unittest.main()
