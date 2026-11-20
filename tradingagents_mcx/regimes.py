"""Causal, fixed-rule intraday regime proposals for research and preview.

Inspired by regime-dependent forecasting; this is not an implementation of the
functional-mixture paper. No fitting, signal issuance, ledger or config writes.
"""
from dataclasses import asdict, dataclass
import math

import pandas as pd

from .economics import plan_economics
from .entries import continuation_candidate
from .indicators import compute_indicators
from .strategies import entry_allowed


@dataclass(frozen=True)
class RegimeRules:
    version: str = 'regime_rules_v2'
    warmup_bars: int = 80
    regime_bars: int = 20
    trend_efficiency_min: float = .35
    range_efficiency_max: float = .25
    trend_slope_min_atr: float = .20
    range_ma_spread_max_atr: float = 1.0
    shock_true_range_multiple: float = 3.0
    shock_atr_multiple: float = 2.0
    range_band_std: float = 2.0
    range_stop_buffer_atr: float = .20
    range_min_stop_atr: float = .80
    maximum_chase_atr: float = .50
    minimum_net_reward_risk: float = 1.50


RULES = RegimeRules()
MODELS = {'continuation_control': 'Current continuation control',
          'trend_only': 'Trend regime only',
          'range_only': 'Range reversion only',
          'regime_switch': 'Regime-aware combination'}


def _unavailable(reason, **details):
    return dict(status='unavailable', rule=RULES.version, reason=reason, **details)


def classify_regime(bars, now):
    """Indicators see completed bars only; the recent 21 bars must be contiguous."""
    out = dict(regime='unavailable', as_of=str(now), reason='No completed candles', rules=asdict(RULES))
    needed = ['open', 'high', 'low', 'close', 'volume']
    if not set(needed) <= set(bars.columns) or not isinstance(bars.index, pd.DatetimeIndex):
        return out
    if bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        return dict(out, reason='Candle times must be unique and sorted')
    completed = bars[bars.index + pd.Timedelta(minutes=5) <= now]
    if len(completed) < RULES.warmup_bars:
        return dict(out, regime='warming_up', reason='Need 80 completed candles for indicator history')
    completed = completed.tail(500).copy()
    completed[needed] = completed[needed].apply(pd.to_numeric, errors='coerce')
    if not completed[['open', 'high', 'low', 'close']].apply(lambda s: s.map(math.isfinite)).all().all():
        return dict(out, reason='Invalid candle values')
    if ((completed[['open', 'high', 'low', 'close']] <= 0).any().any() or (completed.volume < 0).any()
            or (completed.high < completed[['open', 'close', 'low']].max(axis=1)).any()
            or (completed.low > completed[['open', 'close', 'high']].min(axis=1)).any()):
        return dict(out, reason='Invalid candle geometry')
    confirmed = completed.index[-1] + pd.Timedelta(minutes=5)
    out.update(confirmation_bar=str(completed.index[-1]), confirmed_at=str(confirmed),
               contract_expiry=bars.attrs.get('contract_expiry'))
    if not 0 <= (pd.Timestamp(now) - confirmed).total_seconds() < 300:
        return dict(out, reason='Latest completed candle is stale')
    recent = completed.tail(RULES.regime_bars + 1)
    if recent.index[0].date() != now.date() or not recent.index.to_series().diff().dropna().eq(pd.Timedelta(minutes=5)).all():
        return dict(out, reason='Need 21 consecutive completed candles in the current session')
    # MA/ATR/MACD use prices only. Missing volume in older warmup must not
    # invalidate their history; recent regime bars still require complete volume.
    if not recent.volume.map(math.isfinite).all():
        return dict(out, reason='Recent candles lack volume observations')
    # Research bars can carry observed-tick coverage metadata. The live sampled
    # candle path lacks it, so preview labels that freshness limitation separately.
    if 'research_usable' in recent and not recent.research_usable.eq(True).all():
        return dict(out, reason='Recent candles contain an observation gap or missing volume')
    indicators = compute_indicators(completed)
    last = indicators.iloc[-1]
    atr = float(last.atr14)
    tr = pd.concat([completed.high - completed.low, (completed.high - completed.close.shift()).abs(),
                    (completed.low - completed.close.shift()).abs()], axis=1).max(axis=1)
    normal_tr = float(tr.iloc[-21:-1].median())
    normal_atr = float(indicators.atr14.iloc[-61:-1].median())
    if not all(math.isfinite(v) and v > 0 for v in (atr, normal_tr, normal_atr)):
        return dict(out, reason='Volatility history is unavailable or flat')
    travel = float(recent.close.diff().abs().sum())
    efficiency = abs(float(recent.close.iloc[-1] - recent.close.iloc[0])) / travel if travel else 0.
    slope = float(last.ma20 - indicators.ma20.iloc[-6]) / atr
    spread = float(last.ma20 - last.ma60) / atr
    out.update(efficiency=round(efficiency, 4), slope_atr=round(slope, 4),
               ma_spread_atr=round(spread, 4), atr14=round(atr, 4),
               shock_range_ratio=round(float(tr.iloc[-1]) / normal_tr, 4),
               shock_atr_ratio=round(atr / normal_atr, 4))
    if tr.iloc[-1] >= RULES.shock_true_range_multiple * normal_tr or atr >= RULES.shock_atr_multiple * normal_atr:
        return dict(out, regime='unstable', reason='Volatility shock; wait for conditions to settle')
    if efficiency >= RULES.trend_efficiency_min:
        if last.ma5 > last.ma20 > last.ma60 and slope >= RULES.trend_slope_min_atr:
            return dict(out, regime='trend_up', reason='Directional movement and moving-average slope agree upward')
        if last.ma5 < last.ma20 < last.ma60 and slope <= -RULES.trend_slope_min_atr:
            return dict(out, regime='trend_down', reason='Directional movement and moving-average slope agree downward')
    if efficiency <= RULES.range_efficiency_max and abs(spread) <= RULES.range_ma_spread_max_atr:
        return dict(out, regime='range', reason='Low directional efficiency and limited moving-average separation')
    return dict(out, regime='transition', reason='Mixed conditions; neither trend nor range rule is enabled')


