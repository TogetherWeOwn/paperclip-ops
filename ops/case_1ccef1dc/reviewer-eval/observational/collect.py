#!/usr/bin/env python3
"""Fetch reviewer verdict comments using an explicitly configured read-only API.

Reads PAPERCLIP_API_URL / PAPERCLIP_API_KEY from the environment, never prints
credentials. PAPERCLIP_REVIEWER_AGENT_ID selects the reviewer; no private fleet
identity is embedded. Inputs: an authorized runs.json export. Output is private
observational data, not an artifact to commit to this public repository.
Usage: collect.py <runs.json> <out dataset.json>
"""
import json,os,re,sys,urllib.request,concurrent.futures as cf
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
    B=base();H=head()
    for _ in range(3):
        try:
            r=urllib.request.Request(B+path,headers=H)
            return json.load(urllib.request.urlopen(r,timeout=40))
        except Exception as e: err=e
    return None
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
        raise ValueError('PAPERCLIP_REVIEWER_AGENT_ID must be configured explicitly')
    iss=get('/api/issues/%s'%i) or {}
    com=get('/api/issues/%s/comments?order=desc&limit=100'%i) or []
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
    with cf.ThreadPoolExecutor(4) as ex:
        for i,d in ex.map(one,issues): out[i]=d
    write_dataset(argv[1], {'runs':rmap,'issues':out})
    print('done',len(out),file=sys.stderr)
    return 0
if __name__=='__main__':
    sys.exit(main())
