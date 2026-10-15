#!/usr/bin/env bash
# push-queue-forecast.sh — cron: CI queue-wait forecast → Gatus
# `platform_ci-queue-forecast`.
#
# Each tick samples the age of the oldest RUNNER-WAITING JOB for GH_REPO
# through the SAME measurement push-ci-queue-age.sh alerts on (the shared
# _ci_queue_age.sh: job-level `queued` status minus the needs-blocked drain
# job, runs listed server-side with `--created` so a burst of fresh runs
# cannot hide the oldest), appends it to a local samples file, and forecasts
# whether the least-squares age trend crosses the saturation threshold within
# the horizon. Sampling the same number is what makes the two endpoints tile:
# a run-level age (the pre-case_22859569 measure) reads queued-but-draining runs as
# waiting and would disagree with the alert in both directions.
#
# COMPLEMENTS, DOES NOT DUPLICATE, the queue-age alert: the `platform/ci-queue-age`
# endpoint fires when the oldest waiting job is ALREADY past
# QUEUE_AGE_THRESHOLD_MIN (default 60, the alert's default). This forecaster
# fires only while the age is still AT OR BELOW threshold but the trend
# predicts a crossing within FORECAST_HORIZON_MIN. When the queue is already
# saturated (age strictly above threshold, the alert's own comparison) it
# pushes success=true and names the queue-age alert as the owner, so one
# incident never pages twice and no age is left owned by neither.
#
# SAMPLE FLOOR: only runs older than FORECAST_MIN_AGE_MIN (default 5) are
# inspected, which bounds the per-run jobs calls; a wait below the floor is
# normal pickup latency and samples as age 0. Set 0 to sample every run.
#
# FORECAST HORIZON: FORECAST_HORIZON_MIN (default 60) ahead, from a
# least-squares trend over the last FORECAST_LOOKBACK_MIN (default 120) of
# samples. The horizon is a reporting window, not a guarantee: jobs queued
# after the last sample are invisible until the next tick.
#
# STALENESS HANDLING: a gap since the previous sample beyond
# FORECAST_STALE_AFTER_MIN (default 20, about 4 missed 5-min ticks) means the
# trend crosses a blind window, so the script pushes success=false
# (fail-closed: a blind forecaster never reads green). Fewer than 2 samples,
# or samples spanning less than FORECAST_MIN_SPAN_MIN (default 10), pushes
# success=true "warming up" — no history is not an incident.
#
# FAIL-CLOSED: a `gh` failure, an unreadable/unparseable samples file, or an
# unwritable state dir pushes success=false, exit 1. Missing GATUS_URL/token
# or GH_REPO refuses (exit 2) before touching the network.
#
# READ-ONLY: this script runs `gh run list` and `gh api .../runs/<id>/jobs`
# reads only — no dispatches, no re-runs, no execution target. It reads
# runner-waiting job age and nothing else, and appends one "<epoch> <age_min>"
# line per tick to the state-dir samples file. The jobs lookup needs the same
# `actions:read` class as the run list, and the host gh must support
# `run list --created` (an older gh fails closed, never green).
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), GH_REPO
#   (owner/repo, required), FORECAST_STATE_DIR (default
#   /var/lib/status-page/queue-forecast), QUEUE_AGE_THRESHOLD_MIN (default
#   60, match the alert), FORECAST_HORIZON_MIN (default 60),
#   FORECAST_LOOKBACK_MIN (default 120), FORECAST_STALE_AFTER_MIN (default
#   20), FORECAST_MIN_SPAN_MIN (default 10), FORECAST_MIN_AGE_MIN (default
#   5), QUEUE_AGE_IGNORE_JOB (default "Failure log drain") and
#   QUEUE_AGE_RUN_LIMIT (default 1000), both read by _ci_queue_age.sh,
#   FORECAST_KEY_SUFFIX (optional per-repo fan-out, e.g. "-two-web-next" →
#   key platform_ci-queue-forecast-two-web-next).
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-queue-forecast.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"
# shellcheck source=_ci_queue_age.sh
. "$HERE/_ci_queue_age.sh"

