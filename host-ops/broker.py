#!/usr/bin/env python3
"""Pull-only host operations broker with gated Phase-1 write-verb previews.

The production executor has a fixed read-only verb table. Phase-1 host write
verbs (case_b1f57205) are specified for review and tests with a signed dry-run
preview path, but no production write backend exists: every write request ends
in a signed refusal while OWNER decision O4 is unrecorded.

case_5248bcfa adds the owner-approved tier model ahead of that same O4 gate: read
verbs stay automatic; routine-write verbs need one fresh CEO approval per
request; paperclip-restart additionally needs a drain receipt the broker
cannot yet verify host-side, so it can never execute in this build; and an
owner-reserved deny set (key/credential lifecycle, spend, firewall/SSH) is
refused unconditionally, before any approval or config is even consulted.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

VERSION = 1
MAX_REQUEST_TTL_SECONDS = 600
MAX_CLOCK_SKEW_SECONDS = 60
REQUEST_ID_RE = re.compile(r"^HRQ-[A-Za-z0-9][A-Za-z0-9._-]{5,95}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEPLOYED_PRIVATE_KEY_PATH = "/var/lib/paperclip-host-ops/keys/response-signing.pem"
# The image this host actually runs is host state, not a property of this source
# tree: the Compose deployment may carry a registry digest on one host and a
# local tag on another, and it rolls on every deploy. It is therefore root-owned
# configuration rendered into the exact-argv sudoers line by the installer, and
# never a request argument. The pattern is deliberately narrower than the OCI
# grammar: it forbids whitespace, commas, backslashes and quotes so a reference
# can never terminate the sudoers command or widen the grant.
IMAGE_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,159}(?::[A-Za-z0-9._-]{1,127}|@sha256:[0-9a-f]{64})?$")

# logs.read and service.inspect target the node7b1ff338 Docker Compose runtime, not a
# user-systemd unit. Host operator verified 2026-10-05: no paperclip.service
# unit exists anywhere on node7b1ff338 (not-found/inactive, empty FragmentPath). The
# application is Compose project "paperclip", service "server", container
# "paperclip" (/home/ubuntu/stacks/paperclip/compose.yaml); the log source is
# `docker logs paperclip` (local log driver), never the user journal.
# Neither verb takes a caller-selectable container, time range, limit or format.
LOG_CONTAINER = "paperclip"
LOG_UNIT = LOG_CONTAINER
LOG_WINDOW_SECONDS = 900
LOG_LINE_LIMIT = 200
LOG_RAW_BYTE_LIMIT = 64 * 1024
LOG_RESPONSE_BYTE_LIMIT = 64 * 1024
# Docker's local log driver records no syslog priority. Every entry reports
# informational (6) uniformly -- a documented constant, never per-line
# severity invented from message text (which is always redacted).
LOG_PRIORITY = 6
LOG_COMMAND = (
    "/usr/bin/sudo", "-n", "-u", "root",
    "/usr/bin/docker", "logs", LOG_CONTAINER,
    "--tail", "200", "--since", "15m", "--timestamps",
)
# Fixed container-state read. --format renders only .State so image config,
# environment and mounts never reach the broker even before field filtering.
SERVICE_COMMAND = (
    "/usr/bin/sudo", "-n", "-u", "root",
    "/usr/bin/docker", "inspect", LOG_CONTAINER, "--format", "{{json .State}}",
)

# This is the production authority surface. Request arguments are empty for
# every read verb: no request can smuggle a path, unit, image, URL, command, or
# shell fragment into the executor.
READ_VERBS: dict[str, dict[str, Any]] = {
    "health.inspect": {
        "summary": "Read the fixed Paperclip loopback health endpoint and return an allowlisted field set.",
        "arguments": {},
        "executor": "health",
    },
    "logs.read": {
        "summary": "Read up to 200 paperclip container log entries from the last 15 minutes; all free text is redacted before signing (metadata-only tail).",
        "arguments": {},
        "executor": "logs",
    },
    "service.inspect": {
        "summary": "Read fixed paperclip container state through the host's exact sudoers command.",
        "arguments": {},
        "executor": "service",
    },
    "image.inspect": {
        "summary": "Read the configured paperclipImageRef identity through the host's exact sudoers command.",
        "arguments": {},
        "executor": "image",
    },
    "host.inspect": {
        "summary": "Read non-secret host identity, uptime, load, and filesystem capacity.",
        "arguments": {},
        "executor": "host",
    },
    "tooling.locate": {
        "summary": "Find the fixed ops-tooling marker set below host-owned search roots.",
        "arguments": {},
        "executor": "locate",
    },
    "tooling.fingerprint": {
        "summary": "Hash the fixed executable inventory below the configured ops-tooling directory.",
        "arguments": {},
        "executor": "fingerprint",
    },
    "paperclip.stat-chain": {
        "summary": "Read owner, group, mode, and type for the fixed Paperclip data path and its ancestors.",
        "arguments": {},
        "executor": "stat_chain",
    },
}

# Specification only. The production dispatcher below recognizes READ_VERBS
# exclusively, plus a dry-run preview path for PHASE1_WRITE_VERBS that always
# ends in a gate refusal. These records define the review contract without
# creating a code path that can execute a host write.
#
# case_b1f57205 Phase-1 verbs. paperclip.deploy/paperclip.restart predate this
# slice, stay blocked on case_e848a83d-class credential isolation, and keep their
# immediate refusal with no preview.
#
# case_802335d7 adds coolify.app.deploy/coolify.app.restart as disabled exact-target
# staging-app specs on the same preview path: dry-run preview naming the
# planned writes, then the O4 gate refuses. No executor exists in this build.
PHASE1_WRITE_VERBS = frozenset({
    "plugin.install", "plugin.restart", "unit.install",
    "cliproxy.apply", "coolify.env.set", "script.run",
    "coolify.app.deploy", "coolify.app.restart",
})
WRITE_VERB_SPECS: dict[str, dict[str, Any]] = {
    "paperclip.deploy": {
        "enabled": False,
        "arguments": {"commit": {"type": "git_sha1", "pattern": "^[0-9a-f]{40}$"}},
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 900,
            "agentApprovalAllowed": False,
            "blockedBy": "case_e848a83d-class credential isolation remains reproducible",
        },
        "preImage": ["paperclip-local image id", "service active state", "health commit"],
        "timeoutSeconds": 900,
        "postconditions": ["health.status == ok", "health.commit == arguments.commit"],
        "rollback": {
            "operation": "paperclip.restore-preimage",
            "automatic": True,
            "steps": ["retag pre-image id as paperclip-local", "restart the paperclip container", "require healthy endpoint"],
        },
    },
    "paperclip.restart": {
        "enabled": False,
        "arguments": {},
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 300,
            "agentApprovalAllowed": False,
            "blockedBy": "case_e848a83d-class credential isolation remains reproducible",
        },
        "preImage": ["paperclip-local image id", "service active state", "health commit"],
        "timeoutSeconds": 120,
        "postconditions": ["service Status == running", "health.status == ok"],
        "rollback": {
            "operation": "paperclip.restore-preimage",
            "automatic": True,
            "steps": ["retag pre-image id as paperclip-local", "restore prior active/inactive service state", "verify prior health commit when previously active"],
        },
    },
    # case_b1f57205 Phase-1 write verbs. Same contract as above: specification only,
    # enabled False, no production executor in this build. Each spec carries an
    # allow-list descriptor, a dry-run effect list, and the O4 gate: OWNER
    # decision O4 must be recorded on case_b1f57205 before writesEnabled may leave
    # false. Request arguments are names and SHAs only -- never host paths,
    # commands, URLs, or secrets; allow-list resolution happens host-side.
    "plugin.install": {
        "enabled": False,
        "summary": "Copy a reviewed plugin bundle into the plugin packages root, then the disable/enable cycle.",
        "arguments": {
            "package": {"type": "name", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
            "sha": {"type": "git_sha1", "pattern": "^[0-9a-f]{40}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 900,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {"source": "reviewed commit export pinned by arguments.sha", "destination": "plugin packages root entry named by arguments.package", "pathPolicy": "canonicalize host-side under the declared root; refuse symlink escape before any write"},
        "preImage": ["installed bundle sha256", "plugin enabled state", "service active state"],
        "timeoutSeconds": 600,
        "postconditions": ["installed bundle sha256 matches the reviewed export", "readable modes hold", "health.status == ok"],
        "plannedWrites": ["copy reviewed bundle into the plugin packages root", "plugin disable/enable cycle"],
        "rollback": {
            "operation": "plugin.restore-previous",
            "automatic": True,
            "steps": ["restore previous bundle", "repeat the disable/enable cycle", "require healthy endpoint"],
        },
    },
    "plugin.restart": {
        "enabled": False,
        "summary": "Restart an installed plugin through the disable/enable cycle without replacing its bundle.",
        "arguments": {
            "package": {"type": "name", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 300,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {"source": "already-installed bundle entry named by arguments.package"},
        "preImage": ["installed bundle sha256", "plugin enabled state", "service active state"],
        "timeoutSeconds": 300,
        "postconditions": ["plugin enabled state == enabled", "health.status == ok"],
        "plannedWrites": ["plugin disable/enable cycle"],
        "rollback": {
            "operation": "plugin.restore-previous-state",
            "automatic": True,
            "steps": ["restore prior enabled/disabled state", "require healthy endpoint"],
        },
    },
    "unit.install": {
        "enabled": False,
        "summary": "Install a systemd user unit from an allow-listed repo path at a pinned SHA.",
        "arguments": {
            "name": {"type": "name", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
            "sha": {"type": "git_sha1", "pattern": "^[0-9a-f]{40}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 900,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {"source": "allow-listed repo unit path at the pinned arguments.sha, resolved host-side"},
        "preImage": ["installed unit file sha256 if present", "unit active state if present"],
        "timeoutSeconds": 300,
        "postconditions": ["installed unit matches the reviewed source", "daemon-reload succeeds", "unit ActiveState == active"],
        "plannedWrites": ["write unit file", "daemon-reload", "enable and start unit"],
        "rollback": {
            "operation": "unit.restore-previous",
            "automatic": True,
            "steps": ["restore previous unit file or remove the new one", "daemon-reload", "restore prior active/inactive state"],
        },
    },
    "cliproxy.apply": {
        "enabled": False,
        # CISO case_00a30085: cliproxy-only. O4 expressly excludes the sibling
        # gateway restart/revival/binding; restoring it needs an O4 amendment,
        # not a code edit. No sibling-gateway state, write, or rollback step
        # belongs here.
        "summary": "Apply a reviewed cliproxy config.yaml and restart cliproxy.",
        "arguments": {
            "sha": {"type": "git_sha1", "pattern": "^[0-9a-f]{40}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 900,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {"source": "reviewed config file at the pinned arguments.sha, resolved host-side"},
        "preImage": ["active config.yaml sha256", "cliproxy active state"],
        "timeoutSeconds": 300,
        "postconditions": ["active config.yaml matches the reviewed file", "cliproxy healthy"],
        "plannedWrites": ["replace config.yaml", "restart cliproxy"],
        "rollback": {
            "operation": "cliproxy.restore-previous",
            "automatic": True,
            "steps": ["restore previous config.yaml", "restart cliproxy", "require healthy gateway"],
        },
    },
    "coolify.env.set": {
        "enabled": False,
        "summary": "Set only per-app allow-listed Coolify environment entries from an approved versioned server-side mapping; values never enter requests or response bundles.",
        "arguments": {
            "app": {"type": "name", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
            "keys": {"type": "name_list", "pattern": "^[A-Za-z0-9._-]{1,64}(,[A-Za-z0-9._-]{1,64}){0,31}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 300,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {
            "source": "approved versioned host-owned manifest; keys are allow-listed separately for each app",
            "values": "resolve approved mapping references server-side at the pinned manifest revision; never accept caller-supplied values",
            "refuse": "missing app, non-allow-listed key, missing versioned mapping, or duplicate key",
        },
        "preImage": ["full prior values and presence/absence of every affected env entry, captured in protected host-side rollback storage only"],
        "timeoutSeconds": 120,
        "postconditions": ["approved keys match the pinned server-side mapping", "no secret value in any audit or response record"],
        "plannedWrites": ["set only the app's approved env entries from the pinned server-side mapping"],
        "rollback": {
            "operation": "coolify.env.restore-previous",
            "automatic": True,
            "steps": ["restore full prior values from protected host-side preImage", "remove entries absent in preImage", "verify exact prior values and presence without logging them"],
        },
    },
    # case_802335d7 exact-target staging-app verbs. Specification only, enabled
    # False, no production executor in this build. Arguments are names and
    # SHAs only: deploy takes {app, sha}, restart takes {app}. The Coolify
    # app UUID, environment, pinned release set and allowed deploy path are
    # resolved host-side from the versioned stagingApps catalogue; the
    # request never carries endpoint URLs, tokens, argv or secret values.
    # Missing catalogue, malformed entry, unknown app, production target,
    # or a release outside the pinned set fails closed before any preview.
    "coolify.app.deploy": {
        "enabled": False,
        "summary": "Deploy one catalogued staging app at one pinned immutable release SHA resolved host-side; never a floating ref, URL, token, or caller-supplied path.",
        "arguments": {
            "app": {"type": "name", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
            "sha": {"type": "git_sha1", "pattern": "^[0-9a-f]{40}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 900,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {
            "source": "approved versioned host-owned stagingApps catalogue entry for arguments.app at the pinned manifest revision",
            "target": "coolifyAppUuid + environment == staging + deployPath, all resolved host-side; never a request argument",
            "release": "arguments.sha must equal one entry of the catalogue allowedReleases pinned set; floating, ambiguous, or cross-app SHAs refuse",
            "refuse": "missing catalogue, unknown app, production target, malformed entry, or unpinned release",
        },
        "preImage": ["running release SHA of the catalogued staging app", "container active state of the catalogued staging app"],
        "timeoutSeconds": 900,
        "postconditions": ["running release SHA == arguments.sha", "staging health endpoint reports ok at the deployed release", "no spend side effect recorded"],
        "plannedWrites": ["deploy the catalogued staging app at the pinned release through the host allow-listed deploy path"],
        "rollback": {
            "operation": "coolify.app.restore-previous-release",
            "automatic": True,
            "steps": ["redeploy the preImage running release through the same allow-listed deploy path", "require staging health ok at the restored release"],
        },
    },
    "coolify.app.restart": {
        "enabled": False,
        "summary": "Restart one catalogued staging app container without changing its release; binds the running release and prior state, never bypasses a failed deploy.",
        "arguments": {
            "app": {"type": "name", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 300,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {
            "source": "approved versioned host-owned stagingApps catalogue entry for arguments.app at the pinned manifest revision",
            "target": "coolifyAppUuid + environment == staging, resolved host-side; never a request argument",
            "refuse": "missing catalogue, unknown app, production target, or malformed entry",
        },
        "preImage": ["running release SHA of the catalogued staging app", "container active state of the catalogued staging app"],
        "timeoutSeconds": 300,
        "postconditions": ["running release SHA unchanged from preImage", "container active state restored to prior state", "staging health endpoint reports ok", "no spend side effect recorded"],
        "plannedWrites": ["restart the catalogued staging app container at its bound running release"],
        "rollback": {
            "operation": "coolify.app.restore-previous-state",
            "automatic": True,
            "steps": ["restore prior container active/inactive state at the bound running release", "require staging health ok when previously active"],
        },
    },
    "script.run": {
        "enabled": False,
        "summary": "Run an allow-listed repo script at a pinned SHA.",
        "arguments": {
            "script": {"type": "name", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
            "sha": {"type": "git_sha1", "pattern": "^[0-9a-f]{40}$"},
        },
        "authorization": {
            "technicalKey": "board_or_human",
            "singleUse": True,
            "maximumTtlSeconds": 900,
            "agentApprovalAllowed": False,
            "blockedBy": "OWNER decision O4 on case_b1f57205; case_e848a83d-class credential isolation remains reproducible",
        },
        "allowList": {
            "source": "approved versioned host-owned manifest entry for the script at the exact arguments.sha",
            "writeScope": "explicit non-empty set of canonical absolute target paths in the manifest; confine execution to that scope, refuse symlink escape",
            "inverse": "registered inverse at the same pinned SHA; passing rollback-test evidence required before registration",
            "compositionReview": "review script/unit composition before registration, including unit files written by scripts and enabled by unit.install; prove combined rollback restores prior files and enabled/active states",
        },
        "preImage": ["script sha256 at the pinned ref", "prior contents and metadata of the declared write-scope", "composed unit enabled/active states"],
        "timeoutSeconds": 600,
        "postconditions": ["exit status == 0", "no writes outside the script's declared scope"],
        "plannedWrites": ["execute the allow-listed script"],
        "rollback": {
            "operation": "script.registered-inverse",
            "automatic": True,
            "steps": ["run the script's registered inverse at the same pinned SHA", "a script without a tested inverse is not registrable"],
        },
    },
}

# case_5248bcfa: the owner-approved tier for every write verb. routine-write needs
# one CEO approval per request; paperclip-restart additionally needs a fresh,
# host-verified drain receipt because it stops the process every other card
# depends on. Assigned here (not hand-written per spec) so the mapping can
# never drift from PAPERCLIP_RESTART_VERBS.
PAPERCLIP_RESTART_VERBS = frozenset({"paperclip.deploy", "paperclip.restart"})
for _verb, _spec in WRITE_VERB_SPECS.items():
    _spec["tier"] = "paperclip-restart" if _verb in PAPERCLIP_RESTART_VERBS else "routine-write"
del _verb, _spec

# case_5248bcfa owner-reserved deny set: key/credential lifecycle, spend, and
# firewall/SSH changes never reach approval or drain logic at all -- refused
# unconditionally, before any other request validation runs. No future config
# or approval can lift this; there is no code path downstream that consults
# one for these verbs. The namespace prefixes catch a verb that does not exist
# yet; the explicit set documents the ones that motivated this rule.
OWNER_RESERVED_VERB_NAMESPACES = ("key.", "credential.", "firewall.", "ssh.", "spend.")
OWNER_RESERVED_VERBS = frozenset({
    "key.create", "key.rotate", "key.delete",
    "credential.create", "credential.rotate", "credential.delete",
    "firewall.set", "ssh.authorize", "spend.approve",
})


def is_owner_reserved_verb(verb: str) -> bool:
    return verb in OWNER_RESERVED_VERBS or verb.startswith(OWNER_RESERVED_VERB_NAMESPACES)


def verb_tier(verb: str) -> str:
    if verb in READ_VERBS:
        return "read"
    spec = WRITE_VERB_SPECS.get(verb)
    return spec["tier"] if spec is not None else "unknown"


REQUEST_KEYS = {"version", "requestId", "requestedAt", "expiresAt", "verb", "arguments", "requester", "canonicalHash"}
REQUESTER_KEYS = {"agentId", "runId", "issueId"}
APPROVAL_KEYS = {"approver", "decisionRef", "decidedAt"}
DRAIN_RECEIPT_KEYS = {"checkedAt", "runningRuns", "newDispatchStopped"}

# case_5248bcfa write-tier schema. Both write tiers add `reason` and `approval`;
# paperclip-restart additionally adds `drainReceipt`. Read requests keep the
# original, narrower REQUEST_KEYS unchanged, so every existing read
# request/response pair stays byte-identical.
ROUTINE_WRITE_REQUEST_KEYS = REQUEST_KEYS | {"reason", "approval"}
PAPERCLIP_RESTART_REQUEST_KEYS = ROUTINE_WRITE_REQUEST_KEYS | {"drainReceipt"}

REASON_MIN_LENGTH = 1
PAPERCLIP_RESTART_REASON_MIN_LENGTH = 20
# Assumption (not numerically specified by case_5248bcfa): a CEO approval or drain
# receipt is single-use via the replay store regardless of these windows, so
# these are defence in depth against a stale-but-unconsumed approval/receipt
# sitting in a resumed request, not the primary control. Flagged for CISO to
# confirm or adjust.
APPROVAL_MAX_AGE_SECONDS = 3600
DRAIN_RECEIPT_MAX_AGE_SECONDS = 120


class Refusal(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FatalStateError(Exception):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise Refusal("invalid_timestamp", f"{field} must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise Refusal("invalid_timestamp", f"{field} is not a valid RFC3339 timestamp") from exc
    return parsed


def validate_exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise Refusal("invalid_shape", f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise Refusal("invalid_shape", f"{label} keys must be exactly {sorted(expected)}; got {sorted(actual)}")


def request_without_hash(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key != "canonicalHash"}


def validate_reason(reason: Any, tier: str) -> None:
    if not isinstance(reason, str):
        raise Refusal("invalid_reason", "reason must be a string")
    minimum = PAPERCLIP_RESTART_REASON_MIN_LENGTH if tier == "paperclip-restart" else REASON_MIN_LENGTH
    if len(reason.strip()) < minimum:
        raise Refusal("invalid_reason", f"reason must be at least {minimum} non-whitespace characters for {tier}")


def validate_approval_shape(approval: Any) -> None:
    validate_exact_keys(approval, APPROVAL_KEYS, "approval")
    if not isinstance(approval["approver"], str) or not UUID_RE.fullmatch(approval["approver"]):
        raise Refusal("invalid_approval", "approval.approver must be a UUID")
    if not isinstance(approval["decisionRef"], str) or not approval["decisionRef"].strip():
        raise Refusal("invalid_approval", "approval.decisionRef must be a non-empty string")
    parse_iso(approval["decidedAt"], "approval.decidedAt")


def validate_drain_receipt_shape(receipt: Any) -> None:
    validate_exact_keys(receipt, DRAIN_RECEIPT_KEYS, "drainReceipt")
    parse_iso(receipt["checkedAt"], "drainReceipt.checkedAt")
    if not isinstance(receipt["runningRuns"], int) or isinstance(receipt["runningRuns"], bool) or receipt["runningRuns"] < 0:
        raise Refusal("invalid_drain_receipt", "drainReceipt.runningRuns must be a non-negative integer")
    if not isinstance(receipt["newDispatchStopped"], bool):
        raise Refusal("invalid_drain_receipt", "drainReceipt.newDispatchStopped must be a boolean")


def validate_request(request: Any, at: dt.datetime | None = None) -> tuple[dict[str, Any], str]:
    if not isinstance(request, dict):
        raise Refusal("invalid_shape", "request must be an object")
    # Owner-reserved verbs are refused before anything else -- including
    # before the key-shape check below -- so a malformed owner-reserved
    # request is never mistaken for a shape error that a caller could "fix"
    # into an acceptable one. There is no config or approval field that
    # changes this outcome.
    raw_verb = request.get("verb")
    if isinstance(raw_verb, str) and is_owner_reserved_verb(raw_verb):
        raise Refusal("owner_reserved", f"verb {raw_verb!r} is permanently refused: owner-reserved, never available through this broker")
    tier = verb_tier(raw_verb) if isinstance(raw_verb, str) else "unknown"
    expected_keys = {
        "routine-write": ROUTINE_WRITE_REQUEST_KEYS,
        "paperclip-restart": PAPERCLIP_RESTART_REQUEST_KEYS,
    }.get(tier, REQUEST_KEYS)
    validate_exact_keys(request, expected_keys, "request")
    if request["version"] != VERSION:
        raise Refusal("unsupported_version", f"version must be {VERSION}")
    if not isinstance(request["requestId"], str) or not REQUEST_ID_RE.fullmatch(request["requestId"]):
        raise Refusal("invalid_request_id", "requestId must match HRQ- plus 6-96 safe characters")
    if not isinstance(request["verb"], str):
        raise Refusal("invalid_verb", "verb must be a string")
    if not isinstance(request["arguments"], dict):
        raise Refusal("invalid_arguments", "arguments must be an object")
    validate_exact_keys(request["requester"], REQUESTER_KEYS, "requester")
    for key in REQUESTER_KEYS:
        if not isinstance(request["requester"][key], str) or not UUID_RE.fullmatch(request["requester"][key]):
            raise Refusal("invalid_requester", f"requester.{key} must be a UUID")
    if tier in ("routine-write", "paperclip-restart"):
        validate_reason(request["reason"], tier)
        validate_approval_shape(request["approval"])
    if tier == "paperclip-restart":
        validate_drain_receipt_shape(request["drainReceipt"])

    requested = parse_iso(request["requestedAt"], "requestedAt")
    expires = parse_iso(request["expiresAt"], "expiresAt")
    clock = at or now_utc()
    if requested > clock + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise Refusal("request_from_future", "requestedAt exceeds the allowed clock skew")
    if expires <= requested:
        raise Refusal("invalid_expiry", "expiresAt must be later than requestedAt")
    if (expires - requested).total_seconds() > MAX_REQUEST_TTL_SECONDS:
        raise Refusal("ttl_too_long", f"request TTL exceeds {MAX_REQUEST_TTL_SECONDS} seconds")

    calculated = sha256_bytes(canonical_bytes(request_without_hash(request)))
    if not isinstance(request["canonicalHash"], str) or request["canonicalHash"] != calculated:
        raise Refusal("canonical_hash_mismatch", "canonicalHash does not match the canonical request")
    return request, calculated


def validate_read_request(request: dict[str, Any]) -> dict[str, Any]:
    verb = request["verb"]
    if verb in WRITE_VERB_SPECS:
        raise Refusal("write_execution_disabled", "host write execution is not enabled on this broker")
    spec = READ_VERBS.get(verb)
    if spec is None:
        raise Refusal("unknown_verb", "verb is not in the fixed read-only table")
    if request["arguments"] != spec["arguments"]:
        raise Refusal("invalid_arguments", f"{verb} accepts exactly {spec['arguments']}")
    return spec


def _reject_dot_name(verb: str, field: str, value: str) -> None:
    """Reject dot-only and leading-dot names (CISO case_00a30085 change 2).

    The shared name pattern `^[A-Za-z0-9._-]{1,64}$` admits `.` and `..`
    (proven by direct match), so the pattern alone cannot keep such names
    out of the request stream. Canonicalization under the declared root
    and symlink-escape refusal are execution-backend duties (case_9011453f);
    this gate rejects dot-only/leading-dot names at request validation,
    before any preview is rendered.
    """
    if set(value) <= {"."} or value.startswith("."):
        raise Refusal("invalid_arguments", f"{verb}.{field} must not be dot-only or start with '.'")


def validate_write_arguments(request: dict[str, Any]) -> dict[str, Any]:
    """Shape-check a Phase-1 write request against its verb spec.

    No config, gate or execution. Manifest checks happen separately on the
    host before rendering a preview, never in the requester's CLI process.
    """
    verb = request["verb"]
    if verb not in PHASE1_WRITE_VERBS:
        if verb in WRITE_VERB_SPECS:
            raise Refusal("write_execution_disabled", "host write execution is not enabled on this broker")
        raise Refusal("unknown_verb", "verb is not a registered write verb")
    spec = WRITE_VERB_SPECS[verb]
    declared = spec["arguments"]
    arguments = request["arguments"]
    if not isinstance(arguments, dict) or set(arguments) != set(declared):
        raise Refusal("invalid_arguments", f"{verb} accepts exactly the arguments {sorted(declared)}")
    for name, descriptor in declared.items():
        value = arguments[name]
        if not isinstance(value, str) or not re.fullmatch(descriptor["pattern"], value):
            raise Refusal("invalid_arguments", f"{verb}.{name} does not match {descriptor['pattern']}")
        if descriptor.get("type") == "name":
            _reject_dot_name(verb, name, value)
        elif descriptor.get("type") == "name_list":
            for element in value.split(","):
                _reject_dot_name(verb, name, element)
    return spec


def _require_staging_app_entry(manifest: dict[str, Any], app: str) -> dict[str, Any]:
    """Resolve one stagingApps catalogue entry host-side, failing closed.

    The catalogue is host-owned and versioned: app names and release SHAs
    arrive in the request, while the Coolify app UUID, environment, pinned
    release set and allowed deploy path resolve here. Endpoint URLs, tokens,
    arbitrary argv and secret values can never enter through this path
    because no such field exists on either side of the lookup.
    """
    apps = manifest.get("stagingApps")
    entry = apps.get(app) if isinstance(apps, dict) else None
    if not isinstance(entry, dict):
        raise Refusal("app_not_allowlisted", "app is absent from the versioned staging catalogue")
    if entry.get("environment") != "staging":
        raise Refusal("prod_target_refused", "only environment == staging is eligible; production refuses")
    uuid = entry.get("coolifyAppUuid")
    if not isinstance(uuid, str) or not UUID_RE.fullmatch(uuid):
        raise Refusal("staging_catalogue_invalid", "catalogue entry requires a UUID coolifyAppUuid")
    releases = entry.get("allowedReleases")
    if (not isinstance(releases, list) or not 1 <= len(releases) <= 64
            or any(not isinstance(item, str) or not SHA_RE.fullmatch(item) for item in releases)
            or len(set(releases)) != len(releases)):
        raise Refusal("staging_catalogue_invalid", "catalogue entry requires 1-64 unique pinned 40-hex allowedReleases")
    path = entry.get("deployPath")
    if (not isinstance(path, str) or not path.startswith("/") or path == "/"
            or "\x00" in path or "://" in path
            or any(part in {"", ".", ".."} for part in path[1:].split("/"))):
        raise Refusal("staging_catalogue_invalid", "catalogue entry requires a canonical absolute deployPath, never a URL")
    return entry


def verify_deploy_succeeded_host_side(config: dict[str, Any], app: str) -> bool | None:
    """Host-side confirmation that the app's last deploy succeeded.

    Caller state is never sufficient on its own -- see
    validate_coolify_restart_precondition. This build has no wiring to the
    Coolify deploy ledger and always returns None (unknown), which the
    precondition treats as a refusal: a staging-app restart cannot pass this
    gate in this build regardless of the request. Wiring a real host-side
    ledger query is a separate, reviewed follow-up.
    """
    return None


def validate_coolify_restart_precondition(request: dict[str, Any], config: dict[str, Any], deploy_ledger: Any = None) -> None:
    """Refuse a staging-app restart that is not bound to a successful deploy.

    With an explicit synthetic ledger (tests only), a missing or failed
    record refuses with deploy_required so a restart can never bypass row 1.
    Without ledger wiring (this build: deploy_ledger is None), refuse with
    deploy_verification_unavailable: caller attestation alone never
    authorizes a restart.
    """
    app = request["arguments"]["app"]
    if deploy_ledger is None:
        if verify_deploy_succeeded_host_side(config, app) is not True:
            raise Refusal(
                "deploy_verification_unavailable",
                "the broker cannot independently confirm a successful deploy; a restart must bind the running release of a succeeded deploy",
            )
        return
    if not isinstance(deploy_ledger, dict):
        raise Refusal("deploy_required", "restart requires a recorded successful deploy for this app")
    record = deploy_ledger.get(app)
    if (not isinstance(record, dict) or record.get("status") != "succeeded"
            or not isinstance(record.get("releaseSha"), str)
            or not SHA_RE.fullmatch(record["releaseSha"])):
        raise Refusal("deploy_required", "restart requires a recorded successful deploy for this app; a failed deploy cannot be bypassed by restart")


def validate_write_manifest(request: dict[str, Any], manifest: Any) -> str | None:
    """Check host-owned env/script/staging-app manifest records before a dry-run preview.

    This is a specification gate, not an approval verifier or write backend.
    Only references to mappings/evidence are stored here, never secret values.
    Execution-time approval verification, realpath confinement, inverse testing
    and composition enforcement remain prerequisites of the disabled backend.
    """
    verb, args = request["verb"], request["arguments"]
    if verb not in {"coolify.env.set", "script.run", "coolify.app.deploy", "coolify.app.restart"}:
        return None
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest["version"] != 1 or not isinstance(manifest.get("revision"), str)
            or not SHA_RE.fullmatch(manifest["revision"])):
        raise Refusal("write_manifest_required", "a versioned host-owned write allow-list is required")
    if verb == "coolify.env.set":
        apps = manifest.get("apps")
        entry = apps.get(args["app"]) if isinstance(apps, dict) else None
        if not isinstance(entry, dict):
            raise Refusal("env_app_not_allowlisted", "app is absent from the versioned allow-list")
        keys = args["keys"].split(",")
        allowed = entry.get("keys")
        if (not isinstance(allowed, list) or not all(isinstance(key, str) for key in allowed)
                or len(keys) != len(set(keys)) or not set(keys).issubset(allowed)):
            raise Refusal("env_key_not_allowlisted", "every distinct key must be allow-listed for this app")
        mapping = entry.get("mapping")
        if (not isinstance(mapping, dict) or set(mapping) != {"name", "sha"}
                or not isinstance(mapping["name"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", mapping["name"])
                or not isinstance(mapping["sha"], str) or not SHA_RE.fullmatch(mapping["sha"])):
            raise Refusal("env_mapping_required", "approved values require a pinned server-side mapping reference")
    elif verb == "coolify.app.deploy":
        entry = _require_staging_app_entry(manifest, args["app"])
        if args["sha"] not in entry["allowedReleases"]:
            raise Refusal("release_not_allowlisted", "release SHA is not in the catalogue pinned set for this app")
    elif verb == "coolify.app.restart":
        _require_staging_app_entry(manifest, args["app"])
    else:
        scripts = manifest.get("scripts")
        entry = scripts.get(args["script"]) if isinstance(scripts, dict) else None
        if not isinstance(entry, dict) or entry.get("sha") != args["sha"]:
            raise Refusal("script_not_allowlisted", "script must match the exact allow-listed SHA")
        scope = entry.get("writeScope")
        if (not isinstance(scope, list) or not scope or len(scope) > 64
                or any(not isinstance(path, str) or not path.startswith("/") or path == "/"
                       or "\x00" in path or any(part in {"", ".", ".."} for part in path[1:].split("/"))
                       for path in scope)):
            raise Refusal("script_scope_required", "script requires an explicit canonical absolute write-scope")
        inverse = entry.get("inverse")
        if (not isinstance(inverse, dict) or set(inverse) != {"script", "sha", "testEvidenceSha256"}
                or inverse["sha"] != args["sha"] or not isinstance(inverse["script"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", inverse["script"])
                or not isinstance(inverse["testEvidenceSha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", inverse["testEvidenceSha256"])):
            raise Refusal("script_inverse_required", "registration requires a pinned inverse and rollback-test evidence")
        review = entry.get("compositionReviewSha256")
        if not isinstance(review, str) or not re.fullmatch(r"[0-9a-f]{64}", review):
            raise Refusal("script_composition_review_required", "registration requires script/unit composition-review evidence")
    return manifest["revision"]


def writes_gate(config: dict[str, Any], spec: dict[str, Any]) -> None:
    """Refuse write execution until the case_b1f57205 O4 gate is satisfied.

    Both the global config switch and the per-verb spec switch must be true.
    This build ships both false: the dry-run preview path below is the only
    write-verb output. OWNER decision O4 on case_b1f57205 must be recorded before a
    follow-up slice may flip either switch.
    """
    if config.get("writesEnabled") is not True or not spec.get("enabled"):
        raise Refusal(
            "write_execution_disabled",
            "host write execution is disabled: OWNER decision O4 on case_b1f57205 has not been recorded "
            "(writesEnabled=false ships by default; dry-run preview only)",
        )


def validate_write_authorization(request: dict[str, Any], config: dict[str, Any], at: dt.datetime) -> None:
    """Enforce the case_5248bcfa CEO approval authority for both write tiers.

    Shape was already checked in validate_request; this checks the parts that
    depend on config and the clock. agentApprovalAllowed stays False for
    every write verb: approval.approver must be the configured CEO, never the
    requester, so an agent can never approve its own request.
    """
    approval = request["approval"]
    ceo_agent_id = config.get("ceoAgentId")
    if not ceo_agent_id:
        raise Refusal("approval_unconfigured", "no ceoAgentId is configured; this broker cannot authorize any write")
    if approval["approver"] == request["requester"]["agentId"]:
        raise Refusal("approval_invalid", "approval.approver must not be the requester: self-approval is never allowed")
    if approval["approver"] != ceo_agent_id:
        raise Refusal("approval_invalid", "approval.approver does not match the configured ceoAgentId")
    decided_at = parse_iso(approval["decidedAt"], "approval.decidedAt")
    if decided_at > at + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise Refusal("approval_invalid", "approval.decidedAt is in the future")
    if (at - decided_at).total_seconds() > APPROVAL_MAX_AGE_SECONDS:
        raise Refusal("approval_stale", f"approval.decidedAt is older than the {APPROVAL_MAX_AGE_SECONDS}s approval freshness window")


def verify_zero_running_runs_host_side(config: dict[str, Any]) -> bool | None:
    """Host-side confirmation that no Paperclip run is currently executing.

    Caller attestation in drainReceipt is never sufficient on its own -- see
    validate_drain_receipt. This build has no wiring to the Paperclip API and
    always returns None (unknown), which validate_drain_receipt treats as a
    refusal: a paperclip-restart request cannot pass this gate in this build
    regardless of how well-formed or fresh the caller's drainReceipt is. Wiring
    a real host-side query is a separate, reviewed follow-up.
    """
    return None


def validate_drain_receipt(request: dict[str, Any], config: dict[str, Any], at: dt.datetime) -> None:
    receipt = request["drainReceipt"]
    checked_at = parse_iso(receipt["checkedAt"], "drainReceipt.checkedAt")
    if checked_at > at + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise Refusal("invalid_drain_receipt", "drainReceipt.checkedAt is in the future")
    if (at - checked_at).total_seconds() > DRAIN_RECEIPT_MAX_AGE_SECONDS:
        raise Refusal("drain_required", f"drainReceipt.checkedAt is older than the {DRAIN_RECEIPT_MAX_AGE_SECONDS}s drain freshness window")
    if receipt["runningRuns"] != 0 or not receipt["newDispatchStopped"]:
        raise Refusal("drain_required", "drainReceipt must attest newDispatchStopped and runningRuns == 0")
    if verify_zero_running_runs_host_side(config) is not True:
        raise Refusal(
            "drain_verification_unavailable",
            "the broker cannot independently confirm zero running runs; caller attestation alone never authorizes a restart",
        )


def render_preview(request: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "requestId": request["requestId"],
        "canonicalHash": request["canonicalHash"],
        "verb": request["verb"],
        "arguments": request["arguments"],
        "effectClass": "read_only",
        "authorization": "fixed_safe_read",
        "executorRendered": True,
        "summary": spec["summary"],
        "writes": [],
        "credentialsExposedToRequester": False,
    }


def render_write_preview(request: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Render a dry-run preview for a Phase-1 write request.

    Executor-rendered from the fixed verb spec and the validated request only:
    no host state is read, no allow-list is resolved, and nothing is executed.
    The preview names the planned writes so a reviewer can see exactly what a
    future enabled execution would do.
    """
    return {
        "version": VERSION,
        "requestId": request["requestId"],
        "canonicalHash": request["canonicalHash"],
        "verb": request["verb"],
        "arguments": request["arguments"],
        "effectClass": "write_dry_run",
        "authorization": "board_or_human_single_use",
        "executorRendered": True,
        "summary": spec.get("summary", ""),
        "writes": list(spec.get("plannedWrites", [])),
        "preImage": list(spec.get("preImage", [])),
        "postconditions": list(spec.get("postconditions", [])),
        "rollback": spec["rollback"],
        "allowList": dict(spec.get("allowList", {})),
        "dryRun": True,
        "executed": False,
        "credentialsExposedToRequester": False,
    }


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def rename_noreplace(source: Path, destination: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError("atomic no-replace publication requires renameat2")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    if renameat2(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination), rename_noreplace) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def pause_before_publication_for_test() -> None:
    marker = os.environ.get("HOST_OPS_TEST_BEFORE_PUBLISH_MARKER")
    if not marker:
        return
    if os.environ.get("HOST_OPS_TEST_MODE") != "1":
        raise OSError("publication pause seam requires HOST_OPS_TEST_MODE=1")
    marker_path = Path(marker)
    release_path = marker_path.with_name(f"{marker_path.name}.release")
    marker_path.touch()
    deadline = time.monotonic() + 20
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for test publication release")
        time.sleep(0.01)


