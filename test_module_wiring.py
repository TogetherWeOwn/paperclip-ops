#!/usr/bin/env python3
# ===========================================================================
# test_module_wiring.py -- hostile tests for the Gatus module wiring gate
# (case_f3500d44)
#
# Two jobs in one file.
#
#   1. THE GATE. Section 1 runs ops/case_658bf6b9/check_module_wiring.py over this
#      repo's own ops tree. A PR that declares a pushed Gatus endpoint without
#      a push script, a cron line in an OPERATOR.md, or a MODULES.md entry
#      turns this step red -- "merged" can no longer mean "declared but not
#      live".
#
#   2. THE PROOF THE GATE IS NOT VACUOUS. A gate that has only ever seen the
#      healthy repo has never shown it can fail. Sections 2-4 build a minimal
#      green tree under mktemp, then break it one way at a time (missing
#      script, missing cron, missing registry, stale allowlist, ...) and demand
#      the exact finding code and exit status. Section 5 mutates a COPY of the
#      real ops tree (drop the cliproxy cron line) so the real-config path is
#      shown red too. Section 6 pins the fail-closed exit 2 paths ("nothing was
#      measured" must never be exit 0).
#
# Tests assert exit status and finding CODES, never message text.
# Offline: stdlib only, no network, no credential, no host.
#
# Section 7 is the case_6d874ecd push-key contract: the wiring gate above proves
# a declared endpoint's key is NAMED by a push script, but it cannot prove
# the key the script PUSHES at run time equals the key Gatus derives from
# group+name. push-beszel-hosts.sh pushed `beszel-<host>` (dash) for
# `group: beszel` stanzas whose Gatus key is `beszel_<host>`, so every push
# 404d. Section 7 pins the derivation rule itself, the file:line finding
# shape, and the emit-vs-push agreement of both emit-capable scripts.
# ===========================================================================
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "ops", "case_658bf6b9", "check_module_wiring.py")

sys.path.insert(0, os.path.join(HERE, "ops", "case_658bf6b9"))
import check_module_wiring as gate  # noqa: E402

CONFIG = """\
endpoints:
  - name: pull-only
    group: core
    url: https://example.invalid/health

external-endpoints:
  - name: alpha
    group: platform
    token: "${GATUS_EXTERNAL_TOKEN}"
    heartbeat:
      interval: 10m
    alerts:
      - type: discord
        description: "alpha stale"
"""

FRAGMENT = """\
# fragment for module two
external-endpoints:
  - group: "two"
    name: 'beta-one'
    token: "${GATUS_EXTERNAL_TOKEN}"
  - name: beta-two
    group: two
    token: "${GATUS_EXTERNAL_TOKEN}"
"""

SCRIPT_ALPHA = """\
#!/usr/bin/env bash
# push-alpha.sh -- pushes platform_alpha
set -uo pipefail
gatus_push platform_alpha true
"""

# Key built at run time: only the literal prefix `two_beta-` is visible.
SCRIPT_BETA = """\
#!/usr/bin/env bash
for n in one two; do
  gatus_push "two_beta-$n" true
done
"""

OPERATOR = """\
# OPERATOR
```sh
*/5 * * * * root . /root/env; /opt/status-page/current/ops/case_658bf6b9/push-alpha.sh
```
Cron: `*/5 * * * * /opt/ops/ops/case_a96dcb35/push-beta.sh`.
"""

REGISTRY = """\
# Gatus module registry
- platform/alpha
- `two/beta-one` and `two_beta-two`
"""

ALLOWLIST_EMPTY = {"schema": gate.ALLOWLIST_SCHEMA, "entries": []}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def green_tree(root):
    write(root, "ops/case_658bf6b9/gatus-config.yaml", CONFIG)
    write(root, "ops/case_a96dcb35/gatus-endpoints.yaml", FRAGMENT)
    write(root, "ops/case_658bf6b9/push-alpha.sh", SCRIPT_ALPHA)
    write(root, "ops/case_a96dcb35/push-beta.sh", SCRIPT_BETA)
    write(root, "ops/case_658bf6b9/OPERATOR.md", OPERATOR)
    write(root, "ops/case_658bf6b9/MODULES.md", REGISTRY)
    write(root, "ops/case_658bf6b9/module_wiring_allowlist.json", json.dumps(ALLOWLIST_EMPTY))


