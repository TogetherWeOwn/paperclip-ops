# Shared review-only fence: offline acceptance handoff

These are **tests before implementation**, not a deployed fence or approval to
mutate a mirror. The restricted subject is `TogetherWeOwn/CLIProxyAPI#1` (ASCII
case-insensitive, whitespace-trimmed repository identity, exact integral PR).
The restriction is independent of head, draft, CI and review score. Hypothetical
green/five-of-five fixtures are not fresh GitHub evidence or a changed verdict.

Source baseline: `166106f5fc3aadd5a2b25a485de225a4dd254671` in
`TogetherWeOwn/paperclip-ops`. No production implementation is included here.
Preserve these acceptance files in the paired implementation branch. Suspected
acceptance errors require an author correction, not deletion, skips or weaker
assertions. Do not open a standalone ready tests-only PR.

## Run

Use an existing run-owned `PAPERCLIP_RUN_SCRATCH_DIR` or
`PAPERCLIP_SCRATCH_DIR`. No credentials, service containers, dependency installs,
GitHub requests or live timers are needed for these suites.

```sh
python3 -B tests/pr_action_fence/run.py \
  --summary-json "$PAPERCLIP_RUN_SCRATCH_DIR/fence-acceptance.json"

# Diagnostic harness/positive controls only; NOT acceptance completion:
python3 -B tests/pr_action_fence/run.py --controls

# Detailed Python failures (all external transports remain trapped):
python3 -B -m unittest discover -s tests/pr_action_fence -p 'test_*.py' -v

# Detailed Node failures; the preload MUST precede production imports:
TMPDIR="$PAPERCLIP_RUN_SCRATCH_DIR" node \
  --import ./tests/pr_action_fence/offline-preload.mjs \
  --test tests/pr_action_fence/*.test.mjs
```

`run.py` returns nonzero for **any** acceptance failure. It has no
expected-failure-to-success mode. Its optional JSON output contains test names
and counts, not raw subprocess argv, credentials, private paths or API bodies.
Detailed raw baseline failures can quote existing source's transport argv; keep
those local, not in a public artifact or PR comment.

Initial full run: **143 top-level tests, 19 passed, 124 failed, zero test errors
and zero skips**. Python: 64 methods, 6 passed, 58 failed methods (62 assertion
records including subtests). Of those failures, 22 methods exercise existing
public senders and 36 identify not-yet-implemented evaluator/error/integration
contracts. Node: 79 tests, 13 passed, 66 failed. The intentional RED is not solely
missing imports: actual protected sender/writer calls reach fake transports.
No green implementation or installed-runtime claim is made.

## Safety and non-vacuity

- Python tests clear ambient credential variables and trap sockets, urlopen,
  `Popen`, and shell fallback. Only explicitly installed in-memory transport
  doubles run. The trap itself has a passing self-test.
- Node's preload runs before production imports, clears credentials, and traps
  fetch/HTTP(S)/sockets/DNS/datagrams and process fallback. An exception is only
  the canonical pure local Python evaluator, invoked with bounded argument
  arrays, no shell, sanitized environment, and its own Python network/process
  traps. No credential helper or real `gh` can execute.
- `gh` spies accept only the injected fake runner, validate bounded/no-shell
  invocation and record actual mutation calls. Fixture scratch is run-owned.
- The same real consumer functions run for protected and unrelated subjects.
  Unrelated controls reach the fake mutation path; existing HOLD/CI attempt
  gates still suppress when appropriate. A blanket deny cannot pass.
- Inputs are fresh copies (Python) or recursively frozen (Node). Fake clocks
  and fixed head values make duplicate/stale work reproducible.
- The hygiene script has import-time effects in the baseline. Its token command
  and all I/O are inert doubles. Only its legacy owner-reference discovery
  regex is substituted with an opaque fixture reference; real owner matching
  and final PATCH execution remain real. Capture reuses an existing opaque
  classifier fixture rather than publishing new board provenance.

