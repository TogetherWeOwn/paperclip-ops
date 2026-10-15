# case_d5cbfb18 — install the host-side MCP transport — MOVED TO GIT

**This file is a signpost, not the runbook.** (case_5bda0b5a, 2026-08-25)

The runbook now lives in `paperclip-ops`, on `main`, at:

```
mcp/deploy/install-runbook.md
```

Read it there. Before you follow it:

```sh
git -C <checkout> fetch origin
git -C <checkout> diff --exit-code origin/main -- mcp/deploy/install-runbook.md   # expect: no output
```

## Why it moved

`/paperclip/operator-handoff/` is mode `1777` and every file in it is owned by the
shared agent uid `node`. Until this move, this runbook existed **only** here — so
any agent on this box could rewrite the steps you were about to run as root, and
there was nothing to diff it against. It had already been silently wrong once (the
`/etc/caddy/org-request-mcp.bearer` row in step 2, corrected under case_0409633c), and
its step 9 carried an assertion that could not catch the bug it targeted (found
under case_961e6ec2).

That is the same gap case_965c83b3 closed for staged scripts, and the fix is the same
one: **git is the source, this channel is a convenience.** A document that tells
you which files to trust needs a tamper-evident home at least as much as the files
do.

## This file is checked

`channel_drift.sh` lists it as a required mirror of
`mcp/deploy/install-runbook-handoff-pointer.md`. Run, from a checkout, on any box
where `/paperclip` is mounted:

```sh
./channel_drift.sh check --ref origin/main
```

If someone has replaced this pointer — with anything, including a plausible copy
of the runbook — that reports it under **NOT THE COMMITTED COPY** and exits `3`.
A full runbook sitting at this path is therefore a finding, not a convenience:
the only correct content here is this pointer.

**Do not restore a runbook copy to this path.** Send a PR against
`mcp/deploy/install-runbook.md` instead.
