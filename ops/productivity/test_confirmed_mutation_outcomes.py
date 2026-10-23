#!/usr/bin/env python3
"""Confirmed-mutation-outcomes acceptance (immutable; builder never edits this file).

Three reconcilers must count only confirmed control-plane mutations as completed:
declines, promotions, armed monitors and agent resets require successful transport
AND the endpoint's documented success response. Refusal, 000, nonzero curl exit,
malformed success text or a timeout is never success.

Scope: this file plus minimal synthetic fixture support only. No production edits,
no live dependencies; every subprocess is faked except explicit offline --help /
--selftest invocations (which must exit 0 without touching any store).

Builder contract pinned here (all synthetic, all offline):
- Outcomes separate attempted / confirmed / definite_failure / unknown. HTTP 200
  with exit 0 is confirmed. HTTP 403/500 with exit 0 is definite_failure (never
  unknown, never confirmed). TimeoutExpired / curl exit 28 is unknown (never
  confirmed, never blindly retried in-tick). 000 / exit 7 / malformed / empty is
  never confirmed (definite_failure or unknown, explicit, never blind retry).
- Bounce suppression consumes confirmed promotions only. A definite rejected or
  failed PATCH neither suppresses a later legitimate attempt for 24h nor spends
  successful-promotion capacity. Unknown stays explicit and is attempted at most
  once per target per tick.
- Opt-in `--json-summary` emits compatible human text plus one versioned JSON
  object as the final stdout line (first char `{`). Stable action ids: decline /
  promote / arm_monitor / reset_agent. Per-action outcome in
  {confirmed, definite_failure, unknown}; stable lowercase code; explicit
  next_action (`none` for confirmed; refusal/unknown carry an explicit code that
  is never blind retry). Counts partition attempted. One repo-owned outcome
  module is imported by all three callers (suggested
  ops/productivity/confirmed_outcomes.py; any single module with `outcome` in
  its name shared by all three is accepted) and its VERSION matches every JSON
  summary. No new HTTP framework, credentials, endpoint or retry engine.
- Flags (module-level like existing APPLY, plus runtime argv): keep `--apply`
  with dry-run default; add explicit `--dry-run` (zero control-plane calls and
  zero success-state writes); `--apply` + `--dry-run` refuses (nonzero exit, no
  writes). `--help` works offline (exit 0, no store) and documents confirmed /
  dry-run / json-summary; flow_guard help and docstring state its mutating
  PATCH/step invocation and never claim read-only.
- Preserve pressure / quota / STOP / readiness checks and exact mutation scope.
  Ambiguous write timeouts stay visible without same-tick retry. No secret or
  raw-response leakage in text, JSON or logs.
- One focused offline command: the shared outcome module supports
  `python3 <module> --selftest` (exit 0, offline, prints version/vocabulary).

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/productivity -p 'test_*outcomes.py'
Also: discover for test_connection_intent_sweeper.py, test_backlog_promoter.py,
test_flow_guard.py (existing suites stay green; the builder updates any pin of
the old false-success behaviour outside this file).

Expected red on the pre-fix tree: every `Red` test below fails for the stated
missing-behaviour reason (no JSON line, failure logged as completed, no shared
module, no --help / --dry-run / refusal). Controls stay green.
"""

import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import backlog_promoter as bp
import connection_intent_sweeper as cis
import flow_guard as fg

HERE = Path(__file__).resolve().parent
CALLERS = {
    'connection_intent_sweeper': cis,
    'backlog_promoter': bp,
    'flow_guard': fg,
}

ELIGIBLE_ERR = 'claude_auth_required stream disconnected before completion'
CASE_A = 'case_c0ffee01'
CASE_B = 'case_c0ffee02'
INTENT_A = 'intent-c0ffee01'
INTENT_B = 'intent-c0ffee02'

BLIND_RETRY = {'retry', 'retry_now', 'immediate_retry', 'retry_immediately',
               'blind_retry', 'retry_blindly'}
SECRET_WORDS = ('bearer', 'authorization', 'password', 'api_key', 'apikey')
# `token` travels in tool names (gh_token) so it is only a leak when it looks
# like credential material or echoes a faked raw body.
TOKEN_LEAK = re.compile(r'token\s*[:=]|bearer\s+\S+|api[_-]?key\s*[:=]', re.I)


def done_process(stdout='', returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr='')


def timed_out(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd[0] if isinstance(cmd, list) else cmd,
                                    kwargs.get('timeout'))


def host_ok_stub(result=(True, 'ok')):
    stub = types.ModuleType('host_ok')
    stub.host_ok = lambda: result
    return stub


def no_secret_leak(testcase, text, context):
    lower = text.lower()
    for word in SECRET_WORDS:
        testcase.assertNotIn(word, lower, f'{context} leaks {word!r}')
    testcase.assertIsNone(TOKEN_LEAK.search(text), f'{context} leaks credential material')
    testcase.assertNotIn('<html', lower, f'{context} leaks raw HTML')
    testcase.assertNotIn('raw-body-canary', text, f'{context} echoes raw response')


