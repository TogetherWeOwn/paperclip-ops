# _ci_queue_age.sh — shared runner-waiting-job measurement for the CI
# queue-age alert (push-ci-queue-age.sh) and the queue-wait forecaster
# (push-queue-forecast.sh).
#
# Sourced, never executed. Provides one function:
#
#   ci_oldest_waiting_job <min-age-min> <owner/repo>...
#
# Finds the oldest job in `queued` status — a job actually waiting for a
# runner — across the listed repos. Only runs created more than <min-age-min>
# ago are inspected (server-side `--created "<cutoff"`): a job is never older
# than its run, so younger runs cannot hold a job older than the floor, and
# skipping them keeps the per-run jobs calls bounded as repos are added.
# The alert passes its threshold (a younger job cannot breach); the forecaster
# passes a small floor, because a forecast needs the sub-threshold ages.
#
# Why job-level, with the server-side filter (case_22859569, case_6bb27f43): the
# run-level `gh run list --status queued` age pages on a healthy fleet (GitHub
# keeps a run `queued` while its jobs already execute), and the newest-first
# `--limit` keeps the NEWEST runs and drops the oldest — a burst of fresh runs
# hid a ~260m-stuck job. Both callers measure through here so the incident the
# alert owns and the trend the forecaster extrapolates are the same number.
#
# Jobs blocked on `needs:` wait on other jobs, not on the fleet, so the
# IGNORE_JOB-named job (default: the failure-log-drain gate) is excluded.
# Runs are walked oldest-first and the walk stops at the first run younger
# than the oldest waiting job found so far, so a wedged backlog costs one jobs
# call, not one per run.
#
# Inputs (callers validate them and refuse exit 2 before calling):
#   GH_BIN                (default gh)
#   QUEUE_AGE_IGNORE_JOB  (default "Failure log drain")
#   QUEUE_AGE_RUN_LIMIT   (default 1000, per status per repo; a list that
#                          fills it is an error — the oldest runs are the
#                          ones cut off)
#
# Outputs (globals, reset on every call):
#   CQA_NOW              epoch seconds the cutoff was computed from
#   CQA_OLDEST           created_at of the oldest runner-waiting job, "" none
#   CQA_OLDEST_REPO      repo holding it
#   CQA_JOBS_UNREADABLE  runs whose jobs API could not be read
#   CQA_RUNS_INSPECTED   breach-eligible runs listed across repos (queued plus
#                        in_progress older than the floor; lets the alert
#                        report queue depth alongside age so a single wedged
#                        job and a fleet saturation no longer page identically)
#   CQA_ERROR            failure text when the call returns 1
# Returns 0 when the lists were read (CQA_OLDEST may still be empty);
# returns 1 with CQA_ERROR set when a run list failed, was truncated or was
# unparseable — callers fail closed (queue invisibility is itself an incident).
# A run whose jobs API fails is counted in CQA_JOBS_UNREADABLE instead: the
# caller decides, because an oldest job found elsewhere still stands.
ci_oldest_waiting_job() {
  # Sourced, never executed: no top-level `set -e` here, or it would leak
  # into every caller. Enable errexit for this call only and restore the
  # caller's setting on every return path. Every expected failure below
  # (gh, jq, date) stays guarded with `||`, so those paths still return
  # 1 / count CQA_JOBS_UNREADABLE instead of exiting the caller's shell.
  local _cqa_saved_opts="$-"
  set -e
  local min_age="$1"; shift
  local gh_bin="${GH_BIN:-gh}"
  local ignore_job="${QUEUE_AGE_IGNORE_JOB:-Failure log drain}"
  local run_limit="${QUEUE_AGE_RUN_LIMIT:-1000}"
  local repo cutoff queued_runs active_runs runs created rid jobs cand

  CQA_NOW="$(date +%s)"
  CQA_OLDEST=""
  CQA_OLDEST_REPO=""
  CQA_JOBS_UNREADABLE=0
  CQA_RUNS_INSPECTED=0
  CQA_ERROR=""

  # 10# forces base-10: bash arithmetic treats a leading-0 value as octal, so
  # a minutes value of 08/09 would fail this expansion and fail open (empty
  # cutoff matches nothing → nothing waiting). Callers allow leading zeros, so
  # pin decimal here (case_0b2d9007).
  cutoff="$(date -u -d "@$(( CQA_NOW - 10#$min_age * 60 ))" +%Y-%m-%dT%H:%M:%SZ)" \
    || { CQA_ERROR="could not compute the run-age cutoff"; [[ "$_cqa_saved_opts" == *e* ]] || set +e; return 1; }

  for repo in "$@"; do
    # Runs that may hold runner-waiting jobs, created before the cutoff only
    # (server-side, so the newest-first limit cannot drop the oldest runs).
    queued_runs="$("$gh_bin" run list --repo "$repo" --status queued --created "<$cutoff" \
                     --limit "$run_limit" --json databaseId,createdAt 2>&1)" \
      || { CQA_ERROR="gh run list (queued) failed for $repo"; [[ "$_cqa_saved_opts" == *e* ]] || set +e; return 1; }
    active_runs="$("$gh_bin" run list --repo "$repo" --status in_progress --created "<$cutoff" \
                     --limit "$run_limit" --json databaseId,createdAt 2>&1)" \
      || { CQA_ERROR="gh run list (in_progress) failed for $repo"; [[ "$_cqa_saved_opts" == *e* ]] || set +e; return 1; }

    # "<createdAt> <id>" oldest-first. The client-side cutoff re-check is kept
    # so a gh that ignores --created still never inspects a young run.
    runs="$(jq -rs --arg c "$cutoff" --argjson lim "$run_limit" '
              if any(.[]; length >= $lim) then error("truncated") else . end
              | add | map(select(.createdAt < $c)) | unique_by(.databaseId)
              | sort_by(.createdAt) | .[] | "\(.createdAt) \(.databaseId)"' \
            <<<"$queued_runs$active_runs" 2>&1)" \
      || { if [[ "$runs" == *truncated* ]]; then
             CQA_ERROR="run list hit QUEUE_AGE_RUN_LIMIT=$run_limit for $repo; oldest runs unseen"
           else
             CQA_ERROR="unparseable run list for $repo"
           fi
           [[ "$_cqa_saved_opts" == *e* ]] || set +e; return 1; }

    # Depth for triage: every listed run is breach-eligible demand (older
    # than the floor), whether or not the oldest-first walk below reaches
    # its jobs call before the early stop. An empty list adds nothing;
    # grep -c . counts only non-empty lines.
    if [[ -n "$runs" ]]; then
      CQA_RUNS_INSPECTED=$((CQA_RUNS_INSPECTED + $(printf '%s\n' "$runs" | grep -c .)))
    fi

    while read -r created rid; do
      [[ -n "$rid" ]] || continue
      # Oldest-first: no later run can hold a job older than the best so far.
      [[ -n "$CQA_OLDEST" && ! "$created" < "$CQA_OLDEST" ]] && break
      jobs="$("$gh_bin" api "repos/$repo/actions/runs/$rid/jobs?per_page=100" 2>&1)" \
        || { CQA_JOBS_UNREADABLE=$((CQA_JOBS_UNREADABLE + 1)); continue; }
      # Runner-waiting jobs only: status queued, minus the needs-blocked job.
      cand="$(jq -r --arg ignore "$ignore_job" \
        '[.jobs[] | select(.status == "queued" and .name != $ignore) | .created_at] | sort | first // empty' \
        <<<"$jobs")" \
        || { CQA_JOBS_UNREADABLE=$((CQA_JOBS_UNREADABLE + 1)); continue; }
      if [[ -n "$cand" ]] && { [[ -z "$CQA_OLDEST" ]] || [[ "$cand" < "$CQA_OLDEST" ]]; }; then
        CQA_OLDEST="$cand"
        CQA_OLDEST_REPO="$repo"
      fi
    done <<<"$runs"
  done
  [[ "$_cqa_saved_opts" == *e* ]] || set +e
  return 0
}
