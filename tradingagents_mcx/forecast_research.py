"""Point-in-time forecast diagnostics; no signal book, account, orders or LLMs.

Directional probes use fixed time exits, NOT the desk's stop/target lifecycle.
They measure whether forecasts deserve further strategy testing, not wallet P&L.
"""
from collections import Counter
from dataclasses import dataclass
import datetime as dt
import hashlib
import math

import numpy as np
import pandas as pd

from .forecast_models import baseline_forecasts
from .strategies import entry_allowed


@dataclass(frozen=True)
class ForecastProtocol:
    version: str = 'forecast_diagnostics_v1'
    context_bars: int = 80
    horizon_bars: int = 6
    cadence_minutes: int = 30
    max_observation_gap_seconds: int = 120
    execution_delay_seconds: int = 30
    boundary_tolerance_seconds: int = 60
    fee_points: float = 8.0
    slippage_points_each_way: float = 1.0
    min_expected_move_atr: float = 0.25
    seed: int = 20261027

    def __post_init__(self):
        integer_fields = ['context_bars', 'horizon_bars', 'cadence_minutes',
                          'max_observation_gap_seconds', 'execution_delay_seconds',
                          'boundary_tolerance_seconds', 'seed']
        for field in integer_fields:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f'{field} must be a nonnegative integer')
        if not 20 <= self.context_bars <= 512 or not 1 <= self.horizon_bars < self.context_bars:
            raise ValueError('Require 20 <= context <= 512 and 1 <= horizon < context')
        if self.cadence_minutes < self.horizon_bars * 5 or self.cadence_minutes % 5:
            raise ValueError('Cadence must be a multiple of 5 and at least the forecast horizon')
        if self.execution_delay_seconds + self.boundary_tolerance_seconds >= self.horizon_bars * 300:
            raise ValueError('Entry must precede the forecast boundary')
        for field in ['fee_points', 'slippage_points_each_way', 'min_expected_move_atr']:
            if not math.isfinite(getattr(self, field)) or getattr(self, field) < 0:
                raise ValueError(f'Invalid {field}')

    @property
    def cost(self):
        return self.fee_points + 2 * self.slippage_points_each_way


def quality_bars(ticks, protocol):
    """One contract/session at a time; missing bars/volume are never filled."""
    data = ticks.sort_values('ts').set_index('ts').copy()
    if data.empty:
        return pd.DataFrame()
    delta = data.vol_cum.diff()
    gap = data.index.to_series().diff().dt.total_seconds()
    data['volume'] = delta.where((delta >= 0) & (gap <= protocol.max_observation_gap_seconds))
    data['bad_volume'] = data.volume.isna().astype(int)
    data['gap'] = gap
    data['observed_at'] = data.index
    bars = data.ltp.resample('5min').ohlc()
    grouped = data.resample('5min')
    bars['volume'] = grouped.volume.sum(min_count=1)
    bars['first_seen'] = grouped.observed_at.first()
    bars['last_seen'] = grouped.observed_at.last()
    bars['max_gap'] = grouped.gap.max()
    bars['bad_volume'] = grouped.bad_volume.sum()
    bars['usable'] = ((bars.first_seen - bars.index).dt.total_seconds() <= protocol.max_observation_gap_seconds) & (
        (bars.index + pd.Timedelta(minutes=5) - bars.last_seen).dt.total_seconds() <= protocol.max_observation_gap_seconds
    ) & (bars.max_gap <= protocol.max_observation_gap_seconds) & (bars.bad_volume == 0)
    return bars


