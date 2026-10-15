#!/usr/bin/env bash
# Isolated-runner acceptance probe entrypoint (case_4c57f7c0).
#
# Non-destructive, credential-free wrapper. All checks live in
# isolated-probe.py against EFFECTIVE systemctl properties and the live
# MainPID — never unit-file grep. Must run as root on the host.
#
# Required: --service-name <actions.runner.*.service unit under test>,
# --isolated-user, exactly five repeatable --pool-service Docker pool units.
# Defaults: --docker-user gha-runner, --docker-home /home/gha-runner,
# --pg-bin-dir /usr/lib/postgresql/16/bin.
# Refused: --no-native-pg and any skip/dry-run flag (no vacuous passes).
set -Eeuo pipefail

for arg in "$@"; do
  case "$arg" in
    --no-native-pg|--no-native-pg=*)
      printf 'REFUSED: --no-native-pg is not accepted\n' >&2
      exit 2
      ;;
    --skip|--skip=*|--no-check|--no-check=*|--dry-run|--allow-shared-identity)
      printf 'REFUSED: skip flags are not accepted\n' >&2
      exit 2
      ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/isolated-probe.py" "$@"
