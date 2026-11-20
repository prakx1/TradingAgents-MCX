"""Reflection memory — the desk's continuous-learning loop, hardened per the
2026 evidence (SHARP, ReasoningBank, TradingAgents-2026, Xiong et al.):

  * lessons are SCHEMA-BOUND (setup tag, mistake tag, direction_correct, <=3 sentences,
    setup-level, no hindsight prices) and validated before storage — free prose reflection
    measured worse than no learning at all;
  * outcome embargo + point-in-time gating: written only from closed trades, retrieved only
    if resolved before the decision (as_of);
  * retrieval: feature match (no embeddings), k=3, recency on resolution time, dedupe by
    (side, regime, mistake) so five near-duplicates never crowd the prompt;
  * utility counters (retrieved_n / cited_n) so useless lessons can be retired at review.
Rule thresholds are never edited here — the weekly review proposes, a human approves.
"""
import datetime as dt
import glob
import json
import math
import re
from pathlib import Path

import pandas as pd

SETUP_TAGS = ("trend-continuation", "counter-trend", "breakout", "mean-reversion", "news-driven", "other")
MISTAKE_TAGS = ("none", "stop-too-wide", "chased-entry", "weak-vote-flip", "held-underwater",
                "sized-too-big", "target-too-tight", "premature-exit", "other")
HOUR_BUCKETS = ((9, 12, "morning"), (12, 17, "afternoon"), (17, 24, "evening/US"))


def _hour_bucket(ts: str) -> str:
    try:
        h = int(str(ts)[11:13])
    except Exception:
        return "?"
    return next((name for lo, hi, name in HOUR_BUCKETS if lo <= h < hi), "?")


def regime_from_daily(daily: pd.DataFrame, on_date: str) -> str:
    """5-session change of MCX close at a date: > +2% uptrend, < -2% downtrend, else range."""
    if daily is None or not len(daily):
        return "?"
    d = daily[daily["date"] <= on_date.replace("-", "")]
    if len(d) < 3:
        return "?"
    chg = d["close"].iloc[-1] / d["close"].iloc[max(0, len(d) - 6)] - 1
    return "uptrend" if chg > 0.02 else "downtrend" if chg < -0.02 else "range"


def situation_features(tally: dict, daily: pd.DataFrame, now: dt.datetime | None = None) -> dict:
    """Features of the decision about to be made (before the trader speaks): lean from votes."""
    now = now or dt.datetime.now()
    b, s = int(tally.get("BULLISH", 0)), int(tally.get("BEARISH", 0))
    side = "LONG" if b > s else "SHORT" if s > b else None
    margin = abs(b - s)
    conf = "HIGH" if margin >= 3 else "MEDIUM" if margin == 2 else "LOW"
    return {"side": side, "confidence": conf, "vote_margin": margin,
            "regime": regime_from_daily(daily, now.strftime("%Y-%m-%d")), "hour": _hour_bucket(now.strftime("%Y-%m-%d %H:%M"))}


