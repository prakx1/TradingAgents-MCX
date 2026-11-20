"""Read-only intraday replay of rejected and cancelled council plans.

Reports and setup events are the durable source of truth. Replays never issue a
signal, call a broker, or write a trade/account/learning ledger. Only the veto is
removed: explicit entry conditions, fixed barriers and the original expiry stay.
Legacy reports without a complete executable plan remain visible as untestable.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
import json
import math
from pathlib import Path
import re

import pandas as pd

from .history import decision_reason, read_lines, resolved_time
from .setups import setup_clock, validate_setup
from .strategies import now_ist, strategy_config


VERSION = 'intraday_what_if_v1'
# A fixed comparison protocol, not account settings or optimized parameters.
FALLBACK_FEE_POINTS = 8.0
SLIPPAGE_POINTS = 1.0
MAX_GAP_SECONDS = 120


def exact_price(value):
    """Do not turn prose, ranges, WAIT or multiple targets into executable levels."""
    if isinstance(value, bool): return None
    text = str(value or '').replace(',', '').strip()
    match = re.fullmatch(r'(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:(?:rs\.?|inr)?\s*(?:/?bbl|per barrel))?', text, re.I)
    number = float(match[1]) if match else None
    return number if number is not None and math.isfinite(number) and number > 0 else None


def _stamp(report, path):
    stamp = report.get('report_timestamp') or '_'.join(path.stem.split('_')[-2:])
    try:
        time = dt.datetime.strptime(stamp, '%Y%m%d_%H%M%S' if len(stamp)==15 else '%Y%m%d_%H%M')
    except (ValueError, TypeError): return None
    # Older files only record a minute. Start after that whole minute has ended.
    if len(stamp)==13: time += dt.timedelta(minutes=1)
    signal = report.get('signal') or {}
    recorded=[signal.get('created_at'),signal.get('issued_at'),report.get('signal_decided_at'),
              (report.get('quote_recovery') or {}).get('checked_at')]
    return max([time]+[t for value in recorded if (t:=resolved_time(value))])


def _case(report, path, time):
    trader = report.get('trader') or {}
    policy = report.get('strategy_policy') or {}
    fee = (trader.get('paper_economics') or {}).get('fee_points')
    if isinstance(fee,bool) or not isinstance(fee,(int,float)) or not math.isfinite(fee) or fee<0: fee=None
    return dict(id=path.stem, decision_at=str(time),
                origin='Blocked' if (report.get('signal') or {}).get('status')=='blocked' else 'Rejected',
                source='Intraday' if report.get('strategy')=='intraday' else 'Legacy',
                report=str(path), symbol=report.get('symbol','CRUDEOIL'),
                side={'BULLISH':'LONG','BEARISH':'SHORT'}.get(trader.get('direction')),
                contract_expiry=report.get('contract_expiry'),
                decision=(report.get('cio') or {}).get('final_decision','Unavailable'),
                reason=decision_reason(report), proposed_entry=trader.get('entry'),
                stop=exact_price(trader.get('stop')), target=exact_price(trader.get('target')),
                entry_constraints=trader.get('entry_constraints'),
                plan=trader.get('setup'), rule_version=2,
                evidence_at=report.get('setup_evidence_as_of') or (report.get('decision_evidence') or {}).get('as_of'),
                session_open=policy.get('session_open','09:00'), entry_cutoff=policy.get('entry_cutoff','23:00'),
                square_off=policy.get('square_off','23:15'), session_close=policy.get('session_close','23:30'),
                fee_points=FALLBACK_FEE_POINTS if fee is None else fee,
                cost_basis='Comparison assumption: 8 points round trip' if fee is None else 'Fees saved with original proposal',
                slippage_points=SLIPPAGE_POINTS, protocol=VERSION)


@lru_cache(maxsize=4096)
def _report_case(path_text, modified, size):
    path=Path(path_text)
    try:
        report=json.loads(path.read_text())
        if not isinstance(report,dict) or report.get('kind') or report.get('strategy')=='long_term': return None
        if not report.get('trader') or not report.get('cio'): return None
        signal=report.get('signal') or {}
        if signal.get('status') in ('active','reaffirmed','closed','pending','activated'): return None
        cio=report['cio'].get('final_decision','')
        if not signal and ('LONG' in cio or 'SHORT' in cio): return None
        time=_stamp(report,path)
        return _case(report,path,time) if time else None
    except (OSError,ValueError,TypeError,KeyError): return None


def load_cases(cfg, as_of=None):
    as_of=as_of or now_ist();scfg=strategy_config(cfg,'intraday')
    root=Path(scfg['paths']['project_root'])
    folder=Path(scfg['paths']['reports_dir'])
    cases=[]
    for path in [*folder.glob('*.json'), *folder.parent.glob('*.json')]:
        stat=path.stat();case=_report_case(str(path),stat.st_mtime_ns,stat.st_size)
        if case and resolved_time(case['decision_at'])<=as_of: cases.append(dict(case))
    for setup in read_lines(root/'signals/intraday_setups.jsonl'):
        # Use the event time, not a later last-updated timestamp. Only cancellations
        # and replacements are counterfactual interventions; expiry is not renewed.
        event=next((e for e in setup.get('events',[]) if e.get('status') in ('cancelled','superseded')),None)
        cancelled=resolved_time(event.get('at')) if event else None
        created=resolved_time(setup.get('created_at'))
        if not cancelled or not created or cancelled>as_of: continue
        report_path=Path(setup.get('report_path') or root/'missing-report.md').with_suffix('.json')
        report={**(setup.get('authorization') or {}),'strategy':'intraday',
                'contract_expiry':setup.get('contract_expiry')}
        try: report=json.loads(report_path.read_text())
        except (OSError,ValueError): pass
        case=_case(report,report_path,created)
        case.update(id=setup['id'],origin='Cancelled' if event['status']=='cancelled' else 'Replaced',
                    decision_at=str(cancelled), monitor_from=str(created), cancelled_at=str(cancelled),
                    valid_until=setup.get('valid_until'), plan=setup.get('plan'), side=setup.get('side'),
                    contract_expiry=setup.get('contract_expiry'), rule_version=setup.get('rule_version',1),
                    reason=event.get('reason',''), decision=event['status'].upper())
        cases.append(case)
    return sorted(cases,key=lambda r:r['decision_at'],reverse=True)


def clean_ticks(ticks, expiry, day, as_of):
    if ticks is None or not {'ts','ltp','expiry'}<=set(ticks): return pd.DataFrame(columns=['ts','ltp'])
    data=ticks[['ts','ltp','expiry']].copy()
    data['ts']=pd.to_datetime(data.ts,errors='coerce')
    if data.ts.dt.tz is not None: data['ts']=data.ts.dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    data['ltp']=pd.to_numeric(data.ltp,errors='coerce')
    data=data[(data.expiry.astype(str).str[:10]==expiry)&(data.ts.dt.date==day)&(data.ts<=as_of)
              &data.ltp.map(lambda p:math.isfinite(p) and p>0)]
    return data.sort_values('ts').drop_duplicates('ts')


def replay(case, ticks, as_of=None):
    """One counterfactual, using observed contract prices after the saved decision."""
    as_of=as_of or now_ist()
    out={**case, 'status':'not_testable', 'outcome':'Not testable', 'note':'',
         'entry':None,'opened_at':None,'exit':None,'closed_at':None,'net_points':None,'outcome_r':None,
         'scored':False,'data_gap':False}
    def finish(status,note):
        out.update(status=status,note=note)
        if status=='no_entry': out['outcome']='No entry'
        elif status in ('watching','open'): out['outcome']='Still tracking'
        elif status=='incomplete': out['outcome']='Incomplete data'
        return out
    start=resolved_time(case.get('monitor_from') or case['decision_at'])
    if not start or start>as_of: return finish('not_testable','Decision is not yet available')
    expiry=case.get('contract_expiry')
    if not expiry or str(expiry)[:10]<start.date().isoformat():
        return finish('not_testable','No valid recorded contract expiry; prices from another contract are never substituted')
    side=case.get('side');sign=1 if side=='LONG' else -1
    if side not in ('LONG','SHORT'): return finish('not_testable','No directional proposal was recorded')
    try:
        clock=lambda key:dt.datetime.combine(start.date(),dt.time.fromisoformat(case[key]))
        cutoff,square,session_open,session_close=(clock(k) for k in ('entry_cutoff','square_off','session_open','session_close'))
        if not session_open<=start<cutoff: return finish('no_entry','Decision was outside the intraday entry window')
        plan=case.get('plan')
        if plan:
            plan=validate_setup(plan,side,0)
            until=resolved_time(case.get('valid_until')) or resolved_time(setup_clock(plan,case.get('evidence_at'),case['entry_cutoff'])['valid_until'])
            until=min(until,cutoff)
            stop,target=plan['stop'],plan['target']
        else:
            if exact_price(case.get('proposed_entry')) is None:
                return finish('not_testable','WAIT or prose without a complete machine-readable entry plan; no entry invented')
            stop,target=case.get('stop'),case.get('target');until=cutoff
        if stop is None or target is None or (target-stop)*sign<=0: raise ValueError('Missing or inconsistent fixed stop/target')
        constraints=case.get('entry_constraints')
        if constraints:
            if any(constraints.get(k)!=v for k,v in dict(side=side,contract_expiry=expiry,stop=stop,target=target).items()):
                raise ValueError('Saved entry constraints do not match the proposed contract and levels')
            valid=resolved_time(constraints.get('valid_until'));confirmed=resolved_time(constraints.get('confirmed_at'))
            if not valid or not confirmed or confirmed>start:
                raise ValueError('Saved candidate confirmation is incomplete or was not yet available')
            until=min(until,valid)
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        return finish('not_testable',str(exc))
    out.update(stop=stop,target=target)
    if until<=start: return finish('no_entry','Original setup lifetime had already expired')
    data=clean_ticks(ticks,str(expiry)[:10],start.date(),as_of)
    data=data[(data.ts>=session_open)&(data.ts<=session_close)]
    if data.empty: return finish('incomplete','No observed prices for this contract and session')
    bars=data.set_index('ts').ltp.resample('5min').ohlc().dropna()
    if len(bars):
        macd=bars.close.ewm(span=12,adjust=False).mean()-bars.close.ewm(span=26,adjust=False).mean()
        bars['hist_change']=(macd-macd.ewm(span=9,adjust=False).mean()).diff()
        bars['count']=range(1,len(bars)+1)
    bars=list(bars[bars.index>=pd.Timestamp(start).ceil('5min')].itertuples())
    phase='waiting_pullback' if plan and plan['trigger']=='pullback_reclaim' else 'waiting_close'
    bar_index=0;last_bar=None;previous=start;entry=None
    fee=float(case['fee_points']);slip=float(case['slippage_points'])
    samples=data[data.ts>start]
    for tick in samples.itertuples():
        stamp,price=tick.ts.to_pydatetime(),float(tick.ltp)
        if (stamp-previous).total_seconds()>MAX_GAP_SECONDS: out['data_gap']=True
        previous=stamp
        if entry is None:
            if out['data_gap']: return finish('incomplete','A gap before entry prevents verifying the trigger and earlier stop/target touches')
            if stamp>=until: return finish('no_entry','Original setup expired without a valid entry')
            if (price-stop)*sign<=0 or (price-target)*sign>=0:
                return finish('no_entry','Stop or target was reached before a valid entry')
            if plan:
                if case.get('rule_version',2)<2 and (price>plan['entry_max'] if sign==1 else price<plan['entry_min']):
                    return finish('no_entry','Original rule cancelled the plan when price passed its entry range')
                while bar_index<len(bars) and bars[bar_index].Index+pd.Timedelta(minutes=5)<=stamp:
                    bar=bars[bar_index];bar_index+=1;last_bar=bar
                    touches=bar.low<=plan['level']+plan['retest_tolerance'] and bar.high>=plan['level']-plan['retest_tolerance']
                    beyond=(bar.close-plan['level'])*sign
                    if phase=='waiting_pullback' and touches and beyond>0: phase='waiting_entry'
                    elif phase=='waiting_close' and beyond>0:
                        phase='waiting_retest' if plan['trigger']=='breakout_retest' else 'waiting_entry'
                    elif phase in ('waiting_retest','waiting_entry'):
                        if beyond<0: return finish('no_entry','A completed candle invalidated the trigger before entry')
                        if phase=='waiting_retest' and touches: phase='waiting_entry'
                if phase!='waiting_entry': continue
                if (price>plan['entry_max'] if sign==1 else price<plan['entry_min']):
                    return finish('no_entry','Price passed the approved range after confirmation; no chased fill')
                if last_bar is None or (stamp-(last_bar.Index+pd.Timedelta(minutes=5)).to_pydatetime()).total_seconds()>120: continue
                if plan['momentum']=='macd_improving':
                    if last_bar.count<35: return finish('not_testable','Insufficient same-session candle history to verify MACD confirmation')
                    if not math.isfinite(last_bar.hist_change) or last_bar.hist_change*sign<=0: continue
                if not plan['entry_min']<=price<=plan['entry_max']: continue
            elif (stamp-start).total_seconds()>MAX_GAP_SECONDS:
                return finish('incomplete','No observation close enough to the decision for an immediate entry')
            if case.get('cancelled_at') and stamp<resolved_time(case['cancelled_at']):
                return finish('not_testable','Replayed trigger preceded cancellation; not attributable to the cancellation')
            entry=price+sign*slip
            if (entry-stop)*sign<=0 or (target-entry)*sign<=0:
                return finish('no_entry','Adverse entry slippage puts the fill outside the fixed stop and target')
            out.update(entry=entry,opened_at=str(stamp),status='open',entry_observed_price=price)
            continue
        exit_price=price-sign*slip
        net=(exit_price-entry)*sign-fee
        out.update(exit=exit_price,last_observed_at=str(stamp),net_points=round(net,4),
                   outcome_r=round(net/(abs(entry-stop)+slip+fee),4))
        reason=('intraday_square_off' if stamp>=square else 'stop_hit' if (price-stop)*sign<=0
                else 'target_hit' if (price-target)*sign>=0 else None)
        if reason:
            out.update(closed_at=str(stamp),close_reason=reason)
            if out['data_gap']: return finish('incomplete','Observed exit after a data gap; an earlier barrier touch cannot be ruled out')
            out.update(scored=True,outcome='Missed winner' if net>0 else 'Avoided loss' if net<0 else 'Flat')
            return finish('closed',reason.replace('_',' '))
    if entry is not None:
        if as_of>=square or (as_of-previous).total_seconds()>MAX_GAP_SECONDS:
            return finish('incomplete','Waiting for an observed exit price; latest mark is not a completed profit or loss')
        return finish('open','Hypothetical trade still open; watching its fixed stop, target and intraday square-off')
    if as_of>=until:
        if previous<until-dt.timedelta(seconds=MAX_GAP_SECONDS):
            return finish('incomplete','Missing observations before setup expiry; no fill assumed')
        return finish('no_entry','No valid confirmation and entry before the original expiry')
    if (as_of-previous).total_seconds()>MAX_GAP_SECONDS:
        return finish('incomplete','Waiting for saved observations; no fill assumed')
    return finish('watching','Watching the original confirmation rules and entry range')


def evaluate_cases(cfg, cases, as_of=None):
    """Group file reads by session; historical files and accepted trades are untouched."""
    as_of=as_of or now_ist();folder=Path(cfg['paths']['cache_dir'])/'live';ticks={};result=[]
    for case in cases:
        date=resolved_time(case.get('monitor_from') or case['decision_at']).strftime('%Y%m%d')
        key=(case['symbol'],date)
        if key not in ticks:
            try: ticks[key]=pd.read_csv(folder/f'{key[0]}_{key[1]}.csv',usecols=['ts','ltp','expiry'])
            except (OSError,ValueError,pd.errors.EmptyDataError): ticks[key]=pd.DataFrame()
        result.append(replay(case,ticks[key],as_of))
    root=Path(strategy_config(cfg,'intraday')['paths']['project_root'])
    actual=[r for r in read_lines(root/'ledger/paper_intraday.jsonl') if r.get('status') in ('open','closed')
            and (stamp:=resolved_time(r.get('recorded_at') or r.get('opened_at'))) and stamp<=as_of]
    seen={}
    for row in result:
        row['comparison_eligible']=row['scored']
        if not row['opened_at']: continue
        matches=[r for r in actual if all(r.get(k)==row.get(k) for k in ('symbol','contract_expiry','side','stop','target'))
                 and (stamp:=resolved_time(r.get('opened_at')))
                 and abs((stamp-resolved_time(row['opened_at'])).total_seconds())<=MAX_GAP_SECONDS]
        key=tuple(row.get(k) for k in ('symbol','contract_expiry','side','opened_at','entry','stop','target'))
        if matches:
            row.update(actual_trade_ids=[r.get('trade_id') or r['id'] for r in matches],
                       hypothetical_outcome=row['outcome'],outcome='Also taken',comparison_eligible=False)
        elif key in seen:
            row.update(duplicate_of=seen[key],hypothetical_outcome=row['outcome'],
                       outcome='Same opportunity',comparison_eligible=False)
        seen.setdefault(key,row['id'])
    return result
