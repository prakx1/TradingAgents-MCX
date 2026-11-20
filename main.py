#!/usr/bin/env python3
"""TradingAgents-MCX CLI.

  python main.py                          # full pipeline on CRUDEOIL
  python main.py --rounds 3 --notify
  python main.py --modules technical,news # subset of analysts
  python main.py signals                  # signal outcomes in points and R
  python main.py paper                    # virtual wallet and simulated trades
  python main.py chatid                   # discover your Telegram chat id
"""
import argparse
import sys

from tradingagents_mcx.pipeline import run_analysis


def cmd_signals():
    from tradingagents_mcx.config import load_config
    from tradingagents_mcx.signals import SignalBook
    from tradingagents_mcx.strategies import ACTIVE_NAMES, strategy_config
    for name in ACTIVE_NAMES:
        book = SignalBook(strategy_config(load_config(), name))
        print(name, book.stats())
        for row in book.load()[-5:]:
            print(row['id'], row['side'], row['status'], 'entry', row['entry'],
                  'stop', row['stop'], 'target', row['target'], 'R:R', row['reward_risk'])


def cmd_paper():
    from tradingagents_mcx.config import load_config
    from tradingagents_mcx.paper import PaperBook
    book=PaperBook(load_config())
    print('PAPER ONLY',book.account())
    for r in book.all_trades()[:10]:
        print(r['trade_id'],r['side'],r['status'],'entry',r['entry'],'stop',r['stop'],
              'target',r['target'],'lots',r['lots'],'P&L',r.get('pnl_inr'),'open P&L',r.get('unrealized_inr'))


def cmd_chatid():
    from tradingagents_mcx.config import load_config
    from tradingagents_mcx.notify import discover_chat_id
    cid = discover_chat_id(load_config())
    if cid:
        print(f"Your chat_id: {cid}\nPut it in config.json under telegram.chat_id")
    else:
        print("No chat found. Set telegram.bot_token in config.json, send your bot any message, retry.")


def cmd_provider():
    """python main.py provider [name] — show or switch the active LLM provider."""
    import json
    from pathlib import Path
    cfg_p = Path(__file__).parent / "config.json"
    cfg = json.loads(cfg_p.read_text())
    provs = cfg["llm"].get("providers") or {}
    if len(sys.argv) > 2:
        name = sys.argv[2]
        if name not in provs:
            print(f"unknown provider '{name}' — available: {', '.join(provs)}")
            return
        cfg["llm"]["active_provider"] = name
        cfg_p.write_text(json.dumps(cfg, indent=2))
        print(f"switched → {name} ({provs[name]['base_url']}) — daemons pick it up on their next run")
    else:
        active = cfg["llm"].get("active_provider")
        for n, p in provs.items():
            print(f"{'→' if n == active else ' '} {n:12s} {p['base_url']}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("signals", "paper", "chatid", "provider"):
        return {"signals": cmd_signals, "paper": cmd_paper, "chatid": cmd_chatid, "provider": cmd_provider}[sys.argv[1]]()
    p = argparse.ArgumentParser(description="MCX intraday signals: entry, stop, target, R:R")
    p.add_argument("--strategy", choices=["intraday"], default="intraday")
    p.add_argument("--watch", action="store_true", help="analysis only; never issue a signal")
    p.add_argument("--symbol", "-s", default="CRUDEOIL")
    p.add_argument("--rounds", "-r", type=int, default=None)
    p.add_argument("--modules", "-m", default=None)
    p.add_argument("--notify", action="store_true", help="explicitly push results to configured Telegram")
    p.add_argument("--deep", action="store_true")
    args = p.parse_args()
    from tradingagents_mcx.config import load_config
    from tradingagents_mcx.strategies import enabled_strategies
    names = ['intraday'] if args.watch else enabled_strategies(load_config())
    if not names:
        print('Intraday signals are disabled in settings. Enable intraday to run a signal council.')
        return
    failures = []
    for name in names:
        try:
            r = run_analysis(args.symbol, args.modules.split(",") if args.modules else None,
                             args.rounds, notify=args.notify, deep=args.deep, watch=args.watch,
                             strategy=name)
            signal = r.get("signal") or {"status": "view only"}
            print(f"\n{name.upper()}: {r['cio']['final_decision']} | {signal['status']}")
            if signal.get("entry"):
                print(f"Entry {signal['entry']} | Stop {signal['stop']} | Target {signal['target']} "
                      f"| Reward/risk {signal['reward_risk']:.2f}:1 | Valid until {signal['valid_until']} IST")
            elif signal.get("reason"):
                print(signal['reason'])
            print(f"Report: {r.get('report_path')}")
        except Exception as e:
            failures.append(name)
            print(f"{name}: {e}", file=sys.stderr)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