def run_tool(root, *extra):
    p = subprocess.run([sys.executable, TOOL, "--root", root, "--json", *extra],
                       capture_output=True, text=True, timeout=60)
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        doc = {"findings": [], "report": []}
    return p.returncode, {(f["code"], f["key"]) for f in doc["findings"]}, doc


class TmpTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="case_577d9037-")
        self.addCleanup(shutil.rmtree, self.root, True)
        green_tree(self.root)

    def sub(self, rel, old, new):
        path = os.path.join(self.root, rel)
        text = read(path)
        self.assertIn(old, text, f"mutant anchor vanished in {rel}")
        write(self.root, rel, text.replace(old, new))

    def allowlist(self, entries):
        write(self.root, "ops/case_658bf6b9/module_wiring_allowlist.json",
              json.dumps({"schema": gate.ALLOWLIST_SCHEMA, "entries": entries}))

    def codes(self):
        rc, found, _ = run_tool(self.root)
        return rc, found


# ---------------------------------------------------------------------------
# 1. The gate over the real repository
# ---------------------------------------------------------------------------
class RealRepo(unittest.TestCase):
    def test_real_repo_is_fully_wired_or_exempted(self):
        p = subprocess.run([sys.executable, TOOL, "--root", HERE, "--json"],
                           capture_output=True, text=True, timeout=60)
        doc = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, json.dumps(doc["findings"], indent=2))

    def test_real_repo_allowlist_names_an_owning_card_for_every_entry(self):
        entries, err = gate.load_allowlist(
            gate.Path(HERE) / gate.ALLOWLIST_REL)
        self.assertIsNone(err)
        for e in entries:
            self.assertRegex(e["card"], r"^TOG-\d+$")

    def test_real_repo_declares_endpoints(self):
        _, _, doc = run_tool(HERE)
        self.assertGreaterEqual(len(doc["report"]), 10)


