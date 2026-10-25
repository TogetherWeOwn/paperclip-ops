// Title-fallback acceptance: protection-rule source evidence (PR #106 follow-up acceptance).
//
// Test-only handoff for PR #106 (fix/review-verdict-title-fallback). Pins the
// missing/empty-title -> first-summary-line fallback WITHOUT implementing it.
//
// Desired (PR #106): when the review check's output.title is missing/null/
// empty/whitespace-only, accept iff output.summary's FIRST line is exactly
// "5/5". Any other shape refuses with SOURCE_REVIEW_UNVERIFIED. App pin, head
// binding, external_id, status/conclusion and merge-time rules are unchanged
// (see source.test.mjs).
//
// RED expectation on main (no fallback): the 6 *pass* cases FAIL (current
// reader maps title only, so the verifier refuses); all *refuse* cases and
// controls PASS. GREEN once github.js maps summary and source.js applies the
// fallback.
//
// Wiring pointer for the Land owner (do NOT wire here; test-only card):
//   cd protection-rule && node --test test/title-fallback.test.mjs
//   Add test/title-fallback.test.mjs to the "test" script in
//   protection-rule/package.json (same list as test/source.test.mjs) plus the
//   offline-CI contract pin (preview --propose, apply --write).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { verifySourceEvidence } from '../src/source.js'
import { findPolicy } from '../src/environments.js'
import { REASON } from '../src/decide.js'
import { contractFixture, SOURCE, HEAD, PLAN_HEAD, REPO, PR_HEADS } from './contract-fixture.mjs'

const EVIDENCE = findPolicy({ repository: REPO, environment: 'staging-migrate-apply' }).sourceEvidence
const SHAS = [SOURCE, HEAD, PLAN_HEAD]
const REVIEW = { ok: false, reason: REASON.SOURCE_REVIEW_UNVERIFIED }

const verify = (f) => verifySourceEvidence({ github: f.github, repository: REPO, shas: SHAS, evidence: EVIDENCE })
const review = (f, sha) => f.checkRuns[PR_HEADS[sha]][0]

// Set the SOURCE review's output to an explicit title/summary shape.
// title/summary: string | null | undefined (undefined omits the key).
function setOutput(f, sha, title, summary) {
  const run = review(f, sha)
  const output = {}
  if (title !== undefined) output.title = title
  if (summary !== undefined) output.summary = summary
  run.output = output
}

// --- Pass: missing/empty title + exact first-line 5/5 (RED on main) ---

test('fallback passes when the title key is missing and the summary is 5/5', async () => {
  const f = contractFixture()
  setOutput(f, SOURCE, undefined, '5/5')
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

test('fallback passes when the title is empty and the summary is 5/5', async () => {
  const f = contractFixture()
  setOutput(f, SOURCE, '', '5/5')
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

test('fallback passes when the title is null and the summary is 5/5', async () => {
  const f = contractFixture()
  setOutput(f, SOURCE, null, '5/5')
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

test('fallback passes when the title is whitespace-only and the summary is 5/5', async () => {
  // Assumption: whitespace-only title counts as empty (triggers the fallback).
  const f = contractFixture()
  setOutput(f, SOURCE, '   ', '5/5')
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

test('fallback passes when the summary first line is 5/5 with more lines after', async () => {
  const f = contractFixture()
  setOutput(f, SOURCE, undefined, '5/5\n## details\nmore')
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

test('fallback passes when the title is empty and the summary first line is 5/5', async () => {
  const f = contractFixture()
  setOutput(f, SOURCE, '', '5/5\nsecond line')
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

// --- Refuse: variants that must NOT satisfy the fallback (green on main) ---

const refuseCases = {
  'leading space': [undefined, ' 5/5'],
  'trailing space': [undefined, '5/5 '],
  'suffix': [undefined, '5/5 approved'],
  'colon suffix': [undefined, '5/5: all good'],
  'prefix': [undefined, 'Score: 5/5'],
  'second line': [undefined, 'Review complete\n5/5'],
  'empty first line': [undefined, '\n5/5'],
  // The live summary shape contains 5/5 but is not exactly "5/5".
  'typical heading': [undefined, '## Paperclip Review — 5/5'],
  // Fallback applies ONLY when the title is missing/empty.
  'present 4/5 title with 5/5 summary': ['4/5', '5/5'],
  'present reviewed title with 5/5 summary': ['reviewed', '5/5'],
  'empty summary': [undefined, ''],
  'missing summary': ['', undefined],
  'null summary': ['', null],
  'zero-width adverse': [undefined, '5/5\u200b'],
  'broken score': [undefined, '5 /5'],
  // Incidental mention deep in the body, not the first line.
  'incidental mention': ['reviewed', 'we saw 5/5 before, now 4/5'],
}
for (const [name, [title, summary]] of Object.entries(refuseCases)) {
  test('fallback refuses ' + name, async () => {
    const f = contractFixture()
    setOutput(f, SOURCE, title, summary)
    assert.deepEqual(await verify(f), REVIEW)
  })
}

// --- Controls: existing title-only behavior, unchanged by the fallback ---

test('title 5/5 with a heading summary still passes', async () => {
  const f = contractFixture()
  setOutput(f, SOURCE, '5/5', '## Paperclip Review — 5/5')
  assert.deepEqual(await verify(f), { ok: true, shas: SHAS })
})

test('title 4/5 still refuses', async () => {
  const f = contractFixture()
  setOutput(f, SOURCE, '4/5', '5/5')
  assert.deepEqual(await verify(f), REVIEW)
})
