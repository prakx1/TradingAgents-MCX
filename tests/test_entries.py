"""Completed-bar entry proposals: fixed rules, no model decisions or ledger writes."""
import copy
import datetime as dt
import unittest

import numpy as np
import pandas as pd

from tradingagents_mcx.entries import continuation_candidate
from tradingagents_mcx.strategies import DEFAULTS

NOW=dt.datetime(2026,10,13,16)


class EntryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.policy=copy.deepcopy(DEFAULTS['intraday'])
        close=8000+np.arange(84)*2.0
        self.bars=pd.DataFrame(dict(open=close-1,high=close+1,low=close-3,close=close,
            volume=500.),index=pd.date_range(end=NOW-dt.timedelta(minutes=5),periods=84,freq='5min'))
        self.bars.attrs['contract_expiry']='2026-10-19'
        self.quote=dict(ts=str(NOW),ltt=str(NOW),ltp=float(close[-1]),expiry='2026-10-19')

    def candidate(self):return continuation_candidate(self.bars,self.quote,self.policy,NOW)

    def test_confirmed_breakout_proposes_numeric_levels_without_writing_a_trade(self):
        before=self.bars.copy(deep=True)
        result=self.candidate()
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['side'],'LONG')
        self.assertLess(result['stop'],result['entry'])
        self.assertGreaterEqual(result['reward_risk'],1.5)
        self.assertEqual(result['confirmed_at'],str(NOW))
        self.assertEqual(result['target_basis'],'2.4 × completed-bar ATR; projected objective, not observed resistance')
        pd.testing.assert_frame_equal(before,self.bars)

    def test_bearish_rules_are_symmetric(self):
        original=self.bars.copy()
        for target,source in (('open','open'),('close','close'),('high','low'),('low','high')):
            self.bars[target]=16000-original[source]
        self.quote['ltp']=16000-self.quote['ltp']
        result=self.candidate()
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['side'],'SHORT')
        self.assertGreater(result['stop'],result['entry'])
        self.assertLess(result['target'],result['entry'])

    def test_old_quote_wrong_contract_and_unfinished_bar_cannot_qualify(self):
        for mutate in ('stale','contract','future_bar'):
            with self.subTest(mutate=mutate):
                bars=self.bars.copy();quote=dict(self.quote)
                if mutate=='stale':quote['ltt']=str(NOW-dt.timedelta(minutes=3))
                if mutate=='contract':quote['expiry']='2026-11-19'
                if mutate=='future_bar':bars.index+=pd.Timedelta(minutes=5)
                result=continuation_candidate(bars,quote,self.policy,NOW)
                if mutate=='future_bar':
                    self.assertNotEqual(result.get('confirmation_bar'),str(NOW))
                else:self.assertEqual(result['status'],'unavailable')

    def test_no_chase_after_price_runs_away(self):
        self.quote['ltp']+=30
        self.assertEqual(self.candidate()['status'],'unavailable')

    def test_target_is_not_extended_to_pass_a_higher_rr_floor(self):
        low=self.candidate()
        self.policy['min_reward_risk']=3
        high=self.candidate()
        self.assertEqual(high['status'],'unavailable')
        self.assertEqual(high['target'],low['target'])

    def test_mixed_trend_does_not_generate_an_entry(self):
        self.bars.iloc[-1,self.bars.columns.get_loc('close')]=8000
        self.quote['ltp']=8000
        self.assertEqual(self.candidate()['status'],'unavailable')

    def test_quote_from_before_confirmation_cannot_qualify(self):
        self.quote['ltt']=str(NOW-dt.timedelta(seconds=30))
        self.assertEqual(self.candidate()['status'],'unavailable')