def select_windows(ticks, start, end, as_of, policy, protocol):
    """Select using PAST data only, including windows whose future is missing."""
    ticks = ticks[(ticks.ts <= as_of) & (ticks.ts.dt.date >= start) & (ticks.ts.dt.date <= end)].copy()
    windows, skipped = [], Counter()
    for (contract, day), part in ticks.groupby(['expiry', ticks.ts.dt.date]):
        if day > dt.date.fromisoformat(contract):
            continue
        bars = quality_bars(part, protocol)
        begin = pd.Timestamp(dt.datetime.combine(day, dt.time.fromisoformat(policy['session_open'])))
        # Context itself is restricted to the trading session.
        bars = bars[bars.index >= begin]
        squareoff = pd.Timestamp(dt.datetime.combine(day, dt.time.fromisoformat(policy['square_off'])))
        for cutoff in pd.date_range(begin, squareoff, freq=f'{protocol.cadence_minutes}min'):
            if cutoff > as_of or not entry_allowed(policy, cutoff.to_pydatetime()):
                continue
            terminal = cutoff + pd.Timedelta(minutes=5 * protocol.horizon_bars)
            if terminal + pd.Timedelta(seconds=protocol.boundary_tolerance_seconds) > squareoff:
                skipped['horizon_past_squareoff'] += 1
                continue
            index = pd.date_range(end=cutoff - pd.Timedelta(minutes=5), periods=protocol.context_bars, freq='5min')
            context = bars.reindex(index)
            if context[['open', 'high', 'low', 'close', 'volume']].isna().any().any():
                skipped['missing_context'] += 1
                continue
            if not context.usable.eq(True).all():
                skipped['context_observation_gap_or_volume_reset'] += 1
                continue
            tr = pd.concat([context.high - context.low,
                            (context.high - context.close.shift()).abs(),
                            (context.low - context.close.shift()).abs()], axis=1).max(axis=1)
            atr = float(tr.tail(14).mean())
            if not math.isfinite(atr) or atr <= 0:
                skipped['zero_volatility'] += 1
                continue
            prices = context[['open', 'high', 'low', 'close', 'volume']].copy()
            windows.append(dict(cutoff=cutoff, terminal=terminal, contract=contract, context=prices,
                                context_sha256=hashlib.sha256(prices.to_csv().encode()).hexdigest(),
                                reference=float(context.close.iloc[-1]), atr=atr,
                                last_observed_at=str(context.last_seen.iloc[-1])))
    return sorted(windows, key=lambda w: (w['cutoff'], w['contract'])), dict(skipped)


def forecast_window(window, protocol, kronos=None):
    """This function receives no future prices or realised outcomes."""
    forecasts = baseline_forecasts(window['context'], protocol.horizon_bars)
    if kronos is not None:
        forecasts['kronos_small'] = kronos.predict(window['context'], window['cutoff'],
                                                  protocol.horizon_bars, window['contract'])
    for value in forecasts.values():
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid forecast; abort instead of silently dropping a difficult case')
    return dict(cutoff=str(window['cutoff']), terminal=str(window['terminal']), contract=window['contract'],
                reference=window['reference'], atr=window['atr'], context_sha256=window['context_sha256'],
                last_observed_at=window['last_observed_at'], forecasts=forecasts)


def first_observation(ticks, boundary, tolerance):
    after = ticks[(ticks.ts >= boundary) & (ticks.ts <= boundary + pd.Timedelta(seconds=tolerance))]
    return after.sort_values('ts').iloc[0] if len(after) else None


def score_forecasts(records, ticks, as_of, protocol):
    """Evaluate identical windows for every model; unresolved calls remain visible."""
    ticks = ticks[ticks.ts <= as_of].sort_values('ts')
    rows = []
    for record in records:
        cutoff, terminal = pd.Timestamp(record['cutoff']), pd.Timestamp(record['terminal'])
        part = ticks[(ticks.expiry == record['contract']) & (ticks.ts.dt.date == cutoff.date())]
        entry = first_observation(part, cutoff + pd.Timedelta(seconds=protocol.execution_delay_seconds),
                                  protocol.boundary_tolerance_seconds)
        exit_quote = first_observation(part, terminal, protocol.boundary_tolerance_seconds)
        gap = None
        if entry is not None and exit_quote is not None:
            path = part[(part.ts >= entry.ts) & (part.ts <= exit_quote.ts)]
            gap = float(path.ts.diff().dt.total_seconds().max())
        outcome_ok = entry is not None and exit_quote is not None and gap is not None and math.isfinite(gap) and gap <= protocol.max_observation_gap_seconds
        missing = ('missing_entry' if entry is None else 'missing_exit' if exit_quote is None else
                   'observation_gap' if not outcome_ok else None)
        for model, prediction in record['forecasts'].items():
            move = prediction - record['reference']
            threshold = max(protocol.cost, protocol.min_expected_move_atr * record['atr'])
            side = 'LONG' if move > threshold else 'SHORT' if move < -threshold else 'WAIT'
            row = dict(model=model, cutoff=record['cutoff'], contract=record['contract'],
                       terminal=record['terminal'], predicted_price=prediction,
                       reference=record['reference'], expected_move_points=move, side=side,
                       status='evaluated' if outcome_ok else 'unresolved', reason=missing,
                       max_observation_gap_seconds=gap if gap is not None and math.isfinite(gap) else None,
                       entry=str(entry.ts) if entry is not None else None,
                       exit=str(exit_quote.ts) if exit_quote is not None else None)
            if outcome_ok:
                actual_move = float(exit_quote.ltp) - record['reference']
                sign = 1 if side == 'LONG' else -1 if side == 'SHORT' else 0
                gross = sign * (float(exit_quote.ltp) - float(entry.ltp))
                row.update(actual_price=float(exit_quote.ltp), entry_price=float(entry.ltp),
                           absolute_error=abs(prediction - float(exit_quote.ltp)),
                           direction_correct=bool(np.sign(move) == np.sign(actual_move)) if move and actual_move else None,
                           probe_gross_points=gross, probe_cost_points=protocol.cost if sign else 0,
                           probe_net_points=gross - protocol.cost if sign else 0)
            rows.append(row)
    return rows


