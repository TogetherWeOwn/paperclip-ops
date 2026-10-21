# GitHub endpoint inventory (operator tooling)

Which GitHub endpoints operator tooling constructs, where, and with which
credential. Read this before changing any GitHub transport: endpoint shape
alone does not identify the caller — several tools below build the same
`/repos/.../actions/runs` shapes with different credentials.

Scope: live tooling only. Test fixtures (`test_*`, `*_test.py`) build the same
shapes against `.invalid` stubs and are excluded. Line numbers are approximate;
grep the cited anchor if they drift.

## Base URLs

| Base | Default | Users |
| --- | --- | --- |
| `GITHUB_API_URL` | `https://api.github.com` | `ci_failure_drain.sh`, `ci_dark_steps.sh` (Actions-provided) |
| `GH_API_URL` | `https://api.github.com` | `red_main_poll.sh`, `upstream_gate.sh`, `upstream_lint.py`, `gh-event-capture/scripts/check-subscription.sh`, `gh-event-capture/scripts/gh-wrapper.mjs`, `gh-event-capture/scripts/gh-readiness-wrapper.mjs`, `sibling_guard.sh` |
| `GITHUB_API_BASE` | `https://api.github.com` | `push-github-rate-limit.sh` (token-free read) |
| Pinned `https://api.github.com` | — | `gh_token.sh`, `ci_node93f4bccf_guard.py` (default), `push-runner-census.py`, `pr-hygiene/sweep.py`, `push-ci-queue-age-reviewed.py` |
| `gh` CLI credential | — | `gh run list`, `gh api` call sites below (auth lives in the CLI environment, not in our env) |

## Endpoint table

