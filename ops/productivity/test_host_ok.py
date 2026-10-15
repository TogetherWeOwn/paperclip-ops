#!/usr/bin/env python3
"""Offline: host-pressure gate fail-closed behavior. Patched telemetry and disk readers only.

No host, database, API, credentials, or network: every pressure sample and
disk-usage read is a stub, and every caller-denial case asserts its protected
side effects were never invoked.
"""
import collections
import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import host_ok as hk

import backlog_promoter as bp
import pr_supply as ps
import review_error_rerequest as rr

Usage = collections.namedtuple('Usage', ['total', 'used', 'free'])
TOTAL = 10_000


def usage_at(frac):
    used = int(TOTAL * frac)
    return Usage(TOTAL, used, TOTAL - used)


def usage_exact(cap):
    """Usage whose used/total is exactly at cap (never below through float dust)."""
    used = int(cap * TOTAL)
    while used / TOTAL < cap:
        used += 1
    return Usage(TOTAL, used, TOTAL - used)


def pressure_stub(*, read=None, evaluate=None, read_exc=None, eval_exc=None):
    mod = types.ModuleType('host_pressure')
    if read_exc is not None:
        def read_pressure():
            raise read_exc
    else:
        sample = {} if read is None else read

        def read_pressure(_sample=sample):
            return _sample
    if eval_exc is not None:
        def evaluate_pressure(_sample):
            raise eval_exc
    else:
        verdict = {'allowed': True, 'reason': 'headroom'} if evaluate is None else evaluate

        def evaluate_pressure(_sample, _verdict=verdict):
            return dict(_verdict)
    mod.read_pressure = read_pressure
    mod.evaluate_pressure = evaluate_pressure
    return mod


HEALTHY = pressure_stub(evaluate={'allowed': True, 'reason': 'headroom'})


def run_gate(pressure, disks):
    """Run hk.host_ok with a stubbed pressure module and disk reader.

    disks maps a mount to a Usage or to an exception disk_usage should raise.
    Returns (ok, why, disk_calls).
    """
    calls = []

    def fake_du(mount):
        calls.append(mount)
        value = disks[mount]
        if isinstance(value, BaseException):
            raise value
        return value

    with mock.patch.dict(sys.modules, {'host_pressure': pressure}), \
            mock.patch.object(hk.shutil, 'disk_usage', side_effect=fake_du):
        ok, why = hk.host_ok()
    return ok, why, calls


def denied_host(reason='load5'):
    stub = types.ModuleType('host_ok')
    stub.host_ok = lambda: (False, reason)
    return stub


class GateVerdict(unittest.TestCase):
    def test_thresholds_are_the_documented_caps(self):
        # An intentional threshold change updates this test; a silent one fails it.
        self.assertEqual((hk.ROOT_MAX, hk.HOME_MAX), (0.90, 0.93))

    def test_headroom_with_disk_margin_reports_ok(self):
        ok, why, calls = run_gate(HEALTHY, {'/': usage_at(0.5), '/home': usage_at(0.5)})
        self.assertIs(ok, True)
        self.assertEqual(why, 'ok')
        self.assertEqual(calls, ['/', '/home'])

    def test_pressured_telemetry_denies_before_touching_disks(self):
        for reason in ('cpu', 'io', 'memory_pressure', 'load5'):
            with self.subTest(reason=reason):
                pressure = pressure_stub(evaluate={'allowed': False, 'reason': reason})
                ok, why, calls = run_gate(pressure, {'/': usage_at(0.5), '/home': usage_at(0.5)})
                self.assertIs(ok, False)
                self.assertEqual(why, reason)
                self.assertEqual(calls, [])

    def test_verdict_without_allowed_key_denies(self):
        ok, why, calls = run_gate(pressure_stub(evaluate={}), {'/': usage_at(0.5), '/home': usage_at(0.5)})
        self.assertIs(ok, False)
        self.assertEqual(why, 'pressured')
        self.assertEqual(calls, [])

    def test_unreadable_telemetry_denies(self):
        cases = {'read OSError': pressure_stub(read_exc=OSError('no proc')),
                 'read ValueError': pressure_stub(read_exc=ValueError('malformed')),
                 'read RuntimeError': pressure_stub(read_exc=RuntimeError('boom')),
                 'evaluate ValueError': pressure_stub(eval_exc=ValueError('bad sample')),
                 'evaluate RuntimeError': pressure_stub(eval_exc=RuntimeError('boom'))}
        for name, pressure in cases.items():
            with self.subTest(name=name):
                ok, why, calls = run_gate(pressure, {'/': usage_at(0.5), '/home': usage_at(0.5)})
                self.assertIs(ok, False)
                self.assertTrue(why.startswith('pressure unreadable'), why)
                self.assertEqual(calls, [])

    def test_missing_pressure_module_denies(self):
        with mock.patch.dict(sys.modules, {'host_pressure': None}), \
                mock.patch.object(hk.shutil, 'disk_usage') as du:
            ok, why = hk.host_ok()
        self.assertIs(ok, False)
        self.assertTrue(why.startswith('pressure unreadable'), why)
        du.assert_not_called()

    def test_root_usage_at_below_and_above_the_cap(self):
        home = usage_at(0.5)
        ok, _, _ = run_gate(HEALTHY, {'/': usage_at(0.5), '/home': home})
        self.assertIs(ok, True)
        for name, use in (('at', usage_exact(hk.ROOT_MAX)), ('above', usage_at(0.99))):
            with self.subTest(name=name):
                ok, why, _ = run_gate(HEALTHY, {'/': use, '/home': home})
                self.assertIs(ok, False)
                self.assertEqual(why.split(' ')[0], '/')
                self.assertIn('>=', why)

    def test_home_usage_at_below_and_above_the_cap(self):
        root = usage_at(0.5)
        ok, _, _ = run_gate(HEALTHY, {'/': root, '/home': usage_at(0.5)})
        self.assertIs(ok, True)
        for name, use in (('at', usage_exact(hk.HOME_MAX)), ('above', usage_at(0.99))):
            with self.subTest(name=name):
                ok, why, _ = run_gate(HEALTHY, {'/': root, '/home': use})
                self.assertIs(ok, False)
                self.assertEqual(why.split(' ')[0], '/home')
                self.assertIn('>=', why)

    def test_usage_just_below_each_cap_is_allowed(self):
        ok, why, _ = run_gate(HEALTHY, {'/': usage_at(0.89), '/home': usage_at(0.92)})
        self.assertEqual((ok, why), (True, 'ok'))

    def test_unreadable_mount_denies_and_names_the_mount(self):
        for mount in ('/', '/home'):
            with self.subTest(mount=mount):
                other = '/home' if mount == '/' else '/'
                disks = {mount: OSError('permission denied'), other: usage_at(0.5)}
                ok, why, _ = run_gate(HEALTHY, disks)
                self.assertIs(ok, False)
                self.assertTrue(why.startswith(mount), why)
                self.assertIn('unreadable', why)

    def test_result_is_a_bool_pair_with_a_non_empty_reason(self):
        ok, why, _ = run_gate(HEALTHY, {'/': usage_at(0.5), '/home': usage_at(0.5)})
        self.assertIs(type(ok), bool)
        self.assertIsInstance(why, str)
        self.assertTrue(why)


