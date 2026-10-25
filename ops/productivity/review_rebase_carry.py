#!/usr/bin/env python3
"""Read-only own-patch equivalence and re-review scope decisions.

The caller supplies complete, authenticated review evidence and pinned commits.
This module neither fetches objects nor publishes checks. Equivalence covers the
review only; CI and every other required check still gate the new head.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
from collections.abc import Mapping

SHA_RX = re.compile(r'[0-9a-f]{40}\Z')
REPO_RX = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
NAMESPACE = 'paperclip-review-rebase-carry:v1:'
DIFF_FLAGS = ('--no-ext-diff', '--no-textconv', '--no-renames', '--binary',
              '--full-index', '--unified=0', '--no-color',
              '--src-prefix=a/', '--dst-prefix=b/')


class GitError(Exception):
    """Missing, ambiguous, malformed or unavailable evidence; require review."""


def normalized_patch(patch):
    """Match the merge-group normalization, preserving byte/record boundaries."""
    if not isinstance(patch, bytes):
        raise GitError('patch must be bytes')
    files, current = {}, None
    lines = patch.split(b'\n')
    for position, line in enumerate(lines):
        if position < len(lines) - 1:
            line += b'\n'
        if line.startswith(b'diff --git '):
            current = line.rstrip(b'\n')
            files.setdefault(current, []).append(b'\x00file-record\x00')
        elif current is None:
            if line.strip():
                raise GitError('unrecognized patch preamble')
        elif line.startswith(b'index '):
            index = re.fullmatch(rb'index [0-9a-f]+\.\.[0-9a-f]+(?: ([0-7]{6}))?\n?', line)
            if not index:
                raise GitError('invalid Git index header')
            if index.group(1):
                files[current].append(b'file mode ' + index.group(1) + b'\n')
        elif line.startswith((b'@@ ', b' ')):
            continue
        else:
            files[current].append(line)
    return {name: b''.join(body) for name, body in files.items()}


def digest(patch):
    h = hashlib.sha256()
    for name, body in sorted(normalized_patch(patch).items()):
        for value in (name, body):
            h.update(len(value).to_bytes(8, 'big'))
            h.update(value)
    return h.hexdigest()


def _sha(value):
    if not isinstance(value, str) or not SHA_RX.fullmatch(value):
        raise GitError('invalid commit SHA')
    return value


def _base(value):
    if (not isinstance(value, Mapping) or not isinstance(value.get('ref'), str)
            or not value['ref'] or any(ord(c) < 32 for c in value['ref'])):
        raise GitError('invalid base snapshot')
    return value['ref'], _sha(value.get('sha'))


def _git(repo, *args):
    # Do not inherit repository redirection, config injection, object replacement
    # or external diff settings from the calling process. No network operations.
    try:
        path = Path(repo).resolve(strict=True)
        if not path.is_dir() or '://' in os.fspath(repo):
            raise GitError('repository must be a local directory')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                   GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
                   GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
        result = subprocess.run(
            ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null',
             '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
             '-c', 'core.quotePath=true', '-c', 'core.fsmonitor=false',
             '-c', 'diff.ignoreSubmodules=none', '-c', 'diff.submodule=short',
             '-C', str(path), *args],
            env=env, capture_output=True, timeout=120)
        if result.returncode:
            raise GitError('git evidence unavailable')
        return result.stdout
    except GitError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # Do not echo subprocess output or arbitrary environment/config values.
        raise GitError('git evidence unavailable') from None


def merge_base(repo, base_sha, head_sha):
    bases = _git(repo, 'merge-base', '--all', _sha(base_sha), _sha(head_sha)).split()
    if len(bases) != 1:
        raise GitError('missing or ambiguous merge base')
    try:
        return _sha(bases[0].decode('ascii'))
    except UnicodeError:
        raise GitError('invalid merge base') from None


def own_patch(repo, base_sha, head_sha):
    before = merge_base(repo, base_sha, head_sha)
    return _git(repo, 'diff', *DIFF_FLAGS, before, _sha(head_sha), '--')


def latest_review(reviews, number, head):
    """Newest scoped row wins BEFORE score/status checks; never resurrect 5/5."""
    try:
        if type(number) is not int or number < 1:
            return None
        _sha(head)
        if not isinstance(reviews, (list, tuple)):
            return None
        bound = []
        for row in reviews:
            if not isinstance(row, Mapping):
                return None
            if type(row.get('pr')) is not int or not isinstance(row.get('head_sha'), str):
                return None
            if row['pr'] != number or row['head_sha'] != head:
                continue
            if type(row.get('id')) is not int or row['id'] < 1:
                return None
            bound.append(row)
        if not bound:
            return None
        newest = max(row['id'] for row in bound)
        tied = [row for row in bound if row['id'] == newest]
        latest = tied[0]
        if any(row != latest for row in tied):
            return None
        if (latest.get('status') != 'completed' or latest.get('conclusion') != 'success'
                or latest.get('title') != '5/5'):
            return None
        return latest
    except Exception:
        return None


def carry_summary(old_head, new_head, patch_digest):
    return (f'5/5 carried: identical reviewed patch after rebase '
            f'({old_head} -> {new_head}, digest {patch_digest})')


def idempotency_key(repo, number, head):
    if (not isinstance(repo, str) or not REPO_RX.fullmatch(repo)
            or type(number) is not int or number < 1):
        raise GitError('invalid repository or PR')
    return f'{NAMESPACE}{repo.lower()}:{number}:{_sha(head)}'


def carry_applies(proof, head):
    return isinstance(proof, Mapping) and proof.get('new_head') == head


def _complete_compare(compare):
    if compare is None:
        return True
    if not isinstance(compare, Mapping) or compare.get('truncated') is not False:
        return False
    for total, rows in (('total_commits', 'commits'), ('total_files', 'files')):
        if (type(compare.get(total)) is not int or compare[total] < 0
                or not isinstance(compare.get(rows), list)
                or len(compare[rows]) != compare[total]):
            return False
    return True


def decide(repo, number, old_head, new_head, old_base, new_base, reviews, compare=None):
    """Return a proof or full-review reason. Never raise on evidence failures."""
    try:
        _sha(old_head)
        _sha(new_head)
        if old_head == new_head:
            return 'review', {'reason': 'same head is not a rebase'}
        old_ref, old_sha = _base(old_base)
        new_ref, new_sha = _base(new_base)
        if old_ref != new_ref:
            return 'review', {'reason': 'base branch changed'}
        if not _complete_compare(compare):
            return 'review', {'reason': 'incomplete comparison evidence'}
        if not isinstance(reviews, (list, tuple)):
            return 'review', {'reason': 'missing review evidence'}
        for row in reviews:
            if not isinstance(row, Mapping):
                raise GitError('malformed review row')
            if row.get('pr') == number and row.get('head_sha') == new_head:
                return 'review', {'reason': 'new head already has a review row'}
        source = latest_review(reviews, number, old_head)
        if source is None:
            return 'review', {'reason': 'no unambiguous latest successful 5/5'}
        old_patch = own_patch(repo, old_sha, old_head)
        new_patch = own_patch(repo, new_sha, new_head)
        old_digest, new_digest = digest(old_patch), digest(new_patch)
        if old_digest != new_digest or normalized_patch(old_patch) != normalized_patch(new_patch):
            return 'review', {'reason': 'normalized own-patch changed'}
        return 'carry', {'old_head': old_head, 'new_head': new_head,
                         'old_base': old_sha, 'new_base': new_sha,
                         'digest': old_digest,
                         'summary': carry_summary(old_head, new_head, old_digest)}
    except Exception:
        return 'review', {'reason': 'invalid or unavailable evidence'}


def _unquote_path(value):
    if not value.startswith(b'"'):
        return value
    if not value.endswith(b'"'):
        raise GitError('invalid quoted path')
    value = value[1:-1]
    result = bytearray()
    escapes = {ord(k): v for k, v in {'a': 7, 'b': 8, 't': 9, 'n': 10,
                                     'v': 11, 'f': 12, 'r': 13,
                                     '"': 34, '\\': 92}.items()}
    i = 0
    while i < len(value):
        c = value[i]
        i += 1
        if c == 92:
            if i >= len(value):
                raise GitError('invalid path escape')
            c = value[i]
            i += 1
            if 48 <= c <= 55:
                digits = bytes([c]) + value[i:i + 2]
                if len(digits) != 3 or any(d < 48 or d > 55 for d in digits):
                    raise GitError('invalid octal path')
                c = int(digits, 8)
                i += 2
            elif c in escapes:
                c = escapes[c]
            else:
                raise GitError('invalid path escape')
        if c > 255:
            raise GitError('invalid path byte')
        result.append(c)
    return bytes(result)


def _path(header):
    # With --no-renames and fixed prefixes the two names are identical. Derive
    # the unquoted midpoint rather than splitting on spaces inside file names.
    value = header.removeprefix(b'diff --git ')
    if value.startswith(b'"'):
        match = re.fullmatch(rb'("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")', value)
        if not match:
            raise GitError('invalid diff paths')
        left, right = (_unquote_path(v) for v in match.groups())
    else:
        n = (len(value) - 5) // 2
        left, right = value[:n + 2], value[n + 3:]
        if value[n + 2:n + 3] != b' ':
            raise GitError('invalid diff paths')
    if not left.startswith(b'a/') or not right.startswith(b'b/') or left[2:] != right[2:]:
        raise GitError('unexpected rename or prefix')
    return _relative_path(os.fsdecode(left[2:]))


def _relative_path(path):
    if (not isinstance(path, str) or not path or path.startswith('/')
            or '\x00' in path or any(p in ('', '.', '..') for p in path.split('/'))):
        raise GitError('invalid finding path')
    return path


def delta_files(repo, old_head, new_head, old_base, new_base, findings=()):
    try:
        old_ref, old_sha = _base(old_base)
        new_ref, new_sha = _base(new_base)
        if old_ref != new_ref:
            raise GitError('base branch changed')
        old = normalized_patch(own_patch(repo, old_sha, old_head))
        new = normalized_patch(own_patch(repo, new_sha, new_head))
        paths = {_path(name) for name in old.keys() | new.keys()
                 if old.get(name) != new.get(name)}
        if not isinstance(findings, (list, tuple)):
            raise GitError('incomplete findings evidence')
        for finding in findings:
            if not isinstance(finding, Mapping):
                raise GitError('invalid finding')
            paths.add(_relative_path(finding.get('path')))
        return sorted(paths)
    except GitError:
        raise
    except Exception:
        raise GitError('delta evidence unavailable') from None
