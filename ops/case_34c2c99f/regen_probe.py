"""Root process probe for prune_operator_regen_dirs.py: prints each process's cwd, exe and fd links.

Installed as a root-owned file; run as `sudo -n /usr/bin/python3 -I <path>` with no arguments.
"""
import json, os
errors, refs = 0, []
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    base = '/proc/' + pid
    links = [base + '/cwd', base + '/exe']
    try:
        links += [base + '/fd/' + fd for fd in os.listdir(base + '/fd')]
    except FileNotFoundError:
        pass
    except OSError:
        errors += 1
    for link in links:
        try:
            refs.append(os.readlink(link))
        except FileNotFoundError:
            pass
        except OSError:
            errors += 1
print(json.dumps({'errors': errors, 'refs': refs}))
