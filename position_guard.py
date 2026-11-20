#!/usr/bin/env python3
"""Always-on setup confirmation and signal stop/target/square-off monitor."""
import json
import os
import time
from pathlib import Path
import fcntl

from tradingagents_mcx.config import load_config
from tradingagents_mcx.execution import maintain_all
from tradingagents_mcx.strategies import now_ist
from tradingagents_mcx.strategies import strategy_config
from tradingagents_mcx.setups import PendingSetups
from tradingagents_mcx.paper import PaperBook


def main():
    root = Path(__file__).resolve().parent
    with open(root / ".position_guard.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Position guard already running", flush=True)
            return
        print("Intraday signal guard started; legacy signals retain their recorded exit rules; checks every 10 seconds", flush=True)
        while True:
            try:
                cfg = load_config()
                books = maintain_all(cfg)
                state = {"ts": now_ist().isoformat(timespec="seconds"), "pid": os.getpid(),
                         "paper": PaperBook(cfg).account(),
                         "pending_setups": sum(s['status']=='pending' for s in PendingSetups(strategy_config(cfg,'intraday')).load()),
                         "strategies": {name: {"active": sum(c["status"] == "active" for c in calls),
                             "exit_pending": sum(c["status"] == "active" and bool(c.get("exit_pending")) for c in calls)}
                             for name, calls in books.items()}}
                experiment_path=root/'events/mock_experiment.json'
                if experiment_path.exists():state['experiment']=json.loads(experiment_path.read_text())
                target = root / "events" / "position_guard.json"
                target.parent.mkdir(exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_text(json.dumps(state)); tmp.replace(target)
            except Exception as e:
                print(f"{now_ist():%H:%M:%S} guard error: {e}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
