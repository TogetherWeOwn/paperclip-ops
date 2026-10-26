# Served-models carrier install notes

Source-only handoff for `ops/cliproxy-live/live_accounts.py`; this issue does **not** authorize installation. The carrier is pinned to:

- SHA-256: `93a99797b20726182f14938b394f58c4ec6b3adcc87fdfc7bcce787e465bf75e`
- Size: 42,536 bytes
- The verified source contains `import urllib.request` and initializes `KEY = load_key()`; the patch uses those existing host symbols and adds no credential handling outside the already-reviewed optional bearer override.

Oracle auth prerequisite: the patched code reads CLIProxy `GET /v1/models` with `Authorization: Bearer` using the host's existing `KEY` (loaded by `load_key()`), over loopback. No new credential is created. Optional environment overrides (read once at import; unset means the defaults):

- `CLIPROXY_SERVED_MODELS_URL` — oracle URL; default `http://127.0.0.1:8317/v1/models`.
- `CLIPROXY_SERVED_MODELS_KEY_FILE` — path to a key file whose first usable line (`KEY=value` or bare) replaces `KEY` as the bearer; unset/empty keeps `KEY`. A configured file with no usable line omits `servedModels` (fail-open) rather than failing the feed.

## Operator procedure (only when separately authorized)

1. On the host, enter the directory containing `live_accounts.py` and verify the exact preimage before touching it. Every check below aborts the shell on failure (`set -eu`); run the whole block, not line by line:

   ```sh
   set -eu
   test "$(wc -c < live_accounts.py)" -eq 42536
   echo '93a99797b20726182f14938b394f58c4ec6b3adcc87fdfc7bcce787e465bf75e  live_accounts.py' | sha256sum -c -
   if grep -q 'served_bearer' live_accounts.py; then
     printf 'servedModels carrier already applied; stop.\n' >&2
     exit 1
   fi
   ```

   Stop without applying if any check fails. Do not try the old carrier against a different source. The `served_bearer` marker check makes a second application fail closed even if the operator restarts the block from the top.

2. Dry-run the patch, then make a same-filesystem backup and apply it. Record the printed backup path for rollback. The dry-run and apply exit statuses are checked, and any failure restores the backup; commands are noninteractive and will not reverse an applied patch:

   ```sh
   set -eu
   if ! patch --batch --forward --dry-run -p0 < /path/to/host_patch.diff; then
     printf 'Patch dry-run failed; stop without applying.\n' >&2
     exit 1
   fi
   backup="live_accounts.py.pre-servedmodels-$(date -u +%Y%m%dT%H%M%SZ)"
   cp -a live_accounts.py "$backup"
   printf 'Rollback source: %s\n' "$backup"
   if ! patch --batch --forward -p0 < /path/to/host_patch.diff; then
     printf 'Patch apply failed; restoring the backup.\n' >&2
     cp -a "$backup" live_accounts.py
     exit 1
   fi
   if test -e live_accounts.py.rej; then
     printf 'Patch created live_accounts.py.rej; restoring the backup.\n' >&2
     cp -a "$backup" live_accounts.py
     exit 1
   fi
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
