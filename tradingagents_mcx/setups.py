"""Council-approved conditional signals, monitored without another model call.

Only explicit, jointly approved conditions are executable. No prose is converted
to triggers and no past entry is backfilled. All setup outcomes remain on disk.
"""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import json
import math
import uuid
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from .signals import SignalBook, latest_quote, price_level
from .strategies import entry_allowed, now_ist


SETUP_CONTRACT = """CONDITIONAL SIGNAL MONITOR:
When a grounded plan needs future price confirmation, output ENTRY: WAIT and a
SETUP JSON object on ONE line. The monitor checks every 10 seconds after approval.
Schema (all fields required, no extra fields):
{"trigger":"breakout_retest","level":8000,"entry_min":8001,"entry_max":8005,"stop":7980,"target":8050,"retest_tolerance":2,"valid_for_minutes":60,"momentum":"none"}
The numbers above illustrate syntax only; derive every actual level from evidence.
Supported triggers:
- close_beyond: a new completed 5-minute candle closes strictly above level for
  LONG, below for SHORT. Then a fresh quote must be inside entry_min..entry_max.
- breakout_retest: that close, then a LATER completed 5-minute candle trades
  within retest_tolerance of level and closes on the breakout side, then a fresh
  quote in the entry range. A subsequent close through the level cancels the plan.
- pullback_reclaim: wait for a new completed 5-minute candle to trade within
  retest_tolerance of level and close strictly above it for LONG or below it for
  SHORT. Then require a fresh quote in the entry range. This supports buying a
  support pullback or selling a resistance rebound; no prior breakout is needed.
Only full candles starting at/after approval are used; never replay past entries.
momentum must be none or macd_improving (completed-bar MACD histogram increases
for LONG, decreases for SHORT). No other discretionary conditions can be monitored.
valid_for_minutes: 10..120 from the ORIGINAL TRADER evidence timestamp, capped by
today's cutoff. The engine supplies a canonical setup clock to risk and CIO.
Later quote/evidence refreshes do not move that anchor. Use the engine's expiry;
do not reject a plan by recalculating it from a newer review timestamp.
Both risk and CIO must approve the EXACT JSON with SETUP APPROVAL: APPROVE.
The complete conditional plan must be represented in this JSON. If the plan needs
additional confirmation (news, volume, moving averages, manual judgment, etc.),
output SETUP: null; do not silently omit a required condition. Reviewers must reject
JSON that omits a condition required in prose. Do not add conditions only in prose.
The entry range must lie beyond level in the direction of the signal, bracketed
by stop/target, and meet minimum reward/risk at its WORST entry price. Targets
must come from evidence, never be extended just to pass. Before confirmation,
price may be outside the entry range while waiting for the required retest or
pullback. A stop or target touch cancels the setup at any stage. AFTER confirmation,
a quote beyond the worst approved entry cancels it; never chase or widen levels.
Use a meaningful evidence-supported range where possible. An exact one-tick
entry is permissible but fragile; explain that limitation without inventing targets.
For an immediately confirmed numeric ENTRY or no supported setup: SETUP: null.
"""


def setup_clock(plan, evidence_as_of, entry_cutoff):
    """One clock for the proposal, reviewers and monitor registration."""
    minutes=plan.get('valid_for_minutes')
    if isinstance(minutes,bool) or not isinstance(minutes,(int,float)) or not math.isfinite(minutes) or not 10<=minutes<=120:
        raise ValueError('Invalid setup duration')
    anchor=dt.datetime.fromisoformat(evidence_as_of)
    until=min(anchor+dt.timedelta(minutes=minutes),dt.datetime.combine(anchor.date(),dt.time.fromisoformat(entry_cutoff)))
    return dict(evidence_as_of=str(anchor),valid_for_minutes=minutes,valid_until=str(until),basis='original trader evidence')


