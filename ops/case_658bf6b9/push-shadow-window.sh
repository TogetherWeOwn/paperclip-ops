#!/usr/bin/env bash
# push-shadow-window.sh — cron: router shadow-log freshness + joint
# window-landing gauge → Gatus (`router_shadow-freshness`,
# `router_window-landing`).
#
# Reads local JSON inputs (one log file or an hourly-shard directory plus a
# gauge) and pushes two booleans; that is all it does:
#   SHADOW_DECISIONS (required): plugin shadow decision log, either one
#     JSON-lines file or its directory of hourly
#     decisions-YYYY-MM-DD-HHZ.jsonl shards. The newest plugin-shadow record
#     across the retained 48 shards sets the freshness age (now - record.ts).
#     Host records are ignored for freshness; the case_3419ac9d watchdog owns the
#     absence alert. Records are streamed one line at a time; a line over
#     1,048,576 characters fails closed.
#   WINDOW_GAUGE (required): case_6572bf23 window-landing gauge JSON readout
#     (schema case_e8e340c1-window-landing-gauge-v1), already produced from
#     authorized observations by its own producer. This script never builds
#     a gauge from quotas: it reads joint.onTrack98_100 and reports it.
#     The checked-in example fixture is tests-only, labelled with "fixture":
#     true, and always rejected; it can never produce a green live endpoint.
#
# FRESHNESS THRESHOLDS: SHADOW_STALE_AFTER_SEC (default 3600 — one hour)
# bounds the age of the newest shadow record. With no shadow record,
# SHADOW_WARMUP_MAX_SEC (default 600) bounds time since the first valid
# decision record or empty-file mtime; missing/empty shard directories and
# expired warm-up push success=false. Malformed lines (bad JSON,
# missing/unparseable ts) also push false. One exception: a final line with
# NO trailing newline that is not parseable JSON is ignored only when an
# earlier complete record in the same shard was parsed — noted as "tornTail": true
# in the verdict. A lone malformed tail or concatenated records without a newline is
# malformed and DOWN. A malformed tail in an older shard, a line in the middle
# of a log, or a newline-terminated malformed line is also DOWN.
#
# GAUGE FRESHNESS: the gauge's own `evaluatedAt` (epoch MILLISECONDS, the
# producer's clock) must be no older than WINDOW_GAUGE_MAX_AGE_SEC (default
# 600 — 2x an assumed 5-minute producer cadence, tolerating one missed
# producer run; re-size it when the live case_6572bf23 producer is scheduled).
# A dead producer leaves the last readout on disk, and a pusher that keeps
# running would otherwise report that frozen readout as healthy forever.
# An `evaluatedAt` that is older than the max age, missing, non-numeric, or
# in the future (no skew allowance, same as the freshness check) pushes
# success=false with landing.state "stale-gauge" — a state no live-gauge
# verdict ever carries. Check order for landing: bad config, unreadable
# gauge, fixture marker, gauge age, then the band.
#
# FIXTURE REJECTION: the checked-in example fixture carries a top-level
# "fixture": true marker and a frozen evaluatedAt. Every marked gauge is
# rejected as example data (landing.state "fixture-refused"), regardless of
# its values or environment; the live landing key can never receive fixture
# success. A copy with the marker removed is a live-shaped gauge with an
# ancient evaluatedAt and is rejected as stale-gauge, DOWN.
#
# LANDING BAND: the case_91cf972c objective, 98-100%. UP exactly when the
# gauge's joint.onTrack98_100 is true; DOWN when false, when the projection
# is null (window not started: no rate yet), or on missing/malformed/
# wrong-schema input. The push message names the joint projected landing
# and status so the page shows where the week lands, not just up/down.
#
# JSON VERDICT: the evaluators print one line of case_e0da19dc-shadow-window-v1
# JSON, which this script echoes to stdout (the cron log), so the module
# returns JSON on fixtures:
# {"schema":"case_e0da19dc-shadow-window-v1","freshness":{...},"landing":{...}}.
#
# FAIL-CLOSED, ONE EVALUATOR PER ENDPOINT: freshness and landing are
# evaluated by separate python3 runs, so a failure in one never takes the
# other DOWN: an unreadable/malformed SHADOW_DECISIONS pushes shadow-false;
# an unreadable/malformed/wrong-schema/garbled WINDOW_GAUGE pushes
# landing-false; an evaluator crash pushes false for ITS endpoint only (a
# blind monitor never reads green); a DOWN endpoint exits 1 so cron reports
# it. Missing SHADOW_DECISIONS, WINDOW_GAUGE, GATUS_URL or
# GATUS_EXTERNAL_TOKEN refuses (exit 2) before touching the network.
# SHADOW_KEY_SUFFIX must be empty or -slug (path traversal guard);
# NOW_OVERRIDE (epoch seconds, tests only) pins the clock.
#
# READ-ONLY / NO SECRETS: reads two local files, POSTs booleans. Never
# fetches, never resolves secrets, never writes ledgers, never admits work,
# never changes pacing or enforcement. Quota/consumed numbers are
# operational metadata, not secrets. Bearer travels in a 0600 curl config,
# never argv (see _gatus_push.sh).
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh),
#   SHADOW_DECISIONS (file or hourly-shard directory, required), WINDOW_GAUGE
#   (live gauge JSON path, required), SHADOW_STALE_AFTER_SEC (default 3600),
#   SHADOW_WARMUP_MAX_SEC (default 600), WINDOW_GAUGE_MAX_AGE_SEC (default
#   600), SHADOW_KEY_SUFFIX (optional per-source fan-out), NOW_OVERRIDE
#   (tests only).
# Cron (operator): */5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/status-page/current/ops/case_658bf6b9/push-shadow-window.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