# ---------------------------------------------------------------------------
# 2. Green control and the three required wirings
# ---------------------------------------------------------------------------
class RedGreen(TmpTree):
    def test_green_tree_passes(self):
        rc, found = self.codes()
        self.assertEqual((rc, found), (0, set()))

    def test_pull_endpoints_are_not_required_to_have_a_pusher(self):
        # core_pull-only is under `endpoints:`, not `external-endpoints:`.
        rc, found = self.codes()
        self.assertNotIn(("missing-script", "core_pull-only"), found)

    def test_missing_script_fails(self):
        os.remove(os.path.join(self.root, "ops/case_658bf6b9/push-alpha.sh"))
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertIn(("missing-script", "platform_alpha"), found)
        # no script means nothing for a cron line to run
        self.assertIn(("missing-cron", "platform_alpha"), found)

    def test_key_only_in_a_comment_is_not_naming_it(self):
        self.sub("ops/case_658bf6b9/push-alpha.sh",
                 "gatus_push platform_alpha true", "# gatus_push platform_alpha true")
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertIn(("missing-script", "platform_alpha"), found)

    def test_key_as_substring_of_a_longer_key_is_not_naming_it(self):
        self.sub("ops/case_658bf6b9/push-alpha.sh", "platform_alpha true", "platform_alpha-extra true")
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertIn(("missing-script", "platform_alpha"), found)

    def test_missing_cron_line_fails(self):
        self.sub("ops/case_658bf6b9/OPERATOR.md",
                 "*/5 * * * * root . /root/env; /opt/status-page/current/ops/case_658bf6b9/push-alpha.sh",
                 "(cron line removed)")
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertEqual(found, {("missing-cron", "platform_alpha")})

    def test_commented_out_cron_line_does_not_count(self):
        self.sub("ops/case_658bf6b9/OPERATOR.md",
                 "*/5 * * * * root . /root/env; /opt/status-page/current/ops/case_658bf6b9/push-alpha.sh",
                 "# */5 * * * * root . /root/env; /opt/status-page/current/ops/case_658bf6b9/push-alpha.sh")
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("missing-cron", "platform_alpha")}))

    def test_prose_mentioning_the_script_is_not_a_cron_line(self):
        self.sub("ops/case_658bf6b9/OPERATOR.md",
                 "*/5 * * * * root . /root/env; /opt/status-page/current/ops/case_658bf6b9/push-alpha.sh",
                 "Run ops/case_658bf6b9/push-alpha.sh whenever you feel like it")
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("missing-cron", "platform_alpha")}))

    def test_cron_line_for_a_same_named_script_in_another_dir_does_not_count(self):
        self.sub("ops/case_658bf6b9/OPERATOR.md", "ops/case_658bf6b9/push-alpha.sh", "ops/case_7054ff5e/push-alpha.sh")
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("missing-cron", "platform_alpha")}))

    def test_cron_in_a_non_operator_file_does_not_count(self):
        os.remove(os.path.join(self.root, "ops/case_658bf6b9/OPERATOR.md"))
        write(self.root, "ops/case_658bf6b9/NOTES.md", OPERATOR)
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertIn(("missing-cron", "platform_alpha"), found)

    def test_missing_registry_entry_fails(self):
        self.sub("ops/case_658bf6b9/MODULES.md", "- platform/alpha\n", "")
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("missing-registry", "platform_alpha")}))

    def test_registry_entry_hidden_in_an_html_comment_does_not_count(self):
        self.sub("ops/case_658bf6b9/MODULES.md", "- platform/alpha\n", "<!-- platform/alpha -->\n")
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("missing-registry", "platform_alpha")}))

    def test_registry_prefix_match_does_not_count(self):
        self.sub("ops/case_658bf6b9/MODULES.md", "- platform/alpha\n", "- platform/alpha-beta\n")
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("missing-registry", "platform_alpha")}))

    def test_registry_accepts_key_form_as_well_as_group_slash_name(self):
        self.sub("ops/case_658bf6b9/MODULES.md", "- platform/alpha\n", "- `platform_alpha`\n")
        self.assertEqual(self.codes(), (0, set()))


# ---------------------------------------------------------------------------
# 3. Run-time-built keys, parsing, discovery
# ---------------------------------------------------------------------------
class Naming(TmpTree):
    def test_runtime_built_key_is_matched_by_its_literal_prefix(self):
        # green tree already relies on this for two_beta-one / two_beta-two
        self.assertEqual(self.codes(), (0, set()))

    def test_runtime_prefix_must_reach_past_the_group(self):
        self.sub("ops/case_a96dcb35/push-beta.sh", '"two_beta-$n"', '"two_$n"')
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertEqual({k for c, k in found if c == "missing-script"},
                         {"two_beta-one", "two_beta-two"})

    def test_runtime_prefix_of_another_family_does_not_match(self):
        self.sub("ops/case_a96dcb35/push-beta.sh", '"two_beta-$n"', '"two_gamma-$n"')
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertIn(("missing-script", "two_beta-one"), found)

    def test_group_before_name_and_quoted_scalars_parse(self):
        got = gate.parse_external_endpoints(FRAGMENT)
        self.assertEqual([(e["group"], e["name"]) for e in got],
                         [("two", "beta-one"), ("two", "beta-two")])

    def test_alert_and_heartbeat_keys_do_not_leak_into_endpoint_fields(self):
        got = gate.parse_external_endpoints(
            "external-endpoints:\n  - name: a\n    group: g\n    alerts:\n"
            "      - type: x\n        name: not-an-endpoint\n        group: nor-this\n")
        self.assertEqual([(e["group"], e["name"]) for e in got], [("g", "a")])

    def test_pull_endpoints_block_is_ignored(self):
        got = gate.parse_external_endpoints(CONFIG)
        self.assertEqual([(e["group"], e["name"]) for e in got], [("platform", "alpha")])

    def test_key_sanitisation_matches_gatus(self):
        self.assertEqual(gate.endpoint_key("CLIProxy", "Lane_Open code/x.y"), "cliproxy_lane-open-code-x-y")

    def test_endpoint_without_group_is_malformed(self):
        write(self.root, "ops/case_a96dcb35/gatus-endpoints.yaml",
              "external-endpoints:\n  - name: orphan\n    token: x\n")
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertTrue(any(c == "malformed-endpoint" for c, _ in found))

    def test_fragment_in_a_fixtures_dir_is_not_scanned(self):
        write(self.root, "ops/case_658bf6b9/fixtures/gatus-sample.yaml",
              "external-endpoints:\n  - name: ghost\n    group: fx\n")
        self.assertEqual(self.codes(), (0, set()))

    def test_new_fragment_without_wiring_fails_on_all_three(self):
        write(self.root, "ops/case_8d310190/gatus-endpoints.yaml",
              "external-endpoints:\n  - name: gamma\n    group: new\n")
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertEqual(found, {("missing-script", "new_gamma"),
                                 ("missing-cron", "new_gamma"),
                                 ("missing-registry", "new_gamma")})


