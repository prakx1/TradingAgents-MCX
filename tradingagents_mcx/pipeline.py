"""End-to-end pipeline: data -> 6 analysts (parallel) -> debate -> trader -> risk -> CIO.

Port of FuturesAnalysisIntegrator + OptimizedTradingAgentsSystem. The original's
analyst fan-out claimed to be parallel but awaited sequentially; here the six
analysts genuinely run in parallel threads.
"""
from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from . import analysts as an
from . import data_providers as dp
from . import decision
from .config import load_config
from .contracts import contract, fair_value_inr
from .indicators import technical_snapshot
from .llm import LLMClient
from .strategies import strategy_config, strategy_instruction, intraday_data, intraday_evidence, now_ist


# ------------------------------------------------------- derived-context math

def _crude_stocks_entry(eia: dict | None) -> dict | None:
    if not eia:
        return None
    return eia.get("us_crude_stocks_ex_spr_kbbl") or eia.get("us_crude_stocks_ex_spr")


def inventory_cycle(eia: dict | None, wti: pd.DataFrame) -> dict:
    """4-quadrant inventory cycle (active/passive restock/destock), ported from the
    original's +-0.3 sigma classifier, on EIA weekly US crude stocks."""
    stocks = _crude_stocks_entry(eia)
    if not stocks:
        return {"stage": "unknown", "meaning": "EIA data unavailable"}
    series = pd.Series(stocks.get("recent_series", []))
    if len(series) < 8:
        # keyless WPSR path: only latest week -> coarse single-week staging
        wow = stocks.get("week_change", 0) or 0
        price_chg = float(wti["close"].iloc[-1] / wti["close"].iloc[-21] - 1) if len(wti) > 21 else 0.0
        inv_up, price_up = wow > 0, price_chg > 0
        stage = ("active restocking" if inv_up and price_up else
                 "passive restocking" if inv_up else
                 "passive destocking" if price_up else "active destocking")
        return {"stage": f"{stage} (coarse, single-week read)",
                "meaning": "based on one week of keyless WPSR data; add a free EIA API key for full cycle staging",
                "inv_trend": f"{wow:+.1f} million bbl WoW", "price_trend": f"{price_chg:+.1%} over ~20 sessions",
                "correlation": None, "reflexivity": "unavailable without EIA history"}
    chg_4w = float(series.iloc[-1] - series.iloc[-5])
    sigma = float(series.diff(4).std())
    price_chg = float(wti["close"].iloc[-1] / wti["close"].iloc[-21] - 1) if len(wti) > 21 else 0.0
    inv_up, inv_down = chg_4w > 0.3 * sigma, chg_4w < -0.3 * sigma
    price_up = price_chg > 0
    if inv_up and price_up:
        stage, meaning = "active restocking", "demand expectations improving, market actively building stocks"
    elif inv_up:
        stage, meaning = "passive restocking", "weak demand causing involuntary inventory build — bearish"
    elif inv_down and price_up:
        stage, meaning = "passive destocking", "supply tightness draining stocks while price rises — bullish"
    elif inv_down:
        stage, meaning = "active destocking", "pessimistic expectations, market actively de-stocking"
    else:
        stage, meaning = "balanced", "supply and demand roughly balanced"
    weekly_inv = series.diff().dropna().tail(12)
    weekly_px = wti["close"].resample("W-FRI").last().pct_change().dropna().tail(len(weekly_inv))
    corr = None
    if len(weekly_inv) >= 8 and len(weekly_px) >= 8:
        n = min(len(weekly_inv), len(weekly_px))
        corr = round(float(pd.Series(weekly_inv.values[-n:]).corr(pd.Series(weekly_px.values[-n:]))), 2)
    reflexivity = ("classic negative correlation — traditional supply-demand logic holds" if corr is not None and corr < -0.3
                   else "positive reflexivity — price and stocks moving together, possibly speculation-driven" if corr is not None and corr > 0.3
                   else "weak/unclear price-inventory link")
    return {"stage": stage, "meaning": meaning,
            "inv_trend": f"{chg_4w:+,.0f} kbbl over 4 weeks (sigma of 4w changes: {sigma:,.0f})",
            "price_trend": f"{price_chg:+.1%} over ~20 sessions",
            "correlation": corr, "reflexivity": reflexivity}


def three_region(eia: dict | None) -> str:
    """Inventory percentile -> region A/B/C framework from the basis system."""
    stocks = _crude_stocks_entry(eia)
    if not stocks:
        return "unknown (no EIA data)"
    if "percentile_5y" not in stocks:
        return (f"percentile unavailable (keyless WPSR data: {stocks['latest']} million bbl, "
                f"{stocks['week_change']:+} WoW, {stocks.get('yoy_change', 'n/a')} YoY) — add a free EIA key for region classification")
    p = stocks["percentile_5y"]
    if p <= 20:
        return f"Region A — extremely low stocks ({p}th percentile): scarce prompt supply, structurally supports backwardation/premium"
    if p >= 80:
        return f"Region C — inventory suppression ({p}th percentile): ample supply pressures the front of the curve"
    return f"Region B — normal stocks ({p}th percentile): balanced, basis fluctuates moderately"


