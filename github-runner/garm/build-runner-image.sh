#!/usr/bin/env bash
# build-runner-image.sh — reproducible pre-baked GARM runner image builder (case_3f133c53).
#
# SOURCE-ONLY in this checkout: the operator executes this on a pool host in a
# separately coordinated window after independent exact-head review, green CI,
# merge, and explicit execution release. A source merge authorizes no build.
#
# What it builds: a local LXD image alias `garm-runner-YYYYMMDD` for the
# generic-private role (sudo + local Docker daemon + PHP/Composer + mysqldump
# + gh + Go + pinned govulncheck), containing the pinned actions-runner
# tarball staged at the path GARM's install template reuses
# (/home/runner/actions-runner) and every apt package cloud-init/extra-specs
# installs today on every boot — so per-job VMs skip the download + apt +
# docker-install path that costs 2-5 minutes now. An EXIT trap deletes the
# builder VM on any failure; the receipt records tool versions measured inside
# the builder, never copies of the pins.
#
# What it never does: no secrets in the image (pins are nonsecret; the script
# refuses credential-shaped keys and private-key material), no runner
# registration (no config.sh, no .runner/.credentials — GARM still injects the
# JIT config at boot), no TCP Docker relay, no remote DOCKER_HOST. Ephemeral
# one-job-per-VM lifecycle and the docker-socket boundary are unchanged.
#
# Pre-flight (operator, before execution; unknown syntax is HOLD, never guess):
#   lxc --help; lxc launch --help; lxc publish --help
#   lxc version   (recorded in the receipt as lxd_version)
#
# Usage:
#   build-runner-image.sh --pins pins.json --base-digest <64hex>
#       [--date YYYYMMDD] [--context ./build-context] [--receipt-out receipt.json]
#       [--builder-name NAME] [--dry-run]
#
# Pins file schema (garm-runner-image-pins.v1, nonsecret, operator-collected
# from the current pool template — exact versions, never `latest`):
#   {"schema": ..., "runner": {"version","archive","sha256"},
#    "node_tarball": {"file","sha256"}, "go_tarball": {"file","sha256"},
#    "keyrings": [{"file","sha256"}], "apt_sources": [{"file","sha256"}],
#    "apt_packages": [{"name","version"}], "tool_versions": {...11 tools...}}
# The --context dir holds the runner tarball, the node tarball, the Go
# tarball, and the operator-verified keyring/source files named in pins.
# Nothing is downloaded by this script except apt inside the builder VM from
# the pinned sources and the pinned `go install ...@v1.8.0` of govulncheck
# (build-time network only; runtime stays GOPROXY=off with the warm cache).
set -Eeuo pipefail

PINS=""; BASE_DIGEST=""; DATE_STR=""; CONTEXT="./build-context"
RECEIPT_OUT=""; BUILDER_NAME=""; DRY_RUN=0

refuse() { printf 'REFUSED: %s\n' "$1" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pins) PINS="${2:-}"; shift 2 ;;
    --base-digest) BASE_DIGEST="${2:-}"; shift 2 ;;
    --date) DATE_STR="${2:-}"; shift 2 ;;
    --context) CONTEXT="${2:-}"; shift 2 ;;
    --receipt-out) RECEIPT_OUT="${2:-}"; shift 2 ;;
    --builder-name) BUILDER_NAME="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) refuse "unknown argument: $1" ;;
  esac
done

[[ -n "$PINS" ]] || refuse "--pins is required"
[[ -f "$PINS" ]] || refuse "pins file not found: $PINS"
[[ "$BASE_DIGEST" =~ ^[0-9a-f]{64}$ ]] || refuse "--base-digest must be 64 hex"
if [[ -z "$DATE_STR" ]]; then DATE_STR=$(date -u +%Y%m%d); fi
[[ "$DATE_STR" =~ ^[0-9]{8}$ ]] || refuse "--date must be YYYYMMDD"
date -u -d "${DATE_STR:0:4}-${DATE_STR:4:2}-${DATE_STR:6:2}" +%s >/dev/null 2>&1 \
  || refuse "--date is not a calendar date"