# ---------------------------------------------------------------------------
# 4. Allowlist: explicit, owned, and unable to rot
# ---------------------------------------------------------------------------
class Allowlist(TmpTree):
    def gap(self):
        os.remove(os.path.join(self.root, "ops/case_658bf6b9/push-alpha.sh"))

    def test_owned_exemption_turns_a_known_gap_green(self):
        self.gap()
        self.allowlist([{"key": "platform_alpha", "exempt": ["script", "cron"], "card": "case_8c5fe4cc"}])
        rc, found, doc = run_tool(self.root)
        self.assertEqual((rc, found), (0, set()))
        self.assertTrue(any(l.startswith("ALLOWED platform_alpha") for l in doc["report"]))

    def test_exemption_covers_only_the_checks_it_names(self):
        self.gap()
        self.allowlist([{"key": "platform_alpha", "exempt": ["script"], "card": "case_8c5fe4cc"}])
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("missing-cron", "platform_alpha")}))

    def test_stale_exemption_fails_once_the_check_is_compliant(self):
        self.allowlist([{"key": "platform_alpha", "exempt": ["script", "cron"], "card": "case_8c5fe4cc"}])
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertEqual(found, {("allowlist-stale", "platform_alpha")})

    def test_partially_stale_exemption_fails(self):
        self.gap()
        # cron exemption is fine (still failing); registry exemption is stale
        self.allowlist([{"key": "platform_alpha", "exempt": ["script", "cron", "registry"], "card": "case_8c5fe4cc"}])
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("allowlist-stale", "platform_alpha")}))

    def test_orphan_entry_for_an_undeclared_endpoint_fails(self):
        self.allowlist([{"key": "platform_ghost", "exempt": ["cron"], "card": "case_8c5fe4cc"}])
        rc, found = self.codes()
        self.assertEqual((rc, found), (1, {("allowlist-orphan", "platform_ghost")}))

    def test_entry_without_an_owning_card_fails(self):
        self.gap()
        for bad in (None, "", "someone", "TOG-", "case_de259302", 15316):
            entry = {"key": "platform_alpha", "exempt": ["script", "cron"]}
            if bad is not None:
                entry["card"] = bad
            self.allowlist([entry])
            rc, found = self.codes()
            self.assertEqual(rc, 1, f"card={bad!r}")
            self.assertIn(("allowlist-no-card", "platform_alpha"), found, f"card={bad!r}")

    def test_entry_with_unknown_or_empty_exempt_fails(self):
        for bad in ([], ["scripts"], "script", None):
            entry = {"key": "platform_alpha", "card": "case_8c5fe4cc"}
            if bad is not None:
                entry["exempt"] = bad
            self.allowlist([entry])
            rc, found = self.codes()
            self.assertEqual(rc, 1, f"exempt={bad!r}")
            self.assertIn(("allowlist-bad-exempt", "platform_alpha"), found, f"exempt={bad!r}")

    def test_duplicate_entries_fail(self):
        self.gap()
        e = {"key": "platform_alpha", "exempt": ["script", "cron"], "card": "case_8c5fe4cc"}
        self.allowlist([e, e])
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertIn(("allowlist-duplicate", "platform_alpha"), found)

    def test_exempting_one_endpoint_does_not_exempt_a_sibling(self):
        write(self.root, "ops/case_8d310190/gatus-endpoints.yaml",
              "external-endpoints:\n  - name: gamma\n    group: new\n  - name: delta\n    group: new\n")
        self.allowlist([{"key": "new_gamma", "exempt": ["script", "cron", "registry"], "card": "case_8c5fe4cc"}])
        rc, found = self.codes()
        self.assertEqual(rc, 1)
        self.assertEqual({k for _, k in found}, {"new_delta"})


