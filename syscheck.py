#!/usr/bin/env python3
"""Read-only health checks for the intraday signal desk."""
import json
import subprocess
from pathlib import Path
import requests
from tradingagents_mcx.config import load_config
from tradingagents_mcx.signals import SignalBook, latest_quote
from tradingagents_mcx.strategies import NAMES, entry_allowed, now_ist, strategy_config


def main():
    cfg=load_config();now=now_ist();issues=[]
    root=Path(__file__).resolve().parent
    for script in ('live_collector.py','news_sentinel.py','scheduler.py','position_guard.py'):
        alive=subprocess.run(['pgrep','-f',script],capture_output=True).returncode==0
        print(f"{'OK' if alive else 'FAIL'} process {script}")
        if not alive:issues.append(script)
    try:
        response=requests.get('http://localhost:8503/_stcore/health',timeout=5)
        response.raise_for_status();print('OK dashboard HTTP')
    except Exception:
        issues.append('dashboard');print('FAIL dashboard HTTP')
    try:
        heartbeat=json.loads((root/'events/position_guard.json').read_text())
        import datetime as dt
        age=(now-dt.datetime.fromisoformat(heartbeat['ts'])).total_seconds()
        if age>45:raise ValueError('stale')
        print('OK signal guard heartbeat')
    except (OSError,ValueError,KeyError):
        issues.append('guard heartbeat');print('FAIL signal guard heartbeat')
    for name in NAMES:
        scfg=strategy_config(cfg,name)
        if scfg['strategy']['enabled'] and entry_allowed(scfg['strategy'],now):
            q,reason=latest_quote(scfg,now)
            print(f"{'OK' if q else 'FAIL'} {name} quote {reason}")
            if not q:issues.append(name+' quote')
        book=SignalBook(scfg);stats=book.stats()
        print(name,stats)
        if stats['exit_pending']:issues.append(name+' overdue exits')
    from tradingagents_mcx.paper import PaperBook
    paper=PaperBook(cfg)
    print('paper simulation',paper.account())
    if any(r['status']=='open' and r.get('exit_pending') for r in paper.load()):
        issues.append('paper overdue exits')
    print(f'SYSCHECK: {len(issues)} issue(s)')
    return 1 if issues else 0


if __name__=='__main__':raise SystemExit(main())
