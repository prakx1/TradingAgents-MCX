"""Learnings monitors — each lesson from the 8 Sep 2026 retrospective as a live KPI.

compute(...) returns a list of monitors: {id, title, lesson, status, headline, detail, target}
status: 'ok' (on track) | 'watch' (needs attention / not yet provable) | 'bad' (breached).
Every monitor is wrapped so one failure never blanks the tab.
"""
import datetime as dt
import glob
import json
import math
import subprocess
from pathlib import Path

import pandas as pd

LESSONS = {
    "edge": "13 closed trades in one +10% rally is not an edge; ~100 trades are needed before the word is defensible. Compare against buy-and-hold of one mini lot.",
    "stops": "Stops were decorative (median 6.5% away, none ever hit) so rupee-risk per trade was uncontrolled (₹4k–₹91k).",
    "reversals": "Every losing trade was a reversal on a LOW-confidence, one-vote call. Reversing cost ₹11k net vs holding.",
    "thesis": "The biggest loss sat 117 h underwater through 32 'still bullish' reaffirms while the trader's own invalidation level was ignored.",
    "gate": "Gate-off freed the two best trades AND every whipsaw: free the entries, gate the reversals.",
    "sizing": "Lots escalated 2→3→4→9 because size followed vote-count confidence, not stop distance.",
    "committee": "Two of six analysts voted bullish every run; debate winner had no relation to outcomes. Score each stage against always-long.",
    "cadence": "The '30-min' cadence was really a 16-min news loop: 77% of runs changed nothing while direction flipped 52 times.",
    "books": "Phantom fills on parity prices and a stale scheduler flattered the book. No fill unless the price actually traded.",
    "cost": "Model capability is not the bottleneck; structure is. Keep LLM spend small relative to P&L.",
}


def _wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def daily_ohlc(cache_dir: str, symbol: str = "CRUDEOIL") -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(Path(cache_dir) / "live" / f"{symbol}_2*.csv"))):
        try:
            t = pd.read_csv(f, usecols=["ts", "ltp"])
        except Exception:
            continue
        if len(t):
            rows.append({"date": f[-12:-4], "open": float(t["ltp"].iloc[0]), "high": float(t["ltp"].max()),
                         "low": float(t["ltp"].min()), "close": float(t["ltp"].iloc[-1])})
    return pd.DataFrame(rows)


def load_votes(reports_dir: str) -> pd.DataFrame:
    """One row per Council run: module votes, trader direction, fair value. (~0.5s for 500 runs)"""
    rows = []
    for f in sorted(glob.glob(str(Path(reports_dir) / "*.json"))):
        try:
            r = json.loads(Path(f).read_text())
            stem = Path(f).stem
            hhmm = stem.split("_")[-1]
            row = {"run": stem, "date": stem.split("_")[1], "hhmm": hhmm,
                   "ts": pd.Timestamp(f"{stem.split('_')[1]} {hhmm[:2]}:{hhmm[2:]}"),
                   "fair": r["data"]["mcx_fair_value_inr"], "trader_dir": r["trader"]["direction"]}
            for a in r["analysts"]:
                row[a["module"]] = a["direction"]
            rows.append(row)
        except Exception:
            continue
    return pd.DataFrame(rows)


def _scheduler_stale() -> tuple[bool, str]:
    """Scheduler runs the Council in-process: code edited after its start is invisible to it."""
    try:
        pid = subprocess.run(["pgrep", "-of", "scheduler.py"], capture_output=True, text=True).stdout.split()[0]
        lstart = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True).stdout.strip()
        started = dt.datetime.strptime(lstart, "%a %b %d %H:%M:%S %Y")
        newest = max(Path(p).stat().st_mtime for p in glob.glob(str(Path(__file__).parent / "*.py")))
        stale = newest > started.timestamp()
        return stale, f"scheduler started {started:%d %b %H:%M}; newest code {dt.datetime.fromtimestamp(newest):%d %b %H:%M}"
    except Exception:
        return False, "scheduler not running"


