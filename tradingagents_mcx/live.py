"""Real-time engine: live quote collection, intraday signal computation, news pulse.

Data cadence:
  MCX quote     Kite stream, with Moneycontrol fallback when not fresh
  WTI 1-minute  Yahoo CL=F             memoized 60s
  USDINR        Yahoo USDINR=X         memoized 60s
  News pulse    Google News when:2h    memoized 180s, scored locally (lexicon+VADER)

The composite signal is a deterministic indicator (-100..+100) built from seven
sub-signals; it updates every refresh and is logged to cache/live/signals_*.jsonl
so you can audit how it behaved. It complements — never replaces — the AI
committee's standing decision.
"""
from __future__ import annotations

import datetime as dt
import json
import socket
import time
from pathlib import Path

socket.setdefaulttimeout(25)  # feedparser has no timeout of its own — one stuck RSS host must never hang a daemon

import numpy as np
import pandas as pd

from .moneycontrol import get_mcx_quotes

# ------------------------------------------------------------------ memo cache
_MEMO: dict = {}

def _memo(key: str, ttl: float, fn):
    now = time.time()
    hit = _MEMO.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        val = hit[1] if hit else None  # stale-on-error
    _MEMO[key] = (now, val)
    return val


def get_wti_intraday() -> pd.Series | None:
    def fetch():
        import yfinance as yf
        h = yf.Ticker("CL=F").history(period="1d", interval="1m")
        if len(h):
            return h["Close"].tz_localize(None)
        # US holiday / closed session: intraday is empty but WTI is genuinely
        # frozen at its last close — use daily closes so fair value stays defined
        d = yf.Ticker("CL=F").history(period="5d", interval="1d")
        return d["Close"].tz_localize(None) if len(d) else None
    return _memo("wti1m", 60, fetch)


def get_fx_live() -> float | None:
    def fetch():
        import yfinance as yf
        h = yf.Ticker("USDINR=X").history(period="1d", interval="1m")
        return float(h["Close"].iloc[-1]) if len(h) else None
    return _memo("fx1m", 60, fetch)


def llm_gateway_ok(base_url: str, ttl: float = 120) -> bool:
    """Is the LLM endpoint reachable? (VPN-gated gateways go dark when the VPN drops.)"""
    def probe():
        import requests as rq
        for path in ("/health/liveliness", ""):  # LiteLLM liveliness; any HTTP answer = reachable
            try:
                rq.get(base_url.rstrip("/") + path, timeout=4)
                return True
            except Exception:
                continue
        return False
    return bool(_memo("llm_gw_ok", ttl, probe))


def get_quote_cached(symbol: str, cache_dir: str, ttl: float = 15,
                     fail_ttl: float = 90) -> pd.DataFrame | None:
    from .kite_data import cached_quotes
    kite = cached_quotes(cache_dir, symbol)
    if kite is not None and not kite.empty:
        return kite
    key = f"mcq_{symbol}"
    hit = _MEMO.get(key)
    # feed down: back off — retry every fail_ttl, not on every 20s refresh
    if hit and (hit[1] is None or getattr(hit[1], "empty", False)) \
            and time.time() - hit[0] < fail_ttl:
        return None

    def fetch_with_deadline():
        # hard total cap: a drip-feeding server defeats per-read timeouts, so
        # abandon the fetch thread outright after 8s (it dies with its request)
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TO
        ex = ThreadPoolExecutor(1)
        try:
            return ex.submit(get_mcx_quotes, symbol, cache_dir, 2).result(timeout=8)
        except _TO:
            return None
        finally:
            ex.shutdown(wait=False)  # abandon the hung thread, don't join it
    return _memo(key, ttl, fetch_with_deadline)


# ------------------------------------------------------------------ tick store

def _tick_path(cache_dir: str, symbol: str) -> Path:
    p = Path(cache_dir) / "live"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{symbol}_{dt.date.today():%Y%m%d}.csv"


