#!/usr/bin/env python3
"""Intraday signal schedule; post-close reviews never issue signals."""
import datetime as dt
import json
import time
from pathlib import Path
import fcntl

from tradingagents_mcx.config import load_config
from tradingagents_mcx.strategies import now_ist, profiles, strategy_config

ROOT = Path(__file__).resolve().parent


def jobs_for_day(day: dt.date, cfg: dict) -> list[dict]:
    jobs = []
    for name, p in profiles(cfg).items():
        if not p['enabled']:
            continue
        slots = []
        if day.weekday() < 5 and day.isoformat() not in p.get('holidays', []):
            slots.append(('09:02', 'opening bell', False))
            interval = float(p['interval_hours'])
            if interval:
                minute = 9 * 60 + 15
                cutoff = dt.time.fromisoformat(p['entry_cutoff'])
                while minute < 24 * 60 and dt.time(minute // 60, minute % 60) < cutoff:
                    slots.append((f'{minute // 60:02d}:{minute % 60:02d}', 'interval', False))
                    minute += max(1, round(interval * 60))
            if day.weekday() == 2:
                slots.append(('20:10', 'EIA', False))
            slots.append(('23:40', 'post-close', True))
        if name == 'intraday' and float(cfg['analysis'].get('night_watch_hours', 2)) > 0:
            step = max(60, int(float(cfg['analysis'].get('night_watch_hours', 2)) * 60))
            start, end = (60, 8 * 60) if day.weekday() < 5 else (7 * 60, 24 * 60)
            for minute in range(start, end, step if day.weekday() < 5 else step * 2):
                slots.append((f'{minute // 60:02d}:{minute % 60:02d}', 'night watch', True))
        for clock, label, watch in slots:
            when = dt.datetime.combine(day, dt.time.fromisoformat(clock))
            jobs.append(dict(key=f'{day}:{name}:{clock}', when=when, strategy=name, label=label, watch=watch))
    return sorted(jobs, key=lambda j: (j['when'], j['strategy']))


def next_slot(now: dt.datetime, cfg: dict) -> dict:
    return next(j for d in range(8) for j in jobs_for_day(now.date() + dt.timedelta(days=d), cfg) if j['when'] > now)


def main():
    state_path = ROOT / 'events' / 'signal_scheduler.json'
    state_path.parent.mkdir(exist_ok=True)
    with open(ROOT / '.signal_scheduler.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Signal scheduler already running', flush=True)
            return
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        print('Signal scheduler: intraday only; post-close analysis only', flush=True)
        while True:
            now, cfg = now_ist(), load_config()
            from tradingagents_mcx.opportunities import scan_once
            try:
                scan=scan_once(cfg,now)
                if scan['status']=='launched':
                    print(f"{now:%H:%M} opportunity scanner: {scan['reason']}",flush=True)
            except Exception as exc:
                # A failed opportunity scan must not stop the base schedule.
                print(f"{now:%H:%M} opportunity scanner error: {exc}",flush=True)
            from tradingagents_mcx.runs import active_run, launch_council
            if active_run(cfg):
                time.sleep(15)
                continue
            # A completed council can leave a data-provider thread alive. The
            # council state and lock, not that process's lifetime, own admission.
            for job in jobs_for_day(now.date(), cfg):
                age = (now - job['when']).total_seconds()
                if job['key'] in state or not 0 <= age <= 15 * 60:
                    continue
                scfg = strategy_config(cfg, job['strategy'])
                reports = list(Path(scfg['paths']['reports_dir']).glob('*.json'))
                cooldown = float(cfg['analysis'].get('council_cooldown_min', 10)) * 60
                if not job['watch'] and reports and time.time() - max(f.stat().st_mtime for f in reports) < cooldown:
                    state[job['key']] = 'cooldown'
                    started = False
                else:
                    started,message = launch_council(cfg,watch=job['watch'],deep=job['label']=='post-close')
                    if not started:
                        print(f"{now:%H:%M} scheduled review deferred: {message}",flush=True)
                        break  # Retry this slot; a busy council must not consume it.
                    state[job['key']] = 'launched'
                    print(f"{now:%H:%M} {job['strategy']} {job['label']} launched",flush=True)
                state = {k: v for k, v in state.items() if k.startswith(now.date().isoformat())}
                tmp = state_path.with_suffix('.tmp'); tmp.write_text(json.dumps(state)); tmp.replace(state_path)
                if started:break
            time.sleep(15)


if __name__ == '__main__':
    main()
