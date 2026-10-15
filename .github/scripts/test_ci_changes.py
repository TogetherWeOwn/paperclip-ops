#!/usr/bin/env python3
"""Unit tests for ci_changes.py (case_d5bc7950). Offline, stdlib only.

Run: python3 .github/scripts/test_ci_changes.py
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_changes as cc  # noqa: E402


WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "workflows", "ci.yml")
RUN_STEP = re.compile(
    r"^\s+run: (?:(?:python3|node --test|node|bash) )?\.?/?"
    r"([A-Za-z0-9_./-]+\.(?:sh|py|mjs|cjs))(?:\s|$)")


def core_area_scripts():
    """Scripts named by single-line `run:` steps of the suites job's core area.

    case_a2b959d4 consolidated offline-suites into suites; only the core-area
    block is in scope here (other areas' scripts classify their own area).
    """
    with open(WORKFLOW) as fh:
        lines = fh.read().splitlines()
    start = next(i for i, line in enumerate(lines) if "== area: core" in line)
    end = next(i for i in range(start + 1, len(lines))
               if "== area:" in lines[i] or re.match(r"^  [a-z0-9-]+:\s*$", lines[i]))
    found = set()
    for line in lines[start:end]:
        m = RUN_STEP.match(line)
        if m:
            found.add(m.group(1))
    return found


class Classify(unittest.TestCase):
    def result(self, *paths):
        return cc.classify(list(paths))

    def test_deploy_window_wrapper_changes_wake_core(self):
        for path in ("deploy_window_open.sh", "test_deploy_window_open.sh"):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertTrue(r["core"])
                self.assertFalse(r["full"])

    def test_docs_only_runs_nothing(self):
        r = self.result("docs/runbooks/foo.md", "CONTRIBUTING.md")
        self.assertFalse(r["full"])
        self.assertFalse(any(r[a] for a in cc.AREAS))

    def test_readme_wakes_only_core(self):
        # test_readme_script_index.sh parses the README "What is here" table,
        # so a README-only edit must wake offline-suites (core) and nothing else.
        r = self.result("README.md")
        self.assertFalse(r["full"])
        self.assertEqual({a for a in cc.AREAS if r[a]}, {"core"})

    def test_acceptance_transport_changes_wake_core(self):
        for path in ("acceptance_transport.sh", "test_acceptance_transport.sh"):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertTrue(r["core"])
                self.assertFalse(r["full"])

    def test_upstream_maintainer_memory_wakes_core_and_upstream(self):
        # case_214e61d3: a memory-only PR (ingested maintainer/Greptile feedback)
        # must not latch a full run; the lint suites in core read these files.
        # case_1d1195e1: it also wakes the upstream area, so the upstream-gated
        # steps (which execute those suites) run on a memory-only PR.
        r = self.result("upstream-memory/paperclipai__paperclip.json")
        self.assertEqual({a for a in cc.AREAS if r[a]}, {"core", "upstream"})
        self.assertFalse(r["full"])

    def test_upstream_area(self):
        # case_1d1195e1: the upstream-pipeline family wakes core (its guards run
        # in the suites core block) AND upstream (its own gated steps), never
        # full. A non-upstream PR therefore skips the 26-min case_214e61d3
        # mutation gate; an upstream PR still runs its core guards.
        for path in (
            "upstream_gate.sh",
            "upstream_lint.py",
            "upstream_reputation.py",
            "upstream_prepush_hook.sh",
            "upstream_watch.sh",
            "upstream_filing_readiness.sh",
            "upstream_draft_pin_audit.sh",
            "upstream_draft_pins.txt",
            "test_upstream_gate.sh",
            "test_upstream_lint.py",
            "test_upstream_reputation.py",
            "test_upstream_filing_readiness.sh",
            "test_upstream_draft_pin_audit.sh",
            "verification/case_214e61d3-mutation-gate.py",
            "verification/case_28030601-mutation-gate.sh",
            "verification/case_ff1fbbce-pin-conformance-mutants.sh",
            "verification/case_4d10ac5e-filing-strip-mutation-gate.sh",
            "verification/upstream-bundle-filing-gate.sh",
            "verification/upstream-bundle-filing-strip-gate.sh",
            "upstream-memory/paperclipai__paperclip.json",
            "docs/upstream/README.md",
        ):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertEqual({a for a in cc.AREAS if r[a]},
                                 {"core", "upstream"})
                self.assertFalse(r["full"])

    def test_upstream_procedure_docs_wake_suites(self):
        # case_1d1195e1: docs/upstream/ is procedure, not prose: the filing-strip
        # gate scores those files, so editing them must wake the suites that
        # guard them (unlike docs/ prose, which wakes nothing).
        r = self.result("docs/upstream/reputation-pipeline.md")
        self.assertEqual({a for a in cc.AREAS if r[a]}, {"core", "upstream"})
        self.assertFalse(r["full"])

    def test_upstream_steps_are_upstream_gated_in_ci(self):
        # case_1d1195e1: the upstream-pipeline family (suites plus the mutation
        # gates that prove them, incl. the 26-min case_214e61d3 gate) runs only
        # when upstream or full changed -- never on an unrelated core PR.
        with open(WORKFLOW) as fh:
            text = fh.read()
        suites = text.split("  suites:\n", 1)[1].split(
            "  private-mutation-gates:\n", 1)[0]
        for name in (
            "upstream gate suite",
            "upstream lint and reputation suites",
            "upstream pipeline suites are not vacuous (mutation-gated)",
            "upstream readiness suite is not vacuous (mutation-gated)",
            "pinned upstream blobs are filable, and sendable blobs are pinned (mutation-gated)",
            "upstream bundle filing-strip gate",
            "filing-strip gate is not vacuous (mutation-gated)",
        ):
            with self.subTest(step=name):
                m = re.search(
                    r"(?m)^      - name: " + re.escape(name) + r"\n"
                    r"        if: (.+?)\n", suites)
                self.assertIsNotNone(m, f"step {name!r} must carry an if:")
                self.assertEqual(
                    m.group(1),
                    "needs.changes.outputs.full == 'true' || "
                    "needs.changes.outputs.upstream == 'true'")

    def test_suites_job_tests_upstream(self):
        # case_1d1195e1: the suites job-level if must include upstream, or an
        # upstream-only PR (core also true via ALSO_CORE, but a future
        # classifier trim could leave upstream alone) would skip the whole
        # job while ci-ok reports success.
        with open(WORKFLOW) as fh:
            text = fh.read()
        m = re.search(r"(?m)^  suites:\n    needs: \[changes\]\n    if: (.+?)\n",
                      text)
        self.assertIsNotNone(m, "suites job must carry a job-level if:")
        self.assertIn("needs.changes.outputs.upstream == 'true'", m.group(1))

    def test_health_module_inputs_wake_only_core(self):
        for path in (
            "ops/case_73259b15/coolify_restart_signal.sh",
            "ops/case_73259b15/render-coolify-endpoints.sh",
            "ops/case_73259b15/test_coolify_app_health.sh",
            "ops/case_73259b15/test_coolify_wiring.py",
            "ops/case_73259b15/fixtures/healthy.json",
            "ops/case_73259b15/README.md",
        ):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertFalse(r["full"])
                self.assertEqual({a for a in cc.AREAS if r[a]}, {"core"})

    def test_review_bakeoff_inputs_wake_only_core(self):
        for path in (
            "eval/review-bakeoff/test_batch.py",
            "eval/review-bakeoff/test_build_cases.py",
            "eval/review-bakeoff/test_run_review.py",
            "eval/review-bakeoff/test_score.py",
            "eval/review-bakeoff/test_szz_pairs.py",
            "eval/review-bakeoff/szz_pairs.py",
            "eval/review-bakeoff/dataset/cases.json",
            "eval/review-bakeoff/review_prompt.md",
            "eval/review-bakeoff/RESULTS-r1.md",
        ):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertFalse(r["full"])
                self.assertEqual({a for a in cc.AREAS if r[a]}, {"core"})

    def test_review_bakeoff_unittest_suites_run_in_ci(self):
        with open(WORKFLOW) as fh:
            text = fh.read()
        suites = text.split("  suites:\n", 1)[1].split(
            "  private-mutation-gates:\n", 1)[0]
        step = (
            "      - name: reviewer bake-off unittest suites\n"
            "        if: needs.changes.outputs.full == 'true' || "
            "needs.changes.outputs.core == 'true'\n"
            "        working-directory: eval/review-bakeoff\n"
            "        run: python3 -B -m unittest -v test_batch test_build_cases "
            "test_run_review test_score test_szz_pairs\n"
        )
        self.assertIn(step, suites)

    def test_executable_health_runbook_wakes_only_core(self):
        r = self.result("docs/runbooks/coolify-app-health.md")
        self.assertFalse(r["full"])
        self.assertEqual({a for a in cc.AREAS if r[a]}, {"core"})

    def test_runbook_classification_wakes_runbook(self):
        # Paperclip Review on PR #899 (case_1292729c): operator_runbook.sh
        # render is a pure function of the classification file, so a
        # classification-only edit must wake the runbook suite (which
        # re-renders and diffs). Otherwise the §11 drift gate never runs
        # for exactly the edit it exists to catch.
        r = self.result("operator_runbook_classification.json")
        self.assertTrue(r["runbook"])
        self.assertFalse(r["full"])

    def test_lib_moved_into_docs_runs_core(self):
        # --no-renames supplies both paths; the deleted shared code runs full.
        r = self.result("lib/x.py", "docs/x.md")
        self.assertTrue(r["core"])
        self.assertTrue(r["full"])

    def test_plugin_moved_into_docs_runs_its_suite(self):
        r = self.result("plugins/dispatch/src/x.ts", "docs/x.md")
        self.assertTrue(r["dispatch"])
        self.assertFalse(r["full"])
        self.assertFalse(r["core"])

    def test_root_txt_is_suite_input_not_prose(self):
        # quota_brake_exempt.txt etc. gate suites; must wake offline.
        r = self.result("quota_brake_exempt.txt")
        self.assertTrue(r["core"])
        self.assertFalse(r["full"])

    def test_plugin_change_wakes_only_its_suite(self):
        r = self.result("plugins/dispatch/src/x.ts")
        self.assertTrue(r["dispatch"])
        self.assertFalse(r["full"])
        self.assertFalse(r["core"])
        for a in cc.AREAS:
            if a != "dispatch":
                self.assertFalse(r[a], a)

    def test_moved_plugin_path_fails_closed(self):
        # C6 cutover: no area claims plugins/model-selection/ anymore. A touch
        # there latches full (and core) so nothing is skipped; the
        # model-selection-moved guard step (secret-scan job) reds the PR.
        for path in ("plugins/model-selection/scripts/mutation-gate.mjs",
                     "plugins/model-selection/src/manifest.ts",
                     "plugins/model-selection/src/capacity-ledger.ts"):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertNotIn("selection", r)
                self.assertTrue(r["full"])
                self.assertTrue(r["core"])

    def test_notify_suites_wake_only_core(self):
        # Their generic mutation gates moved to the public toolkit's CI; here
        # they are plain offline-suites steps.
        r = self.result("test_leader_notify.sh", "test_decision_notify.sh")
        self.assertEqual({a for a in cc.AREAS if r[a]}, {"core"})
        self.assertFalse(r["full"])

    def test_private_mutation_inputs_wake_both_gates(self):
        expected = {
            "org_request_queue.sh": {"core", "privilege", "private_mutation"},
            "notify_paperclip_issue.sh": {"core", "private_mutation"},
            "test_request_record_integrity.sh": {"core", "private_mutation"},
            "test_notify_transport.sh": {"core", "private_mutation"},
            "test_agent_endpoint_preflight.sh": {"core", "private_mutation"},
            "test_private_mutation_gates.py": {"core", "private_mutation"},
            "verification/private-mutation-gates.sh": {
                "core", "private_mutation"},
            # case_1d1195e1 review: the toolkit pin files restore four
            # upstream-gated subjects, so a pin bump must wake upstream too.
            "scripts/sync-toolkit.sh": {"core", "private_mutation",
                                        "upstream"},
            "vendor/toolkit-files.txt": {"core", "private_mutation",
                                         "upstream"},
            "vendor/toolkit-pins.txt": {"core", "private_mutation",
                                        "upstream"},
        }
        for path, areas in expected.items():
            with self.subTest(path=path):
                r = self.result(path)
                self.assertEqual({a for a in cc.AREAS if r[a]}, areas)
                self.assertFalse(r["full"])

        # This source is public-owned and restored from the pinned toolkit.
        r = self.result("agent_endpoint_preflight.sh")
        self.assertEqual({a for a in cc.AREAS if r[a]}, {"core"})
        self.assertFalse(r["private_mutation"])

    def test_private_mutation_harness_runs_in_ci(self):
        with open(WORKFLOW) as fh:
            text = fh.read()
        suites = text.split("  suites:\n", 1)[1].split(
            "  private-mutation-gates:\n", 1)[0]
        step = (
            "      - name: private mutation harness suite\n"
            "        if: needs.changes.outputs.full == 'true' || "
            "needs.changes.outputs.core == 'true'\n"
            "        run: python3 -m unittest -v test_private_mutation_gates.py\n"
        )
        self.assertTrue(step in suites, "suites must run the fast private mutation harness")

    def test_privilege_area(self):
        # Suites only privilege-suites runs: no offline-suites step touches them.
        r = self.result("test_orgdb_fixture.sh", "schema_drift.sh")
        self.assertTrue(r["privilege"])
        self.assertFalse(r["full"])
        self.assertFalse(r["core"])

    def test_privilege_subjects_wake_privilege(self):
        # CISO F1 (case_f11bc226): the suites' enforcement subjects. A
        # ceiling-weakening edit to either must wake privilege-suites.
        r = self.result("org_provisioner.sh")
        self.assertTrue(r["privilege"])
        self.assertFalse(r["full"])
        r = self.result("org_request_queue.sh")
        self.assertTrue(r["privilege"])
        self.assertFalse(r["full"])

    def test_capture_area(self):
        r = self.result("gh-event-capture/src/app.js")
        self.assertTrue(r["capture"])
        self.assertFalse(r["capture"] is False)
        self.assertFalse(r["full"])
        self.assertFalse(r["core"])

    def test_mcp_and_hostops_share_paths(self):
        r = self.result("mcp/host-ops-mcp.mjs")
        self.assertTrue(r["mcp"])
        self.assertTrue(r["hostops"])
        self.assertFalse(r["full"])

    def test_host_ops_broker_change_wakes_offline_suites(self):
        # case_82df1671: the PR #445 file set classified core=false, so the
        # broker unit suite and the installer contract suite (offline-suites
        # steps) skipped while ci-ok reported success.
        r = self.result("host-ops/RUNBOOK.md", "host-ops/broker.py",
                        "mcp/host-ops-mcp.mjs", "test_host_ops_broker.py")
        self.assertTrue(r["core"])
        self.assertTrue(r["hostops"])
        self.assertTrue(r["mcp"])
        self.assertFalse(r["full"])

    def test_host_ops_inputs_each_wake_offline_and_hostops(self):
        for path in ("host-ops/broker.py", "host-ops/install.sh",
                     "host-ops/install-lib.sh", "host-ops/host-ops-broker.sudoers",
                     "host-ops/RUNBOOK.md", "test_host_ops_broker.py",
                     "test_host_ops_install.sh"):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertEqual({a for a in cc.AREAS if r[a]}, {"core", "hostops"})
                self.assertFalse(r["full"])

    def test_host_ops_transport_alone_stays_out_of_offline(self):
        # The node suites cover mcp/*.mjs; only mcp/deploy/ is offline surface.
        r = self.result("mcp/host-ops-mcp.mjs", "mcp/test/host-ops-mcp.test.mjs")
        self.assertEqual({a for a in cc.AREAS if r[a]}, {"mcp", "hostops"})

    def test_specialist_paths_that_offline_also_runs_wake_core(self):
        # path -> the specialist areas that must stay set beside core.
        expect = {
            "mcp/deploy/acceptance_live.sh": {"mcp", "hostops"},
            "mcp/deploy/case_b22b58a0-verify.sh": {"mcp", "hostops"},
            "test_responsible_leader.sh": set(),
            "test_request_record_integrity.sh": {"private_mutation"},
            "test_decision_notify.sh": set(),
            "test_leader_notify.sh": set(),
            "test_notify_transport.sh": {"private_mutation"},
            "agent_endpoint_preflight.sh": set(),
            "test_agent_endpoint_preflight.sh": {"private_mutation"},
            "org_provisioner.sh": {"privilege"},
            "org_request_queue.sh": {"privilege", "private_mutation"},
            "test_request_queue.sh": {"privilege"},
            "test_privilege_ceilings.sh": {"privilege"},
            "operator_runbook.sh": {"runbook"},
            "docs/OPERATOR-RUNBOOK.md": {"runbook"},
            "plugins/dispatch/src/manifest.ts": {"dispatch"},
            "plugins/dispatch/dist/manifest.js": {"dispatch"},
            "plugins/cliproxy-insight/src/manifest.ts": {"cliproxy"},
            "gh-event-capture/systemd/capture.timer": {"capture"},
        }
        for path, areas in expect.items():
            with self.subTest(path=path):
                r = self.result(path)
                self.assertEqual({a for a in cc.AREAS if r[a]}, areas | {"core"})
                self.assertFalse(r["full"])

    def test_wiring_gate_docs_wake_only_core(self):
        # case_f3500d44: the wiring gate step reads every ops/**/OPERATOR.md
        # cron line and ops/case_658bf6b9/MODULES.md, so a markdown-only edit
        # there must wake offline-suites (core) and nothing else.
        for path in (
            "ops/case_658bf6b9/MODULES.md",
            "ops/case_658bf6b9/OPERATOR.md",
            "ops/case_a2ab2937/OPERATOR.md",
            "ops/case_56fff4c6/OPERATOR.md",
        ):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertFalse(r["full"])
                self.assertEqual({a for a in cc.AREAS if r[a]}, {"core"})

    def test_plugin_sources_beyond_the_manifest_stay_out_of_offline(self):
        for path in ("plugins/dispatch/src/worker.ts",
                     "gh-event-capture/src/app.js"):
            with self.subTest(path=path):
                self.assertFalse(self.result(path)["core"])

    def test_every_core_area_script_wakes_core(self):
        # Drift guard for the case_82df1671 shape: a script that a single-line
        # `run:` step of the suites job's core area executes must classify
        # with core, or its step skips on a PR that changed only that script
        # while the required check reports success.
        scripts = core_area_scripts()
        self.assertGreater(len(scripts), 50, "run-step parser found too few steps")
        missing = sorted(p for p in scripts if not self.result(p)["core"])
        self.assertEqual(missing, [], "suites core area runs these but a change to "
                         "them does not wake it; add them to ALSO_CORE_PREFIXES")

    def test_offline_cutover_contract_inputs_wake_core(self):
        for path in ("vendor/offline-ci-contract.json", "vendor/offline-ci-proof.json",
                     "verification/offline_ci_contract.py",
                     "test_offline_ci_contract.py", "test_toolkit_sync.py"):
            with self.subTest(path=path):
                self.assertTrue(self.result(path)["core"])
        for path in ("vendor/toolkit-files.txt", "vendor/toolkit-pins.txt"):
            with self.subTest(path=path):
                self.assertTrue(self.result(path)["core"])
                self.assertTrue(self.result(path)["private_mutation"])
                self.assertTrue(self.result(path)["upstream"])

    def test_toolkit_pin_bump_wakes_upstream(self):
        # Paperclip Review on PR #875 (case_1d1195e1): sync-toolkit.sh restores
        # four upstream-gated subjects from the pinned toolkit release, so a
        # pin bump (pins file, file manifest, or sync script) must wake the
        # upstream gates that prove them -- otherwise that PR and its main
        # push skip case_28030601/case_ff1fbbce/case_4d10ac5e while ci-ok stays green.
        for path in ("scripts/sync-toolkit.sh",
                     "vendor/toolkit-files.txt",
                     "vendor/toolkit-pins.txt"):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertTrue(r["upstream"])
                self.assertTrue(r["core"])
                self.assertTrue(r["private_mutation"])
                self.assertFalse(r["full"])

    def test_case_0ceebdfd_orphan_suites_wake_core(self):
        # case_87af7e92: the six orphan suites are now wired into suites job.
        # Their source files must classify as core so the PR-time selector
        # runs them when those files change.
        for path in (
            "cliproxy_seam_extract.py",
            "test_cliproxy_seam_extract.py",
            "garm_provider_latency_baseline.py",
            "test_garm_provider_latency_baseline.py",
            "kofra_preview_parity.py",
            "test_kofra_preview_parity.py",
            "upstream_draft_pin_audit.sh",
            "test_upstream_draft_pin_audit.sh",
            "ops/case_80989a33/cliproxy_config_gate.py",
            "ops/case_80989a33/test_cliproxy_config_gate.py",
            "garm_image_lint.py",
            "test_garm_image_lint.py",
            "test_garm_image_lint.sh",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.result(path)["core"])
                self.assertFalse(self.result(path)["full"])

    def test_case_e3e20d2e_writer_compat_wakes_core(self):
        # case_d8913f7c: the watchdog writer-compat suite is now wired into the
        # suites job. A change to the suite must wake the suites job at PR
        # time without latching a full run.
        r = self.result("test_watchdog_writer_compat.py")
        self.assertTrue(r["core"])
        self.assertFalse(r["full"])

    def test_case_7452fc43_watchdog_delivery_wakes_core(self):
        # case_f708f865: the watchdog-delivery proof runs in CI via the
        # platform watchdog gate. Its suite and delivery source must
        # classify as core so the PR-time selector runs the gate when
        # those files change.
        for path in (
            "test_watchdog_delivery.py",
            "watchdog/delivery.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.result(path)["core"])
                self.assertFalse(self.result(path)["full"])

    def test_case_ecf00eba_flap_guard_wakes_core(self):
        # case_e8e49a02: the flap-guard proof is wired into the suites job.
        # Its suite and guard source must classify as core so the PR-time
        # selector runs them when those files change.
        for path in (
            "test_flap_guard.py",
            "ops/case_658bf6b9/flap_guard.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.result(path)["core"])
                self.assertFalse(self.result(path)["full"])

    def test_lockfile_forces_full(self):
        r = self.result("plugins/dispatch/package-lock.json")
        self.assertTrue(r["full"])
        self.assertTrue(all(r[a] for a in cc.AREAS))

    def test_workflow_change_forces_full(self):
        r = self.result(".github/workflows/ci.yml")
        self.assertTrue(r["full"])

    def test_trivy_lockfile_sets_trivy(self):
        # case_572c6391: npm lockfiles latch full (existing contract) and record
        # trivy; other-ecosystem lockfiles record trivy without the full
        # latch (they wake their own area through normal classification).
        # C6 cutover: plugins/model-selection/ moved, so its lockfile falls
        # through to full (and still records trivy); no selection area left.
        r = self.result("plugins/dispatch/package-lock.json")
        self.assertTrue(r["trivy"])
        self.assertTrue(r["full"])
        r = self.result("plugins/model-selection/pnpm-lock.yaml")
        self.assertTrue(r["trivy"])
        self.assertTrue(r["full"])
        self.assertNotIn("selection", r)

    def test_trivy_workflow_sets_trivy(self):
        # case_572c6391: workflow definitions latch full and record trivy.
        r = self.result(".github/workflows/ci.yml")
        self.assertTrue(r["trivy"])
        self.assertTrue(r["full"])

    def test_trivy_dockerfile_wakes_scan(self):
        # case_572c6391: container build inputs wake the scan. Runner/deploy
        # Dockerfiles are also offline tooling surface, so core stays set
        # (the runner-label guards must still run); the scan bit is what
        # this test pins.
        for path in ("github-runner/garm/Dockerfile.isolated",
                     "deploy/Dockerfile"):
            with self.subTest(path=path):
                r = self.result(path)
                self.assertTrue(r["trivy"])
                self.assertFalse(r["full"])
                self.assertTrue(r["core"])

    def test_trivy_docs_only_stays_skipped(self):
        # case_572c6391: prose changes must skip the scan.
        r = self.result("README.md", "docs/runbooks/foo.md")
        self.assertFalse(r["trivy"])
        self.assertFalse(r["full"])

    def test_detector_change_forces_full(self):
        r = self.result(".github/scripts/ci_changes.py")
        self.assertTrue(r["full"])

    def test_lib_is_shared(self):
        r = self.result("lib/pcsql.sh")
        self.assertTrue(r["full"])

    def test_unknown_path_fails_closed(self):
        r = self.result("brand-new-dir/something.xyz")
        self.assertTrue(r["full"])
        self.assertTrue(r["core"])

    def test_empty_list_fails_closed(self):
        r = self.result()
        self.assertTrue(r["full"])


if __name__ == "__main__":
    unittest.main()
