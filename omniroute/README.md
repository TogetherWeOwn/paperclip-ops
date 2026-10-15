# OmniRoute operator tooling

Scripts that an **operator** runs against OmniRoute's management surface. They live here
because an agent run cannot execute them: `:20128/api/*` returns `403 AUTH_001` to an agent key,
and management is a separate *token class*, not a scope an agent key can be granted. That has been
confirmed independently three times (case_552a1655, case_d06b6dba, case_31532730).

They arrived in this repo through case_57a6e928, which resolved step 1 of case_9c11948c: eight runnable files
were staged in `/paperclip/operator-handoff/` whose content existed in no commit — no review, no
history, no rollback, for code the operator installs and runs. **case_64b2e78f added `case_05be2d4e-apply.sh`**,
the last unversioned OmniRoute tool in the channel, on the same terms.

## The standing rule

> **A runnable file in the handoff channel must be byte-identical to a blob committed on `main`.**

Land the PR **first**, then drop the mirror. A drop is a mirror of a committed artifact, never the
source. Full rationale in [`../docs/operator-handoff-channel.md`](../docs/operator-handoff-channel.md);
`../channel_drift.sh check` enforces it on content, not path, so these files may be renamed or moved
here without breaking a compliant drop.

## What is here, and whether it is live

| file | source issue | mutates? | mirrored to the channel? |
|---|---|---|---|
| `case_d06b6dba-register-teamclaude.sh` | case_d06b6dba (`in_review`) | **yes** — registers a provider | yes |
| `case_d06b6dba-verify.sh` | case_d06b6dba (`in_review`) | no | yes |
| `case_31532730-register-cliproxy.sh` | case_31532730 (`in_review`) | **yes** — registers a provider | yes |
| `case_fc6b420b-conn-counts.py` | case_fc6b420b (`cancelled`) | no — DB opened `mode=ro` | yes |
| `case_adae7506-bakeoff-harness.sh` | case_adae7506 (`done`) | no | **no** |
| `case_adae7506-assemble.py` | case_adae7506 (`done`) | no | **no** |
| `case_05be2d4e-apply.sh` | case_05be2d4e (`blocked`) | **yes** — creates 52 combos + 52 mappings | yes |
| `case_30ade3fb-snapshot-sections.py` | case_30ade3fb (`in_review`) | no — pure functions over an already-fetched response | **no** — a library, see below |

"Mirrored" is a separate question from "committed". Everything here is committed and reviewed. Only
the five with live operator work are re-dropped into the channel; staging a tool the operator has no
reason to run is how the channel filled up in the first place. (This sentence read "the four" while
the table already listed five mirrored files — corrected under case_30ade3fb.)

### `case_30ade3fb-snapshot-sections.py` — a library, and a partial answer

case_30ade3fb asks to extend `omniroute_snapshot.py` so the snapshot channel carries `combos` and
`model_combo_mappings`. **That file is not in this repo, not in any commit, and not anywhere on this
box** (`find / -xdev -name 'omniroute_snapshot*'` returns nothing). It runs host-side and keeps the
master key there deliberately (case_2963068b). So the extension could not be written as a diff, and writing
one against a reconstructed-from-memory copy would have produced a patch validated only against its
author's guess at the original.

What is committed instead is the half that carries the entire risk — the two redaction builders —
with the transport left host-side and untouched. The operator wires them in; the docstring gives the
two supported call routes and the one that does not work (`import` fails on the hyphenated filename;
verified, not assumed).

The design turns on one fact read out of omniroute's shipped source, not inferred:
`comboRuntimeConfigSchema` ends in `.passthrough()` (`src/shared/validation/schemas/combo.ts:279`)
and the repository persists `JSON.stringify(combo)` whole (`sqliteComboRepository.ts:207`). A combo's
`config` therefore accepts and stores **arbitrary unknown keys**. A denylist over that cannot be made
safe — it removes only the names somebody thought of — so these builders are a **structural
allowlist**: ~20 named fields copied by exact key, the whole remainder dropped unread and reported by
name in `_unknownKeysDropped`. Free text (`system_message` ≤50k, step `prompt` ≤20k, `description`)
is reduced to presence + length + sha256, never copied. That is a deliberate fidelity loss: the
channel proves *whether* a protected combo changed, not what its prompt says.

