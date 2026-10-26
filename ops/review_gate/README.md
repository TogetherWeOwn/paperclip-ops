# Paginated review gate source port

This is a **source-only** port of the sanitized cursor-pagination gate and its
order-only fairness patch. It is not an installed controller or a new runtime
policy. Python 3.12 or newer is required. No installation, timer, review trigger,
provider call, credential acquisition or host change is part of this port.

## Layout and verification

The isolated directory preserves the supplied sibling imports without replacing
`ops/productivity/review_dedup.py`, whose public check-run API is different.
`pr_controller.py` is inventory-only, not the full controller. Do not install it
as a replacement for a full controller.

Run from the repository root:

```sh
python3 -B verification/review_gate_acceptance/run.py --source-dir ops/review_gate --legacy
```

Outside the agent runtime, first set `PAPERCLIP_RUN_SCRATCH_DIR` to an owned
scratch directory. The harness creates and cleans its own temporary child there.
CI supplies the runner temporary directory; the fast script prefers the owned
agent scratch variable and otherwise uses the standard temporary directory.

The original independent test handoff is commit
`01f091506a1ab14f8ad1b1b673359c9bd6cca27c`; the candidate-filter extension is
`0edec01706863b429b03cc0828dc2836c18e4b7a`. The implementation preserves all four
files under `verification/review_gate_acceptance/` byte-for-byte from that extended
handoff. The same 71-case command runs in the GitHub-hosted offline CI job and
`make verify-fast`.
The canonical `verification/offline_ci_contract.py --write` tool adds only this
new producer to `vendor/offline-ci-contract.json`; no existing producer pin,
job/default hash, gate or guard is removed or weakened.
The original sanitized baseline yields 45 passes and exactly two loudness
failures; the original port yielded 47 passes, no errors and no skips. The harness forbids
real process/network operations and isolates runtime state in scratch fixtures.
The baseline-only mutation driver intentionally refuses repaired candidate
hashes; its pins must not be updated to make it accept this port.

## Provenance and complete port delta

`source-map.json` records the original → sanitized → port SHA256 chain for each
shipped source member, the acquisition ZIP/manifest digests, and independent
test pins. The original files were not accessed by this port: original hashes
are the acquisition attestation, while sanitized/archive/port hashes were
verified locally. No private source, state, configuration, receipt or runtime
inventory is published. The seven unchanged sibling files (including `review_gate_runtime.py`) match
the sanitized bundle exactly. Only `review_gate.py` has an implementation delta:

- `main()` starts with an integer inventory status of zero.
- The existing repository-inventory exception branch sets it to one, keeps the
  existing named-repository failure log, and continues to healthy repositories.
- Every early return and the successful dispatch tail return that status. Thus
  later dedup/load/inflight/first-pass holds cannot erase an inventory failure.
- The normal CLI exits with `sys.exit(main())`. The optional docs dry-run CLI
  retains its own unchanged error status.

A failure never exposes accumulated partial PR inventory to candidate selection,
dedup, dispatch or requested-head state. Healthy repositories remain eligible;
this is not a global abort or a new alert. Status 1 means a repository inventory
failed, even if a healthy repository's batch was successfully delivered. Status
0 means no such failure was caught, **not** that a batch was sent or that all
other baseline guard/transport operations succeeded. Existing non-inventory
error handling and logging are deliberately unchanged.

The sanitized pre-fairness snapshot and neutral fairness diff are comparison
artifacts only, not additional executable entrypoints in this port. Their hashes
remain in `source-map.json`; the exact `fair_order()` algorithm is preserved.

### Acquisition redaction/interface mapping retained by this port