def _quote_error(bars, quote, now, policy, state):
    if not entry_allowed(policy, now):
        return 'Intraday entry window is closed'
    if not quote:
        return 'No fresh quote for an entry proposal'
    try:
        if str(quote['expiry'])[:10] != bars.attrs.get('contract_expiry'):
            return 'Quote and candle contracts differ'
        if pd.Timestamp(quote['expiry']).date() < now.date():
            return 'Quoted contract has expired'
        for value in (quote['ts'], quote.get('ltt') or quote['ts']):
            stamp = pd.Timestamp(value)
            if stamp.tzinfo:
                stamp = stamp.tz_convert('Asia/Kolkata').tz_localize(None)
            if not 0 <= (pd.Timestamp(now) - stamp).total_seconds() <= policy['max_quote_age_seconds']:
                return 'Quote is stale or in the future'
            if stamp < pd.Timestamp(state['confirmed_at']):
                return 'Quote predates the completed confirmation'
        if not math.isfinite(float(quote['ltp'])) or float(quote['ltp']) <= 0:
            return 'Invalid quote price'
    except (KeyError, TypeError, ValueError):
        return 'Incomplete quote metadata'
    return None


def cost_check(candidate, policy, fee_points, slippage_points=1.0, require_net=True):
    """Fixed levels, assessed with adverse slippage each way; never stretch target."""
    if candidate['status'] != 'ready':
        return candidate
    try:
        costs = float(fee_points) + 2 * float(slippage_points)
        if not math.isfinite(slippage_points) or slippage_points < 0 or fee_points < 0:
            raise ValueError('Invalid costs')
        economics = plan_economics(candidate['entry'], candidate['stop'], candidate['target'], candidate['side'], costs)
    except (ValueError, TypeError):
        return dict(candidate, status='unavailable', reason='Invalid execution costs or level geometry')
    risk = abs(candidate['entry'] - candidate['stop'])
    reward = abs(candidate['target'] - candidate['entry'])
    if reward / risk < float(policy['min_reward_risk']):
        return dict(candidate, status='unavailable', reason='Fixed levels fail the gross reward/risk floor', economics=economics)
    if require_net and (reward - costs) / (risk + costs) < RULES.minimum_net_reward_risk:
        return dict(candidate, status='unavailable', reason='Fixed levels fail 1.5:1 reward/risk after fees and slippage', economics=economics)
    return dict(candidate, economics=economics, reason='', fee_points=fee_points,
                slippage_points_each_way=slippage_points, net_floor_required=require_net)


