"""Intraday operation, with legacy strategy profiles retained to read older records."""
from __future__ import annotations

import copy
import math
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
NAMES = ("intraday", "long_term")
ACTIVE_NAMES = ("intraday",)
DEFAULTS = {
    "intraday": {
        "enabled": True, "interval_hours": 0.5,
        "entry_cutoff": "23:00", "square_off": "23:15", "session_open": "09:00",
        "session_close": "23:30", "max_quote_age_seconds": 120,
        "max_hold_days": 0, "enforce_risk_gate": True,
        "modules": ["technical", "news", "basis"], "debate_rounds": 1,
        "min_reward_risk": 1.5,
    },
    "long_term": {
        "enabled": False, "interval_hours": 2.0,
        "entry_cutoff": "23:15", "session_open": "09:00", "session_close": "23:30",
        "max_quote_age_seconds": 120, "max_hold_days": 20, "enforce_risk_gate": True,
        "min_reward_risk": 1.5,
    },
}


def now_ist() -> dt.datetime:
    """Local-naive IST matches the stored tick timestamps on every host timezone."""
    return dt.datetime.now(IST).replace(tzinfo=None)


def profiles(cfg: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for name in NAMES:
        supplied = (cfg.get("strategies") or {}).get(name, {})
        out[name].update(supplied)
        if name == 'long_term':
            out[name]['enabled'] = False  # historical records only; never new long-term work
        p = out[name]
        if not math.isfinite(float(p["min_reward_risk"])) or float(p["min_reward_risk"]) <= 0:
            raise ValueError(f"{name}: min_reward_risk must be finite and positive")
        if not math.isfinite(float(p["interval_hours"])) or float(p["interval_hours"]) < 0 or (0 < float(p["interval_hours"]) < 1 / 60):
            raise ValueError(f"{name}: interval_hours must be zero or at least one minute")
        times = [dt.time.fromisoformat(p[k]) for k in ("session_open", "entry_cutoff", "session_close")]
        if not times[0] < times[1] <= times[2]:
            raise ValueError(f"{name}: session_open < entry_cutoff <= session_close required")
        if name == "intraday" and not times[1] <= dt.time.fromisoformat(p["square_off"]) < times[2]:
            raise ValueError("intraday: entry_cutoff <= square_off < session_close required")
        if name == "long_term" and int(p["max_hold_days"]) < 1:
            raise ValueError("long_term: max_hold_days must be positive")
    return out


def enabled_strategies(cfg: dict) -> list[str]:
    return [name for name in ACTIVE_NAMES if profiles(cfg)[name]['enabled']]


def strategy_config(cfg: dict, name: str) -> dict:
    if name not in NAMES:
        raise ValueError(f"Unknown strategy {name!r}; use intraday or long_term")
    out = copy.deepcopy(cfg)
    profile = profiles(cfg)[name]
    base_reports = Path(cfg["paths"].get("base_reports_dir", cfg["paths"]["reports_dir"]))
    root = Path(cfg["paths"].get("project_root", base_reports.parent))
    out["strategy"] = {"id": name, **profile}
    for key in ("modules", "debate_rounds", "enforce_risk_gate"):
        if key in profile:
            out["analysis"][key] = profile[key]
    out["paths"].update(project_root=str(root), base_reports_dir=str(base_reports),
        reports_dir=str(base_reports / name), signals_dir=str(root / "signals"))
    return out


def entry_allowed(profile: dict, now: dt.datetime) -> bool:
    if now.tzinfo:
        now = now.astimezone(IST).replace(tzinfo=None)
    return (now.weekday() < 5 and now.date().isoformat() not in profile.get("holidays", [])
            and dt.time.fromisoformat(profile["session_open"]) <= now.time()
            < dt.time.fromisoformat(profile["entry_cutoff"]))


def intraday_data(cfg: dict, symbol: str, now: dt.datetime | None = None) -> tuple[pd.DataFrame, str]:
    """Completed five-minute candles from observed MCX ticks of the current contract."""
    now = now or now_ist()
    frames = []
    for path in sorted((Path(cfg["paths"]["cache_dir"]) / "live").glob(f"{symbol}_2*.csv"))[-5:]:
        try:
            frame = pd.read_csv(path, parse_dates=["ts"])
            if {'ts', 'ltp', 'vol_cum'} <= set(frame): frames.append(frame)
        except (ValueError, OSError, pd.errors.EmptyDataError):
            continue
    if not frames:
        return pd.DataFrame(), "Intraday candles unavailable; stand aside until the collector warms up."
    ticks = pd.concat(frames).sort_values("ts").drop_duplicates("ts")
    ticks = ticks[ticks["ts"] <= now]
    if ticks.empty:
        return pd.DataFrame(), "No current intraday observations."
    if "expiry" in ticks:
        ticks = ticks[ticks["expiry"] == ticks["expiry"].iloc[-1]]
    ticks = ticks.set_index("ts")
    ticks["volume"] = ticks.groupby(ticks.index.date)["vol_cum"].diff().clip(lower=0).fillna(0)
    bars = ticks["ltp"].resample("5min").ohlc()
    bars["volume"] = ticks["volume"].resample("5min").sum()
    bars = bars.dropna()
    bars = bars[bars.index + pd.Timedelta(minutes=5) <= now]
    broker_count = 0
    if 'expiry' in ticks:
        bars.attrs['contract_expiry'] = str(ticks['expiry'].iloc[-1])[:10]
        from .kite_history import historical_bars
        broker = historical_bars(cfg['paths']['cache_dir'], symbol, bars.attrs['contract_expiry'], now)
        if not broker.empty:
            expiry = bars.attrs['contract_expiry']
            # Prefer complete broker OHLCV for overlapping intervals. Sampled
            # ticks still supply the newest intervals and remain on disk.
            bars = pd.concat([bars, broker]).sort_index(kind='stable')
            bars = bars[~bars.index.duplicated(keep='last')]
            bars = bars[bars.index.date >= sorted(set(bars.index.date))[-5]] if len(set(bars.index.date)) > 5 else bars
            bars.attrs['contract_expiry'] = expiry
            broker_count = len(bars.index.intersection(broker.index))
            bars.attrs['broker_candles'] = broker_count
    session = ticks[ticks.index.date == now.date()]
    ready = len(bars) >= 80 and len(session) >= 3
    sessions = len(set(bars.index.date))
    today_bars = int(sum(bars.index.date == now.date()))
    details = (f"{len(bars)} completed MCX 5-minute bars across {sessions} sessions "
               f"({bars.index.min()} to {bars.index.max()}, candle-open timestamps in IST); "
               f"{today_bars} completed bars and {len(session)} observed ticks today. "
               "The total is a multi-session indicator lookback, not today's candle count. "
               f"{broker_count} candles use Kite historical OHLCV; other intervals use sampled observations.")
    if len(session):
        vol = session["volume"].sum()
        vwap = (session["ltp"] * session["volume"]).sum() / vol if vol > 0 else session["ltp"].mean()
        details += (f" Session VWAP Rs {vwap:.1f}; observed range Rs {session.ltp.min():.1f}–{session.ltp.max():.1f}; "
                    f"last Rs {session.ltp.iloc[-1]:.1f} at {session.index[-1]}. "
                    "Sampled intervals can miss extremes between polls; broker OHLCV takes precedence where available.")
    if not ready:
        details += " INSUFFICIENT INTRADAY HISTORY: analysis only; no intraday entry permitted."
    return bars if ready else pd.DataFrame(), details


def intraday_evidence(bars: pd.DataFrame, snapshot: dict, note: str, as_of: dt.datetime) -> dict:
    """Carry the observed sequence and indicators through every decision stage."""
    recent = bars.tail(24).round(3).reset_index(names='bar_open_ist') if len(bars) else pd.DataFrame()
    records = [{**row, 'bar_open_ist':str(row['bar_open_ist'])} for row in recent.to_dict('records')]
    indicators = dict(snapshot) if len(bars) else {}
    for old,new in [('change_5d_pct','change_5bar_pct'),('change_20d_pct','change_20bar_pct')]:
        if old in indicators:
            indicators[new] = indicators.pop(old)
    import json
    text = (f'[SHARED INTRADAY EVIDENCE — collected {as_of:%Y-%m-%d %H:%M:%S} IST]\n{note}\n'
            'INDICATORS FROM COMPLETED MCX 5-MINUTE BARS (moving-average periods count bars):\n'
            + json.dumps(indicators,ensure_ascii=False) + '\n'
            + 'RECENT COMPLETED MCX 5-MINUTE OHLCV:\n'
            + (recent.to_string(index=False) if len(recent) else 'Unavailable; no intraday signal may be issued.') + '\n'
            'Each timestamp is the opening of a completed five-minute interval. The latest live trade can differ from its close. '
            'Sampled bar/session extremes and volume-weighted prices need not equal the exchange-wide session range or official daily VWAP. '
            'Do not confuse multi-session indicator history with the number of bars collected today. '
            'These are observed price signals; bid/ask depth and broker fills are not supplied. Do not invent them or treat their absence alone as missing candle evidence.')
    source = 'Kite historical OHLCV with sampled tick intervals' if bars.attrs.get('broker_candles') else 'sampled MCX ticks'
    return dict(as_of=as_of.isoformat(timespec='seconds'),source=source,
                contract_expiry=bars.attrs.get('contract_expiry'),
                session_context=note,indicators=indicators,recent_bars=records,text=text)


def strategy_instruction(cfg: dict) -> str:
    p = cfg["strategy"]
    if p["id"] == "intraday":
        return (f"[MANDATORY DESK HORIZON: INTRADAY]\nSame-session trades only. No new entries from {p['entry_cutoff']} IST; "
                f"mandatory exit at {p['square_off']} IST. Evaluate minutes to hours remaining TODAY. "
                "Use MCX 5-minute bars, session VWAP, momentum, near-term news catalysts and fresh observed contract prices. "
                "Evaluate price-based signals, not broker order execution. Account access, order-book depth and broker fills are not prerequisites for a signal. "
                "Reward/risk uses observed price distances before fees and slippage; discuss execution uncertainty separately without inventing costs. "
                "Daily/weekly inventories, positioning and curves are background context, never a multi-day target. "
                f"Minimum reward/risk is {p['min_reward_risk']}:1. Do not recommend overnight holds or calendar spreads. Targets must be plausible before square-off; "
                "stand aside when data is insufficient, the move is exhausted, or risk-to-reward is below the configured floor.")
    return (f"[MANDATORY DESK HORIZON: LONG TERM]\nMulti-session positions, typically days to weeks; "
            f"maximum {p['max_hold_days']} calendar days and no carry beyond contract expiry. "
            "Use daily trend, inventories, positioning, curve and macro catalysts. Overnight holding is allowed. "
            f"Minimum reward/risk is {p['min_reward_risk']}:1. Do not flip a multi-day thesis on a single intraday signal. Use outright long/short or stand aside; "
            "re-entry after expiry requires a fresh decision on the next contract.")
