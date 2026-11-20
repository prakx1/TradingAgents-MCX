"""Virtual-money execution of approved intraday signals. No broker dependencies.

Original ledger rows are read-only. New trades and recovered legacy exits are
stored separately, linked to the source signal/trade. Signal prices are assumed
paper fills, with explicit sizing and transaction-cost assumptions.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from .history import read_lines, resolved_time
from .strategies import now_ist, strategy_config


DEFAULTS = dict(enabled=True, initial_capital_inr=1_000_000, risk_per_trade_pct=1.0,
                max_lots=4, lot_size_bbl=10, margin_per_lot_inr=27374,
                roundtrip_cost_per_lot_inr=80, max_new_trades_per_day=3,
                daily_loss_limit_pct=1.0, weekly_loss_limit_pct=2.5)


def paper_settings(cfg):
    p = {**DEFAULTS, **cfg.get('paper',{})}
    for key in DEFAULTS.keys()-{'enabled'}:
        if isinstance(p[key],bool) or not math.isfinite(float(p[key])) or float(p[key])<0:
            raise ValueError(f'Invalid paper setting: {key}')
    for key in ('initial_capital_inr','risk_per_trade_pct','max_lots','lot_size_bbl','margin_per_lot_inr'):
        if float(p[key])<=0:
            raise ValueError(f'Paper {key} must be positive')
    for key in ('max_lots','lot_size_bbl','max_new_trades_per_day'):
        if int(p[key])!=float(p[key]):
            raise ValueError(f'Paper {key} must be an integer')
    for key in DEFAULTS.keys()-{'enabled'}:
        p[key]=int(p[key]) if key in ('max_lots','lot_size_bbl','max_new_trades_per_day') else float(p[key])
    return p


def _atomic(path, data):
    tmp=path.with_suffix('.tmp');tmp.write_text(data);tmp.replace(path)


class PaperBook:
    def __init__(self,cfg):
        self.cfg=strategy_config(cfg,'intraday')
        self.policy=paper_settings(cfg)
        self.root=Path(self.cfg['paths']['project_root'])
        self.path=self.root/'ledger/paper_intraday.jsonl'
        self.account_path=self.root/'ledger/paper_account.json'
        self.legacy_path=self.root/'ledger/calls.jsonl'

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            yield

    def load(self):
        # Atomic writers make concurrent dashboard reads safe. Corrupt persisted
        # state is an error, never an excuse to replace it with an empty ledger.
        if not self.path.exists():return []
        return [json.loads(s) for s in self.path.read_text().splitlines() if s.strip()]

    def legacy_trades(self):
        unique={}
        for r in read_lines(self.legacy_path):
            if not r.get('trade_id') or not r.get('filled_at') or not r.get('lots'):
                continue
            status='open' if r['status']=='open' else 'closed'
            risk=abs(float(r['entry'])-float(r.get('stop') or r['entry']))
            unique[r['trade_id']]=dict(id='legacy-'+r['trade_id'],trade_id=r['trade_id'],origin='legacy',
                signal_id=None,symbol=r.get('symbol','CRUDEOIL'),side=r['side'],status=status,
                opened_at=r['filled_at'],closed_at=r.get('closed'),entry=r['entry'],stop=r.get('stop'),
                target=r.get('target'),exit=r.get('close_price'),lots=r['lots'],lot_size_bbl=r['lot_bbl'],
                pnl_inr=r.get('pnl_inr'),unrealized_inr=r.get('unrealized_inr') or 0,
                fees_inr=r.get('costs_inr'),close_reason=r.get('close_reason') or (r['status'] if status=='closed' else None),
                reward_risk=round(abs(float(r['target'])-float(r['entry']))/risk,3) if risk and r.get('target') else None,
                contract_expiry=r.get('contract_expiry'),recorded_status=r['status'])
        return list(unique.values())

    def all_trades(self,include_experimental=False):
        combined={r['id']:r for r in self.legacy_trades()}
        combined.update({r['id']:r for r in self.load()})
        if include_experimental:
            from .mock_experiment import MockExperiment
            combined.update({r['id']:r for r in MockExperiment(self.cfg).load()})
        return sorted(combined.values(),key=lambda r:r['opened_at'],reverse=True)

    def _metadata(self,now=None):
        if self.account_path.exists():return json.loads(self.account_path.read_text())
        # Reading a dashboard must not initialize/reset an account.
        return dict(initial_capital_inr=self.policy['initial_capital_inr'],started_at=None)

    def _initialize(self,now):
        if not self.account_path.exists():
            meta=dict(initial_capital_inr=self.policy['initial_capital_inr'],started_at=now.isoformat(sep=' ',timespec='seconds'),
                      legacy_path=str(self.legacy_path),legacy_sha256=hashlib.sha256(self.legacy_path.read_bytes()).hexdigest()
                      if self.legacy_path.exists() else None,mode='paper',assumptions=self.policy)
            _atomic(self.account_path,json.dumps(meta,indent=2))
        return self._metadata()

    def _account(self,rows,as_of=None):
        base=self._metadata()['initial_capital_inr']
        completed=[];opened=[]
        for r in rows:
            if r['status']=='skipped' or (as_of and resolved_time(r['opened_at'])>as_of):continue
            close=resolved_time(r.get('closed_at'))
            if r['status']=='closed' and (as_of is None or (close and close<=as_of)):
                completed.append(r)
            else:
                opened.append(r)
        realized=sum(float(r.get('pnl_inr') or 0) for r in completed)
        unrealized=sum(float(r.get('unrealized_inr') or 0) for r in opened) if as_of is None else 0
        margin=sum(float(r.get('margin_inr') or r['lots']*self.policy['margin_per_lot_inr']) for r in opened)
        wins=sum(float(r.get('pnl_inr') or 0)>0 for r in completed)
        return dict(initial_capital_inr=base,realized_inr=round(realized,2),unrealized_inr=round(unrealized,2),
            balance_inr=round(base+realized,2),equity_inr=round(base+realized+unrealized,2),
            margin_used_inr=round(margin,2),available_inr=round(base+realized-margin,2),
            open=len(opened),closed=len(completed),wins=wins,
            win_rate=round(100*wins/len(completed),1) if completed else None,
            fees_inr=round(sum(float(r.get('fees_inr') or 0) for r in completed),2))

    def account(self):
        return self._account(self.all_trades())

    def _legacy_ticks(self,trade,now):
        frames=[]
        start=trade['opened_at'][:10].replace('-','')
        for path in sorted((Path(self.cfg['paths']['cache_dir'])/'live').glob(f"{trade['symbol']}_2*.csv")):
            if start<=path.stem.rsplit('_',1)[-1]<=now.strftime('%Y%m%d'):
                try:frames.append(pd.read_csv(path,parse_dates=['ts']))
                except (OSError,ValueError,pd.errors.EmptyDataError):continue
        if not frames:return pd.DataFrame()
        ticks=pd.concat(frames,ignore_index=True)
        if 'expiry' not in ticks or not trade.get('contract_expiry'):return pd.DataFrame()
        return ticks[(ticks.ts>pd.Timestamp(trade['opened_at'])) & (ticks.ts<=now)
                     & (ticks.expiry.astype(str).str[:10]==trade['contract_expiry'])].sort_values('ts')

    def _recover_legacy(self,row,now):
        # Existing old trades keep their recorded stop/target and original
        # 20-day/contract-expiry limit. New intraday rules are not backdated.
        ticks=self._legacy_ticks(row,now)
        sign=1 if row['side']=='LONG' else -1
        deadline=dt.datetime.fromisoformat(row['opened_at'])+dt.timedelta(days=20)
        if row.get('contract_expiry'):
            deadline=min(deadline,dt.datetime.fromisoformat(row['contract_expiry']+' 23:30:00'))
        row['valid_until']=str(deadline)
        for tick in ticks.itertuples():
            px=float(tick.ltp)
            if not math.isfinite(px) or px<=0:continue
            row.update(last_price=px,last_observed_at=str(tick.ts))
            reason=None
            if tick.ts>=deadline:reason='legacy_holding_period_end'
            elif row.get('stop') and (px-float(row['stop']))*sign<=0:reason='stop_hit'
            elif row.get('target') and (px-float(row['target']))*sign>=0:reason='target_hit'
            gross=(px-row['entry'])*sign*row['lots']*row['lot_size_bbl']
            if reason:
                row.update(status='closed',exit=px,closed_at=str(tick.ts),close_reason=reason,
                           pnl_inr=round(gross-row['fees_inr'],2),unrealized_inr=0,recovered_at=str(now),
                           recovery_note='Recovered from saved contract ticks; original ledger row is unchanged.')
                break
            row['unrealized_inr']=round(gross-row['fees_inr'],2)
        if row['status']=='open' and now>=deadline:
            row['exit_pending']='Legacy exit overdue; awaiting an observed price for its contract'

    @staticmethod
    def _mark_from_signal(row,signal):
        size=row['lots']*row['lot_size_bbl'];sign=1 if row['side']=='LONG' else -1
        row['last_price']=signal.get('last_price',signal['entry'])
        row['last_observed_at']=signal.get('last_observed_at') or signal['issued_at']
        row['exit_pending']=signal.get('exit_pending')
        if signal['status']=='closed':
            gross=(signal['exit']-row['entry'])*sign*size
            row.update(status='closed',exit=signal['exit'],closed_at=signal['closed_at'],
                       close_reason=signal['close_reason'],gross_pnl_inr=round(gross,2),
                       pnl_inr=round(gross-row['fees_inr'],2),unrealized_inr=0)
        else:
            row['unrealized_inr']=round((row['last_price']-row['entry'])*sign*size-row['fees_inr'],2)

    def _new_trade(self,signal,existing,now):
        p=self.policy;at=resolved_time(signal['issued_at']);account=self._account(existing,as_of=at)
        from .economics import plan_economics
        economics=plan_economics(signal['entry'],signal['stop'],signal['target'],signal['side'],
                                p['roundtrip_cost_per_lot_inr']/p['lot_size_bbl'])
        balance=max(0,account['balance_inr'])
        risk_per_lot=abs(signal['entry']-signal['stop'])*p['lot_size_bbl']+p['roundtrip_cost_per_lot_inr']
        budget=balance*p['risk_per_trade_pct']/100
        lots=max(0,min(int(p['max_lots']),int(budget//risk_per_lot),int(max(0,account['available_inr'])//p['margin_per_lot_inr'])))
        reason=None
        if not p['enabled']:reason='Automatic paper entries are disabled'
        elif (self.root/'events/KILL').exists():reason='Paper trading pause file is active'
        elif lots<1:reason='Paper risk budget or available margin cannot cover one lot'
        today=[r for r in existing if r['status']!='skipped' and r['opened_at'][:10]==signal['issued_at'][:10]
               and resolved_time(r['opened_at'])<=at]
        if p['max_new_trades_per_day'] and len(today)>=p['max_new_trades_per_day']:
            reason='Paper daily trade limit reached'
        for days,key in ((at.date(),'daily_loss_limit_pct'),(at.date()-dt.timedelta(days=at.weekday()),'weekly_loss_limit_pct')):
            pnl=sum(float(r.get('pnl_inr') or 0) for r in existing if r['status']=='closed'
                    and resolved_time(r.get('closed_at')) and dt.datetime.combine(days,dt.time())<=resolved_time(r['closed_at'])<=at)
            if p[key] and pnl<=-balance*p[key]/100:reason=f'Paper {key.replace("_pct", "").replace("_", " ")} reached'
        row=dict(id='paper-'+signal['id'],trade_id='P-'+signal['id'],signal_id=signal['id'],origin='intraday',
                 symbol=signal['symbol'],side=signal['side'],opened_at=signal['issued_at'],recorded_at=str(now),
                 entry=signal['entry'],stop=signal['stop'],target=signal['target'],reward_risk=signal['reward_risk'],
                 planned_net_reward_risk=economics['net_reward_risk'],paper_economics=economics,
                 entry_method=signal.get('entry_method','council_levels'),
                 valid_until=signal['valid_until'],contract_expiry=signal['contract_expiry'],
                 lots=lots if not reason else 0,lot_size_bbl=p['lot_size_bbl'],status='open' if not reason else 'skipped',
                 margin_inr=lots*p['margin_per_lot_inr'] if not reason else 0,
                 fees_inr=lots*p['roundtrip_cost_per_lot_inr'] if not reason else 0,
                 risk_budget_inr=round(budget,2),risk_inr=round(lots*risk_per_lot,2) if not reason else 0,
                 reason=reason,pnl_inr=None,unrealized_inr=0,report_path=signal.get('report_path'),
                 fill_assumption='Signal emission price; no slippage; main CRUDEOIL quotes with mini-contract sizing')
        if not reason:self._mark_from_signal(row,signal)
        return row

    def sync(self,signals,now=None):
        now=now or now_ist()
        with self.locked():
            meta=self._initialize(now);rows=self.load();indexed={r['id']:r for r in rows}
            legacy=self.legacy_trades()
            for old in legacy:
                if old['status']!='open':continue
                row=indexed.get(old['id'])
                if not row:
                    row={**old,'origin':'legacy_recovered','fees_inr':old.get('fees_inr') or old['lots']*self.policy['roundtrip_cost_per_lot_inr'],
                         'margin_inr':old['lots']*self.policy['margin_per_lot_inr'],'recorded_at':str(now)}
                    rows.append(row);indexed[row['id']]=row
                if row['status']=='open':self._recover_legacy(row,now)
            by_signal={s['id']:s for s in signals}
            for row in rows:
                if row['status']=='open' and row.get('signal_id') in by_signal:
                    self._mark_from_signal(row,by_signal[row['signal_id']])
            for signal in sorted(signals,key=lambda s:s['issued_at']):
                if signal['status'] not in ('active','closed') or resolved_time(signal['issued_at'])>now:continue
                if resolved_time(signal['issued_at'])<resolved_time(meta['started_at']):continue
                if 'paper-'+signal['id'] in indexed:continue
                combined={r['id']:r for r in legacy};combined.update(indexed)
                row=self._new_trade(signal,list(combined.values()),now)
                rows.append(row);indexed[row['id']]=row
            _atomic(self.path,''.join(json.dumps(r,allow_nan=False)+'\n' for r in rows))
            return rows
