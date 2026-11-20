import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from tradingagents_mcx.entry_research import candidates,replay,read_ticks
from tradingagents_mcx.strategies import DEFAULTS


NOW=dt.datetime(2026,10,21,16)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.policy=copy.deepcopy(DEFAULTS['intraday'])
        close=8000+np.arange(84)*2.
        self.bars=pd.DataFrame(dict(open=close-1,high=close+1,low=close-3,close=close,volume=500.),
            index=pd.date_range(end=NOW-dt.timedelta(minutes=5),periods=84,freq='5min'))
        self.bars.attrs['contract_expiry']='2026-11-19'
        self.quote=dict(ts=str(NOW),ltt=str(NOW),ltp=close[-1],expiry='2026-11-19')

    def test_structural_stop_is_outside_recent_extreme_without_stretching_target(self):
        result=candidates(self.bars,self.quote,self.policy,NOW,8)
        old,new=result['continuation_v1'],result['structural_v1']
        self.assertEqual(old['status'],'ready')
        self.assertLess(new['stop'],self.bars.tail(6).low.min())
        self.assertEqual(new['target'],old['target'])
        self.assertEqual(new['status'],'unavailable')  # No stretching target to compensate.

    def test_future_price_spike_cannot_change_past_candidates(self):
        before=self.bars.copy(deep=True)
        expected=candidates(self.bars,self.quote,self.policy,NOW,8)
        future=self.bars.iloc[-1:].copy();future.index=[pd.Timestamp(NOW)]
        future[['high','close']]=99999
        extended=pd.concat([self.bars,future]);extended.attrs=self.bars.attrs.copy()
        self.assertEqual(candidates(extended,self.quote,self.policy,NOW,8),expected)
        pd.testing.assert_frame_equal(before,self.bars)

    def test_short_structural_stop_is_symmetric(self):
        original=self.bars.copy()
        for target,source in [('open','open'),('close','close'),('high','low'),('low','high')]:
            self.bars[target]=16000-original[source]
        self.quote['ltp']=16000-self.quote['ltp']
        result=candidates(self.bars,self.quote,self.policy,NOW,8)
        self.assertGreater(result['structural_v1']['stop'],self.bars.tail(6).high.max())
        self.assertEqual(result['structural_v1']['target'],result['continuation_v1']['target'])

    def test_retest_requires_later_completed_bar_and_net_floor(self):
        # Volatile history, then a six-bar base, breakout and later retest.
        self.bars['high']=self.bars.close+15;self.bars['low']=self.bars.close-15
        values=[(8160,8164,8158,8162),(8162,8165,8160,8163),(8163,8165,8161,8164),
                (8164,8178,8163,8176),(8176,8179,8170,8175),(8175,8176,8164,8170)]
        for i,value in enumerate(values):self.bars.loc[self.bars.index[-6+i],['open','high','low','close']]=value
        self.quote['ltp']=8170.
        result=candidates(self.bars,self.quote,self.policy,NOW,8)
        retest=result['retest_v1']
        self.assertEqual(retest['status'],'ready')
        self.assertLess(pd.Timestamp(retest['breakout_bar']),pd.Timestamp(retest['confirmation_bar']))
        self.assertEqual(retest['level'],8165)
        costly=candidates(self.bars,self.quote,self.policy,NOW,50)
        self.assertEqual(costly['retest_v1']['status'],'ready')
        self.assertEqual(costly['retest_net_v1']['status'],'unavailable')
        self.assertEqual(costly['retest_net_v1']['target'],retest['target'])

    def ticks(self,values):
        return pd.DataFrame([dict(ts=pd.Timestamp(NOW)+pd.Timedelta(seconds=s),ltp=p,expiry='2026-11-19',vol_cum=i)
                             for i,(s,p) in enumerate(values)])

    def forced(self,bars,quote,policy,now,fees):
        return {'continuation_v1':dict(status='ready',side='LONG',entry=100,stop=90,target=120,
                contract_expiry=quote['expiry'],confirmation_bar=str(NOW-dt.timedelta(minutes=5)),confirmed_at=str(NOW))}

    def run_replay(self,ticks,as_of=None):
        with patch('tradingagents_mcx.entry_research.candidates',side_effect=self.forced):
            return replay(ticks,self.policy,8,NOW.date(),NOW.date(),as_of or NOW+dt.timedelta(hours=8),NOW.date())

    def test_observed_stop_gap_and_fees_are_charged_not_idealized_fills(self):
        ticks=self.ticks([(0,100),(10,101),(200,85),(300,100)])
        result=self.run_replay(ticks)
        trade=result['trades']['continuation_v1'][0]
        self.assertEqual(trade['exit'],85)
        self.assertEqual(trade['net_points'],-23)
        self.assertEqual(trade['max_observation_gap_seconds'],190)
        self.assertEqual(result['summaries'][0]['gap_affected'],1)
        self.assertEqual(result['trades']['continuation_v1'][1]['status'],'unresolved')

    def test_unobserved_exit_remains_unresolved_and_future_data_is_excluded(self):
        ticks=self.ticks([(0,100),(10,101),(20,121)])
        result=self.run_replay(ticks,NOW+dt.timedelta(seconds=15))
        self.assertEqual(result['summaries'][0]['closed'],0)
        self.assertEqual(result['summaries'][0]['unresolved'],1)

    def test_daily_cap_and_squareoff_apply_without_overnight_exit(self):
        ticks=self.ticks([(0,100),(10,121),(300,100),(310,121),(600,100),(610,121),(900,100),(910,121)])
        result=self.run_replay(ticks)
        self.assertEqual(result['summaries'][0]['closed'],3)
        self.policy['square_off']='16:05'
        result=self.run_replay(self.ticks([(0,100),(301,99)]))
        self.assertEqual(result['trades']['continuation_v1'][0]['close_reason'],'square_off')

    def test_tick_reader_excludes_future_files_and_does_not_modify_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'CRUDEOIL_20261021.csv';self.ticks([(0,100),(10,101)]).to_csv(p,index=False)
            original=p.read_bytes()
            future=Path(folder)/'CRUDEOIL_20261022.csv';future.write_text('invalid future data')
            ticks,meta=read_ticks(folder,'CRUDEOIL',NOW)
            self.assertEqual(len(ticks),1)
            self.assertEqual(len(meta['sources']),1)
            self.assertEqual(p.read_bytes(),original)


if __name__=='__main__':unittest.main()
