import copy
import datetime as dt
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from tradingagents_mcx.config import _DEFAULTS
from tradingagents_mcx.pipeline import run_analysis
from tradingagents_mcx.signals import SignalBook
from tradingagents_mcx.strategies import strategy_config

NOW=dt.datetime(2026,10,12,14)


class FakeLLM:
    def __init__(self,*args,**kwargs):pass
    def chat(self,system,user,role=None,**kwargs):
        if role=='analyst': return 'Evidence points higher. DIRECTION: BULLISH | CONFIDENCE: HIGH'
        if role=='debate': return 'BULL SCORE: 8.0\nBEAR SCORE: 6.0\nWINNER: BULL\nRATIONALE: evidence'
        if role=='trader': return 'Grounded plan.\nFINAL DIRECTION: BULLISH\nFINAL CONFIDENCE: HIGH\nENTRY: 8000\nSTOP: 7950\nTARGET: 8100'
        if role=='risk': return 'OPERATIONAL CONFIDENCE: MEDIUM'
        if role=='cio': return 'MATRIX CONFIRMED\nFINAL DECISION: MODERATE LONG'
        raise AssertionError(role)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cfg=copy.deepcopy(_DEFAULTS)
        self.cfg['paths']={'cache_dir':str(self.root/'cache'),'reports_dir':str(self.root/'reports')}
        live=self.root/'cache/live';live.mkdir(parents=True)
        (live/'latest_CRUDEOIL.json').write_text(json.dumps(dict(ts=str(NOW),ltp=8000,expiry='2026-10-19',high=8020,low=7970)))
        ticks=pd.DataFrame({'ts':pd.date_range('2026-10-12 09:00',periods=301,freq='1min'),
                            'ltp':8000+np.sin(np.arange(301))*10,'vol_cum':np.arange(301)*100,'expiry':'2026-10-19'})
        ticks.to_csv(live/'CRUDEOIL_20261012.csv',index=False)
        ticks['ts']-=pd.Timedelta(days=3);ticks.to_csv(live/'CRUDEOIL_20261009.csv',index=False)
        idx=pd.date_range('2026-01-01',periods=200,freq='D')
        px=80+np.arange(200)*.01+np.sin(np.arange(200))
        self.daily=pd.DataFrame(dict(open=px,high=px+1,low=px-1,close=px,volume=1000+np.arange(200)),index=idx)
        self.fx=pd.DataFrame({'usdinr':100.},index=idx)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        patches={
            'tradingagents_mcx.pipeline.load_config':dict(return_value=self.cfg),
            'tradingagents_mcx.pipeline.now_ist':dict(return_value=NOW),
            'tradingagents_mcx.strategies.now_ist':dict(return_value=NOW),
            'tradingagents_mcx.signals.now_ist':dict(return_value=NOW),
            'tradingagents_mcx.execution.now_ist':dict(return_value=NOW),
            'tradingagents_mcx.pipeline.LLMClient':dict(new=FakeLLM),
            'tradingagents_mcx.pipeline._mcx_quotes_safe':dict(return_value=None),
            'tradingagents_mcx.data_providers.get_wti_history':dict(return_value=self.daily),
            'tradingagents_mcx.data_providers.get_usdinr_history':dict(return_value=self.fx),
            'tradingagents_mcx.data_providers.get_brent_history':dict(return_value=self.daily),
            'tradingagents_mcx.data_providers.get_wti_curve':dict(return_value=pd.DataFrame()),
            'tradingagents_mcx.data_providers.get_cot_wti':dict(return_value=None),
            'tradingagents_mcx.data_providers.get_eia_fundamentals':dict(return_value=None),
            'tradingagents_mcx.data_providers.get_news':dict(return_value=[dict(title='Oil demand rises',published='2026-10-12',source='test',lane='official',trust=1)]),
            'tradingagents_mcx.data_providers.get_mcx_bhavcopy':dict(return_value=None),
            'tradingagents_mcx.data_providers.get_mcx_participant_oi':dict(return_value=None),
        }
        for target,kw in patches.items():self.stack.enter_context(patch(target,**kw))

    def test_intraday_council_produces_signal_report_and_progress(self):
        results=[run_analysis(strategy='intraday',progress=lambda *a:None)]
        self.assertEqual([r['signal']['status'] for r in results],['active'])
        self.assertIn('5-minute',results[0]['data']['technical_source'])
        for r in results:
            self.assertNotIn('capital_inr',r)
            self.assertNotIn('position_pct',r['trader'])
            path=Path(r['report_path'])
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.with_suffix('.json').read_text())['signal']['reward_risk'],2)
        state=json.loads((self.root/'events/council_intraday.json').read_text())
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['signal_status'],'active')
        self.assertIn('historical_learning',results[0])
        self.assertFalse((self.root/'.council_running').exists())
        self.assertFalse((self.root/'ledger/calls.jsonl').exists())
        self.assertEqual(results[0]['paper_trade']['status'],'open')

    def test_analysis_only_never_emits_a_signal(self):
        r=run_analysis(strategy='intraday',watch=True,progress=lambda *a:None)
        self.assertNotIn('signal',r)
        self.assertEqual(SignalBook(strategy_config(self.cfg,'intraday')).load(),[])
        self.assertIn('/night/',r['report_path'])

    def test_cost_evidence_and_new_lesson_ids_survive_the_whole_council(self):
        prompts=[]
        class CostLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                prompts.append((role,user))
                text=super().chat(system,user,role=role,**kwargs)
                if role=='trader':text+='\nLESSONS USED: L-P-intraday-20261009-100000-example, DD-20261009'
                return text
        with patch('tradingagents_mcx.pipeline.LLMClient',new=CostLLM):
            r=run_analysis(progress=lambda *a:None)
        for role in ('trader','risk','cio'):
            self.assertIn('[KNOWN PAPER EXECUTION COSTS]',next(p for role_,p in prompts if role_==role))
        for role in ('risk','cio'):
            self.assertIn('"net_reward_risk": 1.5862',next(p for role_,p in prompts if role_==role))
        self.assertEqual(r['trader']['lessons_used'],['L-P-intraday-20261009-100000-example','DD-20261009'])
        self.assertEqual(r['paper_trade']['planned_net_reward_risk'],1.5862)
        saved=json.loads(Path(r['report_path']).with_suffix('.json').read_text())
        self.assertEqual(saved['historical_learning']['intraday_feedback']['version'],'intraday_feedback_v2')
        self.assertIn('rapid_stops',saved['historical_learning']['intraday_feedback']['summary'])

    def test_final_quote_recovers_once_and_live_reward_risk_is_still_enforced(self):
        quote_path=self.root/'cache/live/latest_CRUDEOIL.json'
        class StaleAtDecision(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                text=super().chat(system,user,role=role,**kwargs)
                if role=='cio':
                    quote_path.write_text(json.dumps(dict(ts=str(NOW-dt.timedelta(minutes=3)),ltp=8000,expiry='2026-10-19')))
                return text
        for price,expected in ((8030,'blocked'),(8000,'active')):
            with self.subTest(price=price):
                quote_path.write_text(json.dumps(dict(ts=str(NOW),ltp=8000,expiry='2026-10-19')))
                def recover(_):
                    quote_path.write_text(json.dumps(dict(ts=str(NOW),ltp=price,expiry='2026-10-19')))
                with patch('tradingagents_mcx.pipeline.LLMClient',new=StaleAtDecision), \
                     patch('tradingagents_mcx.quote_wait.time.sleep',side_effect=recover) as sleep:
                    result=run_analysis(progress=lambda *a:None)
                self.assertTrue(result['quote_recovery']['recovered'])
                self.assertEqual(result['signal']['status'],expected)
                self.assertEqual(sleep.call_count,1)

    def test_ready_continuation_candidate_becomes_signal_and_paper_trade(self):
        close=7840+np.arange(80)*2.
        bars=pd.DataFrame(dict(open=close-1,high=close+1,low=close-3,close=close,volume=500.),
            index=pd.date_range(end=NOW-dt.timedelta(minutes=5),periods=80,freq='5min'))
        bars.attrs['contract_expiry']='2026-10-19'
        class CandidateLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                if role=='trader':
                    marker='[DETERMINISTIC TREND-CONTINUATION CANDIDATE — proposal for review]\n'
                    c=json.loads(user.split(marker,1)[1].splitlines()[0])
                    assert c['status']=='ready',c
                    return ('Completed trend entry with a projected ATR target.\nFINAL DIRECTION: BULLISH\n'
                            f"FINAL CONFIDENCE: MEDIUM\nENTRY: {c['entry']}\nSTOP: {c['stop']}\nTARGET: {c['target']}\nSETUP: null")
                return super().chat(system,user,role=role,**kwargs)
        with patch('tradingagents_mcx.pipeline.intraday_data',return_value=(bars,'Observed five-minute candles')), \
             patch('tradingagents_mcx.pipeline.LLMClient',new=CandidateLLM):
            r=run_analysis(progress=lambda *a:None)
        self.assertEqual(r['signal']['status'],'active')
        self.assertEqual(r['signal']['entry_method'],'trend_continuation_v1')
        self.assertEqual(r['paper_trade']['status'],'open')
        self.assertEqual(r['paper_trade']['signal_id'],r['signal']['id'])

    def test_range_research_proposal_reaches_council_and_simulated_trade(self):
        self.cfg['entry_models']={'regime_enabled':True}
        close=8000-70*np.sin(np.arange(84)*np.pi/5)
        bars=pd.DataFrame(dict(open=close-2,high=close+5,low=close-5,close=close,volume=500.),
            index=pd.date_range(end=NOW-dt.timedelta(minutes=5),periods=84,freq='5min'))
        bars.loc[bars.index[-1],['open','high','low','close']]=[7897,7905,7896,7903]
        bars.attrs['contract_expiry']='2026-10-19'
        (self.root/'cache/live/latest_CRUDEOIL.json').write_text(json.dumps(dict(ts=str(NOW),ltp=7903,expiry='2026-10-19')))
        class RangeLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                if role=='trader':
                    marker='[REGIME CANDIDATE — experimental council proposal]\n'
                    pool=json.loads(user.split(marker,1)[1].splitlines()[0])
                    c=pool['candidates']['regime']
                    assert c['status']=='ready',c
                    return ('Completed range rejection.\nFINAL DIRECTION: BULLISH\nFINAL CONFIDENCE: MEDIUM\n'
                            f"ENTRY: {c['entry']}\nSTOP: {c['stop']}\nTARGET: {c['target']}\nSETUP: null")
                return super().chat(system,user,role=role,**kwargs)
        with patch('tradingagents_mcx.pipeline.intraday_data',return_value=(bars,'Observed five-minute candles')), \
             patch('tradingagents_mcx.pipeline.LLMClient',new=RangeLLM):
            result=run_analysis(progress=lambda *a:None)
        self.assertEqual(result['entry_models']['regime']['regime'],'range')
        self.assertEqual(result['signal']['status'],'active')
        self.assertEqual(result['signal']['entry_method'],'regime_rules_v2:range_reversion')
        self.assertEqual(result['paper_trade']['status'],'open')
        self.assertEqual(result['paper_trade']['entry_method'],'regime_rules_v2:range_reversion')
        saved=json.loads(Path(result['report_path']).with_suffix('.json').read_text())
        self.assertEqual(saved['entry_models'],result['entry_models'])

    def test_refreshed_risk_and_cio_use_original_trader_expiry(self):
        clock=[NOW];prompts=[]
        setup=dict(trigger='pullback_reclaim',level=8010,entry_min=8011,entry_max=8020,
                   stop=7980,target=8100,retest_tolerance=2,valid_for_minutes=60,momentum='none')
        live=self.root/'cache/live/latest_CRUDEOIL.json'
        def advance(minute):
            clock[0]=NOW+dt.timedelta(minutes=minute)
            live.write_text(json.dumps(dict(ts=str(clock[0]),ltp=8000,expiry='2026-10-19')))
        class ClockLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                prompts.append((role,user))
                if role=='trader':
                    advance(1)
                    return ('Expires at 15:00 IST.\nFINAL DIRECTION: BULLISH\nFINAL CONFIDENCE: HIGH\n'
                            'ENTRY: WAIT\nSTOP: 7980\nTARGET: 8100\nSETUP: '+json.dumps(setup))
                if role=='risk':
                    advance(2)
                    return 'SETUP APPROVAL: APPROVE\nOPERATIONAL CONFIDENCE: MEDIUM'
                if role=='cio':return 'SETUP APPROVAL: APPROVE\nFINAL DECISION: CAUTIOUS LONG'
                return super().chat(system,user,role=role,**kwargs)
        with patch('tradingagents_mcx.pipeline.now_ist',side_effect=lambda:clock[0]), \
             patch('tradingagents_mcx.strategies.now_ist',side_effect=lambda:clock[0]), \
             patch('tradingagents_mcx.pipeline.LLMClient',new=ClockLLM):
            r=run_analysis(progress=lambda *a:None)
        for role in ('risk','cio'):
            p=next(p for role_,p in prompts if role_==role)
            self.assertIn('"evidence_as_of": "2026-10-12 14:00:00"',p)
            self.assertIn('"valid_until": "2026-10-12 15:00:00"',p)
        self.assertEqual(r['signal']['status'],'pending')
        self.assertEqual(r['signal']['valid_until'],'2026-10-12 15:00:00')

    def test_conditional_plan_survives_full_council_and_is_visible_in_ui(self):
        from tradingagents_mcx.setups import PendingSetups
        from streamlit.testing.v1 import AppTest
        setup=dict(trigger='breakout_retest',level=8010,entry_min=8011,entry_max=8020,
                   stop=7980,target=8100,retest_tolerance=2,valid_for_minutes=60,momentum='none')
        class ConditionalLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                if role=='trader':
                    return ('Wait for confirmation.\nFINAL DIRECTION: BULLISH\nFINAL CONFIDENCE: HIGH\n'
                            'ENTRY: WAIT\nSTOP: 7980\nTARGET: 8100\nSETUP: '+json.dumps(setup))
                if role=='risk':return 'SETUP APPROVAL: APPROVE\nOPERATIONAL CONFIDENCE: MEDIUM'
                if role=='cio':return 'SETUP APPROVAL: APPROVE\nFINAL DECISION: CAUTIOUS LONG'
                return super().chat(system,user,role=role,**kwargs)
        with patch('tradingagents_mcx.pipeline.LLMClient',new=ConditionalLLM):
            r=run_analysis(strategy='intraday',progress=lambda *a:None)
        self.assertEqual(r['signal']['status'],'pending')
        self.assertEqual(r['decision_evidence']['contract_expiry'],'2026-10-19')
        self.assertEqual(SignalBook(strategy_config(self.cfg,'intraday')).load(),[])
        self.assertEqual(len(PendingSetups(strategy_config(self.cfg,'intraday')).load()),1)
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
        self.assertFalse(at.exception,list(at.exception))
        self.assertTrue(any('WATCHING LONG SETUP' in x.value for x in at.markdown))
        self.assertTrue(any('Conditional setup history'==x.label for x in at.expander))

    def test_failure_releases_lock(self):
        with patch('tradingagents_mcx.pipeline.LLMClient',side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError,'offline'):run_analysis(progress=lambda *a:None)
        self.assertFalse((self.root/'.council_running').exists())
        self.assertEqual(run_analysis(progress=lambda *a:None)['signal']['status'],'active')

    def test_dashboard_is_intraday_only_without_account_controls(self):
        from streamlit.testing.v1 import AppTest
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'live_dashboard.py'),default_timeout=15).run()
        self.assertFalse(at.exception,list(at.exception))
        self.assertTrue(any('Intraday' in x.value for x in at.subheader))
        self.assertFalse(any('Long term' in x.value for x in at.subheader))
        self.assertFalse(any('capital' in x.label.lower() or 'margin' in x.label.lower() for x in at.number_input))
        self.assertEqual(len([x for x in at.button if x.label.startswith('Run ')]),1)
        self.assertFalse(any('Long-term' in x.label for x in at.checkbox))

    def test_dashboard_renders_active_and_closed_outcomes(self):
        from streamlit.testing.v1 import AppTest
        run_analysis(strategy='intraday',progress=lambda *a:None)
        book=SignalBook(strategy_config(self.cfg,'intraday'))
        ticks=pd.DataFrame([dict(ts=pd.Timestamp('2026-10-12 14:01'),ltp=8100,expiry='2026-10-19')])
        book.mark(ticks,NOW.replace(minute=1))
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'live_dashboard.py'),default_timeout=15).run()
        self.assertFalse(at.exception,list(at.exception))
        self.assertTrue(any('2.00' in str(x.value) for x in at.metric))

    def test_archived_reports_and_ledger_remain_byte_identical(self):
        from streamlit.testing.v1 import AppTest
        folder=self.root/'reports';folder.mkdir()
        ledger=self.root/'ledger/calls.jsonl';ledger.parent.mkdir()
        files={folder/'CRUDEOIL_20260901.json': b'{"symbol":"CRUDEOIL","legacy":true}',
               folder/'CRUDEOIL_20260901.md': b'# Original report\nKeep this history.\n',
               ledger: b'{"id":"old-1","side":"LONG","entry":8000,"status":"closed"}\n'}
        for path,content in files.items():path.write_bytes(content)
        run_analysis(strategy='intraday',progress=lambda *a:None)
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
        self.assertFalse(at.exception,list(at.exception))
        self.assertTrue(any(x.label=='History & learning' for x in at.tabs))
        self.assertTrue(any('History & learning'==x.value for x in at.subheader))
        for path,content in files.items():self.assertEqual(path.read_bytes(),content)

    def test_failed_risk_review_cannot_issue_signal(self):
        with patch('tradingagents_mcx.pipeline.decision.run_risk',return_value={
            'opinion':'Provider unavailable','operational_confidence':'MEDIUM','degraded':True}):
            r=run_analysis(strategy='intraday',progress=lambda *a:None)
        self.assertFalse(r['entry_eligible'])
        self.assertEqual(r['signal']['status'],'blocked')
        self.assertEqual(SignalBook(strategy_config(self.cfg,'intraday')).load(),[])

    def test_long_term_runs_are_rejected_without_changing_archives(self):
        folder=self.root/'reports/long_term';folder.mkdir(parents=True)
        file=folder/'old.json';file.write_text('{"old":true}')
        with self.assertRaisesRegex(ValueError,'Only intraday'):
            run_analysis(strategy='long_term',progress=lambda *a:None)
        self.assertEqual(file.read_text(),'{"old":true}')

    def test_dashboard_disables_duplicate_runs_and_shows_busy_state(self):
        import os,time
        from streamlit.testing.v1 import AppTest
        (self.root/'.council_running').write_text(f'{os.getpid()},{time.time()}')
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
        actions=[x for x in at.button if x.key.startswith(('run_','watch_'))]
        self.assertTrue(actions)
        self.assertTrue(all(x.disabled for x in actions))
        self.assertTrue(any('running' in x.value.lower() for x in at.info))

    def test_analysis_preview_is_visible_on_signal_card(self):
        from streamlit.testing.v1 import AppTest
        r=run_analysis(strategy='intraday',watch=True,progress=lambda *a:None)
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
        self.assertTrue(any('Latest analysis' in x.value and 'MODERATE LONG' in x.value for x in at.caption))

    def test_failed_run_records_visible_error(self):
        with patch('tradingagents_mcx.pipeline.LLMClient',side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError,'offline'):run_analysis(strategy='intraday',progress=lambda *a:None)
        state=json.loads((self.root/'events/council_intraday.json').read_text())
        self.assertEqual(state['status'],'failed')
        self.assertIn('offline',state['message'])

    def test_intraday_evidence_reaches_every_decision_stage(self):
        prompts=[]
        class CapturingLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                prompts.append((role,system,user))
                return super().chat(system,user,role=role,**kwargs)
        with patch('tradingagents_mcx.pipeline.LLMClient',new=CapturingLLM):
            result=run_analysis(progress=lambda *a:None)
        for role in ('trader','risk','cio'):
            prompt=next(p for r,s,p in prompts if r==role)
            self.assertIn('RECENT COMPLETED MCX 5-MINUTE OHLCV',prompt,role)
            self.assertIn('2026-10-12 13:55:00',prompt,role)
            self.assertIn('rsi14',prompt,role)
            self.assertIn('across 2 sessions',prompt,role)
        debater=next(p for r,s,p in prompts if r=='debate' and 'bullish' in s)
        self.assertIn('RECENT COMPLETED MCX 5-MINUTE OHLCV',debater)
        self.assertIn('decision_evidence',result)
        self.assertEqual(sum(r=='debate' for r,s,p in prompts),3)
        trader=next(p for r,s,p in prompts if r=='trader')
        self.assertIn('at most 300 words',trader)
        self.assertNotIn('[THREE-DIMENSION JUDGING FRAMEWORK]',trader)
        self.assertIn('FINAL DIRECTION: BULLISH|BEARISH|NEUTRAL',trader)
        self.assertIn('SETUP: <one-line conditional setup JSON, or null>',trader)
        cio=next(p for r,s,p in prompts if r=='cio')
        self.assertIn('at most 220 words',cio)
        self.assertNotIn('Strategic opportunity assessment (120-160 words)',cio)

    def test_pending_plan_is_reviewed_and_only_explicitly_cancelled(self):
        from tradingagents_mcx.setups import PendingSetups
        mode=['arm'];prompts=[];setup_id=[None]
        setup=dict(trigger='breakout_retest',level=8010,entry_min=8011,entry_max=8020,
                   stop=7980,target=8100,retest_tolerance=2,valid_for_minutes=60,momentum='none')
        class PendingLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                prompts.append((role,user))
                if role=='trader':
                    return ('Wait for the specified pattern.\nFINAL DIRECTION: BULLISH\nFINAL CONFIDENCE: HIGH\n'
                            'ENTRY: WAIT\nSTOP: 7980\nTARGET: 8100\nSETUP: '+json.dumps(setup if mode[0]=='arm' else None))
                if role=='risk':return 'SETUP APPROVAL: APPROVE\nOPERATIONAL CONFIDENCE: MEDIUM'
                if role=='cio':
                    if mode[0]=='arm':return 'SETUP APPROVAL: APPROVE\nFINAL DECISION: CAUTIOUS LONG'
                    cancel=(f'PENDING SETUP ACTION: CANCEL\nPENDING SETUP ID: {setup_id[0]}\n'
                            'PENDING SETUP REASON: Support thesis failed\n') if mode[0]=='cancel' else ''
                    return cancel+'SIGNAL REASON: No new entry\nFINAL DECISION: STAND ASIDE'
                return super().chat(system,user,role=role,**kwargs)
        store=PendingSetups(strategy_config(self.cfg,'intraday'))
        with patch('tradingagents_mcx.pipeline.LLMClient',new=PendingLLM):
            result=run_analysis(progress=lambda *a:None)
            setup_id[0]=result['signal']['id']
            mode[0]='wait';prompts.clear()
            result=run_analysis(progress=lambda *a:None)
            self.assertEqual(store.load()[0]['status'],'pending')
            for role in ('trader','risk','cio'):
                self.assertIn(setup_id[0],next(p for r,p in prompts if r==role))
            mode[0]='cancel'
            result=run_analysis(progress=lambda *a:None)
        self.assertEqual(result['cio']['pending_setup_action'],'CANCEL')
        self.assertEqual(store.load()[0]['status'],'cancelled')
        self.assertIn('Support thesis failed',store.load()[0]['reason'])

    def test_reports_retain_analyst_setup_tail_and_correct_module_count(self):
        from tradingagents_mcx.decision import _context_block
        body='Background context. '*200+'ENTRY TRIGGER: bar close above 8100; STOP 8050; TARGET 8200'
        reports=[dict(module=m,title=m,direction='BULLISH',confidence='MEDIUM',report=body) for m in ('technical','news','basis')]
        text=_context_block(reports)
        self.assertIn('ENTRY TRIGGER: bar close above 8100',text)
        self.assertNotIn('3/6',text)

    def test_candles_arriving_during_review_refresh_votes_and_entry_evidence(self):
        clock=[NOW]
        prompts=[]
        live=self.root/'cache/live'
        def advance(minutes):
            start=clock[0]
            clock[0]=NOW+dt.timedelta(minutes=minutes)
            times=pd.date_range(start+dt.timedelta(minutes=1),clock[0],freq='1min')
            ticks=pd.DataFrame(dict(ts=times,ltp=8000.,vol_cum=40000+np.arange(len(times))*100,
                                    expiry='2026-10-19'))
            ticks.to_csv(live/'CRUDEOIL_20261012.csv',mode='a',header=False,index=False)
            (live/'latest_CRUDEOIL.json').write_text(json.dumps(
                dict(ts=str(clock[0]),ltp=8000,expiry='2026-10-19')))
        class AdvancingLLM(FakeLLM):
            def chat(self,system,user,role=None,**kwargs):
                prompts.append((role,user))
                if role=='analyst' and '[UPDATED INTRADAY TECHNICAL REVIEW]' not in user:
                    return 'Unconfirmed pattern. DIRECTION: NEUTRAL | CONFIDENCE: MEDIUM'
                if role=='debate' and clock[0]==NOW:advance(5)
                if role=='trader':advance(10)
                if role=='risk':advance(15)
                return super().chat(system,user,role=role,**kwargs)
        with patch('tradingagents_mcx.pipeline.LLMClient',new=AdvancingLLM), \
             patch('tradingagents_mcx.pipeline.now_ist',side_effect=lambda:clock[0]), \
             patch('tradingagents_mcx.strategies.now_ist',side_effect=lambda:clock[0]):
            result=run_analysis(progress=lambda *a:None)
        for role,bar in (('trader','14:00:00'),('risk','14:05:00'),('cio','14:10:00')):
            prompt=next(p for r,p in prompts if r==role)
            self.assertIn('2026-10-12 '+bar,prompt,role)
        self.assertEqual(len(result['analyst_revisions']),1)
        revision=result['analyst_revisions'][0]
        self.assertEqual(revision['previous']['direction'],'NEUTRAL')
        self.assertEqual(revision['updated']['direction'],'BULLISH')
        self.assertEqual(result['evidence_stages']['analysts']['as_of'],'2026-10-12T14:00:00')
        self.assertEqual(result['evidence_stages']['trader']['as_of'],'2026-10-12T14:05:00')
        self.assertEqual(result['setup_evidence_as_of'],'2026-10-12T14:05:00')
        self.assertEqual(result['trader']['direction'],'BULLISH')
        self.assertEqual(result['signal']['status'],'active')

    def test_wait_decisions_are_visible_in_history_without_issued_signals(self):
        from streamlit.testing.v1 import AppTest
        with patch('tradingagents_mcx.pipeline.decision.run_cio',return_value={
            'final_decision':'STAND ASIDE','statement':'Wait for a confirmed breakout.','degraded':False}):
            run_analysis(progress=lambda *a:None)
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
        self.assertFalse(at.exception,list(at.exception))
        decisions=[x.value for x in at.dataframe if 'decision' in x.value.columns and 'reason' in x.value.columns]
        self.assertTrue(decisions)
        self.assertEqual(decisions[0].iloc[0]['decision'],'WAIT')
        self.assertFalse(any('starts with the first valid' in x.value for x in at.info))


if __name__=='__main__':unittest.main()
