# Third-party notices

## TradingAgents_for_Futures

The analyst prompts, decision-chain design and related indicator/configuration concepts were translated and adapted from [TradingAgents_for_Futures](https://github.com/haoge10241024/TradingAgents_for_Futures). The source header comments retain their original module lineage. This MCX adaptation adds intraday lifecycle handling, market-data integration, simulation, research tools and a dashboard.

Upstream copyright: **Copyright (c) 2025 商品期货AI分析系统**.
Upstream license: **MIT**, preserved verbatim in [third_party/TradingAgents_for_Futures.LICENSE](third_party/TradingAgents_for_Futures.LICENSE), including its original disclaimer.

## Optional Kronos experiments

`tradingagents_mcx/forecast_models.py` can load a separately installed [Kronos](https://github.com/shiyu-coder/Kronos) checkout and model weights. No upstream Kronos source or weights are distributed in this repository. Review the upstream code and model licenses before installing them. The integration is an offline research tool; it is not a validated live trading strategy.

## Dependencies and data

Python dependencies are installed separately and retain their respective licenses. Data-provider subscriptions, access conditions and redistribution restrictions are separate from this project's source-code license. This repository includes no collected market data, articles, account records, model responses or raw trading ledgers. The README reports a dated aggregate summary of simulated results.
