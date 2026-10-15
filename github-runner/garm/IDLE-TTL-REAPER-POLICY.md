# GARM idle-VM reaper TTL policy (source-only contract)

This public contract describes offline inputs and expected audit behavior. It is
not evidence of a deployed reaper and grants no authority to delete a VM, change
a pool, or execute against a host. Deployment identifiers and live inventory are
intentionally absent.

`idle-ttl-contracts.json` records the proposed constants;
`validate_idle_ttl.py` checks them offline; `../test_isolated_idle_ttl.py`
pins their meaning, including negative controls and load-bearing mutations.

## Thresholds and protections

| Pool class | TTL | Orphan threshold | Disposition |
| --- | --- | --- | --- |
| Ephemeral default | 240 minutes | 480 minutes | Propose only |
| Canary | 240 minutes | 480 minutes | observe-only; never a delete candidate |
| Static | None | None | out-of-scope |

Provisioning-stuck is 30 minutes and drain-stuck is 60 minutes. The separate
210-minute drain-grace floor bounds graceful draining, not the idle TTL.
The newest `min_idle_runners` idle instances in each pool remain warm spares,
regardless of age. Queued work never authorizes raising `max_runners`.

## Proposal order

Candidates are grouped as `idle-excess`, `stale`, then `busy`. Each bucket is
oldest-first by `created_at`. A busy candidate requires independent live-job
correlation before any operational decision. Orphan, stuck, unknown-age and
above-cap observations are breaches, not permission to delete.

Canary exclusion remains an operator responsibility where an audit implementation
has no role filter. A structural `never_candidate` contract does not prove that a
running implementation enforces it. Static runners are not GARM pool instances.

## Report and verification

A JSON audit report carries `tool`, `now`, `ttlMin`, `orphanMin`, `verdict`,
`candidates`, `pools`, `errors`, and `breachReasons`. Candidate records carry
`pool`, `name`, `status`, `runnerStatus`, `ageMin`, `bucket`, and `createdAt`.
Verdicts are `OK`, `PROPOSE`, or `BREACH`. No verdict is execution authorization.

```sh
python3 -B github-runner/garm/validate_idle_ttl.py github-runner/garm/idle-ttl-contracts.json
python3 -B -m unittest discover -s github-runner -p 'test_isolated_idle_ttl.py' -v
```

Successful validation must retain `admission_authorized:false`,
`host_verified:false`, and `installed:false`. A deployment needs its own private
configuration, authorization, job correlation, and two-sided verification.
Reverting this source-only contract changes no installed runtime state.
