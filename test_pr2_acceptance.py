#!/usr/bin/env python3
"""test_pr2_acceptance.py -- acceptance suite for the PR #2 landing.

Pins the PR's landing blockers at acceptance altitude, offline with stdlib
only (no network, no credentials, no host):

  1. The `gatus module wiring gate` CI step
     (`python3 -m unittest -v test_module_wiring.py`) is red on this head:
     pushed Gatus endpoints are declared without a push script, an
     OPERATOR.md cron line, or a MODULES.md registry entry. These tests fail
     until every declared endpoint is wired or carries an owned exemption.
  2. The push-key contract (case_6d874ecd): the key a push script emits at
     run time must equal the key Gatus derives from group+name, otherwise
     every push 404s. Pinned here against regression while the repair lands.

Red before the repair, green after. Mirrors the CI wiring-gate step without
importing the gate's own test module, so this suite stays a valid witness
whatever happens to that module's expectations.
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.join(HERE, "ops", "case_658bf6b9"))
import check_module_wiring as gate  # noqa: E402

# Floor on declared push endpoints. Guards against a degenerate "green by
# deleting every declaration" repair: the gate exits 2 (never 0) when it
# measures nothing, and this floor trips long before that.
MIN_DECLARED_ENDPOINTS = 20


def declared_endpoints(root=HERE):
    """All (key, group, name) triples from ops/**/gatus-*.yaml, minus fixtures."""
    found = []
    base = os.path.join(root, "ops")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d for d in dirnames if d not in gate.SKIP_DIRS)
        for name in sorted(filenames):
            if not re.fullmatch(r"gatus-.*\.ya?ml", name):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                text = fh.read()
            for ent in gate.parse_external_endpoints(text):
                if ent["name"] and ent["group"]:
                    found.append(
                        (gate.endpoint_key(ent["group"], ent["name"]),
                         ent["group"], ent["name"]))
    return found


class WiringGateOnThisRepo(unittest.TestCase):
    """The CI wiring gate must be green on this tree."""

    @classmethod
    def setUpClass(cls):
        cls.findings, cls.report, cls.rc = gate.evaluate(HERE)

    def test_gate_exits_zero_on_this_repo(self):
        self.assertEqual(
            self.rc, 0,
            "wiring gate is red:\n"
            + "\n".join(f"  {c} {k}: {d}" for c, k, d in self.findings))

    def test_every_declared_endpoint_has_a_push_script(self):
        missing = sorted({k for c, k, _ in self.findings
                          if c == "missing-script"})
        self.assertEqual(missing, [],
                         f"endpoints with no push script: {missing}")

    def test_declared_endpoints_are_still_being_measured(self):
        endpoints = declared_endpoints()
        self.assertGreaterEqual(
            len(endpoints), MIN_DECLARED_ENDPOINTS,
            f"only {len(endpoints)} endpoints declared; the gate must keep "
            "measuring the real tree, not an emptied one")


class PushKeyContract(unittest.TestCase):
    """case_6d874ecd: pushed keys must equal Gatus-derived keys."""

    def test_derivation_matches_the_gatus_key_rule(self):
        # Gatus sanitises by trimming space, lowercasing, then mapping each
        # of / _ . , space # + & to '-'; the push key joins group and name
        # with exactly one underscore.
        self.assertEqual(
            gate.endpoint_key("Beszel", "node7525e5a1"),
            "beszel_node7525e5a1")
        self.assertEqual(
            gate.endpoint_key("beszel", "node7b1ff338"),
            "beszel_node7b1ff338")
        self.assertEqual(
            gate.endpoint_key(" g ", "a.b_c d#e+f&g,h/i"),
            "g_a-b-c-d-e-f-g-h-i")

    def test_dash_where_gatus_wants_the_separator_is_a_finding(self):
        script = "ops/case_f2010cb7/push-beszel-hosts.sh"
        finding = gate.contract_finding(
            script, 297, "beszel-node7b1ff338", "beszel", "node7b1ff338")
        self.assertIsNotNone(finding)
        self.assertIn(f"{script}:297", finding)
        self.assertIn("`beszel_node7b1ff338`", finding)

    def test_matching_pair_is_not_a_finding(self):
        self.assertIsNone(
            gate.contract_finding(
                "ops/case_f2010cb7/push-beszel-hosts.sh",
                297, "beszel_node7b1ff338", "beszel", "node7b1ff338"))


class AllowlistHygiene(unittest.TestCase):
    """Owned exemptions must be loadable, carded, and non-rotten."""

    def test_allowlist_loads_and_every_entry_names_an_owning_card(self):
        entries, err = gate.load_allowlist(
            gate.Path(HERE) / gate.ALLOWLIST_REL)
        self.assertIsNone(err, f"allowlist unreadable: {err}")
        for entry in entries:
            self.assertRegex(
                entry.get("card", ""),
                r"^(?:TOG-\d+|case_[0-9a-f]{8})$",
                f"exemption for {entry.get('key')} names no owning card")

    def test_no_stale_orphan_or_cardless_exemption(self):
        bad = sorted({c for c, _, _ in gate.evaluate(HERE)[0]
                      if c.startswith("allowlist-")})
        self.assertEqual(bad, [], f"allowlist rot: {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
