#!/usr/bin/env bash
# push-ci-hung-job.sh — cron: CI jobs that STARTED and never complete → Gatus
# `platform_ci-hung-job`.
#
# case_703e1a90 implements the case_e1e23b50 spec (ops/case_e1e23b50/hung-job-detector.md,
# sections 2-5). ci-queue-age and the wait-time SLO only see QUEUED jobs; a
# job that started and then lost its completion event pages nobody. This
# script owns exactly that: status in_progress, started_at set, past the
# job's timeout + 15m. The classification lives in hung_job_eval.py (stdlib,
# read-only, Checks API only, thresholds in hung_job_thresholds.json); this
# script only runs it and pushes ONE verdict.
#
# Verdict → push. success=false ONLY on HUNG or a fail-closed condition (403,
# unparseable JSON, a full check-runs page, a malformed started_at). WARN and
# UNKNOWN-threshold jobs (no row) are named in the text with success still true
# until 375m (GitHub's 360m default timeout + 15m), then they are HUNG.
# Gatus drops the `error` text of a success=true push, so a WARN-only text is
# read from this script's stdout (cron log); it reaches the page when a HUNG
# push carries it.
#
# Refusals (exit 2, zero pushes, so the 10m heartbeat miss pages): missing
# GATUS_URL / GATUS_EXTERNAL_TOKEN / GH_REPOS, a bad repo, no python3/jq, the
# shared helper or evaluator missing, unreadable thresholds. An evaluator that
# crashes (any other non-zero exit) pushes success=false: a detector that
# cannot run is an incident, not a green.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh), GH_REPOS
# (whitespace owner/repo list, mandatory), GH_BIN (default gh; the host gh
# credential shim), HUNG_JOB_NOW (offline-suite clock pin; never set in cron).
# Needs the same checks:read token class as push-ci-main.sh and nothing else.
# Cron (operator, ops/case_e1e23b50/OPERATOR.md):
# */5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_e1e23b50/push-ci-hung-job.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-ci-hung-job: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
. "$GATUS_HELPER"

EVALUATOR="$HERE/hung_job_eval.py"
[[ -f "$EVALUATOR" ]] \
  || { echo "push-ci-hung-job: refusing — evaluator $EVALUATOR missing" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-ci-hung-job: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-ci-hung-job: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
[[ -n "${GH_REPOS:-}" ]] \
  || { echo "push-ci-hung-job: refusing — GH_REPOS (whitespace owner/repo list) must be set" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-ci-hung-job: refusing — python3 required" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-ci-hung-job: refusing — jq required" >&2; exit 2; }

eval_args=(--repos "$GH_REPOS")
[[ -z "${HUNG_JOB_NOW:-}" ]] || eval_args+=(--now "$HUNG_JOB_NOW")

out="$(python3 "$EVALUATOR" "${eval_args[@]}")"
rc=$?
case "$rc" in
  0) ;;
  2) echo "push-ci-hung-job: refusing — evaluator refused (see its message above)" >&2; exit 2 ;;
  *) gatus_push platform_ci-hung-job false "hung-job evaluator crashed (exit $rc)" || true
     exit 1 ;;
esac

success="$(jq -r '.success' <<<"$out" 2>/dev/null)"
verdict="$(jq -r '.verdict' <<<"$out" 2>/dev/null)"
text="$(jq -r '.text' <<<"$out" 2>/dev/null)"
if [[ "$success" != "true" && "$success" != "false" ]]; then
  gatus_push platform_ci-hung-job false "hung-job evaluator output unparseable" || true
  exit 1
fi
echo "push-ci-hung-job: ${verdict} ${text}"

gatus_push platform_ci-hung-job "$success" "$text" || exit 1
[[ "$success" == "true" ]]
