#!/usr/bin/env python3
# ===========================================================================
# ci_changes.py -- change-area classifier for the CI standard (case_6b39c80f /
# case_d5bc7950). Reads a newline-separated file list (git diff --name-only
# --no-renames) on stdin, or files given as argv, and prints KEY=true/false
# lines for each area plus the full-run latch. The ci.yml `changes` job turns
# those into job outputs; heavy jobs run only when their area (or `full`) is true.
#
# Fail-closed: an unrecognised path, an empty list, or any classifier error
# sets full=true (run everything) rather than skipping. A classifier that
# cannot tell what changed must never read as "nothing changed".
#
# Areas (one boolean per gated job or step area in ci.yml):
#   privilege        privilege-suites targets (orgdb fixture, ceilings, drift)
#   core             core-area steps in the suites job
#   private_mutation PR-only targeted mutants for private integration suites
#   runbook          runbook-area steps in the suites job
#   omniroute, dispatch, cliproxy, capture, mcp, hostops:
#              per-plugin / per-family suites (broker retired with the
#              agent-side minter; selection retired with the C6
#              model-selection cutover; the moved path falls through to full)
#   upstream   upstream-pipeline family (case_1d1195e1): upstream_gate.sh, the
#              linter, the reputation pass, the filing-readiness gate, the pin
#              audit and their suites plus the mutation gates that prove them.
#              These used to wake the whole core block -- including the 26-min
#              case_214e61d3 mutation gate -- on every core PR; they now wake only
#              the upstream-gated steps (and core, which executes them too).
#   trivy      Trivy filesystem-scan inputs (case_572c6391): lockfiles,
#              Dockerfiles, workflow definitions
#   full       force-run latch: dependency manifests, .github, shared code,
#              or the detector itself changed
# ===========================================================================
from __future__ import annotations

import os
import sys