# ---------------------------------------------------------------------------
# 5. Positive control on a COPY of the real ops tree
# ---------------------------------------------------------------------------
class RealConfigMutants(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="case_577d9037-real-")
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(os.path.join(HERE, "ops"), os.path.join(self.root, "ops"))

    def mutate(self, rel, old, new):
        path = os.path.join(self.root, rel)
        text = read(path)
        self.assertIn(old, text, f"mutant anchor vanished in {rel}")
        write(self.root, rel, text.replace(old, new))

    def test_copy_is_green_before_mutation(self):
        rc, found, _ = run_tool(self.root)
        self.assertEqual((rc, found), (0, set()))

    def test_dropping_the_cliproxy_cron_line_turns_all_four_lanes_red(self):
        self.mutate("ops/case_658bf6b9/OPERATOR.md",
                    "/opt/status-page/current/ops/case_658bf6b9/push-cliproxy-lanes.sh",
                    "(removed)")
        rc, found, _ = run_tool(self.root)
        self.assertEqual(rc, 1)
        self.assertEqual(found, {("missing-cron", f"cliproxy_lane-{n}")
                                 for n in ("claude", "codex", "zai", "opencode-go")})

    def test_deleting_a_registry_entry_turns_that_endpoint_red(self):
        reg = os.path.join(self.root, "ops/case_658bf6b9/MODULES.md")
        text = read(reg)
        self.assertIn("`platform/oom-kills`", text)
        write(self.root, "ops/case_658bf6b9/MODULES.md",
              text.replace("`platform/oom-kills`", "`platform/oom-killz`"))
        rc, found, _ = run_tool(self.root)
        self.assertEqual((rc, found), (1, {("missing-registry", "platform_oom-kills")}))

    def test_renaming_the_key_a_script_pushes_turns_that_endpoint_red(self):
        self.mutate("ops/case_658bf6b9/push-oom-kills.sh", "platform_oom-kills", "platform_oom-killz")
        rc, found, _ = run_tool(self.root)
        self.assertEqual(rc, 1)
        self.assertIn(("missing-script", "platform_oom-kills"), found)

    def test_delivering_a_pusher_for_an_exempt_endpoint_makes_the_exemption_stale(self):
        # Recreate the pre-delivery gap only in the copy. The real exemption
        # must disappear when the pusher lands, without invalidating this proof.
        os.remove(os.path.join(self.root, "ops/case_658bf6b9/push-discord-bot.sh"))
        allowlist_rel = "ops/case_658bf6b9/module_wiring_allowlist.json"
        doc = json.loads(read(os.path.join(self.root, allowlist_rel)))
        doc["entries"].append({"key": "platform_discord-bot",
                               "exempt": ["script", "cron"], "card": "case_a9fb7f52"})
        write(self.root, allowlist_rel, json.dumps(doc))
        rc, found, _ = run_tool(self.root)
        self.assertEqual((rc, found), (0, set()))
        write(self.root, "ops/case_658bf6b9/push-discord-bot.sh",
              "#!/usr/bin/env bash\ngatus_push platform_discord-bot true\n")
        rc, found, _ = run_tool(self.root)
        self.assertEqual(rc, 1)
        self.assertIn(("allowlist-stale", "platform_discord-bot"), found)


