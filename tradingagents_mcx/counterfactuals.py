"""Decision-quality engine: was the choice to trade / not trade itself correct?

Builds a decision record per AI Council run (the full gating chain and WHY it
did or didn't trade), then marks counterfactual lanes so gates become measurable:

  COUNCIL-REAL     limit fills only (the ledger)              — champion
  COUNCIL-MARKET   market fill at issuance (shadow)           — is limit-patience worth it?
  COUNCIL-NO-VETO  trade the trader's direction even when the
                   CIO stood aside                            — is the veto worth it?
  SIGNAL-SCALP     every signal flash as a 30-min scalp       — is not trading signals worth it?

All lanes are paper; all marked from the same tick data; nothing is deleted.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

from .ledger import _parse_zone

VETO_DECISIONS = ("STAND ASIDE",)


def load_decisions(reports_dir: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(Path(reports_dir) / "*.json"))):
        try:
            run = json.loads(Path(f).read_text())
        except Exception:
            continue
        stem = Path(f).stem                      # CRUDEOIL_20260820_1127
        hhmm = stem.split("_")[-1]
        ts = pd.Timestamp(f"{run['analysis_date']} {hhmm[:2]}:{hhmm[2:4]}:00")
        t, tally = run["trader"], run["trader"]["module_tally"]
        cio = run["cio"]["final_decision"]
        gate_off = str(run.get("risk_gate", "")).startswith("off — ")
        traded = "LONG" in cio or "SHORT" in cio or gate_off
        ref = run["data"]["mcx_fair_value_inr"]
        sane = lambda v: v if v and 0.7 * ref <= v <= 1.3 * ref else None
        votes = f"{tally['BULLISH']}🐂/{tally['BEARISH']}🐻/{tally['NEUTRAL']}⚪"
        if t["direction"] == "NEUTRAL":
            why = f"{votes} → vote tie → trader NEUTRAL → nothing to trade (by iron law)"
            gate = "no_direction"
        elif gate_off:
            why = (f"{votes} → iron law {t['direction']} ({t['confidence']}) → "
                   f"CIO said {cio} → ⚙️ risk gate OFF → trader direction executed")
            gate = "gate_off"
        elif traded:
            why = (f"{votes} → iron law {t['direction']} ({t['confidence']}) → "
                   f"risk {run['risk']['operational_confidence']} → CIO approved: {cio}")
            gate = "approved"
        else:
            why = (f"{votes} → iron law {t['direction']} ({t['confidence']}) → "
                   f"risk {run['risk']['operational_confidence']} → CIO VETOED → stood aside")
            gate = "vetoed"
        did = f"D-{run['analysis_date'][5:7]}{run['analysis_date'][8:10]}-{hhmm}"
        out.append({"run": hhmm, "decision_id": did, "time": ts, "votes": votes,
                    "trader": f"{t['direction']} ({t['confidence']})",
                    "trader_dir": t["direction"], "risk": run["risk"]["operational_confidence"],
                    "cio": cio, "traded": traded, "gate": gate, "why": why,
                    "ref_price": ref, "stop": sane(_parse_zone(t.get("stop"))),
                    "target": sane(_parse_zone(t.get("target"))), "date": run["analysis_date"]})
    return out


def mark_market_fill(dec: dict, ohlc: pd.DataFrame, lot_bbl: int = 100,
                     max_hold_days: int = 20, cost_inr_per_lot: float = 0.0) -> dict:
    """Counterfactual: entered at MARKET (ref price) the moment the run finished,
    with the trader's stop/target as barriers (stop first on a bar), else
    marked to the latest close. Round-trip cost deducted. Returns {pnl, status, exit}."""
    if dec["trader_dir"] == "NEUTRAL":
        return {"pnl": None, "status": "no direction", "exit": None}
    sign = 1 if dec["trader_dir"] == "BULLISH" else -1
    if ohlc is None or ohlc.empty or not isinstance(ohlc.index, pd.DatetimeIndex):
        return {"pnl": None, "status": "no data yet", "exit": None}  # fresh day: no ticks at all
    window = ohlc[ohlc.index > dec["time"]]
    if window.empty:
        return {"pnl": None, "status": "no data yet", "exit": None}
    entry = dec["ref_price"]

    def _res(px, status):
        return {"pnl": round((px - entry) * sign * lot_bbl - cost_inr_per_lot),
                "status": status, "exit": px}

    for ts, bar in window.iterrows():
        stop_hit = dec["stop"] and ((bar["low"] <= dec["stop"]) if sign == 1 else (bar["high"] >= dec["stop"]))
        tgt_hit = dec["target"] and ((bar["high"] >= dec["target"]) if sign == 1 else (bar["low"] <= dec["target"]))
        if stop_hit:
            return _res(dec["stop"], "stopped")
        if tgt_hit:
            return _res(dec["target"], "target")
        if (ts - dec["time"]).days >= max_hold_days:
            return _res(float(bar["close"]), "expired")
    return _res(float(window["close"].iloc[-1]), "running")


def decision_table(reports_dir: str, ohlc: pd.DataFrame, lot_bbl: int = 100,
                   cost_inr_per_lot: float = 0.0, calls: list[dict] | None = None) -> pd.DataFrame:
    by_decision = {c.get("decision_id"): c for c in (calls or [])}
    rows = []
    for dec in load_decisions(reports_dir):
        cf = mark_market_fill(dec, ohlc, lot_bbl, cost_inr_per_lot=cost_inr_per_lot)
        verdict = ""
        if dec["gate"] == "vetoed" and cf["pnl"] is not None:
            verdict = (f"❌ veto cost ₹{cf['pnl']:+,.0f}" if cf["pnl"] > 0
                       else f"✅ veto saved ₹{-cf['pnl']:+,.0f}" if cf["pnl"] < 0
                       else "➖ veto neutral")
        elif dec["gate"] in ("approved", "gate_off") and cf["pnl"] is not None:
            verdict = f"{'✅' if cf['pnl'] > 0 else '❌' if cf['pnl'] < 0 else '➖'} direction ₹{cf['pnl']:+,.0f}"
            if dec["gate"] == "gate_off":
                verdict += " ⚙️"
        elif dec["gate"] == "no_direction":
            verdict = "— (vote tie)"
        led_call = by_decision.get(dec["decision_id"])
        if led_call and led_call.get("status") == "reaffirmed":
            trade_cell = f"🔁 kept {led_call.get('reaffirms', 'position')}"
        elif led_call and led_call.get("trade_id"):
            trade_cell = led_call["trade_id"]
        else:
            trade_cell = f"T-{dec['decision_id'][2:]}" if dec["traded"] else "—"
        rows.append({"decision": dec["decision_id"], "trade": trade_cell,
                     "votes": dec["votes"], "trader": dec["trader"],
                     "risk": dec["risk"], "CIO": dec["cio"],
                     "traded?": ("⚙️ traded (gate off)" if dec["gate"] == "gate_off"
                                 else "✅ traded" if dec["traded"] else "🚫 not traded"),
                     "counterfactual ₹ (market fill)": cf["pnl"], "cf status": cf["status"],
                     "verdict": verdict, "why": dec["why"]})
    return pd.DataFrame(rows)


def lane_totals(reports_dir: str, ledger_stats: dict, calls: list[dict],
                events: pd.DataFrame, ohlc: pd.DataFrame, ltp: float | None,
                lot_bbl: int = 100, cost_inr_per_lot: float = 0.0) -> dict:
    """Total ₹ per lane, same lot size and same round-trip cost everywhere."""
    decisions = load_decisions(reports_dir)
    no_veto = [mark_market_fill(d, ohlc, lot_bbl, cost_inr_per_lot=cost_inr_per_lot)["pnl"]
               for d in decisions]
    no_veto = [p for p in no_veto if p is not None]
    council_mkt = [mark_market_fill(d, ohlc, lot_bbl, cost_inr_per_lot=cost_inr_per_lot)["pnl"]
                   for d in decisions if d["traded"]]
    council_mkt = [p for p in council_mkt if p is not None]
    scalp = pd.Series(dtype=float)
    if events is not None and len(events):
        scored = events[events["fwd_pct"].notna()]
        if len(scored):
            sign = scored["signal"].str.contains("BUY").map({True: 1, False: -1})
            scalp = (scored["fwd_pct"] / 100) * scored["ltp"] * lot_bbl * sign - cost_inr_per_lot
    return {
        # realized + open mark-to-market, so lanes compare total economic result
        "council_real": {"pnl": round(ledger_stats.get("total_pnl_inr", 0)
                                      + ledger_stats.get("unrealized_inr", 0)),
                         "n": sum(1 for c in calls if c.get("filled_at"))},
        "council_market": {"pnl": round(sum(council_mkt)), "n": len(council_mkt)},
        "council_no_veto": {"pnl": round(sum(no_veto)), "n": len(no_veto)},
        "signal_scalp": {"pnl": round(float(scalp.sum())) if len(scalp) else 0, "n": len(scalp)},
    }


if __name__ == "__main__":
    idx = pd.date_range("2026-08-20 12:00", periods=60, freq="1min")
    px = pd.Series([8100.0] * 30 + [8160.0] * 30, index=idx)
    ohlc = pd.DataFrame({"close": px, "high": px, "low": px})
    dec = {"trader_dir": "BULLISH", "time": pd.Timestamp("2026-08-20 12:05"),
           "ref_price": 8100.0, "stop": 8000.0, "target": 8150.0}
    cf = mark_market_fill(dec, ohlc)
    assert cf["status"] == "target" and cf["pnl"] == 5000, cf
    dec2 = dict(dec, trader_dir="BEARISH", stop=8150.0, target=8050.0)
    cf2 = mark_market_fill(dec2, ohlc)
    assert cf2["status"] == "stopped" and cf2["pnl"] == -5000, cf2
    dec3 = dict(dec, trader_dir="NEUTRAL")
    assert mark_market_fill(dec3, ohlc)["status"] == "no direction"
    dec4 = dict(dec, stop=None, target=None)
    cf4 = mark_market_fill(dec4, ohlc)
    assert cf4["status"] == "running" and cf4["pnl"] == 6000, cf4
    print("counterfactuals self-check OK (target, stop, neutral, running)")
