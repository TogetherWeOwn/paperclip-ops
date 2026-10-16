#!/usr/bin/env python3
"""Synthetic tests for the CLIProxy feed contract (case_0b2823ef).

Hermetic: no network, no credentials, no host files. The oracle transport is
faked; the host producer is never imported (its module-level load_key() opens
a host-only file). Run: python3 -B -m unittest test_feed_contract
"""

import copy
import inspect
import io
import json
import os
import re
import tempfile
import unittest

import feed_contract as fc


# ---------------------------------------------------------------------------
# Oracle fixtures
# ---------------------------------------------------------------------------

def oracle_doc(ids):
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


POSITIVE_IDS = ["gpt-6-luna", "gpt-6.1-sol", "claude-opus-5-5"]
# Bare ids the live feed must NOT serve (case_b9ae3ea2 readback contract).
NEGATIVE_BARE_IDS = ["glm-5-3-flash", "gpt-5-4-mini", "kimi-k3"]


class FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener_factory(body=None, status=200, exc=None, seen=None):
    def opener(req, timeout=None):
        (seen if seen is not None else {}).update(
            {"url": req.full_url, "auth": req.get_header("Authorization")}
        )
        if exc is not None:
            raise exc
        return FakeResp(body, status)

    return opener


def fetch(body=None, status=200, exc=None, seen=None, payload=None):
    raw = payload if payload is not None else oracle_doc(body or [])
    text = raw if isinstance(raw, str) else json.dumps(raw)
    return fc.fetch_served_models(
        "http://127.0.0.1:8317/v1/models",
        "KEY",
        opener=fake_opener_factory(text, status, exc, seen),
    )


class ServedModelsExactIds(unittest.TestCase):
    def test_positives_present_exactly(self):
        ids = fetch(POSITIVE_IDS + ["gemini-2.5-pro", "grok-4.3"])
        for want in POSITIVE_IDS:
            self.assertIn(want, ids)
        self.assertEqual(ids, POSITIVE_IDS + ["gemini-2.5-pro", "grok-4.3"])

    def test_bare_negatives_absent_when_oracle_lacks_them(self):
        ids = fetch(POSITIVE_IDS + ["zai/glm-4.6", "antigravity/gemini-2.5-flash"])
        for bad in NEGATIVE_BARE_IDS:
            self.assertNotIn(bad, ids)

    def test_prefixes_never_stripped_or_synthesized(self):
        # A prefixed oracle id must survive verbatim, and its bare suffix
        # must NOT be synthesized alongside it (no canonicalization).
        ids = fetch(["partner/claude-opus-5-5", "antigravity/gemini-2.5-pro"])
        self.assertIn("partner/claude-opus-5-5", ids)
        self.assertNotIn("claude-opus-5-5", ids)
        self.assertNotIn("gemini-2.5-pro", ids)

    def test_order_preserved_duplicates_collapsed(self):
        ids = fetch(["b-id", "a-id", "b-id"])
        self.assertEqual(ids, ["b-id", "a-id"])

    def test_output_contains_only_oracle_id_strings(self):
        ids = fetch(POSITIVE_IDS)
        self.assertTrue(all(isinstance(i, str) and i for i in ids))
        self.assertEqual(set(ids), set(POSITIVE_IDS))

    def test_malformed_rows_skipped_not_fatal(self):
        payload = {"data": [{"id": "gpt-6-luna"}, {"noid": 1}, {"id": ""}, {"id": "   "}, {"id": None}, "bare-id"]}
        self.assertEqual(fetch(payload=payload), ["gpt-6-luna"])

    def test_nonblank_ids_are_preserved_verbatim(self):
        self.assertEqual(fetch([" partner/model "]), [" partner/model "])


