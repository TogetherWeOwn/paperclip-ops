// Machine-readable failure identity, never a search in diagnostic/error text.
export default async function* reportFailures(source) {
  for await (const event of source) {
    if (event.type !== 'test:fail') continue
    const error = event.data.details.error
    const cause = error?.cause ?? error
    yield JSON.stringify({ type: event.type, name: event.data.name, number: event.data.testNumber,
      failureType: error?.failureType ?? null, errorName: cause?.name ?? null,
      code: cause?.code ?? null }) + '\n'
  }
  yield JSON.stringify({ type: 'report:complete' }) + '\n'
}

export function assertionKilled(result, testName) {
  if (result.status !== 1 || result.signal !== null || result.error) return false
  let failures
  try { failures = result.stdout.trim().split('\n').map((line) => JSON.parse(line)) }
  catch { return false }
  if (failures.length !== 2 || failures[1]?.type !== 'report:complete') return false
  const failure = failures[0]
  return failure?.type === 'test:fail' && failure.number === 1 &&
    typeof failure.name === 'string' && failure.name.startsWith(testName) &&
    failure.failureType === 'testCodeFailure' && failure.errorName === 'AssertionError' &&
    failure.code === 'ERR_ASSERTION'
}