def build_basis_data(wti: pd.DataFrame, fx: pd.DataFrame, brent: pd.DataFrame,
                     eia: dict | None, mcx_curve: pd.DataFrame | None, curve_m: dict) -> dict:
    fx_last = float(fx["usdinr"].iloc[-1])
    wti_last = float(wti["close"].iloc[-1])
    fair = round(fair_value_inr(wti_last, fx_last), 1)
    fx_20d = round(100 * (fx_last / float(fx["usdinr"].iloc[-21]) - 1), 2) if len(fx) > 21 else None
    wti_20d = round(100 * (wti_last / float(wti["close"].iloc[-21]) - 1), 2) if len(wti) > 21 else None
    parity = {"wti_usd": wti_last, "usdinr": fx_last, "fair_value_inr_bbl": fair,
              "wti_20d_change_pct": wti_20d, "usdinr_20d_change_pct": fx_20d,
              "decomposition_note": "MCX fair-value 20d change ~= WTI change + USDINR change; the rupee leg is a basis driver unique to MCX"}
    if mcx_curve is not None and len(mcx_curve):
        try:
            front = float(mcx_curve.iloc[0].get("close") or mcx_curve.iloc[0].get("settle"))
            parity["mcx_front_settle_inr"] = front
            parity["parity_basis_inr"] = round(front - fair, 1)
            parity["parity_basis_pct"] = round(100 * (front - fair) / fair, 2)
        except Exception:
            pass
    else:
        parity["mcx_front_settle_inr"] = "unavailable this run (MCX endpoint blocked) — analysis uses fair value"

    monthly = wti["close"].resample("ME").last().pct_change().dropna()
    by_month = monthly.groupby(monthly.index.month).mean().round(4).to_dict()
    seasonality = {"avg_monthly_wti_return_by_month": {int(k): f"{v:+.2%}" for k, v in by_month.items()},
                   "current_month": dt.date.today().month,
                   "note": "crude seasonality: US driving season (Jun-Aug) demand, winter heating, and refinery turnaround troughs (Mar/Oct)"}
    inv_rel = {"region_classification": three_region(eia),
               "curve_structure": curve_m.get("structure", "n/a")}
    spread = None
    if len(brent) and len(wti):
        joined = pd.concat([wti["close"], brent["close"]], axis=1, keys=["wti", "brent"]).dropna().tail(20)
        if len(joined):
            spread_now = round(float(joined["brent"].iloc[-1] - joined["wti"].iloc[-1]), 2)
            spread = {"brent_wti_spread_usd": spread_now,
                      "spread_20d_avg": round(float((joined["brent"] - joined["wti"]).mean()), 2),
                      "note": "wide Brent-WTI = strong US exports / relatively weak US grade; India imports Brent-linked barrels while MCX settles on WTI"}
    return {"parity": parity, "seasonality": seasonality,
            "inventory_relation": inv_rel, "spatial": spread or {"note": "Brent data unavailable"}}


def build_price_block(snap: dict, fx_last: float, mcx_curve: pd.DataFrame | None) -> str:
    fair = round(snap["price"] * fx_last, 1)
    to_inr = lambda v: round(v * fx_last)
    lines = [
        "[REAL-TIME PRICE DATA]",
        f"WTI front month: ${snap['price']:.2f}/bbl | USDINR: {fx_last:.3f} | MCX fair value: Rs {fair}/bbl",
        f"5-day change {snap['change_5d_pct']}% | 20-day change {snap['change_20d_pct']}%",
        f"Supports (WTI$ -> ~MCX Rs): {[(s, to_inr(s)) for s in snap['supports']]}",
        f"Resistances (WTI$ -> ~MCX Rs): {[(r, to_inr(r)) for r in snap['resistances']]}",
        f"ATR14: ${snap['atr14']} (~Rs {to_inr(snap['atr14'])}/bbl); 2.5xATR stop distance ~ Rs {to_inr(2.5 * snap['atr14'])}",
    ]
    if mcx_curve is not None and len(mcx_curve):
        lines.append(f"MCX official settlements (latest bhavcopy): {mcx_curve.head(3).to_dict('records')}")
    return "\n".join(lines)


def _mcx_quotes_safe(symbol: str, cache_dir: str):
    try:
        from .kite_data import cached_quotes
        kite = cached_quotes(cache_dir, symbol)
        if kite is not None and not kite.empty:
            return kite
        from .moneycontrol import get_mcx_quotes
        return get_mcx_quotes(symbol, cache_dir)
    except Exception:
        return None


# --------------------------------------------------------------- orchestrator