def append_tick(cache_dir: str, symbol: str = "CRUDEOIL") -> dict | None:
    q = get_quote_cached(symbol, cache_dir)
    if q is None or q.empty:
        return None
    front = q.iloc[0]
    # A recently polled response can still contain yesterday's last trade.
    # Never publish stale exchange prints as fresh observations for signal exits.
    from .strategies import now_ist
    observed_now = now_ist()
    if front.get("last_trade"):
        try:
            stamp = pd.Timestamp(front["last_trade"])
            if stamp.tzinfo:
                stamp = stamp.tz_convert("Asia/Kolkata").tz_localize(None)
            if not 0 <= (observed_now - stamp.to_pydatetime()).total_seconds() <= 120:
                return None
        except (TypeError, ValueError):
            return None
    wti = get_wti_intraday()
    fx = get_fx_live()
    wti_last = float(wti.iloc[-1]) if wti is not None and len(wti) else None
    fair = round(wti_last * fx, 1) if wti_last and fx else None
    row = {"ts": observed_now.strftime("%Y-%m-%d %H:%M:%S"),
           "ltp": float(front["close"]), "high": float(front["high"]), "low": float(front["low"]),
           "vol_cum": float(front["volume"]), "oi": float(front["open_interest"]),
           "vwap_feed": float(front.get("vwap") or 0) or None,
           "wti": wti_last, "usdinr": fx, "fair": fair,
           "premium": round(float(front["close"]) - fair, 1) if fair else None,
           "next_close": float(q.iloc[1]["close"]) if len(q) > 1 else None,
           "expiry": front.get("expiry"), "change_pct": float(front.get("change_pct") or 0)}
    path = _tick_path(cache_dir, symbol)
    pd.DataFrame([row]).to_csv(path, mode="a", header=not path.exists(), index=False)
    row["ltt"] = str(front.get("last_trade") or "")  # exchange last-trade time (returned, not stored in CSV)
    row['source'] = front.get('source') or q.attrs.get('source') or 'Moneycontrol'
    for name in ('exchange_timestamp', 'received_at', 'instrument_token', 'tradingsymbol'):
        if name in front: row[name] = front[name]
    # Preserve the legacy CSV schema; richer observations have an append-only companion.
    audit = Path(cache_dir) / 'live' / f'{symbol}_{observed_now:%Y%m%d}.observations.jsonl'
    with audit.open('a') as f: f.write(json.dumps(row, default=str) + '\n')
    try:  # latest.json: the UI reads this instead of polling the feed itself
        from .kite_data import atomic_json
        atomic_json(Path(cache_dir) / 'live' / f'latest_{symbol}.json', row)
    except Exception:
        pass
    return row


def load_ticks(cache_dir: str, symbol: str = "CRUDEOIL") -> pd.DataFrame:
    path = _tick_path(cache_dir, symbol)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, parse_dates=["ts"])
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


# ------------------------------------------------------------------ news pulse
# Context-aware direction rules for crude PRICES. Generic sentiment fails here:
# war risk is bullish for oil, "stocks rise" is bearish while "futures rise" is
# bullish. Rules are evaluated in order; matched words are consumed by context.
_GEO = ("war", "warfare", "hormuz", "attack", "strike", "missile", "drone", "escalat",
        "sanction", "conflict", "tension", "disrupt", "outage", "suspend", "seiz", "halt")
_SUPPLY_CUT = ("opec cut", "cuts output", "output cut", "extends cuts", "curb", "production cut")
_SUPPLY_ADD = ("boost output", "ramp up", "increase production", "raise output",
               "resume exports", "restore production", "glut", "oversupply", "surplus")
_UP = ("surge", "rally", "soar", "jump", "rise", "rises", "climb", "gain", "higher", "rebound")
_DOWN = ("slump", "crash", "tumble", "plunge", "drop", "sink", "fall", "falls", "slide", "lower")
_DEMAND_BAD = ("demand fears", "recession", "slowdown", "weak demand", "demand concerns")
_DEMAND_GOOD = ("demand growth", "strong demand", "record demand")
_PEACE = ("ceasefire", "peace deal", "deal reached", "de-escalat", "truce")


