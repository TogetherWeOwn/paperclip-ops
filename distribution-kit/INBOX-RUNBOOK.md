# Inbox integration runbook — isolated controlled-domain check (DevOps)

Scope: enable Cloudflare Email Routing `email()` delivery to the merged
probe Worker on ONE explicitly allowed spare domain, verify receipt,
isolation, read and deletion, then roll back. $0 incremental cost. No
production testing, no togetherweown.com/two.gg/kofra/nntune, no stranger
contact, no cold email. Owner: DevOps; release owner: CEO. Requires
exact-head review + green CI + security review + CEO public-text approval
before any activation (same gates as the foundation slice).

Reference: https://developers.cloudflare.com/email-routing/email-workers/
("Process incoming emails using the `email()` handler in your Cloudflare
Workers") proves the mechanism, not our account's route.

Isolated tests use only `*.fixtures.example` (never resolve, never a real
recipient, no stranger contacted). This runbook's controlled-domain check
uses one explicitly allowed spare subdomain and the same exact-address,
no-catch-all, bounded handling proven by those fixtures.

## Prerequisites (all must hold; otherwise stop)

- Merged revision + exact-head Code Reviewer APPROVE + `ci-ok` green +
  scoped security PASS on this inbox PR (PII/public-input change).
- Public text still DRAFT until independent-family critique + CEO approval.
  Collection/publication gates from the copy adjudication stay closed:
  no auto-replies, no From-only deletion, email-only signup, consent binds
  probe/version/time, disabled mode omits signup markup.
- Target is an explicitly allowed spare domain from Studio VENUES.md
  (e.g. a test subdomain of host-4e3125f1.example.invalid/host-4b1bfee8.example.invalid) that currently has
  NO MX/route, or an explicitly safe test subdomain. Verify unused first.

## 1. Pre-state (record before touching anything)

```bash
dig MX <test-subdomain> +short
dig TXT <test-subdomain> +short
wrangler kv:key list --binding PROBE_KV --prefix "inbox:<probeId>:" --limit 5
wrangler kv:key list --binding PROBE_KV --prefix "signup:<probeId>:" --limit 5
```

Record: MX (expect empty), existing inbox/signup counts (expect 0 or
explicitly safe), current Worker name/route, rollback = unroute + restore
prior MX (usually "remove route; leave DNS as found").

## 2. Bind (staging/preview first, never prod)

- Use the exact merged head (no local edits). Copy
  `distribution-kit/worker/wrangler.toml`, set `name` per probe, bind the
  verified-empty-or-safe KV namespace as `PROBE_KV`.
- In Cloudflare Email Routing for the test subdomain ONLY, add a rule:
  exact address `replies+<probeId>@<test-subdomain>` -> Send to Worker
  `<probe-name>`. No catch-all. Verify the rule shows the exact address.
- Confirm `wrangler.toml` inbox block for this probe: `enabled:true`,
  matching exact address, bounds (`maxRawBytes 131072`, `maxSubjectChars
  200`, `maxTextChars 2000`, `maxRecords 1000`, `retentionDays 90`).

Cost evidence: Worker + KV on the free tier; screenshot/CLI output showing
no plan change, $0 delta.

## 3. Isolated check (controlled sender only)

From ONE controlled sender you own, send ≤3 small plain-text messages to
the exact test address only. Never to a stranger, never to a list.

- `T1` valid: plain text, unique Message-ID, <10 KB → expect stored
  (`inbox:<probeId>:<dedupeId>` appears, `subject`/`text` bounded).
- `T2` retry: resend T1 byte-identical → expect `duplicate:true`, still one
  record (idempotent).
- `T3` wrong recipient: same sender to `wrong@<test-subdomain>` → expect
  rejected (`unknown-recipient`), zero new keys.

Verify each via exact-key KV reads (see AGENT-INBOX.md), never via inbox
login. Confirm: no auto-reply received, no forward, no attachment stored,
no raw log in Worker logs (only status codes/counts).

## 4. Deletion + retention proof

- Verified deletion: through the authenticated store path only
  (`wrangler kv:key delete --binding PROBE_KV "inbox:<probeId>:<dedupeId>"`);
  confirm `hasInbox` false afterwards. A spoofed-From "delete me" email
  must NOT delete (covered by isolated tests; do not re-prove with a
  spoof in staging).
- Retention: `sweepExpiredInbox` is unit-tested (90-day default); do not
  age staging data. Record the configured `retentionDays`.

## 5. Roll back and record

- Remove the Email Routing rule; verify `dig MX` matches pre-state.
- Optionally delete the ≤3 test keys via the verified delete path; record
  key IDs (hashes only, never bodies) and the exact commands + outputs.
- Register on the executor card: exact domain/route, KV namespace ID
  (redacted to last 4 in public text), pre/post MX, test message IDs
  (hashes), KV key counts, $0 evidence, rollback confirmation.

## Do not

- Do not enable a catch-all, do not reuse a production MX, do not
  overwrite an existing route blindly.
- Do not send to togetherweown.com/two.gg/kofra/nntune or any stranger.
- Do not enable collection/signup based on this inbox check; signup gates
  (controller/contact/country/notice/approval) are separate.
- Do not paste bodies, addresses or secrets into cards, PRs or logs.
