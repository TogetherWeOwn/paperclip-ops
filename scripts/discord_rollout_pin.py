#!/usr/bin/env python3
# ===========================================================================
# discord_rollout_pin.py — bounded existing-worker path for the private ops
# replacement pin (case_50f40818).
#
# Parent delivery: case_0c391e0b (Community Manager). Ancestor: case_8387b7f5.
#
# WHAT THIS IS.
#
# The installed vendor worker `paperclip-plugin-discord` 0.11.0 exposes no
# generic guild/channel/message/pin tool. Measured 2026-10-05 from
# non-secret installed source (no live operations, no credentials):
#
#   dist/manifest.js:298-407  six coordination/signals/configuration tools,
#                              none a guild/channel/message/pin tool.
#   dist/adapter.d.ts:3-20    send/edit/format only, no pin/unpin/pinned-list.
#   dist/discord-api.js:20-30,56-85  POST content/embeds/components with NO
#                              allowed_mentions; returned POST id is not
#                              independent readback.
#   dist/worker.js:2014-2034,2087-2099  only channel-mapping (read),
#                              set-channel, trigger-backfill handlers.
#   dist/discord-api.js:116-165  internal history readers (not exposed).
#   dist/workflow-engine.js:119-136,258-259  workflow-only send_message
#                              (not a bridge action; do not invoke labels).
#
# So this file adds ONLY the genuinely missing bounded transport, OUTSIDE
# vendor source, using ONLY a supported worker-owned execution route: the
# Bot token stays inside the existing installed worker (ctx.http.fetch with
# `Authorization: Bot <token>`), never returned to an agent, never bound as
# a secret, never on argv. There is no generic REST proxy here and no
# token-returning tool. If the worker lacks a sanctioned pin extension, the
# wrapper records the exact unsupported operation and returns a bounded
# legitimate Operator path instead of patching vendor files.
#
# BOUND AUTHORIZATION (all five must hold or the wrapper refuses):
#
#   1. This company only; ops guild 10112201438068849 only. TWO guild
#      10232839005875729 and every other guild/company is rejected.
#   2. Verified text channel named exactly `rollout-status` (type 0).
#      Zero or 2+ matches is a refusal, not a guess.
#   3. Proven private destination: parent/category present, role overwrites
#      present, existing pins/message authors readable. Missing or ambiguous
#      privacy evidence fails closed.
#   4. Exactly one approved replacement, all mentions disabled
#      (allowed_mentions {"parse": []}), task-scoped dedup key, history read
#      before any resend on ambiguous send.
#   5. Pin only a worker-bot-authored message in the validated channel, with
#      independent message + pin-list readback. Rollback unpins ONLY the
#      recorded replacement; old pins and human messages are never touched.
#
# Hard boundaries (from the card): no role/Administrator edits; no prod bot
# restarts or live production tests; no new credentials/connections/bindings,
# creation/rotation/export; no substitution, self-bots, webhooks, public
# posting, spend, cross-company routing or permission expansion.
#
# USAGE (offline):
#   python3 scripts/discord_rollout_pin.py --selftest   # pure-function checks
#   python3 -m unittest -v test_discord_rollout_pin.py  # full suite w/ fakes
#
# No network, no credentials, no Discord calls. Live use is Operator-only
# via the deployment child (see docs/discord-rollout-pin-transport.md).
# ===========================================================================
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from typing import Any


OPS_GUILD_ID = "10112201438068849"
FORBIDDEN_TWO_GUILD_ID = "10232839005875729"
TARGET_CHANNEL_NAME = "rollout-status"
DEDUP_KEY = "discord-rollout-replacement:case_50f40818:v1"
WORKER_CONTRACT_VERSION = "paperclip-plugin-discord@0.11.0"

# The vendor POST path used by the worker-owned route. The wrapper never
# constructs an arbitrary proxy around it: each call below is one of the five
# narrow operations, each checked before the HTTP line would run.
DISCORD_API_BASE = "https://discord.com/api/v10"

SUPPORTED_OPERATIONS = (
    "guild.resolve",
    "channel.resolve",
    "channel.read_privacy",
    "message.publish_once",
    "message.pin_verified",
    "message.readback",
    "pin.rollback_replacement",
)

UNSUPPORTED_VENDOR_OPERATIONS = (
    # Exact gap recorded 2026-10-05: no documented pin operation exists in
    # the installed worker, so pin/unpin/pinned-list can only run through
    # this bounded wrapper on the worker-owned route (Operator-installed),
    # never as a guessed bridge action or workflow label.
    "channels/{id}/pins (PUT/DELETE) via worker-owned route",
    "channels/{id}/pins (GET pinned-list) via worker-owned route",
    "channels/{id}/messages/{mid} (GET independent readback) via worker-owned route",
)


class PinTransportError(Exception):
    """Fail-closed refusal. Message text is intentionally stable for tests."""