def atomic_write(path: Path, data: bytes, mode: int = 0o640, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        # Requesters commonly run with umask 0077. The request file must remain
        # readable by the broker's dedicated group, so enforce the reviewed mode
        # rather than letting caller umask create a permanent unreadable entry.
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        pause_before_publication_for_test()
        if exclusive:
            rename_noreplace(Path(temporary), path)
        else:
            os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:100]


def read_bounded_regular(path: Path, maximum: int = 65536, code: str = "invalid_spool_entry") -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        raise Refusal("unsafe_platform", "safe file reads require O_NOFOLLOW support")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Refusal(code, "file could not be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refusal(code, "file must be a regular non-symlink file")
        if info.st_size > maximum:
            raise Refusal("file_too_large" if code != "invalid_spool_entry" else "request_too_large", f"file exceeds the {maximum}-byte limit")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(fd, maximum + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum:
            raise Refusal("file_too_large" if code != "invalid_spool_entry" else "request_too_large", f"file exceeds the {maximum}-byte limit")
        return bytes(payload)
    finally:
        os.close(fd)


class AuditLog:
    def __init__(self, path: Path, checkpoint_path: Path | None = None):
        self.path = path
        self.checkpoint_path = checkpoint_path
        self.sequence, self.previous_hash = self.verify()

    def _write_checkpoint(self, sequence: int, entry_hash: str, pending_record: dict[str, Any] | None = None) -> None:
        if self.checkpoint_path is None:
            return
        checkpoint: dict[str, Any] = {
            "version": VERSION,
            "state": "pending" if pending_record is not None else "committed",
            "sequence": sequence,
            "entryHash": entry_hash,
        }
        if pending_record is not None:
            checkpoint["record"] = pending_record
        atomic_write(self.checkpoint_path, canonical_bytes(checkpoint), mode=0o600)

    def _read_checkpoint(self) -> dict[str, Any] | None:
        if self.checkpoint_path is None or not self.checkpoint_path.exists():
            return None
        try:
            checkpoint = load_json(self.checkpoint_path)
            if not isinstance(checkpoint, dict):
                raise ValueError("checkpoint is not an object")
            state = checkpoint.get("state")
            expected = {"version", "state", "sequence", "entryHash"}
            if state == "pending":
                expected.add("record")
            if set(checkpoint) != expected or state not in {"committed", "pending"}:
                raise ValueError("invalid keys or state")
            sequence = checkpoint["sequence"]
            entry_hash = checkpoint["entryHash"]
            if checkpoint["version"] != VERSION or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError("invalid version or sequence")
            if not isinstance(entry_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", entry_hash):
                raise ValueError("invalid entry hash")
            if sequence == 0 and entry_hash != "0" * 64:
                raise ValueError("invalid empty-chain hash")
            if state == "pending":
                record = checkpoint["record"]
                if not isinstance(record, dict) or set(record) != {"version", "sequence", "at", "event", "previousHash", "details", "entryHash"}:
                    raise ValueError("invalid pending record")
                unsigned = {key: value for key, value in record.items() if key != "entryHash"}
                if (
                    record["version"] != VERSION
                    or record["sequence"] != sequence + 1
                    or record["previousHash"] != entry_hash
                    or record["entryHash"] != sha256_bytes(canonical_bytes(unsigned))
                ):
                    raise ValueError("pending record does not extend checkpoint")
            return checkpoint
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise Refusal("audit_corrupt", "audit chain-head checkpoint is invalid") from exc

    def _append_encoded(self, encoded: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o620)
        try:
            written = 0
            while written < len(encoded):
                written += os.write(fd, encoded[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            fsync_directory(self.path.parent)

    def _read_chain(self) -> tuple[int, str, dict[str, Any] | None]:
        previous = "0" * 64
        sequence = 0
        last_record = None
        if self.path.exists():
            with self.path.open("rb") as handle:
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        raise Refusal("audit_corrupt", "audit log ends in a partial record")
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise Refusal("audit_corrupt", "audit log contains invalid JSON") from exc
                    entry_hash = record.get("entryHash")
                    unsigned = {key: value for key, value in record.items() if key != "entryHash"}
                    if record.get("sequence") != sequence + 1 or record.get("previousHash") != previous:
                        raise Refusal("audit_corrupt", "audit sequence or previous hash is invalid")
                    if entry_hash != sha256_bytes(canonical_bytes(unsigned)):
                        raise Refusal("audit_corrupt", "audit entry hash is invalid")
                    sequence += 1
                    previous = entry_hash
                    last_record = record
        return sequence, previous, last_record

    def verify(self) -> tuple[int, str]:
        checkpoint = self._read_checkpoint()
        sequence, previous, last_record = self._read_chain()
        if checkpoint is None:
            if sequence != 0:
                raise Refusal("audit_corrupt", "audit checkpoint is missing for a non-empty audit log")
            self._write_checkpoint(0, "0" * 64)
            return 0, "0" * 64

        base = (checkpoint["sequence"], checkpoint["entryHash"])
        if checkpoint["state"] == "committed":
            if (sequence, previous) != base:
                raise Refusal("audit_corrupt", "audit log and committed chain-head checkpoint disagree")
            return sequence, previous

        pending = checkpoint["record"]
        pending_head = (pending["sequence"], pending["entryHash"])
        if (sequence, previous) == base:
            self._append_encoded(canonical_bytes(pending))
            sequence, previous, last_record = pending["sequence"], pending["entryHash"], pending
        elif (sequence, previous) != pending_head or last_record != pending:
            raise Refusal("audit_corrupt", "audit log does not match the pending checkpoint transaction")
        self._write_checkpoint(sequence, previous)
        return sequence, previous

    def append(self, event: str, details: dict[str, Any]) -> dict[str, Any]:
        checkpoint = self._read_checkpoint()
        if checkpoint is not None and checkpoint["state"] == "pending":
            self.sequence, self.previous_hash = self.verify()
        record = {
            "version": VERSION,
            "sequence": self.sequence + 1,
            "at": iso(now_utc()),
            "event": event,
            "previousHash": self.previous_hash,
            "details": details,
        }
        record["entryHash"] = sha256_bytes(canonical_bytes(record))
        self._write_checkpoint(self.sequence, self.previous_hash, record)
        self._append_encoded(canonical_bytes(record))
        self.sequence = record["sequence"]
        self.previous_hash = record["entryHash"]
        self._write_checkpoint(self.sequence, self.previous_hash)
        return record


class ReplayStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS used_requests "
            "(request_id TEXT PRIMARY KEY, canonical_hash TEXT NOT NULL, claim_id TEXT, accepted_at TEXT NOT NULL, final_status TEXT)"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(used_requests)")}
        if "claim_id" not in columns:
            self.db.execute("ALTER TABLE used_requests ADD COLUMN claim_id TEXT")
        self.db.commit()

    def reserve(self, request_id: str, canonical_hash: str, claim_id: str) -> str:
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO used_requests(request_id, canonical_hash, claim_id, accepted_at) VALUES(?,?,?,?)",
                    (request_id, canonical_hash, claim_id, iso(now_utc())),
                )
            return "new"
        except sqlite3.IntegrityError:
            row = self.db.execute(
                "SELECT canonical_hash, claim_id, final_status FROM used_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row[0] != canonical_hash:
                return "replay"
            if row[1] == claim_id:
                return "complete" if row[2] is not None else "resume"
            if row[2] is None and row[1] is None:
                with self.db:
                    updated = self.db.execute(
                        "UPDATE used_requests SET claim_id=? WHERE request_id=? AND claim_id IS NULL AND final_status IS NULL",
                        (claim_id, request_id),
                    ).rowcount
                if updated == 1:
                    return "resume"
            return "replay"

    def finish(self, request_id: str, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE used_requests SET final_status=? WHERE request_id=?", (status, request_id))


def run_fixed(command: list[str], timeout: int = 20) -> str:
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        stderr = completed.stderr.strip().replace("\n", " ")[:500]
        raise Refusal("host_read_failed", f"fixed host read failed with exit {completed.returncode}: {stderr}")
    return completed.stdout


def fake_fact(config: dict[str, Any], key: str) -> Any | None:
    directory = os.environ.get("HOST_OPS_FAKE_FACTS_DIR")
    if not directory:
        return None
    if os.environ.get("HOST_OPS_TEST_MODE") != "1" or not config.get("testMode", False):
        raise Refusal("test_seam_refused", "fake facts require HOST_OPS_TEST_MODE=1 and config testMode=true")
    return load_json(Path(directory) / f"{key}.json")


DOCKER_LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (?P<message>.*)$",
)


def parse_docker_log_line(line: str) -> tuple[str, int] | None:
    """Return (epoch-microseconds, LOG_PRIORITY) for one `docker logs --timestamps` line.

    Docker emits "<RFC3339Nano UTC> <message>" per line -- up to 9 fractional
    digits, which strptime's %f cannot parse, so the fraction is truncated to
    microseconds with exact integer math (no float timestamp). The driver
    records no per-line severity, so every surviving entry reports the fixed
    informational priority. Returns None for lines that do not carry a Docker
    timestamp (partial writes, driver framing), which the caller drops.
    Message text never leaves this function except into the redacted marker.

    Timestamp contract: https://docs.docker.com/reference/cli/docker/container/logs/
    """
    match = DOCKER_LOG_LINE_RE.match(line)
    if match is None:
        return None
    parts = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", match.group("timestamp"))
    if parts is None:
        return None
    try:
        moment = dt.datetime.strptime(parts.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    fraction = parts.group(2) or ""
    micros = int(fraction[:6].ljust(6, "0"))
    epoch_micros = str(int(moment.replace(tzinfo=dt.timezone.utc).timestamp()) * 1_000_000 + micros)
    return epoch_micros, LOG_PRIORITY


def read_log_bytes(timeout: float = 20) -> bytes:
    """Bound raw stdout in memory; never persist container log text or stderr.

    `docker logs --timestamps` framing: https://docs.docker.com/reference/cli/docker/container/logs/
    """
    deadline = time.monotonic() + timeout
    with subprocess.Popen(LOG_COMMAND, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          start_new_session=True) as process, selectors.DefaultSelector() as selector:
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        output = bytearray()
        completed = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise Refusal("logs_timeout", "fixed container log read timed out")
                chunk = os.read(process.stdout.fileno(), min(8192, LOG_RAW_BYTE_LIMIT + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > LOG_RAW_BYTE_LIMIT:
                    raise Refusal("logs_too_large", "fixed container log read exceeds the raw byte cap")
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise Refusal("logs_timeout", "fixed container log read timed out") from exc
            completed = True
            if code != 0:
                raise Refusal("host_read_failed", "fixed container log read failed; raw diagnostics withheld")
            return bytes(output)
        finally:
            if not completed:
                # Also stop descendants if stdout stayed open after sudo exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def execute_logs(config: dict[str, Any]) -> dict[str, Any]:
    # The fake-fact seam carries RAW `docker logs --timestamps` lines, not
    # redacted entries, so offline tests exercise the same parse-and-redact
    # path as the live command. A fixture that could smuggle pre-redacted
    # text past the parser would test a different code path from production.
    fake = fake_fact(config, "logs")
    if fake is None:
        raw = read_log_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refusal("invalid_logs", "fixed container log read did not return text") from exc
        lines: list[str] = text.splitlines()
    else:
        if not isinstance(fake, list) or not all(isinstance(line, str) for line in fake):
            raise Refusal("invalid_logs", "container log test fixture must be a list of raw log lines")
        lines = fake
    if len(lines) > LOG_LINE_LIMIT:
        raise Refusal("invalid_logs", "container log tail exceeds the entry cap")
    redacted = []
    for line in lines:
        if not line.strip():
            continue
        parsed = parse_docker_log_line(line)
        if parsed is None:
            continue
        timestamp, priority = parsed
        # Regex secret lists cannot cover arbitrary credentials, multiline keys
        # or unlabelled values. Fail closed: export only the parsed timestamp
        # and a fixed marker. Message text never leaves the parser.
        redacted.append({"timestamp": timestamp, "priority": priority, "message": "[REDACTED]"})
    if not redacted and any(line.strip() for line in lines):
        raise Refusal("invalid_logs", "fixed container log read returned no timestamped lines")
    if len(redacted) > LOG_LINE_LIMIT:
        raise Refusal("invalid_logs", "container log tail exceeds the entry cap")
    return {"unit": LOG_UNIT, "windowSeconds": LOG_WINDOW_SECONDS, "lineLimit": LOG_LINE_LIMIT,
            "redaction": "all-free-text", "entries": redacted}


def execute_health(config: dict[str, Any]) -> dict[str, Any]:
    fake = fake_fact(config, "health")
    if fake is not None:
        payload = fake
    else:
        url = config.get("healthUrl")
        if url != "http://127.0.0.1:3100/api/health":
            raise Refusal("unsafe_config", "healthUrl must be the fixed loopback Paperclip health URL")
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise Refusal("health_unavailable", f"fixed health endpoint did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise Refusal("invalid_health", "health response must be an object")
    allowed = ("status", "commit", "version", "uptime")
    return {key: payload[key] for key in allowed if key in payload}


def execute_service(config: dict[str, Any]) -> dict[str, Any]:
    """Read the fixed paperclip container's .State through SERVICE_COMMAND.

    Verified topology (host operator, 2026-10-05): node7b1ff338 runs no
    paperclip.service systemd unit anywhere, so the old `--user show` path is
    dead here. The Compose project "paperclip", service "server", container
    "paperclip" is the runtime; `docker inspect --format '{{json .State}}'`
    is the state source. `_` is unused: config is consulted only for the fake
    seam, never for argv (the command is entirely fixed).
    """
    fake = fake_fact(config, "service")
    if fake is not None:
        payload = fake
    else:
        raw = run_fixed(list(SERVICE_COMMAND))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Refusal("invalid_service", "fixed container inspect did not return JSON") from exc
    if not isinstance(payload, dict):
        raise Refusal("invalid_service", "fixed container inspect must return an object")
    result: dict[str, Any] = {}
    for key in ("Status", "Running", "Pid", "StartedAt", "FinishedAt", "ExitCode", "OOMKilled", "Restarting"):
        if key in payload:
            result[key] = payload[key]
    if (not isinstance(result.get("Status"), str) or not result["Status"]
            or not isinstance(result.get("Pid"), int)
            or not isinstance(result.get("Running"), bool)):
        raise Refusal("invalid_service", "container state is missing Status/Pid/Running")
    return result


def execute_image(config: dict[str, Any]) -> dict[str, Any]:
    fake = fake_fact(config, "image")
    if fake is not None:
        payload = fake
    else:
        image_ref = config.get("paperclipImageRef")
        if not isinstance(image_ref, str) or not IMAGE_REF_RE.fullmatch(image_ref):
            raise Refusal("unsafe_config", "paperclipImageRef is invalid")
        raw = run_fixed([
            "/usr/bin/sudo", "-n", "-u", "root",
            "/usr/bin/docker", "image", "inspect", image_ref, "--format", "{{json .}}",
        ])
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Refusal("invalid_image", "fixed image inspect did not return JSON") from exc
    if not isinstance(payload, dict):
        raise Refusal("invalid_image", "fixed image inspect must return an object")
    return {key: payload.get(key) for key in ("Id", "Digest", "RepoTags", "Created", "Size")}


def execute_host(config: dict[str, Any]) -> dict[str, Any]:
    fake = fake_fact(config, "host")
    if fake is not None:
        return fake
    usage = shutil.disk_usage(config["paperclipDataPath"])
    uptime = None
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return {
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "uptimeSeconds": uptime,
        "loadAverage": list(os.getloadavg()),
        "paperclipFilesystem": {"total": usage.total, "used": usage.used, "free": usage.free},
    }


def validate_absolute_config_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise Refusal("unsafe_config", f"{field} must be an absolute host-owned path")
    return Path(value)


def execute_locate(config: dict[str, Any]) -> dict[str, Any]:
    fake = fake_fact(config, "locate")
    if fake is not None:
        return fake
    roots = config.get("toolingSearchRoots")
    markers = config.get("toolingMarkers")
    if not isinstance(roots, list) or not (1 <= len(roots) <= 8):
        raise Refusal("unsafe_config", "toolingSearchRoots must contain 1-8 fixed paths")
    if not isinstance(markers, list) or not (2 <= len(markers) <= 16) or not all(re.fullmatch(r"[A-Za-z0-9._-]+", str(v)) for v in markers):
        raise Refusal("unsafe_config", "toolingMarkers must contain safe basenames")
    found: list[dict[str, Any]] = []
    for raw_root in roots:
        root = validate_absolute_config_path(raw_root, "toolingSearchRoots")
        if not root.is_dir():
            continue
        for current, directories, files in os.walk(root):
            relative_depth = len(Path(current).relative_to(root).parts)
            if relative_depth >= 5:
                directories[:] = []
            directories[:] = [name for name in directories if name not in {".git", "node_modules", "proc", "sys", "dev"}]
            matches = sorted(set(files).intersection(markers))
            if len(matches) >= 2:
                found.append({"path": current, "markers": matches})
            if len(found) >= 50:
                return {"candidates": found, "truncated": True}
    return {"candidates": found, "truncated": False}


def execute_fingerprint(config: dict[str, Any]) -> dict[str, Any]:
    fake = fake_fact(config, "fingerprint")
    if fake is not None:
        return fake
    root = validate_absolute_config_path(config.get("opsToolingPath"), "opsToolingPath")
    if not root.is_dir():
        raise Refusal("tooling_not_found", "configured opsToolingPath is not a directory")
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if len(path.relative_to(root).parts) > 3:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        mode = path.stat().st_mode
        if not (mode & stat.S_IXUSR) and path.suffix not in {".py", ".js", ".mjs"}:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append({"path": str(path.relative_to(root)), "sha256": digest, "size": path.stat().st_size})
    if not records:
        raise Refusal("fingerprint_empty", "configured opsToolingPath contained no fingerprintable files")
    return {"root": str(root), "files": records}


def execute_stat_chain(config: dict[str, Any]) -> dict[str, Any]:
    fake = fake_fact(config, "stat_chain")
    if fake is not None:
        return fake
    path = validate_absolute_config_path(config.get("paperclipDataPath"), "paperclipDataPath")
    chain: list[dict[str, Any]] = []
    current = path
    while True:
        try:
            info = current.lstat()
            chain.append({
                "path": str(current),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                "type": "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "file",
            })
        except FileNotFoundError:
            chain.append({"path": str(current), "missing": True})
        if current == current.parent:
            break
        current = current.parent
    return {"chain": chain}


EXECUTORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "health": execute_health,
    "logs": execute_logs,
    "service": execute_service,
    "image": execute_image,
    "host": execute_host,
    "locate": execute_locate,
    "fingerprint": execute_fingerprint,
    "stat_chain": execute_stat_chain,
}


def require_keys(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise Refusal(code, f"result keys must be exactly {sorted(keys)}")
    return value


def validate_result(verb: str, value: Any) -> dict[str, Any]:
    if verb == "health.inspect":
        if not isinstance(value, dict):
            raise Refusal("invalid_health", "health result must be an object")
        required = {"status", "commit"}
        optional = {"version", "uptime"}
        if not required.issubset(value) or not set(value).issubset(required | optional):
            raise Refusal("invalid_health", "health result requires status/commit and only supports version/uptime extras")
        result = value
        if not isinstance(result["status"], str) or not SHA_RE.fullmatch(result["commit"] or ""):
            raise Refusal("invalid_health", "health result requires string status and 40-hex commit")
        if "version" in result and (not isinstance(result["version"], str) or not result["version"]):
            raise Refusal("invalid_health", "health version must be a non-empty string")
        if "uptime" in result and (
            isinstance(result["uptime"], bool)
            or not isinstance(result["uptime"], (int, float))
            or result["uptime"] < 0
        ):
            raise Refusal("invalid_health", "health uptime must be a non-negative number")
    elif verb == "logs.read":
        result = require_keys(value, {"unit", "windowSeconds", "lineLimit", "redaction", "entries"}, "invalid_logs")
        if (result["unit"] != LOG_UNIT or type(result["windowSeconds"]) is not int
                or result["windowSeconds"] != LOG_WINDOW_SECONDS or type(result["lineLimit"]) is not int
                or result["lineLimit"] != LOG_LINE_LIMIT or result["redaction"] != "all-free-text"
                or not isinstance(result["entries"], list) or len(result["entries"]) > LOG_LINE_LIMIT):
            raise Refusal("invalid_logs", "log result must retain fixed scope, bounds and redaction policy")
        for entry in result["entries"]:
            require_keys(entry, {"timestamp", "priority", "message"}, "invalid_logs")
            if (not isinstance(entry["timestamp"], str) or not re.fullmatch(r"[0-9]{1,20}", entry["timestamp"])
                    or type(entry["priority"]) is not int or not 0 <= entry["priority"] <= 7
                    or entry["message"] != "[REDACTED]"):
                raise Refusal("invalid_logs", "log result contains invalid metadata or unredacted text")
        if len(canonical_bytes(result)) > LOG_RESPONSE_BYTE_LIMIT:
            raise Refusal("logs_too_large", "redacted log result exceeds the byte cap")
    elif verb == "service.inspect":
        # node7b1ff338 Docker shape (verified 2026-10-05): the fixed subset of
        # `docker inspect --format '{{json .State}}'` the executor exports.
        # Required keys prove the state object is complete; optional keys are
        # passed through only when the executor supplied them.
        if not isinstance(value, dict) or not {"Status", "Running", "Pid"}.issubset(value):
            raise Refusal("invalid_service", "service result requires Status/Running/Pid")
        allowed_service_keys = {
            "Status", "Running", "Pid", "StartedAt", "FinishedAt", "ExitCode", "OOMKilled", "Restarting",
        }
        if not set(value).issubset(allowed_service_keys):
            raise Refusal("invalid_service", "service result carries unexpected fields")
        result = value
        if not isinstance(result["Status"], str) or not result["Status"]:
            raise Refusal("invalid_service", "service Status must be a non-empty string")
        if not isinstance(result["Running"], bool) or isinstance(result["Pid"], bool) or not isinstance(result["Pid"], int):
            raise Refusal("invalid_service", "service Running must be boolean and Pid an integer")
        for key in ("StartedAt", "FinishedAt"):
            if key in result and not isinstance(result[key], str):
                raise Refusal("invalid_service", "service timestamps must be strings")
        if "ExitCode" in result and (isinstance(result["ExitCode"], bool) or not isinstance(result["ExitCode"], int)):
            raise Refusal("invalid_service", "service ExitCode must be an integer")
        for key in ("OOMKilled", "Restarting"):
            if key in result and not isinstance(result[key], bool):
                raise Refusal("invalid_service", "service OOMKilled/Restarting must be booleans")
    elif verb == "image.inspect":
        result = require_keys(value, {"Id", "Digest", "RepoTags", "Created", "Size"}, "invalid_image")
        if not isinstance(result["Id"], str) or not result["Id"]:
            raise Refusal("invalid_image", "image Id must be a non-empty string")
        if result["Digest"] is not None and not isinstance(result["Digest"], str):
            raise Refusal("invalid_image", "image Digest must be a string or null")
        if not isinstance(result["RepoTags"], list) or not all(isinstance(tag, str) for tag in result["RepoTags"]):
            raise Refusal("invalid_image", "image RepoTags must be a string array")
        if result["Created"] is not None and not isinstance(result["Created"], str):
            raise Refusal("invalid_image", "image Created must be a string or null")
        if isinstance(result["Size"], bool) or result["Size"] is not None and not isinstance(result["Size"], int):
            raise Refusal("invalid_image", "image Size must be an integer or null")
    elif verb == "host.inspect":
        result = require_keys(value, {"hostname", "kernel", "uptimeSeconds", "loadAverage", "paperclipFilesystem"}, "invalid_host")
        filesystem = result["paperclipFilesystem"]
        if not isinstance(result["hostname"], str) or not result["hostname"] or not isinstance(result["kernel"], str) or not result["kernel"]:
            raise Refusal("invalid_host", "host identity fields must be non-empty strings")
        if not isinstance(result["uptimeSeconds"], (int, float)) or isinstance(result["uptimeSeconds"], bool):
            raise Refusal("invalid_host", "host uptime must be numeric")
        if not isinstance(result["loadAverage"], list) or len(result["loadAverage"]) != 3 or not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in result["loadAverage"]):
            raise Refusal("invalid_host", "host loadAverage must contain three numbers")
        if not isinstance(filesystem, dict) or set(filesystem) != {"total", "used", "free"} or not all(isinstance(filesystem[key], int) and not isinstance(filesystem[key], bool) for key in filesystem):
            raise Refusal("invalid_host", "paperclipFilesystem must contain integer total, used, and free")
    elif verb == "tooling.locate":
        result = require_keys(value, {"candidates", "truncated"}, "invalid_locate")
        if not isinstance(result["candidates"], list) or not isinstance(result["truncated"], bool):
            raise Refusal("invalid_locate", "locate result requires candidates array and boolean truncated")
        for candidate in result["candidates"]:
            if not isinstance(candidate, dict) or set(candidate) != {"path", "markers"} or not isinstance(candidate["path"], str) or not isinstance(candidate["markers"], list) or not all(isinstance(marker, str) for marker in candidate["markers"]):
                raise Refusal("invalid_locate", "locate candidates require path and string marker array")
    elif verb == "tooling.fingerprint":
        result = require_keys(value, {"root", "files"}, "invalid_fingerprint")
        if not isinstance(result["root"], str) or not result["root"] or not isinstance(result["files"], list) or not result["files"]:
            raise Refusal("invalid_fingerprint", "fingerprint result requires root and non-empty files")
        for record in result["files"]:
            if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"} or not isinstance(record["path"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"] or "") or isinstance(record["size"], bool) or not isinstance(record["size"], int):
                raise Refusal("invalid_fingerprint", "fingerprint files require path, sha256, and integer size")
    elif verb == "paperclip.stat-chain":
        result = require_keys(value, {"chain"}, "invalid_stat_chain")
        if not isinstance(result["chain"], list) or not result["chain"]:
            raise Refusal("invalid_stat_chain", "stat-chain result requires a non-empty chain")
        for record in result["chain"]:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                raise Refusal("invalid_stat_chain", "stat-chain rows require a path")
            if set(record) == {"path", "missing"}:
                if record["missing"] is not True:
                    raise Refusal("invalid_stat_chain", "missing stat-chain rows must be true")
            elif set(record) == {"path", "uid", "gid", "mode", "type"}:
                if isinstance(record["uid"], bool) or not isinstance(record["uid"], int) or isinstance(record["gid"], bool) or not isinstance(record["gid"], int) or not re.fullmatch(r"[0-7]{4}", record["mode"] or "") or record["type"] not in {"symlink", "directory", "file"}:
                    raise Refusal("invalid_stat_chain", "stat-chain identity row is invalid")
            else:
                raise Refusal("invalid_stat_chain", "stat-chain row keys are invalid")
    else:
        raise Refusal("invalid_result", "no result schema exists for verb")
    return result


def sign_file(private_key: Path, path: Path, output: Path) -> None:
    completed = subprocess.run(
        ["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(output), str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise Refusal("signing_failed", completed.stderr.strip()[:500])
    os.chmod(output, 0o640)
    signature_fd = os.open(output, os.O_RDONLY)
    try:
        os.fsync(signature_fd)
    finally:
        os.close(signature_fd)


def validate_signing_key(private_key: Path) -> bytes:
    if private_key.is_symlink():
        raise Refusal("invalid_signing_key", "configured signing key must not be a symlink")
    try:
        info = private_key.stat()
    except OSError as exc:
        raise Refusal("invalid_signing_key", "configured signing key is not readable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Refusal("invalid_signing_key", "configured signing key must be a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise Refusal("invalid_signing_key", "configured signing key must not grant group or other access")
    rsa = subprocess.run(
        ["/usr/bin/openssl", "rsa", "-in", str(private_key), "-check", "-noout"],
        check=False,
        capture_output=True,
        timeout=20,
    )
    if rsa.returncode != 0:
        raise Refusal("invalid_signing_key", "configured signing key must be a valid RSA private key")
    description = subprocess.run(
        ["/usr/bin/openssl", "rsa", "-in", str(private_key), "-text", "-noout"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    first_line = description.stdout.splitlines()[0] if description.stdout else ""
    match = re.fullmatch(r"Private-Key: \(([0-9]+) bit, [^)]+\)", first_line)
    if description.returncode != 0 or match is None:
        raise Refusal("invalid_signing_key", "configured signing key size could not be read")
    if int(match.group(1)) < 2048:
        raise Refusal("invalid_signing_key", "configured RSA signing key must be at least 2048 bits")
    public = subprocess.run(
        ["/usr/bin/openssl", "pkey", "-in", str(private_key), "-pubout"],
        check=False,
        capture_output=True,
        timeout=20,
    )
    if public.returncode != 0:
        raise Refusal("invalid_signing_key", public.stderr.decode(errors="replace").strip()[:500])
    with tempfile.TemporaryDirectory(prefix="host-ops-signing-check.") as directory:
        root = Path(directory)
        payload = root / "payload"
        signature = root / "signature"
        payload.write_bytes(b"paperclip-host-ops-signing-check\n")
        try:
            sign_file(private_key, payload, signature)
        except Refusal as exc:
            raise Refusal("invalid_signing_key", "configured signing key cannot use the RSA/SHA-256 signature contract") from exc
        if not verify_captured_signature(public.stdout, payload.read_bytes(), signature.read_bytes()):
            raise Refusal("invalid_signing_key", "configured signing key could not produce a verifiable RSA/SHA-256 signature")
    return public.stdout


def verify_captured_signature(public_key: bytes, response: bytes, signature: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="host-ops-verify.") as directory:
        root = Path(directory)
        public_key_path = root / "public.pem"
        response_path = root / "response.json"
        signature_path = root / "response.sig"
        public_key_path.write_bytes(public_key)
        response_path.write_bytes(response)
        signature_path.write_bytes(signature)
        completed = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(public_key_path), "-signature", str(signature_path), str(response_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return completed.returncode == 0


def read_existing_signed_payload(directory: Path, basename: str, private_key: Path) -> dict[str, Any] | None:
    bundle = directory / f"{basename}.bundle"
    if not bundle.exists():
        return None
    response_path = bundle / f"{basename}.json"
    signature_path = bundle / f"{basename}.sig"
    response = read_bounded_regular(response_path, maximum=1048576, code="invalid_response")
    signature = read_bounded_regular(signature_path, maximum=16384, code="invalid_signature")
    public = subprocess.run(
        ["/usr/bin/openssl", "pkey", "-in", str(private_key), "-pubout"],
        check=False,
        capture_output=True,
        timeout=20,
    )
    if public.returncode != 0 or not verify_captured_signature(public.stdout, response, signature):
        raise FatalStateError("existing signed bundle failed host-key verification")
    try:
        payload = json.loads(response)
    except json.JSONDecodeError as exc:
        raise FatalStateError("existing signed bundle contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FatalStateError("existing signed bundle payload is not an object")
    return payload


def publish_directory_noreplace(staging: Path, bundle: Path, published_mode: int) -> None:
    source_parent_fd = os.open(staging.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    destination_parent_fd = os.open(bundle.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    staging_fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fchmod(staging_fd, published_mode)
        os.fsync(staging_fd)
        rename_noreplace(staging, bundle)
        os.fsync(source_parent_fd)
        os.fsync(destination_parent_fd)
    finally:
        os.close(staging_fd)
        os.close(destination_parent_fd)
        os.close(source_parent_fd)


def persist_signed(directory: Path, basename: str, payload: dict[str, Any], private_key: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    staging_root = directory.parent / ".staging"
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging_root.chmod(0o700)
    bundle = directory / f"{basename}.bundle"
    staging = Path(tempfile.mkdtemp(prefix=f"{directory.name}.{basename}.", dir=staging_root))
    staged_json = staging / f"{basename}.json"
    staged_signature = staging / f"{basename}.sig"
    try:
        atomic_write(staged_json, canonical_bytes(payload))
        sign_file(private_key, staged_json, staged_signature)
        fsync_directory(staging)
        publish_directory_noreplace(staging, bundle, 0o750)
        return bundle / staged_json.name, bundle / staged_signature.name
    finally:
        if staging.exists():
            staged_signature.unlink(missing_ok=True)
            staged_json.unlink(missing_ok=True)
            staging.rmdir()
            fsync_directory(staging_root)


DEPLOYED_SUDOERS_MIRROR_PATH = "/etc/paperclip-host-ops/sudoers.rendered"
# All three node7b1ff338 Docker grants are exact-argv and root-bound. Allowed forms:
#   /usr/bin/docker logs paperclip --tail 200 --since 15m --timestamps
#   /usr/bin/docker inspect paperclip --format {{json .State}}
#   /usr/bin/docker image inspect <ref> --format {{json .}}   (rendered by install.sh)
SUDOERS_LOGS_RE = re.compile(
    r"^Cmnd_Alias PAPERCLIP_LOGS_READ = \\\n"
    r"[ \t]*/usr/bin/docker logs paperclip --tail 200 --since 15m --timestamps[ \t]*$",
    re.MULTILINE,
)
SUDOERS_SERVICE_RE = re.compile(
    r"^Cmnd_Alias PAPERCLIP_SERVICE_READ = \\\n"
    r"[ \t]*/usr/bin/docker inspect paperclip --format \{\{json \.State\}\}[ \t]*$",
    re.MULTILINE,
)
SUDOERS_ROOT_BINDING_RES = (
    re.compile(r"^paperclip-host-reader ALL=\(root\) NOPASSWD: PAPERCLIP_LOGS_READ$", re.MULTILINE),
    re.compile(r"^paperclip-host-reader ALL=\(root\) NOPASSWD: PAPERCLIP_SERVICE_READ$", re.MULTILINE),
    re.compile(r"^paperclip-host-reader ALL=\(root\) NOPASSWD: PAPERCLIP_IMAGE_READ$", re.MULTILINE),
)
SUDOERS_IMAGE_RE = re.compile(
    r"^Cmnd_Alias PAPERCLIP_IMAGE_READ = \\\n[ \t]*/usr/bin/docker image inspect (\S+) --format \{\{json \.\}\}[ \t]*$",
    re.MULTILINE,
)
SUDOERS_IMAGE_BINDING_RE = SUDOERS_ROOT_BINDING_RES[2]


def sudoers_grants_fixed_docker_reads(text: str) -> None:
    """Refuse unless the deployed sudoers grants exactly the three fixed node7b1ff338
    Docker reads, each once, each root-bound. A missing grant means the
    deployed authority does not match the shipped contract; a duplicate or an
    extra sudo-backed docker/systemctl/journalctl line means the broker could
    execute a command this build never reviewed."""
    for pattern, label in (
        (SUDOERS_LOGS_RE, "docker logs paperclip"),
        (SUDOERS_SERVICE_RE, "docker inspect paperclip"),
    ):
        if len(pattern.findall(text)) != 1:
            raise Refusal("unsafe_config", f"deployed sudoers must grant exactly one {label} command")
    for binding in SUDOERS_ROOT_BINDING_RES:
        if len(binding.findall(text)) != 1:
            raise Refusal("unsafe_config", "each fixed Docker read must have exactly one root run-as binding")


def sudoers_granted_image_ref(text: str) -> str:
    """Return the single image reference sudo will actually authorize.

    Exactly one grant may exist. Zero means the deployed sudoers file does not
    match the shipped contract; more than one would mean the broker could pick
    which image it attests, which is the property this pin exists to deny.

    The grant is returned as the argv token sudo compares against, not as the
    file's bytes: install.sh escapes each colon as "\\:" because a bare colon is
    the sudoers run-as separator, and sudo strips that escape before matching.
    Comparing the raw bytes to config.json would refuse every tag and digest
    reference -- i.e. everything but a bare repository name.
    """
    matches = SUDOERS_IMAGE_RE.findall(text)
    if len(matches) != 1:
        raise Refusal(
            "unsafe_config",
            f"deployed sudoers must grant exactly one docker image inspect command, found {len(matches)}",
        )
    if len(SUDOERS_IMAGE_BINDING_RE.findall(text)) != 1:
        raise Refusal("unsafe_config", "docker image inspect must have exactly one root run-as binding")
    return matches[0].replace("\\:", ":")


def require_image_ref_matches_sudoers(image_ref: str) -> None:
    try:
        text = Path(DEPLOYED_SUDOERS_MIRROR_PATH).read_text()
    except OSError as exc:
        raise Refusal("unsafe_config", f"deployed sudoers mirror is unreadable: {exc}") from exc
    granted = sudoers_granted_image_ref(text)
    if granted != image_ref:
        raise Refusal(
            "unsafe_config",
            f"paperclipImageRef {image_ref!r} is not the image the deployed sudoers grants ({granted!r}); "
            "re-run install.sh with --paperclip-image-ref to move both together",
        )


def load_config(path: Path) -> dict[str, Any]:
    config = load_json(path)
    # node7b1ff338 Docker port (case_81816394): paperclipHostUser/paperclipHostUid are
    # retired. The old Ubuntu user-systemd target does not exist on node7b1ff338, and
    # the three Docker grants are container-fixed, not user-targeted. A config
    # that still names them is stale, not forward-compatible: refuse, and
    # re-run install.sh, rather than silently keeping keys nothing reads.
    for retired in ("paperclipHostUser", "paperclipHostUid"):
        if retired in config:
            raise Refusal("unsafe_config", f"{retired} was retired by the node7b1ff338 Docker port; remove it from config.json")
    required = {
        "version", "spoolPath", "statePath", "auditPath", "privateKeyPath", "healthUrl",
        "paperclipDataPath", "paperclipImageRef",
        "opsToolingPath", "toolingSearchRoots", "toolingMarkers", "pollSeconds", "testMode",
    }
    # writesEnabled is optional for backward compatibility with deployed
    # configs written before case_b1f57205; absent means disabled. It must still be
    # an explicit boolean when present so a typo cannot enable writes.
    allowed = required | {"writesEnabled", "writeAllowList", "ceoAgentId"}
    if not isinstance(config, dict) or not required.issubset(config) or not set(config).issubset(allowed):
        raise Refusal("invalid_shape", f"config keys must be {sorted(required)} plus optionally writesEnabled/writeAllowList/ceoAgentId; got {sorted(config) if isinstance(config, dict) else config!r}")
    if config["version"] != VERSION:
        raise Refusal("unsafe_config", f"config version must be {VERSION}")
    writes_enabled = config.get("writesEnabled", False)
    if not isinstance(writes_enabled, bool):
        raise Refusal("unsafe_config", "writesEnabled must be boolean")
    config["writesEnabled"] = writes_enabled
    # ceoAgentId is the case_5248bcfa approval authority: absent means this broker
    # cannot authorize any routine-write or paperclip-restart request, which
    # is the safe default. When present it must already be a well-formed
    # agent id -- a typo here must not silently become "no approval required".
    ceo_agent_id = config.get("ceoAgentId")
    if ceo_agent_id is not None and (not isinstance(ceo_agent_id, str) or not UUID_RE.fullmatch(ceo_agent_id)):
        raise Refusal("unsafe_config", "ceoAgentId must be a UUID string when present")
    config["ceoAgentId"] = ceo_agent_id
    for field in ("spoolPath", "statePath", "auditPath", "privateKeyPath", "paperclipDataPath", "opsToolingPath"):
        validate_absolute_config_path(config[field], field)
    if not isinstance(config["pollSeconds"], int) or not (2 <= config["pollSeconds"] <= 300):
        raise Refusal("unsafe_config", "pollSeconds must be between 2 and 300")
    if not isinstance(config["testMode"], bool):
        raise Refusal("unsafe_config", "testMode must be boolean")
    if not isinstance(config["paperclipImageRef"], str) or not IMAGE_REF_RE.fullmatch(config["paperclipImageRef"]):
        raise Refusal("unsafe_config", "paperclipImageRef must be a plain image reference")
    # The image reference is the one config value that is spliced into a
    # sudo-backed argv, so config alone must not decide it. The service user
    # reads an install-time mirror because sudo requires the authority file to
    # remain root:root 0440; the unit's root-only cmp preflight binds that mirror
    # to the real grant before this unprivileged process starts. Refuse drift
    # before an opaque authorization error or an attestation of the wrong image.
    # The two other Docker grants (logs, service) are container-fixed argv with
    # no config-spliced value, so image agreement plus the startup mirror check
    # covers the whole authority file.
    if not config["testMode"]:
        try:
            text = Path(DEPLOYED_SUDOERS_MIRROR_PATH).read_text()
        except OSError as exc:
            raise Refusal("unsafe_config", f"deployed sudoers mirror is unreadable: {exc}") from exc
        sudoers_grants_fixed_docker_reads(text)
        require_image_ref_matches_sudoers(config["paperclipImageRef"])
    if config["privateKeyPath"] != DEPLOYED_PRIVATE_KEY_PATH and not config["testMode"]:
        raise Refusal("unsafe_config", "privateKeyPath must match the independently pinned deployed signing key")
    return config


class Broker:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.spool = Path(config["spoolPath"])
        self.requests = self.spool / "requests"
        self.previews = self.spool / "previews"
        self.responses = self.spool / "responses"
        self.processing = self.spool / "processing"
        self.private_key = Path(config["privateKeyPath"])
        self.state_path = Path(config["statePath"])
        self.lock_path = self.state_path / "broker.lock"
        self.audit: AuditLog | None = None
        self.replay: ReplayStore | None = None

    def _open_state(self) -> None:
        self.audit = AuditLog(Path(self.config["auditPath"]), self.state_path / "audit-head.json")
        self.replay = ReplayStore(self.state_path / "state.sqlite3")

    def _close_state(self) -> None:
        if self.replay is not None:
            self.replay.db.close()
        self.replay = None
        self.audit = None

    def _require_state(self) -> tuple[AuditLog, ReplayStore]:
        if self.audit is None or self.replay is None:
            raise RuntimeError("broker state is only available while the process lock is held")
        return self.audit, self.replay

    @contextmanager
    def _exclusive_run(self) -> Iterator[None]:
        self.state_path.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._open_state()
            yield
        finally:
            self._close_state()
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _response_name(self, request_id: str, canonical_hash: str, replay: bool = False) -> str:
        suffix = f".{time.time_ns()}.replay" if replay else ""
        return f"{safe_name(request_id)}.{canonical_hash[:12]}{suffix}"

    def _final_response(self, request: dict[str, Any], status: str, code: str, message: str, result: Any, audit_record: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": VERSION,
            "requestId": request.get("requestId"),
            "canonicalHash": request.get("canonicalHash"),
            "verb": request.get("verb"),
            "status": status,
            "code": code,
            "message": message,
            "result": result,
            "finishedAt": iso(now_utc()),
            "audit": {"sequence": audit_record["sequence"], "entryHash": audit_record["entryHash"]},
        }

    def _process_file(self, path: Path) -> None:
        audit, replay = self._require_state()
        if path.parent != self.processing:
            self.processing.mkdir(mode=0o700, parents=True, exist_ok=True)
            while True:
                claimed = self.processing / f"{safe_name(path.stem)}.{os.getpid()}.{time.time_ns()}.json"
                try:
                    rename_noreplace(path, claimed)
                    fsync_directory(self.processing)
                    if path.parent != self.processing:
                        fsync_directory(path.parent)
                    break
                except FileExistsError:
                    continue
                except FileNotFoundError:
                    return
            path = claimed
        request: dict[str, Any] = {"requestId": f"INVALID-{sha256_bytes(os.fsencode(path.name))[:12]}", "canonicalHash": None, "verb": None}
        canonical_hash = str(request["requestId"])[8:]
        reserved = False
        preserve_claim = False
        try:
            raw = read_bounded_regular(path)
            canonical_hash = sha256_bytes(raw)
            request["canonicalHash"] = canonical_hash
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise Refusal("invalid_json", "request is not valid JSON") from exc
            request, canonical_hash = validate_request(parsed)
            if parse_iso(request["expiresAt"], "expiresAt") <= now_utc():
                raise Refusal("expired", "request expired before host acceptance")
            reservation = replay.reserve(request["requestId"], canonical_hash, path.name)
            if reservation == "complete":
                return
            if reservation == "replay":
                replay_audit = audit.append("request.refused", {
                    "requestId": request["requestId"], "canonicalHash": canonical_hash, "reason": "replay",
                })
                response = self._final_response(request, "refused", "replay", "requestId is single-use and was already consumed", None, replay_audit)
                while True:
                    try:
                        persist_signed(self.responses, self._response_name(request["requestId"], canonical_hash, replay=True), response, self.private_key)
                        break
                    except FileExistsError:
                        continue
                return
            reserved = True
            basename = self._response_name(request["requestId"], canonical_hash)
            if reservation == "resume":
                existing = read_existing_signed_payload(self.responses, basename, self.private_key)
                if existing is not None:
                    if existing.get("requestId") != request["requestId"] or existing.get("canonicalHash") != canonical_hash:
                        raise FatalStateError("existing terminal response does not match the retained claim")
                    status = existing.get("status")
                    if status not in {"succeeded", "refused", "failed"}:
                        raise FatalStateError("existing terminal response has an invalid status")
                    replay.finish(request["requestId"], str(status))
                    return
            audit.append("request.accepted" if reservation == "new" else "request.resumed", {
                "requestId": request["requestId"], "canonicalHash": canonical_hash,
                "requester": request["requester"], "verb": request["verb"],
                "requesterIdentityIsTechnicalAuthority": False,
                "requestReason": request.get("reason"),
                "approver": (request.get("approval") or {}).get("approver"),
            })
            if request["verb"] in PHASE1_WRITE_VERBS:
                write_spec = validate_write_arguments(request)
                manifest_revision = validate_write_manifest(request, self.config.get("writeAllowList"))
                preview = render_write_preview(request, write_spec)
                if manifest_revision is not None:
                    preview["allowListRevision"] = manifest_revision
                preview_audit = audit.append("preview.rendered", {
                    "requestId": request["requestId"], "canonicalHash": canonical_hash,
                    "previewHash": sha256_bytes(canonical_bytes(preview)),
                    "dryRun": True,
                })
                preview["audit"] = {"sequence": preview_audit["sequence"], "entryHash": preview_audit["entryHash"]}
                try:
                    persist_signed(self.previews, basename, preview, self.private_key)
                except FileExistsError:
                    if reservation != "resume":
                        raise
                # The preview above is pure documentation of what a future
                # enabled execution would do; rendering it does not depend on
                # authorization. Authorization and the O4 gate are both
                # checked before any production write could ever run.
                validate_write_authorization(request, self.config, now_utc())
                if request["verb"] == "coolify.app.restart":
                    # case_802335d7 row 2: a restart binds the running release of
                    # a succeeded deploy and can never bypass row 1. No
                    # deploy-ledger wiring exists in this build, so even a
                    # fully authorized restart refuses here, before the O4
                    # gate, with deploy_verification_unavailable.
                    validate_coolify_restart_precondition(request, self.config)
                writes_gate(self.config, write_spec)
            elif request["verb"] in WRITE_VERB_SPECS:
                # paperclip.deploy/paperclip.restart predate case_b1f57205's preview
                # path and stay refused with no preview. case_5248bcfa adds the
                # approval and drain checks ahead of that same unconditional
                # refusal below, so a paperclip-restart request that is fully
                # authorized and carries a fresh drain receipt still cannot
                # execute in this build: verify_zero_running_runs_host_side has
                # no host-side wiring and always refuses.
                write_spec = WRITE_VERB_SPECS[request["verb"]]
                validate_write_authorization(request, self.config, now_utc())
                if write_spec["tier"] == "paperclip-restart":
                    validate_drain_receipt(request, self.config, now_utc())

            spec = validate_read_request(request)
            preview = render_preview(request, spec)
            preview_audit = audit.append("preview.rendered", {
                "requestId": request["requestId"], "canonicalHash": canonical_hash,
                "previewHash": sha256_bytes(canonical_bytes(preview)),
            })
            preview["audit"] = {"sequence": preview_audit["sequence"], "entryHash": preview_audit["entryHash"]}
            try:
                persist_signed(self.previews, basename, preview, self.private_key)
            except FileExistsError:
                if reservation != "resume":
                    raise

            result = validate_result(request["verb"], EXECUTORS[spec["executor"]](self.config))
            result_hash = sha256_bytes(canonical_bytes(result))
            execution_audit = audit.append("execution.succeeded", {
                "requestId": request["requestId"], "canonicalHash": canonical_hash,
                "verb": request["verb"], "resultHash": result_hash,
                "writesPerformed": 0, "credentialEnteredRequesterProcess": False,
            })
            response = self._final_response(request, "succeeded", "ok", "fixed read-only verb completed", result, execution_audit)
            if request["verb"] == "logs.read" and len(canonical_bytes(response)) > LOG_RESPONSE_BYTE_LIMIT:
                raise Refusal("logs_too_large", "signed log response exceeds the total byte cap")
            persist_signed(self.responses, basename, response, self.private_key)
            replay.finish(request["requestId"], "succeeded")
        except FatalStateError:
            preserve_claim = True
            raise
        except Refusal as exc:
            refusal_audit = audit.append("execution.refused", {
                "requestId": request.get("requestId"), "canonicalHash": canonical_hash,
                "verb": request.get("verb"), "reason": exc.code, "message": str(exc),
                "writesPerformed": 0,
                "requester": request.get("requester"),
                "requestReason": request.get("reason"),
                "approver": (request.get("approval") or {}).get("approver"),
            })
            response = self._final_response(request, "refused", exc.code, str(exc), None, refusal_audit)
            basename = self._response_name(str(request.get("requestId")), canonical_hash)
            try:
                persist_signed(self.responses, basename, response, self.private_key)
            except FileExistsError:
                persist_signed(self.responses, self._response_name(str(request.get("requestId")), canonical_hash, replay=True), response, self.private_key)
            if reserved:
                replay.finish(str(request["requestId"]), "refused")
        except Exception as exc:
            try:
                failure_audit = audit.append("execution.failed", {
                    "requestId": request.get("requestId"), "canonicalHash": canonical_hash,
                    "verb": request.get("verb"), "reason": type(exc).__name__, "writesPerformed": 0,
                })
                response = self._final_response(request, "failed", "internal_error", f"executor failed: {type(exc).__name__}", None, failure_audit)
                try:
                    persist_signed(self.responses, self._response_name(str(request.get("requestId")), canonical_hash), response, self.private_key)
                except FileExistsError:
                    pass
                if reserved:
                    replay.finish(str(request["requestId"]), "failed")
            except Exception as recovery_exc:
                preserve_claim = True
                raise FatalStateError("could not persist a coherent terminal failure") from recovery_exc
        finally:
            if preserve_claim:
                fsync_directory(self.processing)
            else:
                try:
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink(missing_ok=True)
                    fsync_directory(self.processing)
                except OSError as exc:
                    # A requester must not be able to put the restart-on-failure
                    # service into a permanent poison-entry loop. Quarantine entries
                    # the broker cannot remove; the request group has no read access
                    # to this host-owned directory.
                    quarantine = self.spool / "quarantine"
                    quarantine.mkdir(mode=0o700, parents=True, exist_ok=True)
                    target = quarantine / f"{safe_name(path.name)}.{int(time.time())}"
                    try:
                        os.replace(path, target)
                        fsync_directory(self.processing)
                        fsync_directory(quarantine)
                    except OSError:
                        audit.append("spool.quarantine_failed", {
                            "pathHash": sha256_bytes(os.fsencode(path.name)),
                            "reason": type(exc).__name__,
                        })

    def process_file(self, path: Path) -> None:
        with self._exclusive_run():
            self._process_file(path)

    def run_once(self) -> int:
        self.requests.mkdir(parents=True, exist_ok=True)
        self.previews.mkdir(parents=True, exist_ok=True)
        self.responses.mkdir(parents=True, exist_ok=True)
        self.processing.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._exclusive_run():
            claimed_paths = sorted(self.processing.glob("*.json"))
            request_paths = sorted(self.requests.glob("*.json"))
            for path in claimed_paths + request_paths:
                self._process_file(path)
            return len(claimed_paths) + len(request_paths)


def parse_request_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """Parse the request CLI's --arguments JSON for write verbs.

    Read verbs accept no arguments: the flag must be absent or an empty
    object. Write-verb arguments are names and SHAs only; the parser takes
    them as one JSON object so no path, command, URL, shell, or token flag
    can ever exist on this CLI.
    """
    raw = getattr(args, "arguments_json", None) or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal("invalid_arguments", "arguments must be a JSON object") from exc
    if args.verb in READ_VERBS:
        if parsed != {}:
            raise Refusal("invalid_arguments", f"{args.verb} accepts exactly {{}}")
        return {}
    if not isinstance(parsed, dict):
        raise Refusal("invalid_arguments", "arguments must be a JSON object")
    return parsed


def build_request(args: argparse.Namespace) -> dict[str, Any]:
    requested = now_utc().replace(microsecond=0)
    request = {
        "version": VERSION,
        "requestId": args.request_id,
        "requestedAt": iso(requested),
        "expiresAt": iso(requested + dt.timedelta(seconds=args.ttl)),
        "verb": args.verb,
        "arguments": parse_request_arguments(args),
        "requester": {"agentId": args.agent_id, "runId": args.run_id, "issueId": args.issue_id},
    }
    request["canonicalHash"] = sha256_bytes(canonical_bytes(request))
    validate_request(request, requested)
    if args.verb in READ_VERBS:
        validate_read_request(request)
    else:
        validate_write_arguments(request)
    return request


def command_request(args: argparse.Namespace) -> int:
    request = build_request(args)
    request_dir = Path(args.spool) / "requests"
    filename = f"{safe_name(request['requestId'])}.{request['canonicalHash'][:12]}.json"
    atomic_write(request_dir / filename, canonical_bytes(request), mode=0o640, exclusive=True)
    print(json.dumps({
        "requestId": request["requestId"], "canonicalHash": request["canonicalHash"],
        "requestPath": str(request_dir / filename), "expiresAt": request["expiresAt"],
    }, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    public_key = read_bounded_regular(Path(args.public_key), maximum=16384, code="invalid_public_key")
    response = read_bounded_regular(Path(args.response), maximum=1048576, code="invalid_response")
    signature = read_bounded_regular(Path(args.signature), maximum=16384, code="invalid_signature")
    actual_key_hash = sha256_bytes(public_key)
    if actual_key_hash != args.expected_key_sha256:
        print("REFUSED: public key does not match the independently pinned SHA-256", file=sys.stderr)
        return 2
    if not verify_captured_signature(public_key, response, signature):
        print("REFUSED: signature verification failed", file=sys.stderr)
        return 2
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        print("REFUSED: verified response is not valid JSON", file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


def command_acceptance(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    validate_signing_key(Path(config["privateKeyPath"]))
    validate_result("service.inspect", execute_service(config))
    validate_result("image.inspect", execute_image(config))
    print(json.dumps({"signing": "ok", "service.inspect": "ok", "image.inspect": "ok"}, sort_keys=True))
    return 0


def command_describe(args: argparse.Namespace) -> int:
    print(json.dumps({
        "version": VERSION,
        "readVerbs": READ_VERBS,
        "writeVerbSpecifications": WRITE_VERB_SPECS,
        "ownerReservedVerbs": sorted(OWNER_RESERVED_VERBS),
        "ownerReservedVerbNamespaces": sorted(OWNER_RESERVED_VERB_NAMESPACES),
    }, sort_keys=True, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Pull-only fixed-verb host operations broker")
    sub = root.add_subparsers(dest="command", required=True)

    request = sub.add_parser("request", help="write a canonical request to the pull spool (reads execute; writes are dry-run previews only)")
    request.add_argument("--spool", required=True)
    request.add_argument("--verb", required=True, choices=sorted(set(READ_VERBS) | set(WRITE_VERB_SPECS)))
    request.add_argument("--request-id", required=True)
    request.add_argument("--agent-id", required=True)
    request.add_argument("--run-id", required=True)
    request.add_argument("--issue-id", required=True)
    request.add_argument("--ttl", type=int, default=300, choices=range(30, MAX_REQUEST_TTL_SECONDS + 1))
    # Write-verb arguments arrive as one JSON object of names and SHAs. There
    # is deliberately no per-field flag: no path, command, URL, shell, token,
    # unit, image, or approval argument may ever exist on this CLI.
    request.add_argument("--arguments-json", default="{}")
    request.set_defaults(func=command_request)

    once = sub.add_parser("run-once", help="host side: consume every pending request once")
    once.add_argument("--config", required=True)

    run = sub.add_parser("run", help="host side: poll forever; no inbound socket is opened")
    run.add_argument("--config", required=True)

    verify = sub.add_parser("verify", help="verify a host-signed preview or response")
    verify.add_argument("--public-key", required=True)
    verify.add_argument("--expected-key-sha256", required=True)
    verify.add_argument("--response", required=True)
    verify.add_argument("--signature", required=True)
    verify.set_defaults(func=command_verify)

    acceptance = sub.add_parser("acceptance-check", help="host side: validate the sudo-backed service and image reads")
    acceptance.add_argument("--config", required=True)
    acceptance.set_defaults(func=command_acceptance)

    describe = sub.add_parser("describe-verbs", help="print the fixed read table and disabled write specifications")
    describe.set_defaults(func=command_describe)
    return root


def main() -> int:
    args = parser().parse_args()
    if hasattr(args, "func"):
        try:
            return args.func(args)
        except (Refusal, FileExistsError, OSError, json.JSONDecodeError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2

    try:
        config = load_config(Path(args.config))
        validate_signing_key(Path(config["privateKeyPath"]))
        broker = Broker(config)
        if args.command == "run-once":
            print(json.dumps({"processed": broker.run_once()}))
            return 0
        while True:
            broker.run_once()
            time.sleep(config["pollSeconds"])
    except Refusal as exc:
        print(f"REFUSED [{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
