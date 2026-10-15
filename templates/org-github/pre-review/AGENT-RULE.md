# Agent rule: self-review before ready (one line for every bundle)

> Before marking a PR ready: run `pre-review.sh` (fix every FAIL, justify
> every WARN) plus the 10-bucket rubric self-check, fix everything found,
> and re-run both on each fix delta. Never mark ready with a standing FAIL.

Attest in the PR Verification section:

```
Pre-review: <score>/5 at <short head sha>; deterministic: <0 FAIL, W WARN justified>; covered: <areas>; not covered: <list or none>
```

Keep bundles slim: this rule stays one line and points at the template —
the script and rubric live there, not in the bundle.
