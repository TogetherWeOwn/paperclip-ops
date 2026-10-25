"""Acceptance: discoverable docs + versioned registry for verify-fast.

Builder contract under test (paired card):
* helper gains --emit-registry-json, --emit-docs, --check-docs
  (checks docs/verify-fast-registry.json AND docs/verify-fast-registry.md
  relative to inventory root), --expect-revision <rev> (only with new modes).
* new registry modes read-only: no probes/commands/network/writes/prep.
* --inventory already supported.
* schema {schema_version:1, registry_revision:'sha256:'+64 lowerhex,
  complete:true, total, limit:256, suites:[id,tier,description,command,
  prerequisites,preparation], prerequisite_catalog}.
* revision deterministic across processes; changes on canonical contract
  or helper/wrapper source changes; _maintainer_note excluded; whitespace
  insensitive. Validates schema_version==1 (bool rejected), nonempty
  suites<=256, unique valid ids, tier parse/fast, nonempty str/argv command,
  prereq names in catalog, preparation.actions list[str]; reject malformed
  /overflow without partial stdout. Inputs/outputs <=256KiB.
* --expect-revision mismatch nonzero with stale/remediation text.
* --check-docs byte-identical; fail on absent/stale md OR json.
* no tier-filtered registry/docs. Existing outputs unchanged.
* wrapper adds --registry/--docs/--check-docs, early read-only before
  build_plan, mutually exclusive with --plan/--list/--no-prepare/--parse-only
  (and --json unless registry allows).

Run: python3 -B -m unittest -v test_verify_fast_registry
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / "scripts" / "verify-fast-plan.py"
INVENTORY = ROOT / "scripts" / "verify-fast-inventory.json"
WRAPPER = ROOT / "scripts" / "verify-fast.sh"

TIMEOUT = 15
MAX_BYTES = 256 * 1024
SUITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
REV_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def run_helper(args, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, "-B", str(HELPER), *args],
        capture_output=True, text=True, timeout=TIMEOUT,
        cwd=str(cwd or ROOT), env=env or dict(os.environ),
    )


def run_wrapper(args, cwd=None, env=None):
    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        capture_output=True, text=True, timeout=TIMEOUT,
        cwd=str(cwd or ROOT), env=env or dict(os.environ),
    )


def make_temproot_with_scripts():
    """temp-root/{scripts/*,docs/} outside repo for layout-faithful fixtures.

    Mirrors the repo layout so inventory-root-relative docs resolution
    (inventory at <root>/scripts/*.json -> docs at <root>/docs/) behaves
    exactly as in production. Copies helper+wrapper, plus the factored
    stdlib library scripts/verify_fast_registry.py when it exists, so
    source-change fixtures keep lazy imports working.
    """
    tmp = Path(tempfile.mkdtemp())
    scripts = tmp / "scripts"
    scripts.mkdir()
    (scripts / "verify-fast-inventory.json").write_text(INVENTORY.read_text())
    shutil.copy(HELPER, scripts / "verify-fast-plan.py")
    shutil.copy(WRAPPER, scripts / "verify-fast.sh")
    lib = ROOT / "scripts" / "verify_fast_registry.py"
    if lib.is_file():
        shutil.copy(lib, scripts / "verify_fast_registry.py")
    (tmp / "docs").mkdir(exist_ok=True)
    return tmp


def flip_one_byte(text):
    """Flip a single byte; keeps content parseable but never byte-identical."""
    pos = min(10, len(text) - 1)
    ch = "X" if text[pos] != "X" else "Y"
    return text[:pos] + ch + text[pos + 1:]


def tmp_inventory_copy(extra_mutation=None):
    """Copy repo inventory to a temp dir outside repo; return (dir, path)."""
    tmp = tempfile.mkdtemp()
    dst = Path(tmp) / "verify-fast-inventory.json"
    data = json.loads(INVENTORY.read_text())
    if extra_mutation:
        extra_mutation(data)
    dst.write_text(json.dumps(data, indent=2))
    return tmp, dst


class RegistrySchemaTest(unittest.TestCase):
    def test_emit_registry_json_schema_and_bounds(self):
        c = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json"])
        self.assertEqual(c.returncode, 0, f"exit 0: {c.stderr[:400]!r}")
        self.assertLessEqual(len(c.stdout.encode()), MAX_BYTES, "output bounded")
        self.assertLessEqual(len(c.stderr.encode()), MAX_BYTES, "stderr bounded")
        reg = json.loads(c.stdout)
        self.assertIs(reg.get("schema_version"), 1, "schema_version==1 int (bool rejected)")
        self.assertIs(type(reg.get("schema_version")), int)
        self.assertRegex(reg.get("registry_revision", ""), REV_RE)
        self.assertIs(reg.get("complete"), True)
        self.assertEqual(reg.get("limit"), 256)
        suites = reg.get("suites")
        self.assertIsInstance(suites, list)
        self.assertTrue(0 < len(suites) <= 256)
        self.assertEqual(reg.get("total"), len(suites))
        catalog = reg.get("prerequisite_catalog")
        self.assertIsInstance(catalog, dict)
        for s in suites:
            with self.subTest(suite=s.get("id")):
                self.assertRegex(s.get("id", ""), SUITE_ID_RE)
                self.assertIn(s.get("tier"), ("parse", "fast"))
                self.assertIsInstance(s.get("description", ""), str)
                self.assertTrue(s["description"].strip(), "description nonempty")
                cmd = s.get("command")
                ok = (isinstance(cmd, str) and cmd.strip()) or (
                    isinstance(cmd, list) and cmd and all(isinstance(x, str) and x for x in cmd))
                self.assertTrue(ok, "nonempty str or argv-list command")
                self.assertIsInstance(s.get("prerequisites"), list)
                for n in s["prerequisites"]:
                    self.assertIsInstance(n, str)
                    self.assertIn(n, catalog, f"prereq {n!r} in catalog")
                prep = s.get("preparation")
                self.assertIsInstance(prep, dict)
                self.assertIsInstance(prep.get("actions"), list)
                for a in prep["actions"]:
                    self.assertIsInstance(a, str)


class RegistryReadonlyTest(unittest.TestCase):
    def test_registry_mode_runs_no_probes(self):
        marker = Path(tempfile.mkdtemp()) / "probe-marker"
        def mutate(d):
            d["prerequisite_catalog"]["probe-spy"] = {
                "probe": {"argv": ["python3", "-c", f"open({str(marker)!r},'w').write('x')"]}}
            d["suites"] = [dict(d["suites"][0], prerequisites=["probe-spy"])]
        tmp, inv = tmp_inventory_copy(mutate)
        try:
            c = run_helper(["--inventory", str(inv), "--emit-registry-json"])
            self.assertEqual(c.returncode, 0, f"exit 0: {c.stderr[:400]!r}")
            self.assertFalse(marker.exists(), "registry mode must not run probes")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(str(marker.parent), ignore_errors=True)

    def test_revision_deterministic_across_processes(self):
        a = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json"])
        b = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json"])
        self.assertEqual(a.returncode, 0)
        self.assertEqual(b.returncode, 0)
        ra = json.loads(a.stdout)["registry_revision"]
        rb = json.loads(b.stdout)["registry_revision"]
        self.assertEqual(ra, rb, "revision deterministic across fresh processes")

    def test_revision_ignores_note_and_whitespace_but_tracks_contract(self):
        base = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json"])
        self.assertEqual(base.returncode, 0)
        base_rev = json.loads(base.stdout)["registry_revision"]
        # note-only change + reserialized whitespace: revision must not change
        def note_only(d):
            d["_maintainer_note"] = "completely different note text"
        tmp, inv = tmp_inventory_copy(note_only)
        try:
            c = run_helper(["--inventory", str(inv), "--emit-registry-json"])
            self.assertEqual(c.returncode, 0)
            self.assertEqual(json.loads(c.stdout)["registry_revision"], base_rev,
                             "note/whitespace must not change revision")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # canonical contract change: revision must change
        def cmd_change(d):
            d["suites"][0] = dict(d["suites"][0], command="echo changed-contract")
        tmp2, inv2 = tmp_inventory_copy(cmd_change)
        try:
            c2 = run_helper(["--inventory", str(inv2), "--emit-registry-json"])
            self.assertEqual(c2.returncode, 0)
            self.assertNotEqual(json.loads(c2.stdout)["registry_revision"], base_rev,
                               "contract change must change revision")
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)


    def test_revision_invalidated_by_helper_or_wrapper_source_change(self):
        tmp = make_temproot_with_scripts()
        try:
            inv = tmp / "scripts" / "verify-fast-inventory.json"
            helper = tmp / "scripts" / "verify-fast-plan.py"

            def rev():
                c = subprocess.run(
                    [sys.executable, "-B", str(helper),
                     "--inventory", str(inv), "--emit-registry-json"],
                    capture_output=True, text=True, timeout=TIMEOUT)
                self.assertEqual(c.returncode, 0,
                                 f"need registry exit 0: {c.stderr[:300]!r}")
                return json.loads(c.stdout)["registry_revision"]

            base = rev()
            hsrc = helper.read_text()
            helper.write_text(
                hsrc + "\n# registry-source-probe: helper change must invalidate\n")
            try:
                self.assertNotEqual(rev(), base,
                                    "helper source change must invalidate revision")
            finally:
                helper.write_text(hsrc)
            wrapper = tmp / "scripts" / "verify-fast.sh"
            wsrc = wrapper.read_text()
            wrapper.write_text(
                wsrc + "\n# registry-source-probe: wrapper change must invalidate\n")
            try:
                self.assertNotEqual(rev(), base,
                                    "wrapper source change must invalidate revision")
            finally:
                wrapper.write_text(wsrc)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RegistryValidationTest(unittest.TestCase):
    def _expect_reject(self, mutate):
        tmp, inv = tmp_inventory_copy(mutate)
        try:
            c = run_helper(["--inventory", str(inv), "--emit-registry-json"])
            self.assertNotEqual(c.returncode, 0, "malformed must exit nonzero")
            self.assertTrue(
                not c.stdout.strip() or "registry_revision" not in c.stdout,
                "no partial registry stdout on reject")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rejects_malformed_inventories(self):
        self._expect_reject(lambda d: d.update(schema_version=True))
        self._expect_reject(lambda d: d.update(schema_version="1"))
        self._expect_reject(lambda d: d.update(schema_version=2))
        self._expect_reject(lambda d: d.update(suites=[]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], id="DUPLICATE"),
            dict(d["suites"][1], id="DUPLICATE")]))
        # same-size lowercase duplicate: only uniqueness can catch it
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0]),
            dict(d["suites"][1], id=d["suites"][0]["id"])]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], id="BAD ID!")]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], tier="slow")]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], command="")]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], command=[])]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], command=["ok", 123])]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], prerequisites=["no-such-prereq"])]))
        self._expect_reject(lambda d: d.update(prerequisite_catalog=[]))
        self._expect_reject(lambda d: d.pop("prerequisite_catalog", None))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], preparation={"actions": "not-a-list"})]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], preparation={"actions": ["ok", 123]})]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], preparation="sync-toolkit.sh")]))
        self._expect_reject(lambda d: d.update(suites=[
            dict(d["suites"][0], description="")]))

    def test_rejects_overflow_and_oversize_input(self):
        base = json.loads(INVENTORY.read_text())
        def overflow(d):
            d["suites"] = [dict(base["suites"][0], id=f"s{i:04d}") for i in range(257)]
        self._expect_reject(overflow)
        tmp = tempfile.mkdtemp()
        try:
            big = Path(tmp) / "big.json"
            big.write_text(" " * (MAX_BYTES + 1))
            c = run_helper(["--inventory", str(big), "--emit-registry-json"])
            self.assertNotEqual(c.returncode, 0, "oversize input must exit nonzero")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ExpectRevisionTest(unittest.TestCase):
    def test_expect_revision_match_and_mismatch(self):
        base = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json"])
        self.assertEqual(base.returncode, 0)
        rev = json.loads(base.stdout)["registry_revision"]
        ok = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json",
                         "--expect-revision", rev])
        self.assertEqual(ok.returncode, 0, "matching revision exits 0")
        bad = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json",
                          "--expect-revision", "sha256:" + "0" * 64])
        self.assertNotEqual(bad.returncode, 0, "mismatch exits nonzero")
        combined = (bad.stdout + "\n" + bad.stderr).lower()
        self.assertRegex(combined, re.compile(r"stale|remediat|regenerat|mismatch|expect"),
                         "mismatch must explain staleness/remediation")

    def test_expect_revision_only_valid_with_registry_docs_modes(self):
        c = run_helper(["--inventory", str(INVENTORY), "--emit-json",
                        "--expect-revision", "sha256:" + "0" * 64])
        self.assertNotEqual(c.returncode, 0,
                            "--expect-revision without registry/docs mode must fail")


class DocsCheckTest(unittest.TestCase):
    def test_check_docs_fails_when_absent_or_stale(self):
        tmp, inv = tmp_inventory_copy()
        try:
            docs = inv.parent  # inventory root stand-in: docs/ beside inventory
            # absent docs must fail nonzero (no stdout registry leak)
            c = run_helper(["--inventory", str(inv), "--check-docs"])
            self.assertNotEqual(c.returncode, 0, "absent docs must fail")
            # stale docs must also fail byte-identically
            (Path(tmp) / "docs").mkdir(exist_ok=True)
            (Path(tmp) / "docs" / "verify-fast-registry.json").write_text('{"stale":true}')
            (Path(tmp) / "docs" / "verify-fast-registry.md").write_text('# stale\n')
            c2 = run_helper(["--inventory", str(inv), "--check-docs"])
            _ = docs
            self.assertNotEqual(c2.returncode, 0, "stale docs must fail")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_check_docs_accepts_generated_exact_docs_and_rejects_single_mutation(self):
        # Catches a checker that always refuses: generated byte-exact docs
        # under temp-root/scripts+docs must pass, and mutating EITHER doc
        # alone (one flipped byte) must fail; restoring must pass again.
        tmp = make_temproot_with_scripts()
        try:
            inv = tmp / "scripts" / "verify-fast-inventory.json"
            helper = tmp / "scripts" / "verify-fast-plan.py"
            docs = tmp / "docs"

            def run(*args):
                return subprocess.run(
                    [sys.executable, "-B", str(helper), *args],
                    capture_output=True, text=True, timeout=TIMEOUT)

            rj = run("--inventory", str(inv), "--emit-registry-json")
            self.assertEqual(rj.returncode, 0,
                             f"need registry exit 0: {rj.stderr[:300]!r}")
            (docs / "verify-fast-registry.json").write_text(rj.stdout)
            md = run("--inventory", str(inv), "--emit-docs")
            self.assertEqual(md.returncode, 0,
                             f"need docs exit 0: {md.stderr[:300]!r}")
            (docs / "verify-fast-registry.md").write_text(md.stdout)
            ok = run("--inventory", str(inv), "--check-docs")
            self.assertEqual(ok.returncode, 0,
                             "exact generated JSON+MD must pass --check-docs")
            raw_json = (docs / "verify-fast-registry.json").read_text()
            (docs / "verify-fast-registry.json").write_text(flip_one_byte(raw_json))
            bad_json = run("--inventory", str(inv), "--check-docs")
            self.assertNotEqual(bad_json.returncode, 0,
                                "mutated JSON doc alone must fail --check-docs")
            (docs / "verify-fast-registry.json").write_text(raw_json)
            raw_md = (docs / "verify-fast-registry.md").read_text()
            (docs / "verify-fast-registry.md").write_text(flip_one_byte(raw_md))
            bad_md = run("--inventory", str(inv), "--check-docs")
            self.assertNotEqual(bad_md.returncode, 0,
                                "mutated MD doc alone must fail --check-docs")
            (docs / "verify-fast-registry.md").write_text(raw_md)
            again = run("--inventory", str(inv), "--check-docs")
            self.assertEqual(again.returncode, 0,
                             "restored docs must pass --check-docs again")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_emit_docs_bounded_and_no_tier_filter(self):
        c = run_helper(["--inventory", str(INVENTORY), "--emit-docs"])
        self.assertEqual(c.returncode, 0, f"exit 0: {c.stderr[:400]!r}")
        self.assertLessEqual(len(c.stdout.encode()), MAX_BYTES, "docs bounded")
        self.assertTrue(c.stdout.strip(), "docs markdown nonempty")
        t = run_helper(["--inventory", str(INVENTORY), "--emit-registry-json",
                        "--tier", "parse"])
        self.assertNotEqual(t.returncode, 0, "tier-filtered registry must fail")
        t2 = run_helper(["--inventory", str(INVENTORY), "--emit-docs",
                         "--tier", "parse"])
        self.assertNotEqual(t2.returncode, 0, "tier-filtered docs must fail")


class WrapperRegistryTest(unittest.TestCase):
    def test_wrapper_registry_docs_check_modes(self):
        r = run_wrapper(["--registry"])
        self.assertEqual(r.returncode, 0, f"--registry exit 0: {r.stderr[:400]!r}")
        reg = json.loads(r.stdout)
        self.assertRegex(reg.get("registry_revision", ""), REV_RE)
        d = run_wrapper(["--docs"])
        self.assertEqual(d.returncode, 0, f"--docs exit 0: {d.stderr[:400]!r}")
        self.assertTrue(d.stdout.strip(), "--docs prints markdown")
        k = run_wrapper(["--check-docs"])
        self.assertIn(k.returncode, (0, 1), "--check-docs exits 0/1")
        self.assertTrue(not k.stdout.strip() or "ok" in k.stdout.lower() or "pass" in k.stdout.lower(),
                        "--check-docs (wrapper) no stdout except optional success")

    def test_wrapper_registry_mutually_exclusive(self):
        for flags in (["--registry", "--plan"], ["--docs", "--list"],
                      ["--registry", "--no-prepare"], ["--docs", "--parse-only"]):
            with self.subTest(flags=flags):
                c = run_wrapper(flags)
                self.assertEqual(c.returncode, 2, f"{flags} must exit 2")


class ExistingOutputsUnchangedTest(unittest.TestCase):
    def test_emit_json_and_list_json_shapes_unchanged(self):
        c = run_helper(["--inventory", str(INVENTORY), "--emit-json"])
        self.assertEqual(c.returncode, 0)
        plan = json.loads(c.stdout)
        self.assertIn("suites", plan)
        self.assertNotIn("registry_revision", plan, "plan output must not leak registry keys")
        self.assertNotIn("prerequisite_catalog", plan)
        l = run_helper(["--inventory", str(INVENTORY), "--emit-list-json"])
        self.assertEqual(l.returncode, 0)
        ids = json.loads(l.stdout)
        self.assertIsInstance(ids, list)
        self.assertTrue(ids, "list-json nonempty")


if __name__ == "__main__":
    unittest.main()
