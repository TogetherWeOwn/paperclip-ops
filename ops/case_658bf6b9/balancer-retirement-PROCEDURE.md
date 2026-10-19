# Balancer retirement procedure — apply explicit retirement evidence

Source-only slice: this document is the bounded host procedure the install
owner executes AFTER the source gates (tests, review, green CI, squash
merge). No step here is performed from the agent side.

## Why this exists

The silence guard alarms (STALE) whenever the legacy
`fleet-quota-balancer` feed goes quiet. When that producer is explicitly
retired in favor of native routing, endless STALE pages misreport approved
stillness as a live routing outage. The guard now accepts explicit
retirement evidence: with a valid marker file it reports RETIRED (exit 0)
instead of STALE. Without the file — or with a bad one — the stale
verdict stands. Mere inactivity is never healthy.

## Preconditions (all required before touching the host)

1. This change is squash-merged; apply from the immutable merged pin only,
   never from a branch checkout.
2. Native routing is verified healthy through the owner's existing
   dashboards (model-capacity enforcement on, live production signals,
   no active routing incident). A retirement marker must never be applied
   to explain away an unexamined outage.
3. The retirement approval is recorded: approver, approval instant, and a
   receipt reference (decision record or approval id). Put `approved_by`
   and `approved_at` in the marker. The `evidence` field is optional but
   recommended; copy it verbatim when supplied.

## Marker file

Create the JSON file next to the state file (same directory as the log),
readable by both installs (the user timer and the root cron pusher). Only
the authorized retirement operator may create, replace, or delete the marker
or write to its parent directory. The guard validates marker contents only;
`approved_by` is not an approval-authentication check:

```json
{
  "retired": true,
  "producer": "fleet-quota-balancer",
  "successor": "<native routing surface, e.g. model-capacity>",
  "approved_by": "<approver>",
  "approved_at": "<ISO-8601 approval instant, at or before now>",
  "evidence": "<receipt reference>"
}
```

Rules the guard enforces: `retired` must be boolean true; the four string
fields `producer`, `successor`, `approved_by` and `approved_at` must be
non-empty; `producer` must be exactly `fleet-quota-balancer`;
`approved_at` must parse and must not be in the future. `evidence` is
optional but recommended: the guard accepts the marker with or without
it. Anything else is ignored with a journal note and STALE stands.

## Wiring (two installs, same flag)

1. Detection-only user timer: add `--retirement-file <marker path>` to the
   guard arguments via a `systemctl --user edit` drop-in override (do not
   rewrite the shipped unit), then `systemctl --user daemon-reload`.
2. Root cron pusher: set `BALANCER_SILENCE_RETIREMENT=<marker path>` in the
   existing cron environment (same line that already sets the log/state
   paths). No Gatus config change: the pusher maps RETIRED to
   `success=true` with `balancer retired:` text on the existing dedicated
   endpoint.

## Verification (read-only first, then one live tick)

```sh
# 1. Guard verdict by hand (read-only): expect rc=0 and a RETIRED line.
/path/to/pinned/balancer_silence_guard.py \
  --log <real host log path> --state <real host state path> \
  --retirement-file <marker path>; echo "rc=$?"
# 2. Pusher by hand: expect rc=0 and success=true on the dedicated key.
BALANCER_SILENCE_LOG=<real host log path> \
BALANCER_SILENCE_STATE=<real host state path> \
BALANCER_SILENCE_RETIREMENT=<marker path> \
  /path/to/pinned/ops/case_658bf6b9/push-balancer-silence.sh; echo "rc=$?"
# 3. Let one scheduled tick run, then confirm the Gatus incident resolved
#    (send-on-resolved) and the journal shows the `retirement:` context
#    line plus the RETIRED verdict.
```

## Rollback

Remove the flag (unit override and cron variable), or delete the marker
file: the next tick returns to STALE and Gatus re-fires on the next stale
push. No secrets, credentials, models, pins, rosters or production state
are touched by either direction, so nothing needs rotation or restoration.

## Boundaries

Never restart, enable, or reinstall the retired producer. Never change a
model default, pin, roster, pause, or concurrency setting. Never edit the
Gatus endpoint wiring, reuse another monitor's key, or restart
Paperclip/Caddy. Do not disable the guard timer; a one-shot guard service
restart for verification is the only lifecycle action in this procedure.
