# Public review-routing contract

The authoritative decision implementation is `.github/scripts/review_trigger.py`
(authoritative: review_trigger.py). This document describes source behavior, not
private deployment state or permission to invoke a production reviewer.

A head needs a green `ci-ok` before a review request. Risky paths, security labels,
or a diff exceeding `LARGE_DIFF_LINES = 600` select the risk path. Release automation
also has an explicit request path after CI is green.

The audit sample is deterministic: `sha256(head)` modulo 10 equals zero. Re-running
the decision for the same head does not change its sample membership.

Standard heads with a completed checklist also request review once CI is green;
they are not exempt merely because the standalone cost-routing helper labels them
standard. `review_route.sh` and `review_routing_weekly.py` provide routing analysis
and reporting, not a replacement for the authoritative gate or repository rules.

Public CI tests these decisions offline. Reviewer invocation must use the
configured review-gate integration; a source-level routing decision never grants
merge authority or bypasses a required review check.
