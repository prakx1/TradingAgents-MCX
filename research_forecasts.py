#!/usr/bin/env python3
"""Compare frozen forecasts with simple controls; never issue trades or orders."""
import argparse
from dataclasses import asdict
import datetime as dt
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import time

import pandas as pd

from tradingagents_mcx.config import load_config
from tradingagents_mcx.economics import configured_fee_points
from tradingagents_mcx.entry_research import read_ticks
from tradingagents_mcx.forecast_models import KronosForecaster
from tradingagents_mcx.forecast_research import (
    ForecastProtocol, LIMITATIONS, forecast_window, score_forecasts, select_windows, summarize,
)
from tradingagents_mcx.strategies import now_ist, strategy_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=dt.date.fromisoformat, required=True)
    parser.add_argument('--end', type=dt.date.fromisoformat, required=True)
    parser.add_argument('--as-of', type=dt.datetime.fromisoformat)
    parser.add_argument('--ticks-dir', type=Path)
    parser.add_argument('--model', choices=['baselines', 'kronos'], default='baselines')
    parser.add_argument('--kronos-source', type=Path, default=Path('.research-tools/Kronos'))
    parser.add_argument('--device', choices=['cpu', 'mps', 'cuda'], default='cpu')
    parser.add_argument('--slippage', type=float, default=1.0, help='Assumed adverse price points EACH way')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    now = now_ist()
    as_of = pd.Timestamp(args.as_of or now)
    if as_of.tzinfo:
        as_of = as_of.tz_convert('Asia/Kolkata').tz_localize(None)
    as_of = min(as_of.to_pydatetime(), now)
    if not args.start <= args.end <= as_of.date():
        parser.error('Require start <= end <= as-of date')
    cfg = load_config()
    policy = strategy_config(cfg, 'intraday')['strategy']
    protocol = ForecastProtocol(fee_points=configured_fee_points(cfg), slippage_points_each_way=args.slippage)
    root = Path(__file__).resolve().parent
    output = args.output_dir or root / 'research' / f'forecasts_{now:%Y%m%d_%H%M%S}'
    # Exclusive folder: earlier evidence is never overwritten, including failed runs.
    output.mkdir(parents=True, exist_ok=False)
    ticks, manifest = read_ticks(args.ticks_dir or Path(cfg['paths']['cache_dir']) / 'live',
                                 cfg['analysis'].get('symbol', 'CRUDEOIL'), as_of)
    windows, skipped = select_windows(ticks, args.start, args.end, as_of, policy, protocol)
    print(f'{len(windows)} eligible windows; exclusions: {skipped}', flush=True)
    if not windows:
        parser.error(f'No eligible windows. Research folder retained: {output}')
    kronos = KronosForecaster(args.kronos_source, device=args.device, seed=protocol.seed) if args.model == 'kronos' else None
    code = ['research_forecasts.py', 'tradingagents_mcx/forecast_models.py',
            'tradingagents_mcx/forecast_research.py', 'tradingagents_mcx/entry_research.py',
            'tradingagents_mcx/strategies.py', 'tradingagents_mcx/economics.py']
    versions = {name: importlib.metadata.version(name) for name in ['numpy', 'pandas']}
    if kronos:
        versions.update({name: importlib.metadata.version(name) for name in
                         ['torch', 'huggingface_hub', 'safetensors', 'einops']})
    frozen = dict(created_at=str(now), start=str(args.start), end=str(args.end), as_of=str(as_of),
                  protocol=asdict(protocol), policy=policy, input=manifest,
                  model_metadata=kronos.metadata if kronos else {}, packages=versions,
                  python=platform.python_version(), machine=platform.machine(),
                  models=['persistence', 'momentum', 'mean_reversion'] + (['kronos_small'] if kronos else []),
                  code_sha256={name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in code},
                  selected_windows=[dict(cutoff=str(w['cutoff']), contract=w['contract'],
                                         context_sha256=w['context_sha256']) for w in windows],
                  skipped_windows=skipped, limitations=LIMITATIONS)
    with (output / 'protocol.json').open('x') as f:
        json.dump(frozen, f, indent=2, allow_nan=False)
    records = []
    with (output / 'predictions.jsonl').open('x') as f:
        for i, window in enumerate(windows):
            began = time.monotonic()
            record = forecast_window(window, protocol, kronos)
            record['inference_seconds'] = round(time.monotonic() - began, 4)
            f.write(json.dumps(record, allow_nan=False) + '\n'); f.flush()
            records.append(record)
            if (i + 1) % 10 == 0 or i == len(windows) - 1:
                print(f'Forecasts saved: {i + 1}/{len(windows)}', flush=True)
    rows = score_forecasts(records, ticks, as_of, protocol)
    result = dict(purpose='forecast_research_only', live_rules_changed=False, generated_at=str(now_ist()),
                  **{k: frozen[k] for k in ['start', 'end', 'as_of', 'models', 'limitations']},
                  protocol_sha256=hashlib.sha256((output / 'protocol.json').read_bytes()).hexdigest(),
                  predictions_sha256=hashlib.sha256((output / 'predictions.jsonl').read_bytes()).hexdigest(),
                  summaries=summarize(rows),
                  by_day={day: summarize([r for r in rows if r['cutoff'][:10] == day])
                          for day in sorted({r['cutoff'][:10] for r in rows})},
                  scored_forecasts=rows)
    with (output / 'results.json').open('x') as f:
        json.dump(result, f, indent=2, allow_nan=False)
    print(output, flush=True)
    for summary in result['summaries']:
        print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