```sh
python3 case_30ade3fb-snapshot-sections.py --selftest      # 28 hostile-fixture checks, exit 1 on leak
cat raw.json | python3 case_30ade3fb-snapshot-sections.py --build - --kind combos
python3 case_30ade3fb-snapshot-sections.py --audit built.json
```

`--build` has **two** gates, answering different questions, and both print nothing when they fire —
the operator pipes stdout straight into the 0644 file, so a gate that emits the bad document *and*
returns non-zero has already lost. **Exit 1** is confidentiality: the section was re-audited and
something credential-shaped survived redaction. **Exit 3** is integrity: the section audits perfectly
clean but carries a `_WARNING`, meaning some part of it is *empty by failure rather than by fact* —
an unrecognised envelope, a partial page, or a `models`/`config` that arrived with the wrong type.
Publishing that would put a false clean baseline in the channel, which for a proof channel is worse
than a crash, because nobody sees it.

Three properties worth keeping when editing: the auditor is proven able to *fail* (it reports
findings against the unredacted fixture, clean against the redacted one — a checker that always
passes is worthless); `absent` is reported separately from `empty`, because restoring an absent key
means deleting it while restoring an empty one means writing `""`; and a malformed container is
**digested rather than dropped silently**, so a rewritten step list still changes the bytes.

One limit, stated plainly: `SECRET_PATTERNS` is an allowlist of *known* shapes (OpenAI/Anthropic,
JWT, bearer, GitHub, AWS, Google, Slack, long opaque runs) and it only sees content in positions the
structural allowlist copies. A credential pasted into an allowlisted scalar — a combo `name`, a
`strategy` — passes clean if its shape is not listed. The structural allowlist is the real control;
the auditor is a backstop, never a proof of absence.

**Not mirrored to the channel**, because it is a library with no operator step of its own. It gets
mirrored if and when the host-side tool is actually wired to it.

### The case_adae7506 pair, committed but not mirrored

`case_adae7506-bakeoff-harness.sh` and `case_adae7506-assemble.py` are the eight-arm bake-off instrument.
**That measurement was descoped and cancelled** (case_fc6b420b, 2026-08-24 15:17Z). case_500274db closed without
it, deriving its recommendation from `omniroute@3.8.49` source instead, and case_664812c0 already holds
that recommendation. Nobody is going to run these arms.

They are committed anyway because — unlike the rest of the bake-off cluster — they exist in **no**
work-product attachment. Measured by sha256 against every artifact on case_552a1655/207/208/214, the staged
copies matched nothing: the harness is 64,428 bytes against archived Revs 3/4/5/6/7 at
27,411 / 30,899 / 42,509 / 59,473 / 60,782. Deleting them would have destroyed the only copy. So they
get history and rollback here, and they stop being staged for an operator who has no use for them.

### Why `case_fc6b420b-conn-counts.py` is here at all

case_fc6b420b is cancelled, which made this the obvious file to throw away. It is not, because it has a
live consumer that the cancellation did not touch: `case_31532730-register-cliproxy.sh` cites it as the
attribution reader for step 3, and case_31532730 is `in_review`. It is also standalone — four subcommands,
`selftest` / `counts` / `affinity` / `report`, none of which need the harness at runtime — and it is
read-only by construction (`sqlite3.connect("file:...?mode=ro")`).

It also carries knowledge nothing else does: five defects (D-1…D-5) in the SQL the harness shipped,
including a G8 query that returns a clean `0` unconditionally because `session_account_affinity` is
created by migration 050 and never written by the runtime, which stores affinity in `key_value`.

**Set `case_3efd6c61_PROVIDER` when you use it.** It defaults to `opencode-go`. Against cliproxy the default
filters out every row and prints "zero rows", which reads as *attribution did not land* when it means
*wrong provider filter*. Run `selftest` first — it refuses on a schema it cannot serve rather than
returning an empty result that looks like a measurement.

### `case_05be2d4e-apply.sh` landed byte-identical, on purpose

It is the only file here that was committed **without a single character changed**, including a
comment block its own author has since shown to be wrong. That is not laziness; it is the point.

