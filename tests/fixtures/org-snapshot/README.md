# Org-snapshot fixture (case_1695ae38)

Offline fixture that lets `org_provisioner.sh` serve reads without a live
database, so the queue suite's verdicts can run anywhere.

## Layout

`ORG_SNAPSHOT` for the **provisioner** is this **directory** — or,
interchangeably, the `org.tsv` file itself (the queue's seam shape, with
`grants.tsv` resolving beside it), so a queue invoking the provisioner with
its own file-valued `ORG_SNAPSHOT` reads the same rows:

- `org.tsv` — `id \t orgRoleId \t template \t status \t reportsTo \t title`,
  the same six columns `org_request_queue.sh` reads when its own
  `ORG_SNAPSHOT` points at a file. Roles mirror
  `test/fixtures/orgdb/org.sql` (same UUIDs, same titles) plus a
  `DIR -> MGR -> ENG` chain and one `terminated` row, so the fixture covers
  approve, reject, and ceiling-skip cases. Every line carries exactly six
  tab-separated fields; titles contain no tabs.
- `grants.tsv` — `agent_id \t permission_key \t subtreeRootAgentId-or-empty`
  (optional; absent means no agent holds grants). A missing or empty third
  field is a company-wide (`NULL` scope) grant; a subtree id equal to the
  agent's own id renders as `SELF-subtree`, exactly like the live
  `read_effective_access` query. Grants mirror the template catalog plus the
  case_effd74c7 assignment baseline (`tasks:assign`, company-wide).

## What the snapshot serves, and what it refuses

Reads served from the fixture: `lookup_agent`, `is_descendant_of`,
`read_effective_access` (`access`), and `tree`. Policy refusals that only
need those reads (unknown caller, delegation ceiling, caller-supplied
`reportsTo`, descendant-only deactivation, kill switch) are emitted by the
same code as live, so they read byte-identical.

Writes refuse before their first write with a `read-only` error: `create`
(stops after the policy checks, before transport, the API, or any grant
write), `deactivate` (stops in `apply_exact_grants`), and
`verify-transport`. Operator resolution, the secret/binding checks, and the
heartbeat freshness read stay live-only with the writes they belong to: the
fixture carries no users, secrets, bindings, or runs, and inventing them
would be a verdict about nothing.

Without `ORG_SNAPSHOT` set, behavior is unchanged: live path only, no new
defaults, no new writes. Pure commands (`ceiling`, `templates`,
`template-keys`) never consult the variable.

## Provenance

- `org.tsv` roles/UUIDs/titles derive from `test/fixtures/orgdb/org.sql`;
  `grants.tsv` derives from the `B2_TECH_CHIEF` seed grants there plus the
  template catalog in `org_provisioner.sh` and the case_effd74c7 baseline.
- Expected outputs in `test_org_snapshot.sh` are pinned golden strings. The
  refusal verdicts are emitted by code shared with the live path, so they
  are byte-identical by construction; the `access` rows reproduce the live
  query's columns, order, and scope rendering for the same data.
- The heavy-tier live suites (`test_privilege_ceilings.sh`,
  `test_request_queue.sh` in `privilege-suites`) remain authoritative for
  live-database behavior, and the VPS run remains necessary for the real
  Paperclip API contract. This fixture removes no live coverage.