ALIAS="garm-runner-${DATE_STR}"
if [[ -z "$BUILDER_NAME" ]]; then BUILDER_NAME="garm-img-build-${DATE_STR}-$$"; fi
[[ -d "$CONTEXT" ]] || refuse "context dir not found: $CONTEXT"
[[ -n "$RECEIPT_OUT" ]] || RECEIPT_OUT="${ALIAS}.receipt.json"

# Every refuse or set -e exit after `lxc init` must not leak a running builder
# VM on the pool host: the EXIT trap deletes it (forced; publish path deletes
# first and disarms via BUILDER_CREATED=0, so the trap then no-ops).
BUILDER_CREATED=0
cleanup_builder() {
  if [[ "$BUILDER_CREATED" == 1 ]]; then
    lxc delete --force "$BUILDER_NAME" >/dev/null 2>&1 || true
  fi
}
trap cleanup_builder EXIT

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_ENV="$HERE/../actions-runner.env"
PROFILE="$HERE/generic-image-profile.json"
[[ -f "$RUNNER_ENV" ]] || refuse "missing $RUNNER_ENV"
[[ -f "$PROFILE" ]] || refuse "missing $PROFILE"

# 1-2. Validate pins + verify context artifacts by hash (stdlib python only):
# shape, exact versions, no secrets, runner pin cross-checked against
# github-runner/actions-runner.env. Prints INPUTS_SHA256 on stdout.
INPUTS_SHA256=$(python3 "$HERE/validate_runner_pins.py" "$PINS" \
  --runner-env "$RUNNER_ENV" --context "$CONTEXT") \
  || refuse "pins or context validation failed; see HOLD above"
echo "inputs_sha256: $INPUTS_SHA256"
echo "context: all artifact hashes match"

RUNNER_ARCHIVE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["runner"]["archive"])' "$PINS")
NODE_FILE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["node_tarball"]["file"])' "$PINS")
GO_FILE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["go_tarball"]["file"])' "$PINS")

if [[ "$DRY_RUN" == 1 ]]; then
  cat <<EOF
dry-run plan (no host change):
  base fingerprint : $BASE_DIGEST
  builder VM       : $BUILDER_NAME (ephemeral, deleted after publish)
  image alias      : $ALIAS
  runner archive   : $RUNNER_ARCHIVE (hash-verified, never configured here)
  node bundle      : $NODE_FILE (hash-verified)
  go bundle        : $GO_FILE (hash-verified)
  receipt out      : $RECEIPT_OUT
steps: lxc init <fingerprint> --vm (EXIT trap deletes the builder on any
failure); install operator-verified keyrings/sources first, then the pinned
apt set (includes gh); stage (not register) the runner at /home/runner/actions-runner so
GARM reuses it instead of re-downloading; extract Node + Go bundles to
/opt/node24 + /opt/go; `go install` the pinned govulncheck v1.8.0 and warm the
runner-owned module cache so `GOPROXY=off go run ...govulncheck` resolves
locally; measure the 11 tool versions inside the builder and refuse drift from
pins; machine-id/host-key reset; stop; publish --alias; delete builder; write
receipt from measured versions. Pool image switch + canary are separate
operator steps.
EOF
  exit 0
fi

# 3. Live build (operator host only). Every step fails the run on error.
command -v lxc >/dev/null || refuse "lxc not installed on this host"
LXD_VERSION=$(lxc version 2>/dev/null || refuse "cannot read lxc version")
echo "lxd_version: $LXD_VERSION"
git -C "$HERE/../.." diff --quiet HEAD -- 2>/dev/null \
  || refuse "toolkit checkout is dirty; build only from a clean reviewed revision"
BUILDER_REVISION=$(git -C "$HERE/../.." --no-replace-objects rev-parse HEAD)
PROFILE_SHA256=$(sha256sum "$PROFILE" | cut -d' ' -f1)

lxc init "$BASE_DIGEST" "$BUILDER_NAME" --vm \
  || refuse "base fingerprint not available locally; never substitute another base"
