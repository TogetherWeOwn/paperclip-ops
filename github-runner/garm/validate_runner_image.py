#!/usr/bin/env python3
"""Offline pre-baked runner-image receipt checks only; never admission, authorization or host proof."""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

SCHEMA = "garm-runner-image-receipt.v1"
MAX_BYTES = 1024 * 1024
ROLE = "generic-private"
BASE_IMAGE = "ubuntu:24.04"
ALIAS = re.compile(r"garm-runner-[0-9]{8}")
TOOLS = ("runner", "node", "php", "composer", "psql", "docker",
         "docker_compose", "mysqldump", "gh", "go", "govulncheck")
VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")
CLOCK = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
SECRET_NAME = re.compile(r"password|passwd|secret|token|api[-_]?key|_key", re.I)
RUNNER_ENV_KEYS = ("RUNNER_VERSION", "RUNNER_ARCHIVE", "RUNNER_URL", "RUNNER_SHA256")
TOP_KEYS = {"schema", "alias", "built_at", "builder_revision", "role",
            "base", "inputs_sha256", "profile_sha256", "pins_sha256",
            "tool_versions", "docker", "guarantees", "output",
            "evidence_class", "admission_authorized", "host_verified",
            "installed"}


class InvalidReceipt(ValueError):
    pass


def require(condition, field):
    if not condition:
        raise InvalidReceipt(field)


def obj(value, keys, field):
    require(type(value) is dict, field + ": object required")
    require(set(value) == set(keys), field + ": missing or unexpected fields")
    return value


def flag(value, expected, field):
    require(type(value) is bool and value is expected, field)


def text(value, field):
    require(type(value) is str and 0 < len(value) <= 200
            and re.fullmatch(r"[A-Za-z0-9._:/@+-]+", value) is not None, field)
    require(value.upper() not in {"UNKNOWN", "TODO", "UNVERIFIED", "TBD"}, field)
    return value


def digest(value, field):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value)
            is not None, field)


def revision(value, field):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value)
            is not None, field)


def version(value, field):
    require(type(value) is str and VERSION.fullmatch(value) is not None, field)
    require("latest" not in value.lower(), field)


def no_secret_keys(node, field):
    if isinstance(node, dict):
        for key, item in node.items():
            require(not SECRET_NAME.search(key), field + ": credential-shaped key")
            no_secret_keys(item, field)
    elif isinstance(node, list):
        for item in node:
            no_secret_keys(item, field)


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


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "JSON: duplicate key")
        value[key] = item
    return value


def parse(raw):
    require(len(raw) <= MAX_BYTES, "JSON: input exceeds 1MiB")

    def reject_constant(_value):
        raise InvalidReceipt("JSON: non-finite number")
    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    except InvalidReceipt:
        raise
    except (ValueError, TypeError):
        raise InvalidReceipt("JSON: malformed document")


def validate(receipt, runner_env):
    """Validate one pre-baked runner-image receipt against pinned runner-env pins."""
    require(type(receipt) is dict, "receipt: object required")
    require(set(receipt) == TOP_KEYS, "receipt: missing or unexpected fields")
    require(receipt["schema"] == SCHEMA, "schema: unrecognized receipt schema")
    alias = text(receipt["alias"], "alias")
    require(ALIAS.fullmatch(alias) is not None, "alias: want garm-runner-YYYYMMDD")
    clock = text(receipt["built_at"], "built_at")
    require(CLOCK.fullmatch(clock) is not None, "built_at: want UTC Z clock")
    try:
        moment = datetime.datetime.strptime(clock, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise InvalidReceipt("built_at: impossible calendar date")
    require(moment.strftime("%Y%m%d") == alias.rsplit("-", 1)[1],
            "alias: date must match built_at UTC date")
    revision(receipt["builder_revision"], "builder_revision")
    require(receipt["role"] == ROLE, "role: only generic-private is pre-baked here")

    base = obj(receipt["base"], {"image", "digest"}, "base")
    require(base["image"] == BASE_IMAGE, "base.image: only ubuntu:24.04 is supported")
    digest(base["digest"], "base.digest")

    digest(receipt["inputs_sha256"], "inputs_sha256")
    digest(receipt["profile_sha256"], "profile_sha256")
    digest(receipt["pins_sha256"], "pins_sha256")

    tools = obj(receipt["tool_versions"], set(TOOLS), "tool_versions")
    for name in TOOLS:
        version(tools[name], "tool_versions." + name)
    require(tools["runner"] == runner_env["RUNNER_VERSION"],
            "tool_versions.runner: drift from actions-runner.env")
    require(tools["node"].split(".")[0] == "24", "tool_versions.node: series 24 required")
    require(tools["php"].split(".")[0:2] == ["8", "5"], "tool_versions.php: series 8.5 required")
    require(tools["composer"].split(".")[0] == "2", "tool_versions.composer: series 2 required")
    require(tools["docker_compose"].split(".")[0] == "2",
            "tool_versions.docker_compose: v2 plugin required")
    require(tools["gh"].split(".")[0] == "2",
            "tool_versions.gh: GitHub CLI v2 required")
    require(tools["go"].split(".")[0:2] == ["1", "26"],
            "tool_versions.go: series 1.26 required")
    require(str(tools["govulncheck"]) == "1.8.0",
            "tool_versions.govulncheck: exact 1.8.0 pin required")

    docker = obj(receipt["docker"], {"local_daemon_only", "tcp_relay",
                                     "docker_host_set"}, "docker")
    flag(docker["local_daemon_only"], True, "docker.local_daemon_only")
    flag(docker["tcp_relay"], False, "docker.tcp_relay")
    flag(docker["docker_host_set"], False, "docker.docker_host_set")

    guarantees = obj(receipt["guarantees"], {"no_secrets", "no_registration",
                                             "jit_config_preserved",
                                             "ephemeral_preserved"}, "guarantees")
    for name in ("no_secrets", "no_registration", "jit_config_preserved",
                 "ephemeral_preserved"):
        flag(guarantees[name], True, "guarantees." + name)

    output = obj(receipt["output"], {"fingerprint"}, "output")
    digest(output["fingerprint"], "output.fingerprint")
    require(output["fingerprint"] != base["digest"],
            "output.fingerprint: image must differ from its base")

    require(receipt["evidence_class"] in ("synthetic", "operator-built"),
            "evidence_class: synthetic or operator-built")
    flag(receipt["admission_authorized"], False, "admission_authorized")
    flag(receipt["host_verified"], False, "host_verified")
    flag(receipt["installed"], False, "installed")

    # The guarantees object's key set is fixed above; its `no_secrets`
    # attestation name would trip the credential-shaped-key gate, so the
    # secret scan covers the receipt outside that fixed object only.
    rest = {key: item for key, item in receipt.items() if key != "guarantees"}
    no_secret_keys(rest, "receipt")
    return {"alias": alias, "admission_authorized": False,
            "host_verified": False, "migration_complete": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--runner-env", type=Path, default=None,
                        help="path to github-runner/actions-runner.env")
    args = parser.parse_args(argv)
    env_path = args.runner_env
    if env_path is None:
        env_path = Path(__file__).resolve().parent.parent / "actions-runner.env"
    try:
        raw = args.receipt.read_bytes()
        require(len(raw) > 0, "receipt: empty file")
        receipt = parse(raw.decode("utf-8"))
        runner_env = load_runner_env(env_path)
        result = validate(receipt, runner_env)
    except (OSError, ValueError):
        print("HOLD: malformed receipt or unreadable pins; "
              "nulls are not pins and cannot be used to build or execute",
              file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
