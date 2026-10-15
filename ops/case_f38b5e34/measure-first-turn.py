#!/usr/bin/env python3
"""case_f38b5e34 H10 first-turn context measurement.
Method: for each succeeded heartbeat run, fetch GET /api/heartbeat-runs/{id}/log,
parse the `system/init` event (init.tools / skills / slash_commands counts) and the
FIRST assistant event's usage block. First-turn total =
  input_tokens + cache_creation_input_tokens + cache_read_input_tokens
(prompt-cache lanes report input_tokens=2 with the bulk in cache_creation).
Usage: PAPERCLIP_API_URL, PAPERCLIP_API_KEY, PAPERCLIP_COMPANY_ID in env.
"""
import json, os, subprocess, statistics, collections, sys

def pcurl(url):
    r = subprocess.run(['curl','-sS','-m','30', os.environ['PAPERCLIP_API_URL']+url,
        '-H','Authorization: Bearer '+os.environ['PAPERCLIP_API_KEY']],
        capture_output=True, text=True)
    r.check_returncode()
    return r.stdout

def first_turn(run_id):
    raw = pcurl(f'/api/heartbeat-runs/{run_id}/log')
    env = json.loads(raw.splitlines()[0])
    lines = env['content'].splitlines()
    init = None; usage = None
    dec = json.JSONDecoder()
    for l in lines:
        try: row = json.loads(l)
        except Exception: continue
        try: d,_ = dec.raw_decode(row.get('chunk',''))
        except Exception: continue
        if d.get('type')=='system' and d.get('subtype')=='init' and init is None:
            init = {'tools': len(d.get('tools',[])),
                    'mcp': [s.get('name') for s in d.get('mcp_servers',[])],
                    'slash': len(d.get('slash_commands',[])),
                    'skills': len(d.get('skills',[])),
                    'agents': len(d.get('agents',[]))}
        if d.get('type')=='assistant' and usage is None:
            u = d.get('message',{}).get('usage',{})
            if u.get('input_tokens') is not None:
                usage = {'input': u.get('input_tokens',0),
                         'cache_create': u.get('cache_creation_input_tokens',0),
                         'cache_read': u.get('cache_read_input_tokens',0)}
        if init and usage: break
    return init, usage

def main():
    n = int(sys.argv[1]) if len(sys.argv)>1 else 25
    runs = json.loads(pcurl(f"/api/companies/{os.environ['PAPERCLIP_COMPANY_ID']}/heartbeat-runs?limit=100"))
    ok = [r for r in runs if r.get('status')=='succeeded']
    totals=[]; tools_hist=[]
    done=0
    for r in ok:
        if done>=n: break
        try: init, usage = first_turn(r['id'])
        except Exception as e:
            print(f"{r['id'][:8]} LOG-FAIL {str(e)[:60]}"); continue
        if not (init and usage):
            print(f"{r['id'][:8]} NO-DATA"); continue
        done+=1
        t = usage['input']+usage['cache_create']+usage['cache_read']
        totals.append(t); tools_hist.append(init['tools'])
        print(f"{r['id'][:8]} ag={r['agentId'][:8]} total={t} "
              f"(in={usage['input']} mk={usage['cache_create']} rd={usage['cache_read']}) "
              f"tools={init['tools']} skills={init['skills']} slash={init['slash']}")
    totals.sort()
    p50 = statistics.median(totals)
    p90 = sorted(totals)[min(len(totals)-1,int(0.9*len(totals)))]
    print(f'n={done} min={totals[0]} p50={int(p50)} p90={p90} max={totals[-1]} tools_p50={int(statistics.median(tools_hist))}')

main()
