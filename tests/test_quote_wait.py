import copy
import datetime as dt
import unittest
from unittest.mock import patch

from tradingagents_mcx.strategies import DEFAULTS
from tradingagents_mcx.quote_wait import wait_for_fresh_quote


class QuoteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'strategy':copy.deepcopy(DEFAULTS['intraday'])}
        self.clock=dt.datetime(2026,10,21,14)
        self.delays=[]
        self.quote=dict(expiry='2026-11-19',ts=str(self.clock),ltp=9000)

    def sleep(self,seconds):
        self.delays.append(seconds);self.clock+=dt.timedelta(seconds=seconds)

    def wait(self,**kwargs):
        return wait_for_fresh_quote(self.cfg,clock=lambda:self.clock,sleep=self.sleep,**kwargs)

    def test_transient_stale_quote_recovers_without_accepting_the_stale_price(self):
        with patch('tradingagents_mcx.quote_wait.latest_quote',side_effect=[
            (None,'Exchange last-trade time is stale'),(self.quote,'')]):
            quote,meta=self.wait()
        self.assertEqual(quote,self.quote);self.assertTrue(meta['recovered']);self.assertEqual(self.delays,[5])

    def test_persistent_outage_has_a_bounded_delay_and_no_quote(self):
        with patch('tradingagents_mcx.quote_wait.latest_quote',return_value=(None,'Exchange last-trade time is stale')):
            quote,meta=self.wait()
        self.assertIsNone(quote);self.assertEqual(sum(self.delays),25);self.assertFalse(meta['recovered'])

    def test_no_wait_across_cutoff_or_when_contract_changes(self):
        self.clock=self.clock.replace(hour=23)
        with patch('tradingagents_mcx.quote_wait.latest_quote',return_value=(None,'Exchange last-trade time is stale')):
            self.assertIsNone(self.wait()[0]);self.assertEqual(self.delays,[])
        with patch('tradingagents_mcx.quote_wait.latest_quote',return_value=(self.quote,'')):
            quote,meta=self.wait(expected_contract='2026-10-19')
        self.assertIsNone(quote);self.assertIn('Contract changed',meta['reason']);self.assertEqual(self.delays,[])


if __name__=='__main__':unittest.main()