def regime_candidates(bars, quote, policy, now, fee_points, slippage_points=1.0):
    state = classify_regime(bars, now)
    unavailable = _unavailable(state['reason'], regime=state['regime'])
    result = {name: unavailable.copy() for name in MODELS}
    if state['regime'] in ('unavailable', 'warming_up'):
        return state, result
    error = _quote_error(bars, quote, now, policy, state)
    if error:
        return state, {name: _unavailable(error, regime=state['regime']) for name in MODELS}
    base = continuation_candidate(bars, quote, policy, now)
    base = dict(base, regime=state['regime'])
    result['continuation_control'] = cost_check(base, policy, fee_points, slippage_points, require_net=False)
    if state['regime'].startswith('trend_'):
        candidate = cost_check(base, policy, fee_points, slippage_points)
        candidate = dict(candidate, rule=RULES.version, method='trend_continuation')
        result['trend_only'] = candidate
        result['regime_switch'] = candidate.copy()
        return state, result
    if state['regime'] != 'range':
        return state, result
    completed = bars[bars.index + pd.Timedelta(minutes=5) <= now]
    prior, last = completed.iloc[-21:-1], completed.iloc[-1]
    center, std = float(prior.close.mean()), float(prior.close.std(ddof=0))
    lower, upper = center - RULES.range_band_std * std, center + RULES.range_band_std * std
    atr, entry = state['atr14'], float(quote['ltp'])
    long = last.low <= lower < last.close < center and last.close > last.open
    short = last.high >= upper > last.close > center and last.close < last.open
    if not long and not short:
        candidate = _unavailable('Range identified; wait for a completed rejection at a band', regime='range')
    elif abs(entry - last.close) > RULES.maximum_chase_atr * atr:
        candidate = _unavailable('Price moved away from the range rejection; no chase', regime='range')
    else:
        sign, side = (1, 'LONG') if long else (-1, 'SHORT')
        stop = (math.floor(min(prior.low.min(), last.low) - RULES.range_stop_buffer_atr * atr) if long else
                math.ceil(max(prior.high.max(), last.high) + RULES.range_stop_buffer_atr * atr))
        stop = min(stop, math.floor(entry - RULES.range_min_stop_atr * atr)) if long else max(stop, math.ceil(entry + RULES.range_min_stop_atr * atr))
        target = math.floor(center) if long else math.ceil(center)
        candidate = dict(status='ready', rule=RULES.version, regime='range', method='range_reversion',
                         side=side, entry=entry, stop=stop, target=target, atr14=atr,
                         confirmation_bar=state['confirmation_bar'], confirmed_at=state['confirmed_at'],
                         contract_expiry=quote['expiry'], as_of=str(now), quote_ts=quote['ts'],
                         trigger='completed_band_rejection', center=center, lower_band=lower, upper_band=upper,
                         stop_basis='Beyond the observed range extreme plus 0.2 ATR, at least 0.8 ATR from entry',
                         target_basis='Prior 20-candle mean; frozen and rounded inward')
        if (target - entry) * sign <= 0:
            candidate = dict(candidate, status='unavailable', reason='Price has already reached the range mean')
        candidate = cost_check(candidate, policy, fee_points, slippage_points)
    result['range_only'] = candidate
    result['regime_switch'] = candidate.copy()
    return state, result