# ---------------------------------------------------------------------------
# 6. Fail-closed: nothing measured is never a pass
# ---------------------------------------------------------------------------
class FailClosed(TmpTree):
    def test_no_external_endpoints_anywhere_is_exit_2(self):
        for rel in ("ops/case_658bf6b9/gatus-config.yaml", "ops/case_a96dcb35/gatus-endpoints.yaml"):
            os.remove(os.path.join(self.root, rel))
        rc, found = self.codes()
        self.assertEqual(rc, 2)
        self.assertIn(("nothing-measured", "-"), found)

    def test_no_ops_tree_is_exit_2(self):
        shutil.rmtree(os.path.join(self.root, "ops"))
        self.assertEqual(self.codes()[0], 2)

    def test_missing_registry_is_exit_2(self):
        os.remove(os.path.join(self.root, "ops/case_658bf6b9/MODULES.md"))
        self.assertEqual(self.codes()[0], 2)

    def test_corrupt_allowlist_is_exit_2(self):
        write(self.root, "ops/case_658bf6b9/module_wiring_allowlist.json", "{not json")
        rc, found = self.codes()
        self.assertEqual(rc, 2)
        self.assertTrue(any(c == "allowlist-invalid" for c, _ in found))

    def test_wrong_schema_allowlist_is_exit_2(self):
        write(self.root, "ops/case_658bf6b9/module_wiring_allowlist.json",
              json.dumps({"schema": "other", "entries": []}))
        self.assertEqual(self.codes()[0], 2)

    def test_absent_allowlist_file_is_an_empty_allowlist(self):
        os.remove(os.path.join(self.root, "ops/case_658bf6b9/module_wiring_allowlist.json"))
        self.assertEqual(self.codes(), (0, set()))

    def test_registry_and_allowlist_flags_override_the_defaults(self):
        reg = os.path.join(self.root, "elsewhere.md")
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write("- platform/alpha\n")  # beta endpoints deliberately absent
        rc, found, _ = run_tool(self.root, "--registry", reg)
        self.assertEqual(rc, 1)
        self.assertEqual({k for _, k in found}, {"two_beta-one", "two_beta-two"})

    def test_human_output_ends_with_a_verdict_line(self):
        p = subprocess.run([sys.executable, TOOL, "--root", self.root],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.strip().endswith("PASS"))


# ---------------------------------------------------------------------------
# 7. Push-key contract (case_6d874ecd)
# ---------------------------------------------------------------------------
class PushKeyRule(unittest.TestCase):
    def test_derived_keys_for_the_beszel_hosts(self):
        self.assertEqual(gate.endpoint_key("beszel", "node7b1ff338"), "beszel_node7b1ff338")
        self.assertEqual(gate.endpoint_key("beszel", "ovh-1"), "beszel_ovh-1")

    def test_derived_keys_for_controller_tmp_disk(self):
        self.assertEqual(gate.endpoint_key("controller", "tmp-disk-warn"),
                         "controller_tmp-disk-warn")
        self.assertEqual(gate.endpoint_key("controller", "tmp-disk"),
                         "controller_tmp-disk")

    def test_derivation_matches_gatus_key_go(self):
        # Gatus v5.37.0 config/key/key.go: sanitize trims space, lowercases,
        # then maps each of / _ . , space # + & to '-'; the key joins the two
        # halves with one underscore.
        self.assertEqual(gate.endpoint_key("Beszel", "node7525e5a1"), "beszel_node7b1ff338")
        self.assertEqual(gate.endpoint_key(" g ", "a.b_c d#e+f&g,h/i"),
                         "g_a-b-c-d-e-f-g-h-i")

    def test_derived_key_has_exactly_one_separator(self):
        for group, name in [("beszel", "node7b1ff338"), ("controller", "tmp-disk"),
                            ("CLIProxy", "Lane_Open code/x.y"), ("a_b", "c_d")]:
            self.assertEqual(gate.endpoint_key(group, name).count("_"), 1)


