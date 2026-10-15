#!/usr/bin/env python3
# ===========================================================================
# test_discord_rollout_pin.py — offline suite for scripts/discord_rollout_pin.py
# (case_50f40818). No network, no credentials, no Discord calls.
#
# WHAT THIS SUITE PINS (each section is a load-bearing guard):
#
#  1. Allowlist/company scope: ops guild passes; TWO guild 10232839005875729
#     and every other guild refused. A wrapper rewritten to "any guild" fails.
#  2. Channel resolution: exactly one text `rollout-status` resolves; zero or
#     2+ is a refusal, never a guess. Non-text or wrong-name never resolves.
#  3. Privacy enforcement: parent/category + role overwrites + pin authors
#     required; missing/empty/ambiguous evidence fails closed.
#  4. Mention suppression: every payload carries allowed_mentions {parse: []}
#     with no override; placeholders refused.
#  5. Foreign-author pin rejection: only the worker bot's own message in the
#     validated channel may pin; human or other-bot messages refused.
#  6. Dedup/ambiguous sends: identical content reuses the first message and
#     sends once; 2+ history matches refuse instead of sending again.
#  7. Pin/message readback: guild/channel/message ids, author id/name/bot,
#     UTC timestamp, URL, pinned flag, pin-list membership.
#  8. Rollback preservation: unpin ONLY the recorded replacement; any other
#     target, unpinned replacement, or human pin is refused.
#
# A suite hardcoded to always refuse would pass 1-6 and 8 but fail 7's
# positive readback; a suite hardcoded to always allow would pass 7 but fail
# the refusal sections. Both directions are pinned, so the suite measures
# something. Follows test_host_ops_broker.py unittest style.
# ===========================================================================
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "scripts" / "discord_rollout_pin.py"
SPEC = importlib.util.spec_from_file_location("discord_rollout_pin", TOOL)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def ops_channel(**kw):
    base = {
        "id": "10172687662235724",
        "name": "rollout-status",
        "type": 0,
        "parent_id": "10064085345421855",
        "permission_overwrites": [
            {"id": "everyone", "type": "role", "deny": "1024"},
            {"id": "ops-role", "type": "role", "allow": "1024"},
        ],
    }
    base.update(kw)
    return base


class TestGuildAllowlist(unittest.TestCase):
    def test_ops_guild_passes(self):
        self.assertEqual(mod.validate_guild(mod.OPS_GUILD_ID), mod.OPS_GUILD_ID)

    def test_two_guild_rejected(self):
        with self.assertRaises(mod.PinTransportError) as cm:
            mod.validate_guild("10232839005875729")
        self.assertIn("forbidden TWO", str(cm.exception))

    def test_other_guild_rejected(self):
        for gid in ["123", "", "  ", "10017272426914487"]:
            with self.assertRaises(mod.PinTransportError, msg=gid) as cm:
                mod.validate_guild(gid)
            self.assertTrue(
                "allowlist" in str(cm.exception) or "missing" in str(cm.exception),
                str(cm.exception),
            )


class TestChannelResolution(unittest.TestCase):
    def test_resolves_single_text(self):
        ch = mod.resolve_rollout_channel([ops_channel(), {"id": "x", "name": "other", "type": 0}])
        self.assertEqual(ch["name"], "rollout-status")

    def test_missing_refused(self):
        with self.assertRaises(mod.PinTransportError) as cm:
            mod.resolve_rollout_channel([{"id": "x", "name": "general", "type": 0}])
        self.assertIn("missing", str(cm.exception))

    def test_ambiguous_refused(self):
        with self.assertRaises(mod.PinTransportError) as cm:
            mod.resolve_rollout_channel([ops_channel(id="a"), ops_channel(id="b")])
        self.assertIn("ambiguous", str(cm.exception))

    def test_voice_same_name_ignored(self):
        with self.assertRaises(mod.PinTransportError):
            mod.resolve_rollout_channel([ops_channel(id="v", type=2)])


class TestPrivacy(unittest.TestCase):
    def test_private_ok(self):
        out = mod.check_channel_privacy(ops_channel())
        self.assertEqual(out["channel_id"], "10172687662235724")

    def test_no_parent_refused(self):
        c = ops_channel()
        del c["parent_id"]
        with self.assertRaises(mod.PinTransportError) as cm:
            mod.check_channel_privacy(c)
        self.assertIn("parent/category", str(cm.exception))

    def test_no_overwrites_key_refused(self):
        c = ops_channel()
        del c["permission_overwrites"]
        with self.assertRaises(mod.PinTransportError) as cm:
            mod.check_channel_privacy(c)
        self.assertIn("role overwrites", str(cm.exception))

    def test_empty_overwrites_refused(self):
        with self.assertRaises(mod.PinTransportError):
            mod.check_channel_privacy(ops_channel(permission_overwrites=[]))

    def test_pin_authors_required(self):
        self.assertEqual(mod.validate_existing_pins([
            {"id": "p1", "author": {"id": "a", "username": "owner", "bot": False}},
        ]), 1)
        with self.assertRaises(mod.PinTransportError):
            mod.validate_existing_pins([{"id": "p1", "author": {"id": "", "username": "x", "bot": False}}])
        with self.assertRaises(mod.PinTransportError):
            mod.validate_existing_pins([{"id": "p1", "author": {"id": "a", "username": "x"}}])


