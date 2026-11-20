#!/usr/bin/env python3
"""Kite market-data daemon. Uses quotes/instruments only; never places orders."""
import fcntl
from io import StringIO
import json
from pathlib import Path
import time
import threading
from urllib.parse import urlencode

import pandas as pd
import websocket

from tradingagents_mcx.config import load_config
from tradingagents_mcx.kite_data import KiteData, ROOT, atomic_json, select_contracts
from tradingagents_mcx.kite_stream import QuoteStore, decode_packets
from tradingagents_mcx.kite_history import refresh_candles
from tradingagents_mcx.strategies import now_ist


def status(state, reason, **fields):
    atomic_json(ROOT / 'events/kite_feed.json', dict(status=state, reason=reason,
                checked_at=str(now_ist()), **fields))


def instruments_for(data, cache, symbol, now):
    folder = cache / 'kite/instruments'; folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'MCX_{now:%Y%m%d}.csv'
    content = path.read_text() if path.exists() else data.get('/instruments/MCX', text=True)
    selected = select_contracts(pd.read_csv(StringIO(content)), now, symbol)
    if not selected: raise ValueError('No unexpired MCX futures found')
    if not path.exists():
        try:
            with path.open('x') as f: f.write(content)
        except FileExistsError: pass
    return {int(r['instrument_token']): r for r in selected}


def candle_worker(data, cache, symbol):
    last_bucket = None
    while True:
        now = now_ist(); bucket = (now.date(), now.hour, now.minute // 5)
        if data.session(now) and bucket != last_bucket:
            try:
                instruments = instruments_for(data, cache, symbol, now)
                front = next(iter(instruments.values()))
                result = refresh_candles(data, cache, front, now)
                atomic_json(ROOT / 'events/kite_history.json', dict(status='updated', checked_at=str(now), **result))
                last_bucket = bucket
            except Exception:
                atomic_json(ROOT / 'events/kite_history.json', dict(status='retrying', checked_at=str(now),
                    reason='Historical candles unavailable; retained data remains available'))
        time.sleep(30)


def run():
    cfg = load_config(); cache = Path(cfg['paths']['cache_dir'])
    symbol = cfg['analysis'].get('symbol', 'CRUDEOIL'); data = KiteData()
    websocket.enableTrace(False)
    threading.Thread(target=candle_worker, args=(data, cache, symbol), daemon=True).start()
    while True:
        now = now_ist(); session = data.session(now)
        if not session:
            status('login_required', 'Open the local Kite login page to authorize today’s data session')
            time.sleep(5); continue
        socket = None
        try:
            instruments = instruments_for(data, cache, symbol, now)
            instrument_day = now.date()
            store = QuoteStore(cache)
            # REST validates the data entitlement and seeds the exact same contracts.
            snapshot = data.get('/quote', [('i', 'MCX:' + r['tradingsymbol']) for r in instruments.values()])
            for row in instruments.values():
                quote = snapshot.get('MCX:' + row['tradingsymbol'])
                if quote: store.record(row, quote, now_ist())
            store.publish(now_ist())
            url = 'wss://ws.kite.trade?' + urlencode(dict(api_key=session['api_key'], access_token=session['access_token']))
            socket = websocket.create_connection(url, timeout=10)
            socket.settimeout(2)
            socket.send(json.dumps(dict(a='subscribe', v=list(instruments))))
            socket.send(json.dumps(dict(a='mode', v=['full', list(instruments)])))
            checked = published = 0.
            contracts = [dict(tradingsymbol=r['tradingsymbol'], expiry=str(r['expiry']),
                              instrument_token=r['instrument_token']) for r in instruments.values()]
            status('connected', 'WebSocket subscribed; quote freshness is checked separately', contracts=contracts)
            while True:
                now = now_ist(); clock = time.monotonic()
                if clock - checked >= 5:
                    if data.session(now).get('access_token') != session['access_token'] or now.date() != instrument_day:
                        # Refresh instruments on the next calendar day, even before token expiry.
                        break
                    checked = clock
                    status('connected', 'WebSocket subscribed; quote freshness is checked separately',
                           contracts=contracts, received_ticks=store.count,
                           last_received_at=max((q['received_at'] for q in store.quotes.values()), default=None))
                try: message = socket.recv()
                except websocket.WebSocketTimeoutException: continue
                if message == '': raise ConnectionError('Stream closed')
                for instrument, tick in decode_packets(message, instruments):
                    store.record(instrument, tick, now_ist())
                if clock - published >= 1:
                    store.publish(now_ist()); published = clock
        except Exception:
            # Connection exceptions can embed an authenticated URL. Never log their text.
            status('reconnecting', 'Kite connection unavailable; retrying in 10 seconds. Check login and subscription.')
            time.sleep(10)
        finally:
            if socket is not None:
                try: socket.close()
                except Exception: pass


if __name__ == '__main__':
    with (ROOT / '.kite_feed.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run()
