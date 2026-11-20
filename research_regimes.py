#!/usr/bin/env python3
"""Freeze and replay the regime experiment; never writes trading history."""
import argparse
from dataclasses import asdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil

from tradingagents_mcx.config import load_config
from tradingagents_mcx.economics import configured_fee_points
from tradingagents_mcx.entry_research import read_ticks
from tradingagents_mcx.paper import paper_settings
from tradingagents_mcx.regime_research import LIMITATIONS, ReplayProtocol, replay
from tradingagents_mcx.regimes import RULES
from tradingagents_mcx.strategies import now_ist, strategy_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('start', 'end', 'split'): parser.add_argument('--' + name, required=True, type=dt.date.fromisoformat)
    parser.add_argument('--as-of', type=dt.datetime.fromisoformat)
    parser.add_argument('--ticks-dir', type=Path); parser.add_argument('--output', type=Path)
    args = parser.parse_args(); cfg = load_config(); scfg = strategy_config(cfg, 'intraday'); now = now_ist()
    as_of = min(args.as_of or now, now)
    if not args.start <= args.split <= args.end <= as_of.date(): parser.error('Require start <= split <= end <= as-of')
    protocol = ReplayProtocol(fee_points=configured_fee_points(cfg), daily_entry_limit=paper_settings(cfg)['max_new_trades_per_day'])
    output = args.output or Path(scfg['paths']['project_root']) / 'research' / f'regimes_{now:%Y%m%d_%H%M%S}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists(): raise FileExistsError(output)
    root = Path(__file__).resolve().parent
    names = ['research_regimes.py', 'tradingagents_mcx/regime_research.py', 'tradingagents_mcx/regimes.py',
             'tradingagents_mcx/entries.py', 'tradingagents_mcx/indicators.py', 'tradingagents_mcx/economics.py',
             'tradingagents_mcx/forecast_research.py', 'tradingagents_mcx/entry_research.py', 'tradingagents_mcx/strategies.py']
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}
    ticks, metadata = read_ticks(args.ticks_dir or Path(cfg['paths']['cache_dir']) / 'live', cfg['analysis'].get('symbol', 'CRUDEOIL'), as_of)
    frozen = dict(protocol=asdict(protocol), rules=asdict(RULES), limitations=LIMITATIONS, policy=scfg['strategy'],
        created_at=str(now), as_of=str(as_of), start=str(args.start), end=str(args.end), split=str(args.split),
        input=metadata, code_sha256=hashes)
    source_folder = output.with_suffix('.source')
    source_folder.mkdir(exist_ok=False)
    for name in names:
        target = source_folder / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    with output.with_suffix('.protocol.json').open('x') as f: json.dump(frozen, f, indent=2, allow_nan=False)
    print(f'Rules and inputs frozen; replaying {len(ticks):,} observations.', flush=True)
    result = replay(ticks, scfg['strategy'], args.start, args.end, as_of, args.split, protocol)
    result.update(generated_at=str(now_ist()), input=metadata, code_sha256=hashes)
    with output.open('x') as f: json.dump(result, f, indent=2, allow_nan=False)
    print(output, flush=True)
    for row in result['summaries']:
        if row['period'] == 'all': print(json.dumps(row), flush=True)


if __name__ == '__main__': main()
