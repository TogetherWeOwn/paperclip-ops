# Recovery writer source and deployment contract

This is reusable source documentation, not an installed-service inventory or
permission to deploy. Private board identities, credentials, incident records
and historical rollout evidence are excluded. CI uses isolated fixtures only.

## Safety contract

`recovery_writer.py` centralizes the supported recovery mutations. Dry-run is
the default; only an explicit `--apply` argument permits writes. An observe-only
cycle uses `--max-repairs 0`. Zero proposals is not proof of a healthy board.
The writer re-reads live targets before applying a proposal, preserves protected
model/environment pins and refuses user assignments. Owner-bound proposals use
the explicitly configured executive-agent route, not a guessed identity.

The allowlist, host-pressure admission checks and receipt schema in the source
are authoritative. Host-admission failure refuses the write rather than changing
running work or quota policy. A cap-zero test does not prove apply-time admission.
The benign agent-error reset requires the exact known-benign terminal-run evidence;
other error states remain diagnostic. Do not broaden the allowlist to make an
old proposal apply, clear pins, reopen terminal tasks or resume paused agents.

Keep proposals and before/after receipts in protected runtime storage. Do not
commit live JSONL, tokens, API responses or board inventories. A fixture example
is `python3 recovery_writer.py --proposals proposals.jsonl --max-repairs 0`;
no production API or database is needed for the unit and mutation suites.

## Installation after independent review

A separately authorized operator must select the independently reviewed exact
commit, inspect the bundle manifest and verify its bytes before activation.
Do not install a timer merely because this source passed CI. No command below
was executed against a live host as part of public-source restoration.

```bash
reviewed_commit='<reviewed full commit SHA>'
bundle="$HOME/paperclip-recovery-writer-$reviewed_commit.tar"
./systemd/build-recovery-writer-bundle.sh --source-ref "$reviewed_commit" --output "$bundle"
tar -xOf "$bundle" REVISION
tar -xOf "$bundle" SHA256SUMS
install -d -m 0700 "$HOME/.config/paperclip"
touch "$HOME/.config/paperclip/recovery-writer.env"
chmod 0600 "$HOME/.config/paperclip/recovery-writer.env"
# Populate reviewed private settings out of band; never put credentials on argv.
# PAPERCLIP_RECOVERY_WRITER_MAX_REPAIRS=0 for the first hand-run cycle.
sudo ./systemd/install-recovery-writer.sh --source-ref "$reviewed_commit" --bundle "$bundle" --target-user "$USER"
systemctl --user daemon-reload
systemctl --user start paperclip-recovery-writer.service
systemctl --user status paperclip-recovery-writer.service --no-pager
journalctl --user -u paperclip-recovery-writer.service -n 100 --no-pager
# Inspect the cap-zero receipts and complete the separately approved comparison.
# Do not raise the repair cap or enable recurrence without that evidence.
systemctl --user enable --now paperclip-recovery-writer.timer
```

The operator owns account persistence, credential scope, detector configuration,
reviewed identities and a bounded verification window. Timer activation must
follow the non-writing hand-run, never precede it. Verify the installed REVISION,
SHA256SUMS and unit ExecStart/ExecStartPre before declaring deployment complete.
The current source bounds service runtime and recurrence; source delivery alone
is not evidence that any host has those settings.

See [migration and rollback](recovery-writer-migration.md) for ordering and the
non-revival boundary. Existing critical timers require an owned hold with an
expiry before any stop; tests never stop a live timer.
