# Registered executor action spec: synthetic-alerts binding + live proof (DRAFT — no authority granted)

- Status: **DRAFT for CISO review. This document authorizes nothing.**
- Card: [TOG-28237](/TOG/issues/TOG-28237) (parent [TOG-28235](/TOG/issues/TOG-28235), source [TOG-27759](/TOG/issues/TOG-27759), blocks [TOG-26880](/TOG/issues/TOG-26880))
- Prior CISO verdict: [TOG-27833](/TOG/issues/TOG-27833) (deny — no registered, hash/expiry/custody-bound action)
- Scope: docs/spec only. No GitHub or secret mutation, no SHADOW removal, no resubmission of the old prose requests.

## 1. Why this spec exists

Three queued prose requests are **not executable** and must never be resubmitted, reclassified, or relabelled:

| Queued request | Role | Defect (fail-closed correction) |
|---|---|---|
| `req-20261017T062347Z-1461400.json` | binding/proof | skips main-only creation when the environment already exists → could bind the webhook to an unrestricted environment |
| `req-20261017T104649Z-3976689.json` | tighten | permits protection **after** binding; rollback **removes** the restriction |
| `req-20261017T125958Z-2261914.json` | safety addendum | class `unknown` (not staging) → never executes; kept as **requirements**, not a request |

Verified 2026-10-17 (read-only, no values printed): all three still queued, no receipt in `done`/`processed`/`out`;
executor allowlist is `{"version":1,"actions":{}}`; `/etc/paperclip-operator/SHADOW` present;
`/usr/local/lib/paperclip-operator/runbooks` absent; `/usr/local/sbin/op-approve-exec` installed root-owned.
`synthetic-alerts` exists with `deployment_branch_policy` null (any branch) and zero secrets.

This spec defines **ONE** registered action in exactly the form
`/usr/local/sbin/op-approve-exec` (`op_approval_executor.sh`, sha256 `f91c47e1…0766`, suite 54/54) accepts,
folding the addendum's ordering in as hard gates. Provisioning of the allowlist entry is the Operator's own
host work **after** CISO approval of this spec — never before.

## 2. Principal and approval binding (no new identity invented)

- **Executor principal:** root-held `/usr/local/sbin/op-approve-exec`. It resolves an action id through the
  root-owned allowlist to one fixed runbook script plus validated arguments. No shell passthrough, no arbitrary
  command, no eval, no caller-selected executable. The request `command` field is untrusted data, never executed
  (`ignored_untrusted_command` in the receipt).
- **Approver:** must be a line in root-owned `/etc/paperclip-operator/approvers.txt`. Proposed sole initial
  approver: CEO agent `e4f5c0ce-9d95-4443-97d2-53b35f0a2dca` (per [TOG-17067](/TOG/issues/TOG-17067); CISO confirms
  or names an alternative — CISO decides once).
- **Requester (Operator `be571a9f-…`) cannot self-approve.** Approval requires `decision == "approved"`,
  byte-identical action/args/card binding, valid hash chain, `approved_at <= now < min(request.expires_at,
  approval.expires_at)`, and `superseded != true`. `human_only`/`spend` requests are always refused.
- **SHADOW:** while `/etc/paperclip-operator/SHADOW` exists, `execute` refuses (exit 4, nothing runs);
  `verify-only` still checks the full chain (`shadow=present`). **This spec does not remove SHADOW.**
  Execution additionally requires a separate SHADOW-removal authority (out of scope here).

## 3. Action id and allowlist entry (exact)

Action id: `github-synthetic-alerts-bind-v1` (matches `^[a-z0-9][a-z0-9_-]{1,63}$`).

```json
{
  "version": 1,
  "actions": {
    "github-synthetic-alerts-bind-v1": {
      "script": "/usr/local/lib/paperclip-operator/runbooks/github-synthetic-alerts-bind-v1.sh",
      "args": {
        "repo":               { "pattern": "^TogetherWeOwn/two-bot-next$", "max_len": 64 },
        "environment":        { "pattern": "^synthetic-alerts$",           "max_len": 64 },
        "branch_policy_name": { "pattern": "^main$",                       "max_len": 32 },
        "workflow_file":      { "pattern": "^synthetic-user-flow\\.yml$",  "max_len": 64 },
        "dispatch_ref":       { "pattern": "^main$",                       "max_len": 32 },
        "force_fail":         { "pattern": "^true$",                       "max_len": 8  },
        "custody_ref":        { "pattern": "^worker-ops-alerts$",          "max_len": 32 },
        "target_var_name":    { "pattern": "^SYNTHETIC_ALERT_WEBHOOK_URL$", "max_len": 64 },
        "custody_name":       { "pattern": "^OPS_ALERT_WEBHOOK_URL$",      "max_len": 64 },
        "prod_environment":   { "pattern": "^production$",                "max_len": 32 },
        "prod_var_name":      { "pattern": "^PRODUCTION_WORKER_URL$",      "max_len": 64 }
      },
      "rollback": "Preserve the main-only branch policy (never delete it, never set policy null). On post-bind failure keep environment/policy/secret and record state. If the production PRODUCTION_WORKER_URL override was deleted, POST-restore the exact name/value from executor-private before-state. Dispatch history is irreversible. Removal of the policy or secret requires a new approved action.",
      "timeout_secs": 300,
      "human_only": false,
      "spend": false
    }
  }
}
```