`case_05be2d4e-apply.sh` is item 4 of the batched operator runbook card `f953a82e` on case_48b26cc2
(`in_review`). Its nine-file SHA-256 manifest was published in the case_05be2d4e thread — the one link in
this chain an agent cannot rewrite — and the card tells the owner they may set
`case_3531a34b_SHA_CONFIRMED=yes` on the strength of it. The script refuses to run until that attestation
is made. Re-derived at 03:42Z on 2026-08-24, and again for case_64b2e78f on 2026-08-25: **9/9 still hold.**

Editing a comment changes no behaviour but *would* break that chain and force the owner through
another verification round. So the bytes are the attested bytes, and the drop went compliant with
zero writes to the channel — nothing was re-staged, because nothing needed to be.

**The known-stale part, so nobody rediscovers it as a defect.** The phase 4 comment block carried
*"case_be6c5238 is still open. These combos are sound for streaming callers only."* Both halves are wrong:
case_be6c5238 is `done` (2026-08-23, root-caused to OmniRoute's `diagnostics.ts:176`, not OpenRouter).

**This paragraph itself asserted `0 of 52` terminal legs are affected. That was false, and it was
the wrong *shape* of claim. Corrected 2026-09-03 (case_156388b9); do not reinstate a per-model count.**

The count was false on its own terms: `case_05be2d4e-combo-legs.tsv` column 3 contains
`openrouter/z-ai/glm-5`, which case_be6c5238 §3a measured at **502** non-streaming. Eight further legs are
untested versions of the same families (`glm-5.1`/`5.2`/`5.2-high`/`5.2-xhigh`/`5.3`,
`minimax-m2.5`, `minimax-m2.7`, `minimax-m3`).

But `9 of 52` is not the fix, because **affectedness is not a property of the model.** The defect
fires when a non-streaming response arrives with empty `content` because `max_tokens` truncated a
reasoning model before its first content token — a property of the individual *call*. case_be6c5238 §3b
measured `kimi-k2-thinking` at 502 twice and 200 once **on identical input**, and §3c drove every
model in the 502 column to 0/3 502 simply by raising `max_tokens` to 600. Any census of this bundle
is a number that changes between two runs of the same probe.

The condition to state instead, and the only one that is stable:

> A terminal leg here is at risk **only** for a caller that sends `stream:false` with a `max_tokens`
> small enough to truncate a reasoning model before its content begins. Raising the caller's
> `max_tokens` removes the risk; changing which model the leg names does not.

Two honest limits on this bundle's evidence, unchanged by the above: 48 of the 52 terminal legs have
never been called in any shape, and phase 4 probes **streaming** (`stream:true`), so a green phase 4
would not detect a non-streaming failure anyway.

Phase 4's own probe is correct as written and **must not be "fixed"**: it is `stream:true`
(`case_05be2d4e-apply.sh` phase 4), and the 502 requires `stream:false`. Raising its `max_tokens` would
change a working probe to chase a bug it cannot reach. The same holds for
`case_adae7506-bakeoff-harness.sh`, whose single completion probe is `stream:true` behind guard G3.

Fold the correction into the file on the first commit that has a behavioural reason to touch it, and
re-publish the manifest in the same breath.

**Its five data companions are committed too** — `case_05be2d4e-combo-specs.json`,
`-mapping-plan.json`, `-combo-allowlist.txt`, `-combo-legs.tsv`, `-containment-proof.json`. The
driver is a thin orchestrator; the *specs* are the change being applied to a shared instance, and
they deserve review and rollback more than the code that reads them does.

## Operating rules that apply to every Claude-serving node here

Both were established by case_e793f47a and cost real money to learn:

**A. Do not press the connection "Test" button in the OmniRoute UI.** Its fallback probe is a
system-less Sonnet call — the exact shape that hangs — and a hung request holds a shared upstream
slot, degrading Claude for every company on the box. Use `case_d06b6dba-verify.sh` instead; that is what it
is for.

**B. Do not set `system_message` on any Claude combo.** Anthropic's OAuth gate requires the first
system block to be the Claude Code identity string byte-for-byte. OmniRoute's override strips every
system message and prepends its own, which is precisely the shape measured as hanging. Leaving it
unset is a provable no-op, not a hopeful default.

**C. Address combos by name, never by a bare model id.** A bare Claude id on the completion surface
resolves to some other provider and returns a misleading `200`. Combo names `404` instead of falling
back, which is the property that makes them safe to assert on.

## What CI does and does not prove

CI `bash -n`s every `.sh` here and `py_compile`s every `.py`. A parse error in an operator-run
script would otherwise surface in front of a human doing an install, which is the worst place to
find one.

case_dd69744c added one narrow exception to parse-only coverage: `../test_case_8968f48d_artifacts.sh` runs the two
case_d06b6dba scripts against a stubbed `curl`. It pins the unknown-argument refusal, the dedicated
`claude-catchall` combo, the live `.items` mappings envelope and verifier section B4. Registration
stays dry-run and the verifier receives no `OMNIROUTE_API_KEY`, so the suite performs neither a
management mutation nor inference.

That suite proves the artifact contract in isolation. It does **not** prove a live registration would
succeed, that the management API still has the same behaviour, or that teamclaude answers through
the configured combos. Those remain operator-run checks.

### Which scripts here are registered in CI, and why the rest are not (case_46ada0d8)

Until case_46ada0d8 **no `omniroute/` script was executed by CI at all** — measured on `main` at
`33e8b427`, `grep -cE 'omniroute/[A-Z]' .github/workflows/ci.yml` returned `0`. That was a class
gap, not a one-file oversight, so the answer is written down here per file rather than left as
silence. An unregistered suite is indistinguishable from no suite: it passes because somebody ran it
by hand once, and it keeps "passing" after it rots.

**Registered and executed in the Offline job:**

| Script | Step | Why it can run |
| --- | --- | --- |
| `case_30ade3fb-snapshot-sections.py` | `--selftest`, plus `verification/case_46ada0d8-snapshot-redaction-mutation-gate.sh` | No network, no credential; shells out only to `sys.executable` on itself. |

`case_30ade3fb-snapshot-sections.py` is the one that most needed it. It is a **library with no operator
step of its own**, so nobody has a routine reason to run it — before case_46ada0d8 the first execution of
its 28 checks could have been the run that appended to the 0644
`/paperclip/operator-handoff/omniroute-snapshots.jsonl`.

**Not registered, with the reason measured rather than assumed:**

- **`case_d06b6dba-verify.sh` — registering it would ADD A FALSE GREEN, which is worse than no step.**
  Run with no credentials it exits **0** with `pass=0 fail=0` and three `SKIP` lines. It does not
  fail closed, so a CI step calling it would go green forever while verifying nothing. It reaches
  teamclaude, the management API and `:20129`, none of which a runner can see. Its artifact contract
  is already covered offline by `../test_case_8968f48d_artifacts.sh` (case_dd69744c), which is the right shape.
- **`case_d06b6dba-register-teamclaude.sh`, `case_31532730-register-cliproxy.sh`** — both refuse without
  `--apply` and print usage (exit 0), but the only path worth testing is the one that performs a
  **live management mutation** with `OMNIROUTE_MGMT_TOKEN`. case_dd69744c already covers the dry-run and
  unknown-argument refusals against a stubbed `curl`.
- **`case_07c389cd-verify-chain.sh`** — needs `OMNIROUTE_API_KEY` and a live Claude Code → OmniRoute →
  CLIProxy → Codex chain. There is nothing to stub that would leave the assertion meaningful.
- **`case_05be2d4e-apply.sh`** — deliberately byte-identical to the operator artifact (see above); it
  drives the management API against all 91 leaf ids. Editing it to be CI-runnable would break the
  published nine-file SHA-256 manifest, which is the whole point of that file.
- **`case_fc6b420b-conn-counts.py`** — verified by attempting it: `selftest` exits **2** with
  `could not locate storage.sqlite`. Its assertions are deliberately made against the **live**
  omniroute database (that is what makes them evidence rather than claims), so a fixture DB would
  invert its purpose.
- **`case_adae7506-assemble.py` / `case_adae7506-bakeoff-harness.sh`** — the bake-off was descoped and cancelled
  (case_fc6b420b). `case_adae7506-assemble.py` does run offline, but with no inputs it emits an empty skeleton
  (`"arms": []`), so a CI step would assert nothing about a measurement nobody will take. Kept for
  history and rollback only.

The standing rule for anything added here: **if a script can run with no network and no credential,
register it in the Offline job in the same commit that adds it.** If it cannot, add a row above
saying why. A script that exits 0 without asserting anything — the `case_d06b6dba-verify.sh` shape — must
not be registered until it fails closed.
