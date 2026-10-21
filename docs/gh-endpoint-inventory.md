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
| `GH_API_URL` | `https://api.github.com` | `red_main_poll.sh`, `upstream_gate.sh`, `upstream_lint.py`, `gh-event-capture/scripts/check-subscription.sh` |
| `GITHUB_API_BASE` | `https://api.github.com` | `push-github-rate-limit.sh` (token-free read) |
| Pinned `https://api.github.com` | — | `gh_token.sh`, `ci_node93f4bccf_guard.py` (default), `push-runner-census.py`, `pr-hygiene/sweep.py`, `push-ci-queue-age-reviewed.py` |
| `gh` CLI credential | — | `gh run list`, `gh api` call sites below (auth lives in the CLI environment, not in our env) |

## Endpoint table

| Endpoint shape | Constructed at | Credential | Transport |
| --- | --- | --- | --- |
| `GET /repos/{o}/{r}/actions/runs/{id}/attempts/{n}/jobs?per_page=100` | `ci_failure_drain.sh` (jobs list), `ci_dark_steps.sh:124` (job steps) | `GH_TOKEN` (actions:read) | curl + header file |
| `GET /repos/{o}/{r}/actions/jobs/{id}/logs` (302 to storage, `-L` follows) | `ci_failure_drain.sh` (log fetch) | `GH_TOKEN` (actions:read) | curl `-L` + header file; see REDIRECT SCOPING in that file |
| `GET /repos/{slug}/commits/main`, `GET /repos/{slug}/commits/{sha}/check-runs` | `red_main_poll.sh` (`gh_api`, `resolve_main`, `failing_checks`) | `GH_TOKEN` | curl + 0600 header file. Note: this tool ALSO reads the Paperclip board with `RED_MAIN_API_KEY`/`PAPERCLIP_API_KEY` (`board_titles`) — two credentials, two hosts, one process |
| `GET/POST /app/installations/...`, installation-token mint | `gh_token.sh` (`app_api`, `mint_token`) | GitHub App JWT (short-lived, minted from the App key), then the minted installation token | curl + config file (`curl_authed`) |
| `GET /repos/{repo}/commits/{sha}/pulls`, `.../check-runs`, `GET /repos/{repo}/pulls/{n}[...]`, `.../issues/{n}/comments`, `.../pulls/{n}/files`, `.../contents/...`, `.../git/ref/...`, `gh api graphql` (reviewThreads) | `upstream_gate.sh` (`api_get`, `fetch_all_items`, review threads) | `GH_TOKEN` via curl + header file; `gh` CLI when no token | curl, else `gh api` |
| `gh pr create` (draft upstream PR, publish path) and `PATCH repos/{upstream}/pulls/{n}` (ready-for-review body, enforce path) | `upstream_gate.sh` (publish, enforce) | `gh` CLI credential | `gh` subprocess (the two mutations in this file; create is gated on static gate + lint passing on the exact SHA) |
| `GET {api_base}{path}` (memory init/refresh/ingest, batch, live) | `upstream_lint.py` (`api_get`) | `GH_TOKEN` preferred, else `GH_BIN` (`gh api`) | urllib, else `gh api` subprocess |
| `POST /repos/{repo}/issues/{pr}/comments` (review receipts) | `upstream_lint.py` (receipt poster) | `GH_TOKEN` preferred, else `gh api` | urllib POST, else `gh api -X POST` (mutation) |
| `GET /orgs/{org}/actions/runners`, `GET/POST/PATCH/DELETE /repos/{repo}/actions/variables/...` | `ci_node93f4bccf_guard.py` (`GitHub.request`, `observe_bounded`, `write_variable`) | `GH_TOKEN` (inherited env only) | urllib (reads AND writes) |
| `GET /orgs/{org}/actions/runners` | `ops/case_ebbb469a/push-runner-census.py` (`request`, runners page) | `GH_TOKEN` (scrubbed from the Gatus push subprocess env) | urllib (its docstring also names a runs/jobs shape the code does not request) |
| `GET /repos/TogetherWeOwn/{repo}/pulls...`, `.../pulls/{n}`, `.../commits/{sha}/check-runs` | `ops/pr-hygiene/sweep.py` (`gh()`) | installation token minted by `mint-installation-token.sh`, read from `/tmp/gh-inst-token` | urllib |
| `GET /repos/TogetherWeOwn/{repo}/actions/runs?per_page=1`, `GET .../runs/{id}/jobs?per_page=1` | `ops/case_658bf6b9/push-ci-queue-age-reviewed.py:62,65` | GitHub installation token (correct credential for this shape) | urllib |
| `gh run list --status queued/in_progress` | `ops/case_658bf6b9/_ci_queue_age.sh` (shared lib; sourced by `push-ci-queue-age.sh` and `push-queue-forecast.sh`; `push-garm-pool.sh` issues the same calls) | `gh` CLI credential | `gh` subprocess (the forecast script's header also names a `gh api .../jobs` read the code does not make) |
| `gh api repos/{o}/{r}/commits/.../check-runs`, `.../pulls...` | `ops/case_e1e23b50/hung_job_eval.py:130` | `gh` CLI (host credential shim; `GH_BIN` seam) | `gh api` subprocess |
| `gh api repos/{o}/{r}/rules/branches/{b}` (+ history reads) | `ci_required_checks_audit.py:483`, `ci_required_checks_ownership.py` | `gh` CLI credential (`--gh-bin` / `AUDIT_GH_BIN` seam) | `gh api` subprocess (read-only) |
| `gh` subprocess calls (review/error/backlog helpers) | `ops/productivity/backlog_promoter.py`, `review_error_rerequest.py`, `resolve_outdated_threads.py` | `GH_TOKEN` passed by env into the subprocess only | `gh` subprocess |
| `GET /repos/{repo}/commits/{sha}/pulls`, `/pulls/{n}`, `/commits/{sha}/check-runs`, `/pulls/{n}/files`, `/issues/{n}/comments`; `POST /issues/{n}/labels` | `.github/scripts/review_trigger_step.py` (`api`, `paginate`) | `GH_TOKEN` (CI; HEAD_SHA and REPO required) | urllib (reads plus a label mutation) |
| `GET /app`, `GET /app/hook/config` | `gh-event-capture/scripts/check-subscription.sh` | GitHub App JWT (RS256, openssl-signed per run from the App key) | curl + config file |
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
