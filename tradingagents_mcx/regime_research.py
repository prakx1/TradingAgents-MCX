"""Causal regime replay with delayed, adverse fills. Research artifacts only."""
from collections import Counter
from dataclasses import asdict, dataclass
import datetime as dt
import math

import pandas as pd

from .forecast_research import quality_bars
from .regimes import MODELS, RULES, cost_check, regime_candidates
from .strategies import entry_allowed


@dataclass(frozen=True)
class ReplayProtocol:
    version: str = 'regime_replay_v2'
    execution_delay_seconds: int = 30
    maximum_entry_wait_seconds: int = 120
    max_observation_gap_seconds: int = 120
    fee_points: float = 8.
    slippage_points_each_way: float = 1.
    daily_entry_limit: int = 3
    lookback_sessions: int = 5

    def __post_init__(self):
        for name in ('execution_delay_seconds', 'maximum_entry_wait_seconds', 'max_observation_gap_seconds',
                     'daily_entry_limit', 'lookback_sessions'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0: raise ValueError(name)
        if self.execution_delay_seconds >= self.maximum_entry_wait_seconds: raise ValueError('Entry delay exceeds wait')
        for name in ('fee_points', 'slippage_points_each_way'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0: raise ValueError(name)


LIMITATIONS = [
    'Fixed simplified regime rules inspired by regime-dependent forecasting, not a replication of OilFMP.',
    'Mechanical proposals; excludes AI council selection, actual review latency and API spending.',
    'Legacy sampled CSVs lack exchange timestamps and bid/ask history; receipt time is a proxy.',
    'Recent 21 candles require observed coverage and volume; older price-only indicator warmup can contain gaps or missing volume.',
    'Unobserved intrabar extremes remain unknown. Gap-affected exits are flagged, not repaired.',
    'Fees and adverse one-point slippage each way are assumptions, not broker fills.',
    'One position and three entries per day per model; no account sizing or portfolio loss limits.',
    'Unfinished exits remain unresolved. Calendar slices are retrospective, not untouched holdout data.',
    'No automatic promotion, parameter fitting, or change to existing signals and trades.',
]


def summaries(trades, split, protocol):
    result = []
    for model, rows in trades.items():
        for period in ('all', 'before_split', 'after_split'):
            part = [r for r in rows if period == 'all' or (r['opened_at'][:10] < str(split)) == (period == 'before_split')]
            closed = sorted([r for r in part if r['status'] == 'closed'], key=lambda r: r['closed_at'])
            n = len(closed); wins = sum(r['net_points'] > 0 for r in closed)
            gains = sum(max(0, r['net_points']) for r in closed); losses = -sum(min(0, r['net_points']) for r in closed)
            equity = peak = drawdown = 0.
            for r in closed:
                equity += r['net_points']; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
            clean = [r for r in closed if r['max_observation_gap_seconds'] <= protocol.max_observation_gap_seconds]
            result.append(dict(model=model, label=MODELS[model], period=period, entries=len(part), closed=n,
                unresolved=len(part) - n, wins=wins, win_rate_pct=round(100 * wins / n, 2) if n else None,
                gross_points=round(sum(r['gross_points'] for r in closed), 2), fees_points=round(n * protocol.fee_points, 2),
                net_points=round(equity, 2), mean_net_r=round(sum(r['net_r'] for r in closed) / n, 4) if n else None,
                profit_factor=round(gains / losses, 4) if losses else None,
                realized_drawdown_points=round(drawdown, 2), gap_affected=n - len(clean),
                observed_without_gaps=len(clean), net_points_without_gaps=round(sum(r['net_points'] for r in clean), 2)))
    return result


def replay(ticks, policy, start, end, as_of, split, protocol=ReplayProtocol()):
    ticks = ticks[ticks.ts <= as_of].sort_values('ts').copy()
    by_contract = {}
    for contract, part in ticks.groupby('expiry'):
        sessions = [quality_bars(session, protocol) for _, session in part.groupby(part.ts.dt.date)]
        bars = pd.concat(sessions).dropna(subset=['open', 'high', 'low', 'close'])
        bars['research_usable'] = bars.usable
        by_contract[contract] = bars
    dates = sorted(set(ticks.ts.dt.date))
    lookback = {day: dates[max(0, i - protocol.lookback_sessions + 1)] for i, day in enumerate(dates)}
    trades = {m: [] for m in MODELS}; opened = {m: None for m in MODELS}; pending = {m: None for m in MODELS}
    entries, decisions, rejection_counts = Counter(), [], Counter()
    last_bucket = {}
    for tick in ticks.itertuples():
        now = tick.ts.to_pydatetime(); day = now.date(); price = float(tick.ltp)
        if not start <= day <= end: continue
        for model in MODELS:
            trade = opened[model]
            if trade is not None:
                if day != dt.date.fromisoformat(trade['opened_at'][:10]):
                    trade.update(status='unresolved', reason='Missing same-session exit'); opened[model] = None
                elif tick.expiry == trade['contract_expiry']:
                    gap = (now - dt.datetime.fromisoformat(trade['last_observed_at'])).total_seconds()
                    trade.update(max_observation_gap_seconds=max(trade['max_observation_gap_seconds'], gap),
                                 last_observed_at=str(now), last_price=price)
                    sign = 1 if trade['side'] == 'LONG' else -1
                    reason = ('square_off' if now >= dt.datetime.fromisoformat(trade['valid_until']) else
                              'stop_hit' if sign * (price - trade['stop']) <= 0 else
                              'target_hit' if sign * (price - trade['target']) >= 0 else None)
                    if reason:
                        fill = price - sign * protocol.slippage_points_each_way
                        gross = sign * (fill - trade['entry']); net = gross - protocol.fee_points
                        trade.update(status='closed', closed_at=str(now), exit=fill, observed_exit=price,
                            close_reason=reason, gross_points=gross, fees_points=protocol.fee_points,
                            net_points=net, net_r=net / trade['planned_net_risk_points'])
                        opened[model] = None
            plan = pending[model]
            if plan is None: continue
            elapsed = (now - dt.datetime.fromisoformat(plan['proposed_at'])).total_seconds()
            if day != dt.date.fromisoformat(plan['proposed_at'][:10]) or elapsed > protocol.maximum_entry_wait_seconds:
                rejection_counts[(model, 'Entry observation missing within the delay window')] += 1
                pending[model] = None; continue
            if tick.expiry != plan['contract_expiry']: continue
            sign = 1 if plan['side'] == 'LONG' else -1
            why = ('Stop or target touched before entry' if sign * (price - plan['stop']) <= 0 or sign * (price - plan['target']) >= 0 else
                   'Entry window closed during delay' if not entry_allowed(policy, now) else None)
            if why:
                rejection_counts[(model, why)] += 1; pending[model] = None; continue
            if elapsed < protocol.execution_delay_seconds: continue
            pending[model] = None
            if abs(price - plan['entry']) > RULES.maximum_chase_atr * plan.get('atr14', float('inf')):
                rejection_counts[(model, 'Delayed entry moved beyond the allowed entry area')] += 1; continue
            checked = cost_check(dict(plan, entry=price), policy, protocol.fee_points,
                                 protocol.slippage_points_each_way, require_net=plan.get('net_floor_required', True))
            if checked['status'] != 'ready':
                rejection_counts[(model, checked['reason'])] += 1; continue
            trade = {k: checked[k] for k in ('side', 'stop', 'target', 'contract_expiry', 'confirmation_bar', 'confirmed_at')}
            trade.update(model=model, regime=plan.get('regime'), method=plan.get('method', 'continuation'),
                proposed_at=plan['proposed_at'], opened_at=str(now), status='open',
                entry=price + sign * protocol.slippage_points_each_way, observed_entry=price,
                planned_net_risk_points=checked['economics']['net_risk_points'],
                planned_net_reward_risk=checked['economics']['net_reward_risk'],
                last_observed_at=str(now), last_price=price, max_observation_gap_seconds=0,
                valid_until=str(dt.datetime.combine(day, dt.time.fromisoformat(policy['square_off']))))
            opened[model] = trade; trades[model].append(trade); entries[(model, day)] += 1
        bucket = tick.ts.floor('5min')
        if last_bucket.get(tick.expiry) == bucket or not entry_allowed(policy, now): continue
        last_bucket[tick.expiry] = bucket
        bars = by_contract[tick.expiry]
        history = bars[(bars.index.date >= lookback[day]) & (bars.index + pd.Timedelta(minutes=5) <= now)].copy()
        history.attrs['contract_expiry'] = tick.expiry
        quote = dict(ts=str(now), ltt=str(now), ltp=price, expiry=tick.expiry)
        state, proposals = regime_candidates(history, quote, policy, now, protocol.fee_points, protocol.slippage_points_each_way)
        record = dict(as_of=str(now), contract=tick.expiry, regime=state['regime'], reason=state['reason'], models={})
        for model, proposal in proposals.items():
            why = (proposal.get('reason', 'No valid setup') if proposal['status'] != 'ready' else
                   'Position already open' if opened[model] is not None else
                   'Entry already pending' if pending[model] is not None else
                   'Daily entry cap reached' if entries[(model, day)] >= protocol.daily_entry_limit else None)
            record['models'][model] = dict(status='waiting' if why else 'entry_pending', reason=why or '')
            if not why: pending[model] = dict(proposal, proposed_at=str(now))
        decisions.append(record)
    for model in MODELS:
        if opened[model] is not None: opened[model].update(status='unresolved', reason='Input ends before an observed exit')
        if pending[model] is not None: rejection_counts[(model, 'Input ends before delayed entry')] += 1
    return dict(purpose='research_only', live_rules_changed=False, generated_at=None,
        start=str(start), end=str(end), as_of=str(as_of), split_date=str(split),
        protocol=asdict(protocol), rules=asdict(RULES), limitations=LIMITATIONS,
        summaries=summaries(trades, split, protocol), trades=trades, decisions=decisions,
        regime_counts=dict(Counter(r['regime'] for r in decisions)),
        execution_rejections=[dict(model=m, reason=r, count=n) for (m, r), n in rejection_counts.items()])
