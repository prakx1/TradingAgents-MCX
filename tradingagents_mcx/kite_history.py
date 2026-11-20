"""Broker five-minute candles; originals retained in an append-only audit file."""
import datetime as dt
import json
import math
from pathlib import Path

import pandas as pd

from .kite_data import atomic_json, read_json


def refresh_candles(data, cache_dir, instrument, now):
    payload = data.get(f"/instruments/historical/{int(instrument['instrument_token'])}/5minute",
        dict(**{'from':(now - dt.timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S'),
                'to':now.strftime('%Y-%m-%d %H:%M:%S')}, oi=1))
    rows = []
    for values in payload.get('candles', []):
        if len(values) < 6: raise ValueError('Incomplete historical candle')
        stamp = pd.Timestamp(values[0])
        if stamp.tzinfo: stamp = stamp.tz_convert('Asia/Kolkata').tz_localize(None)
        if stamp + pd.Timedelta(minutes=5) > now: continue
        o, h, l, c, v = map(float, values[1:6])
        if not all(math.isfinite(x) for x in (o,h,l,c,v)) or min(o,h,l,c) <= 0 or v < 0:
            raise ValueError('Invalid historical candle values')
        if h < max(o,l,c) or l > min(o,h,c): raise ValueError('Invalid candle geometry')
        rows.append(dict(ts=str(stamp), open=o, high=h, low=l, close=c, volume=v))
    if not rows: raise ValueError('No completed broker candles returned')
    rows = sorted({r['ts']:r for r in rows}.values(), key=lambda r:r['ts'])
    root = Path(cache_dir) / 'kite/candles'; root.mkdir(parents=True, exist_ok=True)
    path = root / f"{instrument['tradingsymbol']}.json"
    old = {r['ts']:r for r in read_json(path).get('candles', [])}
    changed = [r for r in rows if old.get(r['ts']) != r]
    with path.with_suffix('.audit.jsonl').open('a') as f:
        for row in changed:
            f.write(json.dumps(dict(retrieved_at=str(now), instrument_token=int(instrument['instrument_token']),
                                   tradingsymbol=instrument['tradingsymbol'], expiry=str(instrument['expiry']), **row)) + '\n')
    atomic_json(path, dict(symbol=instrument['name'], expiry=str(instrument['expiry']),
        tradingsymbol=instrument['tradingsymbol'], instrument_token=int(instrument['instrument_token']),
        retrieved_at=str(now), source='kite_historical', candles=rows))
    return dict(completed_candles=len(rows), new_or_revised=len(changed), last_completed_candle=rows[-1]['ts'])


def historical_bars(cache_dir, symbol, expiry, now):
    frames = []
    for path in (Path(cache_dir) / 'kite/candles').glob(f'{symbol}*.json'):
        data = read_json(path)
        if data.get('symbol') != symbol or data.get('expiry') != expiry: continue
        try:
            if pd.Timestamp(data['retrieved_at']) > now: continue  # No historical point-in-time lookahead.
            bars = pd.DataFrame(data['candles']); bars['ts'] = pd.to_datetime(bars.ts)
            frames.append(bars.set_index('ts'))
        except (KeyError, ValueError, TypeError): continue
    if not frames: return pd.DataFrame()
    bars = pd.concat(frames).sort_index()
    return bars[(bars.index + pd.Timedelta(minutes=5) <= now) & ~bars.index.duplicated(keep='last')]