# (area, prefixes...). A path belongs to every area whose prefix matches.
# Order is irrelevant; matching is prefix-per-path, first-area-wins is NOT
# used. A path that matches ANY area skips the generic test_/root fallthrough
# in classify(), so a path pinned to a specialist suite wakes ONLY that suite
# unless offline-suites also executes it -- those paths are listed in
# ALSO_CORE_PREFIXES below (case_82df1671), not left to the fallthrough.
AREA_PREFIXES: dict[str, tuple[str, ...]] = {
    "core": (
        # deploy_window_open.sh is covered by verify-fast and CI suites.
        "deploy_window_open.sh",
        "test_deploy_window_open.sh",
        # The health suite executes the documented cron. Its runbook and
        # every module input (including fixtures) are test surface, not prose.
        "ops/case_73259b15/",
        "docs/runbooks/coolify-app-health.md",
        # case_6d842160: acceptance_transport.sh reply classification and its
        # offline suite run as a core suites step (root fallthrough would
        # also wake core; this pins the intent).
        "acceptance_transport.sh",
        "test_acceptance_transport.sh",
        # case_87af7e92: six orphan suites now wired into the suites job.
        # Each suite is explicitly pinned so a change to its source wakes
        # the suites job at PR time.
        "cliproxy_seam_extract.py",
        "test_cliproxy_seam_extract.py",
        "garm_provider_latency_baseline.py",
        "test_garm_provider_latency_baseline.py",
        # provider-baseline-integrity: the integrity suite runs as its own
        # suites step next to its sibling; pin it so a change to the suite
        # wakes the suites job at PR time.
        "test_provider_baseline_integrity.py",
        "kofra_preview_parity.py",
        "test_kofra_preview_parity.py",
        "test_kofra_preview_parity_json.py",
        "upstream_draft_pin_audit.sh",
        "test_upstream_draft_pin_audit.sh",
        "ops/case_80989a33/cliproxy_config_gate.py",
        "ops/case_80989a33/test_cliproxy_config_gate.py",
        "garm_image_lint.py",
        "test_garm_image_lint.py",
        "test_garm_image_lint.sh",
        # case_d8913f7c: watchdog writer-compat suite now wired into the suites
        # job. Pin the suite so a change to it wakes the suites job at PR
        # time. (The contract sources already wake core through the generic
        # tooling fallthrough; watchdog/detectors.py keeps its full latch.)
        "test_watchdog_writer_compat.py",
        # case_f708f865: watchdog-delivery proof already runs in CI via the
        # platform watchdog gate (verification/platform-watchdog-gate.sh
        # runs test_watchdog_delivery.py with || fail). The suite file
        # would fall through to core as root test_*; watchdog/delivery.py
        # is otherwise an unrecognised path that latches full, so pin both
        # explicitly: a change to either wakes the suites job (and hence
        # the gate) at PR time by intent, under core scope.
        "test_watchdog_delivery.py",
        "watchdog/delivery.py",
        # case_e8e49a02: flap-guard offline proof wired into the suites job.
        # The suite file would fall through to core as root test_* and the
        # guard as ops/, but pin explicitly so a change to either wakes
        # the suites job at PR time by intent, not by fallthrough.
        "test_flap_guard.py",
        "ops/case_658bf6b9/flap_guard.py",
        # Shared migration safety gates (docs/migration-standard.md) wired
        # into the suites job as `migration safety gates suites`. The suite
        # files would fall through to core as root test_* and the gates as
        # root scripts, but pin explicitly so a change to any of them wakes
        # the suites job at PR time by intent, not by fallthrough.
        "migration_timeout_preamble.sh",
        "migration_squawk.sh",
        "migration_sql.py",
        "squawk-migration.toml",
        "migration_rehearsal.sh",
        "migration_drift_gate.py",
        "test_migration_timeout_preamble.sh",
        "test_migration_squawk.sh",
        "test_migration_squawk_json.sh",
        "test_migration_squawk_json_guarantees.sh",
        "test_migration_rehearsal.sh",
        "test_migration_drift_gate.py",
        "test_migration_evidence_ax.py",
        # Harness code, fixtures and scored tables are offline suite inputs.
        "eval/review-bakeoff/",
        # Native-SSH citation gate and its hermetic mutation suite, wired
        # into the suites job. Both would fall through to core as root
        # files; pin explicitly so a change to either wakes the suites job
        # at PR time by intent, not by fallthrough.
        "native_ssh_citation_gate.py",
        "test_native_ssh_citation_gate.sh",
    ),
    "privilege": (
        "test/fixtures/orgdb/",
        "test_orgdb_fixture.sh",
        "test_privilege_ceilings.sh",
        "test_request_queue.sh",
        "test_provisioned_transport_db.py",
        "schema_drift.sh",
        "verification/case_aa908088-mutation-gate.sh",
        # CISO F1 (case_f11bc226): the suites' enforcement subjects. A
        # ceiling-weakening edit to either must wake privilege-suites;
        # without these entries it classified core-only and the suite
        # skipped while its required check reported success via skip.
        "org_provisioner.sh",
        "org_request_queue.sh",
    ),
    # The generic mutation matrix moved to paperclip-operator-toolkit, but
    # these private subjects and integration suites still need local mutants.
    # The endpoint-preflight source is toolkit-owned; its private test remains
    # covered here against the pinned restored source.
    "private_mutation": (
        "org_request_queue.sh",
        "notify_paperclip_issue.sh",
        "test_request_record_integrity.sh",
        "test_notify_transport.sh",
        "test_agent_endpoint_preflight.sh",
        "test_private_mutation_gates.py",
        "verification/private-mutation-gates.sh",
        "scripts/sync-toolkit.sh",
        "vendor/toolkit-files.txt",
        "vendor/toolkit-pins.txt",
    ),
    "runbook": (
        "operator_runbook.sh",
        "operator_runbook_classification.json",
        "test_operator_runbook.sh",
        "docs/OPERATOR-RUNBOOK.md",
        "verification/case_786dae75-runbook-withdrawal-render-gate.sh",
    ),
    "omniroute": (
        "plugins/omniroute-broker/",
        "test_case_e20effbb_mapping_guard.mjs",
    ),
    "dispatch": (
        "plugins/dispatch/",
        "test_dispatch_sweep_failure_visibility.py",
    ),
    "cliproxy": (
        "plugins/cliproxy-insight/",
    ),
    "capture": (
        "gh-event-capture/",
    ),
    "mcp": (
        "mcp/",
    ),
    "hostops": (
        "mcp/",
        "host-ops/",
        "test_host_ops_broker.py",
        "test_host_ops_install.sh",
        "test_host_ops_prepare_first_install.sh",
        "test_host_ops_staging_preflight_carrier.py",
    ),
    # case_1d1195e1: the upstream-pipeline family. Every entry below is ALSO
    # listed in ALSO_CORE_PREFIXES (kept deliberately: an upstream PR still
    # runs its core guards, and the core-block drift test requires executed
    # scripts to wake core). docs/upstream/ would otherwise read as prose
    # and wake nothing, even though the filing-strip gate scores those files.
    # The three toolkit pin files are upstream too: sync-toolkit.sh restores
    # four upstream-gated subjects (upstream_filing_readiness.sh, its suite,
    # case_28030601 and the bundle filing gate) from the pinned toolkit release,
    # so a pin bump rewrites their bytes and must wake the gates that prove
    # them (private_mutation already lists the same three pins for this
    # reason).
    "upstream": (
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
        "upstream-memory/",
        "docs/upstream/",
        "scripts/sync-toolkit.sh",
        "vendor/toolkit-files.txt",
        "vendor/toolkit-pins.txt",
    ),
    # C6 repo split: plugins/model-selection/ moved to
    # paperclip-operator-toolkit (pinned v0.5.1). No area claims it: a touch
    # there falls through to the unrecognised-path rule below (core + full
    # latch, fail-closed) and the model-selection-moved guard step (secret-scan job) reds the PR.
}

