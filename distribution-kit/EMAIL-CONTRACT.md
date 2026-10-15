# Integration contract for the email (inbox) module — sibling slice

Owner: Founding Engineer (separate next PR). This Worker keeps extension
points small so that slice can land without rework.

## What this Worker provides

- **Config:** `probeId`, `hostname`, `allowedOrigins`, `privacy.noticeVersion`.
  The inbox module routes by exact configured address per probe; no catch-all.
- **Storage:** signup records at `signup:<probeId>:<normalizedEmail>` holding
  `{ email, probeId, consentAt, noticeVersion, origin, probeHostname }`.
  Consent binds probe ID, notice version and time. No free text, no IP/UA/referrer,
  no cookies. Retention: at most `limits.retentionDays` (90); `sweepExpired()`
  deletes older records.
- **Private inbox access:** there is NO public admin/export endpoint in this
  Worker, by design. Agents read the inbox through already-authorized
  control-plane tooling (KV read on the bound namespace), never through HTTP.
- **Deletion semantics:** `deleteSignup(probeId, email)` removes one record
  and returns whether it existed. Withdrawal of consent deletes the address.
  The HTTP signup endpoint never reveals whether an address is listed.
  Deletion requests must be verified through an authenticated channel —
  never on the spoofable email From header alone. The store-level
  `deleteSignup()` is the only deletion path and is not exposed over HTTP.
- **Success acknowledgement:** the signup endpoint replies 200 only after the
  record is persisted, with a message identical for first-time and duplicate
  submissions (no address enumeration).

## What the sibling slice must provide (and this one does not)

- The `email()` handler for inbound replies, bounded MIME/message handling,
  per-probe records, deduplication, bounded retention.
- No automated outbound mail and no auto-replies/forwards from this Worker.
  Per the copy-review adjudication on the parent, minimal bounded reply
  text is allowed only inside the expressly requested inbox slice — never
  here, and never sender-only deletion on a spoofable From header.
- Agent-readable fixture access and verified deletion-request handling
  through an authenticated channel.
- Its own tests, review and security review for the PII it touches.

## Constraints both slices share

- $0 new spend; no new paid service; no new app credential — prefer
  existing-authority Cloudflare control-plane access for agents.
- Untrusted email content never becomes agent instructions or tool requests.
- No raw body/email logs. No outbound auto-replies or forwards.
