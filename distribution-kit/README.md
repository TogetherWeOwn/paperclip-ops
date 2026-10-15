# Distribution kit — reusable consent-first probe Worker

DRAFT. Not deployed, not approved for publication. See the parent
acceptance contract (`distribution-kit-acceptance.md` in the workspace) and
Studio `ops/studio/{CHARTER,OUTREACH,VENUES}.md`.

## Layout

- `worker/src/worker.js` — the Worker (`fetch` + Cloudflare Email Routing
  `email()`; one `createApp`, no second framework).
- `worker/src/email.js` — bounded `email()` handling: exact-recipient gate,
  size gate before parsing, minimal plain-text storage, dedupe, no
  auto-reply/forward/fetch, untrusted-data treatment.
- `worker/src/config.js` — config validation, hostname checks, fail-closed
  collection gate + independent `inboxStatus` (disabled collection vs
  handler readiness are separate flags).
- `worker/src/storage.js` — minimal KV adapter, aggregate views, signup
  deletion + bounded inbox records (`inbox:<probeId>:<dedupeId>`) with
  verified deletion and retention sweeps.
- `worker/probe.config.json` — the ONE per-probe file (seed: disabled demo,
  inbox disabled).
- `worker/wrangler.toml` — manifest draft, NOT DEPLOYED (no binding filled;
  email route documented in comments).
- `fixtures/probe-beta.config.json` — second fixture proving config-only reuse
  (collection open, inbox ready on its own exact address).
- `worker/scripts/read-inbox-fixture.mjs` — narrow exact-key local fixture
  reader (no dump, no HTTP).
- `DOMAIN-SETUP.md` — what a genuinely new domain still needs (DevOps).
- `EMAIL-CONTRACT.md` — foundation integration contract this slice implements.
- `AGENT-INBOX.md` — authorized narrow agent read/deletion path (existing
  authority only; no new credential, no public endpoint).
- `INBOX-RUNBOOK.md` — DevOps isolated controlled-domain check.
- `METRICS.md` — page-view counting rules and uncertainty.

## Run the isolated tests

```bash
cd worker && npm test
```

No network, no credentials, no deployment. Fixture hostnames are
`*.fixtures.example` and never resolve.
