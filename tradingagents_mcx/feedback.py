"""Deterministic feedback from completed paper trades and recorded entry events.

Read-only, point-in-time and free of additional model calls or automatic tuning.
Every council archives the returned evidence in its existing report.
"""
from collections import Counter
import datetime as dt
import math
from pathlib import Path

from .economics import plan_economics
from .history import read_lines, resolved_time, decision_history
from .strategies import strategy_config


def intraday_feedback(cfg, as_of):
    root = Path(strategy_config(cfg, 'intraday')['paths']['project_root'])
    rows = {r.get('id'):r for r in read_lines(root/'ledger/paper_intraday.jsonl') if r.get('id')}
    trades, excluded = [], 0
    for row in rows.values():
        if row.get('origin') != 'intraday' or row.get('status') != 'closed':
            continue
        opened, closed = (resolved_time(row.get(k)) for k in ('opened_at', 'closed_at'))
        available = resolved_time(row.get('recovered_at') or row.get('recorded_at'))
        if not opened or not closed or not available or not opened <= closed <= as_of or available > as_of:
            continue
        if opened.date() != closed.date():
            continue
        try:
            entry, stop, target, exit_price, lots, size, fees, net = (
                float(row[k]) for k in ('entry','stop','target','exit','lots','lot_size_bbl','fees_inr','pnl_inr'))
            if not all(math.isfinite(x) for x in (entry,stop,target,exit_price,lots,size,fees,net)) or min(lots,size,exit_price) <= 0:
                raise ValueError('Invalid recorded economics')
            economics = plan_economics(entry,stop,target,row['side'],fees/(lots*size))
            sign = 1 if row['side']=='LONG' else -1
            gross = (exit_price-entry)*sign*lots*size
            if not math.isclose(gross-fees,net,abs_tol=.011):
                raise ValueError('Inconsistent recorded P&L')
            gap = max(0,(stop-exit_price)*sign) if row.get('close_reason')=='stop_hit' else 0
        except (TypeError,ValueError,KeyError):
            excluded += 1
            continue
        trades.append(dict(id='L-'+row['trade_id'],trade_id=row['trade_id'],signal_id=row.get('signal_id'),
            opened_at=row['opened_at'],closed_at=row['closed_at'],side=row['side'],
            entry_method=row.get('entry_method','council_levels'),close_reason=row.get('close_reason'),
            holding_seconds=round((closed-opened).total_seconds(),1),
            rapid_stop=row.get('close_reason')=='stop_hit' and (closed-opened).total_seconds()<=300,
            net_pnl_inr=net,gross_pnl_inr=round(gross,2),fees_inr=fees,
            planned_net_reward_risk=economics['net_reward_risk'],
            net_outcome_r=round(net/(economics['net_risk_points']*lots*size),4),stop_gap_points=gap))
    trades.sort(key=lambda r:r['closed_at'])
    n = len(trades)
    net = sum(r['net_pnl_inr'] for r in trades)
    fees = sum(r['fees_inr'] for r in trades)
    gross = sum(r['gross_pnl_inr'] for r in trades)
    summary = dict(closed=n,wins=sum(r['net_pnl_inr']>0 for r in trades),net_pnl_inr=round(net,2),
        gross_pnl_inr=round(gross,2),fees_inr=round(fees,2),average_net_inr=round(net/n,2) if n else None,
        fee_share_of_gross_pct=round(fees/gross*100,1) if gross>0 else None,excluded_records=excluded)
    losses=-sum(min(0,r['net_pnl_inr']) for r in trades)
    gains=sum(max(0,r['net_pnl_inr']) for r in trades)
    summary.update(rapid_stops=sum(r['rapid_stop'] for r in trades),
        win_rate_pct=round(100*summary['wins']/n,1) if n else None,
        profit_factor=round(gains/losses,3) if losses else None,
        below_one_net_rr=sum(r['planned_net_reward_risk']<1 for r in trades))
    # A bounded observation window; outcomes remain complete above.
    since = max(as_of-dt.timedelta(days=30), min((resolved_time(r['opened_at']) for r in trades),default=as_of-dt.timedelta(days=30)))
    statuses = Counter(); target_before = 0; setup_ids = []
    for setup in read_lines(root/'signals/intraday_setups.jsonl'):
        created = resolved_time(setup.get('created_at'))
        if not created or not since <= created <= as_of:
            continue
        events = [e for e in setup.get('events',[]) if resolved_time(e.get('at')) and resolved_time(e['at']) <= as_of]
        if events:
            # Multiple transitions can occur in one guard tick; last write wins
            # when timestamps tie, just as in the durable event sequence.
            last = sorted(events,key=lambda e:resolved_time(e['at']))[-1]
        elif resolved_time(setup.get('updated_at')) and resolved_time(setup['updated_at']) <= as_of:
            last = setup
        else:
            continue
        statuses[last.get('status','unknown')] += 1
        target_before += last.get('reason') == 'Target reached before the setup could issue a signal'
        setup_ids.append(setup['id'])
    reviews = [r for r in decision_history(cfg) if since <= resolved_time(r['timestamp']) <= as_of and r['status']!='preview']
    stale = [r for r in reviews if r['status']=='blocked' and 'stale' in r.get('reason','').lower()]
    entries = dict(since=since.isoformat(sep=' ',timespec='seconds'),setup_statuses=dict(statuses),
                   target_before_entry=target_before,review_count=len(reviews),stale_quote_blocks=len(stale),
                   setup_ids=setup_ids,stale_quote_reports=[r['report'] for r in stale])
    lines = ['[NEW INTRADAY PAPER FEEDBACK — separate from legacy and gross signal R]',
        f"{n} completed trades; Net paper P&L INR {net:+.2f}; gross INR {gross:+.2f}; recorded fees INR {fees:.2f}. API spend is separate.",
        'Use recorded fees from each trade, never today’s prices to reprice history. These outcomes already correspond to signal outcomes above; do not double-count them.']
    if n:
        lines.append('This is observational evidence, not a proven edge. Do not choose a direction, change thresholds or optimize targets from a small sample.')
        lines.append(f"[ENTRY QUALITY] {summary['rapid_stops']} trades stopped within five minutes; "
            f"{summary['below_one_net_rr']} entries offered less than 1:1 planned reward/risk after fees. "
            'Rapid stop-outs are a timing/placement diagnostic, not proof that wider stops or extra confirmation improve results. '
            'For the current proposal explicitly compare its stop with recent completed-bar swing levels and volatility, '
            'including any change in distance at the latest observed entry price. '
            'If a structural stop makes the supported target uneconomic, assess a fully specified pullback/retest or WAIT; '
            'never stretch the target or widen an existing trade’s stop to manufacture a better outcome.')
    for r in list(reversed(trades))[:6]:
        lines.append(f"[{r['id']}] {r['entry_method']} · {r['side']} · {r['close_reason']} · "
            f"net INR {r['net_pnl_inr']:+.0f}, fees INR {r['fees_inr']:g}, planned net reward/risk {r['planned_net_reward_risk']:.2f}:1, "
            f"net outcome {r['net_outcome_r']:+.2f}R including initial costs; observed stop gap {r['stop_gap_points']:g} points. "
            f"Held {r['holding_seconds']:g} seconds{' (stop within five minutes)' if r['rapid_stop'] else ''}. "
            'Review net economics and gap exposure; the result alone does not prove the setup was good or bad.')
    lines += [f"[ENTRY FEEDBACK since {entries['since']}] {len(setup_ids)} conditional plans: {dict(statuses)}. "
              f"{target_before} hit their target before entry; {len(stale)} of {len(reviews)} completed non-preview reviews were blocked by stale quotes.",
              'Missed targets are not counterfactual profits. Consider a confirmed immediate setup only when present evidence supports it; '
              'do not add redundant confirmation if already satisfied, chase a missed price, relax freshness, or widen targets to force entry.']
    if excluded:
        lines.append(f'{excluded} malformed/inconsistent paper records excluded from this feedback; originals retained.')
    return dict(version='intraday_feedback_v2',as_of=as_of.isoformat(timespec='seconds'),summary=summary,
                trades=trades,entries=entries,lesson_ids=[r['id'] for r in list(reversed(trades))[:6]],context='\n'.join(lines))
