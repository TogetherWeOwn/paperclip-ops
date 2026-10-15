# CLIProxy quota-controller release preflight contract

This public document preserves the retained release gate's source contract. It is
not a current installation receipt, deployment approval, or assertion that this
historical controller is suitable for a running service. Do not repoint a live
host, change credentials or modify routing from this packet.

## Required observations

The retained gate requires session-affinity TTL **15m**, **request-retry 3** and
**max-retry-credentials 0**. Measure the target configuration rather than assuming
these values. The checked-in preflight refuses any mismatch, unsupported version,
unreviewed source reference, missing ancestry or inappropriate rollback state.
These are invariants of this release, not a recommendation to overwrite the
configuration of a different release.

`cliproxy_release_preflight.sh --help` lists the required arguments. It accepts
measured values; it does not probe a remote API, authenticate, install software,
restart services or mutate configuration. Exit 0 means only that those supplied
values and local prerequisites satisfy this specific gate. Exit 2 is a refusal.
Do not replace missing measurements with placeholders to obtain a green result.

## Source and rollback requirements

Inspect the reviewed immutable source reference and supported-version list in the
preflight before preparing an artifact. The source must be an ancestor of the
explicitly trusted line for apply. A fresh public history does not contain old
private commit objects: do not import that history or synthesize ancestry to make
the gate pass. Offline tests explain their unavailable historical-ancestry cases
while continuing the refusal and document-mutation controls.

Retain the protected rollback state outside Git. Apply requires the specified
state path to be absent; rollback requires it to be present and usable according
to the gate. Preserve evidence and verify the reviewed rollback mechanism before
any separately authorized installation. Stopping an existing critical timer
requires an owned hold with an expiry. No commands in this document perform an
installation or a rollback.

## Quarantined superseded examples

The following quoted values exist only as regression evidence. They are not live
instructions, and no executable code block belongs inside this fence.

<!-- STALE-EVIDENCE-BEGIN -->
> Superseded example: session-affinity TTL 6h during apply.
> Superseded example: restore affinity TTL 24h during rollback.
> Neither example is an instruction. The live release contract is stated above.
<!-- STALE-EVIDENCE-END -->

Keep the fence balanced, nonnested and free of runnable commands. The preflight
checks instruction wording across wrapped lines, tables and alternate time units
so formatting cannot turn a rejected configuration into an accepted one.

## Verification

Run `test_cliproxy_release_preflight.sh` and the controller's offline suite in a
disposable environment. The tests mutate the source, arguments and this document,
require a passing unmutated control, and require each invalid case to refuse for
the named reason. Synthetic Keeper restoration tests do not touch a live queue or
database. A successful offline run never authorizes production activation.
