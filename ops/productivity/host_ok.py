"""Shared host-pressure gate for wake-producing reconcilers (2026-10-08: promotions and comments wake agents directly and
bypass the quota governor; at 03:35Z load hit 115 with IO pressure 78%). Returns (ok, reason)."""
import shutil
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
    for mount, cap in (('/', ROOT_MAX), ('/home', HOME_MAX)):
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            return False, f'{mount} unreadable'
        if u.used / u.total >= cap:
            return False, f'{mount} {u.used / u.total:.0%} >= {cap:.0%}'
    return True, 'ok'
