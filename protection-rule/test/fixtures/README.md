# Producer contract fixtures (offline, not staging evidence)

The JSON files are byte-identical copies from Bot PR #566 at immutable pushed
revision [`16be69b2e3ac2225a2692ddc26bd3549bc10e750`](https://github.com/TogetherWeOwn/two-bot-next/commit/16be69b2e3ac2225a2692ddc26bd3549bc10e750):

- [`crates/cutover/tests/fixtures/staging-migrate-plan.json`](https://github.com/TogetherWeOwn/two-bot-next/blob/16be69b2e3ac2225a2692ddc26bd3549bc10e750/crates/cutover/tests/fixtures/staging-migrate-plan.json)
  SHA-256 of JSON bytes: `90cd28da18a68c0d5778c14f3bf02a87b32e8d0cb731e1c28a879168e074571e`.
- [`crates/cutover/tests/fixtures/staging-migrate-apply-claim.json`](https://github.com/TogetherWeOwn/two-bot-next/blob/16be69b2e3ac2225a2692ddc26bd3549bc10e750/crates/cutover/tests/fixtures/staging-migrate-apply-claim.json)
  SHA-256 of JSON bytes: `d9aa664cd887c96f1663f955944ba1c8b8679511da963e928db991cfc933c831`.

The publisher is [`scripts/ci/staging_migrate_claim.py`](https://github.com/TogetherWeOwn/two-bot-next/blob/16be69b2e3ac2225a2692ddc26bd3549bc10e750/scripts/ci/staging_migrate_claim.py).
Its golden CLI output is the claim fixture. The Rust pure-unit test
`claim_transport_projection_fixture_matches_rust` constructs synthetic SQLx
migrations with SQL `SELECT 1;` / `SELECT 2;`, uses the actual
`source_manifest` / `manifest_hash`, and compares the plan fixture.
These are producer-shaped **synthetic** vectors, not a migration run or a
staging integration pass. Producer review/CI remains independently required.

The projection digest is
`e4a3136346ca546df4f1f4f654322c278ff7df1d5902ec5685cd43036b41beea`,
NOT either JSON-byte digest above. The vector deliberately distinguishes source
SHA `a…a` from workflow head `b…b`, carries Unicode `résumé`, pending version
`9007199254740993` (unsafe as a JS Number), and plan run ID `37110947190`.

## ZIP provenance and regeneration

The two `.upload-artifact.zip` files were generated offline with **archiver
7.0.1**, the exact dependency resolved by upload-artifact
[`043fb46d1a93c77aae656e7c1c64a875d1fc6a0a/package-lock.json`](https://github.com/actions/upload-artifact/blob/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a/package-lock.json)
(`@actions/artifact` 6.2.0). The generator follows
[the toolkit ZIP stream implementation](https://github.com/actions/toolkit/blob/main/packages/artifact/src/internal/upload/zip.ts):
`archiver.create('zip', {zlib: {level: 0}})`, `zip.file`, pipe, finalize.
Both have method 0 **and flag 3** with a signed data descriptor. A parser that
rejects descriptors cannot consume these real library-produced archives.
The archive file's entry name is the producer filename, not the artifact NAME.
No upload, Actions run, network callback or staging operation generated them.

For deliberate regeneration, inspect/remove only the old generated ZIPs first
(the generator exclusively creates outputs). From the repository root:

```sh
npm install --prefix "$PAPERCLIP_SCRATCH_DIR/zip-tool" --ignore-scripts --no-audit --no-fund archiver@7.0.1
node protection-rule/test/generate-upload-fixtures.cjs "$PAPERCLIP_SCRATCH_DIR/zip-tool"
```

No library install is needed for CI or the service: committed ZIPs exercise the
reader; the independent byte builder mutates ZIP metadata for rejection tests.

## GitHub source-evidence captures (real, read-only)

`github/*.json` are trimmed real GitHub REST responses for the public Bot repository,
captured 2026-10-07 with read-only GET calls (no write, no credential value
recorded):

- `main-7a5043e-check-runs.json`:
  `GET /repos/TogetherWeOwn/two-bot-next/commits/7a5043e4b378e2648c9287ea00d916a16dacdb08/check-runs?filter=latest&per_page=100`
  (the squash commit of PR #626 on main; `ci-ok`, `check`, `gitleaks`,
  `pr-lint` from GitHub Actions, App `15368`).
- `main-7a5043e-pulls.json`: `.../commits/7a5043e4b378e2648c9287ea00d916a16dacdb08/pulls?per_page=100`
  (exactly PR #626, `merge_commit_sha` = that commit).
- `pr626-head-a4da131-check-runs.json`:
  `.../commits/a4da131e368e086b9c271f164226ac7387a41996/check-runs?filter=latest&per_page=100`
  (the PR head; `Paperclip Review` from App `5191093` `togetherweown-reviewer`,
  `completed`/`success`, title `5/5`, finished 03:58:06Z, before the merge at
  06:09:13Z).

`node test/trim-source-fixtures.cjs <dir>` reproduces them from the raw
responses (not committed; SHA-256 of the raw bytes as captured:
`main-checks.json` `d646d719c440c40a30b778b3c9466a941bb0cc5e776800a58e1da2fffaa93929`,
`main-pulls.json` `2686d82d16a1fe6374f4c74f6f3cf0dc32b6a0645357a92e8dff84937dd5194d`,
`head-checks.json` `d73f74f9f6536a36b6cab313f3906fed4d824c11b9deb25470a66557252c949e`). Each kept run and pull request carries
the fields the reader consumes verbatim. Dropped: unselected runs, owner and
permission blobs, avatar links, and every run's free-text `output.summary` (the review's
carries an internal task link; the reader reads only `output.title`). The one recomputed value is each
list's `total_count`, set to the rows kept. Synthetic fixtures in
`contract-fixture.mjs` clone these objects and rebind only SHAs, the pull request
number, the review's `external_id` and (to keep rows distinct) CI run ids. The
committed fixtures' SHA-256 are pinned in `source.test.mjs`. For this public
snapshot, the six check-run `external_id` values were additionally replaced with
synthetic UUIDs; all other fields, including pull provenance and scored check
fields, are unchanged from the trimmed fixtures. The updated hashes pin these
sanitized bytes, not the original response bytes. The historical App identifier
is fixture provenance, not evidence of the current live ruleset.

Publication admits the two ZIP streams only at their exact reviewed path and
SHA-256, and only when the single archive member equals the corresponding public
JSON fixture byte-for-byte. Their extracted content is scanned too; changed ZIP
bytes, a different path or missing/mismatched JSON remain refused.
