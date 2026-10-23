"""Test-only offline containment and immutable inputs; never an authorization matcher."""
import contextlib
import copy
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.request
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SOURCE_SHA = '166106f5fc3aadd5a2b25a485de225a4dd254671'
REPOSITORY = 'TogetherWeOwn/CLIProxyAPI'
HEAD = '159e43cea3c56aeaf51313651cce425a06256939'
NEW_HEAD = 'b' * 40
ISSUE = '00000000-0000-4000-8000-000000000001'
COMMENT = '00000000-0000-4000-8000-000000000002'
NOW = 1792713600
ACTIONS = ('owner_wake', 'set_readiness', 'update_pr_body',
           'enable_auto_merge', 'merge', 'enqueue_merge')
DENY = 'DENY_REVIEW_ONLY'
INVALID = 'DENY_INVALID_POLICY_OR_CONTEXT'
ALLOW = 'NOT_RESTRICTED'


def pr(repository=REPOSITORY, number=1, head=HEAD, **overrides):
    """Each caller owns a fresh fixture. Future green is hypothetical, not a receipt."""
    value = dict(number=number, repository=repository, repo=repository, head_sha=head,
                 headRefOid=head, expected_head=head, observed_head=head,
                 owner_card_id=ISSUE, title='Offline review-only fixture',
                 url=f'https://github.com/{repository}/pull/{number}', isDraft=True,
                 mergeStateStatus='CLEAN', createdAt='2026-10-01T00:00:00Z',
                 author={'login': 'fixture-author'},
                 statusCheckRollup=[{'name': 'Paperclip Review', 'conclusion': 'SUCCESS'}])
    value.update(overrides)
    return value


def context(repository=REPOSITORY, number=1, head=HEAD, **overrides):
    value = {'repository': repository, 'pr_number': number, 'expected_head': head,
             'observed_head': head, 'action': 'owner_wake', 'consumer': 'offline-acceptance'}
    value.update(overrides)
    return value


def with_context(fn, *args, pr_context, **kwargs):
    """Legacy signatures still execute, so RED is a side effect, not a TypeError.

    Integration contract: final Python senders accept keyword pr_context. The
    fallback is only to exercise the unfenced baseline. It grants no permission.
    """
    parameters = inspect.signature(fn).parameters
    if 'pr_context' in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
        kwargs['pr_context'] = pr_context
    return fn(*args, **kwargs)


def scratch():
    base = os.environ.get('PAPERCLIP_RUN_SCRATCH_DIR') or os.environ.get('PAPERCLIP_SCRATCH_DIR')
    if not base:
        raise RuntimeError('Set PAPERCLIP_SCRATCH_DIR to a run-owned scratch directory')
    return tempfile.TemporaryDirectory(prefix='fence-test-', dir=base)


def forbidden(*args, **kwargs):
    raise AssertionError('OFFLINE TRAP: unexpected network or process access')


@contextlib.contextmanager
def contained():
    """No ambient credentials, sockets, network fallbacks, shell or real gh."""
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'PAPERCLIP_SCRATCH_DIR',
           'PAPERCLIP_RUN_SCRATCH_DIR', 'TMPDIR') if k in os.environ}
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(socket, 'socket', side_effect=forbidden), \
            mock.patch.object(socket, 'create_connection', side_effect=forbidden), \
            mock.patch.object(urllib.request, 'urlopen', side_effect=forbidden), \
            mock.patch.object(subprocess, 'Popen', side_effect=forbidden), \
            mock.patch.object(os, 'system', side_effect=forbidden):
        yield


def import_productivity(name):
    for path in (ROOT / 'ops/productivity', ROOT / 'ops/lib', ROOT / 'ops/pr-hygiene'):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return importlib.import_module(name)


class ApiSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, copy.deepcopy(body)))
        return '201', json.dumps({'id': COMMENT})


def captured(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        result = fn()
    return result, out.getvalue()


def decision_code(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get('decision')
    return getattr(value, 'decision', None)