def run_analysis(symbol: str = "CRUDEOIL", modules: list[str] | None = None,
                 debate_rounds: int | None = None, progress=print, notify: bool = False,
                 deep: bool = False, watch: bool = False, strategy: str = "intraday") -> dict:
    """Serialize Council runs with an OS lock; a failed run never removes another run's lock."""
    import fcntl
    import os
    import time
    if strategy != 'intraday':
        raise ValueError('Only intraday analysis is enabled. Older long-term reports remain in History & learning.')
    cfg = strategy_config(load_config(), strategy)
    root = Path(cfg["paths"]["project_root"])
    metadata = root / ".council_running"
    with open(root / ".council.lock", "a") as lockfile:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Council already convening — skipping duplicate run")
        # Respect a pre-upgrade process which used only the pid marker.
        if metadata.exists():
            try:
                old_pid = int(metadata.read_text().split(",")[0])
                if old_pid != os.getpid():
                    try:
                        os.kill(old_pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise RuntimeError("Council already convening — previous process still running")
            except (ValueError, OSError):
                pass
        metadata.write_text(f"{os.getpid()},{time.time()}")
        tracker = None
        try:
            from .runs import RunTracker
            tracker = RunTracker(cfg, strategy, watch)
            def report_progress(message):
                tracker.progress(message)
                progress(message)
            result = _run_analysis_locked(symbol, modules, debate_rounds, progress=report_progress,
                                         notify=notify, deep=deep, watch=watch, strategy=strategy)
            tracker.finish(result=result)
            return result
        except Exception as exc:
            if tracker:
                tracker.finish(error=exc)
            raise
        finally:
            if metadata.exists() and metadata.read_text().split(",")[0] == str(os.getpid()):
                metadata.unlink()


def _run_analysis_locked(symbol: str = "CRUDEOIL", modules: list[str] | None = None,
                         debate_rounds: int | None = None,
                         progress=print, notify: bool = False, deep: bool = False, watch: bool = False,
                 strategy: str = "intraday") -> dict:
    from .angelone import AngelOne
    from .shoonya import Shoonya

    cfg = strategy_config(load_config(), strategy)
    cfg["analysis"]["symbol"] = symbol
    spec = contract(symbol)
    modules = modules or cfg["analysis"]["modules"]
    rounds = debate_rounds or cfg["analysis"]["debate_rounds"]
    horizon = strategy_instruction(cfg)
    progress(f"[{strategy}] signals only; minimum reward/risk {cfg['strategy']['min_reward_risk']}:1")
    deep_model = ((cfg["llm"].get("routes") or {}).get("deep") or (cfg["llm"].get("models") or {}).get("deep")) if deep else None
    llm = LLMClient(cfg, override_model=deep_model)
    llm.progress = progress
    if deep_model:
        progress(f"[deep run] every role on {deep_model}")
    today = now_ist().date().isoformat()
    keys = cfg["data_keys"]
    brokers = [AngelOne(cfg, cfg["paths"]["cache_dir"]), Shoonya(cfg, cfg["paths"]["cache_dir"])]
    broker = next((b for b in brokers if b.configured), None)

    progress("[1/4] Fetching market data (WTI, USDINR, Brent, curve, COT, EIA, news, MCX)...")
    days = cfg["analysis"]["lookback_days"]
    # NOT a `with` block: __exit__ would wait on every future, so one hung
    # source (no-timeout socket, drip-feed server) stalls the Council for hours.
    # Instead: shared 5-min deadline, abandon what misses it, run on what we have.
    import time as _time
    ex = ThreadPoolExecutor(max_workers=10)
    f_wti = ex.submit(dp.get_wti_history, days)
    f_fx = ex.submit(dp.get_usdinr_history, days)
    f_brent = ex.submit(dp.get_brent_history, 120)
    f_curve = ex.submit(dp.get_wti_curve, 8)
    f_cot = ex.submit(dp.get_cot_wti)
    f_eia = ex.submit(dp.get_eia_fundamentals, keys["eia_api_key"])
    f_news = ex.submit(dp.get_news, cfg["analysis"]["news_days_back"], cfg["analysis"]["max_news"],
                       keys["serper_key"], keys["newsapi_key"], keys["gnews_key"])
    f_mcx = ex.submit(dp.get_mcx_bhavcopy)
    f_mcq = ex.submit(_mcx_quotes_safe, symbol, cfg["paths"]["cache_dir"])
    f_angel = ex.submit(broker.get_candles, symbol, 300) if broker else None
    f_av = ex.submit(dp.get_av_sentiment, keys.get("alpha_vantage_key", "")) \
        if keys.get("alpha_vantage_key") else None
    f_poi = ex.submit(dp.get_mcx_participant_oi)

    _deadline = _time.time() + 300

    def _res(f, what, essential=False):
        if f is None:
            return None
        try:
            return f.result(timeout=max(5, _deadline - _time.time()))
        except Exception as e:
            if essential:
                raise RuntimeError(f"essential fetch '{what}' failed: {e}") from e
            progress(f"    ⚠ {what} fetch failed/timed out — continuing without it")
            return None

    wti, fx = _res(f_wti, "wti", essential=True), _res(f_fx, "usdinr", essential=True)
    brent = _res(f_brent, "brent")
    curve, eia, news = _res(f_curve, "curve"), _res(f_eia, "eia"), _res(f_news, "news") or []
    cot_df = _res(f_cot, "cot")
    # real MCX quotes: Moneycontrol live feed first, official bhavcopy as fallback
    mcx_curve = _res(f_mcq, "mcx_quotes")
    if mcx_curve is None:
        bhav = _res(f_mcx, "mcx_bhavcopy")
        mcx_curve = dp.mcx_crude_from_bhavcopy(bhav) if bhav is not None else None
    mcx_candles = _res(f_angel, "broker_candles")
    av_sentiment = _res(f_av, "av_sentiment")
    mcx_participant_oi = _res(f_poi, "participant_oi")
    ex.shutdown(wait=False)  # abandon any straggler thread
    # timed-out optional frames come back None — downstream expects empty frames
    curve = curve if curve is not None else pd.DataFrame()
    brent = brent if brent is not None else pd.DataFrame()

    # Real MCX series drives technicals when the broker feed has enough history;
    # WTI snapshot always anchors fair value / parity / the trader's price block.
    wti_snap = technical_snapshot(wti)
    use_mcx = mcx_candles is not None and len(mcx_candles) >= 80
    snap = technical_snapshot(mcx_candles) if use_mcx else wti_snap
    tech_label = (f"real MCX {mcx_candles.attrs.get('contract', 'CRUDEOIL')} daily candles "
                  f"({type(broker).__name__})"
                  if use_mcx else "NYMEX WTI daily data (MCX settles on WTI x USDINR)")
    intraday_bars, intraday_note = (intraday_data(cfg, symbol) if strategy == "intraday" else (pd.DataFrame(), ""))
    intraday_ready = strategy != "intraday" or len(intraday_bars) >= 80
    if strategy == "intraday" and intraday_ready:
        snap = technical_snapshot(intraday_bars)
        tech_label = "real MCX completed 5-minute candles (current contract, sampled collector ticks)"
    evidence = intraday_evidence(intraday_bars, snap, intraday_note, now_ist())
    price_in_inr = use_mcx or (strategy == "intraday" and intraday_ready)
    fx_last = float(fx["usdinr"].iloc[-1])
    curve_m = dp.curve_metrics(curve)
    cot = dp.cot_snapshot(cot_df)
    cycle = inventory_cycle(eia, wti)
    basis_data = build_basis_data(wti, fx, brent, eia, mcx_curve, curve_m)
    atr_inr = round(snap["atr14"] if price_in_inr else snap["atr14"] * fx_last, 1)
    inr_levels = {"fair_value_inr": round(wti_snap["price"] * fx_last, 1),
                  "atr_inr": atr_inr, "usdinr": round(fx_last, 3),
                  "usdinr_trend": f"{basis_data['parity']['usdinr_20d_change_pct']:+}% over 20d"
                                  if basis_data["parity"]["usdinr_20d_change_pct"] is not None else ""}
    price_ctx = {"wti": wti_snap["price"], "chg_20d": wti_snap["change_20d_pct"],
                 "structure": curve_m.get("structure", "n/a"), "usdinr": fx_last}

    # Council intelligence briefing: every dataset reaches the analysts under a card that says what it is,
    # its source lane, trust, freshness and how to weigh it (briefing.py) — nothing is dumped raw.
    _maritime, _radar, _digest = [], [], ""
    try:
        _ev = Path(cfg["paths"]["project_root"]) / "events"
        _uk = json.loads((_ev / "ukmto_latest.json").read_text()).get("items", [])
        _maritime = [m for m in _uk if m.get("published") and (dt.datetime.now() - dt.datetime.strptime(m["published"], "%Y-%m-%d %H:%M:%S")).days <= 3][:8]
        from .radar import Radar
        _radar = Radar(Path(cfg["paths"]["project_root"])).timeline(24)[:12]
        _digest = json.loads((_ev / "news_digest.json").read_text()).get("digest", "")
    except Exception:
        pass
    from .briefing import build_briefing
    from .newsfeeds import SOURCES as _SOURCES
    briefing = build_briefing(news, _maritime, _radar, _digest, _SOURCES)
    off_items = [n["title"] for n in news if n.get("lane") == "official"]
    progress(f"    🧾 briefing: {len(news)} items ({len(off_items)} official) · maritime {len(_maritime)} · radar 24h {len(_radar)} · {len(briefing['text'])} chars")
    live_block = _live_market_block(cfg, symbol, wti_snap["price"], fx_last, inr_levels["fair_value_inr"])
    prompts = {
        "technical": an.technical_prompt(symbol, snap, inr_levels, series_label=tech_label,
                                         price_unit="Rs " if price_in_inr else "$"),
        "fundamentals": an.fundamentals_prompt(symbol, eia, cycle, price_ctx),
        "news": an.news_prompt(symbol, news, today, av_sentiment, briefing=briefing["text"]),
        "positioning": an.positioning_prompt(symbol, cot, mcx_participant_oi),
        "term_structure": an.term_structure_prompt(
            symbol,
            (curve.to_string(index=False) if len(curve) else "WTI curve unavailable")
            + ("\n\n[REAL MCX CRUDEOIL STRIP — live quotes]\n"
               + mcx_curve[[c for c in ("expiry", "close", "settle", "volume", "open_interest") if c in mcx_curve.columns]].to_string(index=False)
               if mcx_curve is not None and len(mcx_curve) else ""),
            curve_m, three_region(eia)),
        "basis": an.basis_prompt(symbol, basis_data, today),
    }
    if strategy == "intraday":
        prompts["technical"] = (prompts["technical"].replace("5-day", "5-bar").replace("20-day", "20-bar")
            .replace("Short term (1-3 sessions)", "Next 15-60 minutes")
            .replace("Medium term (1-2 weeks)", "Remainder of today's session, ending at square-off"))
    prompts = {m: horizon + "\n" + evidence['text'] + "\n" + live_block + "\n\n" + p_ for m, p_ in prompts.items()}
    skipped = []
    if not cot:
        skipped.append("positioning")
    if not len(curve):
        skipped.append("term_structure")
    if not news:
        skipped.append("news")
    active = [m for m in modules if m in prompts and m not in skipped]

    progress(f"[2/4] Running {len(active)} analysts in parallel: {', '.join(active)}"
             + (f" (skipped, no data: {skipped})" if skipped else ""))
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(an.run_analyst, llm, m, prompts[m]): m for m in active}
        completed = {}
        for future in as_completed(futures):
            module = futures[future]
            completed[module] = future.result()
            progress(f'[2/4] Analyst reports ready: {len(completed)}/{len(active)} · {module}')
        analyst_results = [completed[m] for m in active]

    progress(f"[3/4] Bull vs bear debate ({rounds} rounds) ...")
    debate = decision.run_debate(llm, f"{spec['name']} ({strategy})", analyst_results, rounds,
                                 evidence=horizon+'\n'+live_block+'\n'+evidence['text'])

    # Analysis takes minutes. Entry/risk/CIO must see the candles that completed
    # meanwhile, rather than repeatedly asking for confirmation already observed.
    evidence_stages = {'analysts':evidence}
    analyst_revisions = []
    entry_bars=intraday_bars
    def refresh_entry_evidence(stage):
        nonlocal evidence, snap, intraday_note, intraday_ready, live_block, entry_bars
        bars,intraday_note = intraday_data(cfg,symbol,now_ist())
        entry_bars=bars
        intraday_ready = len(bars)>=80
        if intraday_ready:
            snap = technical_snapshot(bars)
            inr_levels['atr_inr'] = round(snap['atr14'],1)
        evidence = intraday_evidence(bars,snap if intraday_ready else {},intraday_note,now_ist())
        evidence_stages[stage] = evidence
        live_block = _live_market_block(cfg,symbol,wti_snap['price'],fx_last,inr_levels['fair_value_inr'])

    quote_precheck={}
    if intraday_ready and not watch:
        from .quote_wait import wait_for_fresh_quote
        _,quote_precheck=wait_for_fresh_quote(cfg,expected_contract=evidence.get('contract_expiry'),
                                            progress=progress,clock=now_ist)
    refresh_entry_evidence('trader')
    initial_bars = evidence_stages['analysts']['recent_bars']
    latest_bars = evidence['recent_bars']
    if (initial_bars and latest_bars and initial_bars[-1]['bar_open_ist']!=latest_bars[-1]['bar_open_ist']
            and any(a['module']=='technical' for a in analyst_results)):
        progress('[4/4] Technical analyst checking the newest completed candle before the entry decision ...')
        previous = next(a for a in analyst_results if a['module']=='technical')
        updated = an.run_analyst(llm,'technical',horizon+'\n'+live_block+'\n'+evidence['text']+
            '\n[UPDATED INTRADAY TECHNICAL REVIEW]\nUse these refreshed observations to assess trend, momentum, '
            'entry readiness and evidence-supported stop/target levels. Keep the report within 300 words. '
            'Separate directional bias from entry readiness. Check whether a pattern has ALREADY confirmed in the supplied '
            'completed bars; do not demand an extra future candle by default. If direction remains uncertain, say so. '+
            an.VERDICT_INSTRUCTION)
        analyst_results = [updated if a['module']=='technical' else a for a in analyst_results]
        analyst_revisions.append(dict(module='technical',previous=previous,updated=updated,evidence=evidence))
        refresh_entry_evidence('trader')

    progress("[4/4] Trader is evaluating entry, stop and target ...")
    price_block = (f"[DAILY WTI/FX BACKGROUND — not MCX intraday levels]\n"
                   f"WTI daily reference ${wti_snap['price']:.2f}; USDINR {fx_last:.3f}. "
                   "Use the supplied MCX five-minute indicators and bars for intraday stops and targets.")
    from .signals import SignalBook
    from .execution import maintain_signals
    book = SignalBook(cfg)
    maintain_signals(cfg)
    from .history import learning_context
    from .setups import PendingSetups
    tally = decision._module_tally(analyst_results)
    lean = 'LONG' if len(tally['BULLISH']) > len(tally['BEARISH']) else 'SHORT' if len(tally['BEARISH']) > len(tally['BULLISH']) else None
    learning = learning_context(cfg, as_of=now_ist(), side=lean)
    from .economics import cost_context, review_plan_costs
    pending_context = PendingSetups(cfg).context()
    lessons = "\n\n" + book.context() + '\n\n' + learning['context'] + '\n\n' + pending_context + '\n\n' + cost_context(cfg)
    progress(f"[4/4] Trader evaluating the setup with {learning['eligible_outcomes']} same-day outcomes and {len(learning['lesson_ids'])} saved lessons")
    cio_mem = lessons
    desk_brief = ("\n\n=== DESK BRIEF (compiled by the desk from the same feeds the news analyst read — a count of evidence, not a view) ===\n"
                  + briefing["summary"])
    price_block = horizon + "\n" + evidence['text'] + "\n" + live_block + "\n\n" + price_block + lessons + desk_brief
    if use_mcx and strategy != "intraday":
        price_block += (f"\nREAL MCX DATA ({mcx_candles.attrs.get('contract')}): last close Rs "
                        f"{snap['price']:.0f}/bbl, expiry {mcx_candles.attrs.get('expiry')}; "
                        f"supports Rs {snap['supports']}, resistances Rs {snap['resistances']}")
    from .candidates import build_candidates, preferred_candidate, proposals_context, annotate_selection
    from .signals import latest_quote, price_level
    entry_now=now_ist()
    entry_quote,_=latest_quote(cfg,entry_now)
    entry_models=build_candidates(cfg,entry_bars,entry_quote,entry_now)
    candidate=preferred_candidate(entry_models)
    price_block+='\n\n'+proposals_context(entry_models)
    trader = decision.run_trader(llm, symbol, spec, price_block, analyst_results, debate)
    trader['entry_candidate']=candidate
    annotate_selection(trader,entry_models)
    from .setups import setup_clock
    if isinstance(trader.get('setup'),dict):
        try:trader['setup_timing']=setup_clock(trader['setup'],evidence_stages['trader']['as_of'],cfg['strategy']['entry_cutoff'])
        except (ValueError,TypeError):trader['setup_timing']={'error':'Invalid conditional setup timing; do not arm'}
    refresh_entry_evidence('risk')
    trader['paper_economics']=review_plan_costs(cfg,trader,latest_quote(cfg,now_ist())[0])
    trader['paper_economics_reviews']={'risk':trader['paper_economics']}
    atr_note = (f"ATR14 = Rs {inr_levels['atr_inr']}/bbl; "
                "evaluate the proposed stop distance and reward/risk against THIS strategy's horizon. "
                "Do not compute capital, position percentages, lots, margin or account returns.")
    progress('[4/4] Risk review is checking the intraday setup ...')
    risk = decision.run_risk(llm, symbol, trader, debate, horizon + "\n" + live_block + "\n" + evidence['text'] + "\n\n" + atr_note + lessons + desk_brief)
    if watch:
        cio_mem += ("\n\n=== ANALYSIS ONLY ===\nWrite your current view of the oil market and what would change it. "
                    "Use the live-market block above for market status. If the market is closed, explain your plan for the next session. "
                    "The FINAL DECISION is a research view; no signal is issued by this run.\n")
    progress('[4/4] CIO is making the final decision ...')
    refresh_entry_evidence('cio')
    trader['paper_economics']=review_plan_costs(cfg,trader,latest_quote(cfg,now_ist())[0])
    trader['paper_economics_reviews']['cio']=trader['paper_economics']
    cio_mem = horizon + "\n" + live_block + "\n" + evidence['text'] + "\n" + cio_mem + desk_brief
    cio = decision.run_cio(llm, symbol, trader, risk, debate, today, memory_block=cio_mem)

    result = {"strategy": strategy, "strategy_policy": cfg["strategy"], "historical_learning": learning,
              "quote_precheck": quote_precheck,
              "contract_expiry": evidence.get('contract_expiry'),
              "decision_evidence": evidence,
              "evidence_stages": evidence_stages, "analyst_revisions": analyst_revisions,
              "setup_evidence_as_of": evidence_stages['trader']['as_of'],
              "pending_setup_context": pending_context,
              "entry_candidate": candidate,
              "entry_models": entry_models,
              "entry_eligible": intraday_ready and not risk.get("degraded") and not cio.get("degraded"),
              "entry_block_reason": "Risk/CIO model unavailable or invalid output" if risk.get("degraded") or cio.get("degraded") else intraday_note,
              "symbol": symbol, "contract": spec["name"], "analysis_date": today,
              "risk_gate": "on",
              "data": {"wti": round(wti_snap["price"], 2), "usdinr": round(fx_last, 3),
                       "mcx_fair_value_inr": inr_levels["fair_value_inr"],
                       "technical_source": tech_label, "mcx_last_close_inr": snap["price"] if use_mcx else None,
                       "curve": curve_m, "cot": cot, "eia": eia, "cycle": cycle,
                       "basis": basis_data, "news_count": len(news),
                       "news_official": off_items[:12], "news_lanes": dict(__import__("collections").Counter(n.get("lane", "?") for n in news)),
                       "maritime_incidents_3d": len(_maritime), "radar_events_24h": len(_radar),
                       "news_items": news[:30],
                       "av_sentiment_count": len(av_sentiment or []),
                       "mcx_participant_oi": bool(mcx_participant_oi),
                       "skipped_modules": skipped, "technical_snapshot": snap},
              "analysts": analyst_results, "debate": debate, "trader": trader,
              "risk": risk, "cio": cio,
              "briefing": {"text": briefing["text"], "summary": briefing["summary"], **briefing["json"]}}
    if watch:
        result["kind"] = "night-watch"
        _save_report(result, cfg, news, subdir="night")
        progress("    Analysis-only view saved; no signal issued")
        try:
            (Path(cfg["paths"]["project_root"]) / "events").mkdir(exist_ok=True)
            with open(Path(cfg["paths"]["project_root"]) / "events" / f"events_{dt.date.today():%Y%m%d}.jsonl", "a") as f:
                f.write(json.dumps({"ts": dt.datetime.now().strftime("%H:%M:%S"), "kind": "info", "icon": "🌙",
                                    "msg": f"🌙 Night desk view: {cio['final_decision']} at the open (trader {trader['direction']} {trader['confidence']})"}) + "\n")
        except Exception:
            pass
        return result
    _save_report(result, cfg, news)

    maintain_signals(cfg)
    from .setups import PendingSetups
    # Recover brief collector gaps once, without repeating the paid council.
    # Register still validates session, contract, confirmation, range and live R/R.
    directional=any(side in cio.get('final_decision','') for side in ('LONG','SHORT'))
    if result['entry_eligible'] and directional and (price_level(trader.get('entry')) is not None or trader.get('setup')):
        from .quote_wait import wait_for_fresh_quote
        _,result['quote_recovery']=wait_for_fresh_quote(cfg,expected_contract=result['contract_expiry'],
                                                       progress=progress,clock=now_ist)
        if result['quote_recovery']['reason'].startswith('Contract changed'):
            result['entry_eligible']=False
            result['entry_block_reason']=result['quote_recovery']['reason']
    result["signal"] = PendingSetups(cfg).register(result, now_ist())
    # Exact availability time for rejected-plan comparisons, including quote recovery.
    result['signal_decided_at'] = now_ist().isoformat(timespec='microseconds')
    result["signal_stats"] = book.stats()
    from .paper import PaperBook
    paper = PaperBook(cfg)
    paper_rows = paper.sync(book.load(), now_ist())
    signal_id = result['signal'].get('id') or result['signal'].get('signal_id')
    result['paper_trade'] = next((r for r in paper_rows if r.get('signal_id') == signal_id and signal_id), None)
    progress(f"    {strategy} signal: {result['signal']['status']} — {result['signal'].get('reason', result['signal'].get('side', ''))}")
    _save_report(result, cfg, news)
    if notify:
        from .notify import format_decision, send_telegram
        result["notified"] = send_telegram(cfg, format_decision(result))
    return result