def validate_setup(raw, side, minimum_rr):
    keys = {'trigger','level','entry_min','entry_max','stop','target',
            'retest_tolerance','valid_for_minutes','momentum'}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError('Conditional setup is missing fields or contains unsupported conditions')
    if raw['trigger'] not in ('close_beyond','breakout_retest','pullback_reclaim') or raw['momentum'] not in ('none','macd_improving'):
        raise ValueError('Unsupported setup confirmation rule')
    p = dict(raw)
    for key in keys - {'trigger','momentum'}:
        if isinstance(p[key], bool) or not isinstance(p[key], (float,int)) or not math.isfinite(p[key]):
            raise ValueError(f'Invalid setup number: {key}')
    if not 10 <= p['valid_for_minutes'] <= 120 or p['retest_tolerance'] < 0:
        raise ValueError('Invalid setup duration or retest tolerance')
    if min(p[k] for k in ('level','entry_min','entry_max','stop','target')) <= 0 or p['entry_min'] > p['entry_max']:
        raise ValueError('Invalid setup price range')
    lo, hi, stop, target, level = (p[k] for k in ('entry_min','entry_max','stop','target','level'))
    if not (stop < level <= lo <= hi < target if side == 'LONG' else target < lo <= hi <= level < stop):
        raise ValueError('Setup levels do not match its direction')
    if p['retest_tolerance'] >= abs(level-stop):
        raise ValueError('Retest tolerance reaches the invalidation level')
    worst = hi if side == 'LONG' else lo
    ratio = abs(target-worst)/abs(worst-stop)
    if ratio < minimum_rr:
        raise ValueError(f'Conditional entry range reward/risk {ratio:.2f}:1 is below {minimum_rr}:1')
    return p


