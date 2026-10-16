# Served-models carrier install notes

Source-only handoff for `ops/cliproxy-live/live_accounts.py`; this issue does **not** authorize installation. The carrier is pinned to:

- SHA-256: `93a99797b20726182f14938b394f58c4ec6b3adcc87fdfc7bcce787e465bf75e`
- Size: 42,536 bytes
- The verified source contains `import urllib.request` and initializes `KEY = load_key()`; the patch uses those existing host symbols and adds no credential handling outside the already-reviewed optional bearer override.

## Operator procedure (only when separately authorized)

1. On the host, enter the directory containing `live_accounts.py` and verify the exact preimage before touching it:

   ```sh
   test "$(wc -c < live_accounts.py)" -eq 42536
   echo '93a99797b20726182f14938b394f58c4ec6b3adcc87fdfc7bcce787e465bf75e  live_accounts.py' | sha256sum -c -
   ```

   Stop without applying if either check fails. Do not try the old carrier against a different source.

2. Dry-run the patch, then make a same-filesystem backup and apply it. Record the printed backup path for rollback. The SHA precondition makes a second application invalid; commands are noninteractive and will not reverse an applied patch:

   ```sh
   patch --batch --forward --dry-run -p0 < /path/to/host_patch.diff
   backup="live_accounts.py.pre-servedmodels-$(date -u +%Y%m%dT%H%M%SZ)"
   cp -a live_accounts.py "$backup"
   printf 'Rollback source: %s\n' "$backup"
   patch --batch --forward -p0 < /path/to/host_patch.diff
   ```

3. Verify syntax before restarting anything:

   ```sh
   python3 -m py_compile live_accounts.py
   ```

   If the syntax check fails, restore the backup and stop. Do not alter credentials, routing, exclusions, or existing host configuration.

4. Restart **only the existing read-only `live_accounts.py` feed-reader service** using its already-established service unit and runbook procedure. Do not restart/reconfigure CLIProxy, call reset/write endpoints, or restart unrelated services. If the exact reader unit cannot be positively identified, stop and request the Operator/host owner to identify it; do not guess a unit name. Verify only that this reader service is active. Whether `servedModels` actually appears is intentionally **unverified** here: checking it would exercise the credentialed oracle/network path prohibited by this source-only task and requires its own authorized host verification.

## Rollback

Restore the saved file and restart only that same reader service:

```sh
cp -a "live_accounts.py.pre-servedmodels-<recorded-timestamp>" live_accounts.py
python3 -m py_compile live_accounts.py
```

If restoring or syntax validation fails, leave the service stopped if that is the established safe procedure and escalate to the host Operator. The patch changes only `servedModels` assembly; plan-aware 0.2.46 behavior, `pace_guard`, `detect_free_resets`, `freeResets`, exclusions, and all other feed fields remain unchanged.

## Local source verification

From this repository:

```sh
python3 -B -m unittest discover -s ops/case_0b2823ef -p 'test_feed_contract.py' -v
```

The checked-in tests use mocked oracle responses and do not read host files or make network requests. The original source copy must be hash-verified before applying the patch; no host installation is included in this task.
