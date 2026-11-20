import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from tradingagents_mcx.config import _DEFAULTS
from tradingagents_mcx.mock_experiment import MockExperiment
from tradingagents_mcx.paper import PaperBook
from tradingagents_mcx.execution import maintain_all


NOW=dt.datetime(2026,10,28,16)


class MockExperimentTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.cfg=copy.deepcopy(_DEFAULTS)
        self.cfg['paths']=dict(reports_dir=str(self.root/'reports'),cache_dir=str(self.root/'cache'))
        self.cfg['mock_experiment']=dict(enabled=True)
        self.book=MockExperiment(self.cfg)
        close=8000+np.arange(84)*30.
        self.bars=pd.DataFrame(dict(open=close-2,high=close+5,low=close-5,close=close,volume=500.),
            index=pd.date_range(end=NOW-dt.timedelta(minutes=5),periods=84,freq='5min'))
        self.bars.attrs['contract_expiry']='2026-11-19'
        self.quote(NOW,float(close[-1]))

    def quote(self,when,price,expiry='2026-11-19'):
        folder=self.root/'cache/live';folder.mkdir(parents=True,exist_ok=True)
        (folder/'latest_CRUDEOIL.json').write_text(json.dumps(dict(ts=str(when),ltt=str(when),ltp=price,expiry=expiry)))

    def poll(self,when=NOW):
        with patch('tradingagents_mcx.mock_experiment.intraday_data',return_value=(self.bars,'Ready')):
            return self.book.poll(when)

    def test_rule_candidate_opens_without_council_and_never_touches_its_ledger(self):
        ledger=self.root/'ledger/paper_intraday.jsonl';ledger.parent.mkdir(parents=True)
        ledger.write_text('');before=ledger.read_bytes()
        with patch('tradingagents_mcx.runs.launch_council') as council:
            self.assertEqual(self.poll()['status'],'entered')
            council.assert_not_called()
        row=self.book.load()[0]
        self.assertFalse(row['council_approved'])
        self.assertEqual(row['entry'],row['entry_observed_price']+1)
        self.assertEqual(row['unrealized_inr'],-100)
        self.assertEqual(row['lots'],1)
        self.assertEqual(ledger.read_bytes(),before)
        self.assertFalse((self.root/'signals/intraday.jsonl').exists())
        self.assertEqual(PaperBook(self.cfg).all_trades(),[])
        self.assertEqual(len(PaperBook(self.cfg).all_trades(include_experimental=True)),1)
        self.assertEqual(PaperBook(self.cfg).account()['open'],0)
        self.assertEqual(MockExperiment(self.cfg).poll(NOW)['status'],'position_open')
        self.assertEqual(len(self.book.load()),1)

    def test_observed_stop_gap_charges_fee_once_and_freezes_trade_assumptions(self):
        self.poll();row=self.book.load()[0];later=NOW+dt.timedelta(seconds=10)
        self.quote(later,row['stop']-5)
        self.cfg['mock_experiment']['slippage_points_each_way']=10
        MockExperiment(self.cfg).poll(later)
        closed=self.book.load()[0]
        self.assertEqual(closed['status'],'closed')
        self.assertEqual(closed['close_reason'],'stop_hit')
        self.assertEqual(closed['exit'],row['stop']-6)
        expected=(closed['exit']-row['entry'])*10-80
        self.assertEqual(closed['pnl_inr'],expected)
        self.assertEqual(closed['net_points'],expected/10)
        self.poll(later);self.assertEqual(self.book.load()[0],closed)

    def test_target_and_squareoff_close_automatically_even_if_entries_disabled(self):
        self.poll();row=self.book.load()[0];later=NOW+dt.timedelta(seconds=10)
        self.quote(later,row['target']+3)
        self.cfg['mock_experiment']['enabled']=False
        MockExperiment(self.cfg).poll(later)
        self.assertEqual(self.book.load()[0]['close_reason'],'target_hit')
        self.assertGreater(self.book.load()[0]['pnl_inr'],0)
        self.cfg['mock_experiment']['enabled']=True
        self.cfg['strategies']={'intraday':{'square_off':'16:05','entry_cutoff':'16:04'}}
        # A separate book gives square-off an independent prospective entry.
        self.cfg['paths']=dict(reports_dir=str(self.root/'second/reports'),cache_dir=str(self.root/'cache'))
        book=MockExperiment(self.cfg)
        with patch('tradingagents_mcx.mock_experiment.intraday_data',return_value=(self.bars,'Ready')):
            self.quote(NOW,10490);book.poll(NOW)
        self.cfg['mock_experiment']['enabled']=False
        self.quote(NOW+dt.timedelta(minutes=5),10490)
        MockExperiment(self.cfg).poll(NOW+dt.timedelta(minutes=5))
        self.assertEqual(book.load()[0]['close_reason'],'intraday_square_off')

    def test_stale_quotes_and_changed_contract_never_open_or_close_a_trade(self):
        self.assertEqual(self.poll(NOW+dt.timedelta(seconds=121))['status'],'waiting_quote')
        self.assertEqual(self.book.load(),[])
        self.quote(NOW,10490,'2026-12-18')
        self.assertEqual(self.poll()['status'],'watching')
        self.assertEqual(self.book.load(),[])
        self.quote(NOW,10490);self.poll()
        row=self.book.load()[0]
        later=NOW+dt.timedelta(seconds=10);self.quote(later,row['stop']-50,'2026-12-18')
        self.poll(later);self.assertEqual(self.book.load()[0]['status'],'open')

    def test_cooldown_daily_cap_and_identical_confirmation_prevent_churn(self):
        self.cfg['mock_experiment'].update(max_trades_per_day=1,cooldown_minutes=0)
        self.book=MockExperiment(self.cfg);self.poll();row=self.book.load()[0]
        later=NOW+dt.timedelta(seconds=10);self.quote(later,row['target'])
        self.assertEqual(self.poll(later)['status'],'daily_limit')
        self.cfg['mock_experiment']['max_trades_per_day']=12;self.book=MockExperiment(self.cfg)
        self.quote(later,10490)
        self.assertEqual(self.poll(later)['status'],'already_traded')
        self.cfg['mock_experiment']['cooldown_minutes']=5;self.book=MockExperiment(self.cfg)
        self.assertEqual(self.poll(later)['status'],'cooldown')
        self.assertEqual(len(self.book.load()),1)

    def test_weak_net_economics_are_rejected_without_widening_levels(self):
        close=8000+np.arange(84)*2.
        self.bars.loc[:,['open','high','low','close']]=np.column_stack([close-1,close+1,close-3,close])
        self.quote(NOW,float(close[-1]))
        self.assertEqual(self.poll()['status'],'costs')
        self.assertEqual(self.book.load(),[])

    def test_guard_requires_council_even_when_old_experiment_setting_is_enabled(self):
        with patch('tradingagents_mcx.mock_experiment.intraday_data',return_value=(self.bars,'Ready')):
            maintain_all(self.cfg,NOW)
        self.assertEqual(self.book.load(),[])
        self.assertEqual(PaperBook(self.cfg).load(),[])

    def test_guard_still_exits_historical_experimental_positions(self):
        self.poll()
        row=self.book.load()[0]
        later=NOW+dt.timedelta(seconds=30)
        self.quote(later,row['stop']-5)
        maintain_all(self.cfg,later)
        self.assertEqual(self.book.load()[0]['status'],'closed')
        self.assertEqual(PaperBook(self.cfg).load(),[])

    def test_corrupt_ledger_is_never_replaced_with_empty_history(self):
        self.book.path.parent.mkdir(parents=True);self.book.path.write_text('broken history')
        with self.assertRaises(ValueError):self.poll()
        self.assertEqual(self.book.path.read_text(),'broken history')

    def test_short_entries_use_adverse_fills_and_auto_target_exit(self):
        old=self.bars.copy()
        for target,source in [('open','open'),('close','close'),('high','low'),('low','high')]:
            self.bars[target]=20000-old[source]
        price=float(self.bars.close.iloc[-1]);self.quote(NOW,price)
        self.assertEqual(self.poll()['status'],'entered')
        row=self.book.load()[0]
        self.assertEqual(row['side'],'SHORT');self.assertEqual(row['entry'],price-1)
        later=NOW+dt.timedelta(seconds=10);self.quote(later,row['target']-3)
        self.poll(later);closed=self.book.load()[0]
        self.assertEqual(closed['exit'],row['target']-2)
        self.assertEqual(closed['close_reason'],'target_hit')
        self.assertEqual(closed['pnl_inr'],(row['entry']-closed['exit'])*10-80)

    def test_pause_missing_bars_and_daily_loss_halt_entries(self):
        kill=self.root/'events/KILL';kill.parent.mkdir(parents=True);kill.touch()
        self.assertEqual(self.poll()['status'],'paused');self.assertEqual(self.book.load(),[])
        kill.unlink();self.bars=self.bars.drop(self.bars.index[-2])
        self.assertEqual(self.poll()['status'],'warming_up');self.assertEqual(self.book.load(),[])
        self.book._save([dict(id='old-experiment',status='closed',opened_at=str(NOW-dt.timedelta(days=1)),
                             closed_at=str(NOW),outcome_r=-4)])
        self.assertEqual(self.poll()['status'],'loss_limit')
        self.assertEqual(len(self.book.load()),1)

    def test_trade_cards_and_history_label_experiment_without_changing_history(self):
        from streamlit.testing.v1 import AppTest
        from html import unescape
        self.poll();before=self.book.path.read_bytes()
        with patch('tradingagents_mcx.config.load_config',return_value=self.cfg), \
             patch('tradingagents_mcx.strategies.now_ist',return_value=NOW):
            app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'live_dashboard.py'),default_timeout=15).run()
            self.assertFalse(app.exception,list(app.exception))
            live=unescape('\n'.join(x.value for x in app.tabs[0].markdown))
            self.assertIn('aria-label="Open trade"',live)
            self.assertIn('Experimental',live)
            self.assertIn('AI council required for every entry','\n'.join(x.value for x in app.tabs[0].caption))
            self.assertEqual(app.tabs[1].dataframe[0].value.iloc[0]['track'],'Experimental')
            app.tabs[1].radio[0].set_value('Council + older').run()
            self.assertFalse(app.exception,list(app.exception))
            self.assertFalse(app.tabs[1].dataframe)
        self.assertEqual(self.book.path.read_bytes(),before)


if __name__=='__main__':unittest.main()
