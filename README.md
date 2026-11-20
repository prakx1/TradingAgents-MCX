# TradingAgents-MCX

An intraday MCX crude-oil research desk with an AI council, market-data feeds, entry/stop/target signals, automatic simulated trades and a Streamlit dashboard.

**This application does not place broker orders.** It uses virtual balances and observed prices to simulate trades. It is experimental software for research; it has not demonstrated a reliable profitable edge.

## What it does

- Combines technical, news and basis analysis with bull/bear debate, trader proposals, risk review and a final council decision.
- Evaluates continuation and fixed regime-rule candidates. Council approval is required for automatic entries; adding candidates does not guarantee trades.
- Supports immediate entries and conditional pullback, breakout and retest plans with explicit expiry.
- Monitors stop-losses, targets, reversals and same-session square-off independently of the AI review.
- Shows active simulated trades, complete local trade/signal history, decision reasons, market observations, feed health and estimated API usage costs.
- Replays eligible rejected plans under **What if?**, keeping hypothetical outcomes separate from recorded trades.
- Provides offline entry, regime and optional forecasting experiments. Research results never automatically enable a strategy.

No personal history or precomputed results are included. A fresh installation starts with an empty local record.

## Requirements

- Python 3.10 or newer and a virtual environment.
- macOS or Linux. Service/process locking currently uses Unix APIs.
- An API key for an OpenAI-compatible LLM provider. Reviews consume paid API calls according to the provider you configure.
- A market-data source. Kite market-data access is optional; availability and subscription requirements depend on your provider.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
```

Set `llm.base_url`, `llm.model` and `llm.reasoning_model` in the local `config.json` for your chosen provider. The example uses DeepSeek model names, but its API key and provider profiles are deliberately empty.

Supply credentials through environment variables or your ignored local configuration:

```bash
export LLM_API_KEY='replace-with-your-own-key'
# Optional overrides:
# export LLM_BASE_URL='https://your-provider.example/v1'
# export LLM_MODEL='your-model'
# export LLM_REASONING_MODEL='your-reasoning-model'
```

The application does not load `.env` automatically. If you use one, load it in your shell and keep it private. Never commit actual values from a local configuration.

## Start and stop

```bash
.venv/bin/python desk.py          # start/restart all services
.venv/bin/python desk.py --stop   # stop services, preserve local history
```

Open **http://localhost:8503**. The public-source defaults bind the dashboard to loopback. The launcher starts the data collector, Kite login/feed, news sentinel, scheduler, independent position monitor, maritime watcher and dashboard. Startup enables scheduled API calls and automatic simulation according to your config. Telegram notifications require explicit opt-in; scheduled runs do not send them automatically.

Useful commands:

```bash
.venv/bin/python main.py --watch  # analysis preview; no signal entry
.venv/bin/python main.py          # one intraday council review
.venv/bin/python main.py signals  # local signal outcomes
.venv/bin/python main.py paper    # local simulated-account summary
.venv/bin/python review.py        # saved learning review
.venv/bin/python syscheck.py      # local health diagnostics
```

## Optional Kite market data

Kite integration uses a fixed allowlist of instruments, quote and historical-data endpoints. Order and portfolio endpoints are rejected. The browser login exchanges a temporary request token for a local data session.

1. Create your own Kite application and set its Redirect URL to `http://127.0.0.1:8000`.
2. Create the ignored credentials file:

   ```bash
   mkdir -p .secrets
   chmod 700 .secrets
   cp examples/kite_credentials.example.json .secrets/kite_credentials.json
   chmod 600 .secrets/kite_credentials.json
   ```

3. Fill in your API key and API secret locally. `user_id` is optional and restricts login to that account if provided.
4. Start the desk, then use **Log in to Kite** in the dashboard or open `http://127.0.0.1:8000/login` on the same computer.

