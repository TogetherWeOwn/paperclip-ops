# Setting up infrastructure for a genuinely new probe domain

Swapping `probe.config.json` re-renders the page for a different probe. It
does NOT provision anything. A genuinely new domain needs these separate
steps (DevOps owns them; this slice performs none of them):

1. Pick a spare domain from Studio `VENUES.md`. Never `togetherweown.com`,
   `two.gg`, `kofra.app`/`.io` or `nntune.com`. The Worker itself refuses
   those hosts, but DNS must never point them here either.
2. In Cloudflare, add the hostname and route it to this Worker.
3. Create (or reuse) one KV namespace; bind it as `PROBE_KV`. Verify the
   namespace is empty or explicitly safe before binding — never attach a
   namespace holding another probe's or product's data blindly.
4. Copy `worker/probe.config.json` to the new probe's values: `probeId`,
   `hostname`, `allowedOrigins`, content, changelog seed entry, privacy
   fields. Keep `collectionEnabled: false` until the privacy gate passes.
5. Pre-state record: DNS/MX before state, rollback (unroute + restore),
   and $0 incremental-cost evidence. Do not overwrite existing MX records.
6. Staging acceptance first on an isolated preview host; spare-domain
   activation only after exact-head review, green CI, security review and
   CEO approval of the public text.

Cost: Worker + KV on the free tier; no paid service or plan is used.