| Endpoint shape | Constructed at | Credential | Transport |
| --- | --- | --- | --- |
| `GET /repos/{o}/{r}/actions/runs/{id}/attempts/{n}/jobs?per_page=100` | `ci_failure_drain.sh` (jobs list), `ci_dark_steps.sh:124` (job steps) | `GH_TOKEN` (actions:read) | curl + header file |
| `GET /repos/{o}/{r}/actions/jobs/{id}/logs` (302 to storage, `-L` follows) | `ci_failure_drain.sh` (log fetch) | `GH_TOKEN` (actions:read) | curl `-L` + header file; see REDIRECT SCOPING in that file |
| `GET /repos/{slug}/commits/main`, `GET /repos/{slug}/commits/{sha}/check-runs` | `red_main_poll.sh` (`gh_api`, `resolve_main`, `failing_checks`) | `GH_TOKEN` | curl + 0600 header file. Note: this tool ALSO reads the Paperclip board with `RED_MAIN_API_KEY`/`PAPERCLIP_API_KEY` (`board_titles`) — two credentials, two hosts, one process |
| `GET /repos/{slug}/pulls?state=open&per_page=100` | `sibling_guard.sh:481` (open-PR query) | `GH_TOKEN` when set, else the token from `git credential fill` for this clone (`:462-467`) | curl + 0600 header file |
| `GET/POST /app/installations/...`, installation-token mint | `gh_token.sh` (`app_api`, `mint_token`) | GitHub App JWT (short-lived, minted from the App key), then the minted installation token | curl + config file (`curl_authed`) |
| `GET /repos/{repo}/commits/{sha}/pulls`, `.../check-runs`, `GET /repos/{repo}/pulls/{n}[...]`, `.../issues/{n}/comments`, `.../pulls/{n}/files`, `.../contents/...`, `.../git/ref/...`, `gh api graphql` (reviewThreads) | `upstream_gate.sh` (`api_get`, `fetch_all_items`, review threads) | `GH_TOKEN` via curl + header file; `gh` CLI when no token | curl, else `gh api` |
| `gh pr create` (draft upstream PR, publish path), `PATCH repos/{upstream}/pulls/{n}` + `gh pr ready` (ready body + mark ready, ready path), GraphQL `convertPullRequestToDraft` (audit `--enforce` path) | `upstream_gate.sh:1215` (cmd_publish), `:1550,:1555` (cmd_ready), `:1719` (cmd_audit) | `gh` CLI credential | `gh` subprocess (the four mutations in this file; create is gated on static gate + lint passing on the exact SHA) |
| `GET {api_base}{path}` (memory init/refresh/ingest, batch, live) | `upstream_lint.py` (`api_get`) | `GH_TOKEN` preferred, else `GH_BIN` (`gh api`) | urllib, else `gh api` subprocess |
| `POST /repos/{repo}/issues/{pr}/comments` (review receipts) | `upstream_lint.py` (receipt poster) | `GH_TOKEN` preferred, else `gh api` | urllib POST, else `gh api -X POST` (mutation) |
| `GET /orgs/{org}/actions/runners`, `GET/POST/PATCH/DELETE /repos/{repo}/actions/variables/...` | `ci_node93f4bccf_guard.py` (`GitHub.request`, `observe_bounded`, `write_variable`) | `GH_TOKEN` (inherited env only) | urllib (reads AND writes) |
| `GET /orgs/{org}/actions/runners` | `ops/case_ebbb469a/push-runner-census.py` (`request`, runners page) | `GH_TOKEN` (scrubbed from the Gatus push subprocess env) | urllib (its docstring also names a runs/jobs shape the code does not request) |
| `GET /repos/TogetherWeOwn/{repo}/pulls...`, `.../pulls/{n}`, `.../commits/{sha}/check-runs` | `ops/pr-hygiene/sweep.py` (`gh()`) | installation token minted by `mint-installation-token.sh`, read from `/tmp/gh-inst-token` | urllib |
| `POST /app/installations/{id}/access_tokens` (RS256 App JWT from `/home/ubuntu/secure-drop/github-app.pem`, down-scoped to 3 repos with actions:read + metadata:read), then `GET .../actions/runs?per_page=1`, `GET .../runs/{id}/jobs?per_page=1` (`--check-auth` probe) | `ops/case_658bf6b9/push-ci-queue-age-reviewed.py:58` (mint), `:62,65` (reads) | minted installation token, handed to the detector as `GH_TOKEN` (`:70-73` exec) | urllib + execve |
| `gh run list --status queued/in_progress`, `gh api repos/{repo}/actions/runs/{id}/jobs?per_page=100` | `ops/case_658bf6b9/_ci_queue_age.sh:116` (shared lib; sourced by `push-ci-queue-age.sh` and `push-queue-forecast.sh:67`, which calls `ci_oldest_waiting_job` at `:123`); `push-garm-pool.sh:185` issues the same jobs read itself | `gh` CLI credential | `gh` subprocess |
| `gh api repos/{repo}/commits/main/check-runs?per_page=100` | `ops/case_c542accd/push-ci-main.sh:147` (live on the 5-minute cron, `ops/case_658bf6b9/OPERATOR.md:55`) | `gh` CLI credential (`GH_BIN` seam, default `gh`) | `gh api` subprocess (read-only) |
| `gh api repos/{o}/{r}/commits/.../check-runs`, `.../pulls...` | `ops/case_e1e23b50/hung_job_eval.py:130` | `gh` CLI (host credential shim; `GH_BIN` seam) | `gh api` subprocess |
| `gh api repos/{o}/{r}/rules/branches/{b}` (+ history reads) | `ci_required_checks_audit.py:483`, `ci_required_checks_ownership.py` | `gh` CLI credential (`--gh-bin` / `AUDIT_GH_BIN` seam) | `gh api` subprocess (read-only) |
| `gh pr view` / `gh pr list` / `gh api repos/{repo}/commits/{sha}` (review/error/backlog reads), `gh api search/issues` + `gh api graphql` (reviewThreads read + `resolveReviewThread` mutation) + `gh api repos/{repo}/commits/{head}/check-runs` + `POST .../pulls/{n}/comments/{id}/replies` (outdated-thread resolve) | `ops/productivity/review_error_rerequest.py:78,:145,:169,:181` (`pr_head`, clean/unreviewed scans), `backlog_promoter.py:79`, `resolve_outdated_threads.py:157,:191,:204,:217,:219-220` | installation token minted by `gh_token.sh token` (`gh_env()`/`env()`), in the `gh` child env only, never on argv | `gh` subprocess (reads plus reply + thread-resolve mutations) |
| `gh pr list -R {repo} --state open --limit 100` | `ops/productivity/orphan_prs.py:156`, `pr_supply.py:270` | `GH_TOKEN` minted by `gh_token.sh token` (direct in `orphan_prs.py` main; via `review_error_rerequest.gh_env()` in `pr_supply.py`) | `gh` subprocess with `GH_TOKEN` in the child env only |
| `GET /repos/{repo}/commits/{sha}/pulls`, `/pulls/{n}`, `/commits/{sha}/check-runs`, `/pulls/{n}/files`, `/issues/{n}/comments`; `POST /issues/{n}/labels`, `POST /repos/{repo}/labels` (create-on-404), `POST /issues/{n}/comments` (trigger body) | `.github/scripts/review_trigger_step.py:179,185,190` (`api`, `paginate`) | `GH_TOKEN` (CI; HEAD_SHA and REPO required) | urllib (reads plus label and comment mutations) |
| `GET /app`, `GET /app/hook/config` | `gh-event-capture/scripts/check-subscription.sh` | GitHub App JWT (RS256, openssl-signed per run from the App key) | curl + config file |
| `GET /app/installations` (discover, when unpinned), `POST /app/installations/{id}/access_tokens` | `gh-event-capture/scripts/gh-wrapper.mjs:169` (read grants: pull_requests read + metadata read, sized to the adapter's GraphQL reads), `gh-readiness-wrapper.mjs:215` (write grants at `READINESS_PERMISSIONS` `:70-76`: pull_requests/contents write + checks/statuses/metadata read, sized to the sweep surface) | App JWT (RS256, node-crypto-signed per run from the host-private PEM); the minted token lives only in the real-`gh` child env | fetch + execFile (`gh` argv forwarded verbatim) |
| GraphQL PR/node/check reads, `GET branches/{branch}/protection`, `GET rules/branches/{branch}`; `PATCH pulls/{n}` (body), `POST pulls/{n}/ready_for_review`, GraphQL `convertPullRequestToDraft`, `gh pr merge --auto` / `--disable-auto` | `gh-event-capture/src/github-adapter.js` (reads `:252,:376,:446`; mutations `:487,:504,:521,:299,:560`) | installed `gh` credential path — in the capture runtime the wrapper above mints a per-invocation installation token (runtime pins `gh` to the wrapper) | `gh` subprocess (`gh api --hostname github.com`, `gh pr merge`) |
| `gh pr merge --auto --squash --match-head-commit` (arm writer), `GET repos/{o}/{r}` + `GET repos/{o}/{r}/rules/branches/{branch}` (live branch-gate read) | `gh-event-capture/src/closeout-arm.js` (writer `buildArmArgv` `:169`, run `:187`; gate reads `:212,:227`; CLI entrypoint `:495-500`; the PR-opening agent calls it once at open per header `:1-2`; snapshot reads delegate to the adapter above `:473-474`) | calling agent's inherited `gh` credential (execFile with no env override; header `:40`) — NOT the runtime wrapper mint. Note: this module ALSO opens the S5b merge confirmation on the Paperclip board with `PAPERCLIP_API_KEY` (`:300-307`) — two credentials, two hosts, one process | `gh` subprocess (`gh api --hostname github.com`, `gh pr merge`) |
| `POST /app/installations/{id}/access_tokens`, scoped `GET /repos/{repo}...`, `.../actions/artifacts/{id}/zip` | `protection-rule/src/github.js` | App JWT for the mint, then the installation token (same-origin-only on redirects; explicit repo allowlist) | fetch with redirect budget, https-only, credential-free URLs — the prior art for redirect scoping in this repo |
| `POST {callbackUrl}` (caller-supplied URL, host constrained to api.github.com) | `protection-rule/src/callback.js` (`postVerdict`) | installation token, header-only, whitespace-refused | fetch |
| `GET /repos/{repo}/pulls/{n}` (+ `/commits`, `/files` pages) | `upstream_reputation.py` (delegates to `upstream_lint.api_get`) | delegated: `GH_TOKEN` or `gh api` per `upstream_lint.py` | urllib or `gh api` subprocess |
| `GET /orgs/{org}/repos`, `/meta`, `/rate_limit` | `verification/case_092cd51d-public-fork-disclosure-audit.sh` (CI gate) | `GH_TOKEN` optional (anonymous fallback); never on argv | curl + config file |

## Negative inventory (deliberate non-senders)

- `gh-event-capture/src/gh-pinned.js` pins the `gh` binary and REFUSES
  `GH_TOKEN`/`GITHUB_TOKEN` in the child env — capture subprocesses must not
  inherit GitHub credentials.
- `push-runner-census.py` strips `GH_TOKEN` from the Gatus push subprocess env.
- `ops/case_658bf6b9/push-github-rate-limit.sh` reads token-free
  `GET /rate_limit` by design — no Authorization header is sent.
- `plugins/pr-tracker/src/worker/collect-rest.js` defines a token-bearing
  GitHub fetch helper with no in-repo callers (`setup.js` takes a
  caller-provided `collect`; currently unwired).
- No Paperclip credential (`PAPERCLIP_API_KEY`, `PAPERCLIP_AGENT_API_KEY`) is
  sent to any endpoint in this table by operator tooling (statically verified;
  redirect chains are the residual — see REDIRECT SCOPING in
  `ci_failure_drain.sh`).

## Maintenance

When adding a GitHub call: add a row here with the endpoint shape, the file,
and the credential name. When in doubt, name the credential the code actually
attaches — not the one the endpoint shape suggests.
