#!/usr/bin/env python3
"""Review intraday outcomes and preserved historical learning, without capital."""
from pathlib import Path
from tradingagents_mcx.config import load_config
from tradingagents_mcx.signals import SignalBook
from tradingagents_mcx.strategies import NAMES, now_ist, strategy_config


def main():
    cfg=load_config()
    lines=[f'# Signal review — {now_ist():%Y-%m-%d}', '',
           'Outcomes are observed price changes normalized by each signal’s initial stop distance. No account returns are estimated.', '']
    for name in ('intraday',):
        book=SignalBook(strategy_config(cfg,name)); s=book.stats()
        lines.extend([f'## {name}', '', f"Closed: {s['closed']} · active: {s['active']} · win rate: {s['win_rate']}%",
                      f"Average outcome: {s['mean_r']}R · total: {s['total_r']:+.2f}R · points: {s['points']:+.1f}",
                      f"Overdue exits awaiting data: {s['exit_pending']}", '', 'Recent completed signals:', '', book.context(), ''])
    from tradingagents_mcx.history import learning_context
    lines += ['## Preserved historical evidence', '', learning_context(cfg)['context'], '']
    out=Path(cfg['paths']['reports_dir'])/f'intraday_review_{now_ist():%Y%m%d_%H%M%S}.md'
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text('\n'.join(lines))
    print(out)
    return out


if __name__=='__main__':main()