STALE_AFTER_SEC="${SHADOW_STALE_AFTER_SEC:-3600}"
WARMUP_MAX_SEC="${SHADOW_WARMUP_MAX_SEC:-600}"
GAUGE_MAX_AGE_SEC="${WINDOW_GAUGE_MAX_AGE_SEC:-600}"
KEY_SUFFIX="${SHADOW_KEY_SUFFIX:-}"
NOW_PIN="${NOW_OVERRIDE:-}"

[[ -n "${SHADOW_DECISIONS:-}" ]] \
  || { echo "push-shadow-window: refusing — SHADOW_DECISIONS is unset" >&2; exit 2; }
[[ -n "${WINDOW_GAUGE:-}" ]] \
  || { echo "push-shadow-window: refusing — WINDOW_GAUGE is unset" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-shadow-window: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-shadow-window: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
[[ -z "$KEY_SUFFIX" || "$KEY_SUFFIX" =~ ^-[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
  || { echo "push-shadow-window: refusing — SHADOW_KEY_SUFFIX must be empty or -slug" >&2; exit 2; }

FRESH_KEY="router_shadow-freshness${KEY_SUFFIX}"
LAND_KEY="router_window-landing${KEY_SUFFIX}"

command -v python3 >/dev/null 2>&1 \
  || { echo "push-shadow-window: refusing — python3 required" >&2; exit 2; }

# One python3 program (stdlib only), run once per endpoint so a crash or a
# garbled input on one side never takes the other DOWN. Each run prints two
# lines: a "<FRESH|LAND>\t<true|false>\t<message>" control line, then that
# endpoint's half of the case_e0da19dc-shadow-window-v1 verdict as one JSON object.
read -r -d '' EVALPY <<'PY' || true
import datetime, json, math, os, re, sys
from pathlib import Path

mode = sys.argv[1]
now_pin = os.environ.get("NOW_OVERRIDE") or ""
now = datetime.datetime.fromtimestamp(int(now_pin), datetime.timezone.utc) \
    if now_pin.strip().isdigit() else datetime.datetime.now(datetime.timezone.utc)

def clean(text):
    # The control line is tab/newline-delimited: keep any exception text on one line.
    return " ".join(str(text).split())

def done(key, success, message, verdict):
    print(f"{key}\t{'true' if success else 'false'}\t{clean(message)}")
    print(json.dumps(verdict))
    sys.exit(0)

def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(datetime.timezone.utc)

def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:  # an int too large for a float is garbage, not a time
        return False

# --- freshness: newest plugin-shadow record across retained hourly shards ------
if mode == "fresh":
    dec_path, stale_s = sys.argv[2:4]
    warmup_s = sys.argv[4]
    try:
        stale = int(stale_s)
    except ValueError:
        done("FRESH", False, f"SHADOW_STALE_AFTER_SEC invalid ({stale_s!r}); fail-closed DOWN",
             {"state": "bad-config", "ageSeconds": None, "note": "bad SHADOW_STALE_AFTER_SEC"})
    try:
        warmup_max = int(warmup_s)
    except ValueError:
        done("FRESH", False, f"SHADOW_WARMUP_MAX_SEC invalid ({warmup_s!r}); fail-closed DOWN",
             {"state": "bad-config", "ageSeconds": None, "note": "bad SHADOW_WARMUP_MAX_SEC"})
    try:
        source = Path(dec_path)
        if source.is_dir():
            paths = sorted(
                (p for p in source.iterdir()
                 if re.fullmatch(r"decisions-\d{4}-\d{2}-\d{2}-\d{2}Z\.jsonl", p.name)
                 and p.is_file()),
                key=lambda p: p.name,
            )[-48:]
            if not paths:
                raise OSError("no decisions-YYYY-MM-DD-HHZ.jsonl shards found")
        elif source.is_file():
            paths = [source]
        else:
            raise OSError("shadow decision path is not a readable file or directory")

        max_line_chars = 1024 * 1024
        newest, first_activity, malformed, torn = None, None, None, False
        for path_index, path in enumerate(paths):
            saw_complete_line = False
            with path.open(encoding="utf-8") as h:
                ln = 0
                while True:
                    raw_line = h.readline(max_line_chars + 1)
                    if raw_line == "":
                        break
                    ln += 1
                    unterminated = not raw_line.endswith("\n")
                    line = raw_line[:-1] if not unterminated else raw_line
                    if len(line) > max_line_chars:
                        malformed = f"{path.name} line {ln} exceeds {max_line_chars} characters"
                        break
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as exc:
                        if (path_index == len(paths) - 1 and unterminated
                                and saw_complete_line and exc.msg != "Extra data"):
                            torn = True  # ignore only the final partial append in this shard
                            break
                        malformed = f"{path.name} line {ln} is not JSON"
                        break
                    if not isinstance(rec, dict):
                        malformed = f"{path.name} line {ln} is not an object"
                        break
                    ts = parse_ts(rec.get("ts"))
                    if ts is None:
                        malformed = f"{path.name} line {ln} has missing/unparseable ts"
                        break
                    saw_complete_line = True
                    if first_activity is None or ts < first_activity:
                        first_activity = ts
                    if rec.get("writer") == "plugin-shadow" and (newest is None or ts > newest):
                        newest = ts
            if malformed is not None:
                break
        if malformed is None and first_activity is None:
            first_activity = min(
                datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.timezone.utc)
                for p in paths
            )
    except (OSError, UnicodeDecodeError) as exc:
        done("FRESH", False, f"shadow log unreadable ({exc}); fail-closed DOWN",
             {"state": "unreadable", "ageSeconds": None, "note": clean(exc)})

    def verdict(state, age, note):
        v = {"state": state, "ageSeconds": age, "note": note}
        if torn:
            v["tornTail"] = True
        return v

    torn_note = " (torn final line ignored)" if torn else ""
    if malformed is not None:
        done("FRESH", False, f"shadow log malformed ({malformed}); fail-closed DOWN",
             verdict("malformed", None, malformed))
    if newest is None:
        age = int((now - first_activity).total_seconds())
        if age < 0:
            done("FRESH", False, f"first decision record is in the future ({age}s); clock or log corrupt, fail-closed DOWN",
                 verdict("future", age, "future timestamp"))
        if age > warmup_max:
            done("FRESH", False, f"no shadow records after {age}s of warm-up (> {warmup_max}s); fail-closed DOWN{torn_note}",
                 verdict("warming-expired", age, "warm-up expired"))
        done("FRESH", True, f"no shadow records yet; warming up for {age}s of {warmup_max}s{torn_note}",
             verdict("warming", age, f"warming up for {age}s of {warmup_max}s"))
    age = int((now - newest).total_seconds())
    if age < 0:
        done("FRESH", False, f"newest shadow record is in the future ({age}s); clock or log corrupt, fail-closed DOWN",
             verdict("future", age, "future timestamp"))
    if age > stale:
        done("FRESH", False, f"shadow log stale: newest shadow record {age}s old (> {stale}s); fail-closed DOWN{torn_note}",
             verdict("stale", age, "stale"))
    done("FRESH", True, f"shadow log fresh: newest shadow record {age}s old (staleness horizon {stale}s){torn_note}",
         verdict("fresh", age, None))

# --- landing: case_6572bf23 gauge joint -------------------------------------------
if mode != "land":
    sys.exit(2)
gauge_path, max_age_s = sys.argv[2:4]

def landing(state, pct, note, age=None, source="live"):
    return {"state": state, "projectedLandingPct": pct, "note": note,
            "gaugeAgeSeconds": age, "source": source}

try:
    max_age = int(max_age_s)
    if max_age <= 0:
        raise ValueError("not positive")
except ValueError:
    done("LAND", False, f"WINDOW_GAUGE_MAX_AGE_SEC invalid ({max_age_s!r}); fail-closed DOWN",
         landing("bad-config", None, "bad WINDOW_GAUGE_MAX_AGE_SEC"))

try:
    with open(gauge_path, encoding="utf-8") as h:
        gauge = json.load(h)
    if not isinstance(gauge, dict):
        raise ValueError("gauge root must be an object")
    fixture = gauge.get("fixture") is True
    if fixture:
        done("LAND", False,
             "window gauge is marked as example fixture data, not live telemetry; "
             "refusing fixture on the live landing key; fail-closed DOWN",
             landing("fixture-refused", None, "fixture data is never live", None, "fixture"))
    if gauge.get("schema") != "case_e8e340c1-window-landing-gauge-v1":
        raise ValueError(f"unexpected schema {json.dumps(gauge.get('schema'))}; want case_e8e340c1-window-landing-gauge-v1")
    joint = gauge.get("joint")
    if not isinstance(joint, dict):
        raise ValueError("gauge.joint must be an object")
    proj = joint.get("projectedLandingPct")
    if proj is not None and not finite_number(proj):
        raise ValueError("joint.projectedLandingPct must be a finite number or null")
    landing_val = None if proj is None else float(proj)
    status = joint.get("status")
    on_track = joint.get("onTrack98_100")
except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
    done("LAND", False, f"window gauge unreadable ({exc}); fail-closed DOWN",
         landing("unreadable", None, clean(exc)))

age = None
evaluated = gauge.get("evaluatedAt")
stale_gauge = None
if evaluated is None:
    stale_gauge = "evaluatedAt missing"
elif not finite_number(evaluated):
    stale_gauge = "evaluatedAt is not a number"
else:
    age_exact = (now.timestamp() * 1000 - evaluated) / 1000
    age = math.floor(age_exact) if age_exact < 0 else math.ceil(age_exact)
    if age_exact < 0:
        stale_gauge = f"evaluatedAt is in the future by {-age_exact:.3f}s"
    elif age_exact > max_age:
        stale_gauge = f"evaluatedAt {age}s old (> {max_age}s)"
if stale_gauge is not None:
    done("LAND", False,
         f"window gauge stale: {stale_gauge}; producer dead or clock skewed (evaluatedAt is epoch ms); fail-closed DOWN",
         landing("stale-gauge", None, stale_gauge, age if isinstance(age, int) else None))

if landing_val is None:
    done("LAND", False, "joint projection null (window not started: no rate yet); fail-closed DOWN",
         landing("not-started", None, "not-started", age))
if on_track is True:
    done("LAND", True, f"joint landing {landing_val:.1f}% on-track 98-100% ({status})",
         landing("ontrack", landing_val, status, age))
done("LAND", False, f"joint landing {landing_val:.1f}% off the 98-100% band ({status}); fail-closed DOWN",
     landing("off-band", landing_val, status, age))
PY

# eval_endpoint <FRESH|LAND> <key> <python args...>: sets EP_LINE (control
# line) and EP_JSON (verdict half). An evaluator that crashes or prints an
# incomplete result is that endpoint's own fail-closed DOWN — never the other's.
eval_endpoint() {
  local tag="$1" key="$2"; shift 2
  local out
  out="$(NOW_OVERRIDE="$NOW_PIN" python3 -c "$EVALPY" "$@")" || out=""
  EP_LINE="$(grep "^$tag"$'\t' <<<"$out" | tail -n 1)"
  EP_JSON="$(grep '^{' <<<"$out" | tail -n 1)"
  if [[ -z "$EP_LINE" || -z "$EP_JSON" ]]; then
    echo "push-shadow-window: $key evaluator failed — fail-closed, marking it DOWN" >&2
    EP_LINE="$tag"$'\tfalse\tevaluator failure (fail-closed DOWN)'
    EP_JSON='{"state": "evaluator-failed", "note": "evaluator failure"}'
  fi
}

eval_endpoint FRESH "$FRESH_KEY" fresh "$SHADOW_DECISIONS" "$STALE_AFTER_SEC" "$WARMUP_MAX_SEC"
FRESH_LINE="$EP_LINE"; FRESH_JSON="$EP_JSON"
eval_endpoint LAND "$LAND_KEY" land "$WINDOW_GAUGE" "$GAUGE_MAX_AGE_SEC"
LAND_LINE="$EP_LINE"; LAND_JSON="$EP_JSON"

# Same text json.dumps would print: both halves are single-line JSON objects.
VERDICT="{\"schema\": \"case_e0da19dc-shadow-window-v1\", \"freshness\": $FRESH_JSON, \"landing\": $LAND_JSON}"
echo "push-shadow-window: $VERDICT"  # verdict lands in the cron log, not just the push
fresh_body="${FRESH_LINE#*$'\t'}"
fresh_success="${fresh_body%%$'\t'*}"; fresh_msg="${fresh_body#*$'\t'}"
land_body="${LAND_LINE#*$'\t'}"
land_success="${land_body%%$'\t'*}"; land_msg="${land_body#*$'\t'}"

rc=0
if [[ "$fresh_success" == "true" ]]; then
  gatus_push "$FRESH_KEY" true || rc=1
else
  gatus_push "$FRESH_KEY" false "$fresh_msg" || true
  rc=1  # a DOWN endpoint is an incident: cron must report it
fi
if [[ "$land_success" == "true" ]]; then
  gatus_push "$LAND_KEY" true || rc=1
else
  gatus_push "$LAND_KEY" false "$land_msg" || true
  rc=1
fi
exit "$rc"