class ServedModelsOracleFailure(unittest.TestCase):
    def test_transport_error_omits_key(self):
        with self.assertRaises(fc.ServedModelsError):
            fetch(exc=ConnectionError("refused"))
        doc = {"observedAt": "t", "accounts": []}
        self.assertNotIn("servedModels", fc.attach_served_models(doc, None))

    def test_non_200_omits_key(self):
        with self.assertRaises(fc.ServedModelsError):
            fetch(body=POSITIVE_IDS, status=401)

    def test_non_json_body_omits_key(self):
        with self.assertRaises(fc.ServedModelsError):
            fetch(payload="<html>nope</html>")

    def test_missing_data_list_omits_key(self):
        for bad in ({"models": ["a"]}, {"data": "nope"}, {"data": None}, ["a"], "s", None):
            with self.assertRaises(fc.ServedModelsError, msg=repr(bad)):
                fc.extract_served_ids(bad)

    def test_empty_oracle_is_failure_not_authoritative_empty(self):
        # An empty data[] must not become a published [] that reads as
        # "CLIProxy serves nothing".
        with self.assertRaises(fc.ServedModelsError):
            fc.extract_served_ids({"data": []})
        doc = {"servedModels": POSITIVE_IDS}
        fc.attach_served_models(doc, [])
        self.assertNotIn("servedModels", doc)

    def test_failed_refresh_removes_stale_key(self):
        doc = {"observedAt": "t", "servedModels": POSITIVE_IDS}
        fc.attach_served_models(doc, None)
        self.assertNotIn("servedModels", doc)

    def test_success_replaces_never_merges(self):
        doc = {"servedModels": ["stale-id"]}
        fc.attach_served_models(doc, ["fresh-id"])
        self.assertEqual(doc["servedModels"], ["fresh-id"])

    def test_accounts_never_feed_served_models(self):
        doc = {"accounts": [{"models": ["gpt-6-luna"]}], "observedAt": "t"}
        before = copy.deepcopy(doc)
        fc.attach_served_models(doc, None)
        self.assertEqual(doc, before)


class ServedModelsAuthPattern(unittest.TestCase):
    def test_bearer_key_and_url_passed_through(self):
        seen = {}
        fetch(body=POSITIVE_IDS, seen=seen)
        self.assertEqual(seen["url"], "http://127.0.0.1:8317/v1/models")
        self.assertEqual(seen["auth"], "Bearer KEY")

    def test_default_url_is_loopback_v1_models(self):
        self.assertTrue(
            fc.SERVED_MODELS_URL_DEFAULT.startswith("http://127.0.0.1:")
            and fc.SERVED_MODELS_URL_DEFAULT.endswith("/v1/models")
        )


