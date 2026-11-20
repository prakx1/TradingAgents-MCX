import copy
import datetime as dt
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from tradingagents_mcx.regimes import MODELS, classify_regime, regime_candidates
from tradingagents_mcx.regime_research import ReplayProtocol, replay
from tradingagents_mcx.strategies import DEFAULTS


NOW = dt.datetime(2026, 10, 21, 16)


class RegimeTests(unittest.TestCase):
    def setUp(self):
        self.policy = copy.deepcopy(DEFAULTS['intraday'])
        close = 8000 + np.arange(84) * 50.
        self.bars = pd.DataFrame(dict(open=close - 2, high=close + 5, low=close - 5, close=close, volume=500.),
            index=pd.date_range(end=NOW - dt.timedelta(minutes=5), periods=84, freq='5min'))
        self.bars.attrs['contract_expiry'] = '2026-11-19'
        self.quote = dict(ts=str(NOW), ltt=str(NOW), expiry='2026-11-19', ltp=close[-1])

    def test_trend_and_short_are_symmetric_and_costs_keep_levels_fixed(self):
        state, proposals = regime_candidates(self.bars, self.quote, self.policy, NOW, 8)
        self.assertEqual(state['regime'], 'trend_up'); self.assertEqual(proposals['trend_only']['status'], 'ready')
        _, expensive = regime_candidates(self.bars, self.quote, self.policy, NOW, 80)
        self.assertEqual(expensive['trend_only']['status'], 'unavailable')
        for name in ('stop', 'target'): self.assertEqual(expensive['trend_only'][name], proposals['trend_only'][name])
        b = self.bars.copy()
        for target, source in [('open','open'), ('close','close'), ('high','low'), ('low','high')]: b[target] = 25000 - self.bars[source]
        q = dict(self.quote, ltp=25000 - self.quote['ltp'])
        state, proposals = regime_candidates(b, q, self.policy, NOW, 8)
        self.assertEqual(state['regime'], 'trend_down'); self.assertEqual(proposals['trend_only']['side'], 'SHORT')

    def range_bars(self):
        c = 8000 - 70 * np.sin(np.arange(84) * np.pi / 5)
        b = self.bars.copy()
        b['open'] = c - 2; b['high'] = c + 5; b['low'] = c - 5; b['close'] = c
        b.loc[b.index[-1], ['open','high','low','close']] = [7897,7905,7896,7903]
        return b

    def test_range_rejection_has_fixed_mean_target_and_symmetric_short(self):
        b = self.range_bars(); q = dict(self.quote, ltp=7903)
        state, proposals = regime_candidates(b, q, self.policy, NOW, 8)
        long = proposals['range_only']
        self.assertEqual(state['regime'], 'range'); self.assertEqual(long['status'], 'ready')
        self.assertLess(long['stop'], b.tail(21).low.min())
        self.assertEqual(long['target'], int(np.floor(b.close.iloc[-21:-1].mean())))
        self.assertEqual(proposals['regime_switch'], long)
        mirror = b.copy()
        for target, source in [('open','open'), ('close','close'), ('high','low'), ('low','high')]: mirror[target] = 16000 - b[source]
        state, proposals = regime_candidates(mirror, dict(q, ltp=8097), self.policy, NOW, 8)
        short = proposals['range_only']
        self.assertEqual(state['regime'], 'range'); self.assertEqual(short['status'], 'ready')
        self.assertEqual(short['side'], 'SHORT'); self.assertEqual(short['target'], 16000 - long['target'])

    def test_no_future_information_or_input_mutation(self):
        original = self.bars.copy(deep=True)
        before = regime_candidates(self.bars, self.quote, self.policy, NOW, 8)
        future = self.bars.iloc[-1:].copy(); future.index = [pd.Timestamp(NOW)]
        future['high'] = 999999; future['close'] = 999999
        extended = pd.concat([self.bars, future]); extended.attrs = self.bars.attrs.copy()
        self.assertEqual(regime_candidates(extended, self.quote, self.policy, NOW, 8), before)
        pd.testing.assert_frame_equal(original, self.bars)

    def test_shock_gaps_contract_and_freshness_block_proposals(self):
        b = self.bars.copy(); b.loc[b.index[-1], 'high'] += 1000
        state, proposals = regime_candidates(b, self.quote, self.policy, NOW, 8)
        self.assertEqual(state['regime'], 'unstable'); self.assertEqual(proposals['regime_switch']['status'], 'unavailable')
        self.assertEqual(classify_regime(self.bars.drop(self.bars.index[-10]), NOW)['regime'], 'unavailable')
        b = self.bars.copy(); b['research_usable'] = True; b.loc[b.index[-2], 'research_usable'] = False
        self.assertEqual(classify_regime(b, NOW)['regime'], 'unavailable')
        for change in [dict(expiry='2026-12-18'), dict(ltt=str(NOW - dt.timedelta(seconds=121))), dict(ltp=float('nan'))]:
            _, proposals = regime_candidates(self.bars, dict(self.quote, **change), self.policy, NOW, 8)
            self.assertTrue(all(p['status'] == 'unavailable' for p in proposals.values()))

    def test_missing_old_volume_does_not_erase_price_only_warmup(self):
        before = classify_regime(self.bars, NOW)
        b = self.bars.copy(); b.loc[b.index[0], 'volume'] = float('nan')
        self.assertEqual(classify_regime(b, NOW), before)
        b.loc[b.index[-1], 'volume'] = float('nan')
        self.assertEqual(classify_regime(b, NOW)['regime'], 'unavailable')


