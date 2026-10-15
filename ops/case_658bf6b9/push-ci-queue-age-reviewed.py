#!/usr/bin/python3
"""Host credential/CLI adapter for the reviewed case_e23db91f queue detector."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import sys
import time
import urllib.request
import jwt

REPOS = ['paperclip-ops', 'two-bot-next', 'two-web-next']
DETECTOR = '/opt/status-page/current/ops/case_658bf6b9/push-ci-queue-age.sh'

def read_values(path, names):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, raw = line.removeprefix('export ').partition('=')
        if sep and key.strip() in names:
            parts = shlex.split(raw, comments=True)
            if len(parts) != 1:
                raise ValueError('invalid credential field')
            values[key.strip()] = parts[0]
    if set(values) != set(names):
        raise ValueError('missing credential field')
    return values

def request(path, token, body=None):
    req = urllib.request.Request('https://api.github.com' + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + token,
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json',
                 'User-Agent': 'togetherweown-host-ci-detector',
                 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)

def main():
    os.umask(0o077)
    lock = os.open('/home/ubuntu/.cache/case_60330d6f-ci-queue-age.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('ci-queue transport: prior detector still running; no duplicate push')
        return 0
    os.set_inheritable(lock, True)
    app = read_values('/home/ubuntu/secure-drop/github-app.env', {'GITHUB_APP_ID', 'GITHUB_APP_INSTALLATION_ID'})
    env = dict(os.environ)
    env.update(read_values('/home/ubuntu/.config/case_60330d6f-gatus-push.env', {'GATUS_URL', 'GATUS_EXTERNAL_TOKEN'}))
    now = int(time.time())
    signed = jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': app['GITHUB_APP_ID']},
        Path('/home/ubuntu/secure-drop/github-app.pem').read_text(), algorithm='RS256')
    token = request('/app/installations/' + app['GITHUB_APP_INSTALLATION_ID'] + '/access_tokens', signed,
        {'repositories': REPOS, 'permissions': {'actions': 'read', 'metadata': 'read'}})['token']
    if sys.argv[1:] == ['--check-auth']:
        for repo in REPOS:
            result = request('/repos/TogetherWeOwn/' + repo + '/actions/runs?per_page=1', token)
            runs = result.get('workflow_runs', [])
            if runs:
                request('/repos/TogetherWeOwn/' + repo + '/actions/runs/' + str(runs[0]['id']) + '/jobs?per_page=1', token)
            print('actions runs/jobs readable: TogetherWeOwn/' + repo)
        return 0
    if sys.argv[1:]:
        raise ValueError('unsupported argument')
    env.update({'GH_TOKEN': token, 'GH_BIN': '/usr/bin/gh', 'GH_PROMPT_DISABLED': '1',
                'GH_REPOS': ' '.join('TogetherWeOwn/' + repo for repo in REPOS),
                'QUEUE_AGE_THRESHOLD_MIN': '60'})
    os.execve(DETECTOR, [DETECTOR], env)

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('ci-queue transport failed: ' + type(error).__name__ +
              (' HTTP ' + str(error.code) if hasattr(error, 'code') else ''), file=sys.stderr)
        sys.exit(1)
