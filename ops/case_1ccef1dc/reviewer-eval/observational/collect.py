#!/usr/bin/env python3
"""Fetch reviewer verdict comments using an explicitly configured read-only API.

Reads PAPERCLIP_API_URL / PAPERCLIP_API_KEY from the environment, never prints
credentials. PAPERCLIP_REVIEWER_AGENT_ID selects the reviewer; no private fleet
identity is embedded. Inputs: an authorized runs.json export. Output is private
observational data, not an artifact to commit to this public repository.
Usage: collect.py <runs.json> <out dataset.json>
"""
import json,os,re,sys,urllib.request,concurrent.futures as cf
from urllib.parse import quote, urlencode
from uuid import UUID

PAGE_SIZE = 100
MAX_COMMENT_PAGES = 1000


class CollectionError(ValueError):
    """Incomplete or invalid evidence; safe to display without response contents."""

S=os.environ.get('PAPERCLIP_SCRATCH_DIR','.')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402
def base():
    B=os.environ['PAPERCLIP_API_URL'].rstrip('/')
    if B.endswith('/api'): B=B[:-4]
    return B
def head():
    return {'Authorization':'Bearer '+os.environ['PAPERCLIP_API_KEY']}
def get(path):
    try:
        B=base();H=head()
    except (KeyError, ValueError):
        raise CollectionError('API configuration is missing or invalid') from None
    for _ in range(3):
        try:
            r=urllib.request.Request(B+path,headers=H)
            with urllib.request.urlopen(r,timeout=40) as response:
                return json.load(response)
        except (OSError, ValueError):
            pass
    # Never include URLs, headers, response bodies or raw exception messages.
    raise CollectionError('API fetch failed after 3 attempts') from None


def comments(issue_path):
    """Read descending pages using the API's older-than `after` UUID cursor.

    A short raw page terminates traversal; reviewer filtering happens afterward.
    This is not a snapshot under concurrent edits/deletions. Unsupported servers
    that repeat pages, invalid payloads, and safety-bound exhaustion fail closed.
    """
    result = []
    seen = set()
    cursor = None
    for _ in range(MAX_COMMENT_PAGES):
        params = {'order': 'desc', 'limit': PAGE_SIZE}
        if cursor is not None:
            params['after'] = cursor
        page = get(issue_path + '/comments?' + urlencode(params))
        if not isinstance(page, list) or len(page) > PAGE_SIZE:
            raise CollectionError('invalid comments page')
        for item in page:
            if not isinstance(item, dict):
                raise CollectionError('invalid comment')
            ident = item.get('id')
            try:
                valid_id = isinstance(ident, str) and str(UUID(ident)) == ident
            except (ValueError, AttributeError):
                valid_id = False
            if not valid_id or ident in seen:
                raise CollectionError('invalid or repeated comment cursor')
            if not isinstance(item.get('createdAt'), str) or not item['createdAt']:
                raise CollectionError('invalid comment timestamp')
            if not isinstance(item.get('body'), str):
                raise CollectionError('invalid comment body')
            if any(item.get(key) is not None and not isinstance(item[key], str)
                   for key in ('authorAgentId', 'createdByRunId')):
                raise CollectionError('invalid comment attribution')
            seen.add(ident)
            result.append(item)
        if len(page) < PAGE_SIZE:
            return result
        cursor = page[-1]['id']
    raise CollectionError('comments pagination exceeded safety bound')
def build_rmap(runs):
    rmap={}
    for r in runs:
        u=r.get('usageJson') or {}
        m=(u.get('model') or '?')
        rmap[r['id']]={'model':re.sub(r'\(.*\)','',m),'effort':(re.search(r'\((.*)\)',m) or [None,None])[1],
          'in':u.get('inputTokens'),'out':u.get('outputTokens'),'cost':u.get('costUsd'),
          'start':r.get('startedAt'),'fin':r.get('finishedAt'),'status':r['status'],
          'issue':(r.get('contextSnapshot') or {}).get('issueId')}
    return rmap
def one(i):
    reviewer=os.environ.get('PAPERCLIP_REVIEWER_AGENT_ID', '').strip()
    if not reviewer:
        raise CollectionError('PAPERCLIP_REVIEWER_AGENT_ID must be configured explicitly')
    issue_path = '/api/issues/' + quote(str(i), safe='')
    iss=get(issue_path)
    if not isinstance(iss, dict) or any(
            not isinstance(iss.get(key), str) or not iss[key]
            for key in ('identifier', 'title', 'status')):
        raise CollectionError('invalid issue response')
    com=comments(issue_path)
    return i,{'ident':iss.get('identifier'),'title':iss.get('title'),'status':iss.get('status'),
      'comments':[{'id':c['id'],'at':c['createdAt'],'run':c.get('createdByRunId'),'agent':c.get('authorAgentId'),'body':(c.get('body') or '')[:700]} for c in com if c.get('authorAgentId')==reviewer]}
def write_dataset(out_path, dataset):
    """Persist the observational dataset atomically; fail loudly, never truncate.

    The dataset goes through a same-directory temp file plus fsync plus atomic
    rename, so an ENOSPC mid-write leaves the previous dataset in place. A
    failing write names the path and errno on stderr and exits non-zero.
    """
    try:
        write_json_atomic(out_path, dataset)
    except OSError as exc:
        print("collect: write failed: %s: errno=%s"
              % (out_path, getattr(exc, "errno", "?")), file=sys.stderr)
        raise SystemExit(2)
def main(argv=None):
    argv=argv if argv is not None else sys.argv[1:]
    with open(argv[0]) as f:
        runs=json.load(f)
    rmap=build_rmap(runs)
    issues=sorted({v['issue'] for v in rmap.values() if v['issue']})
    print('runs',len(rmap),'issues',len(issues),file=sys.stderr)
    out={}
    try:
        with cf.ThreadPoolExecutor(4) as ex:
            for i,d in ex.map(one,issues): out[i]=d
    except CollectionError as exc:
        print('collect: collection failed: %s; previous dataset preserved' % exc,
              file=sys.stderr)
        return 2
    write_dataset(argv[1], {'runs':rmap,'issues':out})
    print('done',len(out),file=sys.stderr)
    return 0
if __name__=='__main__':
    sys.exit(main())
