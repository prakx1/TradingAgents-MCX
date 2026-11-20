"""Start/stop this checkout's desk without tying its lifetime to the terminal."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SERVICES = {
    'kite_login': ('kite_login.py', 'kite_login.log'),
    'kite_feed': ('kite_feed.py', 'kite_feed.log'),
    'collector': ('live_collector.py', 'collector.log'),
    'sentinel': ('news_sentinel.py', 'sentinel.log'),
    'scheduler': ('scheduler.py', 'scheduler.log'),
    'guard': ('position_guard.py', 'guard.log'),
    'maritime': ('ukmto_watcher.py', 'ukmto.log'),
    'dashboard': ('live_dashboard.py', 'dashboard.log'),
}


def owned_processes():
    """Match absolute script paths from this checkout; never broad pkill names."""
    result = subprocess.run(['ps', '-axo', 'pid=,args='], capture_output=True, text=True, check=True)
    found = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid, command = fields
        executable = command.split(None, 1)[0].lower()
        if 'python' not in executable and not executable.endswith('/streamlit'):
            continue
        if any(str(ROOT / script) in command for script, _ in SERVICES.values()):
            found.append(int(pid))
    return found


def stop():
    pids = owned_processes()
    for pid in pids:
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while set(pids) & set(owned_processes()):
        if time.monotonic() >= deadline:
            raise RuntimeError('A service is still stopping; retry after it exits.')
        time.sleep(.2)


def start():
    children = {}
    for name, (script, log_name) in SERVICES.items():
        args = [sys.executable, '-u', str(ROOT / script)]
        if name == 'dashboard':
            args = [sys.executable, '-m', 'streamlit', 'run', str(ROOT / script),
                    '--server.port', '8503', '--server.address', '127.0.0.1', '--server.headless', 'true']
        if sys.platform == 'darwin':
            args = ['/usr/bin/caffeinate', '-i', *args]
        with (ROOT / log_name).open('a') as log:
            process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        children[name] = process
        print(f'{name}: started PID {process.pid}', flush=True)
    (ROOT / '.desk_services.json').write_text(json.dumps({k: p.pid for k, p in children.items()}))
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        failed = [name for name, process in children.items() if process.poll() is not None]
        if failed:
            raise RuntimeError(f'Services exited: {", ".join(failed)}. Check their log files.')
        try:
            with urllib.request.urlopen('http://127.0.0.1:8503/_stcore/health', timeout=2) as response:
                if response.status == 200:
                    print('Dashboard ready: http://localhost:8503', flush=True)
                    return
        except OSError:
            pass
        time.sleep(.5)
    raise RuntimeError('Dashboard did not become ready in 40 seconds; check dashboard.log.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true', help='Stop this checkout\'s desk services')
    args = parser.parse_args()
    with (ROOT / '.desk_launcher.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop()
        if args.stop:
            print('Desk services stopped. All stored data retained.')
        else:
            start()


if __name__ == '__main__':
    main()