class TestSend(unittest.TestCase):
    def test_mentions_suppressed_no_override(self):
        p = mod.build_send_payload("Private Discord rollout — status note.")
        self.assertEqual(p["allowed_mentions"], {"parse": []})
        self.assertNotIn("allowedMentions", p)

    def test_placeholder_refused(self):
        for bad in ["TODO: fill me", "a PLACEHOLDER here", "<insert date>", "FIXME later"]:
            with self.assertRaises(mod.PinTransportError, msg=bad):
                mod.build_send_payload(bad)

    def test_empty_refused(self):
        with self.assertRaises(mod.PinTransportError):
            mod.build_send_payload("   ")

    def test_dedup_reuses_single(self):
        w = mod.FakeWorker(channels=[ops_channel()])
        first = w.publish_once("10172687662235724", "hello once")
        second = w.publish_once("10172687662235724", "hello once")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["message"]["id"], second["message"]["id"])
        self.assertEqual(w.sends, 1)

    def test_ambiguous_history_refused(self):
        w = mod.FakeWorker(channels=[ops_channel()])
        w.messages.append({"id": "m1", "content": "dup", "channel_id": "c",
                           "author": {"id": w.bot_user_id, "username": "b", "bot": True}})
        w.messages.append({"id": "m2", "content": "dup", "channel_id": "c",
                           "author": {"id": w.bot_user_id, "username": "b", "bot": True}})
        with self.assertRaises(mod.PinTransportError) as cm:
            mod.find_existing_send(w.messages, "dup")
        self.assertIn("ambiguous", str(cm.exception))


class TestPinReadback(unittest.TestCase):
    def _worker_with_msg(self):
        w = mod.FakeWorker(channels=[ops_channel()])
        r = w.publish_once("10172687662235724", "status note body")
        return w, r["message"]

    def test_bot_authored_pins(self):
        w, msg = self._worker_with_msg()
        rb = w.pin_verified(msg["id"], "10172687662235724")
        self.assertEqual(rb["guild_id"], mod.OPS_GUILD_ID)
        self.assertEqual(rb["channel_id"], "10172687662235724")
        self.assertEqual(rb["message_id"], msg["id"])
        self.assertTrue(rb["author_is_bot"])
        self.assertTrue(rb["pinned"])
        self.assertTrue(rb["pin_list_membership"])
        self.assertIn(f"/{msg['id']}", rb["message_url"])

    def test_human_author_rejected(self):
        w, msg = self._worker_with_msg()
        msg["author"] = {"id": "human-1", "username": "owner", "bot": False}
        with self.assertRaises(mod.PinTransportError) as cm:
            w.pin_verified(msg["id"], "10172687662235724")
        self.assertIn("foreign-author", str(cm.exception))

    def test_other_bot_author_rejected(self):
        # Same bot flag, different id: proves the id check alone refuses.
        # Without this, an id-bypass mutant survives behind the bot-flag guard.
        w, msg = self._worker_with_msg()
        msg["author"] = {"id": "10023457841537223", "username": "other-bot", "bot": True}
        with self.assertRaises(mod.PinTransportError) as cm:
            w.pin_verified(msg["id"], "10172687662235724")
        self.assertIn("foreign-author", str(cm.exception))

    def test_wrong_channel_rejected(self):
        w, msg = self._worker_with_msg()
        with self.assertRaises(mod.PinTransportError) as cm:
            w.pin_verified(msg["id"], "other-channel")
        self.assertIn("outside validated channel", str(cm.exception))

    def test_readback_membership_false_when_unpinned(self):
        w, msg = self._worker_with_msg()
        rb = mod.format_readback(w.guild_id, "10172687662235724", msg, [])
        self.assertFalse(rb["pinned"])
        self.assertFalse(rb["pin_list_membership"])


class TestRollback(unittest.TestCase):
    def test_only_recorded_unpins(self):
        pins = [{"id": "msg-0001-abc"}, {"id": "old-pin-9"}]
        out = mod.rollback_replacement("msg-0001-abc", "msg-0001-abc", pins)
        self.assertEqual(out, {"action": "unpin", "message_id": "msg-0001-abc"})

    def test_other_target_preserved(self):
        pins = [{"id": "msg-0001-abc"}, {"id": "old-pin-9"}]
        with self.assertRaises(mod.PinTransportError) as cm:
            mod.rollback_replacement("msg-0001-abc", "old-pin-9", pins)
        self.assertIn("preserves old pins", str(cm.exception))

    def test_unpinned_nothing_to_do(self):
        with self.assertRaises(mod.PinTransportError):
            mod.rollback_replacement("msg-0001-abc", "msg-0001-abc", [{"id": "old-pin-9"}])


class TestContract(unittest.TestCase):
    def test_supported_ops_bounded(self):
        self.assertIn("message.pin_verified", mod.SUPPORTED_OPERATIONS)
        self.assertNotIn("messages.proxy", mod.SUPPORTED_OPERATIONS)

    def test_unsupported_gap_recorded(self):
        self.assertTrue(any("pins" in s for s in mod.UNSUPPORTED_VENDOR_OPERATIONS))


if __name__ == "__main__":
    unittest.main()