def compute(calls: list[dict], reports_dir: str, cache_dir: str, equity: float,
            usage_path: str | None = None, votes: pd.DataFrame | None = None) -> list[dict]:
    trades = [c for c in calls if c.get("trade_id") and (c.get("lots") or 0) > 0 and c.get("side")]
    closed = [c for c in trades if c["status"] not in ("open",) and c.get("pnl_inr") is not None]
    opens = [c for c in trades if c["status"] == "open"]
    ohlc = daily_ohlc(cache_dir)
    out = []

    def add(mid, title, status, headline, detail, target):
        out.append({"id": mid, "title": title, "lesson": LESSONS[mid], "status": status,
                    "headline": headline, "detail": detail, "target": target})

    def risk_inr(c):
        return abs(c["entry"] - (c.get("stop") or c["entry"])) * c["lots"] * c["lot_bbl"]

    # 1. edge
    try:
        n = len(closed); wins = sum(1 for c in closed if c["pnl_inr"] > 0)
        lo, hi = _wilson(wins, n)
        pnls = [c["pnl_inr"] for c in closed]
        rs = [c["pnl_inr"] / risk_inr(c) for c in closed if risk_inr(c) > 0]
        mean_r = sum(rs) / len(rs) if rs else 0.0
        sd_r = (sum((x - mean_r) ** 2 for x in rs) / (len(rs) - 1)) ** 0.5 if len(rs) > 1 else 0.0
        t_r = mean_r / (sd_r / math.sqrt(len(rs))) if sd_r > 0 and rs else 0.0
        bh = (ohlc["close"].iloc[-1] - ohlc["open"].iloc[0]) * 10 if len(ohlc) else 0.0
        status = "ok" if (n >= 70 and t_r >= 2) else "watch"
        add("edge", "Edge: proven or not yet?", status,
            f"{n} closed · {wins} wins ({wins / n * 100:.0f}%)" if n else "no closed trades",
            f"95% CI {lo * 100:.0f}–{hi * 100:.0f}% · expectancy ₹{(sum(pnls) / n if n else 0):,.0f} · mean {mean_r:+.2f}R · t={t_r:.2f} · "
            f"buy-and-hold 1 mini lot ₹{bh:+,.0f}",
            f"≥70 closed trades with t ≥ 2 · progress {min(n, 70)}/70")
    except Exception as e:
        add("edge", "Edge", "watch", "n/a", str(e)[:80], "")

    # 2. stops
    try:
        pcts = [abs(c["entry"] - c["stop"]) / c["entry"] for c in trades if c.get("stop")]
        med = sorted(pcts)[len(pcts) // 2] * 100 if pcts else 0
        wide = sum(1 for p in pcts if p > 0.03)
        stopped = sum(1 for c in closed if c["status"] in ("stopped", "stop_hit"))
        open_risk = sum(risk_inr(c) for c in opens)
        risk_pct = open_risk / equity * 100 if equity else 0
        status = "bad" if (risk_pct > 2 or (pcts and pcts[-1] > 0.03)) else ("watch" if med > 3 else "ok")
        add("stops", "Stops that mean something", status,
            f"open ₹-risk ₹{open_risk:,.0f} = {risk_pct:.1f}% of equity",
            f"median stop {med:.1f}% from entry · {wide}/{len(pcts)} trades wider than 3% · {stopped}/{len(closed)} exits were stop-outs",
            "stop ≤ 3% of entry · ₹-risk ≤ 2% of equity per trade")
    except Exception as e:
        add("stops", "Stops", "watch", "n/a", str(e)[:80], "")

    # 3. reversals
    try:
        rev = [c for c in closed if c["status"] == "reversed"]
        low = [c for c in rev if (c.get("confidence") or "").upper() == "LOW"]
        cutoff = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
        recent_low = [c for c in low if (c.get("closed") or "") >= cutoff]
        pnl = sum(c["pnl_inr"] for c in rev)
        status = "bad" if recent_low else ("watch" if rev else "ok")
        add("reversals", "Reversals only on conviction", status,
            f"{len(rev)} reversals · ₹{pnl:+,.0f}",
            f"{len(low)} were LOW-confidence calls ({len(recent_low)} in the last 7 days)",
            "0 LOW-confidence reversals; require ≥MEDIUM + 2-vote margin + 2 consecutive runs")
    except Exception as e:
        add("reversals", "Reversals", "watch", "n/a", str(e)[:80], "")

    # 4. thesis discipline (open positions)
    try:
        if opens:
            c = opens[-1]
            hours = (dt.datetime.now() - pd.Timestamp(c["filled_at"]).to_pydatetime()).total_seconds() / 3600
            planned = abs((c.get("target") or c["entry"]) - c["entry"]) * c["lots"] * c["lot_bbl"]
            unreal = c.get("unrealized_inr") or 0
            reaff = sum(1 for x in calls if x.get("status") == "reaffirmed" and x.get("reaffirms") == c["trade_id"])
            status = "bad" if (planned and unreal < -planned) or (hours > 48 and unreal < 0) else ("watch" if unreal < 0 else "ok")
            add("thesis", "No unmanaged theses", status,
                f"{c['trade_id']} {c['side']} · {hours:.0f} h open · M2M ₹{unreal:+,.0f}",
                f"{reaff} reaffirms since fill · planned reward ₹{planned:,.0f} · stop {c.get('stop')}",
                "re-vote with MFE/MAE at 48 h · exit at the stated invalidation, never sit past −1× planned reward")
        else:
            add("thesis", "No unmanaged theses", "ok", "book flat", "no open position", "re-vote at 48 h; exit at stated invalidation")
    except Exception as e:
        add("thesis", "Thesis discipline", "watch", "n/a", str(e)[:80], "")

    # 5. gate: approved vs gate-off trades
    try:
        from .counterfactuals import load_decisions
        gates = {d["decision_id"]: d["gate"] for d in load_decisions(reports_dir)}
        by = {}
        for c in closed:
            g = gates.get(c.get("decision_id"), "?")
            by.setdefault(g, []).append(c["pnl_inr"])
        parts = " · ".join(f"{g}: {len(v)} trades ₹{sum(v):+,.0f}" for g, v in sorted(by.items()))
        go = by.get("gate_off", [])
        status = "watch"
        add("gate", "Free the entries, gate the reversals", status,
            f"gate-off trades: {len(go)} · ₹{sum(go):+,.0f}", parts or "no closed trades",
            "gate-off P&L ≥ approved P&L per trade, with reversals hysteresis-gated")
    except Exception as e:
        add("gate", "Gate", "watch", "n/a", str(e)[:80], "")

    # 6. sizing
    try:
        seq = [int(c["lots"]) for c in trades if c["lot_bbl"] == 10]
        risks = [risk_inr(c) / equity * 100 for c in trades[-5:]] if equity else []
        latest = risks[-1] if risks else 0
        status = "bad" if latest > 2 else ("watch" if max(risks or [0]) > 2 else "ok")
        add("sizing", "Size from the stop, not the vote", status,
            f"latest trade risks {latest:.1f}% of equity" if risks else "no trades",
            f"mini-lot sequence {seq[-10:]} · max {max(seq) if seq else 0} lots",
            "lots = floor(1–2% equity ÷ (stop distance × 10)) · cap 4 lots until 30 trades")
    except Exception as e:
        add("sizing", "Sizing", "watch", "n/a", str(e)[:80], "")

    # 7. committee informativeness vs next-day direction
    try:
        v = votes if votes is not None else load_votes(reports_dir)
        if len(v) and len(ohlc) > 1:
            closes = dict(zip(ohlc["date"], ohlc["close"]))
            dates = sorted(closes)
            nxt = {d: closes[dates[i + 1]] for i, d in enumerate(dates[:-1])}
            v = v[v["date"].isin(nxt)].copy()
            v["up"] = v.apply(lambda r: nxt[r["date"]] > r["fair"], axis=1)
            base = v["up"].mean() * 100
            mods = [m for m in ("technical", "fundamentals", "news", "positioning", "term_structure", "basis") if m in v.columns]
            acc = {}
            for m in mods:
                sub = v[v[m].isin(["BULLISH", "BEARISH"])]
                if len(sub):
                    acc[m] = (((sub[m] == "BULLISH") == sub["up"]).mean() * 100, len(sub))
            best = max(acc.items(), key=lambda kv: kv[1][0]) if acc else None
            beats = [m for m, (a, k) in acc.items() if a > base + 5 and k >= 30]
            detail = " · ".join(f"{m} {a:.0f}%" for m, (a, k) in acc.items())
            status = "ok" if beats else "watch"
            add("committee", "Does the committee beat always-long?", status,
                f"always-long {base:.0f}% next-day · best {best[0]} {best[1][0]:.0f}%" if best else "n/a",
                detail + f" · n={len(v)} runs", "at least one module > always-long by 5pp with n ≥ 30")
        else:
            add("committee", "Committee informativeness", "watch", "insufficient data", "", "")
    except Exception as e:
        add("committee", "Committee", "watch", "n/a", str(e)[:80], "")

    # 8. cadence & churn (last 3 sessions)
    try:
        v = votes if votes is not None else load_votes(reports_dir)
        days = sorted(v["date"].unique())[-3:]
        vv = v[v["date"].isin(days)].sort_values("ts")
        per_day = vv.groupby("date").size()
        flips = int(vv.groupby("date")["trader_dir"].apply(lambda x: (x != x.shift()).sum() - 1).sum())
        gaps = vv.groupby("date")["ts"].diff().dt.total_seconds().dropna() / 60
        med_gap = float(gaps.median()) if len(gaps) else 0
        fpd = flips / max(len(days), 1)
        status = "bad" if fpd > 6 else ("watch" if per_day.mean() > 12 else "ok")
        add("cadence", "Decide less, decide better", status,
            f"{per_day.mean():.0f} runs/day · {fpd:.1f} direction flips/day",
            f"median gap {med_gap:.0f} min over {', '.join(days)}",
            "≤ 8 runs/day (anchors + hardened event gate) · ≤ 3 flips/day")
    except Exception as e:
        add("cadence", "Cadence", "watch", "n/a", str(e)[:80], "")

    # 9. books: fills inside the real range, no overlap, fresh scheduler
    try:
        rng = {r["date"]: (r["low"], r["high"]) for _, r in ohlc.iterrows()}
        phantom = []
        for c in closed:
            px = c.get("close_price"); d = (c.get("closed") or "").replace("-", "")
            if px and d in rng and not (rng[d][0] - 1 <= px <= rng[d][1] + 1):
                phantom.append(c["trade_id"])
        # overlapping same-side filled positions
        overlap = 0
        iv = [(pd.Timestamp(c["filled_at"]), pd.Timestamp(c["closed"]) + pd.Timedelta(days=1) if c.get("closed") else pd.Timestamp.now(), c["side"]) for c in trades if c.get("filled_at")]
        for i, a in enumerate(iv):
            for b in iv[i + 1:]:
                if a[2] == b[2] and a[0] < b[1] and b[0] < a[1]:
                    overlap += 1
        stale, note = _scheduler_stale()
        status = "bad" if (phantom or stale) else ("watch" if overlap else "ok")
        add("books", "Books that reproduce from ticks", status,
            f"{len(phantom)} phantom exits · {overlap} overlapping positions · scheduler {'STALE' if stale else 'fresh'}",
            (", ".join(phantom) + " · " if phantom else "") + note,
            "0 exits outside the day's traded range · ≤ 1 open position per side · restart scheduler after code edits")
    except Exception as e:
        add("books", "Books", "watch", "n/a", str(e)[:80], "")

    # 10. cost vs P&L
    try:
        usd = 0.0; days_u = set()
        if usage_path and Path(usage_path).exists():
            cutoff = (dt.datetime.now() - dt.timedelta(days=7)).isoformat()
            for l in open(usage_path):
                try:
                    u = json.loads(l)
                    if u["ts"] >= cutoff:
                        usd += u["usd"]; days_u.add(u["ts"][:10])
                except Exception:
                    pass
        inr_day = usd * 96 / max(len(days_u), 1)
        pnl7 = sum(c["pnl_inr"] for c in closed if (c.get("closed") or "") >= (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d"))
        status = "ok" if inr_day < 400 else "watch"
        add("cost", "Structure over model", status, f"₹{inr_day:,.0f}/day LLM spend (7-day avg)",
            f"realized P&L last 7 days ₹{pnl7:+,.0f}", "spend < ₹400/day; effort goes to risk layer, sizing, cadence — not model upgrades")
    except Exception as e:
        add("cost", "Cost", "watch", "n/a", str(e)[:80], "")
    return out


if __name__ == "__main__":  # self-check on real data
    import sys
    root = Path(__file__).parent.parent
    from tradingagents_mcx.ledger import Ledger
    led = Ledger(str(root / "ledger"), cost_inr_per_lot=80)
    acct = led.account(1_000_000)
    mons = compute(led.load(), str(root / "reports"), str(root / "cache"), acct["equity"], str(root / "cache" / "llm_usage.jsonl"))
    assert len(mons) == 10 and all(m["status"] in ("ok", "watch", "bad") for m in mons)
    for m in mons:
        print(f"{ {'ok': '🟢', 'watch': '🟡', 'bad': '🔴'}[m['status']] } {m['title']}: {m['headline']}\n     {m['detail'][:150]}")
