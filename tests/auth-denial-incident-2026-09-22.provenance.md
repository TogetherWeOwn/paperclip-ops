# Auth-denial incident fixture: corrected evidence

The original seven-field fixture was committed in `8887cc8c58af532155f9dc4f8c9e39077561a809`
and inspected at `1b58bdd5841b0fc54168f47348648450350c63ca`. It had **65 run records**,
with `id`, `agentId`, `status`, `errorCode`, `error`, `createdAt`, `finishedAt`.
These original values and ordering are preserved. The only added field is
`usageJson`, recovered by an exact ID join, never inferred from an agent's current
model configuration or from error wording.

## Source of the added fields

- Read-only `GET /api/heartbeat-runs/{id}`, 65 of 65 IDs, retrieved 2026-09-23 UTC.
- Retriever run: `00000000-0000-4000-8000-000000000196`.
- Company: `00000000-0000-4000-8000-000000000004`.
- Allowlisted fields: `usageJson.model`, `provider`, `inputTokens`, `outputTokens`,
  `cachedInputTokens`. Missing fields remain missing; unknown usage is `{}`.
- Sanitized evidence attachment: `00000000-0000-4000-8000-000000000197`, registered
  work product `00000000-0000-4000-8000-000000000198`, on case_9cc4c0ad.
- Original metadata-file SHA-256:
  `73761cc9869128f29ce7b01778e04d8e557510444d2c139e25e5e617e23d15dc`.
- The attachment upload succeeded; re-download was unavailable (HTTP 400).
  The joined data comes from the locally preserved sanitized source, not a
  claim of remote attachment byte verification.
- No raw API response, credential, transcript, runtime config or context
  snapshot is part of this fixture.

Director decision: the correction was accepted on case_9cc4c0ad via interaction
`00000000-0000-4000-8000-000000000199`, targeting `lane-evidence` document revision
`00000000-0000-4000-8000-000000000200`. Acceptance correction is **not** merge approval.

## Correct oracle

Between **2026-09-22T21:36:20.118Z** and **2026-09-22T21:48:17.746Z**:

- 8 auth denials, 3 agents; all report `claude-opus-5`.
- 11 interior successes: 8 `gpt-6-astra`, 3 `muse-spark-1.3-contributor(xhigh)`.
- Zero demonstrated interior successes or other terminal control attempts on
  `claude-opus-5`.
- Result: **INSUFFICIENT**, exit 4, zero same-model recoveries.

The earlier claimed recovery, run `00000000-0000-4000-8000-000000000201` by agent
`00000000-0000-4000-8000-000000000202`, reports `gpt-6-astra`. The prior claim of
INTERMITTENT on the denied model is withdrawn. Separate synthetic tests now
prove same-model interleaving and same-agent recovery.

Recorded model metadata is not an upstream account, credential, routing trace
or causal diagnosis. The tool retains `ENTITLEMENT` as a CLI label for a
zero-success window with at least two same-model attempted control runs, not
proof of withdrawn access. Unknown-model and zero-usage records are insufficient
control evidence. Known pre-inference aborts are excluded even with stale usage.
A failed run with positive usage may have produced tokens before its terminal
error; the tool describes terminal outcomes rather than exact request timing.
No verdict authorizes credential, routing, deployment or model-pin changes.

## Reproduce offline

From the repository root (Python 3 standard library only; developed on 3.13.5):

```sh
python3 test_auth_denial_discriminator.py
python3 verification/auth-denial-mutation-gate.py
python3 auth_denial_discriminator.py --input tests/auth-denial-incident-2026-09-22.json --json
# The replay intentionally exits 4, not 0.
```

CI registration targets `.github/workflows/ci.yml` under `offline-suites`.
On 2026-09-23 GitHub rejected the workflow commit for missing `workflows`
permission. The separate patch is preserved in case_9cc4c0ad's `ci-registration`
document; it must land through an authorized path before this work is complete.
Local test success is not CI registration or merge approval.

The gate runs the complete unmodified suite, then stages each mutant independently. Each pair of named tests must first pass against the
unmodified staged source, then fail with assertions against the mutant. Missing
anchors, syntax errors, startup/test-loader errors, skips, population changes
and timeouts fail the gate rather than count as kills. No mutations are applied
to the working checkout. A mutation's effect on unrelated tests is not scored.

The first six mutants cover the promised shortcuts: error-code classifier,
dropped control guard, padded window, nonterminal controls, never-split bursts,
and trusting the denial wording. Six more cover model identity, attempt evidence,
pre-inference failures, per-model grouping, exit severity and the minimum default.

This is a passive analysis tool and CI contract, **not a deployed recurring
monitor**. CI green does not establish current fleet health. The API path reads
heartbeat history only, taking its key from `PAPERCLIP_API_KEY` rather than argv.
For a clean window normal output is silent with exit 0; `--json` explicitly asks
for a machine-readable empty result. `--input -` supports a piped history read.
