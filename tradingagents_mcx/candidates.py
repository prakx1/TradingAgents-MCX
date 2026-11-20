"""Shared numerical proposals for the scanner and council, never trade approvals."""
import json

import pandas as pd

from .economics import configured_fee_points, review_plan_costs
from .entries import candidate_context, continuation_candidate
from .regimes import RULES, regime_candidates


def build_candidates(cfg, bars, quote, now):
    base = continuation_candidate(bars, quote, cfg['strategy'], now)
    if base['status'] == 'ready':
        base['paper_economics'] = review_plan_costs(cfg, dict(base,
            direction='BULLISH' if base['side']=='LONG' else 'BEARISH'), quote)
    enabled = cfg.get('entry_models', {}).get('regime_enabled', False)
    pool = dict(version='council_candidates_v1', as_of=str(now), regime_enabled=bool(enabled),
                candidates={'continuation':base})
    if enabled:
        state, proposals = regime_candidates(bars, quote, cfg['strategy'], now, configured_fee_points(cfg))
        pool['regime'] = state
        pool['candidates']['regime'] = proposals['regime_switch']
    return pool


def preferred_candidate(pool):
    # A qualifying regime proposal includes the stronger cost check. Preserve
    # the established continuation path when the experiment has no proposal.
    candidates = pool['candidates']
    regime = candidates.get('regime', {})
    if regime.get('status') == 'ready': return regime
    if candidates['continuation']['status'] == 'ready': return candidates['continuation']
    return regime or candidates['continuation']


def proposals_context(pool):
    text = candidate_context(pool['candidates']['continuation'])
    if not pool['regime_enabled']: return text
    return (text + '\n\n[REGIME CANDIDATE — experimental council proposal]\n' + json.dumps(pool) + '\n'
        'This fixed-rule trend/range experiment is now available for review. Its historical replay '
        'did not establish a profit edge; no success probability is known. A ready range proposal '
        'already has a completed band-rejection candle and targets the prior mean. A trend proposal '
        'uses the fixed continuation rules. Evaluate every ready proposal against the analyst vote '
        'and current evidence; do not add a redundant future pullback just because it is not the '
        'usual structural entry. If accepted, use its exact entry, stop and target. Do not extend '
        'targets or waive costs. Its 1.5:1 net floor includes configured fees plus one point of '
        'adverse slippage each way and is rechecked at signal time. Mixed or shock regimes do not '
        'authorize a regime trade. This is a proposal, not consensus or risk approval. Reject with '
        'a specific reason when unsupported. The existing council strategy remains available.')


def annotate_selection(trader, pool):
    from .signals import price_level
    trader['entry_method'] = 'council_levels'
    for name in ('regime', 'continuation'):
        candidate = pool['candidates'].get(name, {})
        if (candidate.get('status') != 'ready'
                or trader.get('direction') != ('BULLISH' if candidate['side']=='LONG' else 'BEARISH')
                or not all(price_level(trader.get(k)) == candidate[k] for k in ('entry','stop','target'))):
            continue
        trader['entry_method'] = (candidate['rule'] + ':' + candidate['method'] if name=='regime' else candidate['rule'])
        trader['entry_candidate'] = candidate
        if name == 'regime':
            trader['entry_constraints'] = dict(rule=RULES.version, side=candidate['side'],
                contract_expiry=candidate['contract_expiry'], confirmed_at=candidate['confirmed_at'],
                valid_until=str(pd.Timestamp(candidate['confirmed_at']) + pd.Timedelta(minutes=5)),
                stop=candidate['stop'], target=candidate['target'],
                minimum_net_reward_risk=RULES.minimum_net_reward_risk,
                fee_points=candidate['fee_points'], slippage_points_each_way=candidate['slippage_points_each_way'])
        return