## Real public boundaries exercised

| Suite | Real production boundary and observable oracle |
|---|---|
| `test_python_boundaries.py` | Controller `post_wake`, initial/repeated/pre-policy delivery; alternate CI tick and direct `_post`/`_rerun`; supply owner comments, assigned action cards, pending/direct attachments and direct card writer; company-wide review-error intake and direct sender; orphan intake and direct card writer; hygiene generation and independently called final PATCH. Protected mutation count = zero. |
| `readiness-boundaries.test.mjs` | Readiness generator, repeated event consumption, HOLD body maintenance, sweep entry, four directly callable GitHub writers; independent closeout planner, direct armer and confirmation path. Protected body/ready/draft/auto-merge count = zero; no confirmation after denial. |
| `wake-boundaries.test.mjs` | Capture event sender and independent board comment transport; tracker automatic digest, manual wake and directly injected queued `deliver`. Protected comment/wakeup count = zero. |
| `test_offline_and_errors.py` | Containment self-test and missing/timeout/malformed/runtime helper failures at independently callable Python final senders. Unrelated subjects must also fail closed on evaluator failure. |
| `test_policy_contract.py` | One evaluator/bundle authority; all six actions; exact normalized subject vs PR 2/10/11 and similar names; malformed context and schema; digest/file safety; one-line CLI decisions and exits 0/10/20; diagnostic receipt cannot become authorization. |

For every named final boundary, its direct invocation case is the
**guard-removal mutation oracle**, separate from generator filtering. Once the
guards exist, bypass/remove each final guard in an isolated scratch mutant and
run its named case: it must fail at the mutation-count assertion. Do not mutate
the policy bytes without re-binding the fixture manifest and call that a guard
removal: fail-closed digest validation would hide the intended mutant. No such
post-implementation mutant run is claimed by this red-before handoff.

## Acceptance interface contract for the implementation

These small seams specify trusted context/test injection, not alternate policy
authorities. They are deliberately absent at the baseline.

### Python

- `pr_action_policy.evaluate(bundle, repository, pr_number, action,
  expected_head, observed_head, consumer)` returns a dict or decision object
  with `decision` equal to `DENY_REVIEW_ONLY`,
  `DENY_INVALID_POLICY_OR_CONTEXT`, or `NOT_RESTRICTED`.
- `load_bundle(release_dir=..., expected_manifest_sha256=...)` is explicit
  **test fixture injection** for strict loader tests. Runtime callers resolve
  their installed immutable release, never an environment/CLI replacement.
- The test release's JSON manifest is
  `ops/productivity/pr_action_release.json`: `schema_version: 1`,
  `source_commit`, and `files` mapping release-relative paths to SHA-256.
  Policy/evaluator paths are the proposed canonical productivity paths. The
  expected manifest digest is supplied separately. Fixture digests bind actual
  copied evaluator bytes; fixtures are not approved installation requests.
- Final Python senders accept `pr_context=` containing `repository`,
  `pr_number`, `expected_head`, `observed_head`, `action`, `consumer`, and any
  carried `policy_digest`. Baseline tests inspect the signature only to still
  execute old senders without a TypeError. This fallback grants no permission:
  baseline failures assert actual fake writes. Once integrated, missing context
  must deny, not activate a legacy bypass.
- The CLI uses separate arguments `--repository`, `--pr-number`, `--action`,
  `--expected-head`, `--observed-head`, `--consumer`; one JSON receipt on stdout;
  exits 0 only for valid `NOT_RESTRICTED`, 10 for review-only, 20 for invalid/error.
  Strict decimal CLI conversion does not make Python booleans/strings valid PRs.

### Node

- Writers accept a final PR-context object: `repository`, `number`,
  `expectedHead`, `observedHead`, `consumer`, optional carried `policyDigest`.
  `updatePullRequestBody` puts it after body; `enableAutoMerge` and the separate
  armer after the existing head argument; ready/draft after number.
