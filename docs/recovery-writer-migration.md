# Recovery-writer migration and rollback source contract

This public document describes ordering and safety constraints, not a migration
request, current timer inventory or permission to restart retired writers.
All source tests use disposable fixtures; private historical comparisons and
board records are intentionally absent.

## Comparison before activation

Prepare a reviewed immutable bundle and verify its manifest. Start beside the
existing system in observe-only mode with a repair cap of zero. Compare fresh
proposals with the intended recovery contract using a separately approved,
bounded evidence window. Inspect refusal reasons, target identity, protected
pins, hold barriers and receipts; do not treat missing input as a healthy cycle.
A dry-run cannot establish that apply-time host-pressure admission works.

Any cutover must have a single authorized writer, a saved previous state and
explicit verification criteria. Stop competing writers only through the normal
owned hold-and-expiry process. Retain receipts and release bundles. Do not delete
state or enable old mutators just to make a rollout appear reversible.

## Rollback

Prefer setting the repair cap to zero (observe-only), keeping evidence intact.
If an independently authorized operator must uninstall the writer, first record
a hold with expiry and the previous unit state. Stop future timer activations,
then stop any active service, then verify it is inactive before optional removal.
The example is source only and must not be run by CI or against production tests.

```bash
systemctl --user disable --now paperclip-recovery-writer.timer
systemctl --user stop paperclip-recovery-writer.service
if systemctl --user is-active --quiet paperclip-recovery-writer.service; then
  printf '%s\n' 'rollback refused: writer service is still active' >&2
  exit 1
fi
# Optional uninstall after stop verification; retain releases and receipts.
sudo rm -f /etc/systemd/user/paperclip-recovery-writer.timer \
  /etc/systemd/user/paperclip-recovery-writer.service
systemctl --user daemon-reload
```

Rollback does not re-enable sweep-auto, pin-clearing writers or any other legacy
mutation path. Such a change requires its own reviewed safety packet. It also
does not erase board history: assess any compensating action from the receipt's
before/after evidence through the supported API. A benign error reset is not
proof that the underlying workload is healthy; preserve subsequent failure
signals and return to observe-only if the evidence does not justify applying.