BUILDER_CREATED=1
lxc start "$BUILDER_NAME"
lxc exec "$BUILDER_NAME" -- cloud-init status --wait \
  || refuse "builder cloud-init did not settle"

# Operator-verified third-party sources FIRST (keyrings + lists travel in
# context, never fetched by curl|sh here): third-party packages such as PHP
# 8.5 are absent from the Ubuntu 24.04 archive, so the pinned `apt-get
# install` below only resolves after these are in place. Each file is pushed
# and installed by its exact pins-listed name; every step fails the run on
# error — no glob that misfires on an empty list, no swallowed stderr.
CTX_TMP="/root/img-build-context"
lxc exec "$BUILDER_NAME" -- mkdir -p "$CTX_TMP"
mapfile -t KEYRING_FILES < <(python3 -c 'import json,sys; [print(b["file"]) for b in json.load(open(sys.argv[1])).get("keyrings", [])]' "$PINS")
mapfile -t SOURCE_FILES < <(python3 -c 'import json,sys; [print(b["file"]) for b in json.load(open(sys.argv[1])).get("apt_sources", [])]' "$PINS")
for f in ${KEYRING_FILES[@]+"${KEYRING_FILES[@]}"}; do
  lxc file push "$CONTEXT/$f" "$BUILDER_NAME$CTX_TMP/$f"
  lxc exec "$BUILDER_NAME" -- install -m 644 "$CTX_TMP/$f" "/usr/share/keyrings/$f"
done
for f in ${SOURCE_FILES[@]+"${SOURCE_FILES[@]}"}; do
  lxc file push "$CONTEXT/$f" "$BUILDER_NAME$CTX_TMP/$f"
  lxc exec "$BUILDER_NAME" -- install -m 644 "$CTX_TMP/$f" "/etc/apt/sources.list.d/$f"
done