# Paths pinned to a specialist area above that offline-suites ALSO executes or
# reads (case_82df1671). Without these a change wakes the specialist suite and
# skips offline-suites, whose required check then reports success via skip
# while its step never ran. Each entry names the offline-suites consumer;
# test_ci_changes.py walks the single-line `run:` steps of offline-suites and
# fails when a step's script classifies without `core`, so a new suite cannot
# repeat this.
ALSO_CORE_PREFIXES: tuple[str, ...] = (
    # code-nav tests pin the hash-checked install command, so this spec is
    # executable contract surface even on a markdown-only PR.
    "ops/agent-tooling/serena-pilot-b/OPERATOR_SPEC.md",
    # host operations broker: `host operations broker suite` and `installer
    # contract suite` steps, the host-ops/*.sh parse gate, and
    # test_host_ops_broker.py reads host-ops/RUNBOOK.md.
    "host-ops/",
    "test_host_ops_broker.py",
    "test_host_ops_install.sh",
    "test_host_ops_prepare_first_install.sh",
    # staging-preflight carrier: the `staging-preflight carrier suite` step
    # executes this file in the suites job's core area.
    "test_host_ops_staging_preflight_carrier.py",
    # mcp/deploy: the bash -n parse gate, acceptance_live_selftest.sh, and the
    # case_b22b58a0 verifier that test_case_bc1705d4_verify_g4.sh drives.
    "mcp/deploy/",
    # Notify suites run as offline-suites steps. Generic toolkit-owned logic
    # is mutated there; private integration subjects retain targeted mutants.
    "test_responsible_leader.sh",
    "test_request_record_integrity.sh",
    "test_decision_notify.sh",
    "test_leader_notify.sh",
    "test_notify_transport.sh",
    "notify_paperclip_issue.sh",
    "agent_endpoint_preflight.sh",
    "test_agent_endpoint_preflight.sh",
    "test_private_mutation_gates.py",
    "verification/private-mutation-gates.sh",
    # The targeted job restores these sources using the pinned manifest and
    # checksum before running the private integration suites.
    "scripts/sync-toolkit.sh",
    "vendor/toolkit-files.txt",
    "vendor/toolkit-pins.txt",
    # privilege subjects and suites: test_provisioning_policy.sh and its
    # mutation gate drive org_provisioner.sh; the SQL-backend suite and its
    # mutation gate stage all four.
    "org_provisioner.sh",
    "org_request_queue.sh",
    "test_request_queue.sh",
    "test_privilege_ceilings.sh",
    # runbook: test_operator_runbook.sh section 11 fails when the committed
    # runbook differs from `operator_runbook.sh render`.
    "operator_runbook.sh",
    "docs/OPERATOR-RUNBOOK.md",
    # systemd units that host_timer_registry_check.py reconciles with the
    # timer registry.
    "gh-event-capture/systemd/",
    # wiring gate inputs (case_f3500d44): the gate step in offline-suites reads
    # every ops/**/OPERATOR.md cron line and ops/case_658bf6b9/MODULES.md, so a
    # markdown-only edit there must still wake offline-suites. The module
    # registry is one file (prefix match); OPERATOR.md needs a suffix match
    # in classify() below because prefix lists cannot express ops/**/.
    "ops/case_658bf6b9/MODULES.md",
    # cliproxy mgmt recon (case_7cbfeac4): the `cliproxy mgmt recon argv-leak
    # suite` step in offline-suites guards this script, which otherwise
    # classifies to the cliproxy specialist area only — a recon-only PR
    # would skip its guard while the check stays green.
    "plugins/cliproxy-insight/deploy/cliproxy_mgmt_recon.sh",
    # readme script-index gate: test_readme_script_index.sh parses the README
    # "What is here" table, so a README-only edit must wake offline-suites.
    "README.md",
    # migration contract doc: DocsCanonicalTest in
    # test_migration_evidence_ax.py scores this file, so a docs-only edit to
    # the JSON contract must still wake the suites that guard it.
    "docs/migration-standard.md",
    # case_1d1195e1: upstream-pipeline family. The suites job's core block
    # executes these tools and suites (upstream-gated steps), so a PR that
    # changes only them must still wake core; without these entries it would
    # wake upstream alone and skip its core guards. docs/upstream/ is pinned
    # here rather than left to the prose skip: the filing-strip gate scores
    # those files, so a procedure edit must wake the suites that guard it.
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
    "upstream-memory/",
    "docs/upstream/",
)
# Plugin schedule declarations: host_timer_registry_check.py (offline-suites)
# fails a plugin job schedule that no registry entry cites (I4/I5).
ALSO_CORE_PLUGIN_SUFFIXES = ("/src/manifest.ts", "/dist/manifest.js")