class PushKeyContractFinding(unittest.TestCase):
    SCRIPT = "ops/case_f2010cb7/push-beszel-hosts.sh"

    def test_matching_pair_passes(self):
        self.assertIsNone(
            gate.contract_finding(self.SCRIPT, 297, "beszel_node7b1ff338", "beszel", "node7b1ff338"))

    def test_dash_key_fails_naming_script_line_and_expected_key(self):
        # The case_6d874ecd bug shape: dash where Gatus wants the underscore.
        finding = gate.contract_finding(self.SCRIPT, 297, "beszel-node7b1ff338", "beszel", "node7b1ff338")
        self.assertIn(f"{self.SCRIPT}:297", finding)
        self.assertIn("`beszel_node7b1ff338`", finding)

    def test_mutating_one_registry_key_turns_it_red(self):
        finding = gate.contract_finding(self.SCRIPT, 297, "beszel_node7b1ff338", "beszel", "node7b1ff338-evil")
        self.assertIsNotNone(finding)
        self.assertIn("`beszel_node7b1ff338-evil`", finding)

    def test_dynamic_key_without_a_call_site_still_names_the_script(self):
        finding = gate.contract_finding(self.SCRIPT, 0, "beszel-node7b1ff338", "beszel", "node7b1ff338")
        self.assertIn(self.SCRIPT, finding)
        self.assertIn("`beszel_node7b1ff338`", finding)