The dashboard displays session expiry separately from feed freshness. An active login does not guarantee fresh data. When a session expires, complete the provider's login again; the app does not automate account authentication. Without usable Kite data, configured fallback sources may be used and are identified in the UI. Unofficial feeds can be delayed or unavailable.

## Rules and limitations

The supplied example enables intraday simulation, disables new long-term signals, reviews every 30 minutes, stops new entries at 23:00 IST and squares off at 23:15 IST. Session/holiday settings need to match your intended market calendar. Requirements for completed candles, current-contract observations and warmup can prevent an entry on a new installation.

Signals require council agreement, a fresh observed price, a valid session and contract, explicit directional entry/stop/target geometry and a minimum reward/risk ratio. Conditional plans must confirm before their original expiry. A WAIT decision, an untriggered plan and an executed trade are different records; no trade is forced merely because a review ran.

The default signal floor is **1.5:1 before costs**. Regime-rule entries additionally enforce their saved minimum after fee and slippage assumptions. Other entry paths do not uniformly enforce that net floor. Review those economics before interpreting a nominal reward/risk ratio as a profitable setup.

Simulation uses configured assumptions for balances, lot sizes, margin and fees; these are not current broker quotes. Council-trade fills use observed prices without modeled execution slippage, and mini-sized positions may use the main contract as a price proxy. Stops can gap, polling can miss price extremes, and missing observations can delay exits. Recorded P&L excludes LLM spending. API costs are estimates from a model-price table and may be absent or outdated for your chosen model.

Historical replay cannot reproduce unavailable tick data, broker execution, AI latency or decisions that lack complete numerical plans. Incomplete counterfactuals are shown as untestable rather than given invented fills. A good replay result does not establish future profitability.

## Data and privacy

Credentials and sessions belong in ignored local configuration and `.secrets/`. Runtime records are written under ignored `cache/`, `reports/`, `signals/`, `ledger/`, `events/`, `memory/` and `research/` directories. These records can contain account identifiers, model responses, simulated activity, costs and local paths; review them before sharing. The public export includes source code, synthetic tests and empty credential examples only.

Configured LLM providers receive the analysis prompts: market observations, news context, candidate plans and selected simulated-trade feedback. Provider fallback routes can send prompts to more than one configured endpoint. Broker credentials are used by the data adapters for authentication and are not intended as model inputs. Enabling optional Telegram notifications sends the configured notification payload to Telegram. Streamlit usage-stat collection is disabled in the supplied settings.

## Tests

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

The test suite uses synthetic fixtures, temporary directories and mocked upstream services. It covers signal and setup lifecycles, stale-data handling, simulation, archive separation, quote/session checks, counterfactuals, scheduling and dashboard views. A local loopback HTTP server is used in the login-flow test; no actual broker account or API key is needed.

## Research tools

`research_entries.py`, `research_regimes.py` and `research_forecasts.py` read your locally recorded observations and write new research outputs. Use their `--help` options for date ranges and cutoff controls. They do not place orders or rewrite the trade ledger. Optional forecasting dependencies are listed separately in `requirements-research.txt`; no external model code or weights are bundled. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before installing third-party research models.

## Project structure

- `desk.py`: service lifecycle and dashboard readiness.
- `main.py`, `scheduler.py`, `news_sentinel.py`: council reviews and scheduling.
- `position_guard.py`: signal/setup and simulated-position monitoring.
- `kite_login.py`, `kite_feed.py`, `live_collector.py`: authentication and market observations.
- `live_dashboard.py`: current trades, decisions, history and health.
- `tradingagents_mcx/`: strategy, data, council, storage and replay modules.
- `tests/`: synthetic regression tests.
- [DESIGN.md](DESIGN.md): implementation and lifecycle notes.

## License and attribution

MIT. The analyst prompts and decision-chain concepts were translated and adapted from [TradingAgents_for_Futures](https://github.com/haoge10241024/TradingAgents_for_Futures). Its original copyright and license are retained. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
