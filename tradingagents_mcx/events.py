"""Event detection + durable event log.

Watches state transitions between dashboard refreshes and emits events:
  signal_buy / signal_sell   the live signal entered a BUY/SELL state
  fill                       a Council call's limit entry traded (position opened)
  profit                     a call closed at target
  loss                       a call closed at stop
  council                    a new AI Council decision landed

State lives in <dir>/event_state.json; every event is appended to
<dir>/events_YYYYMMDD.jsonl — files, append-only, nothing is ever lost.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

PRIORITY = ["loss", "profit", "fill", "council_long", "council_short", "council",
            "signal_sell", "signal_buy"]


def _paths(events_dir: str) -> tuple[Path, Path]:
    d = Path(events_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / "event_state.json", d / f"events_{dt.date.today():%Y%m%d}.jsonl"


def detect_events(events_dir: str, signal_label: str | None,
                  calls: list[dict], run_ids: list[str],
                  run_decisions: dict | None = None) -> list[dict]:
    state_p, log_p = _paths(events_dir)
    try:
        state = json.loads(state_p.read_text())
    except Exception:
        state = {"signal": None, "calls": {}, "runs": []}
    events = []
    now = dt.datetime.now().strftime("%H:%M:%S")

    def emit(kind, msg, icon):
        events.append({"ts": now, "kind": kind, "msg": msg, "icon": icon})

    # signal state change into an actionable state
    if signal_label and signal_label != state.get("signal"):
        if "BUY" in signal_label:
            emit("signal_buy", f"Signal → {signal_label}", "🟢")
        elif "SELL" in signal_label:
            emit("signal_sell", f"Signal → {signal_label}", "🔴")
        elif state.get("signal") and any(w in str(state["signal"]) for w in ("BUY", "SELL")):
            emit("signal_neutral", f"Signal cooled → {signal_label}", "⚪")
        state["signal"] = signal_label

    # ledger transitions
    for c in calls:
        if not c.get("side"):
            continue
        prev = state["calls"].get(c["id"], {})
        if c.get("filled_at") and not prev.get("filled"):
            emit("fill", f"FILLED {c.get('trade_id') or c['id'][-6:]} {c['side']} @ ₹{c['entry']:,.0f}", "💰")
        if c["status"] != prev.get("status"):
            if c["status"] == "target_hit":
                emit("profit", f"TARGET HIT {c.get('trade_id') or ''} ₹{c.get('pnl_inr') or 0:+,.0f}", "🎯")
            elif c["status"] == "stopped":
                emit("loss", f"STOPPED {c.get('trade_id') or ''} ₹{c.get('pnl_inr') or 0:+,.0f}", "🛑")
            elif c["status"] == "unfilled":
                emit("unfilled", f"Entry never reached — call lapsed ({c['decision']})", "⏸")
            elif c["status"] == "missed_no_fill":
                emit("missed", f"MISSED — target hit before entry filled ({c['decision']})", "💨")
        state["calls"][c["id"]] = {"status": c["status"], "filled": bool(c.get("filled_at"))}

    # new council runs — direction-aware verdict sound
    for rid in run_ids:
        if rid not in state.get("runs", []):
            if state.get("runs"):  # don't gong the whole backlog on first boot
                decision = (run_decisions or {}).get(rid, "")
                kind = ("council_long" if "LONG" in decision
                        else "council_short" if "SHORT" in decision else "council")
                emit(kind, f"COUNCIL D-{rid}: {decision or 'decided'}", "🤖")
            state.setdefault("runs", []).append(rid)

    state_p.write_text(json.dumps(state))
    if events:
        with open(log_p, "a") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    return events


def load_events(events_dir: str, n: int = 16) -> list[dict]:
    _, log_p = _paths(events_dir)
    if not log_p.exists():
        return []
    lines = log_p.read_text().splitlines()
    return [json.loads(x) for x in lines[-n:]][::-1]


def top_sound(events: list[dict]) -> str | None:
    kinds = {e["kind"] for e in events}
    for k in PRIORITY:
        if k in kinds:
            return k
    return None


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        calls = [{"id": "X1", "side": "LONG", "status": "open", "filled_at": None,
                  "entry": 8000.0, "decision": "CAUTIOUS LONG"}]
        ev1 = detect_events(d, "NEUTRAL", calls, ["0052"])
        assert not any(e["kind"] == "council" for e in ev1)  # backlog not gonged
        ev2 = detect_events(d, "BUY", calls, ["0052", "1127"])
        kinds = [e["kind"] for e in ev2]
        assert "signal_buy" in kinds and "council" in kinds, kinds
        calls[0].update(filled_at="2026-08-20 12:00", status="open")
        ev3 = detect_events(d, "BUY", calls, ["0052", "1127"])
        assert [e["kind"] for e in ev3] == ["fill"], ev3
        calls[0].update(status="target_hit", pnl_inr=6300)
        ev4 = detect_events(d, "BUY", calls, ["0052", "1127"])
        assert [e["kind"] for e in ev4] == ["profit"] and top_sound(ev4) == "profit"
        assert detect_events(d, "BUY", calls, ["0052", "1127"]) == []  # idempotent
        assert len(load_events(d)) == 4  # signal_buy, council, fill, profit
    print("events self-check OK (signal, council, fill, profit, idempotent, log)")
