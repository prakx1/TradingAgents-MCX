import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tradingagents_mcx.config import _DEFAULTS
from tradingagents_mcx.strategies import strategy_config
from tradingagents_mcx.signals import SignalBook
from tradingagents_mcx.execution import maintain_all
from tradingagents_mcx.paper import PaperBook

NOW=dt.datetime(2026,10,13,14)


class PaperTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.cfg=copy.deepcopy(_DEFAULTS)
        self.cfg['paths']=dict(cache_dir=str(self.root/'cache'),reports_dir=str(self.root/'reports'))
        self.cfg['paper']=dict(enabled=True,initial_capital_inr=1000000,risk_per_trade_pct=1,
                               max_lots=4,lot_size_bbl=10,margin_per_lot_inr=27374,
                               roundtrip_cost_per_lot_inr=80)
        self.scfg=strategy_config(self.cfg,'intraday')
        self.live=self.root/'cache/live';self.live.mkdir(parents=True)
        self.quote(NOW,8000)
        self.paper=PaperBook(self.scfg)

    def quote(self,now,px):
        (self.live/'latest_CRUDEOIL.json').write_text(json.dumps(dict(ts=str(now),ltp=px,expiry='2026-10-19')))

    def issue(self,side='LONG',now=NOW):
        return SignalBook(self.scfg).issue(dict(symbol='CRUDEOIL',cio={'final_decision':f'MODERATE {side}'},
            trader=dict(direction='BULLISH' if side=='LONG' else 'BEARISH',entry='8000',
                        stop=7950 if side=='LONG' else 8050,target=8100 if side=='LONG' else 7900)),now)

    def ticks(self,now,px,expiry='2026-10-19'):
        pd.DataFrame([dict(ts=str(now),ltp=px,expiry=expiry)]).to_csv(self.live/'CRUDEOIL_20261013.csv',index=False)

    def test_guard_opens_one_paper_trade_for_approved_signal_then_books_target(self):
        self.issue();maintain_all(self.cfg,NOW)
        trades=self.paper.load();self.assertEqual(len(trades),1)
        self.assertEqual((trades[0]['status'],trades[0]['lots']),('open',4))
        maintain_all(self.cfg,NOW);self.assertEqual(len(self.paper.load()),1)
        now=NOW+dt.timedelta(minutes=1);self.ticks(now,8100);self.quote(now,8100)
        maintain_all(self.cfg,now)
        trade=self.paper.load()[0]
        self.assertEqual(trade['status'],'closed');self.assertEqual(trade['close_reason'],'target_hit')
        self.assertEqual(trade['pnl_inr'],3680)
        self.assertEqual(self.paper.account()['balance_inr'],1003680)
        self.assertEqual(self.paper.account()['margin_used_inr'],0)

    def test_stop_gap_and_square_off_use_signal_observation(self):
        self.issue();maintain_all(self.cfg,NOW)
        now=NOW+dt.timedelta(minutes=1);self.ticks(now,7900);self.quote(now,7900)
        maintain_all(self.cfg,now)
        self.assertEqual(self.paper.load()[0]['pnl_inr'],-4320)
        self.assertEqual(self.paper.load()[0]['exit'],7900)

    def test_unrealized_profit_and_costs_are_separate_from_balance(self):
        self.issue();maintain_all(self.cfg,NOW)
        now=NOW+dt.timedelta(minutes=1);self.ticks(now,8025);self.quote(now,8025)
        maintain_all(self.cfg,now)
        a=self.paper.account()
        self.assertEqual(a['balance_inr'],1000000)
        self.assertEqual(a['unrealized_inr'],680)
        self.assertEqual(a['equity_inr'],1000680)

    def test_legacy_trade_history_and_wallet_continue_without_rewriting_original(self):
        folder=self.root/'ledger';folder.mkdir()
        closed=dict(id='old',trade_id='T-old',status='target_hit',side='LONG',entry=7000,stop=6900,target=7200,
                    filled_at='2026-10-09 10:00:00',opened_ts='2026-10-09 10:00:00',closed='2026-10-09',
                    lots=1,lot_bbl=10,pnl_inr=1920,close_price=7200,symbol='CRUDEOIL')
        open_trade=dict(closed,id='carry',trade_id='T-carry',status='open',entry=8000,stop=7950,target=8100,
                        filled_at='2026-10-13 13:00:00',opened_ts='2026-10-13 13:00:00',closed=None,
                        pnl_inr=None,close_price=None,contract_expiry='2026-10-19')
        path=folder/'calls.jsonl';original=''.join(json.dumps(t)+'\n' for t in (closed,open_trade));path.write_text(original)
        self.ticks(NOW,8100);maintain_all(self.cfg,NOW)
        self.assertEqual(path.read_text(),original)
        self.assertEqual(len(self.paper.all_trades()),2)
        recovered=self.paper.load()[0]
        self.assertEqual(recovered['origin'],'legacy_recovered')
        self.assertEqual(recovered['close_reason'],'target_hit')
        self.assertEqual(self.paper.account()['balance_inr'],1002840)
        self.assertEqual(self.paper.account()['closed'],2)
        from tradingagents_mcx.history import learning_context
        self.assertEqual(learning_context(self.cfg,NOW)['eligible_outcomes'],2)
        self.assertEqual(learning_context(self.cfg,NOW-dt.timedelta(seconds=1))['eligible_outcomes'],1)

    def test_paper_budget_cannot_block_signals_or_spend_unavailable_cash(self):
        self.cfg['paper']['initial_capital_inr']=100
        self.scfg=strategy_config(self.cfg,'intraday');self.paper=PaperBook(self.scfg)
        self.issue();maintain_all(self.cfg,NOW)
        self.assertEqual(SignalBook(self.scfg).load()[0]['status'],'active')
        self.assertEqual(self.paper.load()[0]['status'],'skipped')
        self.assertEqual(self.paper.account()['balance_inr'],100)

    def test_no_paper_trade_for_a_wait_or_preview(self):
        maintain_all(self.cfg,NOW)
        self.assertEqual(self.paper.load(),[])

    def test_intraday_square_off_closes_paper_position(self):
        self.issue();maintain_all(self.cfg,NOW)
        now=NOW.replace(hour=23,minute=15);self.ticks(now,8010);self.quote(now,8010)
        maintain_all(self.cfg,now)
        r=self.paper.load()[0]
        self.assertEqual(r['close_reason'],'intraday_square_off')
        self.assertEqual(r['pnl_inr'],80)

    def test_reversal_closes_one_trade_and_opens_only_one_opposite(self):
        self.issue();maintain_all(self.cfg,NOW)
        now=NOW+dt.timedelta(minutes=1);self.quote(now,8000)
        self.issue('SHORT',now);maintain_all(self.cfg,now)
        rows=self.paper.load()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['close_reason'],'opposite_signal')
        self.assertEqual((rows[1]['status'],rows[1]['side']),('open','SHORT'))
        self.assertEqual(self.paper.account()['balance_inr'],999680)

    def test_restart_keeps_frozen_trade_costs_and_original_virtual_capital(self):
        self.issue();maintain_all(self.cfg,NOW)
        self.cfg['paper'].update(initial_capital_inr=99,roundtrip_cost_per_lot_inr=999)
        now=NOW+dt.timedelta(minutes=1);self.ticks(now,8100);self.quote(now,8100)
        maintain_all(self.cfg,now)
        self.assertEqual(self.paper.account()['initial_capital_inr'],1000000)
        self.assertEqual(self.paper.load()[0]['fees_inr'],320)
        self.assertEqual(self.paper.load()[0]['pnl_inr'],3680)

    def test_no_retroactive_trades_for_signals_before_simulator_started(self):
        self.issue();maintain_all(self.cfg,NOW+dt.timedelta(minutes=1))
        self.assertEqual(self.paper.load(),[])

    def test_disabling_entries_preserves_monitoring_of_open_trades(self):
        self.issue();maintain_all(self.cfg,NOW)
        self.cfg['paper']['enabled']=False
        now=NOW+dt.timedelta(minutes=1);self.ticks(now,8100);self.quote(now,8100)
        maintain_all(self.cfg,now)
        self.assertEqual(self.paper.load()[0]['status'],'closed')

    def test_dashboard_shows_paper_trades_indicator_tape_and_api_cost(self):
        from streamlit.testing.v1 import AppTest
        self.issue();maintain_all(self.cfg,NOW)
        (self.root/'cache/llm_usage.jsonl').write_text(json.dumps(dict(ts=str(NOW),model='gpt-6-luna',usd=.25))+'\n')
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
        self.assertFalse(at.exception,list(at.exception))
        self.assertEqual([x.label for x in at.tabs[:2]],['Live trades','Trade history'])
        self.assertTrue({'Indicator tape','Health','History & learning'}<={x.label for x in at.tabs})
        from html import unescape
        rendered=unescape('\n'.join(x.value for x in at.markdown))
        for label in ('Open now','Open P&L','Booked today','Booked P&L','API cost today','API cost all time'):
            self.assertIn(label,rendered)
        live=unescape('\n'.join(x.value for x in at.tabs[0].markdown))
        self.assertIn('aria-label="Open trade"',live)
        for label in ('ENTRY','CURRENT PRICE','STOP-LOSS','TAKE-PROFIT','OPEN LOSS'):
            self.assertIn(label,live)
        self.assertNotIn('paper',live.lower())
        self.assertIn('Simulation',rendered)
        self.assertTrue(any('trade records' in x.value for x in at.caption))

    def test_dashboard_empty_state_keeps_old_trades_and_filters_without_writes(self):
        from streamlit.testing.v1 import AppTest
        from html import unescape
        self.issue();maintain_all(self.cfg,NOW)
        later=NOW+dt.timedelta(minutes=1);self.ticks(later,8100);self.quote(later,8100)
        maintain_all(self.cfg,later)
        before=self.paper.path.read_bytes()
        app=str(Path(__file__).resolve().parents[1]/'live_dashboard.py')
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg), \
             patch('tradingagents_mcx.strategies.now_ist',return_value=NOW+dt.timedelta(days=1)):
            at=AppTest.from_file(app,default_timeout=15).run()
            self.assertFalse(at.exception,list(at.exception))
            live=unescape('\n'.join(x.value for x in at.tabs[0].markdown))
            self.assertIn('No open trades right now',live)
            self.assertIn('₹+3,680',live)  # Last trade remains visible even on the next day.
            self.assertIn('Booked today</div><div class="desk-value">₹+0',live)
            self.assertNotIn('aria-label="Open trade"',live)
            history=at.tabs[1]
            self.assertEqual(len(history.dataframe[0].value),1)
            history.selectbox[0].select('Today').run()
            self.assertFalse(at.exception,list(at.exception))
            self.assertEqual(len(at.tabs[1].dataframe[0].value),0)
            at.tabs[1].multiselect[0].set_value([]).run()
            self.assertFalse(at.exception,list(at.exception))
            at.tabs[1].selectbox[0].select('All time')
            at.tabs[1].multiselect[0].set_value(['closed']).run()
            self.assertEqual(len(at.tabs[1].dataframe[0].value),1)
        self.assertEqual(self.paper.path.read_bytes(),before)

    def test_dashboard_today_uses_exit_day_for_overnight_legacy_result(self):
        from streamlit.testing.v1 import AppTest
        from html import unescape
        legacy=dict(id='old',trade_id='old',symbol='CRUDEOIL',side='SHORT',status='target_hit',
                    filled_at='2026-10-12 22:00:00',closed=str(NOW),entry=8100,stop=8150,target=8000,
                    close_price=8000,lots=1,lot_bbl=10,pnl_inr=920,costs_inr=80)
        self.paper.legacy_path.parent.mkdir(parents=True)
        self.paper.legacy_path.write_text(json.dumps(legacy)+'\n')
        before=self.paper.legacy_path.read_bytes()
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg), \
             patch('tradingagents_mcx.strategies.now_ist',return_value=NOW):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
            self.assertFalse(at.exception,list(at.exception))
            live=unescape('\n'.join(x.value for x in at.tabs[0].markdown))
            self.assertIn('Booked today</div><div class="desk-value">₹+920',live)
            at.tabs[1].selectbox[0].select('Today').run()
            self.assertEqual(len(at.tabs[1].dataframe[0].value),1)
        self.assertEqual(self.paper.legacy_path.read_bytes(),before)

    def test_research_results_are_visible_but_never_counted_as_real_trades(self):
        from streamlit.testing.v1 import AppTest
        from tradingagents_mcx.entry_research import MODELS,PROTOCOL
        from html import unescape
        folder=self.root/'research';folder.mkdir()
        result=dict(purpose='research_only',start='2026-10-13',end='2026-10-21',as_of='2026-10-21 23:59:59',
            split_date='2026-10-21',fee_points=8,daily_entry_limit=3,protocol=PROTOCOL,
            summaries=[dict(model=m,label=label,period=period,entries=12,closed=12,wins=4,net_points=-100)
                       for m,label in MODELS.items() for period in ('all','before_split','after_split')],
            trades={m:[] for m in MODELS})
        (folder/'entries_test.json').write_text(json.dumps(result))
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
        self.assertFalse(at.exception,list(at.exception))
        rendered=unescape('\n'.join(x.value for x in at.markdown))
        self.assertIn('RESEARCH · NOT LIVE PERFORMANCE',rendered)
        live=unescape('\n'.join(x.value for x in at.tabs[0].markdown))
        self.assertIn('Open now</div><div class="desk-value">0',live)
        self.assertFalse(self.paper.path.exists())
        self.assertEqual(self.paper.all_trades(),[])


if __name__=='__main__':unittest.main()