Notes (executor-enforced, not prose):

- All values are flat strings; no objects/arrays/numbers/booleans. `force_fail: "true"` is translated to JSON
  boolean `true` **inside** the fixed runbook only.
- No arg **name** contains `token`/`secret`/`password`/`api_key`/`private_key` (refused outright).
- `custody_ref` is a symbolic reference only. It carries **no secret value**; the runbook resolves it root-side
  to the existing Worker `OPS_ALERT_WEBHOOK_URL` custody (private host map, never named by path here).
- The `command` field, if present on the future request file, is ignored untrusted data.

Exact instantiation (the only permitted args object — every mutation target is bound here, not
hardcoded invisibly; finding 7):

```json
{
  "repo": "TogetherWeOwn/two-bot-next",
  "environment": "synthetic-alerts",
  "branch_policy_name": "main",
  "workflow_file": "synthetic-user-flow.yml",
  "dispatch_ref": "main",
  "force_fail": "true",
  "custody_ref": "worker-ops-alerts",
  "target_var_name": "SYNTHETIC_ALERT_WEBHOOK_URL",
  "custody_name": "OPS_ALERT_WEBHOOK_URL",
  "prod_environment": "production",
  "prod_var_name": "PRODUCTION_WORKER_URL"
}
```

(`target_var_name` is deliberately NOT called `target_secret_name`: the executor refuses arg names
containing `secret`/`token`/`password`/`api_key`/`private_key` outright. Values carrying those words are
allowed but log-scrubbed; none of the above values trigger the scrub pattern, which requires a
`name=value`-shaped pair.)

## 4. Hash and expiry (exact computation)

Per the executor header, `request_hash = "sha256:" + hex(sha256(canonical action binding))` where the canonical
binding is exactly `jq -S -c '{request_id,card_id,action,args}'` **stdout bytes including the trailing newline**
(never hash a command-substituted string — it strips the newline):

```bash
request_hash="$(jq -S -c '{request_id,card_id,action,args}' -- request.json | sha256sum | cut -d' ' -f1)"
request_hash="sha256:${request_hash}"
```

- `request_id` matches `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`, assigned once at request build time.
- `card_id` is the CISO review card id created from this spec (bound at approval, byte-identical in both files).
- Request skeleton (finding 14 — fields the executor requires beyond action/args):
  ```json
  {
    "request_id": "req-<UTC>-<rand>",
    "card_id": "<CISO-review-card-id>",
    "action": "github-synthetic-alerts-bind-v1",
    "args": { "<sec. 3 exact object>" },
    "request_hash": "sha256:<sec. 4 computation>",
    "created_at": "<ISO-8601Z>",
    "expires_at": "<created_at + 24h>"
  }
  ```
  Approval skeleton: same `request_id`/`card_id`/`action`/`args`/`request_hash` byte-identical, plus
  `approver` (a line in approvers.txt), `decision: "approved"`, `approved_at`, `expires_at`, `superseded != true`.
  (`jq -S -c` output includes the trailing newline in the hash input and depends on the installed jq
  version — the builder records the exact bytes hashed alongside the request so the verifier reproduces them.)
- `created_at`: build time (ISO-8601Z). `expires_at = created_at + 24h` (single execution window).
  The approval file carries its own `expires_at <= request.expires_at`; execution requires
  `approved_at <= now < min(request.expires_at, approval.expires_at)`.
- Replay/concurrency: `processed/<request_id>.claim/` (atomic mkdir) + `processed/<request_id>.receipt.json`.
  A second execute with the same id exits 6 having run nothing. Failures/timeouts record a receipt and exit 7 —
  **never auto-retried**; the operator re-runs by hand after reading the rollback line.

