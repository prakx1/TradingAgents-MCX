"""Mark both signal horizons from observed ticks, without an LLM or UI session."""
from __future__ import annotations
import datetime as dt
from pathlib import Path
import pandas as pd
from .signals import SignalBook
from .strategies import NAMES, now_ist, strategy_config


def maintain_signals(cfg: dict, now: dt.datetime | None = None) -> list[dict]:
    now = now or now_ist()
    book = SignalBook(cfg)
    rows = book.load()
    active = [r for r in rows if r['status'] == 'active']
    if not active:
        return rows
    start = min(r['issued_at'][:10] for r in active).replace('-', '')
    symbol = active[-1].get('symbol', cfg['analysis'].get('symbol', 'CRUDEOIL'))
    frames = []
    for path in sorted((Path(cfg['paths']['cache_dir']) / 'live').glob(f'{symbol}_2*.csv')):
        if start <= path.stem.rsplit('_', 1)[-1] <= now.strftime('%Y%m%d'):
            try:
                frames.append(pd.read_csv(path, parse_dates=['ts']))
            except (OSError, ValueError, pd.errors.EmptyDataError):
                continue
    ticks = pd.concat(frames, ignore_index=True).sort_values('ts').drop_duplicates('ts') if frames else pd.DataFrame()
    return book.mark(ticks, now=now)


def maintain_all(cfg: dict, now: dt.datetime | None = None) -> dict:
    # Disabled desks still need their existing signals monitored.
    now = now or now_ist()
    books = {name: maintain_signals(strategy_config(cfg, name), now) for name in NAMES}
    from .setups import PendingSetups
    scfg = strategy_config(cfg, 'intraday')
    setups = PendingSetups(scfg)
    if any(r['status']=='pending' for r in setups.load()):
        # Include previous sessions for a stable MACD lookback; trigger candles
        # themselves must be newer than the council's approval.
        symbol = scfg['analysis'].get('symbol','CRUDEOIL')
        paths = sorted((Path(cfg['paths']['cache_dir'])/'live').glob(f'{symbol}_2*.csv'))[-5:]
        frames = []
        for path in paths:
            try:
                frames.append(pd.read_csv(path,parse_dates=['ts']))
            except (OSError,ValueError,pd.errors.EmptyDataError):
                continue
        ticks = pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
        setups.poll(ticks,now)
        books['intraday'] = SignalBook(scfg).load()
    from .paper import PaperBook
    PaperBook(scfg).sync(books['intraday'],now)
    from .mock_experiment import MockExperiment
    # Historical experimental positions still need exits. New live mock entries
    # must come through the council signal book, even if an old config enables it.
    MockExperiment(cfg).poll(now, allow_entries=False)
    return books
