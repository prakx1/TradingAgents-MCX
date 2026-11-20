"""Moneycontrol unofficial MCX price feed — real quotes, free, no account.

Discovered by probing: GET priceapi.moneycontrol.com/pricefeed/mcx/commodityfuture/
{SYMBOL}?expiry=DDMONYYYY (e.g. 21SEP2026) returns the live contract quote
(LTP, OHL, prev close, volume, OI, ATP, bid/ask). Expiry list comes from
Shoonya's public symbol master. Unofficial: could change without notice —
every failure returns None and the pipeline falls back to WTI parity.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import requests

from .shoonya import Shoonya

_URL = "https://priceapi.moneycontrol.com/pricefeed/mcx/commodityfuture/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
            "Referer": "https://www.moneycontrol.com/commodity/mcx-crudeoil-price",
            "Accept": "application/json"}


def _fmt_expiry(d: dt.date) -> str:
    return d.strftime("%d%b%Y").upper()


def get_mcx_quotes(symbol: str = "CRUDEOIL", cache_dir: str = "cache",
                   max_contracts: int = 4) -> pd.DataFrame | None:
    """Live quotes for the listed contracts -> same shape as the bhavcopy curve
    (expiry, open/high/low/close, settle=prev close, volume, open_interest), so
    build_basis_data and the trader's price block consume it unchanged."""
    chain = Shoonya({}, cache_dir).crude_futures_chain(symbol)
    if not chain:
        return None
    rows = []
    for c in chain[:max_contracts]:
        try:
            r = requests.get(_URL.format(symbol=symbol),
                             params={"expiry": _fmt_expiry(c["expiry"])},
                             headers=_HEADERS, timeout=(4, 6))
            j = r.json()
            if j.get("code") != "200" or not j.get("data"):
                continue
            d = j["data"]
            rows.append({
                "expiry": d.get("EXPIRY_DATE") or c["expiry"].isoformat(),
                "open": float(d["OPEN"]), "high": float(d["HIGH"]), "low": float(d["LOW"]),
                "close": float(d["pricecurrent"]), "settle": float(d["priceprevclose"]),
                "volume": float(d["VOL"]), "open_interest": float(d["OPEN_INT"]),
                "oi_prev": float(d["OPEN_INT_PREV"]), "vwap": float(d["ATP"]),
                "change_pct": float(d["pricepercentchange"]),
                "last_trade": d.get("LTT", ""),
            })
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            break  # API down/throttled — don't burn a timeout on every remaining contract
        except Exception:
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["trade_date"] = dt.date.today().isoformat()
    df.attrs["source"] = "Moneycontrol live feed (unofficial)"
    return df


if __name__ == "__main__":
    q = get_mcx_quotes()
    assert q is not None and len(q) >= 2 and q["close"].iloc[0] > 1000, q
    front = q.iloc[0]
    print(f"MCX CRUDEOIL live via Moneycontrol: {len(q)} contracts")
    print(f"front {front['expiry']}: LTP Rs {front['close']:.0f} (prev {front['settle']:.0f}, "
          f"{front['change_pct']:+.2f}%), OI {front['open_interest']:.0f}, last trade {front['last_trade']}")
    print(q[["expiry", "close", "volume", "open_interest"]].to_string(index=False))
