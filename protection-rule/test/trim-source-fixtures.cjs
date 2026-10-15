// Reproduces test/fixtures/github/*.json from three raw, read-only GitHub API
// captures of the public Bot repository (commands in fixtures/README.md).
// Usage: node test/trim-source-fixtures.cjs <dir-with-raw-captures>
// Keeps whole check-run and pull-request objects' consumed fields verbatim and
// drops the rest: unselected runs, owner and permission blobs, avatar links, and
// the review's free-text summary (it carries an internal task link the reader
// never reads). The only recomputed value is each list's `total_count`, set to
// the number of rows kept. Nothing is invented.
const fs = require('node:fs')
const path = require('node:path')
const [input] = process.argv.slice(2)
if (!input) throw Error('usage: trim-source-fixtures.cjs <dir>')
const out = path.join(__dirname, 'fixtures', 'github')
const read = (name) => JSON.parse(fs.readFileSync(path.join(input, name), 'utf8'))
const write = (name, value) => fs.writeFileSync(path.join(out, name), JSON.stringify(value, null, 2) + '\n')

function run(r, keepSummary) {
  return {
    id: r.id, name: r.name, head_sha: r.head_sha, external_id: r.external_id,
    html_url: r.html_url, status: r.status, conclusion: r.conclusion,
    started_at: r.started_at, completed_at: r.completed_at,
    output: { title: r.output.title, summary: keepSummary ? r.output.summary : null },
    app: { id: r.app.id, slug: r.app.slug, name: r.app.name },
    check_suite: { id: r.check_suite.id },
  }
}
function runs(file, names) {
  const raw = read(file)
  const picked = names.flatMap((name) => {
    const found = raw.check_runs.filter((r) => r.name === name)
    if (found.length < 1) throw Error('expected a run named ' + name + ' in ' + file)
    return found.map((r) => run(r, false))
  })
  return { total_count: picked.length, check_runs: picked }
}
function pull(p) {
  return {
    id: p.id, number: p.number, state: p.state, title: p.title,
    merged_at: p.merged_at, merge_commit_sha: p.merge_commit_sha,
    head: { ref: p.head.ref, sha: p.head.sha, repo: { full_name: p.head.repo.full_name } },
    base: { ref: p.base.ref, sha: p.base.sha, repo: { full_name: p.base.repo.full_name } },
  }
}
write('main-7a5043e-check-runs.json', runs('main-checks.json', ['ci-ok', 'check', 'gitleaks', 'pr-lint']))
write('pr626-head-a4da131-check-runs.json', runs('head-checks.json', ['Paperclip Review', 'CodeQL', 'ci-ok']))
write('main-7a5043e-pulls.json', read('main-pulls.json').map(pull))
