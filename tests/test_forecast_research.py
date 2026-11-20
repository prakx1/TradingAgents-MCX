import copy
from dataclasses import replace
import datetime as dt
import unittest

import numpy as np
import pandas as pd

from tradingagents_mcx.forecast_research import (
    ForecastProtocol, forecast_window, score_forecasts, select_windows, summarize,
)
from tradingagents_mcx.strategies import DEFAULTS


class ForecastResearchTests(unittest.TestCase):
    def setUp(self):
        self.protocol = ForecastProtocol(context_bars=20)
        self.policy = copy.deepcopy(DEFAULTS['intraday'])
        self.day = dt.date(2026, 10, 26)
        ts = pd.date_range('2026-10-26 09:00', '2026-10-26 14:00', freq='10s')
        self.ticks = pd.DataFrame(dict(ts=ts, ltp=9000 + np.arange(len(ts)) * .1,
                                      vol_cum=np.arange(len(ts)) * 2, expiry='2026-11-19'))
        self.as_of = dt.datetime(2026, 10, 26, 14)

    def windows(self, ticks=None, as_of=None):
        return select_windows(self.ticks if ticks is None else ticks, self.day, self.day,
                              as_of or self.as_of, self.policy, self.protocol)[0]

    def test_appending_future_cannot_change_existing_forecasts(self):
        before = self.ticks.copy(deep=True)
        original = self.windows()
        later = self.ticks.iloc[-20:].copy()
        later.ts += pd.Timedelta(hours=3); later.ltp = 999999
        with_future = self.windows(pd.concat([self.ticks, later]))
        self.assertTrue(original)
        self.assertEqual([forecast_window(w, self.protocol) for w in original],
                         [forecast_window(w, self.protocol) for w in with_future])
        pd.testing.assert_frame_equal(before, self.ticks)

    def test_selected_context_is_completed_same_session_and_same_contract(self):
        wrong = self.ticks.copy(); wrong.expiry = '2026-12-18'; wrong.ltp += 10000
        windows = self.windows(pd.concat([self.ticks, wrong]))
        for window in windows:
            self.assertTrue((window['context'].index + pd.Timedelta(minutes=5) <= window['cutoff']).all())
            self.assertEqual(set(window['context'].index.date), {self.day})
            self.assertEqual(window['context'].close.max() > 15000, window['contract'] == '2026-12-18')

    def test_context_outage_and_volume_reset_reject_windows_without_fill(self):
        clean = self.windows()
        broken = self.ticks[~self.ticks.ts.between('2026-10-26 11:00', '2026-10-26 11:10')]
        self.assertLess(len(self.windows(broken)), len(clean))
        reset = self.ticks.copy(); reset.loc[reset.ts >= pd.Timestamp('2026-10-26 11:00'), 'vol_cum'] -= 2000
        self.assertLess(len(self.windows(reset)), len(clean))

    def record(self):
        return dict(cutoff='2026-10-26 12:00:00', terminal='2026-10-26 12:30:00',
                    contract='2026-11-19', reference=9000., atr=20.,
                    forecasts={'long': 9030., 'short': 8970., 'wait': 9009.})

    def test_delayed_observed_entry_and_each_way_costs_apply_symmetrically(self):
        rows = score_forecasts([self.record()], self.ticks, self.as_of, self.protocol)
        by_model = {r['model']: r for r in rows}
        long, short, wait = [by_model[k] for k in ['long', 'short', 'wait']]
        self.assertEqual(long['entry'], '2026-10-26 12:00:30')
        self.assertAlmostEqual(long['probe_gross_points'], 17.7)
        self.assertAlmostEqual(long['probe_net_points'], 7.7)
        self.assertAlmostEqual(short['probe_net_points'], -27.7)
        self.assertEqual(wait['side'], 'WAIT')
        self.assertEqual(wait['probe_cost_points'], 0)

    def test_missing_future_stays_unresolved_without_changing_call(self):
        record = self.record()
        rows = score_forecasts([record], self.ticks, dt.datetime(2026, 10, 26, 12, 20), self.protocol)
        self.assertTrue(all(r['status'] == 'unresolved' for r in rows))
        self.assertEqual([r['side'] for r in rows], ['LONG', 'SHORT', 'WAIT'])
        summary = summarize(rows)
        self.assertTrue(all(r['evaluated'] == 0 and r['unresolved'] == 1 for r in summary))
        self.assertIsNone(summary[0]['probe_win_rate_pct'])

    def test_outcome_gaps_and_wrong_contract_prices_never_become_fills(self):
        gapped = self.ticks[~self.ticks.ts.between('2026-10-26 12:10', '2026-10-26 12:15')]
        rows = score_forecasts([self.record()], gapped, self.as_of, self.protocol)
        self.assertTrue(all(r['reason'] == 'observation_gap' for r in rows))
        wrong = self.ticks.copy(); wrong.expiry = '2026-12-18'
        rows = score_forecasts([self.record()], wrong, self.as_of, self.protocol)
        self.assertTrue(all(r['reason'] == 'missing_entry' for r in rows))

    def test_windows_selected_even_when_future_is_not_yet_known(self):
        windows = self.windows(as_of=dt.datetime(2026, 10, 26, 12))
        self.assertEqual(windows[-1]['cutoff'], pd.Timestamp('2026-10-26 12:00'))
        self.assertGreater(windows[-1]['terminal'], pd.Timestamp('2026-10-26 12:00'))

    def test_squareoff_and_small_sample_uncertainty(self):
        self.policy['square_off'] = '12:15'
        self.assertTrue(all(w['terminal'] < pd.Timestamp('2026-10-26 12:15') for w in self.windows()))
        rows = score_forecasts([self.record()], self.ticks, self.as_of, self.protocol)
        self.assertTrue(all(r['mean_probe_net_points_day_bootstrap_95pct'] is None for r in summarize(rows)))
        with self.assertRaises(ValueError):
            replace(self.protocol, slippage_points_each_way=float('nan'))
        with self.assertRaises(ValueError):
            replace(self.protocol, cadence_minutes=5)


if __name__ == '__main__':
    unittest.main()