# ---------------------------------------------------------------------------
# 1. Guild allowlist / company scope
# ---------------------------------------------------------------------------
def validate_guild(guild_id: str) -> str:
    gid = (guild_id or "").strip()
    if not gid:
        raise PinTransportError("refused: missing guild id")
    if gid == FORBIDDEN_TWO_GUILD_ID:
        raise PinTransportError("refused: forbidden TWO guild")
    if gid != OPS_GUILD_ID:
        raise PinTransportError("refused: guild not in allowlist")
    return gid


# ---------------------------------------------------------------------------
# 2. Channel resolution
# ---------------------------------------------------------------------------
def resolve_rollout_channel(channels: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        c for c in (channels or [])
        if c.get("name") == TARGET_CHANNEL_NAME and int(c.get("type", -1)) == 0
    ]
    if len(matches) == 0:
        raise PinTransportError("refused: rollout-status channel missing")
    if len(matches) > 1:
        raise PinTransportError("refused: rollout-status channel ambiguous")
    return matches[0]


# ---------------------------------------------------------------------------
# 3. Privacy enforcement
# ---------------------------------------------------------------------------
def check_channel_privacy(channel: dict[str, Any]) -> dict[str, Any]:
    if not channel or not channel.get("id"):
        raise PinTransportError("refused: channel privacy unproven (no id)")
    parent = channel.get("parent_id") or channel.get("parentId") or channel.get("category_id")
    if not parent:
        raise PinTransportError("refused: channel privacy unproven (no parent/category)")
    if "permission_overwrites" not in channel:
        raise PinTransportError("refused: channel privacy unproven (no role overwrites)")
    overwrites = channel.get("permission_overwrites")
    if not isinstance(overwrites, list) or len(overwrites) == 0:
        raise PinTransportError("refused: channel privacy unproven (empty overwrites)")
    return {"channel_id": str(channel["id"]), "parent_id": str(parent),
            "overwrite_count": len(overwrites)}


def validate_existing_pins(pins: list[dict[str, Any]]) -> int:
    for p in (pins or []):
        author = (p or {}).get("author") or {}
        if not author.get("id") or not author.get("username"):
            raise PinTransportError("refused: existing pin author unproven")
        if "bot" not in author:
            raise PinTransportError("refused: existing pin bot flag unproven")
    return len(pins or [])


# ---------------------------------------------------------------------------
# 4. Publish-once with mention suppression + dedup
# ---------------------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r"PLACEHOLDER|TODO:|FIXME|<insert|__FILL__", re.IGNORECASE)


def build_send_payload(content: str, dedup_key: str = DEDUP_KEY) -> dict[str, Any]:
    text = (content or "")
    if not text.strip():
        raise PinTransportError("refused: empty replacement content")
    if PLACEHOLDER_RE.search(text):
        raise PinTransportError("refused: placeholder content")
    if not dedup_key or not dedup_key.startswith("discord-rollout-replacement:"):
        raise PinTransportError("refused: missing task-scoped dedup key")
    # Mention suppression: the installed vendor POST omits allowed_mentions
    # (dist/discord-api.js:20-30,56-85), so every send through this wrapper
    # pins it to parse-nothing. There is no override flag by design.
    return {
        "content": text,
        "allowed_mentions": {"parse": []},
        "dedup_key": dedup_key,
    }


def find_existing_send(history: list[dict[str, Any]], content: str,
                       dedup_key: str = DEDUP_KEY) -> dict[str, Any] | None:
    matches = [m for m in (history or []) if m.get("content") == content]
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise PinTransportError("refused: ambiguous send history, read before resend")
    return matches[0]


# ---------------------------------------------------------------------------
# 5. Pin-only-bot-authored + readback
# ---------------------------------------------------------------------------
def verify_pin_candidate(message: dict[str, Any], bot_user_id: str,
                         validated_channel_id: str) -> dict[str, Any]:
    if not message or not message.get("id"):
        raise PinTransportError("refused: pin candidate has no id")
    if str(message.get("channel_id")) != str(validated_channel_id):
        raise PinTransportError("refused: pin candidate outside validated channel")
    author = message.get("author") or {}
    if str(author.get("id")) != str(bot_user_id):
        raise PinTransportError("refused: foreign-author pin rejected")
    if author.get("bot") is not True:
        raise PinTransportError("refused: foreign-author pin rejected (not bot)")
    return message


def format_readback(guild_id: str, channel_id: str, message: dict[str, Any],
                    pin_list: list[dict[str, Any]]) -> dict[str, Any]:
    validate_guild(guild_id)
    mid = str(message.get("id", ""))
    if not mid:
        raise PinTransportError("refused: readback message has no id")
    author = message.get("author") or {}
    pin_ids = {str(p.get("id")) for p in (pin_list or []) if p.get("id")}
    return {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "message_id": mid,
        "author_id": str(author.get("id", "")),
        "author_name": str(author.get("username", "")),
        "author_is_bot": bool(author.get("bot", False)),
        "timestamp_utc": str(message.get("timestamp", "")),
        "message_url": f"https://discord.com/channels/{guild_id}/{channel_id}/{mid}",
        "pinned": bool(message.get("pinned", False)),
        "pin_list_membership": mid in pin_ids,
    }


