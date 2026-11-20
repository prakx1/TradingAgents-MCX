"""Shoonya (Finvasia) Noren API client — real MCX crude candles, free API.

Configure in config.json:
  "shoonya": {"user_id": "", "password": "", "totp_secret": "",
              "vendor_code": "", "api_secret": "", "imei": "abc1234"}
- user_id/password: your Shoonya login (SDK hashes the password itself)
- totp_secret: the base32 secret shown when you enable TOTP in Shoonya
- vendor_code: usually "<user_id>_U" (shown on the Shoonya API portal)
- api_secret: from https://prism.shoonya.com -> API key
- imei: any stable string

Mirrors the AngelOne interface (.configured / .get_candles / .get_ltp); every
failure returns None so the pipeline degrades to WTI parity.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

from .angelone import totp_now

MASTER_URL = "https://api.shoonya.com/MCX_symbols.txt.zip"


class Shoonya:
    def __init__(self, cfg: dict, cache_dir: str):
        s = cfg.get("shoonya", {})
        self.user_id = s.get("user_id", "")
        self.password = s.get("password", "")
        self.totp_secret = s.get("totp_secret", "")
        self.vendor_code = s.get("vendor_code", "") or (f"{self.user_id}_U" if self.user_id else "")
        self.api_secret = s.get("api_secret", "")
        self.imei = s.get("imei", "tamcx1234")
        self.cache_dir = Path(cache_dir)
        self._api = None

    @property
    def configured(self) -> bool:
        return all([self.user_id, self.password, self.totp_secret, self.api_secret])

    def _login(self):
        if self._api is not None:
            return self._api
        if not self.configured:
            return None
        try:
            from NorenRestApiPy.NorenApi import NorenApi

            class _App(NorenApi):
                def __init__(self):
                    super().__init__(host="https://api.shoonya.com/NorenWClientTP/",
                                     websocket="wss://api.shoonya.com/NorenWSTP/")

            api = _App()
            resp = api.login(userid=self.user_id, password=self.password,
                             twoFA=totp_now(self.totp_secret), vendor_code=self.vendor_code,
                             api_secret=self.api_secret, imei=self.imei)
            if resp and resp.get("stat") == "Ok":
                self._api = api
                return api
        except Exception:
            pass
        return None

    # ------------------------------------------------------ instrument master
    def crude_futures_chain(self, name: str = "CRUDEOIL") -> list[dict]:
        """Unexpired CRUDEOIL futures from the public symbol master (no login)."""
        cache = self.cache_dir / f"shoonya_mcx_{dt.date.today():%Y%m%d}.csv"
        try:
            if cache.exists():
                text = cache.read_text()
            else:
                r = requests.get(MASTER_URL, timeout=60)
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    text = z.read(z.namelist()[0]).decode()
                cache.write_text(text)
                for old in self.cache_dir.glob("shoonya_mcx_*.csv"):
                    if old != cache:
                        old.unlink()
        except Exception:
            return []
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("Symbol") != name or row.get("Instrument") != "FUTCOM":
                continue
            try:
                exp = dt.datetime.strptime(row["Expiry"], "%d-%b-%Y").date()
            except (ValueError, KeyError):
                continue
            if exp >= dt.date.today():
                out.append({"token": row["Token"], "tsym": row["TradingSymbol"],
                            "expiry": exp, "lot": row.get("LotSize")})
        return sorted(out, key=lambda x: x["expiry"])

    # ---------------------------------------------------------- market data
    def get_candles(self, symbol: str = "CRUDEOIL", days: int = 300,
                    interval: str = "ONE_DAY") -> pd.DataFrame | None:
        api = self._login()
        chain = self.crude_futures_chain(symbol)
        if not (api and chain):
            return None
        front = chain[0]
        start = dt.datetime.combine(dt.date.today() - dt.timedelta(days=days), dt.time())
        try:
            rows = api.get_daily_price_series(exchange="MCX", tradingsymbol=front["tsym"],
                                              startdate=start.timestamp(),
                                              enddate=dt.datetime.now().timestamp())
            if not rows:
                return None
            import json as _json
            recs = [_json.loads(r) if isinstance(r, str) else r for r in rows]
            df = pd.DataFrame(recs)
            colmap = {"into": "open", "inth": "high", "intl": "low", "intc": "close",
                      "intv": "volume", "v": "volume", "oi": "open_interest", "intoi": "open_interest"}
            df = df.rename(columns={k: v for k, v in colmap.items() if k in df.columns})
            df.index = pd.to_datetime(df["time"], format="mixed", dayfirst=True)
            keep = [c for c in ("open", "high", "low", "close", "volume", "open_interest") if c in df.columns]
            df = df[keep].astype(float).sort_index()
            df.attrs["contract"] = front["tsym"]
            df.attrs["expiry"] = front["expiry"].isoformat()
            return df.dropna(subset=["close"]) if "close" in df.columns else None
        except Exception:
            return None

    def get_ltp(self, symbol: str = "CRUDEOIL") -> float | None:
        api = self._login()
        chain = self.crude_futures_chain(symbol)
        if not (api and chain):
            return None
        try:
            q = api.get_quotes(exchange="MCX", token=chain[0]["token"])
            return float(q["lp"]) if q and q.get("lp") else None
        except Exception:
            return None


if __name__ == "__main__":
    sh = Shoonya({"shoonya": {}}, "cache")
    assert not sh.configured and sh.get_candles() is None
    chain = sh.crude_futures_chain()
    print("CRUDEOIL futures via Shoonya master:", len(chain))
    for c in chain[:3]:
        print(" ", c["tsym"], "| token", c["token"], "| expiry", c["expiry"], "| lot", c["lot"])
    assert chain, "symbol master failed"
    print("shoonya public-side OK (login/candles need account activation)")
