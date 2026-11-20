"""Per-unit paper economics, independent of capital and position sizing."""
import math


def plan_economics(entry, stop, target, side, fee_points):
    entry, stop, target, fee_points = map(float, (entry, stop, target, fee_points))
    if not all(math.isfinite(x) for x in (entry, stop, target, fee_points)) or fee_points < 0:
        raise ValueError('Invalid price or cost')
    if min(entry, stop, target) <= 0 or side not in ('LONG', 'SHORT'):
        raise ValueError('Invalid price or direction')
    if not (stop < entry < target if side == 'LONG' else target < entry < stop):
        raise ValueError('Stop and target do not bracket entry')
    risk, reward = abs(entry-stop), abs(target-entry)
    loss, gain = risk+fee_points, reward-fee_points
    return dict(gross_reward_risk=round(reward/risk, 4), fee_points=fee_points,
                net_risk_points=loss, net_reward_points=gain,
                net_reward_risk=round(gain/loss, 4),
                breakeven_win_rate_pct=round(100*loss/(risk+reward), 2) if gain > 0 else None)


def configured_fee_points(cfg):
    from .paper import paper_settings
    p = paper_settings(cfg)
    return p['roundtrip_cost_per_lot_inr']/p['lot_size_bbl']


def recorded_economics(trade):
    """Display old rows using their frozen costs, never current settings."""
    try:
        size = float(trade['lots'])*float(trade['lot_size_bbl'])
        if not math.isfinite(size) or size <= 0:
            return None
        costs=float(trade['fees_inr'])/size
        if trade.get('origin')=='experimental':costs+=float(trade['slippage_points_each_way'])
        return plan_economics(trade['entry'],trade['stop'],trade['target'],trade['side'],costs)
    except (KeyError,TypeError,ValueError,ZeroDivisionError):
        return None


def cost_context(cfg):
    fee = configured_fee_points(cfg)
    return (f'[KNOWN PAPER EXECUTION COSTS]\nRound-trip simulation cost = {fee:g} INR per barrel. '
            'For every proposal also evaluate net reward/risk = (abs(target-entry)-cost) / '
            '(abs(entry-stop)+cost). Gross signal R does not include fees. '
            'Explain weak net economics in the entry/risk decision; do not describe configured paper costs as unknown. '
            'Costs are simulation assumptions, not verified broker charges. Actual observed stop gaps can worsen losses; '
            'no slippage allowance or API spending is included in this calculation. '
            'Keep supported stop/target levels fixed; never stretch a target to hide costs. '
            'This evidence does not change the configured signal floor or authorize any entry.')


def review_plan_costs(cfg, trader, quote=None):
    from .signals import price_level
    side = {'BULLISH':'LONG', 'BEARISH':'SHORT'}.get(trader.get('direction'))
    setup = trader.get('setup')
    numeric = price_level(trader.get('entry'))
    if numeric is None and isinstance(setup, dict):
        prices = [setup.get('entry_min'), setup.get('entry_max')]
        stop, target = setup.get('stop'), setup.get('target')
        basis = 'approved range endpoints; conditional, not filled'
    else:
        prices = [quote['ltp'] if quote else numeric]
        stop, target = (price_level(trader.get(k)) for k in ('stop', 'target'))
        basis = 'fresh observed quote' if quote else 'proposed entry only; no usable live quote'
    try:
        variants = [dict(entry=float(px), **plan_economics(px,stop,target,side,configured_fee_points(cfg))) for px in prices]
    except (TypeError, ValueError):
        return dict(status='unavailable', reason='No valid entry/stop/target geometry for a cost review')
    worst = min(variants, key=lambda r:r['net_reward_risk'])
    return dict(status='calculated', basis=basis, **worst,
                below_gross_signal_floor=worst['gross_reward_risk'] < cfg['strategy']['min_reward_risk'],
                net_below_same_ratio=worst['net_reward_risk'] < cfg['strategy']['min_reward_risk'],
                quote_ts=quote.get('ts') if quote else None,
                assumptions='Fixed configured paper fees; excludes stop gaps, slippage and API costs. Advisory review; not an approval.')
