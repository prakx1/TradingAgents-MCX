"""Prospective rule-based mock trades, isolated from council signals and capital.

No broker client, LLM call, synthetic council approval or historical entry fills.
The guard evaluates completed-candle candidates and marks fresh observed prices.
"""
import datetime as dt
import fcntl
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from .candidates import build_candidates, preferred_candidate
from .economics import configured_fee_points, plan_economics
from .kite_data import atomic_json
from .paper import paper_settings
from .signals import latest_quote
from .strategies import entry_allowed, intraday_data, now_ist, strategy_config


DEFAULTS = dict(enabled=False, max_trades_per_day=12, cooldown_minutes=5,
                minimum_net_reward_risk=1.0, slippage_points_each_way=1.0,
                daily_loss_limit_r=4.0)


def experiment_settings(cfg):
    values = {**DEFAULTS, **cfg.get('mock_experiment', {})}
    if not isinstance(values['enabled'], bool): raise ValueError('Experimental enabled must be a boolean')
    for name in DEFAULTS.keys()-{'enabled'}:
        number = values[name]
        if isinstance(number, bool) or not isinstance(number, (int,float)) or not math.isfinite(number) or number < 0:
            raise ValueError('Invalid experimental setting: '+name)
    if values['max_trades_per_day'] < 1 or int(values['max_trades_per_day']) != values['max_trades_per_day']:
        raise ValueError('Experimental daily trade limit must be a positive integer')
    if values['minimum_net_reward_risk'] < 1 or values['daily_loss_limit_r'] <= 0:
        raise ValueError('Experimental net reward/risk must be at least 1 and loss limit positive')
    return values