class PushKeyEmitAgreement(unittest.TestCase):
    """End to end but still offline: emit each script's stanzas, observe the
    keys it really pushes (stub curl / --dry-run: no network, no creds),
    and demand equality through the canonical derivation."""

    BESZEL = "ops/case_f2010cb7/push-beszel-hosts.sh"
    TMPDISK = "ops/case_0ddff3ec/tmp-disk-alarm.sh"

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="case_579f493e-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def emit_pairs(self, script_rel, *args):
        p = subprocess.run(["bash", os.path.join(HERE, script_rel),
                            "--emit-gatus-yaml", *args],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        # Emit prints bare stanzas: the operator appends them under an
        # `external-endpoints:` header, so the parse sees that header here.
        return [(e["group"], e["name"]) for e in gate.parse_external_endpoints(
            "external-endpoints:\n" + p.stdout)]

    def pushed_via_stub_curl(self, script_rel, payload, extra_env=None):
        bindir = os.path.join(self.work, "bin")
        os.makedirs(bindir, exist_ok=True)
        log = os.path.join(self.work, "cfg.log")
        with open(os.path.join(bindir, "curl"), "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\n"
                     "for ((i=1; i<=$#; i++)); do\n"
                     '  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cat "${!j}" >> "$CFG_LOG"; fi\n'
                     "done\nprintf '200'\nexit 0\n")
        os.chmod(os.path.join(bindir, "curl"), 0o755)
        env = dict(os.environ,
                   PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                   CFG_LOG=log,
                   GATUS_URL="http://127.0.0.1:9",
                   GATUS_EXTERNAL_TOKEN="test-token-not-a-secret")
        # The Paperclip container's BASH_ENV startup file hard-resets PATH
        # in every child bash, which would drop the stub-curl bindir. CI
        # runners have no BASH_ENV; drop it here so the stub wins there too.
        env.pop("BASH_ENV", None)
        if extra_env:
            env.update(extra_env)
        open(log, "w", encoding="utf-8").close()
        p = subprocess.run(["bash", os.path.join(HERE, script_rel), payload],
                           capture_output=True, text=True, timeout=120, env=env)
        self.assertIn(p.returncode, (0, 1), p.stderr[-2000:])
        with open(log, encoding="utf-8") as fh:
            return sorted(set(re.findall(r"endpoints/([A-Za-z0-9_-]+)/external", fh.read())))

    def check_agreement(self, script_rel, pairs, pushed):
        expected = sorted(gate.endpoint_key(g, n) for g, n in pairs)
        self.assertEqual(pushed, expected)
        for (g, n), key in zip(sorted(pairs), expected):
            self.assertIsNone(gate.contract_finding(script_rel, 0, key, g, n))

    def test_beszel_emit_and_push_agree(self):
        fix = os.path.join(HERE, "ops", "case_f2010cb7", "fixtures", "healthy.json")
        pairs = self.emit_pairs(self.BESZEL, fix)
        self.assertEqual(pairs, [("beszel", "node7b1ff338"), ("beszel", "ovh-1")])
        self.check_agreement(self.BESZEL, pairs,
                             self.pushed_via_stub_curl(self.BESZEL, fix))

    def test_beszel_key_mirrors_gatus_sanitisation(self):
        host = {"name": "Web.DB_01", "disk_used_percent": 10.0,
                "inode_used_percent": 10.0, "cpu_used_percent": 10.0,
                "mem_used_percent": 10.0, "load15_per_cpu": 0.1,
                "failed_units": 0, "container_restarts_24h": 0}
        payload = os.path.join(self.work, "hosts.json")
        with open(payload, "w", encoding="utf-8") as fh:
            json.dump([host], fh)
        pairs = self.emit_pairs(self.BESZEL, payload)
        # Emit names the SANITIZED host (raw `Web.DB_01` stays in the
        # description text); the derived key must match the pushed one.
        self.assertEqual(pairs, [("beszel", "web-db-01")])
        pushed = self.pushed_via_stub_curl(self.BESZEL, payload)
        self.assertEqual(pushed, ["beszel_web-db-01"])
        self.check_agreement(self.BESZEL, pairs, pushed)

    def test_beszel_hostile_names_emit_and_push_agree(self):
        # Paperclip Review on PR #809: sanitize_key mapped only Gatus's 8
        # separators, so `Hetzner (main)`, `db:1`, `[edge]`, `*alias` and
        # non-ASCII names were refused by gatus_push (exit 1) and emitted
        # raw into unquoted `name:` lines. The catch-all `[^a-z0-9-] -> -`
        # step must keep every host pushable under the key Gatus derives
        # from the emitted stanza.
        names = ["node7b1ff338", "Hetzner (main)", "db:1", "[edge]", "*alias",
                 "Ünïcode-1"]
        with open(os.path.join(HERE, "ops", "case_f2010cb7", "fixtures",
                               "healthy.json"), encoding="utf-8") as fh:
            template = json.load(fh)[0]
        payload = os.path.join(self.work, "hostile.json")
        with open(payload, "w", encoding="utf-8") as fh:
            json.dump([dict(template, name=n) for n in names], fh)
        pairs = self.emit_pairs(self.BESZEL, payload)
        self.assertEqual(len(pairs), len(names))
        for group, name in pairs:
            self.assertEqual(group, "beszel")
            self.assertRegex(name, r"\A[a-z0-9-]+\Z")
        pushed = self.pushed_via_stub_curl(self.BESZEL, payload)
        self.assertEqual(len(pushed), len(names))
        for key in pushed:
            self.assertRegex(key, r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")
        self.check_agreement(self.BESZEL, pairs, pushed)

    def test_beszel_silent_host_uses_the_derived_key(self):
        fix = os.path.join(HERE, "ops", "case_f2010cb7", "fixtures", "healthy.json")
        pushed = self.pushed_via_stub_curl(self.BESZEL, fix,
                                           {"BESZEL_HOSTS": "node7b1ff338 ghost"})
        self.assertIn("beszel_ghost", pushed)

    def test_tmp_disk_emit_and_push_agree(self):
        pairs = self.emit_pairs(self.TMPDISK)
        self.assertEqual(pairs, [("controller", "tmp-disk-warn"),
                                 ("controller", "tmp-disk")])
        p = subprocess.run(["bash", os.path.join(HERE, self.TMPDISK), "--dry-run"],
                           capture_output=True, text=True, timeout=60)
        keys = sorted(set(re.findall(r"^  ([A-Za-z0-9_-]+):", p.stdout, re.M)))
        self.assertEqual(keys, ["controller_tmp-disk", "controller_tmp-disk-warn"])
        self.check_agreement(self.TMPDISK, pairs, keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
