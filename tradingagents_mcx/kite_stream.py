"""Strict MCX full-mode packet decoder and append-only market data storage."""
import datetime as dt
import json
from pathlib import Path
import struct

from .kite_data import atomic_json, normalize_quote
from .strategies import IST


def decode_packets(message, instruments):
    """Ignore heartbeats/text and unknown tokens; reject malformed binary batches."""
    if not isinstance(message, bytes) or len(message) < 2:
        return []
    count = struct.unpack_from('>H', message)[0]
    offset, ticks = 2, []
    for _ in range(count):
        if offset + 2 > len(message): raise ValueError('Incomplete Kite packet header')
        length = struct.unpack_from('>H', message, offset)[0]; offset += 2
        if offset + length > len(message): raise ValueError('Incomplete Kite packet')
        packet = message[offset:offset + length]; offset += length
        if length != 184: continue  # Only subscribed MCX full mode, never index/currency formats.
        integer = lambda start: struct.unpack_from('>I', packet, start)[0]
        token = integer(0)
        instrument = instruments.get(token)
        if not instrument or instrument.get('exchange') != 'MCX': continue
        stamp = lambda start: dt.datetime.fromtimestamp(integer(start), IST).replace(tzinfo=None)
        levels = [dict(quantity=integer(i), price=integer(i + 4) / 100,
                       orders=struct.unpack_from('>H', packet, i + 8)[0]) for i in range(64, 184, 12)]
        ticks.append((instrument, dict(instrument_token=token, last_price=integer(4) / 100,
            last_quantity=integer(8), average_price=integer(12) / 100, volume=integer(16),
            total_buy_quantity=integer(20), total_sell_quantity=integer(24),
            ohlc=dict(open=integer(28) / 100, high=integer(32) / 100,
                      low=integer(36) / 100, close=integer(40) / 100),
            last_trade_time=stamp(44), oi=integer(48), oi_day_high=integer(52), oi_day_low=integer(56),
            timestamp=stamp(60), depth=dict(buy=levels[:5], sell=levels[5:]))))
    if offset != len(message): raise ValueError('Unexpected bytes after Kite packet batch')
    return ticks


class QuoteStore:
    def __init__(self, cache_dir):
        self.root = Path(cache_dir) / 'kite'
        self.quotes = {}
        self.count = 0

    def record(self, instrument, tick, received_at):
        row = normalize_quote(instrument, tick, received_at)
        # Every received tick remains in the audit trail, including stale prints.
        path = self.root / 'ticks' / f"{row['symbol']}_{received_at:%Y%m%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as f: f.write(json.dumps(row, allow_nan=False) + '\n')
        previous = self.quotes.get(row['instrument_token'])
        if previous is None or row['exchange_timestamp'] >= previous['exchange_timestamp']:
            self.quotes[row['instrument_token']] = row
        self.count += 1

    def publish(self, now):
        atomic_json(self.root / 'latest.json', dict(updated_at=str(now), quotes=list(self.quotes.values())))