THRESHOLD_MIN="${QUEUE_AGE_THRESHOLD_MIN:-60}"
HORIZON_MIN="${FORECAST_HORIZON_MIN:-60}"
LOOKBACK_MIN="${FORECAST_LOOKBACK_MIN:-120}"
STALE_AFTER_MIN="${FORECAST_STALE_AFTER_MIN:-20}"
MIN_SPAN_MIN="${FORECAST_MIN_SPAN_MIN:-10}"
MIN_AGE_MIN="${FORECAST_MIN_AGE_MIN:-5}"
STATE_DIR="${FORECAST_STATE_DIR:-/var/lib/status-page/queue-forecast}"
KEY_SUFFIX="${FORECAST_KEY_SUFFIX:-}"
GH_BIN="${GH_BIN:-gh}"
[[ -n "${GH_REPO:-}" ]] \
  || { echo "push-queue-forecast: refusing — GH_REPO (owner/repo) must be set" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-queue-forecast: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-queue-forecast: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
[[ -z "$KEY_SUFFIX" || "$KEY_SUFFIX" =~ ^-[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
  || { echo "push-queue-forecast: refusing — FORECAST_KEY_SUFFIX must be empty or -slug" >&2; exit 2; }
# Same shape check as push-ci-queue-age.sh: GitHub owners carry no dots, and
# `.`/`..` as a name would walk the API path.
[[ "$GH_REPO" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$ && "${GH_REPO#*/}" != "." && "${GH_REPO#*/}" != ".." ]] \
  || { echo "push-queue-forecast: refusing — bad GH_REPO '$GH_REPO' (want owner/repo)" >&2; exit 2; }
for v in QUEUE_AGE_THRESHOLD_MIN:"$THRESHOLD_MIN" FORECAST_HORIZON_MIN:"$HORIZON_MIN" \
         FORECAST_LOOKBACK_MIN:"$LOOKBACK_MIN" FORECAST_STALE_AFTER_MIN:"$STALE_AFTER_MIN" \
         FORECAST_MIN_SPAN_MIN:"$MIN_SPAN_MIN" FORECAST_MIN_AGE_MIN:"$MIN_AGE_MIN"; do
  [[ "${v#*:}" =~ ^[0-9]+$ ]] \
    || { echo "push-queue-forecast: refusing — ${v%%:*} must be whole minutes" >&2; exit 2; }
done
[[ "${QUEUE_AGE_RUN_LIMIT:-1000}" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-queue-forecast: refusing — QUEUE_AGE_RUN_LIMIT must be a positive integer" >&2; exit 2; }

KEY="platform_ci-queue-forecast${KEY_SUFFIX}"
SAMPLES="$STATE_DIR/queue-age-samples${KEY_SUFFIX}.log"

command -v "$GH_BIN" >/dev/null 2>&1 \
  || { echo "push-queue-forecast: refusing — no '$GH_BIN' on PATH" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-queue-forecast: refusing — python3 required" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-queue-forecast: refusing — jq required" >&2; exit 2; }

fail_closed() {  # <message> — push DOWN, exit 1
  gatus_push "$KEY" false "$1" || true
  exit 1
}

mkdir -p "$STATE_DIR" 2>/dev/null \
  || fail_closed "state dir unwritable: $STATE_DIR (fail-closed DOWN)"
[[ -w "$STATE_DIR" ]] \
  || fail_closed "state dir unwritable: $STATE_DIR (fail-closed DOWN)"
if [[ -e "$SAMPLES" && ! -r "$SAMPLES" ]]; then
  fail_closed "samples file unreadable: $SAMPLES (fail-closed DOWN)"
fi

# Job-level, server-filtered measurement shared with the alert (see header).
ci_oldest_waiting_job "$MIN_AGE_MIN" "$GH_REPO" \
  || fail_closed "$CQA_ERROR (fail-closed DOWN)"

now="$CQA_NOW"
if [[ -z "$CQA_OLDEST" ]]; then
  # No waiting job found. A jobs API we could not read means we cannot say
  # that: blind is DOWN, never a drained-queue sample (same rule as the alert).
  (( CQA_JOBS_UNREADABLE > 0 )) \
    && fail_closed "jobs API unreadable for $CQA_JOBS_UNREADABLE run(s) (fail-closed DOWN)"
  age_min=0  # nothing waiting: the queue drained; still sampled below
else
  then="$(date -d "$CQA_OLDEST" +%s 2>/dev/null)" \
    || fail_closed "unparseable timestamp $CQA_OLDEST (fail-closed DOWN)"
  age_min=$(( (now - then) / 60 ))
  (( age_min < 0 )) && age_min=0
fi

# Append the sample, prune to the lookback window, and evaluate in one
# python3 pass (stdlib only): prints "<true|false>\t<message>".
EVAL="$(python3 - "$SAMPLES" "$now" "$age_min" "$THRESHOLD_MIN" "$HORIZON_MIN" \
  "$LOOKBACK_MIN" "$STALE_AFTER_MIN" "$MIN_SPAN_MIN" <<'PY'
import sys

path, now_s, age_s, thr_s, hor_s, look_s, stale_s, span_s = sys.argv[1:9]
now, age = int(now_s), int(age_s)
thr, hor, look, stale_after, min_span = (int(thr_s), int(hor_s), int(look_s),
                                         int(stale_s), int(span_s))

try:
    with open(path, encoding="utf-8") as h:
        raw = h.read().splitlines()
except FileNotFoundError:
    raw = []
except OSError as exc:
    print(f"false\tsamples file unreadable ({exc}); fail-closed DOWN")
    sys.exit(0)

samples = []
for ln, line in enumerate(raw, 1):
    line = line.strip()
    if not line:
        continue
    try:
        ts_s, age_s2 = line.split()
        ts, a = int(ts_s), int(age_s2)
    except ValueError:
        print(f"false\tsamples line {ln} unparseable; fail-closed DOWN")
        sys.exit(0)
    if ts > now or a < 0:
        print(f"false\tsamples line {ln} out of range; fail-closed DOWN")
        sys.exit(0)
    samples.append((ts, a))

prev_ts = samples[-1][0] if samples else None
samples.append((now, age))
samples = [(t, a) for (t, a) in samples if t >= now - look * 60]
try:
    with open(path, "w", encoding="utf-8") as h:
        for t, a in samples:
            h.write(f"{t} {a}\n")
except OSError as exc:
    print(f"false\tsamples file unwritable ({exc}); fail-closed DOWN")
    sys.exit(0)

# Saturated first: the current reading is fresh direct evidence, so a
# blind trend must not override it — the queue-age alert owns the incident.
# Strictly above, the alert's own comparison: at exactly the threshold the
# alert is still green, so the forecaster must still be watching.
if age > thr:
    print(f"true\tsaturated now ({age}m > {thr}m); "
          "queue-age alert owns this incident")
    sys.exit(0)

if prev_ts is not None and (now - prev_ts) > stale_after * 60:
    gap = (now - prev_ts) // 60
    print(f"false\tsamples stale: last sample {gap}m ago "
          f"(> {stale_after}m); trend blind, fail-closed DOWN")
    sys.exit(0)

if len(samples) < 2 or (samples[-1][0] - samples[0][0]) < min_span * 60:
    span = (samples[-1][0] - samples[0][0]) // 60 if samples else 0
    print(f"true\twarming up ({len(samples)} samples over {span}m); "
          "no history is not an incident")
    sys.exit(0)

n = len(samples)
sx = sum(t for t, _ in samples)
sy = sum(a for _, a in samples)
sxx = sum(t * t for t, _ in samples)
sxy = sum(t * a for t, a in samples)
den = n * sxx - sx * sx
slope = (n * sxy - sx * sy) / den if den else 0.0  # age-min per second
slope_min = slope * 60  # age-min per wall-clock minute

if slope_min <= 0:
    print(f"true\tno saturation forecast "
          f"({age}m now, trend {slope_min:+.2f}m/min, threshold {thr}m)")
    sys.exit(0)

mins_to_cross = (thr - age) / slope_min
if mins_to_cross <= hor:
    print(f"false\tforecast: {age}m now, trend {slope_min:+.2f}m/min, "
          f"crosses {thr}m threshold in ~{mins_to_cross:.0f}m "
          f"(horizon {hor}m)")
else:
    print(f"true\tno saturation forecast "
          f"({age}m now, trend {slope_min:+.2f}m/min, "
          f"crossing in ~{mins_to_cross:.0f}m beyond {hor}m horizon)")
PY
)" || fail_closed "forecast evaluator failed (fail-closed DOWN)"

success="${EVAL%%$'\t'*}"
msg="${EVAL#*$'\t'}"
echo "push-queue-forecast: $msg"  # verdict lands in the cron log, not just the push
if [[ "$success" == "true" ]]; then
  gatus_push "$KEY" true || exit 1
else
  gatus_push "$KEY" false "$msg" || true
  exit 1  # a saturation forecast is an incident: cron must report it
fi
