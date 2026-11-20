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

NOW=dt.datetime(2026,10,21,16,1)


class OpportunityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.cfg=copy.deepcopy(_DEFAULTS)
        self.cfg['paths']=dict(reports_dir=str(self.root/'reports'),cache_dir=str(self.root/'cache'))
        close=8000+np.arange(84)*5.
        self.bars=pd.DataFrame(dict(open=close-3,high=close+3,low=close-8,close=close,volume=500.),
            index=pd.date_range(end=NOW.replace(minute=0)-dt.timedelta(minutes=5),periods=84,freq='5min'))
        self.bars.attrs['contract_expiry']='2026-11-19'
        self.quote=dict(ts=str(NOW),ltt=str(NOW),ltp=close[-1],expiry='2026-11-19')

    def scan(self, now=NOW, busy=None):
        from tradingagents_mcx.opportunities import scan_once
        with patch('tradingagents_mcx.opportunities.latest_quote',return_value=(self.quote,'')), \
             patch('tradingagents_mcx.opportunities.intraday_data',return_value=(self.bars,'Ready')), \
             patch('tradingagents_mcx.opportunities.active_run',return_value=busy or {}), \
             patch('tradingagents_mcx.opportunities.launch_council',return_value=(True,'Started')) as launch:
            result=scan_once(self.cfg,now)
        return result,launch.call_count

    def test_ready_candidate_starts_council_between_slots_but_never_writes_a_trade(self):
        result,n=self.scan()
        self.assertEqual(result['status'],'launched');self.assertEqual(n,1)
        self.assertEqual(result['candidate']['status'],'ready')
        self.assertFalse((self.root/'signals/intraday.jsonl').exists())
        self.assertFalse((self.root/'ledger/paper_intraday.jsonl').exists())

    def test_same_confirmation_is_not_relaunched_after_restart(self):
        self.scan()
        result,n=self.scan(NOW+dt.timedelta(seconds=30))
        self.assertEqual(n,0)
        self.assertEqual(result['launches_today'],1)

    def test_busy_disabled_closed_session_and_quote_failures_do_not_launch(self):
        self.assertEqual(self.scan(busy={'status':'running'})[1],0)
        self.cfg['opportunities']={'enabled':False}
        self.assertEqual(self.scan()[1],0)
        self.cfg['opportunities']={'enabled':True}
        self.assertEqual(self.scan(NOW.replace(hour=23))[1],0)
        self.quote['ltt']=str(NOW-dt.timedelta(minutes=10))
        self.assertEqual(self.scan()[1],0)

    def test_persistent_daily_budget_stops_extra_councils(self):
        self.cfg['opportunities']={'max_extra_reviews_per_day':1}
        self.scan();self.bars.index+=pd.Timedelta(minutes=10)
        self.quote.update(ts=str(NOW+dt.timedelta(minutes=10)),ltt=str(NOW+dt.timedelta(minutes=10)))
        result,n=self.scan(NOW+dt.timedelta(minutes=10))
        self.assertEqual(n,0);self.assertEqual(result['status'],'daily_limit')

    def test_increasing_budget_keeps_prior_review_count_and_deduplication(self):
        self.cfg['opportunities']={'max_extra_reviews_per_day':1}
        self.scan()
        later=NOW+dt.timedelta(minutes=10)
        self.quote.update(ts=str(later),ltt=str(later))
        self.cfg['opportunities']['max_extra_reviews_per_day']=12
        result,n=self.scan(later)
        self.assertEqual(n,0);self.assertEqual(result['launches_today'],1)
        self.bars.index+=pd.Timedelta(minutes=10)
        result,n=self.scan(later)
        self.assertEqual(n,1);self.assertEqual(result['launches_today'],2)

    def test_recent_scheduled_review_respects_cooldown(self):
        path=self.root/'reports/intraday/CRUDEOIL_20261021_160000.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(report_timestamp='20261021_160000')))
        result,n=self.scan()
        self.assertEqual(n,0);self.assertEqual(result['status'],'cooldown')

    def test_a_new_completed_candle_can_trigger_another_review_after_cooldown(self):
        self.scan();later=NOW+dt.timedelta(minutes=10)
        self.bars.index+=pd.Timedelta(minutes=10)
        self.quote.update(ts=str(later),ltt=str(later))
        result,n=self.scan(later)
        self.assertEqual(n,1);self.assertEqual(result['launches_today'],2)

    def test_an_open_signal_does_not_trigger_extra_entry_reviews(self):
        path=self.root/'signals/intraday.jsonl';path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(id='existing',status='active'))+'\n')
        result,n=self.scan()
        self.assertEqual(n,0);self.assertEqual(result['status'],'position_open')

    def test_failed_launch_does_not_consume_a_review_slot(self):
        from tradingagents_mcx.opportunities import scan_once
        with patch('tradingagents_mcx.opportunities.latest_quote',return_value=(self.quote,'')), \
             patch('tradingagents_mcx.opportunities.intraday_data',return_value=(self.bars,'Ready')), \
             patch('tradingagents_mcx.opportunities.active_run',return_value={}), \
             patch('tradingagents_mcx.opportunities.launch_council',return_value=(False,'Busy')):
            result=scan_once(self.cfg,NOW)
        self.assertEqual(result['launches_today'],0)
        self.assertEqual(result['attempted_keys'],[])
        self.assertEqual(self.scan()[1],1)

    def test_range_proposal_can_request_a_review_and_preserves_observation_audit(self):
        self.cfg['entry_models']={'regime_enabled':True}
        close=8000-70*np.sin(np.arange(84)*np.pi/5)
        self.bars['open']=close-2;self.bars['high']=close+5
        self.bars['low']=close-5;self.bars['close']=close
        self.bars.loc[self.bars.index[-1],['open','high','low','close']]=[7897,7905,7896,7903]
        self.quote['ltp']=7903
        result,n=self.scan()
        self.assertEqual(n,1)
        self.assertEqual(result['candidate']['method'],'range_reversion')
        audit=self.root/'events/entry_candidates_20261021.jsonl'
        original=audit.read_bytes()
        self.scan(NOW+dt.timedelta(seconds=30))
        self.assertEqual(audit.read_bytes(),original)
        self.assertEqual(json.loads(original)['regime']['regime'],'range')
        self.assertFalse((self.root/'signals/intraday.jsonl').exists())


if __name__=='__main__':unittest.main()
