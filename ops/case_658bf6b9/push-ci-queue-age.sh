#!/usr/bin/env bash
# push-ci-queue-age.sh — cron: oldest runner-waiting CI job age → Gatus
# `platform_ci-queue-age`.
#
# Lists recent queued + in_progress runs, inspects each run's jobs, and fails
# when the oldest job with status `queued` — i.e. actually waiting for a
# runner — exceeds QUEUE_AGE_THRESHOLD_MIN (default 60). No waiting job =
# success. A `gh` failure, an unreadable jobs API, or an unparseable timestamp
# is a failure push (exit 1): queue invisibility is itself an incident for a
# status page that claims CI health.
#
# Why job-level (case_22859569): the previous run-level measurement (`gh run list
# --status queued`, oldest run createdAt, threshold 30) paged on a healthy
# fleet. GitHub keeps a run `queued` while its jobs are already executing
# (observed 2026-10-01: a run listed queued with jobs completing), and 30m sat
# inside normal backlog waits (14–34m job waits on the 7-listener
# two-isolated fleet during ordinary PR bursts). The page had no allowlisted
# remediation because nothing was stuck — the fleet was draining.
#
# Jobs blocked on `needs:` wait on other jobs, not on the fleet, and would
# otherwise age with the run's longest job — so they are excluded. In this
# repo that is exactly one job, the failure-log-drain gate (`needs:` every
# suite, `if: failure()`); override with QUEUE_AGE_IGNORE_JOB if that changes.
#
# Multi-repo (case_fb6d8052): the node7b1ff338 fleet serves several repos, and a
# single-repo script left the host running a hand-edited copy that still
# measured run-level age at 30m — the exact false page case_22859569 removed.
# GH_REPOS takes a whitespace-separated owner/repo list; the failure text
# names the repo holding the oldest waiting job. Only runs created more than
# the threshold ago are inspected: a job is never older than its run, so
# younger runs cannot breach, and skipping them keeps the per-run jobs calls
# bounded as repos are added.
#
# Server-side age filter (case_6bb27f43): the run list sorts newest-first, so a
# client-side `--limit 50` kept the 50 NEWEST runs and dropped the oldest.
# On 2026-10-02 two-bot-next held 116 queued runs; the page read 99m while a
# job had waited ~260m, and a burst of 50+ fresh runs would have hidden the
# stuck one entirely (success=true on a dead fleet). `--created "<cutoff"`
# makes GitHub return only breach-eligible runs; they are walked oldest-first
# and the walk stops at the first run younger than the oldest waiting job
# found so far, so a wedged backlog costs one jobs call, not one per run. A
# list that still fills QUEUE_AGE_RUN_LIMIT is a failure push: the oldest
# runs are the ones cut off.
#
# Shared measurement (case_cce0de98): the job-level walk above lives in
# _ci_queue_age.sh and is also what the queue-wait forecaster
# (push-queue-forecast.sh) samples, so the incident this alert owns and the
# trend the forecaster extrapolates are the same number.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN, GH_REPOS (owner/repo list; GH_REPO
# alone still works), QUEUE_AGE_THRESHOLD_MIN (default 60),
# QUEUE_AGE_IGNORE_JOB (default "Failure log drain"), QUEUE_AGE_RUN_LIMIT
# (default 1000, per status per repo), plus whatever the gh credential shim
# needs. The host gh must support `run list --created`; an older gh fails
# closed (a "gh run list ... failed" failure push), never green. The jobs
# lookup needs the same `actions:read` class as the run list itself, for every
# listed repo.
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-ci-queue-age.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"
# shellcheck source=_ci_queue_age.sh
. "$HERE/_ci_queue_age.sh"

THRESHOLD_MIN="${QUEUE_AGE_THRESHOLD_MIN:-60}"
RUN_LIMIT="${QUEUE_AGE_RUN_LIMIT:-1000}"
GH_BIN="${GH_BIN:-gh}"
[[ "$THRESHOLD_MIN" =~ ^[0-9]+$ ]] \
  || { echo "push-ci-queue-age: refusing — QUEUE_AGE_THRESHOLD_MIN must be whole minutes" >&2; exit 2; }
[[ "$RUN_LIMIT" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-ci-queue-age: refusing — QUEUE_AGE_RUN_LIMIT must be a positive integer" >&2; exit 2; }
read -r -a REPOS <<<"${GH_REPOS:-${GH_REPO:-}}"
(( ${#REPOS[@]} > 0 )) \
  || { echo "push-ci-queue-age: refusing — GH_REPOS (or GH_REPO) must be set" >&2; exit 2; }
for repo in "${REPOS[@]}"; do
  # GitHub owners carry no dots; `.`/`..` as a name would walk the API path.
  [[ "$repo" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$ && "${repo#*/}" != "." && "${repo#*/}" != ".." ]] \
    || { echo "push-ci-queue-age: refusing — bad repo '$repo' (want owner/repo)" >&2; exit 2; }
done

command -v "$GH_BIN" >/dev/null 2>&1 \
  || { echo "push-ci-queue-age: refusing — no '$GH_BIN' on PATH" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-ci-queue-age: refusing — jq required" >&2; exit 2; }

# The job-level measurement lives in _ci_queue_age.sh (shared with the
# queue-wait forecaster, push-queue-forecast.sh); it reads GH_BIN,
# QUEUE_AGE_IGNORE_JOB and QUEUE_AGE_RUN_LIMIT from this environment. Only
# runs older than the threshold are inspected: a younger job cannot breach.
ci_oldest_waiting_job "$THRESHOLD_MIN" "${REPOS[@]}" \
  || { gatus_push platform_ci-queue-age false "$CQA_ERROR"; exit 1; }
now="$CQA_NOW"
oldest="$CQA_OLDEST"
oldest_repo="$CQA_OLDEST_REPO"
jobs_unreadable="$CQA_JOBS_UNREADABLE"

if [[ -z "$oldest" ]]; then
  if (( jobs_unreadable > 0 )); then
    gatus_push platform_ci-queue-age false "jobs API unreadable for $jobs_unreadable run(s)"
    exit 1
  fi
  gatus_push platform_ci-queue-age true   # nothing waiting: nothing stale
  exit 0
fi

then="$(date -d "$oldest" +%s 2>/dev/null)" \
  || { gatus_push platform_ci-queue-age false "unparseable timestamp $oldest"; exit 1; }
age_min=$(( (now - then) / 60 ))

if (( age_min > 10#$THRESHOLD_MIN )); then
  gatus_push platform_ci-queue-age false "oldest runner-waiting job ${age_min}m old in ${oldest_repo} (threshold ${THRESHOLD_MIN}m)"
else
  gatus_push platform_ci-queue-age true
fi
