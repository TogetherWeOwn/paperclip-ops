#!/usr/bin/env bash
# Mutation controls for the public hosted-runner isolation contract.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 - "$ROOT" <<'PY'
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
gate = root / "verification/case_221a4156-selfhosted-runner-gate.sh"
text = (root / ".github/workflows/ci.yml").read_text()

def verdict(path):
    return subprocess.run(["bash", str(gate), str(path)], capture_output=True, text=True)

baseline = verdict(root / ".github/workflows/ci.yml")
if baseline.returncode:
    raise SystemExit("baseline failed:\n" + baseline.stdout + baseline.stderr)
print("ok - baseline")
mutations = [
    ("no-identity-proof", '[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]', "true # proof removed"),
    ("dirty-checkout", "clean: true", "clean: false"),
    ("renamed-check", "name: suites", "name: suites tests"),
    ("cancel-main", "cancel-in-progress: ${{ github.event_name == 'pull_request' }}", "cancel-in-progress: true"),
    ("service-container", "    timeout-minutes: 20", "    services:\n      db:\n        image: postgres:16\n    timeout-minutes: 20"),
    ("host-socket", "    timeout-minutes: 20", "    env:\n      DOCKER_HOST: unix:///var/run/docker.sock\n    timeout-minutes: 20"),
    ("missing-merge-group", "  merge_group:", "  ignored_merge_group:"),
    ("missing-job", "  privilege-suites:", "  removed-privilege:"),
    ("larger-paid-runner", "runs-on: ubuntu-latest", "runs-on: ubuntu-latest-16-cores"),
    ("configurable-route", "runs-on: ubuntu-latest", "runs-on: ${{ vars.RUNNER }}"),
]
# Mutate each job independently: changing only the first route cannot prove
# that a later job is protected too.
jobs = text.split("\njobs:\n", 1)[1]
for match in re.finditer(r"(?ms)^  ([\w-]+):\n.*?^    runs-on: ubuntu-latest", jobs):
    old = match.group(0)
    mutations.append(("private-route-" + match.group(1), old,
                      old.rsplit("runs-on:", 1)[0] + "runs-on: [self-hosted, private]"))
with tempfile.TemporaryDirectory() as tmp:
    for name, old, new in mutations:
        if old not in text:
            raise SystemExit(f"mutation anchor absent: {name}")
        mutant = text.replace(old, new, 1)
        assert mutant != text
        path = Path(tmp) / (name + ".yml")
        path.write_text(mutant)
        result = verdict(path)
        if result.returncode != 1 or "FAIL:" not in result.stdout:
            raise SystemExit(f"mutant survived or failed unattributably: {name}\n{result.stdout}{result.stderr}")
        print(f"ok - {name}")
print(f"{len(mutations) + 1} passed, 0 failed")
PY