def extract_json_summary(stdout):
    """Final stdout line starting with `{`, parsed; None when absent."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.lstrip().startswith('{'):
            try:
                return json.loads(line)
            except ValueError:
                return 'malformed-json-line'
    return None


def dry_run_attr(module):
    for name in ('DRY_RUN', 'DRYRUN', 'DRY_RUN_FLAG'):
        if hasattr(module, name):
            return name
    return None


def json_flag_attr(module):
    for name in ('JSON_SUMMARY', 'JSON_SUMMARY_FLAG', 'EMIT_JSON', 'EMIT_JSON_SUMMARY'):
        if hasattr(module, name):
            return name
    return None


def outcome_imports_of(path):
    text = Path(path).read_text()
    found = set()
    for line in text.splitlines():
        low = line.lower()
        if 'outcome' not in low:
            continue
        m = re.match(r'\s*import\s+([\w\.]+)', line)
        if m and 'outcome' in m.group(1).lower():
            found.add(m.group(1).split('.')[0])
            continue
        m = re.match(r'\s*from\s+([\w\.]+)\s+import', line)
        if m and 'outcome' in m.group(1).lower():
            found.add(m.group(1).split('.')[0])
    return found


def shared_outcome_module():
    per_caller = {name: outcome_imports_of(mod.__file__) for name, mod in CALLERS.items()}
    shared = set.intersection(*per_caller.values()) if per_caller else set()
    shared = {m for m in shared if (HERE / f'{m}.py').exists()}
    return shared, per_caller


# ---------------------------------------------------------------- sweeper ---

def run_sweeper(rows, curl_script, *, apply=True, dry_run=False, json_summary=False):
    """curl_script: list of CompletedProcess / TimeoutExpired-raiser, one per curl call."""
    calls = []
    script = list(curl_script)
    out, err = io.StringIO(), io.StringIO()

    def fake_run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        item = script.pop(0) if script else done_process('200')
        if isinstance(item, Exception):
            raise item
        return item

    argv = ['sweeper']
    if apply:
        argv.append('--apply')
    if dry_run:
        argv.append('--dry-run')
    if json_summary:
        argv.append('--json-summary')
    patches = [mock.patch.object(cis, 'APPLY', apply),
               mock.patch.object(cis, 'psql', return_value=[list(r) for r in rows]),
               mock.patch.object(cis.subprocess, 'run', side_effect=fake_run),
               mock.patch.object(sys, 'argv', argv)]
    dname = dry_run_attr(cis)
    if dname:
        patches.append(mock.patch.object(cis, dname, dry_run))
    jname = json_flag_attr(cis)
    if jname:
        patches.append(mock.patch.object(cis, jname, json_summary))
    exit_code = 0
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / 'actions.log'
        patches.append(mock.patch.object(cis, 'LOG', log))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    cis.main()
                except SystemExit as exc:
                    exit_code = exc.code if isinstance(exc.code, int) else 1
        log_text = log.read_text() if log.exists() else ''
        log_exists = log.exists()
    curls = [(a, k) for a, k in calls if a and a[0] == 'curl']
    return {'stdout': out.getvalue(), 'stderr': err.getvalue(), 'calls': calls,
            'curls': curls, 'log': log_text, 'log_exists': log_exists,
            'exit': exit_code}


def sweeper_row(iid=INTENT_A, ident=CASE_A, agent='agent-1', err=ELIGIBLE_ERR):
    return [iid, ident, agent, err]


class SweeperConfirmedOutcomes(unittest.TestCase):
    def test_control_200_with_exit_0_is_a_confirmed_decline(self):
        got = run_sweeper([sweeper_row()], [done_process('200')])
        self.assertEqual(len(got['curls']), 1)
        self.assertIn(f'declined {INTENT_A}', got['stdout'])
        self.assertIn(f'{CASE_A}:200', got['log'])
        self.assertEqual(got['exit'], 0)
        no_secret_leak(self, got['stdout'] + got['log'], 'sweeper 200')

    def test_control_timeout_is_unknown_never_a_decline(self):
        got = run_sweeper([sweeper_row()],
                          [subprocess.TimeoutExpired('curl', cis.CURL_TIMEOUT)])
        self.assertEqual(len(got['curls']), 1)
        self.assertIn(f'FAILED decline {INTENT_A}', got['stdout'])
        self.assertNotIn(f'declined {INTENT_A} on', got['stdout'].replace('FAILED decline', ''))
        self.assertEqual(got['log'], '')
        self.assertFalse(got['log_exists'])
        self.assertNotEqual(got['exit'], 0)

    def test_red_403_is_never_a_decline(self):
        got = run_sweeper([sweeper_row()], [done_process('403')])
        self.assertEqual(len(got['curls']), 1, 'the attempt must happen once')
        self.assertNotIn(f'declined {INTENT_A} on', got['stdout'])
        self.assertNotIn(CASE_A, got['log'], 'a refused decline must not be logged as declined')
        self.assertFalse(got['log_exists'], 'no success-state write on refusal')
        combined = got['stdout'] + got['stderr']
        self.assertTrue('FAILED' in combined or 'failed' in combined.lower() or 'refus' in combined.lower(),
                        'refusal must be visible, not silent success')
        no_secret_leak(self, combined + got['log'], 'sweeper 403')

    def test_red_500_is_never_a_decline(self):
        got = run_sweeper([sweeper_row()], [done_process('500')])
        self.assertNotIn(f'declined {INTENT_A} on', got['stdout'])
        self.assertNotIn(CASE_A, got['log'])
        self.assertFalse(got['log_exists'])

    def test_red_000_empty_and_malformed_are_never_declines(self):
        for stdout, code in (('000', 7), ('', 0), ('OK', 0), ('<html>raw-body-canary', 0)):
            with self.subTest(stdout=stdout, code=code):
                got = run_sweeper([sweeper_row()], [done_process(stdout, code)])
                self.assertNotIn(f'declined {INTENT_A} on', got['stdout'])
                self.assertNotIn(CASE_A, got['log'])
                self.assertFalse(got['log_exists'])
                no_secret_leak(self, got['stdout'] + got['log'] + got['stderr'],
                               f'sweeper malformed {stdout!r}')

    def test_red_nonzero_curl_exit_is_never_a_decline(self):
        for code in (7, 28):
            with self.subTest(exit=code):
                got = run_sweeper([sweeper_row()], [done_process('000', code)])
                self.assertNotIn(f'declined {INTENT_A} on', got['stdout'])
                self.assertNotIn(CASE_A, got['log'])
                self.assertFalse(got['log_exists'])

    def test_red_mixed_batch_confirms_only_the_200(self):
        rows = [sweeper_row(INTENT_A, CASE_A), sweeper_row(INTENT_B, CASE_B)]
        got = run_sweeper(rows, [done_process('200'), done_process('500')])
        self.assertEqual(len(got['curls']), 2)
        self.assertIn(f'declined {INTENT_A}', got['stdout'])
        self.assertNotIn(f'declined {INTENT_B} on', got['stdout'])
        self.assertIn(CASE_A, got['log'])
        self.assertNotIn(CASE_B, got['log'])

    def test_red_secret_like_raw_body_is_never_echoed(self):
        got = run_sweeper([sweeper_row()], [done_process('token=raw-body-canary bearer abc', 0)])
        combined = got['stdout'] + got['stderr'] + got['log']
        self.assertNotIn('raw-body-canary', combined)
        no_secret_leak(self, combined, 'sweeper raw body')


# --------------------------------------------------------------- promoter ---

PROM_CARD_A = ('card-a1', CASE_A, 'Add retry to the deploy step', 'medium', 'agent-1', 'idle')
PROM_CARD_B = ('card-b2', CASE_B, 'Tighten the deploy guard', 'medium', 'agent-1', 'idle')


def run_promoter_tick(*, rows=(PROM_CARD_A,), runnable='0', patch_script=(),
                      log_path=None, tmpdir=None, apply=True, dry_run=False,
                      json_summary=False, pr_state='OPEN'):
    """One mocked promoter tick. patch_script: per-PATCH CompletedProcess stdout/returncode
    tuples, TimeoutExpired instances, or 'timeout' markers. Returns patches/calls/log/stdout."""
    patches, calls = [], []
    script = list(patch_script)

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs)))
        if cmd[0] == 'docker':
            sql = cmd[-1]
            if 'count(*)' in sql:
                return done_process(runnable + '\n')
            if 'from agents' in sql:
                return done_process('agent-1\tag-1\tidle\n')
            if "p.name='TWO Bot Next'" in sql:
                return done_process('\n'.join('\t'.join(r) for r in rows) + ('\n' if rows else ''))
            return done_process('\n')
        if cmd[0].endswith('gh_token.sh'):
            return done_process('tok\n')
        if cmd[0] == 'gh':
            return done_process(pr_state + '\n')
        item = script.pop(0) if script else ('200', 0)
        if isinstance(item, subprocess.TimeoutExpired):
            raise item
        stdout, returncode = item
        target = cmd[cmd.index('-X') + 2].rsplit('/', 1)[1]
        patches.append(target)
        return done_process(stdout, returncode)

    argv = ['promoter']
    if apply:
        argv.append('--apply')
    if dry_run:
        argv.append('--dry-run')
    if json_summary:
        argv.append('--json-summary')
    own_tmp = tmpdir is None
    tmpdir = tmpdir or tempfile.mkdtemp()
    log = log_path or Path(tmpdir, 'operator.log')
    if not log.exists():
        log.write_text('')
    out = io.StringIO()
    error = None
    stack_patches = [
        mock.patch.dict(sys.modules, {'host_ok': host_ok_stub()}),
        mock.patch.dict(bp._pr_cache, clear=True),
        mock.patch.object(bp, 'APPLY', apply),
        mock.patch.object(bp, 'LOG', log),
        mock.patch.object(bp, 'PAUSE', Path(tmpdir, 'PAUSE')),
        mock.patch.object(bp.subprocess, 'run', side_effect=fake_run),
        mock.patch.object(sys, 'argv', argv),
    ]
    dname = dry_run_attr(bp)
    if dname:
        stack_patches.append(mock.patch.object(bp, dname, dry_run))
    jname = json_flag_attr(bp)
    if jname:
        stack_patches.append(mock.patch.object(bp, jname, json_summary))
    with contextlib.ExitStack() as stack:
        for p in stack_patches:
            stack.enter_context(p)
        with contextlib.redirect_stdout(out):
            try:
                bp.main()
            except Exception as exc:  # noqa: BLE001 - harness records it
                error = exc
    result = {'patches': patches, 'calls': calls, 'log': log.read_text(),
              'stdout': out.getvalue(), 'error': error, 'log_path': log, 'tmpdir': tmpdir}
    return result


class PromoterConfirmedOutcomes(unittest.TestCase):
    def test_control_200_promotion_is_confirmed_and_suppresses_bounce(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            first = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                      log_path=log, tmpdir=tmp)
            self.assertIsNone(first['error'])
            self.assertEqual(first['patches'], ['card-a1'])
            self.assertIn(f'{CASE_A} backlog->todo', first['log'])
            second = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                       log_path=log, tmpdir=tmp)
            self.assertIsNone(second['error'])
            self.assertEqual(second['patches'], [],
                             'a confirmed promotion suppresses the bounce tick')

    def test_red_403_never_suppresses_a_later_legitimate_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            first = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('403', 0)],
                                      log_path=log, tmpdir=tmp)
            self.assertIsNone(first['error'])
            self.assertEqual(first['patches'], ['card-a1'], 'the refused attempt happens once')
            second = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                       log_path=log, tmpdir=tmp)
            self.assertIsNone(second['error'])
            self.assertEqual(second['patches'], ['card-a1'],
                             'a refused PATCH must not create 24h bounce suppression')
            self.assertIn(f'{CASE_A} backlog->todo', second['log'])

    def test_red_500_000_malformed_never_create_bounce_suppression(self):
        for stdout, code in (('500', 0), ('000', 7), ('', 0), ('OK', 0)):
            with self.subTest(stdout=stdout, code=code):
                with tempfile.TemporaryDirectory() as tmp:
                    log = Path(tmp, 'operator.log')
                    log.write_text('')
                    first = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[(stdout, code)],
                                              log_path=log, tmpdir=tmp)
                    self.assertIsNone(first['error'])
                    second = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                               log_path=log, tmpdir=tmp)
                    self.assertEqual(second['patches'], ['card-a1'],
                                     f'{stdout!r}/{code} must not suppress the next tick')

    def test_red_timeout_is_unknown_never_confirmed_never_retried_in_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            first = run_promoter_tick(
                rows=(PROM_CARD_A,), log_path=log, tmpdir=tmp,
                patch_script=[subprocess.TimeoutExpired('curl', bp.CURL_TIMEOUT_S)])
            self.assertIsNone(first['error'])
            curl_patches = [c for c, _ in first['calls'] if c and c[0] == 'curl']
            self.assertEqual(len(curl_patches), 1, 'unknown must not be retried in-tick')
            self.assertNotIn(f'{CASE_A} backlog->todo', first['log'].replace('unknown', '').replace('timeout', ''),
                             'a timed-out write must not read as a completed promotion')
            second = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                       log_path=log, tmpdir=tmp)
            self.assertEqual(second['patches'], ['card-a1'],
                             'unknown must not suppress the next legitimate attempt')

    def test_red_definite_failure_does_not_spend_success_capacity(self):
        # One runnable slot left: a refused first card must not consume it.
        need_one = str(bp.TARGET_RUNNABLE - 1)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            got = run_promoter_tick(rows=(PROM_CARD_A, PROM_CARD_B), runnable=need_one,
                                    patch_script=[('403', 0), ('200', 0)],
                                    log_path=log, tmpdir=tmp)
            self.assertIsNone(got['error'])
            self.assertEqual(got['patches'], ['card-a1', 'card-b2'],
                             'after a refusal the next card must still be attempted')
            self.assertIn(CASE_B, got['log'])
            second = run_promoter_tick(rows=(PROM_CARD_B,), patch_script=[('200', 0)],
                                       log_path=log, tmpdir=tmp)
            self.assertEqual(second['patches'], [],
                             'only the confirmed card suppresses')

    def test_red_exit_28_is_unknown_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            got = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('000', 28)],
                                    log_path=log, tmpdir=tmp)
            self.assertIsNone(got['error'])
            second = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                       log_path=log, tmpdir=tmp)
            self.assertEqual(second['patches'], ['card-a1'])

    def test_red_raw_body_is_never_echoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            got = run_promoter_tick(rows=(PROM_CARD_A,),
                                    patch_script=[('token=raw-body-canary', 0)],
                                    log_path=log, tmpdir=tmp)
            combined = got['stdout'] + got['log']
            self.assertNotIn('raw-body-canary', combined)
            no_secret_leak(self, combined, 'promoter raw body')


# -------------------------------------------------------------- flow guard ---

def run_flow_guard(*, loops='card-1\tcase_f10aaa01\t6\n',
                   agents='agent-9\tagent-1\tprocess_lost\n',
                   patch_script=(), apply=True, dry_run=False, json_summary=False):
    calls = []
    script = list(patch_script)
    out = io.StringIO()

    def fake_run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        if args[0] == 'docker':
            sql = kwargs.get('input', '')
            if 'issue_continuation_needed' in sql:
                return done_process(loops)
            return done_process(agents)
        if args[0] == 'curl':
            if 'PATCH' not in args:
                return done_process(json.dumps({'executionPolicy': {'mode': 'normal', 'stages': []}}))
            item = script.pop(0) if script else ('200', 0)
            if isinstance(item, subprocess.TimeoutExpired):
                raise item
            stdout, returncode = item
            return done_process(stdout, returncode)
        return done_process('0 rows\n')

    argv = ['guard']
    if apply:
        argv.append('--apply')
    if dry_run:
        argv.append('--dry-run')
    if json_summary:
        argv.append('--json-summary')
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(fg, 'STATE', tmp), \
            mock.patch.object(fg, 'log') as log, \
            mock.patch.object(fg.subprocess, 'run', side_effect=fake_run), \
            mock.patch.object(sys, 'argv', argv), \
            contextlib.redirect_stdout(out):
        dname = dry_run_attr(fg)
        jname = json_flag_attr(fg)
        with contextlib.ExitStack() as stack:
            if dname:
                stack.enter_context(mock.patch.object(fg, dname, dry_run))
            if jname:
                stack.enter_context(mock.patch.object(fg, jname, json_summary))
            # flow_guard main takes no flag plumbing today; the argv patch above
            # is what the fixed version must honour.
            fg.main()
    messages = [c.args[0] for c in log.call_args_list]
    patch_calls = [a for a, _ in calls if a and a[0] == 'curl' and 'PATCH' in a]
    return {'calls': calls, 'patch_calls': patch_calls, 'log_messages': messages,
            'stdout': out.getvalue()}


class FlowGuardConfirmedOutcomes(unittest.TestCase):
    def test_control_200_arms_and_resets(self):
        got = run_flow_guard(patch_script=[('200', 0), ('200', 0)])
        self.assertEqual(len(got['patch_calls']), 2)
        joined = '\n'.join(got['log_messages'])
        self.assertIn('armed 2h monitor', joined)
        self.assertIn('reset to idle', joined)

    def test_control_patch_timeout_is_unknown_never_armed_or_reset(self):
        boom = subprocess.TimeoutExpired('curl', fg.CURL_TIMEOUT_SECONDS)
        got = run_flow_guard(patch_script=[boom, boom])
        self.assertEqual(len(got['patch_calls']), 2)
        joined = '\n'.join(got['log_messages'])
        self.assertNotIn('armed 2h monitor', joined)
        self.assertNotIn('reset to idle', joined)
        self.assertIn('outcome unknown', joined)

    def test_red_http_failures_never_arm_or_reset(self):
        for stdout, code in (('403', 0), ('500', 0), ('000', 7), ('', 0), ('OK', 0)):
            with self.subTest(stdout=stdout, code=code):
                got = run_flow_guard(patch_script=[(stdout, code), (stdout, code)])
                self.assertEqual(len(got['patch_calls']), 2, 'attempts happen once each')
                joined = '\n'.join(got['log_messages'])
                self.assertNotIn('armed 2h monitor', joined,
                                 f'{stdout!r}/{code} must not read as an armed monitor')
                self.assertNotIn('reset to idle', joined,
                                 f'{stdout!r}/{code} must not read as a reset')

    def test_red_exit_28_is_unknown_never_armed(self):
        got = run_flow_guard(patch_script=[('000', 28), ('000', 28)])
        joined = '\n'.join(got['log_messages'])
        self.assertNotIn('armed 2h monitor', joined)
        self.assertNotIn('reset to idle', joined)

    def test_red_mixed_batch_confirms_only_the_200(self):
        got = run_flow_guard(patch_script=[('200', 0), ('500', 0)])
        joined = '\n'.join(got['log_messages'])
        self.assertIn('armed 2h monitor', joined)
        self.assertNotIn('reset to idle', joined)

    def test_red_raw_body_is_never_echoed(self):
        got = run_flow_guard(patch_script=[('token=raw-body-canary', 0)] * 2)
        combined = '\n'.join(got['log_messages']) + got['stdout']
        self.assertNotIn('raw-body-canary', combined)
        no_secret_leak(self, combined, 'flow guard raw body')


# --------------------------------------- structured summaries + contract ---

def require_json_summary(testcase, stdout, caller):
    summary = extract_json_summary(stdout)
    testcase.assertIsInstance(
        summary, dict,
        f'{caller} --json-summary must print one JSON object as the final stdout line')
    return summary


def check_summary_shape(testcase, summary, caller, expected_actions):
    testcase.assertIn('version', summary)
    testcase.assertTrue(str(summary['version']).strip(), 'version must be non-empty')
    testcase.assertIn('actions', summary)
    testcase.assertIsInstance(summary['actions'], list)
    testcase.assertIn('counts', summary)
    counts = summary['counts']
    for key in ('attempted', 'confirmed', 'definite_failure', 'unknown'):
        testcase.assertIn(key, counts, f'counts.{key} missing')
        testcase.assertIsInstance(counts[key], int)
    testcase.assertEqual(counts['attempted'], len(summary['actions']))
    testcase.assertEqual(counts['attempted'],
                         counts['confirmed'] + counts['definite_failure'] + counts['unknown'],
                         'counts must partition attempted')
    if expected_actions is not None:
        testcase.assertEqual([a.get('action') for a in summary['actions']], expected_actions)
    for action in summary['actions']:
        testcase.assertIn(action.get('outcome'), {'confirmed', 'definite_failure', 'unknown'})
        testcase.assertTrue(str(action.get('target', '')).strip(), 'stable target id required')
        code = str(action.get('code', ''))
        testcase.assertTrue(code and ' ' not in code.strip(), 'stable lowercase code required')
        testcase.assertEqual(code, code.lower(), 'code must be lowercase')
        testcase.assertTrue(str(action.get('next_action', '')).strip(), 'next_action required')
        testcase.assertNotIn(action['next_action'], BLIND_RETRY,
                             'refusal/unknown must never be a blind retry')
        no_secret_leak(testcase, json.dumps(action), f'{caller} action JSON')
    no_secret_leak(testcase, json.dumps(summary), f'{caller} summary JSON')


class StructuredSummariesAndSharedContract(unittest.TestCase):
    def test_red_sweeper_json_summary_partitions_outcomes(self):
        rows = [sweeper_row(INTENT_A, CASE_A), sweeper_row(INTENT_B, CASE_B)]
        got = run_sweeper(rows, [done_process('200'), done_process('403')],
                          json_summary=True)
        summary = require_json_summary(self, got['stdout'], 'sweeper')
        check_summary_shape(self, summary, 'sweeper', ['decline', 'decline'])
        self.assertEqual([a['outcome'] for a in summary['actions']],
                         ['confirmed', 'definite_failure'])
        self.assertEqual(summary['counts'], {'attempted': 2, 'confirmed': 1,
                                             'definite_failure': 1, 'unknown': 0})
        self.assertEqual(summary['actions'][0]['next_action'], 'none')
        self.assertIn(CASE_A, summary['actions'][0]['target'] + INTENT_A)
        self.assertTrue(any(k in summary['actions'][1]['code'] for k in ('403', 'refus', 'forbid', 'denied')),
                        'refusal code must name the refusal')

    def test_red_promoter_json_summary_counts_only_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            got = run_promoter_tick(rows=(PROM_CARD_A, PROM_CARD_B),
                                    patch_script=[('403', 0), ('200', 0)],
                                    log_path=log, tmpdir=tmp, json_summary=True)
        summary = require_json_summary(self, got['stdout'], 'promoter')
        check_summary_shape(self, summary, 'promoter', ['promote', 'promote'])
        self.assertEqual(summary['counts']['confirmed'], 1)
        self.assertEqual(summary['counts']['definite_failure'], 1)
        self.assertEqual(summary['counts']['attempted'], 2)

    def test_red_flow_guard_json_summary_marks_unknown(self):
        boom = subprocess.TimeoutExpired('curl', fg.CURL_TIMEOUT_SECONDS)
        got = run_flow_guard(patch_script=[('200', 0), boom], json_summary=True)
        summary = require_json_summary(self, got['stdout'], 'flow_guard')
        check_summary_shape(self, summary, 'flow_guard', ['arm_monitor', 'reset_agent'])
        by_action = {a['action']: a for a in summary['actions']}
        self.assertEqual(by_action['arm_monitor']['outcome'], 'confirmed')
        self.assertEqual(by_action['reset_agent']['outcome'], 'unknown')
        self.assertNotIn(by_action['reset_agent']['next_action'], BLIND_RETRY | {'none'},
                         'unknown needs an explicit non-blind next_action')
        self.assertIn('timeout', by_action['reset_agent']['code'])

    def test_red_unknown_is_never_blindly_retried_in_json(self):
        got = run_sweeper([sweeper_row()], [done_process('000', 28)], json_summary=True)
        summary = require_json_summary(self, got['stdout'], 'sweeper-unknown')
        self.assertEqual(summary['actions'][0]['outcome'], 'unknown')
        self.assertNotIn(summary['actions'][0]['next_action'], BLIND_RETRY)

    def test_control_no_json_line_without_the_opt_in_flag(self):
        got = run_sweeper([sweeper_row()], [done_process('200')], json_summary=False)
        self.assertIsNone(extract_json_summary(got['stdout']),
                          'JSON appears only with --json-summary')
        self.assertIn(f'declined {INTENT_A}', got['stdout'],
                      'compatible human text stays without the flag')

    def test_red_one_shared_outcome_contract_across_callers(self):
        shared, per_caller = shared_outcome_module()
        self.assertTrue(shared,
                        f'one repo-owned outcome module must be imported by all three callers, got {per_caller}')
        self.assertEqual(len(shared), 1, f'exactly one shared outcome module, got {sorted(shared)}')
        name = sorted(shared)[0]
        mod = __import__(name)
        version = str(getattr(mod, 'VERSION', getattr(mod, 'SCHEMA_VERSION', ''))).strip()
        self.assertTrue(version, 'shared outcome module must expose VERSION')
        versions = set()
        rows = [sweeper_row()]
        got = run_sweeper(rows, [done_process('200')], json_summary=True)
        versions.add(require_json_summary(self, got['stdout'], 'sweeper-version')['version'])
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            got = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                    log_path=log, tmpdir=tmp, json_summary=True)
        versions.add(require_json_summary(self, got['stdout'], 'promoter-version')['version'])
        got = run_flow_guard(patch_script=[('200', 0), ('200', 0)], json_summary=True)
        versions.add(require_json_summary(self, got['stdout'], 'guard-version')['version'])
        self.assertEqual(len(versions), 1, f'one shared JSON version, got {sorted(map(str, versions))}')
        self.assertEqual(str(next(iter(versions))), version,
                         'JSON version must match the shared contract VERSION')

    def test_control_human_text_stays_compatible(self):
        got = run_sweeper([sweeper_row()], [done_process('200')])
        self.assertIn('declined', got['stdout'])
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            promoted = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                         log_path=log, tmpdir=tmp)
        self.assertIn('backlog->todo', promoted['log'])


# --------------------------------------------- flags, help, guards, docs ---

def run_help(path):
    return subprocess.run([sys.executable, str(path), '--help'],
                          capture_output=True, text=True, timeout=10)


class FlagsHelpGuardsAndDocs(unittest.TestCase):
    def test_control_dry_run_by_default_writes_nothing(self):
        got = run_sweeper([sweeper_row()], [], apply=False)
        self.assertEqual(got['curls'], [])
        self.assertFalse(got['log_exists'])
        self.assertIn('would decline', got['stdout'])
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            dry = run_promoter_tick(rows=(PROM_CARD_A,), log_path=log, tmpdir=tmp, apply=False)
        self.assertEqual(dry['patches'], [])
        self.assertEqual(dry['log'], '')

    def test_red_explicit_dry_run_writes_nothing(self):
        self.assertTrue(dry_run_attr(cis) is not None or '--dry-run' in Path(cis.__file__).read_text(),
                        'sweeper must implement explicit --dry-run')
        self.assertTrue(dry_run_attr(bp) is not None or '--dry-run' in Path(bp.__file__).read_text(),
                        'promoter must implement explicit --dry-run')
        got = run_sweeper([sweeper_row()], [], apply=False, dry_run=True)
        self.assertEqual(got['curls'], [])
        self.assertFalse(got['log_exists'])
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            dry = run_promoter_tick(rows=(PROM_CARD_A,), log_path=log, tmpdir=tmp,
                                    apply=False, dry_run=True)
        self.assertEqual(dry['patches'], [])
        self.assertEqual(dry['log'], '')

    def test_red_contradictory_apply_flags_refuse_without_writes(self):
        got = run_sweeper([sweeper_row()], [done_process('200')], apply=True, dry_run=True)
        self.assertNotEqual(got['exit'], 0, 'contradictory --apply --dry-run must refuse')
        self.assertEqual(got['curls'], [], 'refusal must make zero control-plane calls')
        self.assertFalse(got['log_exists'], 'refusal must write zero success state')
        combined = (got['stdout'] + got['stderr']).lower()
        self.assertTrue('refus' in combined or 'contradict' in combined or 'mutually' in combined,
                        'refusal must say why')
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            refused = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                        log_path=log, tmpdir=tmp, apply=True, dry_run=True)
        self.assertEqual(refused['patches'], [])
        self.assertEqual(refused['log'], '')
        self.assertTrue(refused['error'] is None or isinstance(refused['error'], SystemExit))

    def test_red_help_is_offline_and_documents_the_contract(self):
        for name, mod in CALLERS.items():
            with self.subTest(caller=name):
                proc = run_help(mod.__file__)
                self.assertEqual(proc.returncode, 0, f'{name} --help must exit 0 offline: {proc.stderr[:300]}')
                text = proc.stdout.lower()
                for token in ('confirm', 'dry-run', 'json-summary', 'apply'):
                    self.assertIn(token, text, f'{name} --help must document {token}')
                self.assertLess(len(proc.stdout.splitlines()), 200, 'help stays concise')
                no_secret_leak(self, proc.stdout + proc.stderr, f'{name} --help')

    def test_red_flow_guard_documents_its_mutating_invocation(self):
        proc = run_help(fg.__file__)
        self.assertEqual(proc.returncode, 0, f'flow_guard --help must exit 0: {proc.stderr[:300]}')
        blob = (proc.stdout + Path(fg.__file__).read_text()).lower()
        self.assertTrue('mutat' in blob or 'patch' in blob or 'writes' in blob,
                        'flow_guard must state its mutating PATCH/step invocation')
        self.assertNotIn('read-only', blob, 'flow_guard must never claim read-only')
        self.assertNotIn('read only', proc.stdout.lower())

    def test_control_pressure_stop_and_quota_checks_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            pressured = run_promoter_tick(rows=(PROM_CARD_A,), log_path=log, tmpdir=tmp)
            # Baseline helper uses a healthy host stub; now prove the gate is still wired
            # by running main with a pressured stub directly.
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                raise AssertionError('pressured run must not reach any subprocess')

            with mock.patch.dict(sys.modules, {'host_ok': host_ok_stub((False, 'pressured'))}), \
                    mock.patch.object(bp, 'APPLY', True), \
                    mock.patch.object(bp, 'LOG', log), \
                    mock.patch.object(bp, 'PAUSE', Path(tmp, 'PAUSE')), \
                    mock.patch.object(bp.subprocess, 'run', side_effect=fake_run), \
                    contextlib.redirect_stdout(io.StringIO()):
                bp.main()
            self.assertEqual(calls, [])
            pause = Path(tmp, 'PAUSE')
            pause.write_text('stop')
            with mock.patch.dict(sys.modules, {'host_ok': host_ok_stub()}), \
                    mock.patch.dict(bp._pr_cache, clear=True), \
                    mock.patch.object(bp, 'APPLY', True), \
                    mock.patch.object(bp, 'LOG', log), \
                    mock.patch.object(bp, 'PAUSE', pause), \
                    mock.patch.object(bp.subprocess, 'run', side_effect=fake_run), \
                    contextlib.redirect_stdout(io.StringIO()):
                bp.main()
            self.assertEqual(calls, [])
            self.assertEqual(pressured['patches'], ['card-a1'])

    def test_control_mutation_scope_is_unchanged(self):
        got = run_sweeper([sweeper_row()], [done_process('200')])
        urls = [a for a, _ in got['curls'] for a in a if isinstance(a, str) and a.startswith('http')]
        self.assertEqual(len(urls), 1)
        self.assertIn('/decline', urls[0])
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            promoted = run_promoter_tick(rows=(PROM_CARD_A,), patch_script=[('200', 0)],
                                         log_path=log, tmpdir=tmp)
        curls = [c for c, _ in promoted['calls'] if c and c[0] == 'curl']
        self.assertEqual(len(curls), 1)
        self.assertIn('PATCH', curls[0])
        body = curls[0][curls[0].index('-d') + 1]
        self.assertEqual(json.loads(body).get('status'), 'todo')
        guard = run_flow_guard(patch_script=[('200', 0), ('200', 0)])
        self.assertEqual(len(guard['patch_calls']), 2)

    def test_control_timeouts_stay_visible_without_same_tick_retry(self):
        got = run_sweeper([sweeper_row()], [subprocess.TimeoutExpired('curl', cis.CURL_TIMEOUT)])
        self.assertEqual(len(got['curls']), 1)
        boom = subprocess.TimeoutExpired('curl', fg.CURL_TIMEOUT_SECONDS)
        guard = run_flow_guard(patch_script=[boom, boom])
        self.assertEqual(len(guard['patch_calls']), 2)
        self.assertIn('outcome unknown', '\n'.join(guard['log_messages']))


class OfflineCommand(unittest.TestCase):
    def test_red_shared_contract_offers_one_offline_selftest(self):
        shared, per_caller = shared_outcome_module()
        self.assertTrue(shared, f'shared outcome module missing, got {per_caller}')
        name = sorted(shared)[0]
        proc = subprocess.run([sys.executable, str(HERE / f'{name}.py'), '--selftest'],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f'{name} --selftest must exit 0 offline: {proc.stderr[:300]}')
        text = (proc.stdout + proc.stderr).lower()
        self.assertIn('version', text)
        self.assertTrue('confirm' in text or 'outcome' in text)
        no_secret_leak(self, proc.stdout + proc.stderr, f'{name} --selftest')


if __name__ == '__main__':
    unittest.main()
