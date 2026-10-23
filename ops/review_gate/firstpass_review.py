#!/usr/bin/env python3
"""Read-only first-pass gate hook slice. No model call, dispatch, credential lookup or card mutation code included."""
import review_gate_runtime as runtime
import json
import re
from pathlib import Path
ROOT = runtime.ROOT
CONFIG = runtime.FIRSTPASS_CONFIG
STATE = runtime.FIRSTPASS_STATE
PAUSE = runtime.FIRSTPASS_PAUSE
SPECIALIST_NAMES = ['bug-scan', 'history', 'test-semantics', 'claims-docs', 'error-handling', 'types-contracts', 'guidelines']
CONFIDENCE_DEFAULT = 80
META_JSON = runtime.PROVIDER_USAGE
DEFAULT_CONFIG = {'enabled': True, 'model': 'muse-wonder-full-1.0-contributor', 'effort': 'high', 'max_concurrent': 10, 'ceiling': 12, 'repos': ['primary-service-a', 'primary-service-b', 'tertiary-service'], 'priority': 'high', 'priority_prs': [], 'fallback_on_error': True, 'shadow_max_concurrent': 2, 'pace_aware': False, 'shadow_review_mode': 'specialists', 'shadow_v3_max_concurrent': 16, 'shadow_x2_enabled': True, 'shadow_x2_max_concurrent': 6, 'sol_shadow': {'enabled': True, 'model': 'gpt-6.1-sol', 'effort': 'high', 'max_concurrent': 3}, 'review_mode': 'single', 'specialists': list(SPECIALIST_NAMES), 'confidence_threshold': CONFIDENCE_DEFAULT}

def meta_available(records):
    for r in records or []:
        if r.get('health') != 'healthy':
            continue
        try:
            w = float(r.get('weekly_utilization', 1))
            h = float(r.get('five_hour_utilization', 1))
        except (TypeError, ValueError):
            continue
        if w < 0.95 and h < 0.95:
            return True
    return False

def should_request_claude(fp_status):
    return fp_status in ('passed', 'fallback')

def short_name(repo):
    return (repo or '').split('/')[-1].lower()

def in_scope(repo, cfg):
    want = {r.lower() for r in (cfg or {}).get('repos', [])}
    return short_name(repo) in want

def gate_decision(full, n, head, cfg, fp_status, meta_ok, guards_ok=True):
    if not in_scope(full, cfg):
        return 'request'
    if fp_status is None:
        return 'request' if not meta_ok or not guards_ok else 'hold'
    if fp_status == 'pending' and (not guards_ok):
        return 'request'
    return 'request' if should_request_claude(fp_status) else 'hold'
CPU_SOME60_MAX = 35.0
LOAD5_CEILING = 64.0
HOME_PCT_MAX = 90.0
META_5H_MAX = 0.85

def parse_cpu_some60(line):
    try:
        m = re.search('some\\s+avg10=[\\d.]+\\s+avg60=([\\d.]+)', line or '')
        return float(m.group(1)) if m else float('inf')
    except (ValueError, AttributeError):
        return float('inf')

def cpu_some60():
    try:
        with open('/proc/pressure/cpu') as f:
            for line in f:
                if line.startswith('some'):
                    return parse_cpu_some60(line)
    except OSError:
        pass
    return float('inf')

def guards(load5, home_pct, max5h, cpu60):
    try:
        return float(cpu60) < CPU_SOME60_MAX and float(load5) < LOAD5_CEILING and (float(home_pct) < HOME_PCT_MAX) and (float(max5h) < META_5H_MAX)
    except (TypeError, ValueError):
        return False

def host_readings(records=None):
    try:
        load5 = float(open('/proc/loadavg').read().split()[0])
    except (OSError, ValueError, IndexError):
        load5 = float('inf')
    try:
        import shutil
        du = shutil.disk_usage(runtime.HOST_DISK_ROOT)
        home_pct = 100.0 * du.used / du.total
    except OSError:
        home_pct = float('inf')
    try:
        max5h = max((float(r.get('five_hour_utilization', 1)) for r in records or []))
    except ValueError:
        max5h = float('inf')
    return (load5, home_pct, max5h, cpu_some60())

def load_config():
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
            return {**DEFAULT_CONFIG, **cfg}
        except ValueError:
            pass
    return dict(DEFAULT_CONFIG)

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except ValueError:
            return {}
    return {}

def meta_records():
    try:
        return json.loads(META_JSON.read_text()).get('records', [])
    except (OSError, ValueError):
        return []

def fp_key(full, n, head):
    return f'{full.lower()}#{n}@{head.lower()}'

def parse_priority_prs(cfg):
    out = []
    for entry in (cfg or {}).get('priority_prs') or []:
        m = re.match('^\\s*(.+?)#(\\d+)\\s*$', str(entry or ''))
        if m:
            out.append((m.group(1).strip().lower(), int(m.group(2))))
    return out

def status(full, n, head):
    try:
        st = load_state()
    except (OSError, ValueError):
        return 'fallback'
    return (st.get(fp_key(full, n, head)) or {}).get('status')