class Memory:
    def __init__(self, root: str | Path):
        self.dir = Path(root) / "memory"
        self.dir.mkdir(exist_ok=True)
        self.path = self.dir / "lessons.jsonl"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def _save(self, mem: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(m) + "\n" for m in mem))
        tmp.replace(self.path)

    # ------------------------------------------------------------- reflect
    def reflect_pending(self, calls: list[dict], reports_dir: str, daily: pd.DataFrame, llm) -> list[dict]:
        done = {m.get("trade_id") for m in self.load()}  # decision-day lessons carry no trade_id
        new = []
        for c in calls:
            if not c.get("trade_id") or c["status"] == "open" or c.get("pnl_inr") is None or c["trade_id"] in done:
                continue
            if not c.get("filled_at") or not (c.get("lots") or 0) or not c.get("closed"):
                continue
            rep = self._report_for(c, reports_dir)
            tally = (rep or {}).get("trader", {}).get("module_tally") or {}
            vote_margin = c.get("vote_margin")
            if vote_margin is None and tally:
                vote_margin = abs(int(tally.get("BULLISH", 0)) - int(tally.get("BEARISH", 0)))
            filled = pd.Timestamp(c["filled_at"])
            mark_ts = c.get("unrealized_at") or ""
            resolved = pd.Timestamp(mark_ts) if mark_ts and pd.Timestamp(mark_ts) >= filled else pd.Timestamp(c["closed"] + " 23:30:00")
            if resolved < filled:
                resolved = filled
            risk = abs(c["entry"] - (c.get("stop") or c["entry"])) * c["lots"] * c["lot_bbl"]
            feats = {"side": c["side"], "confidence": (c.get("confidence") or "?").upper(), "vote_margin": vote_margin,
                     "regime": regime_from_daily(daily, c["filled_at"][:10]), "hour": _hour_bucket(c["filled_at"]),
                     "stop_pct": round(abs(c["entry"] - c["stop"]) / c["entry"] * 100, 1) if c.get("stop") else None,
                     "reward_risk": c.get("reward_risk")}
            outcome = {"pnl_inr": c["pnl_inr"], "r": round(c["pnl_inr"] / risk, 2) if risk else None,
                       "status": c["status"], "close_reason": c.get("close_reason") or c["status"],
                       "hold_h": round((resolved - filled).total_seconds() / 3600, 1), "lots": c["lots"],
                       "entry": c["entry"], "stop": c.get("stop"), "target": c.get("target"), "close_price": c.get("close_price")}
            reasoning = (rep or {}).get("trader", {}).get("reasoning", "")[-1200:]
            structured = self._write_lesson(llm, c, feats, outcome, reasoning)
            rec = {"id": f"L-{c['trade_id']}", "trade_id": c["trade_id"],
                   "written_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "resolved_at": resolved.strftime("%Y-%m-%d %H:%M:%S"),
                   "features": feats, "outcome": outcome, **structured,
                   "retrieved_n": 0, "cited_n": 0, "state": "active"}
            with open(self.path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            new.append(rec)
        return new

    @staticmethod
    def _report_for(c: dict, reports_dir: str) -> dict | None:
        try:
            did = c.get("decision_id", "")
            f = next(iter(glob.glob(str(Path(reports_dir) / f"*_{did.split('-')[-1][:4]}.json"))), None)
            return json.loads(Path(f).read_text()) if f else None
        except Exception:
            return None

    @staticmethod
    def _write_lesson(llm, c, feats, outcome, reasoning) -> dict:
        facts = (f"Trade: {c['side']} {c['lots']} lot(s), stop {feats['stop_pct']}% from entry, confidence {feats['confidence']}, "
                 f"vote margin {feats['vote_margin']}, regime {feats['regime']}, {feats['hour']} session.\n"
                 f"Outcome: {outcome['close_reason']}, {outcome['r']}R (₹{outcome['pnl_inr']:+,.0f}), held {outcome['hold_h']} h.\n"
                 f"Trader's plan at entry (excerpt): {reasoning[-900:] if reasoning else 'n/a'}")
        fallback = {"setup_tag": "other", "mistake_tag": "other", "direction_correct": (outcome.get("r") or 0) > 0,
                    "lesson": f"({outcome['close_reason']}, {outcome['r']}R) lesson unavailable."}
        try:
            out = llm.chat(
                "You are the desk's post-trade reviewer. Return ONLY a JSON object with keys: "
                f"setup_tag (one of {list(SETUP_TAGS)}), mistake_tag (one of {list(MISTAKE_TAGS)}; 'none' if the process was right "
                "even if the trade lost), direction_correct (true/false: did price move the trade's way meaningfully at any point), "
                "lesson (at most 3 sentences, about the SETUP CLASS not this trade: no specific prices, no dates, no news that was "
                "unknowable at entry; phrase as a rule the same committee can apply next time). Every word must earn its place.",
                facts, role="sentinel", max_tokens=700)
            m = re.search(r"\{.*\}", out, re.S)
            d = json.loads(m.group(0)) if m else {}
            return {"setup_tag": d.get("setup_tag") if d.get("setup_tag") in SETUP_TAGS else "other",
                    "mistake_tag": d.get("mistake_tag") if d.get("mistake_tag") in MISTAKE_TAGS else "other",
                    "direction_correct": bool(d.get("direction_correct", fallback["direction_correct"])),
                    "lesson": " ".join(str(d.get("lesson", "")).split())[:600] or fallback["lesson"]}
        except Exception:
            return fallback

    # ------------------------------------------------------------ retrieve
    def retrieve(self, feats: dict, k: int = 3, as_of: dt.datetime | None = None) -> list[dict]:
        as_of = as_of or dt.datetime.now()
        mem = [m for m in self.load() if m.get("state", "active") == "active" and m.get("kind", "trade") == "trade"
               and pd.Timestamp(m.get("resolved_at") or m.get("written_at")) <= as_of]  # point-in-time gate
        if not mem:
            return []

        def score(m):
            f = m["features"]; s = 0.0
            s += 3.0 if f.get("side") == feats.get("side") else 0.0
            s += 2.0 if f.get("regime") == feats.get("regime") else 0.0
            s += 1.5 if f.get("confidence") == feats.get("confidence") else 0.0
            try:
                s += 1.0 if abs(int(f.get("vote_margin") or 0) - int(feats.get("vote_margin") or 0)) <= 1 else 0.0
            except Exception:
                pass
            s += 0.5 if f.get("hour") == feats.get("hour") else 0.0
            age_d = max(0.0, (as_of - pd.Timestamp(m.get("resolved_at") or m["written_at"]).to_pydatetime()).days)
            return s + math.exp(-age_d / 30)

        out, seen = [], set()
        for m in sorted(mem, key=score, reverse=True):
            key = (m["features"].get("side"), m["features"].get("regime"), m.get("mistake_tag"))
            if key in seen:  # dedupe near-duplicates so the block carries variety, not a chorus
                continue
            seen.add(key); out.append(m)
            if len(out) >= k:
                break
        return out

    def lessons_for(self, feats: dict, k: int = 3, as_of: dt.datetime | None = None) -> tuple[str, list[str]]:
        top = self.retrieve(feats, k, as_of)
        if not top:
            return "", []
        as_of = as_of or dt.datetime.now()
        similar = [m for m in self.load() if m.get("kind", "trade") == "trade" and m["features"].get("side") == feats.get("side")
                   and m["features"].get("regime") == feats.get("regime")
                   and pd.Timestamp(m.get("resolved_at") or m["written_at"]) <= as_of]
        agg = ""
        if similar:
            wins = sum(1 for m in similar if (m["outcome"].get("pnl_inr") or 0) > 0)
            avg_r = sum(m["outcome"].get("r") or 0 for m in similar) / len(similar)
            agg = (f"Your record in this setup ({feats.get('side')} in a {feats.get('regime')}): {len(similar)} trades, "
                   f"{wins} wins, average {avg_r:+.2f}R.\n")
        lines = "\n".join(f"- {m['id']} [{m['features'].get('regime')}, {m['features'].get('confidence')}, "
                          f"{m.get('mistake_tag')}, {m['outcome'].get('r')}R] {m['lesson']}" for m in top)
        block = (f"\n\n[LESSONS FROM YOUR OWN PAST TRADES — most similar setups first; evidence, not orders. "
                 f"If one influences this decision, name its id in the LESSONS USED line]\n{agg}{lines}\n")
        return block, [m["id"] for m in top]

    def lessons_block(self, feats: dict, k: int = 3) -> str:  # backwards-compatible
        return self.lessons_for(feats, k)[0]

    def bump(self, ids: list[str], field: str) -> None:
        """Utility counters: retrieved_n when injected, cited_n when the trader names it."""
        if not ids:
            return
        mem = self.load()
        for m in mem:
            if m["id"] in ids:
                m[field] = int(m.get(field, 0)) + 1
        self._save(mem)

    # --------------------------------------------- decision-day reflection (480+ samples)
    DAY_VERDICTS = ("too-cautious", "too-active", "well-calibrated", "wrong-direction", "mixed")

    @staticmethod
    def pending_decision_days(dtab: pd.DataFrame, done_ids: set, up_to: str) -> list[str]:
        year = str(dt.date.today().year)
        days = sorted({f"{year}-{d[2:4]}-{d[4:6]}" for d in dtab["decision"]})
        return [d for d in days if d < up_to and f"DD-{d.replace('-', '')}" not in done_ids]

    def reflect_decision_days(self, dtab: pd.DataFrame, llm, up_to: str, daily: pd.DataFrame | None = None) -> list[dict]:
        """One lesson per trading DAY over ALL Council decisions (stand-asides, vetoes, flips),
        scored by the market-fill counterfactual of the trader's direction. Feeds the CIO."""
        done = {m["id"] for m in self.load()}
        new = []
        for day in self.pending_decision_days(dtab, done, up_to):
            g = dtab[dtab["decision"].str[2:6] == day[5:7] + day[8:10]]
            if not len(g):
                continue
            cf = pd.to_numeric(g["counterfactual ₹ (market fill)"], errors="coerce")
            dirs = g["trader"].str.split(" ").str[0]
            aside = g["CIO"] == "STAND ASIDE"          # what the CIO wanted to skip (regardless of the gate)
            executed = g["trade"].astype(str).str.startswith("T-")  # real new positions opened
            st = {"runs": int(len(g)), "bullish": int((dirs == "BULLISH").sum()), "bearish": int((dirs == "BEARISH").sum()),
                  "stand_aside": int(aside.sum()), "traded": int(executed.sum()),
                  "flips": int(max(0, (dirs != dirs.shift()).sum() - 1)),
                  "vetoed_cf_inr": round(float(cf[aside].sum())), "taken_cf_inr": round(float(cf[~aside].sum())),
                  "cf_positive_pct": round(float((cf > 0).mean() * 100)) if cf.notna().any() else None}
            regime = regime_from_daily(daily, day) if daily is not None else "?"
            facts = (f"Trading day {day}, regime {regime}. Council ran {st['runs']} times: trader BULLISH {st['bullish']}x, "
                     f"BEARISH {st['bearish']}x, direction flips {st['flips']}. CIO stood aside {st['stand_aside']}x; trades taken {st['traded']}. "
                     f"Counterfactual (market-fill of the trader's direction, post-cost): the calls the CIO wanted to SKIP would have made "
                     f"₹{st['vetoed_cf_inr']:+,}; the calls it approved made ₹{st['taken_cf_inr']:+,}; {st['cf_positive_pct']}% of directional calls were profitable.")
            verdict, lesson = "mixed", ""
            try:
                out = llm.chat(
                    "You are the desk's decision auditor. Return ONLY a JSON object: verdict (one of "
                    f"{list(self.DAY_VERDICTS)}) and lesson (at most 3 sentences about the DECISION PROCESS — when this committee "
                    "should trust its direction, stand aside, or stop re-deciding; no prices, no dates, no unknowable news).",
                    facts, role="sentinel", max_tokens=600)
                m = re.search(r"\{.*\}", out, re.S)
                d = json.loads(m.group(0)) if m else {}
                verdict = d.get("verdict") if d.get("verdict") in self.DAY_VERDICTS else "mixed"
                lesson = " ".join(str(d.get("lesson", "")).split())[:500]
            except Exception:
                pass
            rec = {"id": f"DD-{day.replace('-', '')}", "kind": "decision-day", "day": day,
                   "written_at": dt.datetime.now().isoformat(timespec="seconds"), "resolved_at": f"{day} 23:30:00",
                   "features": {"regime": regime, **st}, "outcome": {"vetoed_cf_inr": st["vetoed_cf_inr"], "taken_cf_inr": st["taken_cf_inr"]},
                   "verdict": verdict, "lesson": lesson or f"({verdict}) lesson unavailable", "retrieved_n": 0, "cited_n": 0, "state": "active"}
            with open(self.path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            new.append(rec)
        return new

    def cio_block(self, k: int = 3, as_of: dt.datetime | None = None) -> str:
        """The CIO's memory: the last k decision-day lessons (recency; point-in-time gated)."""
        as_of = as_of or dt.datetime.now()
        days = [m for m in self.load() if m.get("kind") == "decision-day" and m.get("state", "active") == "active"
                and pd.Timestamp(m["resolved_at"]) <= as_of]
        days = sorted(days, key=lambda m: m["day"], reverse=True)[:k]
        if not days:
            return ""
        lines = "\n".join(f"- {m['day']} ({m['features'].get('regime')}): {m['features']['runs']} runs, stood aside {m['features']['stand_aside']}x, "
                           f"opened {m['features']['traded']} position(s); the calls you wanted to skip would have made ₹{m['outcome']['vetoed_cf_inr']:+,}; "
                           f"verdict {m['verdict']}. {m['lesson']}" for m in days)
        return (f"\n\n=== Your recent decision record (one line per day; evidence, not orders) ===\n{lines}\n"
                "Caveat: the counterfactual sums overlapping 30-minute calls as if capital were unlimited — read it as "
                "direction quality per call, not as money left on the table. A verdict from one regime does not transfer to another.\n")



if __name__ == "__main__":  # self-check: schema, point-in-time gate, dedupe, counters
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        m = Memory(d)
        now = dt.datetime(2026, 9, 9, 12, 0)
        recs = [("LONG", "uptrend", "LOW", 0.8, "none", "2026-09-01 12:00:00"),
                ("SHORT", "uptrend", "LOW", -0.8, "weak-vote-flip", "2026-09-02 12:00:00"),
                ("SHORT", "uptrend", "LOW", -0.5, "weak-vote-flip", "2026-09-03 12:00:00"),   # duplicate class
                ("SHORT", "uptrend", "LOW", -0.3, "chased-entry", "2026-09-04 12:00:00"),
                ("SHORT", "uptrend", "LOW", -0.9, "stop-too-wide", "2026-09-20 12:00:00")]    # future: must be gated out
        with open(m.path, "a") as fh:
            for i, (side, reg, conf, r, mis, res) in enumerate(recs):
                fh.write(json.dumps({"id": f"L-T{i}", "trade_id": f"T{i}", "written_at": "2026-09-09T11:00:00", "resolved_at": res,
                                     "features": {"side": side, "regime": reg, "confidence": conf, "vote_margin": 1, "hour": "morning"},
                                     "outcome": {"pnl_inr": r * 1000, "r": r}, "mistake_tag": mis, "setup_tag": "counter-trend",
                                     "lesson": f"lesson {i}", "retrieved_n": 0, "cited_n": 0, "state": "active"}) + "\n")
        feats = {"side": "SHORT", "regime": "uptrend", "confidence": "LOW", "vote_margin": 1, "hour": "morning"}
        top = m.retrieve(feats, k=3, as_of=now)
        ids = [t["id"] for t in top]
        assert "L-T4" not in ids, ids                       # point-in-time gate
        assert not ("L-T1" in ids and "L-T2" in ids), ids   # dedupe same (side, regime, mistake)
        assert ids[0] in ("L-T1", "L-T2", "L-T3") and len(ids) == 3, ids
        blk, used = m.lessons_for(feats, as_of=now)
        assert "3 trades, 0 wins" in blk and used == ids, blk
        m.bump(ids[:1], "retrieved_n"); m.bump(ids[:1], "cited_n")
        rec = next(x for x in m.load() if x["id"] == ids[0]); assert rec["retrieved_n"] == 1 and rec["cited_n"] == 1
        daily = pd.DataFrame({"date": ["20260901", "20260902", "20260903", "20260904", "20260905", "20260908"], "close": [8000, 8100, 8200, 8300, 8400, 8500]})
        assert regime_from_daily(daily, "2026-09-08") == "uptrend"
        class _Stub:
            def chat(self, *a, **k): return '{"verdict": "too-cautious", "lesson": "Trust a wide vote margin in a trend; stop re-deciding every quarter hour."}'
        dtab = pd.DataFrame({"decision": ["D-0908-1011", "D-0908-1047", "D-0908-1117", "D-0909-1000"],
                             "trader": ["BULLISH (LOW)", "BULLISH (MEDIUM)", "NEUTRAL (LOW)", "BULLISH (LOW)"],
                             "CIO": ["STAND ASIDE", "STAND ASIDE", "STAND ASIDE", "CAUTIOUS LONG"],
                             "traded?": ["🚫 not traded", "🚫 not traded", "— (vote tie)", "✅ traded"],
                             "trade": ["—", "—", "—", "T-0909-1000"],
                             "counterfactual ₹ (market fill)": [1200.0, 800.0, None, 300.0]})
        dd = m.reflect_decision_days(dtab, _Stub(), up_to="2026-09-09", daily=daily)
        assert len(dd) == 1 and dd[0]["id"] == "DD-20260908" and dd[0]["features"]["vetoed_cf_inr"] == 2000 and dd[0]["verdict"] == "too-cautious", dd
        assert m.reflect_decision_days(dtab, _Stub(), up_to="2026-09-09", daily=daily) == []   # idempotent
        blk = m.cio_block(as_of=dt.datetime(2026, 9, 9, 12))
        assert "2026-09-08" in blk and "₹+2,000" in blk, blk
        assert all(x.get("kind", "trade") == "trade" for x in m.retrieve(feats, k=5, as_of=now))  # trade retrieval unaffected
        print("memory self-check OK (point-in-time gate, dedupe, block, counters, regime, decision-day reflection, CIO block)")