## 5. Custody path for `SYNTHETIC_ALERT_WEBHOOK_URL` (no value printed anywhere)

- Source: the **existing** Worker `OPS_ALERT_WEBHOOK_URL` custody. No new webhook, no export to the Operator,
  no secret read in any review. **No value appears in this spec, the request, the approval, the receipt, or logs.**
- Transport: the fixed runbook (running root-side behind the executor) streams the custody value **on stdin only**
  to `gh secret set SYNTHETIC_ALERT_WEBHOOK_URL --repo TogetherWeOwn/two-bot-next --env synthetic-alerts`.
  The executor takes no credential on argv, reads no credential file, and exports no credential variable
  (`/proc/*/cmdline` is world-readable).
- Verification is **name/metadata-only**: secret count `0 → 1`; never a value read-back.
- Precondition: secret count must be **0** immediately before binding (see gate B). If a secret is already
  present, abort — no rotation, no overwrite, no delete.

## 6. Runbook step sequence with serialized hard gates

`R = repos/TogetherWeOwn/two-bot-next/environments/synthetic-alerts`. Every step aborts the whole run on
mismatch (sanitized refusal receipt, exit 3-class). Steps are serial; no parallel execution.

- **G0 — preflight reads (no mutation).** `GET R` (record `deployment_branch_policy`, `protection_rules`,
  secret count by name only via `GET R/secrets` (`per_page=100`; names only, single page expected);
  `GET R/deployment-branch-policies`; `GET` repo variable `PRODUCTION_WORKER_URL`;
  `GET` `production`-environment variable `PRODUCTION_WORKER_URL` — compared **privately in executor memory**,
  never printed. (These are non-secret config URL vars, not the webhook secret, which is never readable —
  finding 9.) Abort unless: `protection_rules` empty; secret count `0`; policy state is either exactly the
  target or absent/null (to be tightened). Equality-gate decision table (finding 9):
  | repo var | prod-env var | verdict |
  |---|---|---|
  | nonempty, byte-equal | nonempty, byte-equal | G3 deletion armed (before-state captured first) |
  | missing (404) on prod-env side | — | G3 skipped, record reason, continue (nothing to clean) |
  | unequal, or either empty | — | **abort whole run** (contract assumption violated; fail-closed) |
  (Fallback verification per the original contract: repo-level var is the single source
  per `.github/workflows/synthetic-user-flow.yml:23-29`.)
- **G1a — create-or-tighten (the ONLY policy-write phase, BEFORE any secret; finding 2).**
  It is two API writes, executed in this fixed order with a read-first abort:
  (i) read current policy + policy list; if anything differs from {absent/null} or {exactly the target},
  **abort before writing anything** — never overwrite, never delete (a new spec is required);
  (ii) iff absent/null, `PUT R` with `{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}`;
  (iii) iff no `main` policy exists, `POST R/deployment-branch-policies {"name":"main","type":"branch"}`.
  From `null` (any branch) to custom-main-only is a tighten; a differing existing policy (e.g. protected-only)
  takes the abort path, so this phase can never downgrade (finding 10). Preserve any stronger preexisting
  protection — never weaken. **Never protect-after-bind: no policy write exists after G1a.**
  If (iii) fails after (ii) succeeded, Gate B catches the half-state and aborts unbound (finding 2/3).
- **Gate B — strict read-back immediately BEFORE the secret bind.** Both predicates must print `true`:
  `gh api R --jq '.deployment_branch_policy | .custom_branch_policies == true and .protected_branches == false'`
  and `gh api R/deployment-branch-policies --jq '.total_count == 1 and ([.branch_policies[] | {name,type}] == [{"name":"main","type":"branch"}])'`.
  Re-check secret count `0`. **Any mismatch = abort and leave the secret unbound.**
- **G2 — bind.** Stream custody value on stdin to `gh secret set SYNTHETIC_ALERT_WEBHOOK_URL
  --repo TogetherWeOwn/two-bot-next --env synthetic-alerts`. Confirm count `0 → 1` by name only.
- **G3 — equality-gated production override removal (only).** Only if G0 recorded both values nonempty and
  exactly equal **and** executor-private before-state (exact name/value) was captured: `DELETE
  R-prod/variables/PRODUCTION_WORKER_URL` where `R-prod = repos/TogetherWeOwn/two-bot-next/environments/production`.
  Preserve the repository variable and all production protections. Otherwise skip with recorded reason.
- **Gate C — same strict read-back again immediately BEFORE the single dispatch.** Re-run both Gate B
  predicates verbatim. **Any mismatch = abort (secret stays bound under the intact main-only policy; no dispatch).**
- **G4 — single dispatch from `main`.** `gh workflow run synthetic-user-flow.yml --repo TogetherWeOwn/two-bot-next
  --ref main -f force_fail=true` — **exactly once**. Resolve and record the run URL; record job conclusions.
  Proof of delivery (ops-channel ALERT timestamp) is verified by the Operator on [TOG-27759](/TOG/issues/TOG-27759)
  through the existing authorized read path — a successful sender step alone does not establish it.
- **G5 — scheduled acceptance (Operator, not executor).** Confirm one scheduled (cron) run on `main` for both
  jobs plus both probe artifacts; prod stays passive/read-only (never force-fail prod). Manual force-fail runs
  are excluded from incident history, so a later green cron sample will not send RECOVERY for the manual proof.

### 6b. Verb table (finding 14 — every network mutation named)

| Step | Method + endpoint (all under `repos/TogetherWeOwn/two-bot-next`) | Effect |
|---|---|---|
| G0 | `GET environments/synthetic-alerts`, `GET environments/synthetic-alerts/deployment-branch-policies`, `GET environments/synthetic-alerts/secrets` (names only), `GET actions/variables/PRODUCTION_WORKER_URL`, `GET environments/production/variables/PRODUCTION_WORKER_URL` | reads only |
| G1a | `PUT environments/synthetic-alerts`, `POST environments/synthetic-alerts/deployment-branch-policies` | the only policy writes |
| Gate B / Gate C | same GETs as G0 policy/secret reads | reads only, abort on mismatch |
| G2 | `gh secret set` (PUT secret, value on stdin) | one secret write |
| G3 | `DELETE environments/production/variables/PRODUCTION_WORKER_URL` (armed only) | at most one var delete |
| G4 | `POST actions/workflows/synthetic-user-flow.yml/dispatches`, then `GET actions/runs` (record URL) | one dispatch + reads |

No other endpoints are reachable from the fixed runbook. Purpose split (finding 11): the branch policy
restricts the *secret's exposure scope*; the dispatch tests *alert delivery*. `--ref main` is per the
task contract (single dispatch from `main`); the run URL + SHA are recorded, never presented as a measured
deployed revision.

## 7. Rollback (restriction-preserving; never removes the gate)

| Step failed | Rollback |
|---|---|
| G0 / G1a / Gate B (pre-bind) | No live change to undo (G1a either completed to the exact target state or aborted before writing). Leave the restriction in place. |
| G2 (bind) fails | Record state; retry only via a fresh approved request (never auto-retry). |
| G3 (override delete) applied, later failure | `POST` the exact override name/value from executor-private before-state to the `production` environment. |
| G4 (dispatch) fails | Preserve environment, policy, and secret; record state. No automatic deletion. |
| Any gate mismatch | Sanitized refusal receipt; secret left unbound (pre-bind) or bound under intact main-only policy (Gate C). |
| **Forbidden rollbacks** | Deleting the `main` policy, setting `deployment_branch_policy` to null, weakening reviewers/protection, deleting run/message evidence, rotating or deleting the secret to "undo". Each requires its own new approved action. |

## 8. Explicit non-goals (refused by this spec)

No deploy, restart, rotation, production failure injection, new spend, second dispatch, broad sudo, generic shell
executor, catch-all GitHub admin grant, SHADOW removal, guard disablement, or policy-gate change. `human_only`
and `spend` are `false`; any such request is refused (exit 3).

## 9. Verification before provisioning and before execution

- Source: `bash -n` on the runbook; executor suite `./test_op_approval_executor.sh` → `54 passed, 0 failed`;
  `sha256sum` re-pinned at the exact merging head; `verify-only` against the built request+approval reports the
  full chain with `shadow=present` (would-run vs shadow-blocks distinguished without executing).
- Host (read-only): `test -f /etc/paperclip-operator/SHADOW && echo SHADOW-present`;
  `ls -l /usr/local/sbin/op-*` (root-owned, no group/other write).
- GitHub (read-only, names only): environment/policy/secret-count reads; repo + `production` variable presence
  reads without printing values; `gh run list --workflow synthetic-user-flow.yml` baseline.

## 10. Provisioning (Operator host work, only after CISO approval of this spec)

**Install guard (finding 16): do NOT install this entry before the CISO approval is recorded on the review
card. The JSON in sec. 3 is a review artifact until then — copying it into the live allowlist early is a
policy violation, not a shortcut.**

1. Write `/usr/local/lib/paperclip-operator/runbooks/github-synthetic-alerts-bind-v1.sh` implementing **only**
   sec. 6–6b (root-owned `755`, no group/other write). The runbook source is reviewed as part of the CISO
   approval; at install time record its `sha256sum` in the provisioning comment (finding 17 — the allowlist
   schema addresses scripts by path, so the hash pin lives in the approval/provisioning record, and the
   executor's root-owned/non-writable check plus `verify-only` re-confirm it before any execution; post-approval
   edits would require a fresh approval because the recorded pin would mismatch).
2. Add the sec. 3 entry to root-owned `/etc/paperclip-operator/allowlist.json` (bump nothing else),
   filling in no values beyond sec. 3–4.
3. Run `verify-only` with the built request + CISO-approved approval file; confirm `shadow=present`, hash/expiry/
   custody chain green. Leave SHADOW in place. No execution until the separate execution approval + SHADOW-removal
   authority exist.

## 11. Residual risks accepted by design (adversarial-review disposition, findings 1/4/5/6/12/13/15)

- **Sequence lives in the fixed runbook, not the executor** (finding 1). The executor constrains action id +
  exact args + approval + expiry; ordering/gates are enforced by the root-owned, source-reviewed, hash-pinned
  runbook. That is the executor's designed trust boundary, not a gap in this spec.
- **Gate read-backs are check-then-act without a distributed lock** (finding 4). Mitigation: single-threaded
  runbook, gates seconds before the guarded write, abort on any mismatch. A concurrent admin mutation inside
  that window is a residual risk, detectable post-hoc (receipt records both read-backs + write results).
- **"Abort leaves the secret unbound" means the secret** (finding 3). A pre-bind abort after G1a leaves the
  main-only restriction in place — that is the required behavior (rollback must not remove the restriction),
  stated here so no reviewer mistakes it for a side effect.
- **Post-G2 timeout wedges retry by design** (finding 5). Gate B's count-0 precondition will refuse a naive
  re-run; resume requires a NEW approved action (fail-closed). The half-bound state (policy + secret, no proof)
  is the safe direction to be stuck in.
- **"Rollback" here is scoped restoration + preservation** (finding 6), per the task's explicit rule that
  rollback must not remove the restriction. Only the prod-override delete is restored; everything else is
  preserved and recorded.
- **`force_fail: "true"` is the only permitted input** (finding 12) because the task mandates exactly one
  forced proof dispatch; the non-alert (green) path is already proven by scheduled runs on record. `spend:false`
  is the executor's money-spend class flag, unrelated to Actions minutes. Success is NOT claimed at dispatch:
  delivery proof (G5) is verified by the Operator afterwards.
- **A fresh approved request re-runs** (finding 13). The replay guard is per-`request_id`; cross-request
  duplication is prevented by the fresh-approval requirement plus Operator run-count verification — not by the
  executor. Single-dispatch holds within one execution (fixed script, no loops/retries).
- **Humanness lives in the approval, not the run** (finding 15). `human_only:false` is required because the
  executor cannot delegate humanness; the human decision is the CISO/CEO approval of this exact binding.
  Four privilege domains in one action is why this needs CISO review — not a reason to split it into four
  unreviewed ones.
- **Before-state holds only the non-secret URL var value** (finding 8), root-only in the processed dir, used
  solely for POST-restore. The webhook secret value is piped (custody → `gh secret set` stdin) per the
  executor's CUSTODY model and never persisted, printed, or placed on argv/env.

## 12. Ask for the CISO (new authority — CISO/CEO decide once)

Approve this registered-action spec as the single executable contract for the [TOG-27759](/TOG/issues/TOG-27759)
binding/proof, including: action id + exact args (sec. 3–4), 24h hash/expiry binding, stdin-only custody from the
existing Worker `OPS_ALERT_WEBHOOK_URL` custody with name-only verification (sec. 5), serialized main-only gates
with abort-on-mismatch (sec. 6), restriction-preserving rollback (sec. 7), and the sec. 8 refusals. Alternatively
record an explicit denial naming the missing grant and the legitimate grant process. This review grants no
execution by itself; provisioning (sec. 10) follows approval.

---
*Skills used: paperclip, paperclip-superpowers-adapter, security-and-hardening, doubt-driven-development, verification-before-completion.*
*Cross-model second opinion skipped: non-interactive Paperclip run (per doubt-driven-development). Fresh-context single-model adversarial review applied before finalizing (see card thread).*
