// S5b merge-confirmation opener (design §2 + §5.4): the agent-side helper the
// originating agent calls once at PR open. This is the opener side of the S5a
// contract: the task holds one pending merge confirmation from open to merge,
// the built-in sweep accepts it on merge (one merged wake), and S5a's
// predicate ignores exactly this confirmation for exception wakes while
// refusing any other pending interaction.
//
// Opener constraints (from the S5a mirror note, verified against the server
// intent pattern): the title's FIRST line carries the merge intent with the PR
// reference itself ("Please confirm that owner/repo#N is merged"), because the
// pattern anchors at the joined text start and `.` never crosses a newline. A
// bare "Please confirm merge" title does NOT match. Every emitted word is
// inside the server merge-only vocabulary: smuggled extra actions ("also
// deploy to prod") are never emitted, and the vocabulary is never widened
// here — import the S5a predicate in tests instead of copying it, so
// server-vocabulary drift fails loudly there.
//
// Continuation policy is `wake_assignee_on_accept` (design §2 table): the
// owner is woken only when the confirmation is accepted on merge, never before.
// `payload.supersedeOnUserComment` is false so an owner comment on the task
// between open and merge does not expire the confirmation (the platform
// default is true). No `target` is set: a document target would void the
// confirmation, and none is needed. No arming/merging logic lives here — S6a
// owns arming, S6b owns disarm, and no live exception wake ships in this
// slice.
//
// Importable (`buildMergeConfirmation`, `openMergeConfirmation`) and
// shell-callable (`node src/merge-confirmation.js --issue <uuid>
// --repo <owner/repo> --pr <N>`; `--dry-run` prints the envelope without
// posting). Credentials travel by inherited environment (`PAPERCLIP_API_URL`,
// `PAPERCLIP_API_KEY`), never on argv.

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

function prContext(input) {
  requireValue(input !== null && typeof input === 'object' && !Array.isArray(input),
    'merge confirmation needs a repository and PR number')
  requireValue(typeof input.repository === 'string' && REPOSITORY.test(input.repository),
    'merge confirmation needs a repository like owner/repo')
  requireValue(Number.isSafeInteger(input.number) && input.number > 0,
    'merge confirmation needs a positive integer PR number')
  return { repository: input.repository, number: input.number }
}

function prUrl(repository, number) {
  return `https://github.com/${repository}/pull/${number}`
}

/**
 * Pure builder: the exact `POST /api/issues/{id}/interactions` envelope for
 * this PR's merge confirmation. Deterministic per (repository, number), so a
 * retried open under the same idempotency key reuses the row instead of
 * opening a second confirmation.
 */
export function buildMergeConfirmation(input) {
  const { repository, number } = prContext(input)
  const url = prUrl(repository, number)
  const shorthand = `${repository}#${number}`
  return {
    kind: 'request_confirmation',
    title: `Please confirm that ${shorthand} is merged`,
    summary: `Confirm that ${shorthand} is merged and green`,
    continuationPolicy: 'wake_assignee_on_accept',
    resolverPolicy: 'board_only',
    idempotencyKey: `merge-confirmation:${shorthand}`,
    payload: {
      version: 1,
      prompt: `Please confirm that ${url} is merged and all checks are green`,
      acceptLabel: 'Confirm merge',
      detailsMarkdown: url,
      supersedeOnUserComment: false,
    },
  }
}

function originFromApiUrl(value) {
  requireValue(typeof value === 'string' && value.length > 0, 'PAPERCLIP_API_URL is missing')
  const stripped = value.replace(/\/+$/, '').replace(/\/api$/, '')
  const origin = new URL(stripped).origin
  requireValue(!new URL(value.replace(/\/+$/, '')).username, 'Paperclip API URL must carry no credentials')
  return origin
}

/**
 * Opens the confirmation on the issue via the interactions API. `fetchImpl`
 * is injectable so tests never touch the network. Throws — failing closed,
 * never half-opening — on invalid input, transport errors, or a response
 * that does not confirm the confirmation back.
 */