def crude_headline_score(title: str) -> float:
    """-1..+1 price-direction score for a crude headline."""
    t = title.lower()
    score = 0.0
    inventory_ctx = any(k in t for k in ("stock", "inventor", "stockpile", "spr"))
    if inventory_ctx:  # inventory context flips the sign of move-words
        if any(k in t for k in ("build",) + _UP):
            score -= 0.5
        if any(k in t for k in ("draw",) + _DOWN):
            score += 0.5
    else:
        if any(k in t for k in _UP):
            score += 0.3
        if any(k in t for k in _DOWN):
            score -= 0.3
    if any(k in t for k in _GEO):
        score += 0.5   # supply-risk premium
    if any(k in t for k in _PEACE):
        score -= 0.5
    if any(k in t for k in _SUPPLY_CUT):
        score += 0.4
    if any(k in t for k in _SUPPLY_ADD):
        score -= 0.4
    if any(k in t for k in _DEMAND_BAD):
        score -= 0.4
    if any(k in t for k in _DEMAND_GOOD):
        score += 0.4
    if score == 0.0:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            vader = _MEMO.setdefault("_vader", (0, SentimentIntensityAnalyzer()))[1]
            score = float(vader.polarity_scores(title)["compound"]) * 0.3
        except Exception:
            score = 0.0
    return float(np.clip(score, -1, 1))