| Surface | Sanitization delta | Behavior retained |
|---|---|---|
| All code | Historical comments/docstrings removed; neutral headers and AST-unparsed formatting | Executable algorithms; no private operational prose |
| Runtime identities | Company, endpoint, reviewer and executor become neutral environment-backed UUIDs; reviewer SQL uses the same constant | Same principal relationships/predicates; no authority granted |
| Focus/ranking | Neutral configurable focus/rank examples replace private inventory | Pin/rank precedence, same-rank round robin and within-repository order |
| Runtime paths | Root, stops, state, log, findings, context, first-pass, usage and disk-root paths become neutral configurable paths | Same read/write points and STOP checks; no state copied |
| DB transport | Private command prefix becomes `runtime.DB_COMMAND`, failing by default | Same tabular arguments, SQL predicates, parsing and differing error behavior |
| GitHub transport | Private token-helper command becomes `runtime.TOKEN_COMMAND`, failing by default | Same token environment and call-site timeout/return handling; no credential supplied |
| Dispatch | API base/curl-config, project selector and request text become neutral parameters; historical transport prose removed | Same batch identity, issue body, receipt checks and state marking only on matching delivery |
| Findings helper | Owner/path/principal/transport parameters; unused standalone CLI omitted; one diagnostic's provenance removed | All gate-used classification, context, postcheck and late-novel blocking behavior |
| Controller | Only imports, QUERY, gh and fetch_repository extracted | Both query strings and pagination function unchanged; unrelated controller actions absent |
| First-pass | Only the transitive read-only gate dependency surface extracted; disk-root parameterized | Same defaults, model labels, thresholds, caps, state keys and fallback decisions |
| Supplied tests | Neutral repository fixtures; unrelated optional docs-suite loader omitted | All ten original gate and five fairness assertions retained |
| Optional docs lane | Lazy hook and exception fence preserved; absent dependency not fabricated | Unavailable-feature fallback only; optional live integration not established |
| Fairness patch | Neutral before/after diff regenerated during acquisition | Only fair_order insertion and replacement of the old sort; no bulk-list rewrite or scheduler aging |

Runtime settings are documented by `review_gate_runtime.py`. Its values are
placeholders, not deployable defaults. A separately authorized executor must
inject the **unchanged** private identities, focus/rank policy, paths and
transports through the existing configuration contract. Do not treat configurable
values as permission to change policy, models, caps, security or credentials.

## Candidate admission extension

The gate query now requests `PullRequest.isCrossRepository` and
`author { __typename login }` on every inventory page. Before candidate coverage
or dispatch, only an explicit boolean `False` and a known `User` actor with a
nonblank string login qualify. `Bot`, unknown actor types, and missing, null or
malformed metadata are skipped, not converted into favorable review evidence or
required-check exemptions. Actor type, not a login substring, identifies bots.
The commit-OID comparison remains an independent exact-head check; equal OIDs do
not establish that a PR comes from the same repository.

The extension changes only the gate query and this admission predicate. The
`source-map.json` port hashes above describe the original pagination port, not
the extended gate. The extended test file SHA256 is
`01cca1a41f433020cca68145840fc1f3e650b1ed7f8751edacfe05750b678908`.
Against the original port, the extended suite has 52 passing methods and 19 red
methods (45 assertion failures); with this filter all 71 methods pass. Existing
CI wiring already executes the full command; no workflow or gate change is needed.
Optional docs-lane integration remains unqualified as noted below.

## Preserved behavior and explicit limits

- Cursor `first:20`, at most 100 pages, PR-number deduplication, GraphQL-error
  rejection and invalid/repeated-cursor rejection are unchanged.
- Non-draft/exact-head checks, required/newest check interpretation, truncated
  check holds, completed/live/queued dedup, retry/backoff, state pruning, focus
  threshold, reviewer capacity/inflight and dispatch receipt guards are unchanged.
- The batch formula is still `min(15, max(4, 4*mcr - queued_n))`. Fair ordering is
  before first-pass filtering; the batch cut remains after that filtering.
- No direct final-review host-pressure fence exists in this baseline. Existing
  first-pass host/provider guards can fall back to normal review. This source
  port does not silently add a guard or claim that policy gap resolved.
- The optional docs-lane implementation/config/tests were not supplied. The
  import fence remains, but full optional-feature parity is not established.
- Offline fixtures are not live DB/provider, deployed-host, staging, service
  manager or production verification. Required current-head CI, independent
  review and applicable security gates remain prerequisites for merging.

## Undo and downstream boundary

Undo is a separately reviewed source revert. Merge does not authorize an install
or prove host acceptance. Installation, fresh exact-state fences, health checks,
tick evidence and byte-exact previous-file rollback belong to the separately
authorized executor under the existing machine contract. No install helper or
new runtime scheduling is shipped here.