def rollback_replacement(recorded_message_id: str, target_message_id: str,
                         pin_list: list[dict[str, Any]]) -> dict[str, Any]:
    if not recorded_message_id or not target_message_id:
        raise PinTransportError("refused: rollback needs recorded + target ids")
    if str(target_message_id) != str(recorded_message_id):
        # Never unpin pre-existing pins, never touch human messages.
        raise PinTransportError("refused: rollback preserves old pins")
    pin_ids = {str(p.get("id")) for p in (pin_list or []) if p.get("id")}
    if str(recorded_message_id) not in pin_ids:
        raise PinTransportError("refused: replacement not pinned, nothing to rollback")
    return {"action": "unpin", "message_id": str(recorded_message_id)}


# ---------------------------------------------------------------------------
# Offline fake worker (deterministic, no network)
# ---------------------------------------------------------------------------
class FakeWorker:
    def __init__(self, guild_id: str = OPS_GUILD_ID,
                 bot_user_id: str = "10033305811700754",
                 bot_username: str = "paperclip-ops-bot",
                 channels: list[dict[str, Any]] | None = None,
                 messages: list[dict[str, Any]] | None = None,
                 pins: list[dict[str, Any]] | None = None):
        self.guild_id = guild_id
        self.bot_user_id = bot_user_id
        self.bot_username = bot_username
        self.channels: list[dict[str, Any]] = list(channels) if channels else []
        self.messages: list[dict[str, Any]] = list(messages) if messages else []
        self.pins: list[dict[str, Any]] = list(pins) if pins else []
        self.sends = 0

    def list_channels(self) -> list[dict[str, Any]]:
        validate_guild(self.guild_id)
        return list(self.channels)

    def publish_once(self, channel_id: str, content: str) -> dict[str, Any]:
        payload = build_send_payload(content)
        existing = find_existing_send(self.messages, payload["content"])
        if existing is not None:
            return {"reused": True, "message": existing, "payload": payload}
        mid = f"msg-{len(self.messages)+1:04d}-{hashlib.sha1(payload['content'].encode()[:32]).hexdigest()[:6]}"
        msg = {
            "id": mid,
            "channel_id": str(channel_id),
            "content": payload["content"],
            "author": {"id": self.bot_user_id, "username": self.bot_username, "bot": True},
            "timestamp": "2026-10-05T00:00:00.000Z",
            "pinned": False,
        }
        self.messages.append(msg)
        self.sends += 1
        return {"reused": False, "message": msg, "payload": payload}

    def pin_verified(self, message_id: str, channel_id: str) -> dict[str, Any]:
        msg = next((m for m in self.messages if str(m.get("id")) == str(message_id)), None)
        if msg is None:
            raise PinTransportError("refused: pin candidate has no id")
        verify_pin_candidate(msg, self.bot_user_id, channel_id)
        msg["pinned"] = True
        if all(str(p.get("id")) != str(message_id) for p in self.pins):
            self.pins.append(dict(msg))
        return format_readback(self.guild_id, channel_id, msg, self.pins)


def selftest() -> int:
    failures = 0

    def check(name: str, fn) -> None:
        nonlocal failures
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001 — selftest reports, never throws
            failures += 1
            print(f"FAIL {name}: unexpected {e}")
        else:
            print(f"ok {name}")

    def expect_refused(fn, needle: str) -> None:
        try:
            fn()
        except PinTransportError as e:
            assert needle in str(e), f"want {needle!r} in {e}"
            return
        raise AssertionError(f"expected refusal containing {needle!r}")

    check("guild allowlist ok", lambda: validate_guild(OPS_GUILD_ID))
    check("guild rejects TWO", lambda: expect_refused(
        lambda: validate_guild(FORBIDDEN_TWO_GUILD_ID), "forbidden TWO"))
    check("guild rejects other", lambda: expect_refused(
        lambda: validate_guild("123"), "allowlist"))
    check("mentions suppressed", lambda: (
        lambda p: (_ for _ in ()).throw(AssertionError("mentions not suppressed"))
        if p.get("allowed_mentions") != {"parse": []} else None
    )(build_send_payload("hello world")))
    check("placeholder refused", lambda: expect_refused(
        lambda: build_send_payload("TODO: fill this PLACEHOLDER"), "placeholder"))
    if failures:
        print(f"SELFTEST {failures} failures")
        return 1
    print("SELFTEST ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bounded rollout-pin transport (offline).")
    ap.add_argument("--selftest", action="store_true", help="run pure-function checks")
    ap.add_argument("--contract", action="store_true", help="print operation contract")
    args = ap.parse_args(argv)
    if args.contract:
        print("supported: " + ", ".join(SUPPORTED_OPERATIONS))
        print("vendor: " + WORKER_CONTRACT_VERSION)
        print("guild: " + OPS_GUILD_ID)
        return 0
    if args.selftest:
        return selftest()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
