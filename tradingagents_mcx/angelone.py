"""Angel One SmartAPI client — real MCX crude candles and quotes.

Free API (no monthly fee). Configure in config.json:
  "angel_one": {"api_key": "...", "client_id": "...", "pin": "...", "totp_secret": "..."}
- api_key:     create an app at https://smartapi.angelone.in/create  (type: Market Feeds or Trading)
- totp_secret: enable at https://smartapi.angelone.in/enable-totp (the base32 string under the QR)
- pin:         your login PIN (SmartAPI uses it as the password field)

Every method returns None on any failure so the pipeline degrades to WTI parity.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import struct
import time
from pathlib import Path

import pandas as pd
import requests

BASE = "https://apiconnect.angelone.in"
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


def totp_now(secret: str) -> str:
    """RFC 6238 TOTP (SHA1, 30s, 6 digits) — stdlib only."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(time.time()) // 30)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[19] & 15
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**6).zfill(6)


class AngelOne:
    def __init__(self, cfg: dict, cache_dir: str):
        ao = cfg.get("angel_one", {})
        self.api_key = ao.get("api_key", "")
        self.client_id = ao.get("client_id", "")
        self.pin = ao.get("pin", "")
        self.totp_secret = ao.get("totp_secret", "")
        self.cache_dir = Path(cache_dir)
        self.jwt = None

    @property
    def configured(self) -> bool:
        return all([self.api_key, self.client_id, self.pin, self.totp_secret])

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "X-UserType": "USER", "X-SourceID": "WEB", "X-PrivateKey": self.api_key,
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
             "X-MACAddress": "00:00:00:00:00:00"}
        if self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    def login(self) -> bool:
        if not self.configured:
            return False
        if self.jwt:
            return True
        try:
            r = requests.post(f"{BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
                              headers=self._headers(),
                              json={"clientcode": self.client_id, "password": self.pin,
                                    "totp": totp_now(self.totp_secret)},
                              timeout=20)
            data = r.json()
            if data.get("status"):
                self.jwt = data["data"]["jwtToken"]
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------ instrument master
    def _scrip_master(self) -> list[dict] | None:
        """MCX rows of the daily instrument master, cached one day (full file ~100MB)."""
        cache = self.cache_dir / f"mcx_scrips_{dt.date.today():%Y%m%d}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        try:
            r = requests.get(SCRIP_MASTER, timeout=120)
            mcx = [s for s in r.json() if s.get("exch_seg") == "MCX"]
            cache.write_text(json.dumps(mcx))
            for old in self.cache_dir.glob("mcx_scrips_*.json"):
                if old != cache:
                    old.unlink()
            return mcx
        except Exception:
            return None

    def crude_futures_chain(self, name: str = "CRUDEOIL") -> list[dict]:
        """Unexpired CRUDEOIL futures sorted by expiry (front first)."""
        scrips = self._scrip_master() or []
        out = []
        for s in scrips:
            if s.get("name") != name or s.get("instrumenttype") != "FUTCOM":
                continue
            try:
                exp = dt.datetime.strptime(s["expiry"], "%d%b%Y").date()
            except (ValueError, KeyError):
                continue
            if exp >= dt.date.today():
                out.append({"token": s["token"], "symbol": s["symbol"], "expiry": exp,
                            "lot": s.get("lotsize"), "tick": s.get("tick_size")})
        return sorted(out, key=lambda x: x["expiry"])

    # --------------------------------------------------------------- market data
    def get_candles(self, symbol: str = "CRUDEOIL", days: int = 200,
                    interval: str = "ONE_DAY") -> pd.DataFrame | None:
        """Daily candles for the front-month contract. New contracts carry only a few
        months of history — the pipeline falls back to WTI if fewer than ~80 bars."""
        if not self.login():
            return None
        chain = self.crude_futures_chain(symbol)
        if not chain:
            return None
        front = chain[0]
        try:
            r = requests.post(f"{BASE}/rest/secure/angelbroking/historical/v1/getCandleData",
                              headers=self._headers(),
                              json={"exchange": "MCX", "symboltoken": front["token"],
                                    "interval": interval,
                                    "fromdate": (dt.date.today() - dt.timedelta(days=days)).strftime("%Y-%m-%d 09:00"),
                                    "todate": dt.datetime.now().strftime("%Y-%m-%d %H:%M")},
                              timeout=30)
            rows = r.json().get("data") or []
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df.index = pd.to_datetime(df.pop("ts")).dt.tz_localize(None)
            df.attrs["contract"] = front["symbol"]
            df.attrs["expiry"] = front["expiry"].isoformat()
            return df.astype(float)
        except Exception:
            return None

    def get_ltp(self, symbol: str = "CRUDEOIL") -> float | None:
        if not self.login():
            return None
        chain = self.crude_futures_chain(symbol)
        if not chain:
            return None
        try:
            r = requests.post(f"{BASE}/rest/secure/angelbroking/market/v1/quote/",
                              headers=self._headers(),
                              json={"mode": "LTP", "exchangeTokens": {"MCX": [chain[0]["token"]]}},
                              timeout=15)
            fetched = r.json()["data"]["fetched"]
            return float(fetched[0]["ltp"]) if fetched else None
        except Exception:
            return None


if __name__ == "__main__":
    # RFC 6238 test vector: SHA1, secret "12345678901234567890" (b32: GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ), T=59 -> 94287082
    import unittest.mock as um
    with um.patch("time.time", return_value=59):
        assert totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ") == "94287082"[-6:], "TOTP vector failed"
    print("totp RFC-6238 vector OK")
    ao = AngelOne({"angel_one": {}}, "/tmp")
    assert not ao.configured and ao.get_candles() is None
    print("unconfigured degradation OK")
