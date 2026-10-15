#!/usr/bin/env python3
"""Offline arithmetic/refusal fixtures, not captured host namespace evidence."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "host-ops" / "spool_mapping.py"
SPEC = importlib.util.spec_from_file_location("spool_mapping", SCRIPT)
mapping = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mapping
SPEC.loader.exec_module(mapping)

# Synthetic rootless observation: host ubuntu maps to namespace root, while
# container node 1000 maps to host 100999. This is not a deployed-host capture.
BASE = "         0       1000          1\n         1     100000      65536\n"
# Synthetic explicitly delegated supplementary group, not an installer grant.
GROUPS = "0 1000 1\n1 100000 1999\n2000 995 1\n2001 102000 63536\n"


class MappingTests(unittest.TestCase):
    def test_direct_namespace_mapping(self):
        extents = mapping.parse_map(BASE)
        self.assertEqual(mapping.to_parent(0, extents), 1000)
        self.assertEqual(mapping.to_parent(1, extents), 100000)
        self.assertEqual(mapping.to_parent(1000, extents), 100999)
        self.assertEqual(mapping.to_parent(65536, extents), 165535)
        with self.assertRaises(mapping.Refusal):
            mapping.to_parent(65537, extents)

    def test_nested_mapping_composes_in_order(self):
        child = mapping.parse_map("0 0 1\n1000 42 1\n")
        parent = mapping.parse_map("0 1000 1\n42 100999 1\n")
        identity = mapping.requester_identity([child, parent], [child, parent])
        self.assertEqual(identity, {"hostUid": 100999, "hostGids": [100999]})
        with self.assertRaises(mapping.Refusal):
            mapping.requester_identity([parent, child], [parent, child])

    def test_shared_namespace_is_not_a_second_layer(self):
        extents = mapping.parse_map(BASE)
        with self.assertRaises(mapping.Refusal):
            mapping.requester_identity([extents, extents], [extents, extents])

    def test_adjacent_ranges_and_last_valid_id(self):
        extents = mapping.parse_map(f"0 2 2\n2 0 2\n4 {mapping.MAX_ID} 1\n")
        self.assertEqual(mapping.to_parent(4, extents), mapping.MAX_ID)
        self.assertEqual(mapping.to_parent(3, extents), 1)

    def test_invalid_rows_and_ranges(self):
        cases = ["", "\n", "0 0 1\n\n", "0 0 0", "-1 0 1", "0 +1 1", "0 0 1 # hi",
                 "0 0 1 2", "0 0", "0\v0\v1", "0 0 1\x00", "０ 0 1",
                 "0 0 4294967296", "4294967295 0 1", "0 4294967295 1",
                 "4294967294 0 2", "0 4294967294 2", "0 0 1\n" * 341,
                 " " * (mapping.MAX_BYTES + 1)]
        for text in cases:
            with self.subTest(text=text[:80]), self.assertRaises(mapping.Refusal):
                mapping.parse_map(text)

    def test_overlaps_on_either_axis(self):
        for text in ("0 100 5\n4 200 1", "0 100 5\n9 104 2", "0 1 1\n0 2 1"):
            with self.subTest(text=text), self.assertRaises(mapping.Refusal):
                mapping.parse_map(text)

    def test_numeric_ids_do_not_accept_booleans_or_strings(self):
        for value in (True, False, "1000", 1.0, -1, 2**32 - 1):
            with self.subTest(value=value), self.assertRaises(mapping.Refusal):
                mapping.to_parent(value, mapping.parse_map(BASE))

    def test_chain_is_explicit_bounded_and_symmetric(self):
        extents = mapping.parse_map(BASE)
        for chain in ([], [extents] * 9):
            with self.assertRaises(mapping.Refusal):
                mapping.to_host(1000, chain)
        with self.assertRaises(mapping.Refusal):
            mapping.requester_identity([extents], [extents, extents])

    def test_keep_id_or_different_subuid_is_not_expected_node_mapping(self):
        for text in ("0 100000 1000\n1000 1000 1", "0 200000 65536"):
            extents = mapping.parse_map(text)
            with self.subTest(text=text), self.assertRaises(mapping.Refusal):
                mapping.requester_identity([extents], [extents])

    def test_uid_and_gid_maps_are_not_interchangeable(self):
        identity = mapping.requester_identity([mapping.parse_map(BASE)],
                                             [mapping.parse_map("0 200000 65536")])
        self.assertEqual(identity["hostUid"], 100999)
        self.assertEqual(identity["hostGids"], [201000])

    def test_default_rootless_map_does_not_grant_host_group(self):
        extents = mapping.parse_map(BASE)
        identity = mapping.requester_identity([extents], [extents], supplementary=[995])
        self.assertEqual(identity["hostGids"], [100994, 100999])
        with self.assertRaises(mapping.Refusal):
            mapping.require_group_access(identity, broker_uid=994, requests_gid=995)

    def test_explicit_group_mapping_and_observed_membership_are_both_needed(self):
        uid_maps, gid_maps = [mapping.parse_map(BASE)], [mapping.parse_map(GROUPS)]
        identity = mapping.requester_identity(uid_maps, gid_maps)
        with self.assertRaises(mapping.Refusal):
            mapping.require_group_access(identity, 994, 995)
        identity = mapping.requester_identity(uid_maps, gid_maps, supplementary=[2000, 2000])
        self.assertEqual(identity["hostGids"], [995, 100999])
        mapping.require_group_access(identity, 994, 995)

    def test_unmapped_supplementary_group_and_host_root_refuse(self):
        extents = mapping.parse_map(BASE)
        with self.assertRaises(mapping.Refusal):
            mapping.requester_identity([extents], [extents], supplementary=[999999])
        with self.assertRaises(mapping.Refusal):
            mapping.requester_identity([extents], [mapping.parse_map("1000 0 1")])

    def test_invalid_broker_or_request_group_refuses(self):
        identity = {"hostUid": 100999, "hostGids": [995]}
        for uid, gid in ((0, 995), (100999, 995), (994, 0), (994, 100999)):
            with self.subTest(uid=uid, gid=gid), self.assertRaises(mapping.Refusal):
                mapping.require_group_access(identity, uid, gid)

    def test_capture_refuses_symlink_and_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, link, fifo = (Path(tmp) / name for name in ("map", "link", "fifo"))
            source.write_text(BASE)
            self.assertEqual(mapping.read_map(source), mapping.parse_map(BASE))
            link.symlink_to(source)
            os.mkfifo(fifo)
            for path in (link, fifo):
                result = subprocess.run([sys.executable, str(SCRIPT), "--uid-map", str(path),
                                         "--gid-map", str(source), "--broker-uid", "994",
                                         "--requests-gid", "995"], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")

    def test_cli_positive_and_negative_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            uid, gid = Path(tmp) / "uid-map", Path(tmp) / "gid-map"
            uid.write_text(BASE)
            gid.write_text(GROUPS)
            argv = [sys.executable, str(SCRIPT), "--uid-map", str(uid), "--gid-map", str(gid),
                    "--broker-uid", "994", "--requests-gid", "995"]
            result = subprocess.run(argv + ["--supplementary-gid", "2000"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["identity"], {"hostUid": 100999, "hostGids": [995, 100999]})
            self.assertFalse(data["hostAcceptance"])
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            gid.write_bytes(b"\xff")
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(str(gid), result.stderr)
            gid.write_bytes(b"1" * (mapping.MAX_BYTES + 1))
            self.assertEqual(subprocess.run(argv, capture_output=True).returncode, 2)
            gid.unlink()
            self.assertEqual(subprocess.run(argv, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