class ReplayTests(unittest.TestCase):
    def setUp(self): self.policy = copy.deepcopy(DEFAULTS['intraday'])

    def forced(self, bars, quote, policy, now, fees, slip):
        p = dict(status='ready', side='LONG', entry=100, stop=80, target=160, atr14=20,
                 contract_expiry=quote['expiry'], confirmation_bar=str(NOW - dt.timedelta(minutes=5)),
                 confirmed_at=str(NOW), regime='trend_up', net_floor_required=True)
        return dict(regime='trend_up', reason='test'), {m: p.copy() for m in MODELS}

    def run_replay(self, values, as_of=None):
        ticks = pd.DataFrame([dict(ts=pd.Timestamp(NOW) + pd.Timedelta(seconds=s), ltp=p,
                         expiry='2026-11-19', vol_cum=i) for i, (s,p) in enumerate(values)])
        with patch('tradingagents_mcx.regime_research.regime_candidates', side_effect=self.forced):
            return replay(ticks, self.policy, NOW.date(), NOW.date(), as_of or NOW + dt.timedelta(hours=8), NOW.date())

    def test_delayed_adverse_fills_and_stop_gap_charge_cost_once(self):
        r = self.run_replay([(0,100),(10,102),(30,101),(40,100),(250,75)])
        t = r['trades']['regime_switch'][0]
        self.assertEqual(t['opened_at'], str(NOW + dt.timedelta(seconds=30)))
        self.assertEqual(t['entry'], 102); self.assertEqual(t['exit'], 74)
        self.assertEqual(t['net_points'], -36); self.assertEqual(t['fees_points'], 8)
        self.assertEqual(t['max_observation_gap_seconds'], 210)
        self.assertEqual(r['summaries'][-3]['gap_affected'], 1)

    def test_pre_entry_target_touch_cancels_and_missing_entry_cannot_fill_later(self):
        for values in [[(0,100),(10,161),(30,100)], [(0,100),(10,79),(30,100)], [(0,100),(121,100)]]:
            r = self.run_replay(values)
            self.assertEqual(r['trades']['regime_switch'], [])
            self.assertTrue(r['execution_rejections'])

    def test_net_floor_is_rechecked_after_delay_and_levels_are_not_stretched(self):
        r = self.run_replay([(0,100),(30,109),(40,161)])
        self.assertEqual(r['trades']['regime_switch'], [])
        self.assertIn('reward/risk', r['execution_rejections'][0]['reason'])

    def test_daily_cap_squareoff_and_unresolved_exit(self):
        r = self.run_replay([(0,100),(30,100),(40,161),(300,100),(330,100),(340,161),
                             (600,100),(630,100),(640,161),(900,100),(930,100),(940,161)])
        self.assertEqual(len(r['trades']['regime_switch']), 3)
        self.policy['square_off'] = '16:05'
        r = self.run_replay([(0,100),(30,100),(301,99)])
        self.assertEqual(r['trades']['regime_switch'][0]['close_reason'], 'square_off')
        r = self.run_replay([(0,100),(30,100),(40,161)], as_of=NOW + dt.timedelta(seconds=35))
        self.assertEqual(r['trades']['regime_switch'][0]['status'], 'unresolved')

    def test_protocol_rejects_bad_execution_assumptions(self):
        for fields in [dict(fee_points=float('nan')), dict(daily_entry_limit=0), dict(execution_delay_seconds=121)]:
            with self.assertRaises(ValueError): ReplayProtocol(**fields)


if __name__ == '__main__': unittest.main()