class CallerDenial(unittest.TestCase):
    def test_backlog_promoter_denial_skips_all_reads_and_writes(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {'host_ok': denied_host('load5')}), \
                mock.patch.object(bp, 'APPLY', True), \
                mock.patch.object(bp, 'LOG', Path(tmp, 'operator.log')), \
                mock.patch.object(bp, 'PAUSE', Path(tmp, 'PAUSE')), \
                mock.patch.object(bp, 'psql') as psql, \
                mock.patch.object(bp, 'api') as api, \
                contextlib.redirect_stdout(out):
            self.assertIsNone(bp.main())
        psql.assert_not_called()
        api.assert_not_called()
        self.assertIn('host under pressure', out.getvalue())
        self.assertIn('no new wakes', out.getvalue())

    def test_pr_supply_denial_skips_all_wakes(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {'host_ok': denied_host('io')}), \
                mock.patch.object(ps, 'APPLY', True), \
                mock.patch.object(ps, 'STATE', Path(tmp, 'state.json')), \
                mock.patch.object(ps, 'HELD', Path(tmp, 'held.txt')), \
                mock.patch.object(ps, 'PAUSE', Path(tmp, 'PAUSE')), \
                mock.patch.object(ps, 'LOG', Path(tmp, 'operator.log')), \
                mock.patch.object(ps, 'api') as api, \
                mock.patch.object(rr, 'psql') as psql, \
                mock.patch.object(rr, 'gh_env') as gh_env, \
                mock.patch.object(rr, 'comment') as comment, \
                mock.patch.object(ps.subprocess, 'run') as run, \
                contextlib.redirect_stdout(out):
            self.assertIsNone(ps.main())
        for double in (api, psql, gh_env, comment, run):
            double.assert_not_called()
        self.assertIn('host under pressure', out.getvalue())
        self.assertIn('no new wakes', out.getvalue())
        self.assertFalse(Path(tmp, 'state.json').exists())

    def test_review_rerequest_denial_skips_all_wakes(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {'host_ok': denied_host('disk 97%')}), \
                mock.patch.object(rr, 'APPLY', True), \
                mock.patch.object(rr, 'STATE', Path(tmp, 'state.json')), \
                mock.patch.object(rr, 'LOG', Path(tmp, 'operator.log')), \
                mock.patch.object(rr, 'PARKED', Path(tmp, 'parked.txt')), \
                mock.patch.object(rr, 'psql') as psql, \
                mock.patch.object(rr, 'gh_env') as gh_env, \
                mock.patch.object(rr, 'comment') as comment, \
                mock.patch.object(rr, 'merge_ready') as merge_ready, \
                mock.patch.object(rr, 'unreviewed') as unreviewed, \
                contextlib.redirect_stdout(out):
            self.assertIsNone(rr.main())
        for double in (psql, gh_env, comment, merge_ready, unreviewed):
            double.assert_not_called()
        self.assertIn('host under pressure', out.getvalue())
        self.assertIn('no new wakes', out.getvalue())
        self.assertFalse(Path(tmp, 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
