# Historical zero-spend configuration contract

This source-only kit documents the controls expected by the offline suite. It
is not a live plan/pricing assertion, installed configuration, spending approval
or instruction to restore a retired review service.

1. **No payment method** on file; do not add one to run these source tests.
2. **Flex Usage Limit = $0**; verify the provider still enforces zero overage
   before any separately authorized integration. A repository config alone is
   not a billing control.
3. Base effort only, automatic reviews disabled, auto-approval disabled, and
   status/description updates disabled. The example includes one review author.
4. **Only select repositories**: a separately approved installation must be
   explicitly scoped, never all current or future repositories.

`two-web-next.greptile-config.json` is a credential-free example. The test checks
its actual fields and rejects private task identifiers. It does not contact the
provider or alter account settings. Recheck current provider behavior rather
than treating historical plan descriptions as guarantees. Retain the existing
required review path; no review bypass is authorized by this file.
