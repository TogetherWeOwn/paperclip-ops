# paperclip-ops

Operator tools, offline tests, and automation components for Paperclip deployments.
Licensed under MIT, matching the companion
[paperclip-operator-toolkit](https://github.com/TogetherWeOwn/paperclip-operator-toolkit).

## Fresh-history publication candidate

This tree is a sanitized source snapshot, not a clone of the previous operational
repository. Its initial commit has no parents. Historical commits, deployment
backups, live configuration, operational evidence, private runbooks, member data,
and credential references are intentionally excluded. Never import old Git
objects, bundles, branches, tags, or PR commits into this repository.

Deployment-specific identifiers have been replaced with reserved example
addresses and synthetic identifiers. They are not working production defaults.
Keep real configuration and credentials in an external private store; inject them
at installation/runtime. **Do not deploy this snapshot over a running host.**
Source-path mappings and private overlays require independent integration review
before a host can switch to a release from this repository.

Public-facing workflows use standard GitHub-hosted runners. Private infrastructure
canary, telemetry, and live review-trigger workflows are not included. The
retained integration workflows still need an exact-head compatibility pass after
the private-data extraction; a passing secret scan is not a claim that every
legacy suite passes or that this is a production-ready release.

## Safety checks

`.gitleaks.toml` extends the scanner's default rules. Its exceptions require both
an exact file path and the reviewed literal or test line, scoped to named rules.
They cover synthetic test
credentials and file-integrity hashes, not directory-wide exclusions. Exceptions
for removed historical fixtures do not authorize restoring those private files.

Run the secret and privacy checks before adding a new commit. The public-safety
workflow checks both the working tree and all fetched Git history. Do not disable
it to import a patch. Changes derived from an old private PR must be reviewed as
plain diffs and committed freshly; keep author/card custody on the private board,
not in public branch names, commit messages, or PR text.

The optional toolkit restoration script uses pinned public archives and checksums.
Its legacy compatibility manifest is not a release-readiness certificate: verify
restored paths and their contents after sanitization before using it for a build.
Never add a private artifact to make an old fixture pass.

## Contributing

Use a feature branch and a reviewed pull request. Report actual tests and their
results, preserve the attribution of original contributors, and keep hostnames,
private network addresses, member details, secrets, and internal task references
out of public text and files. Production configuration is outside this tree.