class PendingSetups:
    def __init__(self, cfg):
        self.cfg = cfg
        self.policy = cfg['strategy']
        self.path = Path(cfg['paths']['signals_dir']) / f"{self.policy['id']}_setups.jsonl"
        self.path.parent.mkdir(parents=True,exist_ok=True)

    @contextmanager
    def locked(self):
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            yield

    def load(self):
        return [json.loads(s) for s in self.path.read_text().splitlines() if s.strip()] if self.path.exists() else []

    def context(self):
        pending=[{k:r.get(k) for k in ('id','side','created_at','valid_until','phase','plan')}
                 for r in self.load() if r['status']=='pending']
        return ('[EXISTING PENDING SETUPS]\n'+json.dumps(pending)+'\n'
                'These are already authorized for automatic monitoring. A new WAIT or STAND ASIDE '
                'does not revoke them. Review their exact conditions against the new evidence. '
                'The CIO may explicitly cancel a listed setup by ID with an evidence-based reason; '
                'otherwise it retains its original levels and expiry. A valid new signal/setup replaces it.')

    def save(self, rows):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in rows))
        temp.replace(self.path)

    @staticmethod
    def update(row, now, status=None, phase=None, reason=''):
        row.update(updated_at=str(now),reason=reason)
        if status:
            row['status'] = status
        if phase:
            row['phase'] = phase
        event = dict(status=row['status'],phase=row['phase'],reason=reason)
        previous = row.setdefault('events',[])
        if not previous or any(previous[-1].get(k) != v for k,v in event.items()):
            previous.append(dict(at=str(now),**event))

    def register(self, result, now=None):
        """Publish a council result atomically against the live setup monitor."""
        now = now or now_ist()
        blocked = lambda reason: dict(status='blocked',strategy='intraday',reason=reason)
        if self.policy['id'] != 'intraday' or result.get('kind') == 'night-watch':
            return blocked('Only an intraday signal council can arm a setup')
        with self.locked():
            rows = self.load()
            for old in rows:
                if old['status'] == 'pending' and old.get('replacement_policy')!='explicit_or_valid_replacement':
                    self.update(old,now,status='superseded',reason='A newer council decision replaced this pending plan')
                elif (old['status']=='pending' and result['cio'].get('pending_setup_action')=='CANCEL'
                        and result['cio'].get('pending_setup_id')==old['id']
                        and result['cio'].get('pending_setup_reason')
                        and result.get('entry_eligible') is True
                        and not any(result.get(role,{}).get('degraded') for role in ('risk','cio'))):
                    self.update(old,now,status='cancelled',reason='Council cancelled this setup: '+result['cio']['pending_setup_reason'])
            if rows:
                self.save(rows)
            def supersede_pending():
                for old in rows:
                    if old['status']=='pending':
                        self.update(old,now,status='superseded',reason='A valid new signal or setup replaced this pending plan')
            trader = result['trader']
            if price_level(trader.get('entry')) is not None or not trader.get('setup'):
                issued=SignalBook(self.cfg).issue(result,now)
                if issued['status'] in ('active','reaffirmed','closed'):
                    supersede_pending()
                    if rows:self.save(rows)
                return issued
            verdict = result['cio']['final_decision']
            side = 'LONG' if 'LONG' in verdict else 'SHORT' if 'SHORT' in verdict else None
            if side is None:
                return SignalBook(self.cfg).issue(result,now)
            if trader.get('direction') != ('BULLISH' if side=='LONG' else 'BEARISH'):
                return blocked('Conditional setup has no matching directional approval')
            if any(result.get(role,{}).get('setup_approval') != 'APPROVE' or result.get(role,{}).get('degraded')
                   for role in ('risk','cio')):
                return blocked('Conditional setup requires explicit risk and CIO approval of every condition')
            if result.get('entry_eligible') is not True or not self.policy.get('enabled') or not entry_allowed(self.policy,now):
                return blocked('Conditional setup cannot arm: data eligibility, enabled strategy and open entry window required')
            quote,error = latest_quote(self.cfg,now)
            if not quote:
                return blocked(error)
            if result.get('contract_expiry') != str(quote['expiry'])[:10]:
                return blocked('Setup contract is missing or differs from the live contract')
            try:
                plan = validate_setup(trader['setup'],side,float(self.policy['min_reward_risk']))
                timing=setup_clock(plan,result.get('setup_evidence_as_of') or result['decision_evidence']['as_of'],self.policy['entry_cutoff'])
                as_of=dt.datetime.fromisoformat(timing['evidence_as_of'])
                until=dt.datetime.fromisoformat(timing['valid_until'])
                if as_of > now or as_of.date()!=now.date() or until <= now:
                    raise ValueError('Conditional plan expired before approval; a fresh review is required')
            except (ValueError,KeyError,TypeError) as e:
                return blocked(str(e))
            # Repeating the same authorization must not erase a breakout/retest
            # already observed or move the original approval and expiry clocks.
            for old in rows:
                if (old['status']=='pending' and old.get('rule_version',1)>=2
                        and old.get('replacement_policy')=='explicit_or_valid_replacement'
                        and old['symbol']==result['symbol'] and old['side']==side
                        and old['contract_expiry']==result['contract_expiry'] and old['plan']==plan
                        and dt.datetime.fromisoformat(old['valid_until'])>now):
                    old.setdefault('reaffirmations',[]).append(dict(at=str(now),report_path=result.get('report_path')))
                    self.save(rows)
                    return {**{k:v for k,v in old.items() if k not in ('authorization','events','reaffirmations')},
                            'reaffirmed':True}
            row = dict(id=f"setup-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",strategy='intraday',
                       symbol=result['symbol'],side=side,contract_expiry=result['contract_expiry'],
                       created_at=str(now),valid_until=str(until),status='pending',rule_version=2,
                       replacement_policy='explicit_or_valid_replacement',
                       phase='waiting_pullback' if plan['trigger']=='pullback_reclaim' else 'waiting_close',
                       report_path=result.get('report_path'),plan=plan,
                       authorization=dict(symbol=result['symbol'],cio=copy.deepcopy(result['cio']),
                           trader=copy.deepcopy(trader),entry_eligible=True,report_path=result.get('report_path')))
            self.update(row,now,reason=(f"Watching for a pullback/rejection at {plan['level']:g} and a completed close {'above' if side=='LONG' else 'below'} it"
                        if plan['trigger']=='pullback_reclaim' else
                        f"Watching for a completed 5-minute close {'above' if side=='LONG' else 'below'} {plan['level']:g}"))
            supersede_pending()
            rows.append(row)
            self.save(rows)
            return {k:v for k,v in row.items() if k not in ('authorization','events')}

    def poll(self, ticks, now=None):
        now = now or now_ist()
        with self.locked():
            rows = self.load()
            for row in rows:
                if row['status'] != 'pending':
                    continue
                # Recover safely if the process stopped after writing the signal.
                signals=SignalBook(self.cfg).load()
                existing = next((s for s in signals if s.get('setup_id')==row['id']),None)
                if existing:
                    row['signal_id'] = existing['id']
                    self.update(row,now,status='activated',reason='Confirmed setup issued a signal at the observed price')
                    continue
                if (row.get('replacement_policy')=='explicit_or_valid_replacement'
                        and any(pd.Timestamp(s['issued_at'])>=pd.Timestamp(row['created_at']) for s in signals)):
                    self.update(row,now,status='superseded',reason='A newer issued signal replaced this pending plan')
                    continue
                if now >= dt.datetime.fromisoformat(row['valid_until']) or not entry_allowed(self.policy,now):
                    self.update(row,now,status='expired',reason='Setup expired without a valid entry')
                    continue
                if not self.policy.get('enabled'):
                    self.update(row,now,status='cancelled',reason='Intraday signals disabled')
                    continue
                quote,error = latest_quote(self.cfg,now)
                if not quote:
                    self.update(row,now,reason=error)
                    continue
                if str(quote['expiry'])[:10] != row['contract_expiry']:
                    self.update(row,now,status='cancelled',reason='Contract changed; a new council setup is required')
                    continue
                self._evaluate(row,ticks,quote,now)
            if rows:
                self.save(rows)
            return rows

    def _evaluate(self, row, ticks, quote, now):
        p = row['plan']; long = row['side']=='LONG'; sign = 1 if long else -1
        px = float(quote['ltp'])
        data = ticks.copy()
        if len(data) and {'ts','ltp','expiry'} <= set(data):
            data = data[(data.ts <= now) & (data.expiry.astype(str).str[:10]==row['contract_expiry'])]
            data = data.sort_values('ts').drop_duplicates('ts')
            data['ltp'] = pd.to_numeric(data.ltp,errors='coerce')
            data = data[data.ltp.map(lambda x: math.isfinite(x) and x>0)]
        else:
            data = pd.DataFrame(columns=['ts','ltp'])
        since = data[data.ts >= pd.Timestamp(row['created_at'])]
        observed = [px,*since.ltp.tolist()]
        if any((v-p['stop'])*sign<=0 for v in observed):
            self.update(row,now,status='invalidated',reason='Observed stop level before entry confirmation')
            return
        if any((v-p['target'])*sign>=0 for v in observed):
            self.update(row,now,status='missed',reason='Target reached before the setup could issue a signal')
            return
        # An observed move past the approved range is never replayed at an old price.
        if row.get('rule_version',1)<2 and any(v>p['entry_max'] if long else v<p['entry_min'] for v in observed):
            self.update(row,now,status='missed',reason='Price passed the approved entry range; no late or backdated signal')
            return
        if data.empty:
            self.update(row,now,reason='Waiting for observed completed candles')
            return
        bars = data.set_index('ts').ltp.resample('5min').ohlc().dropna()
        bars = bars[bars.index+pd.Timedelta(minutes=5)<=now]
        first_bar = pd.Timestamp(row['created_at']).ceil('5min')
        new = bars[bars.index >= first_bar]
        if row.get('last_bar'):
            new = new[new.index>pd.Timestamp(row['last_bar'])]
        for stamp,bar in new.iterrows():
            row['last_bar'] = str(stamp)
            touches = bar.low<=p['level']+p['retest_tolerance'] and bar.high>=p['level']-p['retest_tolerance']
            if row['phase']=='waiting_pullback':
                if touches and (bar.close-p['level'])*sign > 0:
                    row['confirmed_at'] = str(stamp+pd.Timedelta(minutes=5))
                    self.update(row,now,phase='waiting_entry',reason='Pullback/rejection candle confirmed; waiting for an eligible live entry')
            elif row['phase']=='waiting_close':
                if (bar.close-p['level'])*sign > 0:
                    row['breakout_bar'] = str(stamp)
                    phase = 'waiting_retest' if p['trigger']=='breakout_retest' else 'waiting_entry'
                    if phase=='waiting_entry':row['confirmed_at']=str(stamp+pd.Timedelta(minutes=5))
                    self.update(row,now,phase=phase,reason='Breakout close confirmed; waiting for '+('a later retest candle' if phase=='waiting_retest' else 'an eligible live entry'))
            else:
                if (bar.close-p['level'])*sign < 0:
                    self.update(row,now,status='invalidated',reason='A completed candle closed back through the trigger level')
                    return
                if row['phase']=='waiting_retest' and touches:
                    row['retest_bar'] = str(stamp)
                    row['confirmed_at'] = str(stamp+pd.Timedelta(minutes=5))
                    self.update(row,now,phase='waiting_entry',reason='Breakout and retest confirmed; waiting for an eligible live entry')
        if row['phase']!='waiting_entry':
            # A temporary stale-quote warning must clear when the feed recovers,
            # even if no new candle has confirmed the pattern yet.
            direction = 'above' if long else 'below'
            reason = (f"Waiting for a pullback/rejection at {p['level']:g} and a completed close {direction} it"
                      if row['phase']=='waiting_pullback' else
                      'Breakout close confirmed; waiting for a later retest candle'
                      if row['phase']=='waiting_retest' else
                      f"Waiting for a completed 5-minute close {direction} {p['level']:g}")
            self.update(row,now,reason=reason)
            return
        if row.get('rule_version',1)>=2:
            # Before confirmation, movement away from the level is part of a
            # pullback/retest setup. Only later prices can represent a missed entry.
            confirmed = pd.Timestamp(row['confirmed_at'])
            post = data[data.ts>=confirmed].ltp.tolist()
            quote_time = pd.Timestamp(quote.get('ltt') or quote['ts'])
            if quote_time.tzinfo:quote_time=quote_time.tz_convert('Asia/Kolkata').tz_localize(None)
            if quote_time>=confirmed:post.append(px)
            if any(v>p['entry_max'] if long else v<p['entry_min'] for v in post):
                self.update(row,now,status='missed',reason='Price passed the approved entry range after confirmation; no late entry')
                return
        if bars.empty or (now-(bars.index[-1]+pd.Timedelta(minutes=5))).total_seconds()>120:
            self.update(row,now,reason='Waiting for up-to-date completed candles')
            return
        if p['momentum']=='macd_improving':
            if len(bars)<35:
                self.update(row,now,reason='Insufficient completed candles for MACD confirmation')
                return
            macd = bars.close.ewm(span=12,adjust=False).mean()-bars.close.ewm(span=26,adjust=False).mean()
            hist = macd-macd.ewm(span=9,adjust=False).mean()
            if (hist.iloc[-1]-hist.iloc[-2])*sign<=0:
                self.update(row,now,reason='Waiting for improving completed-bar MACD momentum')
                return
        if not p['entry_min']<=px<=p['entry_max']:
            self.update(row,now,reason='Confirmation complete; waiting for a fresh quote in the approved entry range')
            return
        result = copy.deepcopy(row['authorization'])
        result['trader'].update(entry=px,stop=p['stop'],target=p['target'])
        result['setup_id'] = row['id']
        result['setup_entry_range'] = [p['entry_min'],p['entry_max']]
        result['contract_expiry'] = row['contract_expiry']
        result['setup_confirmed_at'] = str(bars.index[-1]+pd.Timedelta(minutes=5))
        issued = SignalBook(self.cfg).issue(result,now)
        if issued['status'] in ('active','closed','reaffirmed'):
            row['signal_id'] = issued.get('id') or issued['signal_id']
            self.update(row,now,status='activated' if issued['status']!='reaffirmed' else 'reaffirmed',
                        reason='Confirmed setup issued a signal at the observed price' if issued['status']!='reaffirmed' else issued['reason'])
        else:
            self.update(row,now,reason=issued['reason'])