export async function openMergeConfirmation({ issueId, repository, number, baseUrl, token, runId = null,
  fetchImpl = globalThis.fetch, timeoutMs = 30000 } = {}) {
  const context = prContext({ repository, number })
  requireValue(typeof issueId === 'string' && UUID.test(issueId), 'merge confirmation needs an issue UUID')
  requireValue(typeof token === 'string' && token.length > 0 && !/\s/.test(token),
    'Paperclip credential is missing or invalid')
  requireValue(Number.isInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 120000,
    'merge confirmation transport budget is invalid')
  const origin = originFromApiUrl(baseUrl)
  const envelope = buildMergeConfirmation(context)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let response
  try {
    response = await fetchImpl(`${origin}/api/issues/${issueId}/interactions`, {
      method: 'POST',
      redirect: 'manual',
      credentials: 'omit',
      cache: 'no-store',
      signal: controller.signal,
      headers: {
        authorization: 'Bearer ' + token,
        accept: 'application/json',
        'content-type': 'application/json',
        ...(runId !== null && typeof runId === 'string' && UUID.test(runId)
          ? { 'X-Paperclip-Run-Id': runId } : {}),
      },
      body: JSON.stringify(envelope),
    })
  } catch {
    // Transport failure only: never conflate it with a server refusal, and
    // never retain provider bodies or nested causes.
    throw new Error(controller.signal.aborted
      ? 'merge confirmation post timed out' : 'merge confirmation post failed')
  } finally {
    clearTimeout(timer)
  }
  requireValue((response.status === 200 || response.status === 201) && !response.redirected &&
    /^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') ?? ''),
    'Paperclip did not confirm the merge confirmation')
  const row = await response.json().catch(() => null)
  requireValue(row !== null && typeof row === 'object' && !Array.isArray(row) &&
    row.kind === 'request_confirmation' && typeof row.id === 'string',
    'Paperclip did not confirm the merge confirmation')
  return row
}

function usage() {
  return [
    'usage: node src/merge-confirmation.js --issue <uuid> --repo <owner/repo> --pr <N> [--dry-run]',
    '',
    'Opens the S5b merge confirmation on the issue. Credentials come from the',
    'environment (PAPERCLIP_API_URL, PAPERCLIP_API_KEY, optional PAPERCLIP_RUN_ID),',
    'never from argv. --dry-run prints the envelope JSON without posting.',
  ].join('\n')
}

function parseArgs(argv) {
  const args = { issue: null, repo: null, pr: null, dryRun: false }
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i]
    if (flag === '--dry-run') { args.dryRun = true; continue }
    if (flag === '--help' || flag === '-h') return { help: true }
    const next = argv[i + 1]
    if ((flag === '--issue' || flag === '--repo' || flag === '--pr') && typeof next === 'string') {
      args[flag.slice(2)] = next
      i += 1
      continue
    }
    throw new Error(`unknown argument: ${flag}\n${usage()}`)
  }
  return args
}

async function main(argv, env) {
  const args = parseArgs(argv)
  if (args.help) {
    process.stdout.write(`${usage()}\n`)
    return 0
  }
  if (args.issue === null || args.repo === null || args.pr === null) throw new Error(`missing argument\n${usage()}`)
  const number = Number(args.pr)
  if (args.dryRun) {
    process.stdout.write(`${JSON.stringify(buildMergeConfirmation({ repository: args.repo, number }), null, 2)}\n`)
    return 0
  }
  const row = await openMergeConfirmation({
    issueId: args.issue,
    repository: args.repo,
    number,
    baseUrl: env.PAPERCLIP_API_URL,
    token: env.PAPERCLIP_API_KEY,
    runId: env.PAPERCLIP_RUN_ID ?? null,
  })
  process.stdout.write(`${JSON.stringify({ id: row.id, kind: row.kind, status: row.status ?? null })}\n`)
  return 0
}

// Shell entrypoint only; importing this module never posts.
const invokedAsScript = process.argv[1] !== undefined &&
  import.meta.url === new URL(`file://${process.argv[1]}`, 'file://').href
if (invokedAsScript) {
  let code = 0
  try {
    code = await main(process.argv.slice(2), process.env)
  } catch (error) {
    process.stderr.write(`merge-confirmation: ${error instanceof Error ? error.message.split('\n')[0] : 'failed'}\n`)
    code = /unknown argument|missing argument/.test(error instanceof Error ? error.message : '') ? 2 : 1
  }
  process.exit(code)
}