def news_snapshot(max_age_min: float = 20) -> dict:
    """The sentinel is the single fetcher (50 feeds, ~25s/pass). UI and collector read its snapshot;
    fall back to a live fetch only if the sentinel has been silent for max_age_min."""
    p = Path(__file__).resolve().parent.parent / "events" / "news_latest.json"
    try:
        d = json.loads(p.read_text())
        age = (dt.datetime.now() - dt.datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
        if age <= max_age_min:
            d.setdefault("status", {}); d.setdefault("n_sources_live", d.get("n_sources_live", 0))
            return d
    except Exception:
        pass
    return news_pulse()


def news_pulse(ttl: float = 60, window_h: float = 2.0) -> dict:
    """Recent crude headlines from EVERY registered source (tradingagents_mcx.newsfeeds),
    deduped, locally scored, with the source that saw each one first.
    {score: -1..1, items: [{title, score, source, lane, trust, first_source, published}], status: {...}}"""
    def fetch():
        from .newsfeeds import FirstSeen, fetch_all
        from .data_providers import _MUST, _EXCLUDE
        fs = FirstSeen(Path(__file__).resolve().parent.parent / "events" / "news_first_seen.json")
        raw, status = fetch_all(fs, max_age_h=max(window_h, 6))
        cutoff = (dt.datetime.now() - dt.timedelta(hours=window_h)).strftime("%Y-%m-%d %H:%M")
        items = []
        for it in raw:
            low = it["title"].lower()
            if not any(k in low for k in _MUST + ("tanker", "hormuz", "refinery", "pipeline")):
                continue
            if any(k in low for k in _EXCLUDE):
                continue
            recent = (not it["published"]) or it["published"] >= cutoff
            items.append({"title": it["title"], "score": round(crude_headline_score(it["title"]), 2),
                          "source": it["source"], "lane": it["lane"], "trust": it["trust"],
                          "first_source": it.get("first_source"), "published": it["published"], "recent": recent})
        window = [i for i in items if i["recent"]] or items[:14]
        score = float(np.clip(np.mean([i["score"] * (0.5 + i["trust"]) for i in window]), -1, 1)) if window else 0.0
        return {"score": round(score, 2), "items": window[:20], "status": status,
                "n_sources_live": sum(1 for v in status.values() if not v["error"])}
    return _memo("news_pulse", ttl, fetch) or {"score": 0.0, "items": [], "status": {}, "n_sources_live": 0}


# ------------------------------------------------------------------- signals

_WEIGHTS = {"wti_lead": 0.25, "momentum": 0.20, "vwap": 0.15, "rsi": 0.10,
            "basis": 0.10, "oi_flow": 0.10, "news": 0.10}


def _clip(x, scale):
    return float(np.clip(x / scale, -1, 1))


def compute_signal(ticks: pd.DataFrame, news_score: float = 0.0) -> dict:
    if len(ticks) < 3:
        return {"label": "WARMING UP", "composite": 0, "subs": {}, "ticks": len(ticks),
                "note": "collecting ticks — signal needs a few minutes of session data"}
    m = ticks.set_index("ts")["ltp"].resample("1min").last().dropna()
    ltp = float(ticks["ltp"].iloc[-1])
    subs, notes = {}, {}

    # 1. WTI lead-lag: WTI moves first, MCX follows within minutes
    wti = get_wti_intraday()
    subs["wti_lead"] = 0.0
    if wti is not None and len(wti) > 6 and len(m) >= 6:
        wti_ret = wti.iloc[-1] / wti.iloc[-6] - 1
        mcx_ret = m.iloc[-1] / m.iloc[-6] - 1
        subs["wti_lead"] = _clip(wti_ret - mcx_ret, 0.0015)
        notes["wti_lead"] = f"WTI 5m {wti_ret:+.2%} vs MCX {mcx_ret:+.2%} → catch-up gap"

    # 2. EMA momentum (9 vs 21 on 1-min)
    subs["momentum"] = 0.0
    if len(m) >= 21:
        ema9, ema21 = m.ewm(span=9).mean().iloc[-1], m.ewm(span=21).mean().iloc[-1]
        subs["momentum"] = _clip((ema9 - ema21) / ltp, 0.0008)
        notes["momentum"] = f"EMA9 {ema9:.0f} vs EMA21 {ema21:.0f}"

    # 3. Session VWAP position (volume-weighted from cumulative-volume deltas)
    subs["vwap"] = 0.0
    dv = ticks["vol_cum"].diff().clip(lower=0)
    if dv.sum() > 0:
        vwap = float((ticks["ltp"] * dv).sum() / dv.sum())
        subs["vwap"] = _clip((ltp - vwap) / ltp, 0.0015)
        notes["vwap"] = f"LTP {ltp:.0f} vs session VWAP {vwap:.0f}"

    # 4. RSI14 on 1-min
    subs["rsi"] = 0.0
    if len(m) >= 15:
        d = m.diff()
        rs = d.clip(lower=0).ewm(alpha=1 / 14).mean().iloc[-1] / max(
            (-d.clip(upper=0)).ewm(alpha=1 / 14).mean().iloc[-1], 1e-9)
        rsi = 100 - 100 / (1 + rs)
        subs["rsi"] = float(np.clip((rsi - 50) / 50, -1, 1))
        notes["rsi"] = f"RSI14(1m) {rsi:.0f}"

    # 5. Parity premium z-score vs session
    subs["basis"] = 0.0
    prem = ticks["premium"].dropna()
    if len(prem) >= 5 and prem.std() > 0:
        z = (prem.iloc[-1] - prem.mean()) / prem.std()
        subs["basis"] = _clip(z, 2.0)
        notes["basis"] = f"premium ₹{prem.iloc[-1]:+.0f} (session z {z:+.1f})"

    # 6. OI money-flow quadrant vs ~30 min ago (the original repo's 4-quadrant logic, live)
    subs["oi_flow"] = 0.0
    past = ticks[ticks["ts"] <= ticks["ts"].iloc[-1] - pd.Timedelta(minutes=30)]
    if len(past):
        d_oi = ticks["oi"].iloc[-1] - past["oi"].iloc[-1]
        d_px = ltp - past["ltp"].iloc[-1]
        if d_oi > 0:
            subs["oi_flow"] = 1.0 if d_px > 0 else -1.0
            notes["oi_flow"] = "long buildup" if d_px > 0 else "short buildup"
        elif d_oi < 0:
            subs["oi_flow"] = -0.5 if d_px > 0 else 0.5
            notes["oi_flow"] = "short covering" if d_px > 0 else "long unwinding"

    # 7. News pulse
    subs["news"] = float(np.clip(news_score, -1, 1))

    composite = round(100 * sum(_WEIGHTS[k] * v for k, v in subs.items()), 1)
    # Confluence gates (OSS intraday practice: AND conditions, not just weighted sums).
    # STRONG requires the weighted score AND all three primary gates agreeing.
    long_gates = [subs["momentum"] > 0, subs["vwap"] > 0.1, subs["wti_lead"] > 0.1]
    short_gates = [subs["momentum"] < 0, subs["vwap"] < -0.1, subs["wti_lead"] < -0.1]
    label = ("STRONG BUY" if composite >= 45 else "BUY" if composite >= 18 else
             "STRONG SELL" if composite <= -45 else "SELL" if composite <= -18 else "NEUTRAL")
    if label == "STRONG BUY" and not all(long_gates):
        label = "BUY"
    if label == "STRONG SELL" and not all(short_gates):
        label = "SELL"
    confluence = (f"gates: {sum(long_gates)}/3 long, {sum(short_gates)}/3 short "
                  "(momentum · vwap · wti_lead)")
    return {"label": label, "composite": composite, "confluence": confluence,
            "subs": {k: round(v, 2) for k, v in subs.items()}, "notes": notes,
            "ltp": ltp, "ticks": len(ticks), "ts": str(ticks["ts"].iloc[-1])}


def log_signal(cache_dir: str, sig: dict, symbol: str = "CRUDEOIL") -> None:
    p = Path(cache_dir) / "live" / f"signals_{symbol}_{dt.date.today():%Y%m%d}.jsonl"
    with open(p, "a") as f:
        f.write(json.dumps(sig) + "\n")


def load_signal_history(cache_dir: str, symbol: str = "CRUDEOIL") -> pd.DataFrame:
    p = Path(cache_dir) / "live" / f"signals_{symbol}_{dt.date.today():%Y%m%d}.jsonl"
    if not p.exists():
        return pd.DataFrame()
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return pd.DataFrame([r for r in rows if "composite" in r])


def evaluate_signal_outcomes(ticks: pd.DataFrame, hist: pd.DataFrame,
                             horizon_min: int = 30, move_pct: float = 0.10) -> dict:
    """Score the live signal's track record: bucket signals to 5-min, then for each
    actionable bucket (BUY/SELL family) check the forward move over `horizon_min`.
    correct = moved >= move_pct% in the signal's direction; wrong = >= move_pct%
    against; flat = neither; pending = horizon not yet elapsed."""
    if hist.empty or ticks.empty:  # fresh day / no signals yet: same shape as the full result
        return {"events": pd.DataFrame(), "hit_rate": None, "scored": 0, "correct": 0, "wrong": 0,
                "flat": 0, "pending": 0, "horizon_min": horizon_min, "move_pct": move_pct}
    h = hist.copy()
    h["ts"] = pd.to_datetime(h["ts"])
    buckets = (h.set_index("ts").resample("5min")
               .agg({"label": "last", "composite": "last", "ltp": "last"}).dropna())
    px = ticks.set_index("ts")["ltp"]
    last_ts = px.index[-1]
    events = []
    for ts, row in buckets.iterrows():
        if not any(w in str(row["label"]) for w in ("BUY", "SELL")):
            continue
        side = 1 if "BUY" in row["label"] else -1
        tgt_time = ts + pd.Timedelta(minutes=horizon_min)
        if tgt_time > last_ts:
            outcome, fwd = "pending", None
        else:
            fwd_px = px[px.index >= tgt_time]
            fwd = 100 * (float(fwd_px.iloc[0]) / row["ltp"] - 1)
            directional = fwd * side
            outcome = ("correct" if directional >= move_pct
                       else "wrong" if directional <= -move_pct else "flat")
        events.append({"time": ts, "signal": row["label"], "score": row["composite"],
                       "ltp": row["ltp"], "fwd_pct": None if fwd is None else round(fwd, 3),
                       "outcome": outcome})
    ev = pd.DataFrame(events)
    scored = ev[ev["outcome"].isin(("correct", "wrong"))] if len(ev) else ev
    hit = round(100 * (scored["outcome"] == "correct").mean(), 1) if len(scored) else None
    return {"events": ev, "hit_rate": hit, "scored": len(scored),
            "correct": int((scored["outcome"] == "correct").sum()) if len(scored) else 0,
            "wrong": int((scored["outcome"] == "wrong").sum()) if len(scored) else 0,
            "flat": int((ev["outcome"] == "flat").sum()) if len(ev) else 0,
            "pending": int((ev["outcome"] == "pending").sum()) if len(ev) else 0,
            "horizon_min": horizon_min, "move_pct": move_pct}


if __name__ == "__main__":
    # live smoke: one tick + signal
    row = append_tick("cache")
    print("tick:", {k: row[k] for k in ("ts", "ltp", "wti", "usdinr", "fair", "premium")} if row else "FEED DOWN")
    ticks = load_ticks("cache")
    pulse = news_pulse()
    sig = compute_signal(ticks, pulse["score"])
    print("news pulse:", pulse["score"], f"({len(pulse['items'])} headlines)")
    print("signal:", sig["label"], sig["composite"], "| subs:", sig.get("subs"))
    assert row is None or (row["ltp"] > 1000 and sig["label"])
    print("live engine OK")
