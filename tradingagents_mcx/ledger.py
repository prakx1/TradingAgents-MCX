"""Paper-trade ledger — every directional CIO call is recorded and marked to market.

A call is opened when the CIO decision is any LONG/SHORT (STAND ASIDE is logged but
not tracked). Marking uses daily highs/lows of the MCX-INR series: stop checked
before target on the same bar (conservative). Calls expire at the front contract's
expiry or after `max_hold_days`.

Storage: ledger/calls.jsonl — one JSON object per call, rewritten in place on update.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd

MAX_HOLD_DAYS = 20  # ponytail: fixed horizon; per-call horizons if trader output warrants it


def _market_open(now: dt.datetime) -> bool:
    """MCX crude session (IST). ponytail: fixed 23:30 close; the winter 23:55 close only makes the roll gate more conservative."""
    return now.weekday() < 5 and dt.time(9, 0) <= now.time() <= dt.time(23, 30)


def _parse_zone(text: str | None) -> float | None:
    """'8,050-8,100' -> 8075.0 (zone mean); '8,128 / 8,320 / 8,989' -> 8128.0 (T1);
    '8320' -> 8320.0; 'WAIT'/'N/A' -> None. Commas stripped first — Indian number
    formatting otherwise shatters into fragments."""
    if not text:
        return None
    cleaned = str(text).replace(",", "")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", cleaned)]
    nums = [n for n in nums if n >= 100]  # drop percentages/lot counts riding along
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0]
    if re.search(r"\d\s*[-–—]\s*\d", cleaned):  # a zone like 7990-8010 -> mean
        return (nums[0] + nums[1]) / 2
    return nums[0]  # a list like 8200 / 8400 -> first target is the marking level


def _lots(capital: float, position_pct: str | None, price: float, margin_pct: float, lot_bbl: int) -> int:
    pcts = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", position_pct or "")]
    pct = (sum(pcts) / len(pcts) if pcts else 3.0) / 100
    margin_per_lot = price * lot_bbl * margin_pct / 100
    return max(1, int(capital * pct / margin_per_lot)) if margin_per_lot else 1


import fcntl
import os
import threading

_LOCK = threading.Lock()  # thread-safety inside one process (Streamlit fragments)


class Ledger:
    BLOCKED = ("reaffirmed", "no_margin", "halted", "blocked_roll", "rejected_rr", "rejected_risk",
               "held", "flattened", "limit_positions")

    def __init__(self, ledger_dir: str, cost_inr_per_lot: float = 0.0,
                 execution: str = "market", risk: dict | None = None):
        """cost_inr_per_lot: round-trip transaction cost per lot (~Rs 180 for MCX
        crude at a discount broker), deducted from P&L on every filled trade.
        execution: 'market' = agentic policy, enter at the live price the moment
        the call is issued (always filled); 'limit' = wait for the AI's entry
        price (may never fill / can be missed)."""
        self.path = Path(ledger_dir) / "calls.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lockfile = self.path.with_suffix(".lock")
        self.cost = cost_inr_per_lot
        self.execution = execution
        self.risk = risk or {}  # P0 risk layer, every rule switchable (see config analysis.risk)
        self.root = Path(ledger_dir).parent  # repo root: cache/live/latest_*.json and events/ live here
        self.kill_file = self.root / "events" / "KILL"

    def _flock(self):
        """Process-level lock (CLI runs vs the dashboard)."""
        fd = open(self._lockfile, "w")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # partial line from a torn write — skip, never crash
        return out

    def _write(self, calls: list[dict]) -> None:
        # Safety net: never overwrite real data with emptiness (the wipe-bug guard)
        if not calls and self.path.exists() and self.path.stat().st_size > 0:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(c) for c in calls) + ("\n" if calls else ""))
        os.replace(tmp, self.path)  # atomic on POSIX — readers see old or new, never torn

    def record(self, result: dict, capital_inr: float, contract_spec: dict) -> dict:
        with _LOCK:
            fd = self._flock()
            try:
                return self._record(result, capital_inr, contract_spec)
            finally:
                fd.close()

    def margin_per_lot(self, price: float, contract_spec: dict) -> float:
        broker = float(self.risk.get("broker_margin_per_lot_inr", 0) or 0)
        return broker or price * contract_spec["lot_size_bbl"] * contract_spec["typical_margin_pct"] / 100

    def real_plan(self, entry: float, stop: float, lot_bbl: int, margin_per_lot: float) -> dict | None:
        """Translate a paper call into the operator's REAL account: lots from the
        rupee-risk budget, capped so margin never exceeds 60% of the account."""
        acc = float(self.risk.get("real_account_inr", 0) or 0)
        if not acc:
            return None
        rp = float(self.risk.get("risk_per_trade_pct", 1) or 1)
        dist = abs(entry - stop)
        lots_risk = int(acc * rp / 100 // (dist * lot_bbl)) if dist else 0
        lots_margin = int(acc * 0.6 // margin_per_lot) if margin_per_lot else 0
        lots = max(0, min(lots_risk, lots_margin))
        return {"account_inr": acc, "lots": lots, "margin_inr": round(lots * margin_per_lot),
                "risk_inr": round(lots * dist * lot_bbl),
                "binding": "risk budget" if lots_risk <= lots_margin else "margin"}

    def _flatten(self, calls: list[dict], ref_price: float, reason: str,
                 new_trade_id: str | None = None, only_side: str | None = None) -> int:
        """Close open positions at market and book the P&L (reversal, halt, hysteresis flatten)."""
        n = 0
        for old in calls:
            if old["status"] != "open" or not old.get("side") or (only_side and old["side"] != only_side):
                continue
            sgn = 1 if old["side"] == "LONG" else -1
            if old.get("filled_at"):
                gross = (ref_price - old["entry"]) * sgn * old["lots"] * old["lot_bbl"]
                old["costs_inr"] = round(self.cost * old["lots"], 0)
                old["pnl_inr"] = round(gross - old["costs_inr"], 0)
            else:
                old["pnl_inr"] = 0.0
            old.update(status="reversed" if reason == "reversal" else "closed", close_reason=reason,
                       closed=str(dt.date.today()), close_price=ref_price, unrealized_inr=None)
            if new_trade_id:
                old["reversed_by"] = new_trade_id
            n += 1
        return n

    def _period_pnl(self, calls: list[dict], since: str) -> float:
        realized = sum(c.get("pnl_inr") or 0 for c in calls
                       if c.get("side") and c["status"] not in ("open",) and (c.get("closed") or "") >= since)
        unreal = sum(c.get("unrealized_inr") or 0 for c in calls if c["status"] == "open" and c.get("filled_at"))
        return realized + unreal

    def _record(self, result: dict, capital_inr: float, contract_spec: dict) -> dict:
        decision = result["cio"]["final_decision"]
        t = result["trader"]
        r = self.risk
        ref_price, ref_source = result["data"]["mcx_fair_value_inr"], "parity (WTI x USDINR)"
        contract_expiry, month_expired = None, False
        try:  # the traded contract's own print: live during the session, the close after it — never parity when a print exists
            _latest = json.loads((self.root / "cache" / "live" / f"latest_{contract_spec.get('feed_symbol', 'CRUDEOIL')}.json").read_text())
            _now = dt.datetime.now()
            _age = (_now - dt.datetime.strptime(_latest["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds()
            _open = _market_open(_now)
            _exp = str(_latest.get("expiry") or "")[:10] or None
            # the month in that print has expired: after its expiry-day close, or any later day
            month_expired = bool(_exp) and (_exp < _now.date().isoformat() or (_exp == _now.date().isoformat() and not _open))
            _ltp = float(_latest.get("ltp") or 0)
            if _ltp > 0 and (_age <= 1800 or (not _open and _age <= 4 * 86400)):
                ref_price, ref_source, contract_expiry = _ltp, f"contract print {_latest['ts'][5:16]}", _exp
        except Exception:
            pass
        side = "LONG" if "LONG" in decision else "SHORT" if "SHORT" in decision else None
        lot_bbl = contract_spec["lot_size_bbl"]
        margin_pct = contract_spec["typical_margin_pct"]
        conf = (t.get("confidence") or "LOW").upper()
        tally = t.get("module_tally") or {}
        vote_margin = abs(int(tally.get("BULLISH", 0)) - int(tally.get("BEARISH", 0)))

        def sane(level):  # prose stops ("spread widens >+300") parse to junk — keep only price-like levels
            return level if level and 0.7 * ref_price <= level <= 1.3 * ref_price else None

        now = dt.datetime.now()
        if self.execution == "market" and side:
            entry = ref_price  # agentic policy: enter NOW at the live price, no waiting
            filled_at = now.strftime("%Y-%m-%d %H:%M:%S")
        else:
            entry = sane(_parse_zone(t["entry"])) or ref_price
            filled_at = None
        call = {
            "id": f"{result['symbol']}_{now:%Y%m%d_%H%M%S}",
            "contract_expiry": contract_expiry, "ref_source": ref_source,
            "trade_id": f"T-{now:%m%d-%H%M}" if side else None,
            "decision_id": f"D-{now:%m%d-%H%M}",
            "opened": result["analysis_date"], "opened_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": result["symbol"], "filled_at": filled_at,
            "execution": self.execution,
            "decision": decision, "side": side, "trader_dir": t.get("direction"),
            "entry": entry,
            "stop": sane(_parse_zone(t["stop"])), "target": sane(_parse_zone(t["target"])),
            "lots": 0,  # sized below from CURRENT equity with margin + risk enforcement
            "lot_bbl": lot_bbl,
            "confidence": conf, "vote_margin": vote_margin, "position_pct": t["position_pct"],
            "ref_price_at_open": ref_price,
            "status": "open" if side else "no_trade",
            "closed": None, "close_price": None, "pnl_inr": None,
        }
        existing = {c.get("trade_id") for c in self.load()} | {c.get("decision_id") for c in self.load()}
        for key in ("trade_id", "decision_id"):  # minute-resolution ids collide when two runs land in one minute
            base, n = call[key], 2
            while base is not None and call[key] in existing:
                call[key] = f"{base}-{n}"; n += 1
        # An agentic position must never run unprotected: if the trader's stop was
        # prose/unparseable, apply the risk framework's default (2.5% from entry).
        if side and call["stop"] is None:
            sgn = 1 if side == "LONG" else -1
            call["stop"] = round(entry * (1 - sgn * 0.025), 1)
            call["stop_source"] = "auto-2.5%"
        # P0: working-stop cap — a stop further than cap% is decorative, clamp it
        cap = float(r.get("stop_cap_pct", 0) or 0)
        if side and cap:
            sgn = 1 if side == "LONG" else -1
            cap_level = round(entry * (1 - sgn * cap / 100), 1)
            if (side == "LONG" and call["stop"] < cap_level) or (side == "SHORT" and call["stop"] > cap_level):
                call["trader_stop"] = call["stop"]
                call["stop"] = cap_level
                call["stop_source"] = f"cap-{cap:g}%"

        calls = self.load()
        today = str(dt.date.today())

        def block(status, why):
            call.update(status=status, blocked_by=why, lots=0, trade_id=None, filled_at=None)

        # P0: kill switch + daily/weekly loss limits — flatten and refuse new entries
        if side:
            eq0 = self.account(capital_inr, margin_pct, calls=calls)["equity"]
            dl = float(r.get("daily_loss_limit_pct", 0) or 0)
            wl = float(r.get("weekly_loss_limit_pct", 0) or 0)
            week = str(dt.date.today() - dt.timedelta(days=7))
            halt = None
            if self.kill_file.exists():
                halt = "kill switch"
            elif dl and self._period_pnl(calls, today) <= -eq0 * dl / 100:
                halt = f"daily loss limit {dl:g}% of equity"
            elif wl and self._period_pnl(calls, week) <= -eq0 * wl / 100:
                halt = f"weekly loss limit {wl:g}% of equity"
            if halt:
                self._flatten(calls, ref_price, halt)
                block("halted", halt)

        # Expiry roll: the only print we have is of a month that has expired — the next month's expiry is unknown
        # until the feed rolls at 09:00, so a position cannot be booked honestly. Re-decided at the opening bell.
        if side and call["status"] == "open" and month_expired:
            block("blocked_roll", f"front month {contract_expiry or '?'} expired at the close; next-month expiry unknown until the feed rolls")

        # P0: reward:risk floor at the working stop
        if side and call["status"] == "open":
            min_rr = float(r.get("min_reward_risk", 0) or 0)
            risk_dist = abs(entry - call["stop"])
            if min_rr and call.get("target") and risk_dist > 0:
                rr = abs(call["target"] - entry) / risk_dist
                call["reward_risk"] = round(rr, 2)
                if rr < min_rr:
                    block("rejected_rr", f"reward:risk {rr:.2f} below the {min_rr:g} floor")

        # Position-aware desk rules (one position per direction, no stacking):
        #   same-direction call while a position is open  -> REAFFIRMED, no new trade
        #   opposite-direction call                        -> REVERSE (P0: only with hysteresis)
        if side and call["status"] == "open":
            open_calls = [c for c in calls if c["status"] == "open" and c.get("side")]
            same = [c for c in open_calls if c["side"] == side]
            opposite = [c for c in open_calls if c["side"] != side]
            if same:
                call.update(status="reaffirmed", filled_at=None, lots=0, trade_id=None,
                            reaffirms=same[-1].get("trade_id"))
            elif opposite:
                need_conf = (r.get("reversal_min_confidence") or "").upper()
                if need_conf:  # hysteresis on
                    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
                    need_margin = int(r.get("reversal_min_margin", 2) or 0)
                    need_runs = int(r.get("reversal_consecutive_runs", 2) or 1)
                    prev = [c.get("trader_dir") for c in calls[-(need_runs - 1):]] if need_runs > 1 else []
                    consecutive = len(prev) == need_runs - 1 and all(pd_ == t.get("direction") for pd_ in prev)
                    cool_h = float(r.get("reversal_cooldown_hours", 0) or 0)
                    fills = [pd.Timestamp(c["filled_at"]) for c in opposite if c.get("filled_at")]
                    cooled = not fills or (now - max(fills).to_pydatetime()).total_seconds() >= cool_h * 3600
                    ok = (rank.get(conf, 0) >= rank.get(need_conf, 1) and vote_margin >= need_margin
                          and consecutive and cooled)
                    if not ok:
                        why = (f"reversal needs >={need_conf}, vote margin >={need_margin}, {need_runs} consecutive "
                               f"runs, {cool_h:g}h cooldown — got {conf}, margin {vote_margin}, "
                               f"consecutive={consecutive}, cooled={cooled}")
                        if rank.get(conf, 0) >= 1:
                            self._flatten(calls, ref_price, "flatten (hysteresis)", only_side=opposite[0]["side"])
                            block("flattened", why)
                        else:
                            block("held", why)
                if call["status"] == "open":
                    self._flatten(calls, ref_price, "reversal", new_trade_id=call["trade_id"],
                                  only_side=opposite[0]["side"])

        # Sizing AFTER any close is booked: compounding equity, P0 rupee-risk budget, lot cap, margin
        if side and call["status"] == "open":
            acct = self.account(capital_inr, margin_pct, calls=calls)
            want = _lots(acct["equity"], t["position_pct"], ref_price, margin_pct, lot_bbl)
            rp = float(r.get("risk_per_trade_pct", 0) or 0)
            risk_dist = abs(entry - call["stop"])
            if rp and risk_dist > 0:
                call["risk_budget_inr"] = round(acct["equity"] * rp / 100)
                want = min(want, int(call["risk_budget_inr"] // (risk_dist * lot_bbl)))
            mx = int(r.get("max_lots", 0) or 0)
            if mx:
                want = min(want, mx)
            mpl = self.margin_per_lot(ref_price, contract_spec)
            affordable = int(acct["free_margin"] // mpl) if mpl else 0
            if want < 1:
                block("rejected_risk", f"stop {risk_dist:.0f} pts too wide for the {rp:g}% risk budget "
                                       f"(₹{call.get('risk_budget_inr', 0):,})")
            elif affordable < 1:
                block("no_margin", "not enough free margin")
            else:
                call["lots"] = min(want, affordable)
                call["risk_inr"] = round(risk_dist * call["lots"] * lot_bbl)
                call["real_plan"] = self.real_plan(entry, call["stop"], lot_bbl, mpl)
            mxp = int(r.get("max_new_positions_per_day", 0) or 0)
            if call["status"] == "open" and mxp:
                n_today = sum(1 for c in calls if c.get("trade_id") and (c.get("lots") or 0) > 0
                              and (c.get("opened_ts") or "")[:10] == today)
                if n_today >= mxp:
                    block("limit_positions", f"already {n_today} new positions today (cap {mxp})")
        calls.append(call)
        per_side: dict = {}
        for c in calls:  # P0 invariant: never more than one open filled position per side
            if c["status"] == "open" and c.get("side") and c.get("filled_at"):
                per_side[c["side"]] = per_side.get(c["side"], 0) + 1
        assert all(v <= 1 for v in per_side.values()), f"ledger invariant: >1 open position per side {per_side}"
        self._write(calls)
        return call

    def settle_expired(self, front_expiry: str, settle_price_for) -> list[dict]:
        """Cash-settle open positions whose contract expired (the feed's front month moved on).
        MCX crude is cash-settled at the due-date rate; paper uses the expiring contract's
        last recorded price on its expiry day (settle_price_for(expiry_date) -> float | None).
        The next Council run sees a flat book and re-enters in the new month only if it still wants to."""
        with _LOCK:
            fd = self._flock()
            try:
                calls = self.load(); settled = []
                for c in calls:
                    exp = c.get("contract_expiry")
                    if c["status"] != "open" or not c.get("side") or not exp or exp >= front_expiry:
                        continue
                    px = settle_price_for(exp)
                    if px is None:
                        continue
                    sgn = 1 if c["side"] == "LONG" else -1
                    gross = (px - c["entry"]) * sgn * c["lots"] * c["lot_bbl"] if c.get("filled_at") else 0.0
                    c["costs_inr"] = round(self.cost * c["lots"], 0) if c.get("filled_at") else 0.0
                    c["pnl_inr"] = round(gross - c["costs_inr"], 0)
                    c.update(status="closed", close_reason=f"expiry-settled {exp}", closed=exp,
                             close_price=px, unrealized_inr=None)
                    settled.append(c)
                if settled:
                    self._write(calls)
                return settled
            finally:
                fd.close()

    def mark(self, ohlc_inr: pd.DataFrame) -> list[dict]:
        with _LOCK:
            fd = self._flock()
            try:
                return self._mark(ohlc_inr)
            finally:
                fd.close()

    def _mark(self, ohlc_inr: pd.DataFrame) -> list[dict]:
        """Mark open calls against an OHLC frame in INR/bbl (real MCX, parity daily,
        or intraday ticks with high=low=close).

        Honest-fill rule: a call only activates once price actually trades through
        its entry level (bar low <= entry <= bar high) AFTER the call was issued.
        Until then it is 'awaiting fill' — no P&L. Unfilled past MAX_HOLD_DAYS
        closes as 'unfilled' (excluded from win rate). Stop is checked before
        target on the same bar (conservative)."""
        calls = self.load()
        for c in calls:
            if c["status"] != "open":
                continue
            opened = pd.Timestamp(c.get("opened_ts") or c["opened"])
            window = ohlc_inr[ohlc_inr.index > opened]
            if window.empty:
                continue
            sign = 1 if c["side"] == "LONG" else -1
            filled = c.get("filled_at") is not None
            # Fill rule by approach side (catches gap-through fills the touch test misses):
            # entry below issuance price -> fills when traded at-or-below it;
            # entry above -> fills when traded at-or-above it.
            entry_from_below = c["entry"] <= (c.get("ref_price_at_open") or c["entry"])
            for ts, bar in window.iterrows():
                if not filled:
                    crossed = (bar["low"] <= c["entry"]) if entry_from_below else (bar["high"] >= c["entry"])
                    if crossed:
                        filled = True
                        c["filled_at"] = str(ts)
                        c["fill_price"] = c["entry"]  # at the limit; gap fills would be same-or-better for the trade
                        # falls through to same-bar stop/target checks below
                    elif c["target"] and ((bar["high"] >= c["target"]) if sign == 1 else (bar["low"] <= c["target"])):
                        # target reached before the entry ever filled: the setup is gone — cancel
                        c.update(status="missed_no_fill", closed=str(ts.date()),
                                 close_price=None, pnl_inr=0.0)
                        break
                    elif (ts - opened).days >= MAX_HOLD_DAYS:
                        c.update(status="unfilled", closed=str(ts.date()),
                                 close_price=None, pnl_inr=0.0)
                        break
                    else:
                        continue
                stop_hit = c["stop"] and ((bar["low"] <= c["stop"]) if sign == 1 else (bar["high"] >= c["stop"]))
                tgt_hit = c["target"] and ((bar["high"] >= c["target"]) if sign == 1 else (bar["low"] <= c["target"]))
                if stop_hit:
                    c.update(status="stopped", closed=str(ts.date()), close_price=c["stop"])
                    break
                if tgt_hit:
                    c.update(status="target_hit", closed=str(ts.date()), close_price=c["target"])
                    break
                if (ts - opened).days >= MAX_HOLD_DAYS:
                    c.update(status="expired", closed=str(ts.date()), close_price=float(bar["close"]))
                    break
            if c.get("filled_at") and c["status"] not in ("unfilled", "open"):
                # CLOSED: realized P&L, booked forever
                close_px = c["close_price"] if c["close_price"] is not None else float(window["close"].iloc[-1])
                gross = (close_px - c["entry"]) * sign * c["lots"] * c["lot_bbl"]
                c["costs_inr"] = round(self.cost * c["lots"], 0)
                c["pnl_inr"] = round(gross - c["costs_inr"], 0)
                c["unrealized_inr"] = None
            elif c.get("filled_at") and c["status"] == "open":
                # OPEN position: mark-to-market goes to unrealized — NEVER into realized P&L
                mtm = (float(window["close"].iloc[-1]) - c["entry"]) * sign * c["lots"] * c["lot_bbl"]
                c["unrealized_inr"] = round(mtm - self.cost * c["lots"], 0)
                c["pnl_inr"] = None
            elif c["status"] == "open":  # awaiting fill: no position exists — no P&L of any kind
                c["pnl_inr"] = None
                c["unrealized_inr"] = None
            if c["status"] == "open":
                c["unrealized_at"] = str(window.index[-1])
        self._write(calls)
        return calls

    def account(self, capital_start: float, margin_pct: float = 15.0, calls: list | None = None) -> dict:
        """The wallet: equity = start + realized P&L (compounds); margin blocked by
        open filled positions; free = equity - blocked. Fully derived from calls."""
        calls = self.load() if calls is None else calls
        realized = sum(c.get("pnl_inr") or 0 for c in calls
                       if c.get("side") and c["status"] not in ("open", "reaffirmed"))
        unrealized = sum(c.get("unrealized_inr") or 0 for c in calls
                         if c["status"] == "open" and c.get("filled_at"))
        blocked = sum(c["entry"] * c["lot_bbl"] * c["lots"] * margin_pct / 100
                      for c in calls if c["status"] == "open" and c.get("filled_at"))
        equity = capital_start + realized
        return {"start": capital_start, "realized": round(realized),
                "unrealized": round(unrealized), "equity": round(equity),
                "margin_blocked": round(blocked), "free_margin": round(equity - blocked),
                "return_pct": round(100 * realized / capital_start, 2)}

    def stats(self) -> dict:
        calls = [c for c in self.load() if c["side"] and c["status"] not in self.BLOCKED]
        closed = [c for c in calls if c["status"] in ("stopped", "target_hit", "expired", "reversed", "closed")]
        wins = [c for c in closed if (c["pnl_inr"] or 0) > 0]
        awaiting = sum(1 for c in calls if c["status"] == "open" and not c.get("filled_at"))
        return {"total_calls": len(calls),
                "open": sum(1 for c in calls if c["status"] == "open"),
                "awaiting_fill": awaiting,
                "unfilled": sum(1 for c in calls if c["status"] in ("unfilled", "missed_no_fill")),
                "closed": len(closed), "wins": len(wins),
                "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
                "total_pnl_inr": round(sum(c["pnl_inr"] or 0 for c in closed), 0),
                "unrealized_inr": round(sum(c.get("unrealized_inr") or 0 for c in calls
                                            if c["status"] == "open" and c.get("filled_at")), 0)}


if __name__ == "__main__":
    import json as _json
    import tempfile

    def _backdate(led, ts="2026-08-01 10:00:00"):
        calls = led.load()
        for c in calls:
            c["opened_ts"] = ts
        led._write(calls)

    with tempfile.TemporaryDirectory() as d:
        result = {"cio": {"final_decision": "MODERATE LONG"}, "analysis_date": "2026-08-01",
                  "symbol": "CRUDEOIL", "data": {"mcx_fair_value_inr": 8000.0},
                  "trader": {"entry": "7,990-8,010", "stop": "7,900", "target": "8,200 / 8,400",
                             "confidence": "MEDIUM", "position_pct": "5-10%"}}
        spec = {"typical_margin_pct": 15.0, "lot_size_bbl": 100}

        led = Ledger(d, execution="limit")
        call = led.record(result, 1_000_000, spec)
        assert call["side"] == "LONG" and call["entry"] == 8000.0 and call["target"] == 8200.0 and call["lots"] == 1
        _backdate(led)
        idx = pd.date_range("2026-08-02", periods=5, freq="D")
        df = pd.DataFrame({"open": 8000, "high": [8050, 8100, 8250, 8300, 8300],
                           "low": [7950, 7980, 8050, 8100, 8150],
                           "close": [8020, 8080, 8210, 8280, 8290]}, index=idx)
        marked = led.mark(df)  # fills day 1 (7950<=8000<=8050), target day 3
        assert marked[0]["filled_at"] and marked[0]["status"] == "target_hit" \
            and marked[0]["pnl_inr"] == 20000.0, marked[0]
        # stop-before-target on same bar (fills and stops same bar, conservative)
        led2 = Ledger(d + "/2", execution="limit")
        led2.record(result, 1_000_000, spec)
        _backdate(led2)
        df2 = pd.DataFrame({"open": 8000, "high": [8250], "low": [7890], "close": [8000]},
                           index=pd.date_range("2026-08-02", periods=1))
        m2 = led2.mark(df2)
        assert m2[0]["status"] == "stopped" and m2[0]["pnl_inr"] == -10000.0, m2[0]
        # honest fill: price never reaches the entry zone -> awaiting fill, no P&L
        led3 = Ledger(d + "/3", execution="limit")
        led3.record(result, 1_000_000, spec)
        _backdate(led3)
        df3 = pd.DataFrame({"open": 8100, "high": [8150, 8180], "low": [8050, 8100],
                            "close": [8140, 8160]}, index=pd.date_range("2026-08-02", periods=2))
        m3 = led3.mark(df3)
        assert m3[0]["status"] == "open" and m3[0]["filled_at"] is None and m3[0]["pnl_inr"] is None, m3[0]
        assert led3.stats()["awaiting_fill"] == 1
        # missed: target crossed before entry ever fills -> setup gone, call cancelled
        led7 = Ledger(d + "/7", execution="limit")
        led7.record(result, 1_000_000, spec)
        _backdate(led7)
        df7 = pd.DataFrame({"open": 8100, "high": [8150, 8250], "low": [8050, 8120],
                            "close": [8140, 8230]}, index=pd.date_range("2026-08-02", periods=2))
        m7 = led7.mark(df7)
        assert m7[0]["status"] == "missed_no_fill" and m7[0]["pnl_inr"] == 0.0 and not m7[0]["filled_at"], m7[0]
        # gap-through fill: bar trades entirely BELOW the limit entry -> must still fill
        led5 = Ledger(d + "/5", execution="limit")
        led5.record(result, 1_000_000, spec)  # entry 8000, ref 8000
        _backdate(led5)
        df5 = pd.DataFrame({"open": 7980, "high": [7990, 8250], "low": [7950, 8050],
                            "close": [7960, 8210]}, index=pd.date_range("2026-08-02", periods=2))
        m5 = led5.mark(df5)  # bar1 trades entirely below entry (7950-7990): gap-through fill, stop untouched
        assert m5[0]["filled_at"] and m5[0]["status"] == "target_hit" and m5[0]["pnl_inr"] == 20000.0, m5[0]
        # breakout entry ABOVE issuance price: must NOT fill on a dip, only on the rally through it
        res6 = dict(result, trader=dict(result["trader"], entry="8,150", target="8,400", stop="8,000"),
                    data={"mcx_fair_value_inr": 8000.0})
        led6 = Ledger(d + "/6", execution="limit")
        led6.record(res6, 1_000_000, spec)  # entry 8150 > ref 8000
        _backdate(led6)
        df6 = pd.DataFrame({"open": 8000, "high": [8100, 8100], "low": [7900, 7950],
                            "close": [8050, 8050]}, index=pd.date_range("2026-08-02", periods=2))
        assert led6.mark(df6)[0]["filled_at"] is None  # dips don't trigger a buy-stop
        df6b = pd.DataFrame({"open": 8100, "high": [8200], "low": [8050], "close": [8180]},
                            index=pd.date_range("2026-08-04", periods=1))
        assert led6.mark(df6b)[0]["filled_at"] is not None  # rally through 8150 fills it
        # unfilled expiry: never touched for > MAX_HOLD_DAYS
        df4 = pd.DataFrame({"open": 8100, "high": [8150] * 25, "low": [8050] * 25,
                            "close": [8120] * 25}, index=pd.date_range("2026-08-02", periods=25))
        m4 = led3.mark(df4)
        assert m4[0]["status"] == "unfilled" and m4[0]["pnl_inr"] == 0.0, m4[0]
        s = led.stats()
        assert s["win_rate"] == 100.0 and s["total_pnl_inr"] == 20000.0

        # market execution policy: filled at issuance price immediately, target books post-cost
        led8 = Ledger(d + "/8", cost_inr_per_lot=180, execution="market")
        led8.record(result, 1_000_000, spec)
        assert led8.load()[0]["filled_at"] and led8.load()[0]["entry"] == 8000.0
        _backdate(led8)
        df8 = pd.DataFrame({"open": 8000, "high": [8250], "low": [7950], "close": [8210]},
                           index=pd.date_range("2026-08-02", periods=1))
        m8 = led8.mark(df8)
        assert m8[0]["status"] == "target_hit" and m8[0]["pnl_inr"] == 19820.0, m8[0]

        # position-aware: same-direction second call -> reaffirmed; opposite -> reversal
        led9 = Ledger(d + "/9", cost_inr_per_lot=180, execution="market")
        led9.record(result, 1_000_000, spec)                      # LONG open @8000
        c2 = led9.record(result, 1_000_000, spec)                 # LONG again -> reaffirm
        assert c2["status"] == "reaffirmed" and c2["lots"] == 0, c2
        res_short = dict(result, cio={"final_decision": "CAUTIOUS SHORT"})
        c3 = led9.record(res_short, 1_000_000, spec)              # SHORT -> reverses the long
        state = {c["status"] for c in led9.load()}
        assert "reversed" in state and c3["status"] == "open" and c3["side"] == "SHORT", state
        rev = [c for c in led9.load() if c["status"] == "reversed"][0]
        assert rev["pnl_inr"] == -180.0, rev  # closed at same ref -> only costs
    print("ledger self-check OK (fill logic, stop-first, awaiting-fill, unfilled-expiry)")

    # ---------------------------------------------------------------- P0 risk layer
    risk = {"stop_cap_pct": 3.0, "risk_per_trade_pct": 1.0, "max_lots": 4, "min_reward_risk": 1.2,
            "reversal_min_confidence": "MEDIUM", "reversal_min_margin": 2, "reversal_consecutive_runs": 2,
            "reversal_cooldown_hours": 0, "max_new_positions_per_day": 3, "daily_loss_limit_pct": 1.0,
            "real_account_inr": 500000, "broker_margin_per_lot_inr": 27374}
    spec_m = {"lot_size_bbl": 10, "typical_margin_pct": 30}

    def res(dec, direction, conf, bull, bear, stop, target):
        return {"cio": {"final_decision": dec}, "analysis_date": "2026-09-09", "symbol": "CRUDEOILM",
                "data": {"mcx_fair_value_inr": 8000.0},
                "trader": {"entry": "8000", "stop": stop, "target": target, "confidence": conf,
                           "position_pct": "10-15%", "direction": direction,
                           "module_tally": {"BULLISH": bull, "BEARISH": bear, "NEUTRAL": 6 - bull - bear}}}

    with tempfile.TemporaryDirectory() as d:
        led = Ledger(d, cost_inr_per_lot=80, risk=risk)
        c = led.record(res("CAUTIOUS LONG", "BULLISH", "LOW", 3, 2, "7,200", "8,600"), 1_000_000, spec_m)
        assert c["status"] == "open" and c["stop"] == 7760.0 and c["stop_source"] == "cap-3%", c  # 10% stop clamped to 3%
        assert c["lots"] == 4 and c["risk_inr"] == 9600 and c["risk_budget_inr"] == 10000, c   # 1% budget / (240 x 10) = 4
        assert c["real_plan"]["lots"] == 2 and c["real_plan"]["margin_inr"] == 54748, c["real_plan"]  # ₹5L real account
        c2 = led.record(res("CAUTIOUS LONG", "BULLISH", "LOW", 3, 2, "7,200", "8,600"), 1_000_000, spec_m)
        assert c2["status"] == "reaffirmed"
        c3 = led.record(res("CAUTIOUS SHORT", "BEARISH", "LOW", 2, 3, "8,300", "7,500"), 1_000_000, spec_m)
        assert c3["status"] == "held" and led.stats()["open"] == 1, c3          # LOW opposite: ignored
        c4 = led.record(res("CAUTIOUS SHORT", "BEARISH", "MEDIUM", 2, 3, "8,300", "7,500"), 1_000_000, spec_m)
        assert c4["status"] == "flattened" and led.stats()["open"] == 0, c4     # MEDIUM but margin 1: flatten only
        old = next(x for x in led.load() if x.get("trade_id") == c["trade_id"])
        assert old["status"] == "closed" and old["close_reason"].startswith("flatten"), old
        c5 = led.record(res("CAUTIOUS LONG", "BULLISH", "MEDIUM", 4, 2, "7,700", "8,600"), 1_000_000, spec_m)
        assert c5["status"] == "open", c5
        c6 = led.record(res("CAUTIOUS SHORT", "BEARISH", "MEDIUM", 1, 4, "8,300", "7,500"), 1_000_000, spec_m)
        assert c6["status"] == "flattened", c6                                   # first opposite run: not consecutive yet
        c7 = led.record(res("CAUTIOUS SHORT", "BEARISH", "MEDIUM", 1, 4, "8,300", "7,500"), 1_000_000, spec_m)
        assert c7["status"] == "open" and c7["side"] == "SHORT", c7              # second consecutive run: trades
        c8 = led.record(res("CAUTIOUS LONG", "BULLISH", "MEDIUM", 4, 2, "7,700", "8,100"), 1_000_000, spec_m)
        assert c8["status"] == "rejected_rr", c8                                 # 100 reward vs 240 risk
        calls = led.load()
        next(x for x in calls if x["status"] == "open")["unrealized_inr"] = -20000
        led._write(calls)
        c9 = led.record(res("CAUTIOUS LONG", "BULLISH", "HIGH", 5, 1, "7,700", "8,600"), 1_000_000, spec_m)
        assert c9["status"] == "halted" and "daily loss" in c9["blocked_by"] and led.stats()["open"] == 0, c9
        led.kill_file.parent.mkdir(exist_ok=True); led.kill_file.write_text("stop")
        c10 = led.record(res("CAUTIOUS LONG", "BULLISH", "HIGH", 5, 1, "7,700", "8,600"), 1_000_000, spec_m)
        assert c10["status"] == "halted" and c10["blocked_by"] == "kill switch", c10
        led.kill_file.unlink()  # the temp dir's PARENT is the system temp folder: a leftover KILL would halt every later test run
        # expiry settlement: front rolled Sep -> Oct; Sep position settles at Sep's last print, not Oct's price
        calls = led.load()
        for x in calls:
            if x["status"] == "open":
                x["contract_expiry"] = "2026-09-21"
        led._write(calls)
        n_open = sum(1 for x in led.load() if x["status"] == "open")
        st_ = led.settle_expired("2026-10-19", lambda exp: 9676.0 if exp == "2026-09-21" else None)
        assert len(st_) == n_open and all(x["close_reason"].startswith("expiry-settled") and x["close_price"] == 9676.0 for x in st_), st_
        assert led.settle_expired("2026-10-19", lambda exp: 9676.0) == []   # idempotent
        # entry at the contract's own print (+ its expiry month), and the expiry-roll gate
    with tempfile.TemporaryDirectory() as root:
        live = Path(root) / "cache" / "live"; live.mkdir(parents=True)
        led = Ledger(str(Path(root) / "ledger"), execution="market")
        base = {"cio": {"final_decision": "MODERATE LONG"}, "analysis_date": str(dt.date.today()), "symbol": "CRUDEOIL",
                "data": {"mcx_fair_value_inr": 9065.1},
                "trader": {"entry": "9,431", "stop": "9,300", "target": "9,800", "confidence": "MEDIUM", "position_pct": "5-10%"}}
        spec = {"typical_margin_pct": 15.0, "lot_size_bbl": 10}
        _now = dt.datetime.now()
        (live / "latest_CRUDEOIL.json").write_text(json.dumps({"ts": _now.strftime("%Y-%m-%d %H:%M:%S"), "ltp": 9431.0,
                                                                "expiry": (_now.date() + dt.timedelta(days=20)).isoformat()}))
        c1 = led.record(base, 1_000_000, spec)
        assert c1["entry"] == 9431.0 and c1["ref_source"].startswith("contract print") and c1["contract_expiry"], c1
        led2 = Ledger(str(Path(root) / "ledger2"), execution="market")
        (Path(root) / "cache" / "live" / "latest_CRUDEOIL.json").write_text(json.dumps(
            {"ts": _now.strftime("%Y-%m-%d %H:%M:%S"), "ltp": 9160.0, "expiry": (_now.date() - dt.timedelta(days=1)).isoformat()}))
        c2 = led2.record(base, 1_000_000, spec)
        assert c2["status"] == "blocked_roll" and c2["lots"] == 0 and c2["trade_id"] is None, c2
        led3 = Ledger(str(Path(root) / "ledger3"), execution="market")
        (Path(root) / "cache" / "live" / "latest_CRUDEOIL.json").unlink()
        c3 = led3.record(base, 1_000_000, spec)
        assert c3["entry"] == 9065.1 and c3["ref_source"].startswith("parity") and c3["contract_expiry"] is None, c3
    print("entry-at-contract-print + expiry-roll gate self-tests passed")
    print("P0 risk layer self-tests passed: stop cap, risk sizing, real plan, hysteresis (held/flattened/reversed), R:R floor, daily loss halt, kill switch")
