#!/usr/bin/env python3
"""Offline pre-baked image pins checks only; never a build, download or host action."""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SCHEMA = "garm-runner-image-pins.v1"
MAX_BYTES = 1024 * 1024
TOOLS = ("runner", "node", "php", "composer", "psql", "docker",
         "docker_compose", "mysqldump", "gh", "go", "govulncheck")
RUNNER_ENV_KEYS = ("RUNNER_VERSION", "RUNNER_ARCHIVE", "RUNNER_URL", "RUNNER_SHA256")
SECRET = re.compile(r"password|passwd|secret|token|api[-_]?key|_key|BEGIN .*PRIVATE KEY", re.I)
CREDENTIAL_VALUE = re.compile(r"(ghp_|github_pat_|xox[bpas]-|-----BEGIN .*PRIVATE KEY)")
NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]*")
TOOL_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")
HEX64 = re.compile(r"[0-9a-f]{64}")


class InvalidPins(ValueError):
    pass


def require(condition, field):
    if not condition:
        raise InvalidPins(field)


def scan(node, field):
    if isinstance(node, dict):
        for key, item in node.items():
            require(not SECRET.search(key), field + ": credential-shaped pins key")
            scan(item, field)
    elif isinstance(node, list):
        for item in node:
            scan(item, field)
    elif isinstance(node, str):
        require(not CREDENTIAL_VALUE.search(node), field + ": credential-shaped pins value")


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "JSON: duplicate key")
        value[key] = item
    return value


def parse(raw):
    require(len(raw) <= MAX_BYTES, "JSON: input exceeds 1MiB")

    def reject_constant(_value):
        raise InvalidPins("JSON: non-finite number")
    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    except InvalidPins:
        raise
    except (ValueError, TypeError):
        raise InvalidPins("JSON: malformed document")


def load_runner_env(path):
    """Read KEY=VALUE pins from github-runner/actions-runner.env."""
    pairs = {}
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            require("=" in line, "runner env: malformed line")
            key, _, val = line.partition("=")
            pairs[key.strip()] = val.strip()
    for key in RUNNER_ENV_KEYS:
        require(pairs.get(key), "runner env: missing " + key)
    return pairs


def validate(pins, runner_env, context=None):
    """Validate pins; with context, also verify every staged artifact hash."""
    require(type(pins) is dict, "pins: object required")
    require(pins.get("schema") == SCHEMA, "pins: schema must be " + SCHEMA)
    scan(pins, "pins")

    runner = pins.get("runner")
    require(type(runner) is dict, "pins.runner: object required")
    for key, env_key in (("version", "RUNNER_VERSION"),
                         ("archive", "RUNNER_ARCHIVE"),
                         ("sha256", "RUNNER_SHA256")):
        require(runner.get(key) == runner_env.get(env_key),
                "pins.runner." + key + ": drift from actions-runner.env")
    require(HEX64.fullmatch(runner["sha256"]) is not None, "pins.runner.sha256: want 64 hex")

    node = pins.get("node_tarball")
    require(type(node) is dict, "pins.node_tarball: object required")
    require(type(node.get("file")) is str and node["file"] and "/" not in node["file"],
            "pins.node_tarball.file: bare filename required")
    require(HEX64.fullmatch(node.get("sha256", "")) is not None,
            "pins.node_tarball.sha256: want 64 hex")

    gotar = pins.get("go_tarball")
    require(type(gotar) is dict, "pins.go_tarball: object required")
    require(type(gotar.get("file")) is str and gotar["file"] and "/" not in gotar["file"],
            "pins.go_tarball.file: bare filename required")
    require(HEX64.fullmatch(gotar.get("sha256", "")) is not None,
            "pins.go_tarball.sha256: want 64 hex")

    tools = pins.get("tool_versions")
    require(type(tools) is dict and set(tools) == set(TOOLS),
            "pins.tool_versions: missing or unexpected tools")
    for name in TOOLS:
        require(TOOL_VERSION.fullmatch(str(tools[name])) is not None,
                "pins.tool_versions." + name + ": exact version required")
        require("latest" not in str(tools[name]).lower(),
                "pins.tool_versions." + name + ": floating tag refused")
    require(tools["runner"] == runner_env["RUNNER_VERSION"],
            "pins.tool_versions.runner: drift from actions-runner.env")
    require(tools["node"].split(".")[0] == "24", "pins.tool_versions.node: series 24 required")
    require(tools["php"].split(".")[0:2] == ["8", "5"],
            "pins.tool_versions.php: series 8.5 required")
    require(tools["composer"].split(".")[0] == "2",
            "pins.tool_versions.composer: series 2 required")
    require(tools["docker_compose"].split(".")[0] == "2",
            "pins.tool_versions.docker_compose: v2 plugin required")
    require(tools["gh"].split(".")[0] == "2",
            "pins.tool_versions.gh: GitHub CLI v2 required")
    require(tools["go"].split(".")[0:2] == ["1", "26"],
            "pins.tool_versions.go: series 1.26 required")
    require(str(tools["govulncheck"]) == "1.8.0",
            "pins.tool_versions.govulncheck: exact 1.8.0 pin required")

    for pkg in pins.get("apt_packages", []):
        require(type(pkg) is dict, "pins.apt_packages: objects required")
        require(NAME.fullmatch(str(pkg.get("name", ""))) is not None,
                "pins.apt_packages: malformed package name")
        version = str(pkg.get("version", ""))
        require(VERSION.fullmatch(version) is not None and version not in ("latest", ""),
                "pins.apt_packages: exact =version pin required")

    wanted = []
    for blob in list(pins.get("keyrings", [])) + list(pins.get("apt_sources", [])):
        require(type(blob) is dict, "pins context entry: object required")
        require(type(blob.get("file")) is str and blob["file"]
                and "/" not in blob["file"] and not blob["file"].startswith("."),
                "pins context entry: bare filename required")
        require(HEX64.fullmatch(blob.get("sha256", "")) is not None,
                "pins context entry: want 64 hex")
        wanted.append((blob["file"], blob["sha256"]))

    if context is not None:
        root = Path(context)
        staged = [(runner["archive"], runner["sha256"]),
                  (node["file"], node["sha256"]),
                  (gotar["file"], gotar["sha256"])] + wanted
        for name, want in staged:
            path = root / name
            require(path.is_file(), "context: missing " + name)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            require(digest == want, "context: hash mismatch")

    canonical = json.dumps(pins, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pins", type=Path)
    parser.add_argument("--runner-env", type=Path, default=None)
    parser.add_argument("--context", type=Path, default=None)
    parser.add_argument("--canonical-out", type=Path, default=None)
    args = parser.parse_args(argv)
    env_path = args.runner_env
    if env_path is None:
        env_path = Path(__file__).resolve().parent.parent / "actions-runner.env"
    try:
        raw = args.pins.read_bytes()
        require(len(raw) > 0, "pins: empty file")
        pins = parse(raw.decode("utf-8"))
        runner_env = load_runner_env(env_path)
        inputs_sha256 = validate(pins, runner_env, args.context)
        if args.canonical_out is not None:
            canonical = json.dumps(pins, sort_keys=True, separators=(",", ":"))
            args.canonical_out.write_text(canonical, encoding="utf-8")
    except (OSError, ValueError):
        print("HOLD: malformed pins or unreadable inputs; "
              "nulls are not pins and cannot be used to build or execute",
              file=sys.stderr)
        return 1
    print(inputs_sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