def write_key_file(text):
    fd, path = tempfile.mkstemp(prefix="served-key-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class ServedBearerContract(unittest.TestCase):
    def test_no_key_file_returns_fallback(self):
        self.assertEqual(fc.served_bearer("", "KEY"), "KEY")
        self.assertEqual(fc.served_bearer(), "")

    def test_bare_line_returned_verbatim(self):
        path = write_key_file("secret-bearer\n")
        try:
            self.assertEqual(fc.served_bearer(path, "KEY"), "secret-bearer")
        finally:
            os.unlink(path)

    def test_key_equals_value_unquoted(self):
        path = write_key_file("# comment\nKEY= file-bearer \n")
        try:
            self.assertEqual(fc.served_bearer(path, "KEY"), "file-bearer")
        finally:
            os.unlink(path)

    def test_key_equals_value_strips_matching_quotes(self):
        path = write_key_file('KEY="quoted-bearer"\n')
        try:
            self.assertEqual(fc.served_bearer(path, "KEY"), "quoted-bearer")
        finally:
            os.unlink(path)
        path = write_key_file("KEY='single-bearer'\n")
        try:
            self.assertEqual(fc.served_bearer(path, "KEY"), "single-bearer")
        finally:
            os.unlink(path)
        path = write_key_file('KEY="unmatched\n')
        try:
            self.assertEqual(fc.served_bearer(path, "KEY"), '"unmatched')
        finally:
            os.unlink(path)

    def test_empty_file_raises_served_models_error(self):
        path = write_key_file("")
        try:
            with self.assertRaises(fc.ServedModelsError):
                fc.served_bearer(path, "KEY")
        finally:
            os.unlink(path)

    def test_comment_only_file_raises_served_models_error(self):
        path = write_key_file("# only a comment\n\n  \n")
        try:
            with self.assertRaises(fc.ServedModelsError):
                fc.served_bearer(path, "KEY")
        finally:
            os.unlink(path)

    def test_bad_key_file_is_fail_open_not_system_exit(self):
        # Mirrors build()'s `except Exception: served = None` guard: a
        # mis-provisioned key file must omit servedModels, never 502 the feed
        # (SystemExit is BaseException and would escape that guard).
        path = write_key_file("")
        try:
            try:
                fc.served_bearer(path, "KEY")
                served = ["unreachable"]
            except Exception:
                served = None
            self.assertIsNone(served)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Durable exclusions
# ---------------------------------------------------------------------------

FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"
NOW = 1_790_000_000.0  # ~2026-09; before the live 2026-10-17 hold expiry
AFTER_HOLD = 1_793_000_000.0  # ~2026-10-28; after the live hold expiry


class DurableExclusionContract(unittest.TestCase):
    def test_future_until_still_excludes(self):
        self.assertTrue(fc.exclusion_is_active({"until": FUTURE}, NOW))

    def test_expired_until_does_not_exclude(self):
        self.assertFalse(fc.exclusion_is_active({"until": PAST}, NOW))

    def test_live_hold_shape_active_before_expiry_idle_after(self):
        entry = {"until": "2026-10-17T00:00:00Z", "reason": "x"}
        self.assertTrue(fc.exclusion_is_active(entry, NOW))
        self.assertFalse(fc.exclusion_is_active(entry, AFTER_HOLD))

    def test_durable_true_excludes_without_until(self):
        self.assertTrue(fc.exclusion_is_active({"durable": True}, NOW))
        self.assertTrue(fc.exclusion_is_active({"durable": True}, AFTER_HOLD))

    def test_durable_true_beats_expired_until(self):
        self.assertTrue(
            fc.exclusion_is_active({"durable": True, "until": PAST}, AFTER_HOLD)
        )

    def test_non_boolean_durable_never_means_forever(self):
        for bad in ("true", "True", 1, 1.0, ["x"], {"x": 1}):
            self.assertFalse(
                fc.exclusion_is_active({"durable": bad}, AFTER_HOLD), msg=repr(bad)
            )
            # ... and without an until it still excludes nothing at all.
            self.assertFalse(
                fc.exclusion_is_active({"durable": bad}, NOW), msg=repr(bad)
            )

    def test_invalid_until_without_durable_excludes_nothing(self):
        for bad in (None, "", "not-a-date", "99999", {"until": FUTURE}, [FUTURE]):
            self.assertFalse(fc.exclusion_is_active({"until": bad}, NOW), msg=repr(bad))

    def test_missing_until_without_durable_excludes_nothing(self):
        self.assertFalse(fc.exclusion_is_active({"reason": "x"}, NOW))
        self.assertFalse(fc.exclusion_is_active({}, NOW))

    def test_non_dict_entry_excludes_nothing(self):
        for bad in (None, "zen-free", ["zen-free"], 42, True):
            self.assertFalse(fc.exclusion_is_active(bad, NOW), msg=repr(bad))

    def test_epoch_until_supported(self):
        self.assertTrue(fc.exclusion_is_active({"until": NOW + 100}, NOW))
        self.assertFalse(fc.exclusion_is_active({"until": NOW - 100}, NOW))


def live_like_excl():
    return {
        "zai": {"until": FUTURE, "reason": "z"},
        "zen-free": {"until": "2026-10-17T00:00:00Z", "reason": "FreeTierError"},
        "model:muse-spark-1.2-contributor": {"until": "2026-10-17T00:00:00Z", "reason": "m"},
        "model:grok-3-mini": {"until": "2026-10-17T00:00:00Z", "reason": "m"},
        "model:grok-3-mini-fast": {"until": "2026-10-17T00:00:00Z", "reason": "m"},
    }


def accounts():
    return [
        {"lane": "zen-free-lane-1", "provider": "zen-free", "accountKey": "zen-free-lane-1",
         "health": "healthy", "models": ["mimo-v2.6-flash-free", "ling-3.1-flash-free"]},
        {"lane": "codex-lane-1", "provider": "codex", "accountKey": "codex-lane-1",
         "health": "healthy", "models": ["gpt-6-luna", "gpt-5-4-mini", "x/grok-3-mini"]},
        {"lane": "meta-lane-7", "provider": "meta", "accountKey": "meta-lane-7",
         "health": "degraded", "models": ["muse-spark-1.3", "muse-spark-1.2-contributor"]},
    ]


class ApplyExclusions(unittest.TestCase):
    def test_expiring_provider_entry_preserved(self):
        accs = fc.apply_exclusions(accounts(), live_like_excl(), NOW)
        zen = accs[0]
        self.assertEqual(zen["health"], "unavailable")
        self.assertIn("FreeTierError", zen["excludedReason"])
        # Unrelated accounts untouched.
        self.assertEqual(accs[1]["health"], "healthy")
        self.assertNotIn("excludedReason", accs[1])
        self.assertEqual(accs[2]["health"], "degraded")

    def test_expiring_model_entries_preserved(self):
        accs = fc.apply_exclusions(accounts(), live_like_excl(), NOW)
        self.assertNotIn("muse-spark-1.2-contributor", accs[2]["models"])
        self.assertIn("muse-spark-1.3", accs[2]["models"])
        self.assertNotIn("grok-3-mini", accs[1]["models"])
        self.assertIn("gpt-6-luna", accs[1]["models"])

    def test_durable_zen_free_survives_hold_expiry(self):
        excl = live_like_excl()
        excl["zen-free"] = {"durable": True, "until": "2026-10-17T00:00:00Z",
                            "reason": "FreeTierError"}
        accs = fc.apply_exclusions(accounts(), excl, AFTER_HOLD)
        self.assertEqual(accs[0]["health"], "unavailable")
        # ... while the untouched expiring model entries lapse as before.
        self.assertIn("muse-spark-1.2-contributor", accs[2]["models"])

    def test_durable_model_entry_drops_everywhere(self):
        excl = {"model:ling-3.1-flash-free": {"durable": True, "reason": "p"}}
        accs = fc.apply_exclusions(accounts(), excl, AFTER_HOLD)
        self.assertNotIn("ling-3.1-flash-free", accs[0]["models"])
        self.assertIn("mimo-v2.6-flash-free", accs[0]["models"])

    def test_invalid_durable_string_re_admits_after_expiry(self):
        # A no-expiry JSON edit WITHOUT the boolean contract must NOT
        # silently stay excluded: it lapses like any expired entry.
        excl = {"zen-free": {"durable": "true", "reason": "hand edit"}}
        accs = fc.apply_exclusions(accounts(), excl, NOW)
        self.assertEqual(accs[0]["health"], "healthy")
        self.assertNotIn("excludedReason", accs[0])

    def test_accountkey_beats_provider_lookup(self):
        excl = {"codex-lane-1": {"until": FUTURE, "reason": "lane r"}}
        accs = fc.apply_exclusions(accounts(), excl, NOW)
        self.assertEqual(accs[1]["health"], "unavailable")
        self.assertEqual(accs[1]["excludedReason"], "lane r")

    def test_reason_truncated_to_200(self):
        excl = {"zen-free": {"until": FUTURE, "reason": "r" * 500}}
        accs = fc.apply_exclusions(accounts(), excl, NOW)
        self.assertEqual(len(accs[0]["excludedReason"]), 200)

    def test_excl_map_not_mutated(self):
        excl = live_like_excl()
        before = copy.deepcopy(excl)
        fc.apply_exclusions(accounts(), excl, NOW)
        self.assertEqual(excl, before)

    def test_non_dict_excl_is_noop(self):
        accs = accounts()
        before = copy.deepcopy(accs)
        self.assertIs(fc.apply_exclusions(accs, None, NOW), accs)
        self.assertEqual(accs, before)

    def test_every_unrelated_entry_preserved(self):
        excl = live_like_excl()
        excl["other-prov"] = {"until": FUTURE, "reason": "unrelated"}
        accs = accounts()
        accs.append({"lane": "o", "provider": "other-prov", "accountKey": "o",
                     "health": "healthy", "models": ["m1"]})
        out = fc.apply_exclusions(accs, excl, NOW)
        self.assertEqual(out[3]["health"], "unavailable")
        self.assertEqual(out[1]["health"], "healthy")


# ---------------------------------------------------------------------------
# Patch pin: the host diff must carry these functions verbatim
# ---------------------------------------------------------------------------

PINNED_FUNCTIONS = (
    "ServedModelsError",
    "served_bearer",
    "extract_served_ids",
    "fetch_served_models",
    "attach_served_models",
)


class HostPatchPin(unittest.TestCase):
    @classmethod
    def added_lines(cls):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host_patch.diff")
        with open(path) as f:
            return [l[1:] for l in f.read().splitlines() if l.startswith("+") and not l.startswith("+++")]

    def test_patch_exists_and_names_target_sha(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host_patch.diff")
        with open(path) as f:
            head = f.read(2000)
        self.assertIn("Target: ops/cliproxy-live/live_accounts.py", head)
        self.assertIn("93a99797b20726182f14938b394f58c4ec6b3adcc87fdfc7bcce787e465bf75e", head)
        self.assertIn("Before byte count: 42536", head)
        self.assertIn("servedModels oracle only", head)

    def test_patch_does_not_replace_live_behavior(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host_patch.diff")
        with open(path) as f:
            patch = f.read()
        self.assertNotRegex(patch, r"(?m)^-[^-]")
        self.assertIn("preserves plan-aware 0.2.46", patch.lower())
        self.assertIn("pace_guard", patch)
        self.assertIn("detect_free_resets", patch)
        self.assertIn("freeResets", patch)

    def test_pinned_functions_verbatim_in_patch(self):
        normed = [re.sub(r"[ \t]+", " ", l).strip() for l in self.added_lines()]
        for name in PINNED_FUNCTIONS:
            src = inspect.getsource(getattr(fc, name))
            # Module uses _-aliased stdlib imports; the patch runs inside the
            # host file, so compare with the host-side import names mapped.
            host_src = (
                src.replace("_dt.", "dt.")
                .replace("_json.", "json.")
                .replace("_time.", "time.")
                .replace("_urllib_request.", "urllib.request.")
            )
            want = [l for l in
                    (re.sub(r"[ \t]+", " ", s).strip() for s in host_src.splitlines())
                    if l and not l.startswith("#")]
            self.assertIn(
                want[0], normed,
                msg="def %s missing from host_patch.diff" % name,
            )
            for line in want[1:]:
                self.assertIn(line, normed,
                              msg="%s: patch diverged at %r" % (name, line))


class HostPatchHunkStructure(unittest.TestCase):
    def test_hunk_headers_match_bodies(self):
        # Guards the install artifact against stale @@ counts/starts: a
        # later edit to a pinned function must update the hunk headers or
        # this fails (patch/git-apply reject such files as malformed).
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host_patch.diff")
        with open(path) as f:
            lines = f.read().splitlines()
        header_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        headers = [(i, l) for i, l in enumerate(lines) if l.startswith("@@")]
        self.assertGreaterEqual(len(headers), 1, "no hunks in host_patch.diff")
        cumulative = 0
        for idx, (lineno, header) in enumerate(headers):
            m = header_re.match(header)
            self.assertIsNotNone(m, msg="bad hunk header: %r" % header)
            old_start, old_count, new_start, new_count = m.groups()
            old_start = int(old_start)
            old_count = int(old_count or 1)
            new_start = int(new_start)
            new_count = int(new_count or 1)
            end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
            body = [l for l in lines[lineno + 1:end] if not l.startswith("\\")]
            old = sum(1 for l in body if l.startswith(" ") or l.startswith("-"))
            new = sum(1 for l in body if l.startswith(" ") or l.startswith("+"))
            self.assertEqual(
                old, old_count,
                msg="hunk %d old count: header says %d, body has %d"
                % (idx + 1, old_count, old),
            )
            self.assertEqual(
                new, new_count,
                msg="hunk %d new count: header says %d, body has %d"
                % (idx + 1, new_count, new),
            )
            expected_start = old_start + cumulative + (1 if old_count == 0 else 0)
            self.assertEqual(
                new_start, expected_start,
                msg="hunk %d new start: header says %d, want %d"
                % (idx + 1, new_start, expected_start),
            )
            cumulative += new_count - old_count


if __name__ == "__main__":
    unittest.main()
