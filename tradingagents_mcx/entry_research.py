"""Fixed-rule entry comparisons on observed ticks, isolated from the trade book.

This is a diagnostic replay, not the AI council or a broker-fill backtest.
Rules are fixed before scoring; future bars never enter a proposal. No model
calls, configuration updates, signal issuance, or ledger writes occur here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import pandas as pd

from .economics import plan_economics
from .entries import continuation_candidate
from .indicators import compute_indicators
from .strategies import entry_allowed


MODELS = {
    'continuation_v1': 'Current continuation rule',
    'structural_v1': 'Same trigger, structural stop',
    'retest_v1': 'Completed retest + structural stop',
    'retest_net_v1': 'Retest + structural stop + net R/R floor',
}
PROTOCOL = dict(version='entry_research_v1', swing_bars=6, breakout_reference_bars=3,
    retest_tolerance_atr=.15, minimum_stop_atr=1.2, target_atr=2.4,
    maximum_quote_distance_atr=.5, warmup_bars=80, lookback_sessions=5,
    cadence='first observed tick in each five-minute bucket',
    position_limit=1, daily_entry_limit=3,
    net_floor='configured gross R/R floor applied after configured fees, only in retest_net_v1',
    exits='first subsequent observed same-contract price crossing stop/target or session square-off',
    missing_close='unresolved; never invent an exit at the last saved price',
    limitations=[
        'Mechanical candidates only: excludes AI council selection, model latency and approval.',
        'Historical CSVs lack exchange last-trade timestamps; receipt time is a proxy only.',
        'Sampled prices omit intervening extremes; observed gaps are flagged, not repaired.',
        'No slippage or API spending; configured round-trip fees frozen in this run.',
        'Each model runs independently with the same daily trade cap; trade samples differ.',
        'Retrospective diagnostics are not evidence of future profitability or automatic promotion.',
    ])


def protocol_hash():
    return hashlib.sha256(json.dumps(PROTOCOL,sort_keys=True).encode()).hexdigest()


def candidates(bars, quote, policy, now, fee_points):
    """Return fixed alternatives without modifying bars, quotes or active rules."""
    base=continuation_candidate(bars,quote,policy,now)
    out={'continuation_v1':base}
    unavailable=lambda why:dict(status='unavailable',reason=why)
    out.update({name:unavailable('No eligible completed setup') for name in MODELS if name not in out})
    if not quote or not entry_allowed(policy,now):return out
    quote_times=[]
    try:
        for value in (quote['ts'],quote.get('ltt') or quote['ts']):
            stamp=pd.Timestamp(value)
            if stamp.tzinfo:stamp=stamp.tz_convert('Asia/Kolkata').tz_localize(None)
            if not 0<=(now-stamp.to_pydatetime()).total_seconds()<=policy['max_quote_age_seconds']:return out
            quote_times.append(stamp)
        if str(quote['expiry'])[:10]!=bars.attrs.get('contract_expiry'):return out
        entry=float(quote['ltp'])
        if not math.isfinite(entry) or entry<=0:return out
    except (KeyError,TypeError,ValueError):return out
    completed=bars[bars.index+pd.Timedelta(minutes=5)<=now]
    if len(completed)<PROTOCOL['warmup_bars']:return out
    confirmed=completed.index[-1]+pd.Timedelta(minutes=5)
    if not 0<=(now-confirmed).total_seconds()<300 or any(stamp<confirmed for stamp in quote_times):return out
    ind=compute_indicators(completed);last=ind.iloc[-1];atr=float(last.atr14)
    if not math.isfinite(atr) or atr<=0:return out
    long=last.ma5>last.ma20>last.ma60 and last.close>=last.ma5 and last.macd>0 and last.macd_hist>0
    short=last.ma5<last.ma20<last.ma60 and last.close<=last.ma5 and last.macd<0 and last.macd_hist<0
    if not long and not short:return out
    side='LONG' if long else 'SHORT';sign=1 if long else -1
    if abs(entry-last.close)>PROTOCOL['maximum_quote_distance_atr']*atr or (entry-last.ma5)*sign<0:return out
    recent=completed.tail(PROTOCOL['swing_bars'])
    # A thirty-minute swing must not silently bridge an outage or session break.
    if len(recent)!=PROTOCOL['swing_bars'] or recent.index[0].date()!=now.date() or not (recent.index.to_series().diff().dropna()==pd.Timedelta(minutes=5)).all():return out
    stop=math.floor(min(recent.low.min()-1,entry-PROTOCOL['minimum_stop_atr']*atr)) if long else math.ceil(max(recent.high.max()+1,entry+PROTOCOL['minimum_stop_atr']*atr))
    target=math.floor(entry+PROTOCOL['target_atr']*atr) if long else math.ceil(entry-PROTOCOL['target_atr']*atr)
    try:economics=plan_economics(entry,stop,target,side,fee_points)
    except ValueError:return out
    common=dict(side=side,entry=entry,stop=stop,target=target,atr14=atr,
        confirmation_bar=str(completed.index[-1]),confirmed_at=str(confirmed),
        contract_expiry=str(quote['expiry'])[:10],reward_risk=economics['gross_reward_risk'],
        economics=economics,stop_basis='Beyond six completed same-session bars and at least 1.2 ATR',
        target_basis='Fixed 2.4 ATR projection; never extended to pass a floor')
    passes=economics['gross_reward_risk']>=float(policy['min_reward_risk'])
    if base['status']=='ready':
        out['structural_v1']=dict(common,status='ready' if passes else 'unavailable',
            reason='' if passes else 'Structural stop leaves insufficient gross reward/risk',trigger=base['trigger'])
    # Breakout must precede the confirming retest. Every referenced bar is complete.
    references=PROTOCOL['breakout_reference_bars']
    for i in range(len(completed)-2,max(references-1,len(completed)-PROTOCOL['swing_bars']-1),-1):
        previous=completed.iloc[i-references:i];breakout=completed.iloc[i];later=completed.iloc[i+1:]
        segment=completed.iloc[i-references:]
        if segment.index[0].date()!=now.date() or not (segment.index.to_series().diff().dropna()==pd.Timedelta(minutes=5)).all():continue
        level=float(previous.high.max() if long else previous.low.min())
        if (breakout.close-level)*sign<=0 or ((later.close-level)*sign<=0).any():continue
        if not last.low<=level+PROTOCOL['retest_tolerance_atr']*atr or not last.high>=level-PROTOCOL['retest_tolerance_atr']*atr:continue
        retest=dict(common,status='ready' if passes else 'unavailable',
            reason='' if passes else 'Retest stop leaves insufficient gross reward/risk',
            trigger='completed_breakout_retest',level=level,breakout_bar=str(completed.index[i]))
        out['retest_v1']=retest
        net_pass=passes and economics['net_reward_risk']>=float(policy['min_reward_risk'])
        out['retest_net_v1']=dict(retest,status='ready' if net_pass else 'unavailable',
            reason='' if net_pass else 'Retest fails the reward/risk floor after fees')
        break
    return out


def read_ticks(directory, symbol, as_of):
    frames=[];sources=[];rejected=0
    for path in sorted(Path(directory).glob(f'{symbol}_2*.csv')):
        # Never choose historical lookback files using future filenames.
        try:day=dt.datetime.strptime(path.stem.rsplit('_',1)[-1],'%Y%m%d').date()
        except ValueError:continue
        if day>as_of.date():continue
        data=path.read_bytes()
        from io import BytesIO
        frame=pd.read_csv(BytesIO(data))
        if not {'ts','ltp','expiry','vol_cum'}<=set(frame):
            rejected+=len(frame);continue
        frame['ts']=pd.to_datetime(frame.ts,errors='coerce')
        frame['ltp']=pd.to_numeric(frame.ltp,errors='coerce')
        frame['vol_cum']=pd.to_numeric(frame.vol_cum,errors='coerce')
        valid=frame.ts.notna() & (frame.ts<=as_of) & frame.ltp.map(lambda v:math.isfinite(v) and v>0)
        expiry=pd.to_datetime(frame.expiry,errors='coerce')
        valid &= expiry.notna() & (expiry.dt.date>=frame.ts.dt.date) & frame.vol_cum.map(lambda v:math.isfinite(v) and v>=0)
        rejected+=int((~valid & (frame.ts<=as_of)).sum())
        frame=frame[valid].copy()
        frame['expiry']=frame.expiry.astype(str).str[:10]
        frame=frame[frame.expiry>=frame.ts.dt.strftime('%Y-%m-%d')]
        frames.append(frame[['ts','ltp','expiry','vol_cum']])
        sources.append(dict(file=path.name,sha256=hashlib.sha256(data).hexdigest(),rows=len(frame)))
    ticks=pd.concat(frames,ignore_index=True).sort_values('ts').drop_duplicates(['ts','expiry']) if frames else pd.DataFrame(columns=['ts','ltp','expiry','vol_cum'])
    return ticks,dict(sources=sources,rejected_rows=rejected)


def bars_from_ticks(ticks):
    data=ticks.sort_values('ts').set_index('ts').copy()
    data['volume']=data.groupby(data.index.date).vol_cum.diff().clip(lower=0).fillna(0)
    bars=data.ltp.resample('5min').ohlc()
    bars['volume']=data.volume.resample('5min').sum()
    return bars.dropna()


def replay(ticks, policy, fee_points, start, end, as_of, split_date, daily_limit=3):
    """Walk observations in order; unfinished trades remain unresolved.

    Prebuilt bars are sliced strictly before their completion timestamp. Each
    indicator call sees only that prefix, with at most five observed sessions.
    """
    ticks=ticks[ticks.ts<=as_of].sort_values('ts').copy()
    by_contract={expiry:bars_from_ticks(part) for expiry,part in ticks.groupby('expiry')}
    dates=sorted(set(ticks.ts.dt.date));lookback={day:dates[max(0,i-PROTOCOL['lookback_sessions']+1)] for i,day in enumerate(dates)}
    trades={model:[] for model in MODELS};opened={model:None for model in MODELS}
    counts=Counter();candidates_seen=Counter();last_bucket=None;previous_time={}
    for tick in ticks.itertuples():
        now=tick.ts.to_pydatetime();day=now.date();px=float(tick.ltp)
        if day<start or day>end:continue
        previous=previous_time.get(tick.expiry);previous_time[tick.expiry]=now
        for model,trade in list(opened.items()):
            if trade is None:continue
            if day!=dt.date.fromisoformat(trade['opened_at'][:10]):
                trade.update(status='unresolved',reason='No same-session quote reached square-off');opened[model]=None;continue
            if tick.expiry!=trade['contract_expiry']:continue
            trade['max_observation_gap_seconds']=max(trade['max_observation_gap_seconds'],(now-dt.datetime.fromisoformat(trade['last_observed_at'])).total_seconds())
            trade.update(last_observed_at=str(now),last_price=px)
            sign=1 if trade['side']=='LONG' else -1
            reason=('square_off' if now>=dt.datetime.fromisoformat(trade['valid_until']) else
                    'stop_hit' if (px-trade['stop'])*sign<=0 else
                    'target_hit' if (px-trade['target'])*sign>=0 else None)
            if reason:
                gross=(px-trade['entry'])*sign;net=gross-fee_points
                trade.update(status='closed',closed_at=str(now),exit=px,close_reason=reason,
                    gross_points=gross,fees_points=fee_points,net_points=net,
                    net_r=net/(abs(trade['entry']-trade['stop'])+fee_points))
                opened[model]=None
        bucket=(tick.expiry,tick.ts.floor('5min'))
        if bucket==last_bucket or not entry_allowed(policy,now):continue
        last_bucket=bucket
        bars=by_contract[tick.expiry]
        bars=bars[(bars.index.date>=lookback[day]) & (bars.index+pd.Timedelta(minutes=5)<=now)].copy()
        bars.attrs['contract_expiry']=tick.expiry
        quote=dict(ts=str(now),ltt=str(now),ltp=px,expiry=tick.expiry)
        for model,candidate in candidates(bars,quote,policy,now,fee_points).items():
            if candidate['status']!='ready':continue
            candidates_seen[model]+=1
            if opened[model] is not None or (daily_limit and counts[(model,day)]>=daily_limit):continue
            trade={k:candidate[k] for k in ('side','entry','stop','target','contract_expiry','confirmation_bar','confirmed_at')}
            trade.update(model=model,opened_at=str(now),status='open',last_price=px,last_observed_at=str(now),
                valid_until=str(dt.datetime.combine(day,dt.time.fromisoformat(policy['square_off']))),
                max_observation_gap_seconds=0,entry_after_gap_seconds=(now-previous).total_seconds() if previous else None,
                net_reward_risk=plan_economics(px,trade['stop'],trade['target'],trade['side'],fee_points)['net_reward_risk'])
            trades[model].append(trade);opened[model]=trade;counts[(model,day)]+=1
    for model,trade in opened.items():
        if trade:
            trade.update(status='unresolved',reason='Input ends before an observed exit')
    summaries=[]
    for model,rows in trades.items():
        for period in ('all','before_split','after_split'):
            subset=[r for r in rows if period=='all' or (r['opened_at'][:10]<str(split_date))==(period=='before_split')]
            closed=[r for r in subset if r['status']=='closed'];n=len(closed)
            wins=sum(r['net_points']>0 for r in closed)
            gains=sum(max(0,r['net_points']) for r in closed);loss=-sum(min(0,r['net_points']) for r in closed)
            summaries.append(dict(model=model,label=MODELS[model],period=period,entries=len(subset),closed=n,
                unresolved=len(subset)-n,wins=wins,win_rate_pct=round(100*wins/n,2) if n else None,
                gross_points=round(sum(r['gross_points'] for r in closed),2),fees_points=round(n*fee_points,2),
                net_points=round(sum(r['net_points'] for r in closed),2),
                mean_net_r=round(sum(r['net_r'] for r in closed)/n,4) if n else None,
                profit_factor=round(gains/loss,4) if loss else None,
                gap_affected=sum(r['max_observation_gap_seconds']>policy['max_quote_age_seconds'] for r in closed)))
    return dict(protocol=PROTOCOL,protocol_sha256=protocol_hash(),as_of=str(as_of),
        start=str(start),end=str(end),split_date=str(split_date),fee_points=fee_points,daily_entry_limit=daily_limit,
        strategy_policy=policy,candidates_seen=dict(candidates_seen),summaries=summaries,trades=trades)
