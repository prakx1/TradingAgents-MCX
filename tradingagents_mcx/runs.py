"""Observable council runs and duplicate-safe dashboard launch requests."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from .strategies import now_ist, strategy_config


def root_for(cfg):
    return Path(strategy_config(cfg, 'intraday')['paths']['project_root'])


def alive(pid):
    try:
        if int(pid) <= 0:
            return False
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_state(cfg, name='intraday'):
    try:
        state = json.loads((root_for(cfg)/'events'/f'council_{name}.json').read_text())
    except (OSError, ValueError):
        return {}
    if state.get('status') in ('starting', 'running') and not alive(state.get('pid')):
        state.update(status='interrupted', message='The council process stopped before saving a result. Run it again.')
    return state


def active_run(cfg):
    for name in ('intraday', 'long_term'):
        state = read_state(cfg, name)
        if state.get('status') in ('starting', 'running'):
            return state
    # A council started before this update still has a live PID marker.
    try:
        pid, stamp = (root_for(cfg)/'.council_running').read_text().split(',')[:2]
        if alive(pid):
            from .strategies import IST
            return dict(status='running', pid=int(pid), strategy='intraday',
                        started_at=dt.datetime.fromtimestamp(float(stamp), IST).replace(tzinfo=None).isoformat(),
                        stage=0, message='Council running; detailed progress appears from its next run.')
    except (OSError, ValueError):
        pass
    return {}


def write_state(root, state):
    target = root/'events'/f"council_{state['strategy']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(target)


class RunTracker:
    def __init__(self, cfg, name, watch):
        self.root = root_for(cfg)
        self.lock = threading.RLock()
        stamp = now_ist().isoformat(timespec='seconds')
        self.state = dict(strategy=name, status='running', pid=os.getpid(), watch=watch,
                          started_at=stamp, updated_at=stamp, stage=0, message='Starting council', events=[])
        # The dashboard writes its starting state before releasing this launch lock.
        with (self.root/'.council_launch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            write_state(self.root, self.state)

    def progress(self, message):
        with self.lock:
            message = str(message).strip()
            stamp = now_ist().isoformat(timespec='seconds')
            self.state.update(updated_at=stamp, message=message)
            match = re.match(r'\[(\d+)/4\]', message)
            if match:
                self.state['stage'] = int(match[1])
            self.state['events'] = (self.state['events'] + [dict(ts=stamp, message=message)])[-25:]
            write_state(self.root, self.state)

    def finish(self, result=None, error=None):
        with self.lock:
            self.state.update(status='failed' if error else 'completed', finished_at=now_ist().isoformat(timespec='seconds'))
            if result:
                self.state.update(report_path=result.get('report_path'), decision=result['cio']['final_decision'],
                                  signal_status=result.get('signal', {}).get('status', 'analysis only'), stage=4)
            self.progress(str(error)[:300] if error else f"Completed: {self.state.get('decision', '')} · {self.state.get('signal_status', '')}")


def launch_council(cfg, watch=False, deep=False):
    if not watch and not strategy_config(cfg, 'intraday')['strategy']['enabled']:
        return False, 'Intraday signals are disabled in settings. Enable intraday first.'
    root = root_for(cfg)
    with (root/'.council_launch.lock').open('a') as launch_lock:
        fcntl.flock(launch_lock, fcntl.LOCK_EX)
        busy = active_run(cfg)
        if busy:
            return False, 'Council already running. Its progress is shown above.'
        with (root/'.council.lock').open('a') as council_lock:
            try:
                fcntl.flock(council_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False, 'Council already running. Wait for its result.'
        args = [sys.executable, '-u', str(root/'main.py'), '--strategy', 'intraday']
        if watch:
            args.append('--watch')
        if deep:
            args.append('--deep')
        try:
            with (root/'council.log').open('a') as log:
                child = subprocess.Popen(args, cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            stamp = now_ist().isoformat(timespec='seconds')
            write_state(root, dict(strategy='intraday', status='starting', pid=child.pid, watch=watch,
                                  started_at=stamp, updated_at=stamp, stage=0, message='Starting council', events=[]))
        except OSError as exc:
            return False, f'Could not start the council: {exc}'
    return True, 'Analysis preview started.' if watch else 'Intraday council started.'
