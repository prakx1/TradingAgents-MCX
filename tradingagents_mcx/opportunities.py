"""Spot confirmed entries between scheduled councils, without issuing signals.

The scanner uses existing fixed candidate rules, then requests the full council.
Persistent deduplication, cooldown and a daily extra-review budget bound API use.
"""
import datetime as dt
import fcntl
import json
import math
from pathlib import Path

from .candidates import build_candidates, preferred_candidate
from .economics import review_plan_costs
from .history import read_lines, resolved_time
from .runs import active_run, launch_council
from .signals import latest_quote
from .strategies import entry_allowed, intraday_data, now_ist, strategy_config


DEFAULTS=dict(enabled=True,cooldown_minutes=5,max_extra_reviews_per_day=6)


def scanner_settings(cfg):
    settings={**DEFAULTS,**cfg.get('opportunities',{})}
    for key in ('cooldown_minutes','max_extra_reviews_per_day'):
        value=float(settings[key])
        if not math.isfinite(value) or value<0 or isinstance(settings[key],bool):
            raise ValueError(f'Invalid opportunity scanner {key}')
    if int(settings['max_extra_reviews_per_day'])!=float(settings['max_extra_reviews_per_day']):
        raise ValueError('Opportunity review budget must be a whole number')
    return settings


def scan_once(cfg, now=None):
    now=now or now_ist();scfg=strategy_config(cfg,'intraday');settings=scanner_settings(cfg)
    root=Path(scfg['paths']['project_root']);folder=root/'events';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'opportunity_scanner.json'
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:state=json.loads(path.read_text())
        except FileNotFoundError:state={}
        # Corrupt persistent state is not an excuse to reset the review budget.
        day=now.date().isoformat()
        if state.get('date')!=day:
            state=dict(date=day,launches_today=0,attempted_keys=[],last_launch_at=None)
        state.update(checked_at=str(now),settings=settings)
        state.pop('candidate',None)
        def save(status,reason):
            previous=(state.get('status'),state.get('reason'))
            state.update(status=status,reason=reason)
            tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(state,allow_nan=False));tmp.replace(path)
            if previous!=(status,reason):
                with (folder/f'opportunity_scans_{now:%Y%m%d}.jsonl').open('a') as f:
                    f.write(json.dumps(dict(at=str(now),status=status,reason=reason,
                        candidate=state.get('candidate'),launches_today=state['launches_today']))+'\n')
            return state
        if not settings['enabled'] or not scfg['strategy']['enabled']:
            return save('disabled','Opportunity scanner is disabled')
        if not entry_allowed(scfg['strategy'],now):
            return save('outside_session','Outside the intraday entry window')
        if active_run(cfg):return save('council_busy','Council is already reviewing the market')
        if any(r.get('status')=='active' for r in read_lines(root/'signals/intraday.jsonl')):
            return save('position_open','An active signal is already being monitored')
        quote,reason=latest_quote(scfg,now)
        if quote is None:return save('waiting_quote',reason)
        bars,note=intraday_data(scfg,scfg['analysis'].get('symbol','CRUDEOIL'),now)
        if bars.empty:return save('warming_up',note)
        pool=build_candidates(scfg,bars,quote,now)
        state['entry_models']=pool
        # Append each new candle or candidate-state change, even after the extra
        # review budget is used. This records evidence, never historical fills.
        observed_key=json.dumps([str(bars.attrs.get('contract_expiry')),str(bars.index[-1]),
            pool.get('regime',{}).get('regime'),
            {name:[c.get(k) for k in ('status','reason','side')] for name,c in pool['candidates'].items()}],sort_keys=True)
        if state.get('last_observed_key')!=observed_key:
            with (folder/f'entry_candidates_{now:%Y%m%d}.jsonl').open('a') as f:
                f.write(json.dumps(pool,allow_nan=False)+'\n')
            state['last_observed_key']=observed_key
        candidate=preferred_candidate(pool)
        state['candidate']=candidate
        if state['launches_today']>=settings['max_extra_reviews_per_day']:
            return save('daily_limit','Extra-review budget used; scheduled councils continue')
        recent=[resolved_time(state.get('last_launch_at'))]
        for report in Path(scfg['paths']['reports_dir']).glob('*.json'):
            try:
                stamp=json.loads(report.read_text()).get('report_timestamp')
                recent.append(dt.datetime.strptime(stamp,'%Y%m%d_%H%M%S' if len(stamp)==15 else '%Y%m%d_%H%M'))
            except (OSError,ValueError,TypeError):continue
        last=max((stamp for stamp in recent if stamp and stamp<=now),default=None)
        if last and (now-last).total_seconds()<float(settings['cooldown_minutes'])*60:
            return save('cooldown','Allowing the last review to settle before another opportunity review')
        if candidate['status']!='ready':return save('watching',candidate['reason'])
        economics=review_plan_costs(scfg,dict(candidate,
            direction='BULLISH' if candidate['side']=='LONG' else 'BEARISH'),quote)
        candidate['paper_economics']=economics
        if economics['status']!='calculated' or economics['net_reward_points']<=0:
            return save('costs','The proposed target does not cover configured paper fees')
        key='|'.join(str(candidate[k]) for k in ('rule','contract_expiry','confirmation_bar','side'))
        if key in state['attempted_keys']:
            return save('already_reviewed','This completed-candle opportunity was already sent to council')
        # Persist the intent first: a process crash must not spend the same slot twice.
        state['attempted_keys'].append(key);state['launches_today']+=1
        state['last_launch_at']=str(now)
        save('launching',f"Requesting council review of confirmed {candidate['side']} opportunity")
        started,message=launch_council(cfg)
        if not started:
            state['attempted_keys'].remove(key);state['launches_today']-=1
            state['last_launch_at']=str(last) if last else None
            return save('launch_deferred',message)
        return save('launched',f"Confirmed {candidate['side']} opportunity sent to the full council; this is not a trade approval")
