#!/usr/bin/env python3
"""Standalone tick collector — keeps the intraday record building even when the
dashboard is closed. Long-running daemon:

  nohup .venv/bin/python live_collector.py >> collector.log 2>&1 &

Ticks every 10s during MCX hours (Mon-Fri 08:55-23:35 IST); idles off-hours and
resumes by itself the next session — no morning restart needed.
"""
import datetime as dt
import time

from tradingagents_mcx.config import load_config
from tradingagents_mcx.live import append_tick, compute_signal, load_ticks, log_signal, news_snapshot

CFG = load_config()
CACHE = CFG["paths"]["cache_dir"]


def market_open(now: dt.datetime) -> bool:
    return now.weekday() < 5 and dt.time(8, 55) <= now.time() <= dt.time(23, 35)


print(f"[collector] started {dt.datetime.now():%H:%M:%S}")
idle_logged = False
while True:
    now = dt.datetime.now()
    if not market_open(now):
        if not idle_logged:
            print(f"{now:%H:%M:%S} [collector] market closed — idling until next session", flush=True)
            idle_logged = True
        time.sleep(60)
        continue
    idle_logged = False
    try:
        row = append_tick(CACHE)
        if row:
            sig = compute_signal(load_ticks(CACHE), news_snapshot()["score"])
            if sig.get("subs"):
                log_signal(CACHE, sig)
            print(f"{row['ts']} ltp={row['ltp']:.0f} prem={row['premium']} "
                  f"sig={sig['label']} {sig['composite']:+.0f}", flush=True)
        else:
            print(f"{dt.datetime.now():%H:%M:%S} feed empty", flush=True)
    except Exception as e:
        print(f"{dt.datetime.now():%H:%M:%S} error: {e}", flush=True)
    time.sleep(10)  # feed prints once a minute; 10s catches each new print within seconds