def summarize(rows):
    out = []
    for model in sorted({r['model'] for r in rows}):
        all_rows = [r for r in rows if r['model'] == model]
        valid = [r for r in all_rows if r['status'] == 'evaluated']
        calls = [r for r in valid if r['side'] != 'WAIT']
        directional = [r for r in valid if r['direction_correct'] is not None]
        by_day = {}
        for r in calls:
            by_day.setdefault(r['cutoff'][:10], []).append(r['probe_net_points'])
        # Uncertainty is deliberately not estimated from tiny correlated samples.
        interval = None
        if len(by_day) >= 5 and len(calls) >= 30:
            rng = np.random.default_rng(20261027)
            days = list(by_day.values())
            means = [float(np.mean([v for i in rng.integers(0, len(days), len(days)) for v in days[i]]))
                     for _ in range(2000)]
            interval = [round(float(v), 3) for v in np.quantile(means, [0.025, 0.975])]
        out.append(dict(model=model, forecasts=len(all_rows), evaluated=len(valid),
                        unresolved=len(all_rows) - len(valid), calls=sum(r['side'] != 'WAIT' for r in all_rows),
                        evaluated_calls=len(calls), call_coverage_pct=round(100 * len(calls) / len(valid), 2) if valid else None,
                        mae_points=round(float(np.mean([r['absolute_error'] for r in valid])), 3) if valid else None,
                        directional_n=len(directional),
                        directional_accuracy_pct=round(100 * sum(r['direction_correct'] for r in directional) / len(directional), 2) if directional else None,
                        probe_win_rate_pct=round(100 * sum(r['probe_net_points'] > 0 for r in calls) / len(calls), 2) if calls else None,
                        probe_net_points=round(sum(r['probe_net_points'] for r in calls), 3),
                        mean_probe_net_points=round(float(np.mean([r['probe_net_points'] for r in calls])), 3) if calls else None,
                        mean_probe_net_points_day_bootstrap_95pct=interval,
                        sessions_with_calls=len(by_day)))
    return out


LIMITATIONS = [
    'Retrospective diagnostic, not an untouched holdout or a replication of any published strategy.',
    'Five-minute candles are reconstructed from sampled LTP; extremes between observations may be missing.',
    'CSV history has receipt times, not exchange last-trade times. Repeated stale quotes cannot be detected.',
    'Only 80 contiguous same-session bars with bounded observation gaps qualify; this biases coverage toward late sessions.',
    'OHLC mean times sampled volume is a turnover proxy; MCX contracts differ from pretraining markets.',
    'Prediction quality is scored against the first observed boundary price within tolerance, not an official candle close.',
    'Directional probes are independent 30-minute fixed-exit diagnostics, without stops, targets, sizing, daily caps or council selection.',
    'Probe totals are price points per unit, not actual trades, wallet returns or a deployable strategy backtest.',
    'Fees and constant per-side slippage are assumptions. Bid/ask, market impact, API spend and actual model latency are not reproduced.',
    'Forecast calls are recorded before outcomes. Missing entry/exit quotes and observation gaps remain unresolved.',
    'Sampling multiple model paths does not produce a calibrated probability of winning.',
    'Day-block intervals are exploratory; fewer than five sessions or 30 evaluated calls receive no interval.',
    'No model is promoted to the live council or simulated account automatically.'
]
