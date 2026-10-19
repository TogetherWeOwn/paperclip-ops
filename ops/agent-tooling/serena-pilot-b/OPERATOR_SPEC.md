# Operator spec: Serena 2.0.0 Pilot B install + registration (single host step)

One `Operator:` card covers this whole spec. No second card for the same step.

## Scope

Install serena-agent 2.0.0 inside the paperclip container (container
python3.13) and register it as a company `mcp_local_stdio` tool connection
bound ONLY to the four builder agents below. Telemetry off. Dashboard off.
Exactly 3 tools via the read-only context in this directory.

Builder agent IDs (bound set, nothing else):

- Founding Engineer: `2fe4d7f9-9bf9-41c7-9d6e-e84d7bb0ae22`
- Web Engineer: `69c12a25-80df-4c80-8026-f12651e653eb`
- QA & Release Engineer: `69f42350-2fc9-412a-9752-657656a1a4ef`
- DevOps & Reliability Engineer: `7179efed-7baa-48e4-a7c9-06be4a9fbbab`

## ACT

1. Install pinned (hashes regenerated at install time):
   `pip install --requirement ops/agent-tooling/serena-pilot-b/requirements-serena-2.0.0-lock.txt`
   (container python3.13; satisfies `>=3.11,<3.15`).
2. Language servers: tsserver (TS), rust-analyzer (already at
   `/paperclip/.cargo/bin`), Svelte (nntunev2). Pre-fetch with
   `serena download-ls-dependencies <ids>` or the SolidLSP equivalent so
   first activation does not download at call time.
3. Register `mcp_local_stdio` with this exact stdio argv (order matters):
   - `SERENA_USAGE_REPORTING=false` in the environment (telemetry is ON by
     default upstream; verified opt-out in 2.0.0 sdist).
   - `serena start-mcp-server --transport stdio --agent-interface tools
     --context ops/agent-tooling/serena-pilot-b/contexts/builder-readonly.yml
     --mode no-memories --enable-web-dashboard False
     --open-web-dashboard False --enable-gui-log-window False
     --project-from-cwd`
   - Why `--agent-interface tools`: 2.0.0 defaults to REPL, where tool
     filtering does NOT apply. Without it the 3-tool limit silently fails.
   - Why `--mode no-memories`: excludes memory/onboarding tools so the
     server cannot write into repos (Stage-0 "wrote nothing" claim).
4. Bind the connection to the four builder IDs above and no other agent.
5. Health check: from a builder-scoped session, `tools/list` shows exactly
   `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`; one
   `get_symbols_overview` call on a small repo file returns an outline.

## WHY

Owner-approved Pilot B ([TOG-31954](/TOG/issues/TOG-31954)): Stage-0 measured
55-162x fewer tokens than whole-file reads on 300k+ token files, with exact
language-server references. Builders re-read big files hundreds of times/week.

## UNDO (rollback)

1. Remove the builder binding (or the whole `mcp_local_stdio` connection).
2. Uninstall: `pip uninstall serena-agent` (leave language servers; note it).
3. Report: connection/registration ID removed, `tools/list` no longer shows
   the three tools for builders. Rollback is triggered on canary failure
   (see CANARY.md) or on any write/outbound call observed from the server.

## Flags for the record (not blockers)

- License is GPL-3.0-or-later (internal container use; no distribution).
  CISO awareness only; no new external dependency at call time (Stage-0
  observed no outbound calls; re-verify in canary).
- Stage-0 RSS was ~1.9 GB; watch container memory during canary.
- Evidence files (`RESULTS.md` snippet, `REPORT.md` baselines) were not in
  paperclip-ops at this head; context/pin authored from the 2.0.0 sdist and
  PyPI metadata. Reconcile with the snippet when located (gap G1).
