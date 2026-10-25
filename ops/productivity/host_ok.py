"""Shared host-pressure gate for wake-producing reconcilers (2026-10-08: promotions and comments wake agents directly and
bypass the quota governor; at 03:35Z load hit 115 with IO pressure 78%). Returns (ok, reason)."""
import os
import shutil
import sys
from pathlib import Path

LOAD5_MAX = 40.0
ROOT_MAX = 0.90
HOME_MAX = 0.93


def host_ok():
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'lib'))
    try:
        import host_pressure
        verdict = host_pressure.evaluate_pressure(host_pressure.read_pressure())
    except Exception as exc:  # fail closed: unreadable telemetry => not ok
        return False, f'pressure unreadable: {exc}'
    if not verdict.get('allowed', False):
        return False, str(verdict.get('reason', 'pressured'))
    held = _memcg_hold()
    if held:
        return False, held
    for mount, cap in (('/', ROOT_MAX), ('/home', HOME_MAX)):
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            return False, f'{mount} unreadable'
        if u.used / u.total >= cap:
            return False, f'{mount} {u.used / u.total:.0%} >= {cap:.0%}'
    return True, 'ok'


def _memcg_hold():
    """Container memory gate, off until PAPERCLIP_MEMCG_DIR names the Paperclip container's cgroup directory
    (unset = no change; undo = unset it). PAPERCLIP_MEMCG_MODE=shadow logs the verdict and never holds.
    Returns the hold reason, or '' to continue. Unreadable telemetry holds unless shadowing."""
    cgroup_dir = os.environ.get('PAPERCLIP_MEMCG_DIR', '').strip()
    if not cgroup_dir:
        return ''
    shadow = os.environ.get('PAPERCLIP_MEMCG_MODE', 'enforce').strip() == 'shadow'
    try:
        import memcg_pressure
        verdict = memcg_pressure.evaluate_memcg(memcg_pressure.read_memcg(cgroup_dir))
        reason = '' if verdict['allowed'] else str(verdict['reason'])
    except Exception as exc:  # fail closed, like the host pressure read above
        reason = f'memcg unreadable: {exc}'
    if reason and shadow:
        print(f'host_ok memcg shadow hold: {reason}', file=sys.stderr)
        return ''
    return reason
