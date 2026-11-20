"""Deterministic intraday proposals for council review, never automatic approvals.

The versioned rule uses fixed volatility multipliers. It does not optimize them
against saved outcomes, extend targets to pass a floor, or write signal records.
"""
from __future__ import annotations

import json
import math

import pandas as pd

from .indicators import compute_indicators
from .strategies import entry_allowed


def continuation_candidate(bars, quote, policy, now):
    unavailable=lambda reason,**data:dict(status='unavailable',rule='trend_continuation_v1',reason=reason,**data)
    if not entry_allowed(policy,now):return unavailable('Intraday entry window is closed')
    if not quote:return unavailable('No fresh quote for an immediate entry proposal')
    try:
        quote_times=[]
        for value in (quote['ts'],quote.get('ltt') or quote['ts']):
            stamp=pd.Timestamp(value)
            if stamp.tzinfo:stamp=stamp.tz_convert('Asia/Kolkata').tz_localize(None)
            if not 0<=(now-stamp.to_pydatetime()).total_seconds()<=policy['max_quote_age_seconds']:
                return unavailable('The quote is stale or in the future')
            quote_times.append(stamp)
        if bars.attrs.get('contract_expiry')!=str(quote['expiry'])[:10]:
            return unavailable('Quote and candle contracts differ')
        entry=float(quote['ltp'])
        if not math.isfinite(entry) or entry<=0:return unavailable('Invalid quote price')
    except (KeyError,TypeError,ValueError):return unavailable('Incomplete quote metadata')
    completed=bars[bars.index+pd.Timedelta(minutes=5)<=now]
    if len(completed)<80:return unavailable('At least 80 completed candles are required')
    confirmed=completed.index[-1]+pd.Timedelta(minutes=5)
    if (now-confirmed).total_seconds()>=300:return unavailable('Completed candles are stale')
    if any(stamp<confirmed for stamp in quote_times):return unavailable('Quote predates completed confirmation')
    ind=compute_indicators(completed)
    last=ind.iloc[-1];atr=float(last.atr14)
    if not math.isfinite(atr) or atr<=0:return unavailable('ATR is unavailable')
    long=(last.ma5>last.ma20>last.ma60 and last.close>=last.ma5 and last.macd>0 and last.macd_hist>0)
    short=(last.ma5<last.ma20<last.ma60 and last.close<=last.ma5 and last.macd<0 and last.macd_hist<0)
    if not long and not short:return unavailable('Trend and momentum are not aligned')
    side='LONG' if long else 'SHORT';sign=1 if long else -1
    previous=completed.iloc[-4:-1]
    breakout=last.close>previous.high.max() if long else last.close<previous.low.min()
    reclaim=last.low<=last.ma5<=last.close if long else last.high>=last.ma5>=last.close
    if not breakout and not reclaim:return unavailable('No completed breakout or MA5 reclaim')
    if abs(entry-last.close)>.5*atr or (entry-last.ma5)*sign<0:
        return unavailable('Live price moved away from the confirmed entry area; no chase')
    # MCX CRUDEOIL tick is one INR/bbl. Round risk outward and reward inward.
    stop=(math.floor(min(last.low-1,entry-1.2*atr)) if long
          else math.ceil(max(last.high+1,entry+1.2*atr)))
    target=math.floor(entry+2.4*atr) if long else math.ceil(entry-2.4*atr)
    rr=abs(target-entry)/abs(entry-stop)
    values=dict(side=side,entry=entry,stop=stop,target=target,reward_risk=round(rr,3),
        atr14=round(atr,4),confirmation_bar=str(completed.index[-1]),confirmed_at=str(confirmed),
        as_of=str(now),quote_ts=quote['ts'],quote_ltt=quote.get('ltt'),contract_expiry=quote['expiry'],
        trigger='completed_3bar_breakout' if breakout else 'completed_ma5_reclaim',
        target_basis='2.4 × completed-bar ATR; projected objective, not observed resistance',
        stop_basis='Beyond the confirmation candle and at least 1.2 × ATR from entry',
        rules='MA5/20/60 and MACD aligned; completed breakout or MA5 reclaim; fresh quote within 0.5 ATR of that close')
    if stop<=0 or target<=0:return unavailable('Invalid projected levels',**values)
    if rr<float(policy['min_reward_risk']):return unavailable('Fixed-rule reward/risk is below the configured floor',**values)
    return dict(status='ready',rule='trend_continuation_v1',**values)


def candidate_context(candidate):
    return ('[DETERMINISTIC TREND-CONTINUATION CANDIDATE — proposal for review]\n'
            +json.dumps(candidate)+'\n'
            'This is a separate predefined continuation method alongside structural pullbacks. Its '
            'target is a fixed 2.4-ATR projection, not a claim of observed resistance or proven profitability. '
            'Do not enlarge it to pass a reward/risk floor. If ready, its price pattern has already confirmed; '
            'assess a numeric entry now using the module vote and current risks instead of automatically '
            'requiring another future pullback. A nearby old high or high RSI is a risk to evaluate, '
            'not alone proof that an aligned trend cannot continue. Accept or reject with specific evidence. '
            'Only council consensus, risk/CIO review and final live-price checks can create a signal. '
            'If accepted, copy the exact candidate stop/target and identify the ATR target basis. '
            'Otherwise assess a different supported setup or WAIT. Never force a trade.')
