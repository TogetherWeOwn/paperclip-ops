#!/usr/bin/env bash
# Public-repository successor to the private runner-isolation contract.
set -Eeuo pipefail
workflow=${1:-.github/workflows/ci.yml}
[[ -f "$workflow" ]] || { printf 'FAIL: missing %s\n' "$workflow" >&2; exit 1; }
python3 - "$workflow" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text()
match = re.search(r"(?ms)^jobs:\n(?P<jobs>.*)\Z", text)
if not match:
    raise SystemExit("FAIL: jobs block is missing")
parts = re.split(r"(?m)^  ([a-zA-Z0-9_-]+):\n", match.group("jobs"))
bodies = dict(zip(parts[1::2], parts[2::2]))
required = {"changes", "privilege-suites", "suites", "private-mutation-gates",
            "trivy-scan", "secret-scan", "ci-ok", "failure-log-drain"}
body = bodies.get("suites", "")
routes = re.findall(r"(?m)^    runs-on: (.+)$", text)
checks = {
    "all expected jobs exist": required <= bodies.keys(),
    "required check name is unchanged": "    name: suites\n" in body,
    "every job uses a standard GitHub-hosted runner": bool(bodies) and all(
        re.findall(r"(?m)^    runs-on: (.+)$", b) == ["ubuntu-latest"]
        for b in bodies.values()),
    "no private or configurable runner route": len(routes) == len(bodies)
        and all(route == "ubuntu-latest" for route in routes),
    "no service-container regression": not re.search(r"(?m)^    services:", text),
    "no host Docker socket": not re.search(r"(?m)^\s*(?:DOCKER_HOST:|.*docker\.sock)", text),
    "runner identity is a positive control": all(
        '[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]' in bodies.get(job, "")
        for job in ("suites", "failure-log-drain")),
    "a push to main never cancels the previous main run": bool(re.search(
        r"(?m)^  cancel-in-progress: \$\{\{ github\.event_name == 'pull_request' \}\}\s*$", text)),
    "checkout cleans the workspace": bool(re.search(
        r"(?ms)- uses: actions/checkout@[0-9a-f]{40}[^\n]*\n\s+with:\n\s+clean: true", body)),
    "merge queue runs CI": bool(re.search(r"(?m)^  merge_group:", text)),
}
for name, ok in checks.items():
    print(("PASS" if ok else "FAIL") + ": " + name)
raise SystemExit(0 if all(checks.values()) else 1)
PY
