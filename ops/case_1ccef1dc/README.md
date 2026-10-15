# Review-stack source kit

This is reusable source, not an installed App inventory, a live reviewer policy,
or approval to change a service. No private keys, account details, operational
review transcripts or measured model results are included.

## App registration template

`review-bot/app-manifest.json` retains metadata/read, pull_requests/write,
checks/read and statuses/read. It has no webhook or default events and does not
request contents, administration, issues, actions or organization permissions.
The `public: false` flag describes the proposed App, not this repository.

`review-bot/registration_url.py` prints the corresponding non-secret setup URL.
This source link is not an instruction to register or install another App:

   https://github.com/organizations/TogetherWeOwn/settings/apps/new?name=togetherweown-review-bot&description=Independent+review+identity+for+TogetherWeOwn+agent-authored+pull+requests.+Submits+exact-head+reviews+only%3A+it+cannot+push%2C+merge%2C+or+change+settings.&url=https%3A%2F%2Fgithub.com%2FTogetherWeOwn&public=false&webhook_active=false&checks=read&metadata=read&pull_requests=write&statuses=read

## Offline evaluation

`reviewer-eval/` builds synthetic mutation variants from public source and tests
scoring, containment and atomic persistence. The public base references and seed
edits are source definitions, not production defects or review findings. Historical
private survival measurements are not bundled: missing `ci_silent` values mean
unknown until a fresh isolated survival run. Private review datasets and results
must stay outside this repository and outside the reviewer sandbox.

`reviewer-eval/observational/analyze.py` reports descriptive statistics from an
explicitly supplied dataset; it makes no network calls and cannot establish model
quality or causal performance differences. Its tests use synthetic input.

Run `bash test_case_c1a76495_review_stack.sh` from the repository root. It retains
manifest, free-tier, mutation, containment, scoring and atomic-write assertions.

## Integration boundary

The Greptile files preserve a historical source contract only; they do not enable
that service, spend money or replace the repository's required review path. Use
the currently authorized review gate for real pull requests. Any separately
approved integration must record its previous settings and exact rollback before
changing them; a source-test pass is not installation acceptance.
