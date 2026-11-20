import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from tradingagents_mcx.config import _DEFAULTS
from tradingagents_mcx.what_if import evaluate_cases, load_cases, replay


START=dt.datetime(2026,10,28,10)


def case(**changes):
    return dict(id='declined-1',decision_at=str(START),origin='Rejected',symbol='CRUDEOIL',
                side='LONG',contract_expiry='2026-11-19',proposed_entry='8000',stop=7970,target=8080,
                plan=None,session_open='09:00',entry_cutoff='23:00',square_off='23:15',session_close='23:30',
                fee_points=8.,slippage_points=1.,**changes)


def ticks(prices,start=START,frequency='30s',expiry='2026-11-19'):
    return pd.DataFrame(dict(ts=pd.date_range(start,periods=len(prices),freq=frequency),ltp=prices,expiry=expiry))


def plan(trigger='close_beyond',**changes):
    return dict(trigger=trigger,level=8000,entry_min=8001,entry_max=8005,stop=7970,target=8080,
                retest_tolerance=2,valid_for_minutes=45,momentum='none',**changes)


class WhatIfTests(unittest.TestCase):
    def test_long_fills_after_decision_and_uses_observed_target_with_costs(self):
        data=ticks([8085,8000,8020,8090])
        result=replay(case(),data,START+dt.timedelta(minutes=2))
        self.assertEqual(result['entry'],8001)
        self.assertEqual(result['exit'],8089)
        self.assertEqual(result['net_points'],80)
        self.assertEqual(result['outcome_r'],2)
        self.assertEqual(result['outcome'],'Missed winner')
        self.assertTrue(result['scored'])

    def test_stop_gap_uses_observed_price_and_short_is_symmetric(self):
        long=replay(case(),ticks([8000,8000,7950]),START+dt.timedelta(minutes=1))
        self.assertEqual(long['exit'],7949)
        self.assertEqual(long['net_points'],-60)
        self.assertEqual(long['outcome'],'Avoided loss')
        short_case=case();short_case.update(side='SHORT',stop=8030,target=7920)
        short=replay(short_case,ticks([8000,8000,8050]),START+dt.timedelta(minutes=1))
        self.assertEqual(short['net_points'],-60)
        self.assertEqual(short['outcome'],'Avoided loss')

    def test_no_future_prices_enter_a_running_mark(self):
        result=replay(case(),ticks([8000,8000,8085]),START+dt.timedelta(seconds=30))
        self.assertEqual(result['status'],'open');self.assertFalse(result['scored'])
        self.assertIsNone(result['closed_at'])

    def test_wait_ambiguous_prose_and_missing_contract_are_visible_but_unscored(self):
        for change in [dict(proposed_entry='WAIT'),dict(proposed_entry='8000 after a retest'),
                       dict(proposed_entry='8000-8005'),dict(contract_expiry=None),dict(stop=None),dict(side=None)]:
            row=case();row.update(change)
            result=replay(row,ticks([8000,8000,8085]),START+dt.timedelta(minutes=1))
            self.assertEqual(result['status'],'not_testable',change)
            self.assertFalse(result['scored'])

    def test_wrong_contract_and_gaps_do_not_produce_winners(self):
        result=replay(case(),ticks([8000,8000,8085],expiry='2026-12-18'),START+dt.timedelta(minutes=1))
        self.assertEqual(result['status'],'incomplete')
        data=ticks([8000,8000,8085]);data.loc[2,'ts']=START+dt.timedelta(minutes=6)
        result=replay(case(),data,START+dt.timedelta(minutes=6))
        self.assertEqual(result['status'],'incomplete');self.assertFalse(result['scored'])

    def test_conditional_entry_waits_for_full_completed_candle(self):
        row=case(evidence_at=str(START));row['plan']=plan();row['proposed_entry']='WAIT'
        data=ticks([8002]*12+[8085])
        early=replay(row,data,START+dt.timedelta(minutes=4))
        self.assertEqual(early['status'],'watching');self.assertIsNone(early['entry'])
        done=replay(row,data,START+dt.timedelta(minutes=6))
        self.assertEqual(done['opened_at'],'2026-10-28 10:05:00')
        self.assertEqual(done['outcome'],'Missed winner')

    def test_retest_must_be_a_later_candle_and_target_before_entry_is_not_profit(self):
        row=case(evidence_at=str(START));row['plan']=plan('breakout_retest')
        data=ticks([8002]*21+[8085])
        early=replay(row,data,START+dt.timedelta(minutes=5))
        self.assertIsNone(early['entry'])
        done=replay(row,data,START+dt.timedelta(minutes=10,seconds=30))
        self.assertEqual(done['opened_at'],'2026-10-28 10:10:00')
        data.loc[8,'ltp']=8085
        missed=replay(row,data,START+dt.timedelta(minutes=11))
        self.assertEqual(missed['status'],'no_entry');self.assertFalse(missed['scored'])
        self.assertIsNone(missed['net_points'])

    def test_cancellation_keeps_original_clock_and_does_not_claim_earlier_fills(self):
        row=case(evidence_at=str(START),monitor_from=str(START),cancelled_at=str(START+dt.timedelta(minutes=3)))
        row.update(plan=plan(),origin='Cancelled',decision_at=row['cancelled_at'],valid_until=str(START+dt.timedelta(minutes=45)))
        data=ticks([8002]*12+[8085])
        self.assertEqual(replay(row,data,START+dt.timedelta(minutes=6))['outcome'],'Missed winner')
        row['valid_until']=str(START+dt.timedelta(minutes=4))
        self.assertEqual(replay(row,data,START+dt.timedelta(minutes=6))['status'],'no_entry')
        row['valid_until']=str(START+dt.timedelta(minutes=45));row['cancelled_at']=str(START+dt.timedelta(minutes=6))
        self.assertEqual(replay(row,data,START+dt.timedelta(minutes=7))['status'],'not_testable')

    def test_pullback_confirmation_and_no_chasing(self):
        row=case(evidence_at=str(START));row['plan']=plan('pullback_reclaim')
        data=ticks([8002]*11+[8085])
        self.assertEqual(replay(row,data,START+dt.timedelta(minutes=6))['outcome'],'Missed winner')
        data.loc[10,'ltp']=8010
        self.assertEqual(replay(row,data,START+dt.timedelta(minutes=6))['status'],'no_entry')

    def test_intraday_square_off_missing_exit_and_no_next_day_contamination(self):
        late=START.replace(hour=22,minute=59)
        row=case();row['decision_at']=str(late)
        data=ticks([8000,8000]+[8010]*32,start=late)
        result=replay(row,data,late+dt.timedelta(minutes=17))
        self.assertEqual(result['close_reason'],'intraday_square_off')
        self.assertEqual(result['closed_at'],'2026-10-28 23:15:00')
        self.assertEqual(result['net_points'],0)
        missing=replay(row,data.iloc[:5],late+dt.timedelta(days=1))
        self.assertEqual(missing['status'],'incomplete');self.assertFalse(missing['scored'])

    def test_report_and_cancel_history_are_read_only_and_point_in_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=copy.deepcopy(_DEFAULTS)
            cfg['paths']=dict(reports_dir=str(root/'reports'),cache_dir=str(root/'cache'))
            folder=root/'reports/intraday';folder.mkdir(parents=True)
            report=dict(report_timestamp='20261028_100000',strategy='intraday',symbol='CRUDEOIL',
                        contract_expiry='2026-11-19',trader=dict(direction='BULLISH',entry='8000',stop='7970',target='8080'),
                        cio=dict(final_decision='STAND ASIDE'),signal=dict(status='stand_aside'))
            path=folder/'CRUDEOIL_20261028_100000.json';path.write_text(json.dumps(report))
            setup=dict(id='cancel-1',side='LONG',created_at=str(START),valid_until=str(START+dt.timedelta(minutes=45)),
                       contract_expiry='2026-11-19',plan=plan(),report_path=str(path.with_suffix('.md')),rule_version=2,
                       events=[dict(at=str(START+dt.timedelta(minutes=3)),status='cancelled',reason='Council cancelled')])
            (root/'signals').mkdir();(root/'signals/intraday_setups.jsonl').write_text(json.dumps(setup)+'\n')
            (root/'ledger').mkdir();(root/'ledger/calls.jsonl').write_text('{"historical":true}\n')
            live=root/'cache/live';live.mkdir(parents=True);ticks([8002]*12+[8085]).to_csv(live/'CRUDEOIL_20261028.csv',index=False)
            hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
            before=hashes()
            self.assertEqual(len(load_cases(cfg,START+dt.timedelta(minutes=2))),1)
            cases=load_cases(cfg,START+dt.timedelta(minutes=6));self.assertEqual(len(cases),2)
            result=evaluate_cases(cfg,cases,START+dt.timedelta(minutes=6))
            self.assertTrue(all(r['scored'] for r in result))
            self.assertEqual(hashes(),before)
            actual=dict(id='actual-1',trade_id='P-1',symbol='CRUDEOIL',side='LONG',status='closed',
                        contract_expiry='2026-11-19',opened_at=str(START+dt.timedelta(minutes=5,seconds=6)),
                        stop=7970,target=8080)
            (root/'ledger/paper_intraday.jsonl').write_text(json.dumps(actual)+'\n')
            duplicate={**cases[1],'id':'same-plan-again'}
            result=evaluate_cases(cfg,[*cases,duplicate],START+dt.timedelta(minutes=6))
            cancelled=next(r for r in result if r['id']=='cancel-1')
            self.assertEqual(cancelled['outcome'],'Also taken')
            self.assertEqual(cancelled['actual_trade_ids'],['P-1'])
            self.assertFalse(cancelled['comparison_eligible'])
            self.assertEqual(result[-1]['outcome'],'Same opportunity')
            self.assertFalse(result[-1]['comparison_eligible'])

    def test_quote_recovery_and_final_decision_delay_shadow_entry(self):
        from tradingagents_mcx.what_if import _stamp
        report=dict(report_timestamp='20261028_100000',quote_recovery=dict(checked_at='2026-10-28 10:00:20'),
                    signal_decided_at='2026-10-28 10:00:22.200000')
        self.assertEqual(_stamp(report,Path('CRUDEOIL_20261028_100000.json')),START+dt.timedelta(seconds=22.2))

    def test_numeric_candidate_confirmation_expiry_is_not_extended(self):
        row=case(entry_constraints=dict(side='LONG',contract_expiry='2026-11-19',stop=7970,target=8080,
                 confirmed_at=str(START-dt.timedelta(minutes=5)),valid_until=str(START)))
        result=replay(row,ticks([8000,8000,8085]),START+dt.timedelta(minutes=1))
        self.assertEqual(result['status'],'no_entry');self.assertFalse(result['scored'])

    def test_what_if_ui_filters_outcomes_and_has_separate_hypothetical_labels(self):
        from unittest.mock import patch
        from streamlit.testing.v1 import AppTest
        from live_dashboard import what_if_results
        cfg=copy.deepcopy(_DEFAULTS)
        with tempfile.TemporaryDirectory() as directory:
            cfg['paths']=dict(reports_dir=str(Path(directory)/'reports'),cache_dir=str(Path(directory)/'cache'))
            row=case();row.update(report=str(Path(directory)/'rejected.json'),source='Intraday',reason='CIO rejected',cost_basis='Recorded fees')
            result=replay(row,ticks([8000,8000,8085]),START+dt.timedelta(minutes=1))
            result['comparison_eligible']=True
            what_if_results.clear()
            code='import json\nfrom live_dashboard import what_if_view\nwhat_if_view(json.loads('+repr(json.dumps(cfg))+'))'
            with patch('tradingagents_mcx.what_if.load_cases',return_value=[row]), \
                 patch('tradingagents_mcx.what_if.evaluate_cases',return_value=[result]):
                app=AppTest.from_string(code,default_timeout=15).run()
                self.assertFalse(app.exception,list(app.exception))
                self.assertEqual(app.dataframe[0].value.iloc[0]['outcome'],'Missed winner')
                self.assertIn('Hypothetical outcomes only',' '.join(x.value for x in app.caption))
                app.selectbox(key='what_if_outcome').set_value('Avoided loss').run()
                self.assertFalse(app.exception,list(app.exception));self.assertFalse(app.dataframe)
                self.assertTrue(any('No plans match' in x.value for x in app.info))
            what_if_results.clear()


if __name__=='__main__': unittest.main()
