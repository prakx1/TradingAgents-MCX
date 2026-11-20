#!/usr/bin/env python3
"""Replay fixed entry alternatives; writes a NEW research artifact only."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

from tradingagents_mcx.config import load_config
from tradingagents_mcx.economics import configured_fee_points
from tradingagents_mcx.entry_research import read_ticks,replay,PROTOCOL,protocol_hash
from tradingagents_mcx.strategies import now_ist,strategy_config
from tradingagents_mcx.paper import paper_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',required=True,type=dt.date.fromisoformat)
    parser.add_argument('--end',required=True,type=dt.date.fromisoformat)
    parser.add_argument('--split',required=True,type=dt.date.fromisoformat,help='Calendar comparison boundary, not an untouched holdout')
    parser.add_argument('--as-of',type=dt.datetime.fromisoformat)
    parser.add_argument('--ticks-dir',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();cfg=load_config();scfg=strategy_config(cfg,'intraday');now=now_ist()
    as_of=min(args.as_of or now,now)
    if args.end<args.start or args.end>as_of.date():parser.error('Require start <= end <= as-of date')
    directory=args.ticks_dir or Path(cfg['paths']['cache_dir'])/'live'
    output=args.output or Path(scfg['paths']['project_root'])/'research'/f'entries_{now:%Y%m%d_%H%M%S}.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation keeps every run and freezes the protocol before scoring.
    protocol=output.with_suffix('.protocol.json')
    code_root=Path(__file__).resolve().parent
    code_hashes={name:hashlib.sha256((code_root/name).read_bytes()).hexdigest() for name in
        ('tradingagents_mcx/entry_research.py','tradingagents_mcx/entries.py','tradingagents_mcx/indicators.py','research_entries.py')}
    frozen=dict(protocol=PROTOCOL,sha256=protocol_hash(),created_at=str(now),start=str(args.start),end=str(args.end),
        split=str(args.split),as_of=str(as_of),fee_points=configured_fee_points(cfg),strategy_policy=scfg['strategy'],
        daily_entry_limit=paper_settings(cfg)['max_new_trades_per_day'],code_sha256=code_hashes)
    with protocol.open('x') as f:json.dump(frozen,f,indent=2)
    if output.exists():raise FileExistsError(output)
    ticks,meta=read_ticks(directory,cfg['analysis'].get('symbol','CRUDEOIL'),as_of)
    print(f'Protocol frozen: {protocol}; {len(ticks):,} observations',flush=True)
    result=replay(ticks,scfg['strategy'],configured_fee_points(cfg),args.start,args.end,as_of,args.split,
                  paper_settings(cfg)['max_new_trades_per_day'])
    result.update(input=meta,generated_at=str(now_ist()),purpose='research_only',live_rules_changed=False,code_sha256=code_hashes)
    with output.open('x') as f:json.dump(result,f,indent=2,allow_nan=False)
    print(output,flush=True)
    for row in result['summaries']:
        if row['period']=='all':print(json.dumps(row),flush=True)


if __name__=='__main__':main()
