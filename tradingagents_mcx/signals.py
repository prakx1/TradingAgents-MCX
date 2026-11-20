"""Durable entry/stop/target signals. No accounts, capital, lots or broker orders.

Each horizon has one active signal. Outcomes are observed price points and R,
with the initial stop distance frozen as the denominator. These are signal
outcomes, not executable fills or account returns.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from .strategies import entry_allowed, now_ist


def price_level(value) -> float | None:
    numbers = [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", str(value or "").replace(",", ""))]
    numbers = [x for x in numbers if math.isfinite(x) and abs(x) >= 100]
    return numbers[0] if numbers and numbers[0] > 0 else None


def latest_quote(cfg: dict, now: dt.datetime) -> tuple[dict | None, str]:
    symbol = cfg["analysis"].get("symbol", "CRUDEOIL")
    try:
        row = json.loads((Path(cfg["paths"]["cache_dir"]) / "live" / f"latest_{symbol}.json").read_text())
        age = (now - pd.Timestamp(row["ts"]).to_pydatetime()).total_seconds()
        limit = float(cfg["strategy"].get("max_quote_age_seconds", 120))
        if not 0 <= age <= limit:
            return None, "MCX quote is stale or has a future timestamp"
        if row.get("ltt"):
            ts = pd.Timestamp(row["ltt"])
            if ts.tzinfo:
                ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
            if not 0 <= (now - ts.to_pydatetime()).total_seconds() <= limit:
                return None, "Exchange last-trade time is stale"
        if not math.isfinite(float(row["ltp"])) or float(row["ltp"]) <= 0:
            return None, "Invalid contract price"
        expiry = dt.date.fromisoformat(str(row["expiry"])[:10])
        if expiry < now.date():
            return None, "The quoted contract has expired"
        return row, ""
    except (OSError, ValueError, KeyError, TypeError):
        return None, "No usable live MCX contract quote"


class SignalBook:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.policy = cfg["strategy"]
        self.name = self.policy["id"]
        self.path = Path(cfg["paths"]["signals_dir"]) / f"{self.name}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        with open(self.path.with_suffix(".lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def _save(self, rows: list[dict]):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in rows))
        tmp.replace(self.path)

    @staticmethod
    def _close(signal: dict, price: float, at: dt.datetime, reason: str):
        sign = 1 if signal["side"] == "LONG" else -1
        points = (price - signal["entry"]) * sign
        signal.update(status="closed", closed_at=at.isoformat(sep=" ", timespec="seconds"),
                      exit=round(price, 2), close_reason=reason,
                      points=round(points, 2), outcome_r=round(points / signal["risk_points"], 3))
        signal.pop("exit_pending", None)

    def issue(self, result: dict, now: dt.datetime | None = None) -> dict:
        now = now or now_ist()
        verdict = result["cio"]["final_decision"]
        side = "LONG" if "LONG" in verdict else "SHORT" if "SHORT" in verdict else None
        def blocked(reason, status="blocked"):
            return {"status": status, "reason": reason, "strategy": self.name}
        if self.name != 'intraday':
            return blocked('Long-term mode is archived. Only intraday signals can be issued.')
        if side and result["trader"].get("direction") != ("BULLISH" if side == "LONG" else "BEARISH"):
            return blocked("CIO direction conflicts with the analyst/trader direction")
        if side is None:
            from .history import decision_reason
            return blocked(decision_reason(result), "stand_aside")
        if not self.policy.get("enabled", True):
            return blocked("Strategy disabled")
        if not entry_allowed(self.policy, now):
            return blocked(f"Entry window closed; cutoff {self.policy['entry_cutoff']} IST")
        if result.get("entry_eligible") is False:
            return blocked(result.get("entry_block_reason", "Insufficient strategy data"))
        if price_level(result['trader'].get('entry')) is None:
            return blocked('Trader selected WAIT or supplied no confirmed numeric entry; conditional setups are not active signals')
        quote, error = latest_quote(self.cfg, now)
        if quote is None:
            return blocked(error)
        entry = float(quote["ltp"])
        if result.get('setup_id'):
            bounds = result.get('setup_entry_range')
            if not bounds or not bounds[0] <= entry <= bounds[1]:
                return blocked('Latest quote moved outside the approved setup entry range')
            if result.get('contract_expiry') != str(quote['expiry'])[:10]:
                return blocked('Latest quote changed contract after setup confirmation')
            confirmed = result.get('setup_confirmed_at')
            if not confirmed:
                return blocked('Conditional setup lacks a confirmation timestamp')
            for stamp in (quote['ts'],quote.get('ltt') or quote['ts']):
                observed = pd.Timestamp(stamp)
                if observed.tzinfo:
                    observed = observed.tz_convert('Asia/Kolkata').tz_localize(None)
                if observed < pd.Timestamp(confirmed):
                    return blocked('Waiting for a fresh trade after candle confirmation')
        stop, target = (price_level(result["trader"].get(k)) for k in ("stop", "target"))
        if stop is None or target is None or not (stop < entry < target if side == "LONG" else target < entry < stop):
            return blocked("Stop and target must bracket the current contract price in the signal direction")
        risk, reward = abs(entry - stop), abs(target - entry)
        ratio = reward / risk
        if ratio < float(self.policy["min_reward_risk"]):
            return blocked(f"Reward/risk {ratio:.2f}:1 is below {self.policy['min_reward_risk']}:1")
        constraints = result['trader'].get('entry_constraints')
        if result['trader'].get('entry_method','').startswith('regime_rules_'):
            from .economics import configured_fee_points, plan_economics
            from .regimes import RULES
            try:
                if (not constraints or constraints['rule']!=RULES.version or constraints['side']!=side
                        or constraints['contract_expiry']!=str(quote['expiry'])[:10]
                        or stop!=constraints['stop'] or target!=constraints['target']):
                    return blocked('Regime proposal identity or fixed levels changed; a new review is required')
                if now>=pd.Timestamp(constraints['valid_until']):
                    return blocked('Regime confirmation expired during council review')
                for stamp in (quote['ts'],quote.get('ltt') or quote['ts']):
                    observed=pd.Timestamp(stamp)
                    if observed.tzinfo:observed=observed.tz_convert('Asia/Kolkata').tz_localize(None)
                    if observed<pd.Timestamp(constraints['confirmed_at']):
                        return blocked('Waiting for a fresh trade after regime confirmation')
                fees=max(configured_fee_points(self.cfg),float(constraints['fee_points']))
                costs=fees+2*float(constraints['slippage_points_each_way'])
                net=plan_economics(entry,stop,target,side,costs)
                floor=max(RULES.minimum_net_reward_risk,float(constraints['minimum_net_reward_risk']))
                if (reward-costs)/(risk+costs)<floor:
                    return blocked(f'Regime entry fails {floor:g}:1 reward/risk after fees and slippage at the current price')
            except (KeyError,TypeError,ValueError):
                return blocked('Incomplete regime execution constraints; a new review is required')
        expiry = str(quote["expiry"])[:10]
        expiry_close = dt.datetime.combine(dt.date.fromisoformat(expiry), dt.time.fromisoformat(self.policy["session_close"]))
        if self.name == "intraday":
            until = dt.datetime.combine(now.date(), dt.time.fromisoformat(self.policy["square_off"]))
        else:
            until = min(now + dt.timedelta(days=int(self.policy["max_hold_days"])), expiry_close)
        signal = {"id": f"{self.name}-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
                  "strategy": self.name, "symbol": result["symbol"], "contract_expiry": expiry,
                  "issued_at": now.isoformat(sep=" ", timespec="seconds"),
                  "valid_until": until.isoformat(sep=" ", timespec="seconds"),
                  "side": side, "entry": entry, "stop": stop, "target": target,
                  "suggested_entry": result["trader"].get("entry"),
                  "entry_method": result['trader'].get('entry_method','council_levels'),
                  "risk_points": round(risk, 4), "reward_points": round(reward, 4), "reward_risk": round(ratio, 3),
                  "confidence": result["trader"].get("confidence", "LOW"),
                  "status": "active", "report_path": result.get("report_path"),
                  "quote_ts": quote["ts"], "last_price": entry, "current_r": 0.0}
        if result.get('setup_id'):
            signal['setup_id'] = result['setup_id']
        if constraints and result['trader'].get('entry_method','').startswith('regime_rules_'):
            signal['entry_constraints']=constraints
            signal['regime_entry_economics']=net
        with self.locked():
            rows = self.load()
            if result.get('setup_id'):
                prior = next((r for r in rows if r.get('setup_id') == result['setup_id']), None)
                if prior:
                    return prior  # a restarted monitor must never issue the same plan twice
            active = [r for r in rows if r["status"] == "active"]
            if active:
                old = active[-1]
                if old['contract_expiry'] != expiry or old['symbol'] != result['symbol']:
                    return blocked('Existing signal belongs to a different contract; wait for its own exit price')
                if old.get("exit_pending") or pd.Timestamp(old["valid_until"]) <= now:
                    return blocked("Previous signal exit is pending fresh data; no replacement signal")
                if old["side"] == side:
                    return {"status": "reaffirmed", "signal_id": old["id"], "strategy": self.name,
                            "reason": "Existing signal retained with its original entry, stop and target"}
                self._close(old, entry, now, "opposite_signal")
            rows.append(signal)
            self._save(rows)
        return signal

    def mark(self, ticks: pd.DataFrame, now: dt.datetime | None = None) -> list[dict]:
        now = now or now_ist()
        with self.locked():
            rows = self.load()
            changed = False
            for s in rows:
                if s["status"] != "active":
                    continue
                issued, until = pd.Timestamp(s["issued_at"]), pd.Timestamp(s["valid_until"])
                window = ticks.copy()
                if len(window):
                    window = window[(window.ts > issued) & (window.ts <= now)].sort_values("ts")
                    # Never score an old contract against the new front month's price.
                    if "expiry" in window:
                        window = window[window.expiry.astype(str).str[:10] == s["contract_expiry"]]
                    else:
                        window = window.iloc[:0]
                for row in window.itertuples():
                    ts, px = pd.Timestamp(row.ts), float(row.ltp)
                    if not math.isfinite(px) or px <= 0:
                        continue
                    if ts >= until:
                        reason = "intraday_square_off" if self.name == "intraday" else "holding_period_end"
                        if (ts - until).total_seconds() > 120:
                            reason += "_delayed_data"
                        self._close(s, px, ts.to_pydatetime(), reason)
                        changed = True
                        break
                    stop_hit = px <= s["stop"] if s["side"] == "LONG" else px >= s["stop"]
                    target_hit = px >= s["target"] if s["side"] == "LONG" else px <= s["target"]
                    if stop_hit or target_hit:
                        # Observed price, including gaps; never invent a fill at a skipped stop.
                        self._close(s, px, ts.to_pydatetime(), "stop_hit" if stop_hit else "target_hit")
                        changed = True
                        break
                    sign = 1 if s["side"] == "LONG" else -1
                    s.update(last_price=px, last_observed_at=str(ts), current_r=round((px - s["entry"]) * sign / s["risk_points"], 3))
                    changed = True
                if s["status"] == "active" and pd.Timestamp(now) >= until:
                    # Expiry has no next old-month quote: explicitly mark at its last observed print.
                    expired_contract = now.date() > dt.date.fromisoformat(s["contract_expiry"])
                    if expired_contract and s.get("last_observed_at"):
                        self._close(s, s["last_price"], pd.Timestamp(s["last_observed_at"]).to_pydatetime(), "contract_expiry_last_observation")
                    else:
                        s["exit_pending"] = "Exit overdue: no observed contract price at/after the deadline"
                    changed = True
            if changed:
                self._save(rows)
            return rows

    def stats(self) -> dict:
        rows = self.load()
        closed = [r for r in rows if r["status"] == "closed"]
        wins = sum(r["outcome_r"] > 0 for r in closed)
        total_r = sum(r["outcome_r"] for r in closed)
        return {"signals": len(rows), "active": sum(r["status"] == "active" for r in rows),
                "closed": len(closed), "wins": wins, "win_rate": round(100 * wins / len(closed), 1) if closed else None,
                "total_r": round(total_r, 3), "mean_r": round(total_r / len(closed), 3) if closed else None,
                "points": round(sum(r["points"] for r in closed), 2),
                "exit_pending": sum(r["status"] == "active" and bool(r.get("exit_pending")) for r in rows)}

    def context(self) -> str:
        closed = [r for r in self.load() if r["status"] == "closed"][-10:]
        if not closed:
            return "No completed signals yet in this strategy. Do not infer a proven edge."
        lines = [f"{r['side']} {r['close_reason']}: {r['outcome_r']:+.2f}R" for r in closed]
        return "Recent outcomes for THIS horizon (observed price signals, small sample):\n" + "\n".join(lines)
