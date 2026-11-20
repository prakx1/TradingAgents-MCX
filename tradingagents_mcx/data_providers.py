"""Data providers for MCX Crude Oil analysis — all free sources.

Replaces the original's akshare (China-only) layer:
  OHLCV            akshare futures_hist_em  -> yfinance WTI (CL=F) + MCX bhavcopy
  Basis            100ppi spot vs futures   -> MCX price vs WTIxUSDINR import parity
  Term structure   exchange daily bulletins -> NYMEX WTI monthly strip via yfinance
  Inventory        exchange warehouse stock -> EIA weekly petroleum status (free API key)
  Positioning      Sina broker top-20 seats -> CFTC Commitments of Traders (no key)
  News             SHMET/Sina/Chinese RSS   -> Google News RSS (India) + oil RSS feeds

Every provider degrades gracefully: on failure it returns None/empty plus a note,
and the analyst pipeline runs with whatever succeeded (mirrors the original's
per-module data-quality assessment).
"""
from __future__ import annotations

import datetime as dt
import re
from email.utils import parsedate_to_datetime

import pandas as pd
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# ---------------------------------------------------------------- prices / FX

def _naive(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance returns a plain Index (not DatetimeIndex) on empty/flaky fetches;
    tz_localize then raises and takes the whole Council run down."""
    if df is None or len(df) == 0:
        raise RuntimeError("yfinance returned no rows (rate-limited or transient) — retry next run")
    idx = pd.to_datetime(df.index)
    df.index = idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx
    return df


def _yf_history(symbol: str, days: int) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(symbol).history(period=f"{max(days, 90)}d", auto_adjust=False)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = _naive(df)
    return df.dropna(subset=["close"])


_YF_CACHE = Path(__file__).resolve().parent.parent / "cache" / "yf"


def _with_cache(key: str, fetch, live_col: str | None = None) -> pd.DataFrame:
    """Daily history with a last-good-copy fallback. yfinance rate-limits several times a day and each
    failure used to abort the whole Council. On success the frame is persisted; on failure the cached
    copy (<= 4 days old) is served, topped up with a provisional bar for today built from the collector's
    ticks, which carry the live WTI and USDINR on every print."""
    import datetime as _dt
    import time as _time
    _YF_CACHE.mkdir(parents=True, exist_ok=True)
    f = _YF_CACHE / f"{key}.csv"
    try:
        df = fetch()
        df.to_csv(f)
        return df
    except Exception as e:
        if not f.exists() or _time.time() - f.stat().st_mtime > 4 * 86400:
            raise
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        print(f"    ⚠ {key}: yfinance failed ({str(e)[:60]}) — using cached history to {df.index[-1].date()}", flush=True)
        if live_col:
            tf = _YF_CACHE.parent / "live" / f"CRUDEOIL_{_dt.date.today():%Y%m%d}.csv"
            today = pd.Timestamp(_dt.date.today())
            if tf.exists() and df.index[-1].normalize() < today:
                v = pd.read_csv(tf, usecols=[live_col])[live_col].dropna().astype(float)
                if len(v):
                    row = {c: float(v.iloc[-1]) for c in df.columns}
                    if "open" in df.columns:
                        row.update(open=float(v.iloc[0]), high=float(v.max()), low=float(v.min()), close=float(v.iloc[-1]))
                    if "volume" in df.columns:
                        row["volume"] = 0.0
                    df.loc[today] = row
                    print(f"    ⚠ {key}: today's bar provisional from live ticks (last {float(v.iloc[-1]):.2f})", flush=True)
        return df


def get_wti_history(days: int = 365) -> pd.DataFrame:
    """Daily WTI front-month OHLCV. MCX crude is cash-settled on WTI, so WTI is
    the true price driver; MCX bhavcopy (below) adds the INR series when available."""
    return _with_cache("CL=F", lambda: _yf_history("CL=F", days), live_col="wti")


def get_brent_history(days: int = 120) -> pd.DataFrame:
    return _with_cache("BZ=F", lambda: _yf_history("BZ=F", days))


def _usdinr_yf(days: int) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker("USDINR=X").history(period=f"{max(days, 90)}d")
    df = df.rename(columns=str.lower)[["close"]].rename(columns={"close": "usdinr"})
    df = _naive(df)
    return df.dropna()


def get_usdinr_history(days: int = 365) -> pd.DataFrame:
    return _with_cache("USDINR=X", lambda: _usdinr_yf(days), live_col="usdinr")


def get_brent_last() -> float | None:
    import yfinance as yf
    h = yf.Ticker("BZ=F").history(period="5d")
    return round(float(h["Close"].iloc[-1]), 2) if len(h) else None


def synthetic_mcx_series(wti: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    """INR fair-value series: WTI OHLC x same-day USDINR close. Approximate for
    intraday range but exact at settlement (MCX settles at WTI x RBI rate)."""
    fx_aligned = fx.reindex(wti.index).ffill()
    out = wti.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = (wti[col] * fx_aligned["usdinr"]).round(1)
    out["usdinr"] = fx_aligned["usdinr"].round(3)
    return out.dropna(subset=["close"])


# ---------------------------------------------------------------- MCX official

def get_mcx_bhavcopy(date: dt.date | None = None, retries_back: int = 5) -> pd.DataFrame | None:
    """Official MCX end-of-day bhavcopy (settlement prices, volume, OI for every
    contract). Free, no key — but mcxindia.com sits behind Akamai bot protection
    and only answers browser-like sessions from Indian residential IPs. Tries the
    maintained `mcxlib` package first, then a raw session; returns None on failure
    (analysis then runs on the WTI-parity series)."""
    date = date or dt.date.today()
    try:  # pip install mcxlib — handles MCX's cookie/session dance
        from mcxlib import get_bhav_copy
        for back in range(retries_back + 1):
            d = date - dt.timedelta(days=back)
            if d.weekday() >= 5:
                continue
            try:
                df = get_bhav_copy(trade_date=d.strftime("%Y%m%d"), instrument="ALL")
                if df is not None and len(df):
                    df["trade_date"] = d.isoformat()
                    return df
            except Exception:
                continue
    except ImportError:
        pass
    sess = requests.Session()
    sess.headers.update({**UA, "Referer": "https://www.mcxindia.com/market-data/bhavcopy",
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/json; charset=UTF-8"})
    try:
        sess.get("https://www.mcxindia.com/market-data/bhavcopy", timeout=20)
    except Exception:
        return None
    for back in range(retries_back + 1):
        d = date - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            r = sess.post("https://www.mcxindia.com/backpage.aspx/GetDateWiseBhavCopy",
                          json={"Date": d.strftime("%m/%d/%Y"), "InstrumentName": "ALL"}, timeout=25)
            rows = (r.json().get("d") or {}).get("Data") or []
            if rows:
                df = pd.DataFrame(rows)
                df["trade_date"] = d.isoformat()
                return df
        except Exception:
            continue
    return None


def mcx_crude_from_bhavcopy(bhav: pd.DataFrame) -> pd.DataFrame | None:
    """Extract CRUDEOIL futures rows -> per-expiry settlement/volume/OI curve."""
    if bhav is None or bhav.empty:
        return None
    cols = {c.lower(): c for c in bhav.columns}
    sym_col = cols.get("symbol") or cols.get("instrumentname")
    if not sym_col:
        return None
    fut = bhav[bhav[sym_col].astype(str).str.upper() == "CRUDEOIL"].copy()
    if fut.empty:
        return None
    rename = {}
    for want, options in {
        "expiry": ("expirydate", "expiry"), "open": ("open",), "high": ("high",),
        "low": ("low",), "close": ("close", "closeprice"), "settle": ("previousclose", "settlementprice", "settleprice"),
        "volume": ("volume", "volumeinthousands"), "open_interest": ("openinterest", "oi"),
        "value_lakhs": ("value",),
    }.items():
        for o in options:
            if o in cols:
                rename[cols[o]] = want
                break
    fut = fut.rename(columns=rename)
    keep = [c for c in ("expiry", "open", "high", "low", "close", "settle", "volume", "open_interest", "trade_date") if c in fut.columns]
    return fut[keep].reset_index(drop=True)


# ---------------------------------------------------------------- term structure

_MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec

def get_wti_curve(months: int = 8) -> pd.DataFrame:
    """NYMEX WTI strip via yfinance month contracts (CLU26.NYM style)."""
    import yfinance as yf
    today = dt.date.today()
    rows = []
    y, m = today.year, today.month + 1  # front tradable months start next month
    for _ in range(months):
        if m > 12:
            m -= 12
            y += 1
        sym = f"CL{_MONTH_CODES[m - 1]}{str(y)[-2:]}.NYM"
        try:
            h = yf.Ticker(sym).history(period="5d")
            if len(h):
                rows.append({"contract": sym.replace(".NYM", ""), "month": f"{y}-{m:02d}",
                             "close": round(float(h["Close"].iloc[-1]), 2),
                             "volume": int(h["Volume"].iloc[-1])})
        except Exception:
            pass
        m += 1
    return pd.DataFrame(rows)


def curve_metrics(curve: pd.DataFrame) -> dict:
    """Structure classification + annualized roll yield, as the original computed
    (roll_yield = ((next/current - 1) / month_diff) * 12)."""
    if curve is None or len(curve) < 2:
        return {}
    c = curve["close"].tolist()
    m1, m2, mlast = c[0], c[1], c[-1]
    spread_12 = round(m1 - m2, 2)
    slope_pct = round(100 * (mlast / m1 - 1), 2)
    roll_yield_ann = round(((m2 / m1 - 1) / 1) * 12 * 100, 2)  # front-to-second, annualized %
    if slope_pct < -1:
        structure = "backwardation (near > far: tight prompt supply, bullish carry)"
    elif slope_pct > 1:
        structure = "contango (far > near: ample supply / storage economics, bearish carry)"
    else:
        structure = "flat"
    return {"structure": structure, "m1": m1, "m2": m2, "m1_m2_spread": spread_12,
            "front_to_back_slope_pct": slope_pct, "roll_yield_annualized_pct": -roll_yield_ann,
            "contracts": len(c)}


# ---------------------------------------------------------------- EIA fundamentals

_EIA_SERIES = {  # v2 seriesid route needs the full v1-style id: PET.<SERIES>.W
    "us_crude_stocks_ex_spr_kbbl": "PET.WCESTUS1.W",
    "cushing_stocks_kbbl": "PET.W_EPC0_SAX_YCUOK_MBBL.W",
    "spr_stocks_kbbl": "PET.WCSSTUS1.W",
    "us_production_kbpd": "PET.WCRFPUS2.W",
    "refinery_utilization_pct": "PET.WPULEUS3.W",
}

def get_eia_wpsr_keyless() -> dict | None:
    """EIA Weekly Petroleum Status Report headline table — NO KEY NEEDED.
    https://ir.eia.gov/wpsr/table1.csv updates the moment the report drops
    (Wed 10:30 ET / 20:00 IST). Gives latest level + WoW + YoY change, but no
    long history — get a free EIA key for percentiles and cycle staging."""
    import csv
    import io
    try:
        r = requests.get("https://ir.eia.gov/wpsr/table1.csv", headers=UA, timeout=30)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))
    except Exception:
        return None
    header, out = rows[0], {}
    wanted = {"Commercial (Excluding SPR)": "us_crude_stocks_ex_spr",
              "Strategic Petroleum Reserve (SPR)": "spr_stocks",
              "Total Motor Gasoline": "gasoline_stocks",
              "Distillate Fuel Oil": "distillate_stocks"}
    for row in rows[1:]:
        if row and row[0] in wanted:
            try:
                out[wanted[row[0]]] = {
                    "latest": float(row[1]), "week_change": float(row[3]),
                    "yoy_change": float(row[6]), "unit": "million bbl",
                    "period": header[1], "source": "EIA WPSR table1.csv (keyless)",
                }
            except (ValueError, IndexError):
                continue
    return out or None


def get_eia_fundamentals(api_key: str, weeks: int = 260) -> dict | None:
    """Weekly U.S. petroleum data (EIA open data v2, free key). Returns latest
    values, weekly change, and percentile over the fetched history — the direct
    analog of the original's exchange-inventory percentile analysis.
    Without a key, falls back to the keyless WPSR headline table."""
    if not api_key:
        return get_eia_wpsr_keyless()
    out = {}
    for name, series in _EIA_SERIES.items():
        try:
            r = requests.get(f"https://api.eia.gov/v2/seriesid/{series}",
                             params={"api_key": api_key, "length": weeks,
                                     "sort[0][column]": "period", "sort[0][direction]": "desc"},
                             timeout=30)
            data = r.json()["response"]["data"]
            if not data:
                continue
            vals = [float(d["value"]) for d in data if d["value"] is not None]
            latest, prev = vals[0], vals[1] if len(vals) > 1 else vals[0]
            hist = pd.Series(vals[::-1])
            out[name] = {
                "latest": latest, "week_change": round(latest - prev, 1),
                "change_4w": round(latest - vals[4], 1) if len(vals) > 4 else None,
                "percentile_5y": round(100 * (hist <= latest).mean(), 1),
                "period": data[0]["period"], "unit": data[0].get("units", ""),
                "recent_series": vals[:26][::-1],  # oldest-first, ~6 months weekly
            }
        except Exception:
            continue
    return out or None


# ---------------------------------------------------------------- CFTC positioning

_COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
_WTI_CODE = "067651"  # NYMEX WTI-PHYSICAL

def get_cot_wti(weeks: int = 104) -> pd.DataFrame | None:
    """CFTC Disaggregated COT for NYMEX WTI. Managed-money longs/shorts and their
    weekly changes are the analog of the original spider-web dB/dS 'smart money'
    signals; non-reportables are the retail (reverse-indicator) seat."""
    try:
        r = requests.get(_COT_URL, params={
            "cftc_contract_market_code": _WTI_CODE,
            "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": weeks}, timeout=30)
        rows = r.json()
        if not rows:
            return None
    except Exception:
        return None
    recs = []
    for row in rows:
        g = lambda k: float(row[k]) if row.get(k) not in (None, "") else None
        recs.append({
            "date": row["report_date_as_yyyy_mm_dd"][:10],
            "open_interest": g("open_interest_all"),
            "mm_long": g("m_money_positions_long_all"), "mm_short": g("m_money_positions_short_all"),
            "mm_long_chg": g("change_in_m_money_long_all"), "mm_short_chg": g("change_in_m_money_short_all"),
            "prod_long": g("prod_merc_positions_long"), "prod_short": g("prod_merc_positions_short"),
            "swap_long": g("swap_positions_long_all"), "swap_short": g("swap__positions_short_all"),
            "retail_long": g("nonrept_positions_long_all"), "retail_short": g("nonrept_positions_short_all"),
            "conc_top4_long_pct": g("conc_gross_le_4_tdr_long"), "conc_top4_short_pct": g("conc_gross_le_4_tdr_short"),
        })
    df = pd.DataFrame(recs).sort_values("date").reset_index(drop=True)
    df["mm_net"] = df["mm_long"] - df["mm_short"]
    df["retail_net"] = df["retail_long"] - df["retail_short"]
    return df


def cot_snapshot(df: pd.DataFrame) -> dict:
    """Latest COT read with history percentiles + spider-web style signal."""
    if df is None or df.empty:
        return {}
    last = df.iloc[-1]
    dB, dS = last["mm_long_chg"] or 0, last["mm_short_chg"] or 0
    if dB > 0 and dS < 0:
        spider = "strongly bullish (smart money adding longs, covering shorts)"
    elif dB < 0 and dS > 0:
        spider = "strongly bearish (smart money cutting longs, adding shorts)"
    elif dB > 0 and dS > 0:
        spider = "two-sided position building — rising conviction dispute"
    elif dB < 0 and dS < 0:
        spider = "two-sided liquidation — de-risking"
    else:
        spider = "neutral"
    pct = lambda s: round(100 * (df[s] <= last[s]).mean(), 1)
    return {
        "report_date": last["date"],
        "open_interest": int(last["open_interest"]),
        "mm_long": int(last["mm_long"]), "mm_short": int(last["mm_short"]),
        "mm_net": int(last["mm_net"]), "mm_net_percentile": pct("mm_net"),
        "mm_long_change_wow": int(dB), "mm_short_change_wow": int(dS),
        "spider_web_signal": spider,
        "long_short_ratio": round(last["mm_long"] / last["mm_short"], 2) if last["mm_short"] else None,
        "retail_net": int(last["retail_net"]), "retail_net_percentile": pct("retail_net"),
        "producer_net": int((last["prod_long"] or 0) - (last["prod_short"] or 0)),
        "top4_concentration_long_pct": last["conc_top4_long_pct"],
        "top4_concentration_short_pct": last["conc_top4_short_pct"],
        "weeks_of_history": len(df),
    }


# ---------------------------------------------------------------- news

_GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
_RSS_FEEDS = [  # all verified live 2026-08-19
    ("OilPrice.com", "https://oilprice.com/rss/main"),
    ("Moneycontrol Commodities", "https://www.moneycontrol.com/rss/commodities.xml"),
    ("ET Commodities", "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms"),
    ("Business Standard Commodities", "https://www.business-standard.com/rss/markets/commodities-10608.rss"),
    ("Investing.com Commodities", "https://www.investing.com/rss/news_11.rss"),
]
_QUERIES = [
    "crude oil price WTI when:{d}d",
    "OPEC production oil when:{d}d",
    "US crude inventories EIA when:{d}d",
    "MCX crude oil when:{d}d",
    "oil supply geopolitics sanctions when:{d}d",
]
# Relevance gate ported from the original (_is_relevant_financial_news + scoring)
_MUST = ("crude", "oil", "wti", "brent", "opec", "petroleum", "energy")
_BOOST = {"price": 1.0, "inventor": 1.5, "opec": 1.5, "production": 1.0, "supply": 1.0,
          "demand": 1.0, "eia": 1.5, "sanction": 1.2, "mcx": 2.0, "rupee": 1.0,
          "barrel": 0.8, "futures": 0.8, "iran": 0.8, "russia": 0.8, "output": 0.8}
_EXCLUDE = ("ipo", "mutual fund", "cryptocur", "olive oil", "palm oil", "coconut oil",
            "edible oil", "essential oil", "oil massage", "castor oil")


def _score(title: str) -> float:
    t = title.lower()
    if not any(k in t for k in _MUST) or any(k in t for k in _EXCLUDE):
        return 0.0
    return 3.0 + sum(w for k, w in _BOOST.items() if k in t)


def get_news(days_back: int = 3, max_items: int = 40, serper_key: str = "",
             newsapi_key: str = "", gnews_key: str = "") -> list[dict]:
    """Multi-source crude oil news, relevance-scored and deduped.
    Free tier: Google News RSS + public RSS (no keys). Optional keys enrich."""
    import feedparser
    items = []

    def add(title, link, source, published, lane="legacy", trust=0.5):
        s = _score(title or "")
        if lane == "official" and s <= 0:
            s = 2.0  # primary sources (EIA/OPEC/OFAC/UKMTO/CENTCOM) are always in scope
        if s > 0:
            items.append({"title": title.strip(), "url": link, "source": source, "published": published,
                          "score": round(s + (1.0 if lane == "official" else 0.0), 1), "lane": lane, "trust": trust})

    # the full source registry (50 verified feeds incl. official, wire, regional, shipping, social)
    try:
        from .newsfeeds import fetch_all
        reg_items, _status = fetch_all(None, max_age_h=max(24, days_back * 24))
        for it in reg_items:
            add(it["title"], it.get("link", ""), it["source"], it.get("published", ""), it.get("lane", "wire"), it.get("trust", 0.5))
    except Exception:
        reg_items = []
    for q in ([] if reg_items else _QUERIES):  # legacy Google/RSS path only if the registry failed
        try:
            feed = feedparser.parse(_GOOGLE_NEWS.format(q=requests.utils.quote(q.format(d=days_back))))
            for e in feed.entries[:15]:
                add(e.get("title"), e.get("link"), (e.get("source") or {}).get("title", "GoogleNews"),
                    _norm_date(e.get("published")))
        except Exception:
            continue
    for name, url in ([] if reg_items else _RSS_FEEDS):
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:15]:
                add(e.get("title"), e.get("link"), name, _norm_date(e.get("published")))
        except Exception:
            continue

    if serper_key:  # original repo's optional Serper enrichment, localized gl=in
        try:
            r = requests.post("https://google.serper.dev/news",
                              headers={"X-API-KEY": serper_key},
                              json={"q": "MCX crude oil OR WTI crude price", "num": 10, "gl": "in", "hl": "en"},
                              timeout=20)
            for n in r.json().get("news", []):
                add(n.get("title"), n.get("link"), n.get("source", "Serper"), n.get("date", ""))
        except Exception:
            pass
    if newsapi_key:
        try:
            r = requests.get("https://newsapi.org/v2/everything",
                             params={"q": "crude oil OR WTI OR OPEC", "language": "en",
                                     "sortBy": "publishedAt", "pageSize": 20, "apiKey": newsapi_key},
                             timeout=20)
            for a in r.json().get("articles", []):
                add(a.get("title"), a.get("url"), (a.get("source") or {}).get("name", "NewsAPI"),
                    (a.get("publishedAt") or "")[:10])
        except Exception:
            pass
    if gnews_key:
        try:
            r = requests.get("https://gnews.io/api/v4/search",
                             params={"q": "crude oil", "lang": "en", "max": 10, "apikey": gnews_key},
                             timeout=20)
            for a in r.json().get("articles", []):
                add(a.get("title"), a.get("url"), (a.get("source") or {}).get("name", "GNews"),
                    (a.get("publishedAt") or "")[:10])
        except Exception:
            pass

    seen, unique = set(), []
    for it in sorted(items, key=lambda x: -x["score"]):
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key and key not in seen:
            seen.add(key)
            unique.append(it)
    official = [u for u in unique if u.get("lane") == "official"][:12]
    rest = [u for u in unique if u.get("lane") != "official"][:max(0, max_items - len(official))]
    return official + rest


def _norm_date(s):
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(s)[:16]


# ------------------------------------------------- India/extras (full version)

def get_usdinr_reference() -> dict | None:
    """Daily USDINR fix, keyless (frankfurter = ECB 16:00 CET fix — within paise of
    the RBI/FBIL reference rate MCX settles on; RBI publishes no API)."""
    try:
        r = requests.get("https://api.frankfurter.dev/v1/latest",
                         params={"base": "USD", "symbols": "INR"}, timeout=15)
        j = r.json()
        return {"rate": float(j["rates"]["INR"]), "date": j["date"],
                "source": "frankfurter/ECB daily fix (proxy for RBI reference rate)"}
    except Exception:
        return None


def get_mcx_participant_oi() -> dict | None:
    """MCX PRO vs CLIENT participant data via mcxlib (works from Indian residential
    IPs; Akamai-blocked elsewhere). India-native complement to CFTC COT."""
    try:
        from mcxlib import get_pro_cli_details
        df = get_pro_cli_details(trade_month=dt.date.today().strftime("%Y%m"))
        if df is None or df.empty:
            return None
        return {"latest": df.tail(5).to_dict("records"), "source": "MCX pro/client disclosure"}
    except Exception:
        return None


def get_av_sentiment(api_key: str, limit: int = 12) -> list[dict] | None:
    """Alpha Vantage NEWS_SENTIMENT for energy — the only free *scored* oil
    sentiment API (25 req/day free tier)."""
    if not api_key:
        return None
    try:
        r = requests.get("https://www.alphavantage.co/query",
                         params={"function": "NEWS_SENTIMENT", "topics": "energy_transportation",
                                 "sort": "LATEST", "limit": limit, "apikey": api_key}, timeout=30)
        feed = r.json().get("feed") or []
        return [{"title": a["title"], "sentiment": a.get("overall_sentiment_label"),
                 "score": a.get("overall_sentiment_score"), "source": a.get("source"),
                 "published": (a.get("time_published") or "")[:8]} for a in feed[:limit]] or None
    except Exception:
        return None


if __name__ == "__main__":
    wti = get_wti_history(120)
    fx = get_usdinr_history(120)
    mcx = synthetic_mcx_series(wti, fx)
    assert len(mcx) > 50 and mcx["close"].iloc[-1] > 1000, "synthetic INR series looks wrong"
    curve = get_wti_curve(6)
    cm = curve_metrics(curve)
    cot = cot_snapshot(get_cot_wti())
    news = get_news(2, 10)
    print("WTI last:", round(float(wti['close'].iloc[-1]), 2),
          "| MCX fair value:", round(float(mcx['close'].iloc[-1]), 1), "INR/bbl")
    print("Curve:", cm.get("structure"), "| m1-m2:", cm.get("m1_m2_spread"))
    print("COT:", cot.get("spider_web_signal"), "| MM net:", cot.get("mm_net"))
    print("News items:", len(news), "| top:", news[0]["title"][:80] if news else "none")
    assert cot and news, "COT or news failed"
    print("data_providers self-check OK")