- Existing writer head arguments remain exact-head preconditions; refresh
  trusted identity/head read-only before final execution. A receipt/context is
  not an authorization token. Drift fixtures also change the fake live head.
- Constructors use optional `policyRun(file, argv, options)` test injection for
  the **shared CLI subprocess**, distinct from the GitHub `run` transport.
  Error doubles never implement a repository matcher; they model ENOENT,
  nonzero, malformed output and timeout. Default production behavior uses the
  release-bound helper. Missing/failing policy denies unrelated subjects too.
- Planner/runner denial records and direct-writer results/errors must include
  the explicit decision code. A thrown random transport/schema exception or
  inventory no-op is not a review-only receipt.

## Explicit NON-RUNTIME integration contracts and installation HOLD

The source baseline does **not** contain these canonical effective consumers:

- `ops/productivity/pr_controller.py` (full classifier, six-hour controller
  constant and installed dispatch call sites);
- `ops/productivity/pr_automerge.sh`;
- `ops/productivity/pr_pump.py`;
- `ops/productivity/shepherd/tick.sh` and `ops/productivity/shepherd/triage.py`;
- a dedicated direct-merge or queue-submit/retry/final-merge implementation.

Existing controller patches and their synthetic fixtures do not close this
source gap. The queue health probe is not an enqueue consumer. No private tree,
installed executable or host receipt was fetched or reconstructed here.

`test_missing_integration_contract.py` is explicitly **non-runtime**. Its
prospective `ops/productivity/pr_action_integration.py` facade must bind
`offline_boundary(name)` to reviewed sanitized canonical source, or a focused
exact-baseline integration packet. Required names are controller initial,
draft-idle and repeat; legacy arm; pump/shepherd direct merge; queue submit,
retry and final merge. Each descriptor must carry `coverage`,
`runtime_integrated: false`, source/packet path and SHA-256, callable `invoke`,
and (for packets) before/after SHA-256, exact anchors and `no_fuzz: true`.
A descriptor or synthetic driver alone is **not** canonical source or proof of
an installed runtime.

The facade's injected invocation is `invoke(work_item, resolve_pr=..., transport=...)`.
It must execute the mapped boundary under test, not reimplement a toy matcher.
Tests assert zero writes at generation/consume/retry/final stages, exact work
identity/head/action/digest carriage, fresh identity reads, drift quarantine,
all direct merge variants, both queue ends, fail-closed helper errors, and valid
unrelated controls preserving existing gates. Exact-packet verification must
reject wrong anchors, fuzz and before/after digest faults.

Remediation/rollback functions are **offline planning contracts only**, exercised
with executor stubs. Their allowed operations are exact-subject
`disable_auto_merge`, `remove_queue_entry`, `restore_verified_fence_bundle`,
and `verify_denials`. Absence is a no-op; uncertainty/failure retains HOLD;
unchanged draft/head/unmerged state and null request/queue state need after
proof. No pre-fence restart, promotion, request recreation or HOLD release is a
valid rollback.

Until real baseline/source material is supplied, these cases fail with an
explicit coverage-gap assertion. Keep installation/publication HOLD and return
exact missing inputs for scoped admission if needed. Do not skip them, import
private source, pretend a toy fixture is deployed coverage, or certify a partial
universal fence. Passing all offline tests later still does not attest host
installation, external administrators or unknown integrations.

## Delivery gates

Required existing fast checks remain unchanged. This test-only handoff does not
waive the orphan-suite registration gate or authorize editing production/CI
policy. Before a push, run `context-pack --base origin/main` and required local
checks. Before implementation delivery, ensure this exact acceptance command is
executed in the existing required CI graph, then obtain green exact-head checks,
independent security approval and normal review gates. Keep an implementation
PR intentionally draft until its current-head independent security gate clears.
Never apply those lifecycle operations to the protected mirror.