# Pinned toolchain inside the builder VM (exact =version pins from pins file).
APT_LIST=$(python3 -c 'import json,sys; print(" ".join("%s=%s" % (p["name"], p["version"]) for p in json.load(open(sys.argv[1]))["apt_packages"]))' "$PINS")
lxc exec "$BUILDER_NAME" -- apt-get update
# shellcheck disable=SC2086
lxc exec "$BUILDER_NAME" -- apt-get install -y --no-install-recommends $APT_LIST
lxc exec "$BUILDER_NAME" -- rm -rf /var/lib/apt/lists/*

# Stage (not register) the runner at the path GARM's install template reuses
# (RUN_HOME=/home/<user>/actions-runner): a runner anywhere else is ignored
# at boot and the tarball is re-downloaded, losing the boot-time win.
# installdependencies.sh runs now (as root, apt) so boots skip it too.
RUN_USER="runner"
RUN_HOME="/home/runner/actions-runner"
if lxc exec "$BUILDER_NAME" -- id "$RUN_USER" >/dev/null 2>&1; then
  : "runner user already present"
else
  lxc exec "$BUILDER_NAME" -- useradd -m -s /bin/bash "$RUN_USER"
fi
# The builder pre-creates `runner` so the staged tree has stable ownership,
# but cloud-init then skips user creation on job VMs — and with it the group
# setup GARM's default_user would apply. Mirror that membership here: sudo
# per the generic profile's runtime_user, docker for service-container jobs
# (the docker group exists by now from the pinned toolchain install).
lxc exec "$BUILDER_NAME" -- usermod -aG sudo,docker "$RUN_USER"
lxc file push "$CONTEXT/$RUNNER_ARCHIVE" "$BUILDER_NAME/opt/stage-runner.tar.gz"
lxc file push "$CONTEXT/$NODE_FILE" "$BUILDER_NAME/opt/stage-node.tar.gz"
lxc file push "$CONTEXT/$GO_FILE" "$BUILDER_NAME/opt/stage-go.tar.gz"
# The credential scan below excludes Go source shipped by this archive only.
# Refuse to merge the pinned distribution into any pre-existing Go tree.
if ! lxc exec "$BUILDER_NAME" -- test ! -e /opt/go; then
  refuse "pre-existing /opt/go tree in builder; refusing to merge with pinned Go source"
fi
lxc exec "$BUILDER_NAME" -- mkdir -p "$RUN_HOME" /opt/node24 /opt/go
lxc exec "$BUILDER_NAME" -- tar -xzf /opt/stage-runner.tar.gz -C "$RUN_HOME"
lxc exec "$BUILDER_NAME" -- tar -xzf /opt/stage-node.tar.gz --strip-components=1 -C /opt/node24
lxc exec "$BUILDER_NAME" -- tar -xzf /opt/stage-go.tar.gz --strip-components=1 -C /opt/go
lxc exec "$BUILDER_NAME" -- rm -f /opt/stage-runner.tar.gz /opt/stage-node.tar.gz /opt/stage-go.tar.gz
# Pinned govulncheck (build-time network only): install the exact v1.8.0 as the
# job user so the binary and the warm module cache are runner-owned. Run from
# the runner home: lxc exec starts in /root, which runner cannot enter. Runtime
# stays GOPROXY=off with no @version fallback; `go run ...govulncheck`
# resolves from this cache via worker/go.mod's pin.
GOVULNCHECK_VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tool_versions"]["govulncheck"])' "$PINS")
lxc exec "$BUILDER_NAME" -- bash -c "cd '$RUN_HOME' && sudo -u '$RUN_USER' -H /opt/go/bin/go install 'golang.org/x/vuln/cmd/govulncheck@v${GOVULNCHECK_VERSION}' && sudo -u '$RUN_USER' -H /opt/go/bin/go clean -cache -testcache -fuzzcache"
# Expose the pinned tools on the PATH inherited by runner jobs.
lxc exec "$BUILDER_NAME" -- ln -s /opt/go/bin/go /usr/local/bin/go
lxc exec "$BUILDER_NAME" -- ln -s /opt/go/bin/gofmt /usr/local/bin/gofmt
lxc exec "$BUILDER_NAME" -- ln -s /home/runner/go/bin/govulncheck /usr/local/bin/govulncheck
lxc exec "$BUILDER_NAME" -- bash -c 'command -v go >/dev/null && command -v govulncheck >/dev/null'
lxc exec "$BUILDER_NAME" -- chown -R "$RUN_USER:$RUN_USER" "$RUN_HOME"
lxc exec "$BUILDER_NAME" -- bash -c "cd $RUN_HOME && ./bin/installdependencies.sh"
lxc exec "$BUILDER_NAME" -- "$RUN_HOME/bin/Runner.Listener" --version
lxc exec "$BUILDER_NAME" -- /opt/node24/bin/node --version
# Registration must be absent: a registered runner would bake fleet identity.
# GARM injects the JIT config into RUN_HOME at boot.
if lxc exec "$BUILDER_NAME" -- test -e "$RUN_HOME/.runner" || \
   lxc exec "$BUILDER_NAME" -- test -e "$RUN_HOME/.credentials" || \
   lxc exec "$BUILDER_NAME" -- test -e "$RUN_HOME/.credentials_rsaparams"; then
  refuse "runner registration material present in builder; image must be unregistered"
fi
# No credential artifacts staged outside the Node runtime and pinned Go source.
# Substring matching (*token*, *secret*) is refused as a method: the pinned
# runner tarball itself vendors such names (npm token.js), so it
# false-positives on every build. Match registration and private-key artifacts
# by name; prune Node runtime and the checksum-verified Go distribution.
if ! IMG_CREDENTIAL_SCAN=$(lxc exec "$BUILDER_NAME" -- bash -c 'find /home /root /opt \( -path /opt/node24 -o -path /opt/go/src \) -prune -o \( -name ".runner" -o -name ".credentials*" -o -name "*.pem" -o -name "*id_rsa*" -o -name "*.key" \) -print' 2>/dev/null); then
  refuse "credential scan failed in builder"
fi
if [[ -n "$IMG_CREDENTIAL_SCAN" ]]; then
  refuse "credential artifacts present in builder"
fi
lxc exec "$BUILDER_NAME" -- rm -rf "$CTX_TMP"

# Measure the 11 tool versions INSIDE the builder and refuse drift from pins:
# the receipt records measured versions, never copies of the pins. Raw
# --version lines travel via environment (never argv) into a local parse
# step; any unparseable line or any mismatch with pins.tool_versions refuses.
export IMG_RAW_RUNNER IMG_RAW_NODE IMG_RAW_PHP IMG_RAW_COMPOSER
export IMG_RAW_PSQL IMG_RAW_DOCKER IMG_RAW_COMPOSE IMG_RAW_MYSQLDUMP
export IMG_RAW_GH IMG_RAW_GO IMG_RAW_GOVULNCHECK
IMG_RAW_RUNNER=$(lxc exec "$BUILDER_NAME" -- "$RUN_HOME/bin/Runner.Listener" --version | tr -d '\r' | head -1)
IMG_RAW_NODE=$(lxc exec "$BUILDER_NAME" -- /opt/node24/bin/node --version | tr -d '\r' | head -1)
IMG_RAW_PHP=$(lxc exec "$BUILDER_NAME" -- php --version | tr -d '\r' | head -1)
IMG_RAW_COMPOSER=$(lxc exec "$BUILDER_NAME" -- composer --version | tr -d '\r' | head -1)
IMG_RAW_PSQL=$(lxc exec "$BUILDER_NAME" -- psql --version | tr -d '\r' | head -1)
IMG_RAW_DOCKER=$(lxc exec "$BUILDER_NAME" -- docker --version | tr -d '\r' | head -1)
IMG_RAW_COMPOSE=$(lxc exec "$BUILDER_NAME" -- docker compose version | tr -d '\r' | head -1)
IMG_RAW_MYSQLDUMP=$(lxc exec "$BUILDER_NAME" -- mysqldump --version | tr -d '\r' | head -1)
IMG_RAW_GH=$(lxc exec "$BUILDER_NAME" -- gh --version | tr -d '\r' | head -1)
IMG_RAW_GO=$(lxc exec "$BUILDER_NAME" -- /opt/go/bin/go version | tr -d '\r' | head -1)
IMG_RAW_GOVULNCHECK=$(lxc exec "$BUILDER_NAME" -- /opt/go/bin/go version -m /home/runner/go/bin/govulncheck 2>/dev/null | tr -d '\r' | awk '$1 == "mod" && $2 == "golang.org/x/vuln" && !found { print $2 " " $3; found=1 }')
MEASURED_JSON="$(mktemp)"
python3 - "$PINS" "$MEASURED_JSON" <<'MEASURE_EOF'
import json, os, re, sys
pins_path, out_path = sys.argv[1:3]
pins = json.load(open(pins_path, encoding="utf-8"))
raw = {
    "runner": os.environ.get("IMG_RAW_RUNNER", ""),
    "node": os.environ.get("IMG_RAW_NODE", ""),
    "php": os.environ.get("IMG_RAW_PHP", ""),
    "composer": os.environ.get("IMG_RAW_COMPOSER", ""),
    "psql": os.environ.get("IMG_RAW_PSQL", ""),
    "docker": os.environ.get("IMG_RAW_DOCKER", ""),
    "docker_compose": os.environ.get("IMG_RAW_COMPOSE", ""),
    "mysqldump": os.environ.get("IMG_RAW_MYSQLDUMP", ""),
    "gh": os.environ.get("IMG_RAW_GH", ""),
    "go": os.environ.get("IMG_RAW_GO", ""),
    "govulncheck": os.environ.get("IMG_RAW_GOVULNCHECK", ""),
}
patterns = {
    "runner": r"^(\d+\.\d+\.\d+)\s*$",
    "node": r"^v(\d+\.\d+\.\d+)\s*$",
    "php": r"PHP (\d+\.\d+\.\d+)",
    "composer": r"Composer version (\d+\.\d+\.\d+)",
    "psql": r"psql \(PostgreSQL\) (\d+\.\d+(?:\.\d+)?)",
    "docker": r"Docker version (\d+\.\d+\.\d+)",
    "docker_compose": r"Docker Compose version v?(\d+\.\d+\.\d+)",
    "mysqldump": r"Ver (\d+\.\d+\.\d+)",
    "gh": r"gh version (\d+\.\d+\.\d+)",
    "go": r"go version go(\d+\.\d+\.\d+)",
    "govulncheck": r"golang\.org/x/vuln\s+v?(\d+\.\d+\.\d+)",
}
measured = {}
for name, pattern in patterns.items():
    match = re.search(pattern, raw[name])
    if not match:
        print("REFUSED: unparseable %s version line" % name, file=sys.stderr)
        sys.exit(2)
    measured[name] = match.group(1)
drifted = [name for name in measured
           if str(pins["tool_versions"].get(name)) != measured[name]]
if drifted:
    print("REFUSED: measured tool drift from pins: %s" % ",".join(sorted(drifted)),
          file=sys.stderr)
    sys.exit(2)
json.dump(measured, open(out_path, "w", encoding="utf-8"),
          indent=2, sort_keys=True)
open(out_path, "a", encoding="utf-8").write("\n")
print("measured: " + json.dumps(measured, sort_keys=True))
MEASURE_EOF
[[ -s "$MEASURED_JSON" ]] || refuse "tool version measurement produced no output"

# Fresh-VM identity: each job VM must regenerate its own identity.
lxc exec "$BUILDER_NAME" -- rm -f /etc/ssh/ssh_host_*
lxc exec "$BUILDER_NAME" -- truncate -s 0 /etc/machine-id
lxc exec "$BUILDER_NAME" -- cloud-init clean --logs

lxc stop "$BUILDER_NAME" --timeout 60
FINGERPRINT=$(lxc publish "$BUILDER_NAME" --alias "$ALIAS" --public=false --verbose 2>&1 \
  | grep -oE '[0-9a-f]{64}' | head -1)
[[ "$FINGERPRINT" =~ ^[0-9a-f]{64}$ ]] || refuse "could not read published fingerprint"
lxc delete "$BUILDER_NAME"
BUILDER_CREATED=0
echo "published: $ALIAS fingerprint $FINGERPRINT"

# 4. Receipt (case_190ff300 pattern): digests + provenance, never an admission.
# tool_versions are the MEASURED versions from the builder, never a copy of
# the pins (the measure step already refused drift, and the offline receipt
# checker re-enforces the runner/series pins).
BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$RECEIPT_OUT" "$ALIAS" "$BUILT_AT" "$BUILDER_REVISION" "$BASE_DIGEST" \
  "$INPUTS_SHA256" "$PROFILE_SHA256" "$PINS" "$FINGERPRINT" "$LXD_VERSION" \
  "$MEASURED_JSON" <<'RECEIPT_EOF'
import json, sys
args = sys.argv[1:12]
(out, alias, built_at, rev, base, inputs, profile,
 pins_path, fp, lxd, measured_path) = args
measured = json.load(open(measured_path, encoding="utf-8"))
receipt = {
    "schema": "garm-runner-image-receipt.v1",
    "alias": alias,
    "built_at": built_at,
    "builder_revision": rev,
    "role": "generic-private",
    "base": {"image": "ubuntu:24.04", "digest": base},
    "inputs_sha256": inputs,
    "profile_sha256": profile,
    "pins_sha256": inputs,
    "tool_versions": measured,
    "docker": {"local_daemon_only": True, "tcp_relay": False, "docker_host_set": False},
    "guarantees": {"no_secrets": True, "no_registration": True,
                   "jit_config_preserved": True, "ephemeral_preserved": True},
    "output": {"fingerprint": fp},
    "evidence_class": "operator-built",
    "admission_authorized": False,
    "host_verified": False,
    "installed": False,
}
json.dump(receipt, open(out, "w", encoding="utf-8"), indent=2, sort_keys=True)
open(out, "a", encoding="utf-8").write("\n")
print("receipt: " + out)
RECEIPT_EOF

python3 "$HERE/validate_runner_image.py" "$RECEIPT_OUT"
rm -f "$MEASURED_JSON"
echo "BUILD COMPLETE: $ALIAS ($FINGERPRINT). Rollback reference: record the pool's current image before any switch."
