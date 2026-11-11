# Policy-only refusal scaffold

This is **not a diagnostic harness**. Both Paperclip and CLIProxyAPI are unsupported. No package is materialized, no product is started, and no scenario is run. The manually dispatched `Policy boundary fixture (unavailable)` workflow intentionally exits with an error; it does not check out code or execute a fixture. The separate pull-request job tests the refusal contract. Neither establishes isolation or readiness.

`python3 -I -B scripts/policy-refusal/refusal.py` prints JSON with `qualification=POLICY_ONLY`, `stock_reference=UNESTABLISHED`, `upstream_defect_eligible=false`, `scenario_observation=NOT_RUN`, and `verdict=ERROR`, then exits 1. Supplied arguments are ignored. This is a refusal, not an upstream defect claim. Existing transcripts must not be used as evidence.

## Missing capabilities

| Capability | Status |
| --- | --- |
| Reviewed immutable controller and guest identities, provenance and configuration | Not established. |
| Credential, process, user, network and runner isolation | Not established on Actions; runner labels are not a security boundary. |
| Independent denial witnesses for host, DNS, IPv4 and IPv6 paths | Not established; UDP receiver silence cannot prove denial. |
| Native policy-constrained materialization and fixed-authority source admission | Not implemented or verified. |
| Writer-stop and sealed graph/content-identical handoff | Not implemented or verified. |
| Supported product and isolated scenario | None; observation is `NOT_RUN`. |

A future successful policy-only sample requires the missing controls, negative controls, exact-head Actions evidence and independent security and QA review. It would still not establish unmodified-stock equivalence or authorize an upstream defect claim.
