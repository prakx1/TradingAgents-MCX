import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from tradingagents_mcx.config import _DEFAULTS
from tradingagents_mcx.history import learning_context

NOW = dt.datetime(2026, 10, 19, 12)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = copy.deepcopy(_DEFAULTS)
        self.cfg['paths'] = dict(reports_dir=str(self.root/'reports'), cache_dir=str(self.root/'cache'))

    def write(self, name, rows):
        path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        return path

    def trade(self, **changes):
        return dict(dict(id='paper-one', trade_id='P-one', signal_id='one', origin='intraday',
                         status='closed', side='SHORT', opened_at='2026-10-16 14:47:30',
                         recorded_at='2026-10-16 14:47:31', closed_at='2026-10-16 15:18:59',
                         entry=9205, stop=9222, target=9173, exit=9229, lots=4, lot_size_bbl=10,
                         fees_inr=320, pnl_inr=-1280, close_reason='stop_hit', entry_method='trend_continuation_v1'), **changes)

    def test_new_paper_results_reach_learning_with_frozen_fees_and_source_ids(self):
        path = self.write('ledger/paper_intraday.jsonl', [self.trade()]); before = path.read_bytes()
        self.cfg['paper'] = dict(roundtrip_cost_per_lot_inr=999)
        result = learning_context(self.cfg, NOW)
        feedback = result['intraday_feedback']
        self.assertEqual(feedback['summary']['net_pnl_inr'], -1280)
        self.assertEqual(feedback['summary']['fees_inr'], 320)
        self.assertEqual(feedback['trades'][0]['planned_net_reward_risk'], .96)
        self.assertEqual(feedback['trades'][0]['stop_gap_points'], 7)
        self.assertIn('L-P-one', result['lesson_ids'])
        self.assertIn('Net paper P&L', result['context'])
        self.assertEqual(path.read_bytes(), before)

    def test_open_future_overnight_and_legacy_trades_cannot_leak_into_new_performance(self):
        self.write('ledger/paper_intraday.jsonl', [self.trade(), self.trade(id='open', status='open'),
            self.trade(id='future', closed_at='2026-10-20 12:00'),
            self.trade(id='written-later', recorded_at='2026-10-20 12:00'),
            self.trade(id='overnight', closed_at='2026-10-17 12:00'),
            self.trade(id='legacy', origin='legacy_recovered')])
        feedback = learning_context(self.cfg, NOW)['intraday_feedback']
        self.assertEqual(feedback['summary']['closed'], 1)
        self.assertEqual(len(feedback['trades']), 1)

    def test_setup_feedback_uses_only_events_known_at_the_decision(self):
        self.write('signals/intraday_setups.jsonl', [dict(id='setup-1', created_at='2026-10-19 10:00',
            updated_at='2026-10-19 13:00', status='missed', reason='Target reached before the setup could issue a signal',
            events=[dict(at='2026-10-19 10:00', status='pending', reason='Waiting for confirmation'),
                    dict(at='2026-10-19 13:00', status='missed', reason='Target reached before the setup could issue a signal')])])
        result = learning_context(self.cfg, NOW)['intraday_feedback']['entries']
        self.assertEqual(result['setup_statuses'], {'pending': 1})
        self.assertEqual(result['target_before_entry'], 0)

    def test_duplicate_rows_do_not_double_count_and_bad_economics_are_excluded(self):
        self.write('ledger/paper_intraday.jsonl', [self.trade(), self.trade(),
            self.trade(id='invalid', fees_inr=float('nan'))])
        summary = learning_context(self.cfg, NOW)['intraday_feedback']['summary']
        self.assertEqual(summary['closed'], 1)
        self.assertEqual(summary['excluded_records'], 1)

    def test_learning_has_no_mutation_or_extra_model_call(self):
        self.write('ledger/paper_intraday.jsonl', [self.trade()])
        original = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        learning_context(self.cfg, NOW)
        self.assertEqual(original, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_rapid_stop_feedback_is_point_in_time_and_does_not_count_wins(self):
        self.write('ledger/paper_intraday.jsonl',[self.trade(closed_at='2026-10-16 14:49:30'),
            self.trade(id='win',trade_id='P-win',closed_at='2026-10-16 14:50:30',exit=9173,
                       pnl_inr=960,close_reason='target_hit'),
            self.trade(id='future',closed_at='2026-10-20 12:00')])
        result=learning_context(self.cfg,NOW)
        summary=result['intraday_feedback']['summary']
        self.assertEqual(summary['rapid_stops'],1)
        self.assertEqual(summary['closed'],2)
        self.assertEqual(summary['win_rate_pct'],50)
        self.assertEqual(summary['profit_factor'],.75)
        self.assertEqual(result['intraday_feedback']['trades'][0]['holding_seconds'],120)
        self.assertIn('stop within five minutes',result['context'])
        self.assertIn('latest observed entry price',result['context'])

    def test_same_tick_setup_transitions_use_the_last_recorded_event(self):
        self.write('signals/intraday_setups.jsonl',[dict(id='setup-1',created_at='2026-10-19 10:00',
            events=[dict(at='2026-10-19 10:00',status='pending',reason='Waiting'),
                    dict(at='2026-10-19 10:00',status='missed',reason='Target reached before the setup could issue a signal')])])
        entries=learning_context(self.cfg,NOW)['intraday_feedback']['entries']
        self.assertEqual(entries['setup_statuses'],{'missed':1})
        self.assertEqual(entries['target_before_entry'],1)

    def test_conditional_cost_review_uses_worst_endpoint_for_both_directions(self):
        from tradingagents_mcx.economics import review_plan_costs
        from tradingagents_mcx.strategies import strategy_config
        cfg=strategy_config(self.cfg,'intraday')
        costs=review_plan_costs(cfg,dict(direction='BULLISH',entry=8000,stop=7980,target=8040))
        self.assertFalse(costs['below_gross_signal_floor'])
        self.assertTrue(costs['net_below_same_ratio'])
        long=review_plan_costs(cfg,dict(direction='BULLISH',entry='WAIT',
            setup=dict(entry_min=8000,entry_max=8010,stop=7950,target=8100)))
        short=review_plan_costs(cfg,dict(direction='BEARISH',entry='WAIT',
            setup=dict(entry_min=7990,entry_max=8000,stop=8050,target=7900)))
        self.assertEqual(long['entry'],8010)
        self.assertEqual(short['entry'],7990)
        self.assertAlmostEqual(long['net_reward_risk'],82/68,places=4)
        self.assertEqual(short['net_reward_risk'],long['net_reward_risk'])

    def test_cost_review_does_not_invent_a_probability_when_target_cannot_cover_fees(self):
        from tradingagents_mcx.economics import plan_economics
        cost=plan_economics(8000,7950,8005,'LONG',8)
        self.assertLess(cost['net_reward_risk'],0)
        self.assertIsNone(cost['breakeven_win_rate_pct'])
        with self.assertRaises(ValueError):plan_economics(8000,8050,8100,'LONG',8)


if __name__ == '__main__': unittest.main()