def _live_market_block(cfg: dict, symbol: str, wti_px: float, fx: float, fair: float) -> str:
    """The front contract's own last print, for every role — levels are on the CONTRACT, never on parity."""
    try:
        t = json.loads((Path(cfg["paths"]["cache_dir"]) / "live" / f"latest_{symbol}.json").read_text())
        ts = dt.datetime.strptime(t["ts"], "%Y-%m-%d %H:%M:%S")
        now = now_ist()
        _market_open = lambda t: t.weekday() < 5 and dt.time(9) <= t.time() <= dt.time.fromisoformat(cfg["strategy"]["session_close"])
        age_min = (now - ts).total_seconds() / 60
        state = ("LIVE" if age_min <= 10 else f"STALE PRINT — feed stalled {age_min:.0f} min") if _market_open(now) \
            else f"LAST PRINT — market closed, {age_min / 60:.1f}h ago"
        ltp = float(t["ltp"])
        exp, nxt = str(t.get("expiry") or "")[:10], float(t.get("next_close") or 0)
        rolled = ""  # the print belongs to a month that expired at that close: the tradable contract is now the next month
        month_expired = bool(exp) and (exp < now.date().isoformat() or (exp == now.date().isoformat() and not _market_open(now)))
        if month_expired and nxt > 0:
            rolled = (f"\nNOTE: that {exp} contract EXPIRED at this close. From the next session the tradable front contract is the FOLLOWING month, "
                      f"last close Rs {nxt:,.0f}/bbl (premium over parity Rs {nxt - fair:+,.0f}). Quote every level on the NEXT-month contract, not on Rs {ltp:,.0f}.")
        return (f"[LIVE MARKET — {state} · MCX {symbol} front contract, expiry {t.get('expiry')}, print {t['ts'][11:16]} IST]\n"
                f"Contract price Rs {ltp:,.0f}/bbl (session high {float(t.get('high') or ltp):,.0f} / low {float(t.get('low') or ltp):,.0f}, "
                f"{float(t.get('change_pct') or 0):+.2f}% on the day) · WTI ${wti_px:.2f} · USDINR {fx:.3f} · "
                f"parity (WTI x USDINR) Rs {fair:,.0f} · contract premium over parity Rs {ltp - fair:+,.0f}/bbl.\n"
                f"RULE: every Rs/bbl level you write (entry, stop, target, support, resistance) must be on the CONTRACT price above, not on parity. "
                f"Parity is a fair-value reference; the contract trades at a premium or discount to it that can reach several hundred rupees near expiry. "
                f"Prices quoted inside headlines are historical, not this print." + rolled)
    except Exception:
        return (f"[LIVE MARKET — no MCX print available] WTI ${wti_px:.2f} · USDINR {fx:.3f} · parity Rs {fair:,.0f}/bbl. "
                f"Levels are on parity this run — say so explicitly wherever you quote a level.")