# Paths that force a full run (CI standard rule 4): dependency manifests or
# lockfiles, the workflow/config layer, shared code every suite depends on,
# and the detector itself. Suffix match (any depth) for manifests.
FULL_SUFFIXES = ("package.json", "package-lock.json")
FULL_PREFIXES = (
    ".github/",
    "lib/",
    "required_checks.json",
    "tool_grant_expectations.json",
)

# case_572c6391: inputs for the PR-scoped Trivy filesystem scan. Lockfiles and
# workflow definitions already latch full (above), so they wake every suite;
# the trivy area exists so the scan can stay scoped to exactly these inputs
# plus container build inputs (Dockerfiles), which otherwise classify as core
# tooling. Suffix match (any depth) for manifests; Dockerfile is a basename
# match handled in classify().
TRIVY_SUFFIXES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Gemfile.lock",
    "Cargo.lock",
    "go.sum",
    "go.mod",
    "poetry.lock",
    "Pipfile.lock",
)
TRIVY_PREFIXES = (
    ".github/workflows/",
)

AREAS = ("privilege", "core", "private_mutation", "runbook", "omniroute",
         "dispatch", "cliproxy", "capture", "mcp", "hostops",
         "upstream", "trivy")


def classify(paths: list[str]) -> dict[str, bool]:
    """Return {area: changed, ..., 'full': latch}. Fail-closed on unknown."""
    out: dict[str, bool] = {a: False for a in AREAS}
    out["full"] = False
    if not paths:
        out["full"] = True
        return out
    core_hit = False
    for raw in paths:
        p = raw.strip()
        if not p:
            continue
        # case_572c6391: Trivy inputs. Checked before the full latch so a
        # lockfile/workflow path records trivy=true even though it also
        # latches full (and `continue`s past area matching below).
        base = p.rsplit("/", 1)[-1]
        if (p.endswith(TRIVY_SUFFIXES) or p.startswith(TRIVY_PREFIXES)
                or base == "Dockerfile" or base.startswith("Dockerfile.")
                or base.endswith(".dockerfile")):
            out["trivy"] = True
        if p.endswith(FULL_SUFFIXES) or p.startswith(FULL_PREFIXES):
            out["full"] = True
            continue
        matched = False
        for area, prefixes in AREA_PREFIXES.items():
            if p.startswith(prefixes):
                out[area] = True
                matched = True
        if p.startswith(ALSO_CORE_PREFIXES) or (
                p.startswith("plugins/") and p.endswith(ALSO_CORE_PLUGIN_SUFFIXES)) or (
                p.startswith("ops/") and p.endswith("/OPERATOR.md")):
            core_hit = True
        if matched:
            continue
        # Docs-only prose skips every suite: docs/ trees and markdown files.
        # README.md is the exception: the index gate parses it (ALSO_CORE_PREFIXES).
        # NOTE: root *.txt files are NOT prose here (quota_brake_exempt.txt,
        # upstream_draft_pins.txt are suite inputs) and must keep falling
        # through to core below.
        if p.startswith("docs/") or p.endswith(".md"):
            continue
        if p.startswith(("test_", "tests/", "verification/", "scripts/",
                         "github-runner/", "hooks/", "ops/", "deploy/",
                         "upstream-memory/")) \
                or "/" not in p:
            # Root scripts, suites, gates and tooling dirs are offline-suites
            # surface. (Root dotfiles like .gitignore fall here too: harmless,
            # offline is the cheap suite next to the two mutation jobs.)
            core_hit = True
            continue
        # Anything unrecognised is shared surface until proven otherwise:
        # it counts as core AND latches full. Core so offline-suites (the
        # parse gate, the audit, the drift suites) still sees it; full so
        # no heavy suite is skipped on a path the classifier never learned.
        core_hit = True
        out["full"] = True
    # Preserve explicit core matches as well as the generic tooling paths.
    # A change confined to one plugin must NOT wake offline-suites.
    out["core"] = out["core"] or core_hit
    if out["full"]:
        for a in AREAS:
            out[a] = True
    return out


def main() -> int:
    if len(sys.argv) > 1:
        paths = list(sys.argv[1:])
    else:
        paths = [ln.strip() for ln in sys.stdin if ln.strip()]
    try:
        result = classify(paths)
    except Exception as exc:  # fail closed: measure nothing, run everything
        print(f"classifier error, fail-closed, running everything: {exc}",
              file=sys.stderr)
        print("full=true")
        for a in AREAS:
            print(f"{a}=true")
        return 0
    print(f"full={'true' if result['full'] else 'false'}")
    for a in AREAS:
        print(f"{a}={'true' if result[a] else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
