"""Kite authentication and market-data-only access. No broker order methods."""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import urlencode

import pandas as pd
import requests

from .strategies import now_ist

ROOT = Path(__file__).resolve().parents[1]
API = 'https://api.kite.trade'


def atomic_json(path, value, private=False):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        os.chmod(temp, 0o600 if private else 0o644)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, default=str, allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def read_json(path):
    try: return json.loads(Path(path).read_text())
    except (OSError, ValueError): return {}


class KiteData:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.private = self.root / '.secrets'
        self.credentials_path = self.private / 'kite_credentials.json'
        self.session_path = self.private / 'kite_session.json'

    def credentials(self):
        return read_json(self.credentials_path)

    def session(self, now=None):
        now = now or now_ist()
        value = read_json(self.session_path)
        try:
            if not value.get('access_token') or not now < dt.datetime.fromisoformat(value['expires_at']):
                return {}
            if value.get('api_key') != self.credentials().get('api_key'): return {}
        except (TypeError, ValueError, KeyError): return {}
        return value

    def session_status(self, now=None):
        """Read-only login metadata for display; never return credentials or tokens.

        Expiry describes the saved login. Stream connectivity and quote freshness
        are separate checks, so a closed market does not imply an expired login.
        """
        now = pd.Timestamp(now or now_ist())
        if now.tzinfo: now = now.tz_convert('Asia/Kolkata').tz_localize(None)
        credentials = self.credentials()
        configured = isinstance(credentials, dict) and bool(credentials.get('api_key') and credentials.get('api_secret'))
        status = dict(state='login_required' if configured else 'not_configured',
                      expires_at=None, seconds_remaining=None, configured=configured)
        if not configured: return status
        value = read_json(self.session_path)
        if not isinstance(value, dict) or not value.get('access_token') or value.get('api_key') != credentials['api_key']:
            return status
        try:
            expires = pd.Timestamp(value['expires_at'])
            if pd.isna(expires): return status
            if expires.tzinfo: expires = expires.tz_convert('Asia/Kolkata').tz_localize(None)
            seconds = (expires - now).total_seconds()
        except (KeyError, TypeError, ValueError, OverflowError): return status
        state = 'expired' if seconds <= 0 else 'expiring_soon' if seconds <= 3600 else 'active'
        return dict(status, state=state, expires_at=expires.isoformat(), seconds_remaining=max(0, math.ceil(seconds)))

    def login_url(self, state):
        key = self.credentials().get('api_key')
        if not key: raise ValueError('Kite credentials are not configured locally')
        return 'https://kite.zerodha.com/connect/login?' + urlencode(
            dict(v=3, api_key=key, redirect_params=urlencode(dict(state=state))))

    def exchange_token(self, request_token):
        credentials = self.credentials()
        try:
            checksum = hashlib.sha256((credentials['api_key'] + request_token + credentials['api_secret']).encode()).hexdigest()
            response = requests.post(API + '/session/token', data=dict(api_key=credentials['api_key'],
                request_token=request_token, checksum=checksum), headers={'X-Kite-Version': '3'}, timeout=15)
            payload = response.json()
            if not response.ok or payload.get('status') != 'success':
                raise ValueError('Kite login failed; retry from the local login page')
            data = payload['data']
            if credentials.get('user_id') and data['user_id'] != credentials['user_id']:
                raise ValueError('The logged-in Zerodha account does not match the configured account')
            now = now_ist()
            expires = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(6))
            value = dict(api_key=credentials['api_key'], access_token=data['access_token'],
                         user_id=data['user_id'], created_at=str(now), expires_at=str(expires))
            self.private.mkdir(parents=True, exist_ok=True); self.private.chmod(0o700)
            atomic_json(self.session_path, value, private=True)
            return dict(connected=True, expires_at=str(expires))
        except (requests.RequestException, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError('Kite token exchange failed; retry the browser login') from None

    def get(self, path, params=None, text=False):
        # Fixed allowlist: even an accidental caller cannot use order/account APIs.
        allowed = path in ('/instruments/MCX', '/quote') or (
            path.startswith('/instruments/historical/') and path.endswith('/5minute')
            and path.split('/')[-2].isdigit())
        if not allowed: raise ValueError('Only MCX instruments, quotes and historical candles are allowed')
        session = self.session()
        if not session: raise ValueError('Kite login required')
        try:
            response = requests.get(API + path, params=params, headers={'X-Kite-Version': '3',
                'Authorization': f"token {session['api_key']}:{session['access_token']}"}, timeout=15)
            if response.status_code in (401, 403): raise ValueError('Kite login or data subscription required')
            if not response.ok: raise ValueError(f'Kite market data unavailable (HTTP {response.status_code})')
            if text: return response.text
            payload = response.json()
            if payload.get('status') != 'success': raise ValueError('Kite market data request failed')
            return payload['data']
        except (requests.RequestException, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError('Kite market-data connection failed') from None


def select_contracts(frame, now, symbol='CRUDEOIL'):
    frame = frame.copy()
    if not {'exchange', 'instrument_type', 'name', 'expiry', 'tradingsymbol', 'instrument_token'} <= set(frame):
        raise ValueError('Invalid Kite instrument master')
    expiry = pd.to_datetime(frame.expiry, errors='coerce')
    good = ((frame.exchange == 'MCX') & (frame.instrument_type == 'FUT') & (frame.name == symbol)
            & expiry.notna() & (expiry.dt.date >= now.date()))
    return frame[good].sort_values('expiry').head(2).to_dict('records')


def normalize_quote(instrument, tick, received_at):
    """REST-shaped exchange data, retaining both clocks and contract identity."""
    expiry = str(instrument['expiry'])[:10]
    ohlc = tick['ohlc']; price = float(tick['last_price'])
    if not math.isfinite(price) or price <= 0: raise ValueError('Invalid Kite price')
    previous = float(ohlc['close'])
    for value in [previous, *[ohlc[k] for k in ('open', 'high', 'low')], tick['volume'], tick['oi']]:
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError('Invalid Kite quote values')
    return dict(expiry=expiry, symbol=instrument['name'], instrument_token=int(instrument['instrument_token']),
        tradingsymbol=instrument['tradingsymbol'], received_at=str(received_at),
        exchange_timestamp=str(tick['timestamp']), last_trade=str(tick['last_trade_time']),
        open=float(ohlc['open']), high=float(ohlc['high']), low=float(ohlc['low']), close=price,
        settle=previous, volume=float(tick['volume']), open_interest=float(tick['oi']),
        vwap=float(tick.get('average_price', 0)), change_pct=(price / previous - 1) * 100 if previous else 0,
        depth=tick.get('depth', {}), source='kite')


def cached_quotes(cache_dir, symbol='CRUDEOIL', now=None):
    now = now or now_ist()
    snapshot = read_json(Path(cache_dir) / 'kite' / 'latest.json')
    all_rows = sorted([r for r in snapshot.get('quotes', []) if r.get('symbol') == symbol
                       and r.get('expiry', '') >= now.date().isoformat()], key=lambda r: r['expiry'])
    rows = []
    for row in all_rows:
        try:
            ages = [(now - pd.Timestamp(row[key]).to_pydatetime()).total_seconds()
                    for key in ('received_at', 'exchange_timestamp', 'last_trade')]
            valid = 0 <= ages[0] <= 30 and all(0 <= age <= 120 for age in ages[1:])
            if not valid:
                if not rows: return None  # Do not silently promote a later contract.
                continue
            rows.append(row)
        except (ValueError, KeyError, TypeError): return None
    if not rows: return None
    frame = pd.DataFrame(rows); frame['trade_date'] = now.date().isoformat()
    frame.attrs['source'] = 'Kite Connect streaming market data'
    return frame
