import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memcg_pressure as mp

GIB = 1024 ** 3
LIMIT = 40 * GIB


def files(limit=str(LIMIT), current=LIMIT - 1, anon=0, shmem=0, slab=0, stack=0, psi=0.0, cache=24 * GIB):
    return {
        "memory.max": f"{limit}\n",
        "memory.current": f"{current}\n",
        "memory.pressure": f"some avg10=0.00 avg60={psi} avg300=0.00 total=1\nfull avg10=0.00 avg60={psi} avg300=0.00 total=1\n",
        "memory.stat": f"anon {anon}\nfile {cache}\nshmem {shmem}\nslab_unreclaimable {slab}\nkernel_stack {stack}\n",
    }


def sample(**kw):
    table = files(**kw)
    return mp.read_memcg("/cg", lambda path: table[path.rsplit("/", 1)[1]])


class VerdictTest(unittest.TestCase):
    def test_thresholds_are_the_documented_bounds(self):
        # An intentional change updates this test; a silent one fails it.
        self.assertEqual((mp.DENY_RATIO, mp.PSI_FULL_MAX, mp.PER_WORKER_BYTES), (0.60, 5.0, 3 * GIB))

    def test_full_page_cache_with_modest_anon_is_headroom(self):
        # Observed healthy state: memory.current ~ memory.max, anon ~14.8 GiB of 40 GiB.
        verdict = mp.evaluate_memcg(sample(anon=int(14.8 * GIB), cache=24 * GIB))
        self.assertEqual((verdict["allowed"], verdict["reason"]), (True, "headroom"))

    def test_unreclaimable_share_at_the_line_is_denied(self):
        at_line = int(mp.DENY_RATIO * LIMIT)
        self.assertEqual(mp.evaluate_memcg(sample(anon=at_line - 1))["reason"], "headroom")
        self.assertEqual(mp.evaluate_memcg(sample(anon=at_line))["reason"], "memcg_unreclaimable")

    def test_shmem_slab_and_stacks_count_toward_the_line(self):
        parts = int(0.6 * LIMIT) // 4 + 1
        verdict = mp.evaluate_memcg(sample(anon=parts, shmem=parts, slab=parts, stack=parts))
        self.assertEqual(verdict["reason"], "memcg_unreclaimable")

    def test_cgroup_psi_denies_even_with_low_anon(self):
        self.assertEqual(mp.evaluate_memcg(sample(anon=GIB, psi=5.01))["reason"], "memcg_psi")
        self.assertTrue(mp.evaluate_memcg(sample(anon=GIB, psi=5.0))["allowed"])

    def test_workers_fit_below_the_line_and_floor_at_zero(self):
        deny_line = int(mp.DENY_RATIO * LIMIT)
        self.assertEqual(mp.evaluate_memcg(sample(anon=deny_line - 7 * GIB))["workersFit"], 2)
        self.assertEqual(mp.evaluate_memcg(sample(anon=deny_line + GIB))["workersFit"], 0)
        self.assertEqual(mp.evaluate_memcg(sample(anon=deny_line - 7 * GIB), per_worker_bytes=GIB)["workersFit"], 7)

    def test_psi_hold_advertises_no_workers(self):
        verdict = mp.evaluate_memcg(sample(anon=GIB, psi=9.0))
        self.assertEqual((verdict["reason"], verdict["workersFit"]), ("memcg_psi", 0))

    def test_non_positive_worker_budget_is_rejected(self):
        for bad in (0, -GIB, True, 1.5, None):
            with self.subTest(bad=bad), self.assertRaises(mp.MemcgError):
                mp.evaluate_memcg(sample(anon=GIB), per_worker_bytes=bad)

    def test_uncapped_cgroup_ignores_psi(self):
        self.assertEqual(mp.evaluate_memcg(sample(limit="max", psi=90.0))["reason"], "uncapped")

    def test_uncapped_cgroup_is_not_this_guards_concern(self):
        verdict = mp.evaluate_memcg(sample(limit="max", anon=10 * GIB))
        self.assertEqual((verdict["allowed"], verdict["reason"]), (True, "uncapped"))


class FailClosedTest(unittest.TestCase):
    def read(self, table):
        return lambda path: table[path.rsplit("/", 1)[1]]

    def test_malformed_or_missing_inputs_raise(self):
        broken = {
            "negative limit": {"memory.max": "-1\n"},
            "zero limit": {"memory.max": "0\n"},
            "text limit": {"memory.max": "lots\n"},
            "no full row": {"memory.pressure": "some avg60=0.00\n"},
            "psi out of range": {"memory.pressure": "full avg60=101.0\n"},
            "duplicate psi key": {"memory.pressure": "full avg60=90 avg60=0\n"},
            "junk psi token": {"memory.pressure": "full avg60=1 junk\n"},
            "psi nan": {"memory.pressure": "full avg60=nan\n"},
            "missing stat": {"memory.stat": "anon 1\nshmem 1\n"},
            "duplicate stat": {"memory.stat": "anon 1\nanon 2\nshmem 0\nslab_unreclaimable 0\nkernel_stack 0\n"},
            "text stat": {"memory.stat": "anon many\nshmem 0\nslab_unreclaimable 0\nkernel_stack 0\n"},
        }
        for name, override in broken.items():
            with self.subTest(name=name), self.assertRaises(mp.MemcgError):
                mp.read_memcg("/cg", self.read({**files(), **override}))

    def test_unreadable_directory_raises(self):
        with self.assertRaises(mp.MemcgError):
            mp.read_memcg("/nonexistent-cgroup")

    def test_evaluate_rejects_bad_samples(self):
        good = sample(anon=GIB)
        for bad in (None, [], {**good, "anon": True}, {**good, "anon": -1}, {**good, "limit": 0},
                    {**good, "psiFullAvg60": None}, {k: v for k, v in good.items() if k != "shmem"}):
            with self.subTest(bad=bad), self.assertRaises(mp.MemcgError):
                mp.evaluate_memcg(bad)


class CliTest(unittest.TestCase):
    def run_cli(self, argv, **kw):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            for name, text in files(**kw).items():
                (Path(tmp) / name).write_text(text)
            code = mp.main(["--dir", tmp] + argv)
        return code, out.getvalue()

    def test_exit_codes_and_json(self):
        code, out = self.run_cli([], anon=GIB)
        self.assertEqual((code, json.loads(out)["reason"]), (0, "headroom"))
        code, out = self.run_cli([], anon=30 * GIB)
        self.assertEqual((code, json.loads(out)["reason"]), (1, "memcg_unreclaimable"))

    def test_per_worker_override_and_usage_errors(self):
        code, out = self.run_cli(["--per-worker-gib", "1"], anon=GIB)
        self.assertEqual((code, json.loads(out)["workersFit"]), (0, 23))
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["--bogus"], ["--per-worker-gib"], ["--per-worker-gib", "0"], ["--per-worker-gib", "inf"]):
                self.assertEqual(mp.main(argv), 2)

    def test_unreadable_telemetry_exits_two(self):
        with mock.patch.object(mp, "read_memcg", side_effect=mp.MemcgError("gone")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mp.main([]), 2)
        self.assertEqual(json.loads(out.getvalue())["reason"], "unreadable")


if __name__ == "__main__":
    unittest.main()
