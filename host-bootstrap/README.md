# host-bootstrap — the operator's company bootstrap script

The one script in this company's history that created the company. It lived
only at `~/paperclip-enterprise-company/` on the operator's host — outside every
container mount, unreadable from any agent, and never reviewed. case_f13d7088.

`ROLLBACK.md:62` has told operators to run
`./bootstrap_paperclip_enterprise_company.local.sh` since 2026-08-22. Until this
directory existed, that instruction pointed at a file this repo did not contain,
so "re-run the bootstrap to resume a partial failure" was an instruction nobody
could review, diff, or roll back. That is the same class of gap
`channel_drift.sh` exists to close for every other operator-run script here.

## What is in here

| File | What it is |
|---|---|
| `bootstrap_paperclip_enterprise_company.local.sh` | The operator's script. Byte-identical to the host copy as handed over on 2026-09-05, **plus the case_ad767aaf guard** — see below. |
| `paperclipai_enterprise_org_operating_model.md` | The companion document the script stages into the server container, cited by every agent's metadata and AGENTS.md. case_ad767aaf. |

## Provenance — how this copy was established

Handed over by the operator on case_f13d7088 (2026-09-05 14:58Z), who copied the host
file to `/paperclip/operator-handoff/case_f13d7088-bootstrap_paperclip_enterprise_company.local.sh`.

    host path   ~/paperclip-enterprise-company/bootstrap_paperclip_enterprise_company.local.sh
    size        30936 bytes
    sha256      e7d255a2d7a27cf9e0802db6b2b1bd3324f5924ab4d3382547751c68f286a19a

That sha256 was re-derived in-container against the drop before committing, and
the committed blob was `cmp`-verified against the drop. The operator's stated
hash, the drop on disk, and the blob in this directory are one artifact.

## Why it is NOT redacted

The issue anticipated redacting embedded secret values. There are none to
redact, verified independently of the operator's own scan: no literal
credential, key, token or password appears in the file. The board bearer token
is read at RUNTIME from `~/.paperclip/auth.json` (line 284) and deliberately
kept out of `argv` — `[DELTA 3]` at line 106 and the `curl -K -` config-on-stdin
construction at line 279 exist for exactly that reason. Database credentials
stay inside the `paperclip-db` container and are never read by the script.

Redacting would have been actively harmful here. **The value of this copy is
that it is byte-identical**: `channel_drift.sh` matches a handoff drop to a
committed blob by CONTENT, so a redacted copy would report the operator's real
script as UNVERSIONED forever — the detector would be measuring the redaction,
not the drift. A copy that cannot be compared to the running file is decoration.

## The companion document — CLOSED (case_ad767aaf)

The script resolves a sibling file next to itself:

    REPORT_SRC=".../paperclipai_enterprise_org_operating_model.md"

It is staged into the server container and its container path is written into
every agent's `metadata.operatingModelReport` and instruction bundle, so it is
the cited canonical source for this company's org and permission design. It was
not part of the case_f13d7088 handover. It is now committed here.

    host path   ~/paperclip-enterprise-company/paperclipai_enterprise_org_operating_model.md
    drop        /paperclip/operator-handoff/case_ad767aaf-paperclipai_enterprise_org_operating_model.md
    size        112346 bytes
    sha256      d6248d5b31ad642a84f29b3325ddc3b0d425e9ef41a92640533cf8c217735b34

Verified two ways before committing, not one: the drop was `cmp`-identical to
the copy **already staged in this company's container** at
`/paperclip/instances/default/companies/<id>/ops/`, which is the exact path the
running agents' instructions cite. So this blob is not merely what the operator
says they have — it is the document the live fleet was pointed at. Secret-scanned
clean (no key, token, password or private key material; the only long opaque
strings in it are URL path fragments).

### The silent-success bug is fixed, not just documented

The script tolerated the file's absence via `if [[ -f "$REPORT_SRC" ]]`, so a
bootstrap on a host lacking it **succeeded**, printed nothing, and produced a
complete company whose every agent cited a document that was never staged.

`[DELTA 9]` now refuses in **preflight** — before the backup and before any
mutation, and on the `DRY_RUN=1` path, so the gap is reported for free and a
host missing the file is turned away rather than left holding a half-built
company. The staging step no longer has an `-f` test at all; instead it verifies
after `podman cp` that the file is readable **inside the container** at the size
it expects, because an unverified copy is the same silent failure one layer down.

`test_bootstrap_operating_model.sh` guards this, and runs in CI. It executes the
guard's own bytes, extracted from the script by marker, so it cannot drift into
testing a paraphrase; if the markers move it exits 2 (REFUSED) rather than
passing over zero lines. Six mutations were confirmed to redden the specific
case that claims to cover each.

**Caveat on that green:** CI cannot run the bootstrap — no podman, no
containers, no host. Green means the refusal is present and works, never that a
bootstrap would succeed.

## Divergence from the host copy — action required

This script is **no longer byte-identical to the host copy**. That is the rule
below working as intended, not a defect: the guard was added here first.

    host copy (as handed over)  30936 bytes  sha256 e7d255a2…86a19a
    this blob (with DELTA 9)    33956 bytes  sha256 398511a6…ee748c

Until the operator re-copies both files to the host, `channel_drift.sh` reports
`case_f13d7088-bootstrap_…local.sh` as **STALE**, and the host's script still has the
silent-success bug. The operator must copy this script **and** the operating
model document to `~/paperclip-enterprise-company/`, then re-drop the script to
`/paperclip/operator-handoff/`.

Note that `channel_drift.sh` cannot see the document itself: it is a `.md` with
no shebang and no exec bit, so it is not runnable by any of that script's three
tests. The same hole case_5bda0b5a found. It is covered here by
`test_bootstrap_operating_model.sh` asserting the committed copy is present and
substantive, not by the channel detector.

## Rules for changing this file

It is a MIRROR, and mirrors only work in one direction. **The host is not the
source of truth — this is.**

- Change it here, in a reviewed PR, then have the operator re-copy it to the
  host. Never edit the host copy and backfill.
- After any change, the operator must re-drop it to
  `/paperclip/operator-handoff/` or the drop goes STALE against this blob.
- Verify with `./channel_drift.sh check` from a clone; a drop matching this
  blob reports MIRRORED.

## Running it

Not from a container. It needs `podman`, the host's `~/.paperclip/auth.json`,
and both the `paperclip` and `paperclip-db` containers. It is idempotent —
existing company, goal and agents are matched and reused (agents on
`metadata.orgRoleId`) and grant sets are re-applied with replacement semantics.

`DRY_RUN=1` runs preflight and the plan with no mutation. Use it first; it is
the only free way to check the CLI/server version gate and permission-key
preflight. Rollback and resume procedures: `ROLLBACK.md`.