def _save_report(result: dict, cfg: dict, news: list[dict], subdir: str | None = None) -> None:
    ts = result.setdefault("report_timestamp", now_ist().strftime("%Y%m%d_%H%M%S"))
    out = Path(cfg["paths"]["reports_dir"]) / subdir if subdir else Path(cfg["paths"]["reports_dir"])
    out.mkdir(parents=True, exist_ok=True)
    result["report_path"] = str(out / f"{result['symbol']}_{ts}.md")
    json_path = out / f"{result['symbol']}_{ts}.json"
    temporary = json_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2, default=str))
    temporary.replace(json_path)
    lines = [f"# {result['contract']} — {result.get('strategy', 'long_term')} Analysis ({result['analysis_date']})", "",
             f"Signal status: {(result.get('signal') or {}).get('status', 'view only')} | Minimum reward/risk {result['strategy_policy']['min_reward_risk']}:1", "",
             f"**WTI** ${result['data']['wti']} | **USDINR** {result['data']['usdinr']:.3f} | "
             f"**MCX fair value** Rs {result['data']['mcx_fair_value_inr']}/bbl", "",
             f"## Final Decision: {result['cio']['final_decision']}",
             f"Trader: {result['trader']['direction']} ({result['trader']['confidence']}), "
             f"entry {result['trader']['entry']}, "
             f"stop {result['trader']['stop']}, target {result['trader']['target']}", ""]
    signal = result.get('signal') or {}
    if signal.get('entry') is not None:
        lines += [f"Observed signal entry {signal['entry']} | Stop {signal['stop']} | Target {signal['target']}",
                  f"Risk : reward = 1 : {signal['reward_risk']:.2f} | Risk {signal['risk_points']} points | Reward {signal['reward_points']} points",
                  f"Valid until {signal['valid_until']} IST | Contract expiry {signal['contract_expiry']}", ""]
    elif signal.get('reason'):
        lines += [signal['reason'], ""]
    if signal.get('plan'):
        plan = signal['plan']
        lines += [f"Conditional entry {plan['entry_min']}–{plan['entry_max']} | Stop {plan['stop']} | Target {plan['target']}",
                  f"Confirmation: {plan['trigger']} at {plan['level']} | Momentum: {plan['momentum']}",
                  f"Setup expires {signal['valid_until']} IST; a pending plan is not an issued signal.", ""]
    if result.get('paper_trade'):
        trade=result['paper_trade']
        lines += [f"Paper trade {trade['trade_id']}: {trade['status']} | {trade['lots']} simulated lots",
                  f"Virtual risk budget Rs {trade['risk_budget_inr']} | Estimated round-trip costs Rs {trade['fees_inr']}",
                  trade.get('reason') or trade['fill_assumption'], ""]
    for r in result["analysts"]:
        lines += [f"## {r['title']} — {r['direction']} ({r['confidence']})", "", r["report"], ""]
    lines += ["## Debate", "", result["debate"]["summary"], ""]
    for rd in result["debate"]["rounds"]:
        lines += [f"### Round {rd['round']} (bull {rd['bull_score']} vs bear {rd['bear_score']})",
                  "", "**Bull:** " + rd["bull"], "", "**Bear:** " + rd["bear"], ""]
    lines += ["## Trader", "", result["trader"]["reasoning"], "",
              "## Risk Manager", "", result["risk"]["opinion"], "",
              "## CIO", "", result["cio"]["statement"], "",
              "## Historical evidence used", "", result.get('historical_learning', {}).get('context', 'No historical evidence selected.'), "",
              "## What the Council saw", "", result.get("briefing", {}).get("summary", ""), "",
              "## News basis", ""] + [f"- [{n['published']}] {n['title']} ({n['source']})" for n in news[:25]]
    lines += ["", "---", "*Educational analysis only — not investment advice. Futures trading involves substantial risk of loss.*"]
    (out / f"{result['symbol']}_{ts}.md").write_text("\n".join(lines))
    result["report_path"] = str(out / f"{result['symbol']}_{ts}.md")