class MockExperiment:
    def __init__(self, cfg):
        self.cfg = strategy_config(cfg, 'intraday')
        self.settings = experiment_settings(cfg)
        self.root = Path(self.cfg['paths']['project_root'])
        self.path = self.root/'ledger/mock_experimental.jsonl'
        self.state_path = self.root/'events/mock_experiment.json'

    def load(self):
        if not self.path.exists(): return []
        # Never silently reset a corrupt ledger.
        return [json.loads(s) for s in self.path.read_text().splitlines() if s.strip()]

    def _save(self, rows):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(''.join(json.dumps(r, allow_nan=False)+'\n' for r in rows))
        tmp.replace(self.path)

    def _state(self, status, reason, now, **details):
        value = dict(status=status, reason=reason, checked_at=str(now), settings=self.settings, **details)
        previous = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if any(previous.get(k) != value.get(k) for k in ('status','reason','trade_id')):
            with (self.state_path.parent/f'mock_experiment_events_{now:%Y%m%d}.jsonl').open('a') as f:
                f.write(json.dumps(value, allow_nan=False)+'\n')
        atomic_json(self.state_path, value)
        return value

    @staticmethod
    def _mark(row, quote, now):
        deadline = pd.Timestamp(row['valid_until'])
        if now >= deadline: row['exit_pending'] = 'Square-off overdue; waiting for a fresh price for this contract'
        if not quote or str(quote['expiry'])[:10] != row['contract_expiry']: return
        observed = pd.Timestamp(quote.get('ltt') or quote['ts'])
        if observed.tzinfo: observed = observed.tz_convert('Asia/Kolkata').tz_localize(None)
        if observed <= pd.Timestamp(row['opened_at']): return
        if observed < pd.Timestamp(row.get('last_observed_at') or row['opened_at']): return
        price = float(quote['ltp']); sign = 1 if row['side']=='LONG' else -1
        slip = row['slippage_points_each_way']
        exit_price = price-sign*slip
        size = row['lots']*row['lot_size_bbl']
        gross = (exit_price-row['entry'])*sign*size
        net = gross-row['fees_inr']
        row.update(last_price=price,last_observed_at=str(observed),unrealized_inr=round(net,2))
        reason = None
        if observed >= deadline:
            reason = 'intraday_square_off' + ('_delayed_data' if (observed-deadline).total_seconds()>120 else '')
        elif (price-row['stop'])*sign <= 0: reason = 'stop_hit'
        elif (price-row['target'])*sign >= 0: reason = 'target_hit'
        if reason:
            points = net/size
            row.update(status='closed',exit=exit_price,exit_observed_price=price,closed_at=str(now),
                close_reason=reason,gross_pnl_inr=round(gross,2),pnl_inr=round(net,2),
                net_points=round(points,4),outcome_r=round(points/row['initial_net_risk_points'],4),
                unrealized_inr=0)
            row.pop('exit_pending',None)

    def poll(self, now=None, *, allow_entries=True):
        now = now or now_ist()
        if (not allow_entries or not self.settings['enabled']) and not self.path.exists():
            if self.state_path.exists():return self._state('paused','Experimental entries are disabled',now,entries_today=0,open=0,closed=0)
            return dict(status='disabled',reason='Experimental entries are disabled',checked_at=str(now))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            rows = self.load(); before = json.dumps(rows,sort_keys=True)
            quote, quote_reason = latest_quote(self.cfg,now)
            for row in rows:
                if row['status']=='open': self._mark(row,quote,now)
            if json.dumps(rows,sort_keys=True)!=before: self._save(rows)
            opened = [r for r in rows if r['status']=='open']
            today = [r for r in rows if r['opened_at'][:10]==now.date().isoformat()]
            details = dict(entries_today=len(today),open=len(opened),closed=sum(r['status']=='closed' for r in rows))
            def status(code,reason,**extra): return self._state(code,reason,now,**details,**extra)
            if opened:
                note = opened[0].get('exit_pending') or ('Experimental trade open; '+quote_reason if not quote
                       else 'Experimental trade open; stop and target are being monitored')
                return status('position_open',note,trade_id=opened[0]['id'])
            if not allow_entries:
                return status('paused','Council approval is required for new trades; historical experimental exits remain monitored')
            if (not self.settings['enabled'] or not paper_settings(self.cfg)['enabled']
                    or not self.cfg['strategy']['enabled'] or (self.root/'events/KILL').exists()):
                return status('paused','Experimental entries are paused; existing trades keep their exits')
            if not entry_allowed(self.cfg['strategy'],now): return status('outside_session','Outside the intraday entry window')
            if len(today)>=self.settings['max_trades_per_day']: return status('daily_limit','Experimental daily trade limit reached')
            if sum(r.get('outcome_r',0) for r in rows if r['status']=='closed' and r['closed_at'][:10]==now.date().isoformat())<=-self.settings['daily_loss_limit_r']:
                return status('loss_limit','Experimental daily loss limit reached')
            last_close = max((pd.Timestamp(r['closed_at']) for r in rows if r['status']=='closed'),default=None)
            if last_close is not None and (now-last_close).total_seconds()<60*self.settings['cooldown_minutes']:
                return status('cooldown',f"Waiting {self.settings['cooldown_minutes']:g} minutes after the last experimental exit")
            if not quote: return status('waiting_quote',quote_reason)
            bars,note = intraday_data(self.cfg,self.cfg['analysis'].get('symbol','CRUDEOIL'),now)
            if bars.empty: return status('warming_up',note)
            recent=bars[bars.index+pd.Timedelta(minutes=5)<=now].tail(4)
            if (len(recent)<4 or recent.index[0].date()!=now.date()
                    or not recent.index.to_series().diff().dropna().eq(pd.Timedelta(minutes=5)).all()):
                return status('warming_up','Need four consecutive completed candles in this session')
            pool = build_candidates(self.cfg,bars,quote,now)
            candidate = preferred_candidate(pool)
            if candidate['status']!='ready': return status('watching',candidate['reason'],entry_models=pool)
            key = '|'.join(str(candidate.get(k,'')) for k in ('rule','method','contract_expiry','confirmation_bar','side'))
            if any(r['candidate_key']==key for r in rows): return status('already_traded','This completed-candle candidate already opened an experimental trade',entry_models=pool)
            sign = 1 if candidate['side']=='LONG' else -1
            slip = self.settings['slippage_points_each_way']
            entry = float(quote['ltp'])+sign*slip
            stop,target = candidate['stop'],candidate['target']
            fees = configured_fee_points(self.cfg)
            try:
                # Entry slippage is in entry; charge exit slippage with fees here.
                economics = plan_economics(entry,stop,target,candidate['side'],fees+slip)
            except ValueError: return status('costs','Adverse entry fill no longer fits the fixed stop and target',entry_models=pool)
            floor = max(self.settings['minimum_net_reward_risk'],1.5 if candidate.get('net_floor_required') else 0)
            if (abs(target-entry)/abs(entry-stop)<self.cfg['strategy']['min_reward_risk']
                    or economics['net_reward_points']/economics['net_risk_points']<floor):
                return status('costs',f'Experimental setup needs at least {floor:g}:1 after fees and slippage',entry_models=pool)
            size = paper_settings(self.cfg)['lot_size_bbl']
            ident = 'experimental-'+hashlib.sha256(key.encode()).hexdigest()[:20]
            deadline = dt.datetime.combine(now.date(),dt.time.fromisoformat(self.cfg['strategy']['square_off']))
            row = dict(id=ident,trade_id=ident,signal_id=None,origin='experimental',track='Experimental',
                symbol=self.cfg['analysis'].get('symbol','CRUDEOIL'),side=candidate['side'],status='open',
                opened_at=str(now),recorded_at=str(now),contract_expiry=str(quote['expiry'])[:10],
                entry=entry,entry_observed_price=float(quote['ltp']),stop=stop,target=target,
                entry_method=candidate['rule']+(':'+candidate['method'] if candidate.get('method') else ''),
                candidate_key=key,candidate=candidate,regime=pool.get('regime'),
                authorization='deterministic_experiment',council_approved=False,
                lots=1,lot_size_bbl=size,fees_inr=fees*size,slippage_points_each_way=slip,
                reward_risk=economics['gross_reward_risk'],planned_net_reward_risk=economics['net_reward_risk'],
                initial_net_risk_points=economics['net_risk_points'],valid_until=str(deadline),
                last_price=float(quote['ltp']),last_observed_at=str(quote.get('ltt') or quote['ts']),
                pnl_inr=None,unrealized_inr=round(-(fees+2*slip)*size,2),
                fill_assumption=f'Observed main-contract price, {slip:g} point adverse slippage each way; one simulated mini-sized lot',
                experiment_settings=self.settings,report_path=None)
            rows.append(row);self._save(rows)
            details.update(entries_today=len(today)+1,open=1)
            return status('entered',f"Experimental {row['side']} entered automatically from completed-candle rules",trade_id=ident,entry_models=pool)
