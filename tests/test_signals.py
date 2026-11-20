import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

import numpy as np
import pandas as pd

from tradingagents_mcx.config import _DEFAULTS
from tradingagents_mcx.strategies import strategy_config, profiles, intraday_data, now_ist
from tradingagents_mcx.signals import SignalBook
from tradingagents_mcx.execution import maintain_all

NOW = dt.datetime(2026, 10, 12, 14, 0)


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(_DEFAULTS)
        self.cfg['paths'] = {'reports_dir': str(self.root/'reports'), 'cache_dir': str(self.root/'cache')}
        self.live = self.root/'cache/live'; self.live.mkdir(parents=True)
        self.quote()

    def quote(self, now=NOW, price=8000, expiry='2026-10-19', **extra):
        row = dict(ts=str(now), ltp=price, expiry=expiry, **extra)
        (self.live/'latest_CRUDEOIL.json').write_text(json.dumps(row))

    def book(self, name='intraday'):
        return SignalBook(strategy_config(self.cfg, name))

    def legacy_book(self):
        book=self.book('long_term')
        book._save([dict(id='legacy-long',strategy='long_term',symbol='CRUDEOIL',side='LONG',
                        contract_expiry='2026-10-19',issued_at=str(NOW),valid_until='2026-10-19 23:30:00',
                        entry=8000,stop=7950,target=8100,risk_points=50,reward_points=100,reward_risk=2,
                        status='active',confidence='MEDIUM',last_price=8000,current_r=0)])
        return book

    def result(self, side='LONG', **extra):
        return {'symbol':'CRUDEOIL', 'cio':{'final_decision':f'MODERATE {side}'},
                'trader':{'direction':'BULLISH' if side=='LONG' else 'BEARISH', 'entry':'8000',
                          'stop':'7950' if side=='LONG' else '8050',
                          'target':'8100' if side=='LONG' else '7900', 'confidence':'MEDIUM'}, **extra}

    def ticks(self, pairs, expiry='2026-10-19'):
        return pd.DataFrame([{'ts':pd.Timestamp(ts), 'ltp':px, 'expiry':expiry} for ts,px in pairs])

    def test_issue_has_rr_and_no_account_fields(self):
        signal=self.book().issue(self.result(),NOW)
        self.assertEqual(signal['reward_risk'],2)
        self.assertEqual(signal['risk_points'],50)
        self.assertFalse({'capital','capital_inr','lots','margin','position_pct'} & set(signal))

    def test_horizons_do_not_reverse_each_other(self):
        self.book().issue(self.result(),NOW)
        self.legacy_book()
        self.assertEqual(self.book().load()[0]['status'],'active')
        self.assertEqual(self.book('long_term').load()[0]['status'],'active')

    def test_same_side_reaffirms_and_keeps_original_levels(self):
        b=self.book(); original=b.issue(self.result(),NOW)
        self.assertEqual(b.issue(self.result(),NOW)['status'],'reaffirmed')
        self.assertEqual(b.load(),[original])

    def test_opposite_signal_closes_only_own_horizon(self):
        b=self.book(); b.issue(self.result(),NOW)
        b.issue(self.result('SHORT'),NOW)
        self.assertEqual([r['status'] for r in b.load()],['closed','active'])
        self.assertEqual(b.load()[0]['outcome_r'],0)

    def test_stand_aside_does_not_close_active_signal(self):
        b=self.book(); b.issue(self.result(),NOW)
        r=self.result();r['cio']['final_decision']='STAND ASIDE'
        self.assertEqual(b.issue(r,NOW)['status'],'stand_aside')
        self.assertEqual(b.load()[0]['status'],'active')

    def test_rr_is_recomputed_at_live_price(self):
        self.quote(price=8030)
        r=self.book().issue(self.result(),NOW)
        self.assertEqual(r['status'],'blocked')
        self.assertIn('0.88',r['reason'])

    def test_regime_proposal_rechecks_net_costs_contract_and_expiry(self):
        result=self.result()
        result['trader'].update(entry_method='regime_rules_v2:trend_continuation',entry_constraints=dict(
            rule='regime_rules_v2',side='LONG',contract_expiry='2026-10-19',confirmed_at=str(NOW),
            valid_until=str(NOW+dt.timedelta(minutes=5)),stop=7950,target=8100,
            fee_points=8,slippage_points_each_way=1,minimum_net_reward_risk=1.5))
        self.quote(price=8001)
        blocked=self.book().issue(result,NOW)
        self.assertEqual(blocked['status'],'blocked')
        self.assertIn('fees and slippage',blocked['reason'])
        self.quote(expiry='2026-11-19')
        self.assertEqual(self.book().issue(result,NOW)['status'],'blocked')
        later=NOW+dt.timedelta(minutes=5);self.quote(now=later)
        self.assertIn('expired',self.book().issue(result,later)['reason'])
        self.quote(ltt=str(NOW-dt.timedelta(seconds=1)))
        self.assertIn('after regime confirmation',self.book().issue(result,NOW)['reason'])
        self.quote()
        signal=self.book().issue(result,NOW)
        self.assertEqual(signal['status'],'active')
        self.assertEqual(signal['regime_entry_economics']['net_reward_risk'],1.5)

    def test_invalid_levels_are_rejected(self):
        r=self.result();r['trader']['stop']='8050'
        self.assertEqual(self.book().issue(r,NOW)['status'],'blocked')

    def test_negative_price_is_not_silently_converted_to_positive(self):
        r=self.result();r['trader']['stop']='-7950'
        self.assertEqual(self.book().issue(r,NOW)['status'],'blocked')

    def test_missing_target_is_rejected(self):
        r=self.result();r['trader']['target']='WAIT'
        self.assertEqual(self.book().issue(r,NOW)['status'],'blocked')

    def test_conditional_wait_is_not_issued_as_an_active_signal(self):
        r=self.result();r['trader']['entry']='WAIT'
        self.assertEqual(self.book().issue(r,NOW)['status'],'blocked')
        self.assertEqual(self.book().load(),[])

    def test_stale_and_future_quotes_are_rejected(self):
        for delta in (-121,121):
            self.quote(now=NOW+dt.timedelta(seconds=delta))
            self.assertEqual(self.book().issue(self.result(),NOW)['status'],'blocked')

    def test_stale_exchange_timestamp_is_rejected(self):
        self.quote(ltt=str(NOW-dt.timedelta(minutes=10)))
        self.assertEqual(self.book().issue(self.result(),NOW)['status'],'blocked')

    def test_unready_intraday_history_blocks_entry(self):
        self.assertEqual(self.book().issue(self.result(entry_eligible=False),NOW)['status'],'blocked')

    def test_entry_cutoff_and_weekend(self):
        for when in (NOW.replace(hour=23),NOW.replace(hour=8),dt.datetime(2026,10,17,14)):
            self.quote(now=when)
            self.assertEqual(self.book().issue(self.result(),when)['status'],'blocked')

    def test_long_term_also_rejects_post_close_entry(self):
        when=NOW.replace(hour=23,minute=40);self.quote(now=when)
        self.assertEqual(self.book('long_term').issue(self.result(),when)['status'],'blocked')

    def test_intraday_exits_at_cutoff_long_term_carries(self):
        for name in ('intraday','long_term'):
            b=self.legacy_book() if name=='long_term' else self.book()
            if name=='intraday':b.issue(self.result(),NOW)
            b.mark(self.ticks([('2026-10-12 23:14',8020),('2026-10-12 23:15',8025)]), NOW.replace(hour=23,minute=15))
        self.assertEqual(self.book().load()[0]['close_reason'],'intraday_square_off')
        self.assertEqual(self.book().load()[0]['outcome_r'],0.5)
        self.assertEqual(self.book('long_term').load()[0]['status'],'active')

    def test_missing_eod_tick_is_flagged_and_recovered_honestly(self):
        b=self.book();b.issue(self.result(),NOW)
        b.mark(self.ticks([('2026-10-12 23:14',8020)]),NOW.replace(hour=23,minute=16))
        self.assertTrue(b.load()[0]['exit_pending'])
        b.mark(self.ticks([('2026-10-13 09:00',7900)]),dt.datetime(2026,10,13,9))
        r=b.load()[0]
        self.assertEqual(r['close_reason'],'intraday_square_off_delayed_data')
        self.assertEqual(r['outcome_r'],-2)

    def test_empty_feed_marks_exit_pending(self):
        b=self.book();b.issue(self.result(),NOW)
        b.mark(pd.DataFrame(),NOW.replace(hour=23,minute=16))
        self.assertTrue(b.load()[0]['exit_pending'])

    def test_stop_uses_observed_gap_price(self):
        b=self.book();b.issue(self.result(),NOW)
        b.mark(self.ticks([('2026-10-12 14:01',7900)]),NOW.replace(minute=1))
        self.assertEqual(b.load()[0]['outcome_r'],-2)
        self.assertEqual(b.load()[0]['close_reason'],'stop_hit')

    def test_target_and_short_outcomes(self):
        b=self.book();b.issue(self.result('SHORT'),NOW)
        b.mark(self.ticks([('2026-10-12 14:01',7900)]),NOW.replace(minute=1))
        self.assertEqual(b.load()[0]['outcome_r'],2)
        self.assertEqual(b.stats()['win_rate'],100)

    def test_future_ticks_do_not_leak(self):
        b=self.book();b.issue(self.result(),NOW)
        b.mark(self.ticks([('2026-10-12 15:00',8100)]),NOW)
        self.assertEqual(b.load()[0]['status'],'active')

    def test_new_contract_prices_do_not_mark_old_signals(self):
        b=self.book();b.issue(self.result(),NOW)
        b.mark(self.ticks([('2026-10-12 14:01',8500)],'2026-11-19'),NOW.replace(minute=1))
        self.assertEqual(b.load()[0]['status'],'active')

    def test_reversal_cannot_close_old_contract_at_new_contract_price(self):
        b=self.book();original=b.issue(self.result(),NOW)
        self.quote(expiry='2026-11-19')
        self.assertEqual(b.issue(self.result('SHORT'),NOW)['status'],'blocked')
        self.assertEqual(b.load(),[original])

    def test_unidentified_contract_ticks_do_not_score_signals(self):
        b=self.book();original=b.issue(self.result(),NOW)
        ticks=self.ticks([('2026-10-12 14:01',8500)]).drop(columns='expiry')
        b.mark(ticks,NOW.replace(minute=1))
        self.assertEqual(b.load(),[original])

    def test_long_term_cannot_be_reenabled_by_old_config(self):
        self.cfg['strategies']={'long_term':{'enabled':True}}
        result=self.book('long_term').issue(self.result(),NOW)
        self.assertEqual(result['status'],'blocked')
        self.assertIn('archived',result['reason'])
        self.assertFalse(profiles(self.cfg)['long_term']['enabled'])

    def test_guard_marks_both_books_without_dashboard(self):
        self.book().issue(self.result(),NOW);self.legacy_book()
        self.ticks([('2026-10-12 23:15',8025)]).to_csv(self.live/'CRUDEOIL_20261012.csv',index=False)
        maintain_all(self.cfg,NOW.replace(hour=23,minute=15))
        self.assertEqual(self.book().load()[0]['status'],'closed')
        self.assertEqual(self.book('long_term').load()[0]['status'],'active')

    def test_config_is_independent_and_validated(self):
        before=copy.deepcopy(self.cfg)
        a,b=strategy_config(self.cfg,'intraday'),strategy_config(self.cfg,'long_term')
        self.assertNotEqual(a['paths']['reports_dir'],b['paths']['reports_dir'])
        self.assertEqual(self.cfg,before)
        for bad in ({'square_off':'23:45'},{'interval_hours':-1},{'min_reward_risk':0}):
            self.cfg['strategies']={'intraday':bad}
            with self.assertRaises(ValueError):profiles(self.cfg)

    def test_intraday_bars_exclude_unfinished_and_other_contracts(self):
        index=pd.date_range('2026-10-12 09:00',periods=301,freq='1min')
        frame=pd.DataFrame(dict(ts=index,ltp=8000+np.sin(np.arange(len(index))),vol_cum=np.arange(len(index)),expiry='2026-10-19'))
        previous=frame.copy();previous['ts']-=pd.Timedelta(days=3)
        frame.to_csv(self.live/'CRUDEOIL_20261012.csv',index=False)
        previous.to_csv(self.live/'CRUDEOIL_20261009.csv',index=False)
        bars,note=intraday_data(strategy_config(self.cfg,'intraday'),'CRUDEOIL',NOW)
        self.assertGreaterEqual(len(bars),80)
        self.assertTrue((bars.index+pd.Timedelta(minutes=5)<=NOW).all())
        frame['expiry']='2026-11-19';frame.to_csv(self.live/'CRUDEOIL_20261012.csv',index=False)
        bars,_=intraday_data(strategy_config(self.cfg,'intraday'),'CRUDEOIL',NOW)
        self.assertTrue(bars.empty) # only 60 bars for the new contract; no stitched history

    def test_scheduler_keeps_horizons_and_post_close_separate(self):
        from scheduler import jobs_for_day
        jobs=jobs_for_day(NOW.date(),self.cfg)
        intra=[j for j in jobs if j['strategy']=='intraday']
        self.assertTrue(intra)
        self.assertTrue(all(j['when'].time()<dt.time(23) for j in intra if not j['watch']))
        self.assertEqual({j['strategy'] for j in jobs},{'intraday'})
        post=[j for j in jobs if j['label']=='post-close']
        self.assertEqual(len(post),1);self.assertTrue(post[0]['watch'])

    def test_direction_cannot_be_flipped_by_llm(self):
        from tradingagents_mcx.decision import _parse_trader,run_cio
        tally={'BULLISH':['technical','news'],'BEARISH':[],'NEUTRAL':[]}
        trader=_parse_trader('FINAL DIRECTION: BEARISH\nFINAL CONFIDENCE: MEDIUM',tally)
        self.assertEqual(trader['direction'],'BULLISH')
        class Stub:
            def chat(self,*args,**kwargs):return 'FINAL DECISION: AGGRESSIVE SHORT'
        trader['reasoning']=''
        cio=run_cio(Stub(),'CRUDEOIL',trader,{'operational_confidence':'MEDIUM','opinion':''},
                    {'winner':'BULL','bull_score':7,'bear_score':5},'2026-10-12')
        self.assertEqual(cio['final_decision'],'STAND ASIDE')


if __name__=='__main__':unittest.main()
