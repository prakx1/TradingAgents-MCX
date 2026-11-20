"""A WAIT plan must survive the council and activate only on new observations."""
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
from tradingagents_mcx.setups import PendingSetups

NOW = dt.datetime(2026, 10, 12, 14)


class SetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = copy.deepcopy(_DEFAULTS)
        self.cfg['paths'] = dict(reports_dir=str(self.root/'reports'), cache_dir=str(self.root/'cache'))
        self.scfg = strategy_config(self.cfg, 'intraday')
        self.live = self.root/'cache/live'; self.live.mkdir(parents=True)
        self.store = PendingSetups(self.scfg)
        self.quote(NOW, 8000)

    def quote(self, when, price, expiry='2026-10-19'):
        (self.live/'latest_CRUDEOIL.json').write_text(json.dumps(dict(ts=str(when),ltp=price,expiry=expiry)))

    def result(self, side='LONG'):
        long = side == 'LONG'
        return dict(symbol='CRUDEOIL', contract_expiry='2026-10-19', entry_eligible=True,
            decision_evidence={'as_of':str(NOW)},
            cio=dict(final_decision=f'CAUTIOUS {side}',setup_approval='APPROVE'),
            risk=dict(setup_approval='APPROVE'),
            trader=dict(direction='BULLISH' if long else 'BEARISH',entry='WAIT',confidence='MEDIUM',
                setup=dict(trigger='breakout_retest',level=8010 if long else 7990,
                    entry_min=8011 if long else 7980,entry_max=8020 if long else 7989,
                    stop=7980 if long else 8020,target=8100 if long else 7900,
                    retest_tolerance=2,valid_for_minutes=60,momentum='none')))

    def ticks(self, prices, start=NOW):
        rows = [dict(ts=start+dt.timedelta(minutes=i),ltp=p,expiry='2026-10-19',vol_cum=i*100)
                for i,p in enumerate(prices)]
        pd.DataFrame(rows).to_csv(self.live/'CRUDEOIL_20261012.csv',index=False)

    def test_wait_plan_activates_via_guard_after_close_retest_and_fresh_entry(self):
        pending = self.store.register(self.result(), NOW)
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(SignalBook(self.scfg).load(), [])
        self.ticks([8000,8005,8012,8014,8013,8012,8010,8011,8011,8012,8015])
        now = NOW+dt.timedelta(minutes=10)
        self.quote(now, 8015)
        maintain_all(self.cfg, now)
        signal = SignalBook(self.scfg).load()[0]
        self.assertEqual(signal['entry'],8015)
        self.assertEqual(signal['issued_at'],str(now))
        self.assertEqual(signal['setup_id'],pending['id'])
        self.assertEqual(self.store.load()[0]['status'],'activated')
        maintain_all(self.cfg,now)
        self.assertEqual(len(SignalBook(self.scfg).load()),1)

    def test_unfinished_retest_candle_cannot_trigger(self):
        self.store.register(self.result(),NOW)
        self.ticks([8000,8005,8012,8014,8013,8012,8010,8011,8011,8012])
        now=NOW+dt.timedelta(minutes=9);self.quote(now,8012)
        maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load(),[])
        self.assertEqual(self.store.load()[0]['phase'],'waiting_retest')

    def test_historical_confirmation_is_never_backfilled(self):
        self.ticks([8000,8005,8012,8014,8013,8012,8010,8011,8011,8012], NOW-dt.timedelta(minutes=10))
        self.store.register(self.result(),NOW)
        self.quote(NOW,8015)
        maintain_all(self.cfg,NOW)
        self.assertEqual(SignalBook(self.scfg).load(),[])

    def test_chase_rejected_and_record_preserved(self):
        self.store.register(self.result(),NOW)
        self.ticks([8000,8005,8012,8014,8013,8012,8010,8011,8011,8012,8050])
        now=NOW+dt.timedelta(minutes=10);self.quote(now,8050)
        maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load(),[])
        self.assertEqual(self.store.load()[0]['status'],'missed')

    def test_older_approved_setups_keep_their_original_rules(self):
        pending=self.store.register(self.result('SHORT'),NOW)
        rows=self.store.load()
        rows[0].pop('rule_version')
        self.store.save(rows)
        self.quote(NOW,7970)
        maintain_all(self.cfg,NOW)
        old=self.store.load()[0]
        self.assertEqual(old['id'],pending['id'])
        self.assertEqual(old['status'],'missed')
        self.assertEqual(SignalBook(self.scfg).load(),[])

    def test_unapproved_or_unknown_conditions_never_arm(self):
        for field in ('risk','cio'):
            r=self.result();r[field]['setup_approval']='REJECT'
            self.assertEqual(self.store.register(r,NOW)['status'],'blocked')
        r=self.result();r['trader']['setup']['extra_confirmation']='news improves'
        self.assertEqual(self.store.register(r,NOW)['status'],'blocked')
        self.assertEqual(self.store.load(),[])

    def test_expired_setup_cannot_issue(self):
        self.store.register(self.result(),NOW)
        now=NOW+dt.timedelta(minutes=61);self.quote(now,8015)
        maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'expired')
        self.assertEqual(SignalBook(self.scfg).load(),[])

    def confirmed_ticks(self):
        self.ticks([8000,8005,8012,8014,8013,8012,8010,8011,8011,8012,8015])
        now=NOW+dt.timedelta(minutes=10)
        self.quote(now,8015)
        return now

    def test_short_setup_uses_inverse_conditions(self):
        self.store.register(self.result('SHORT'),NOW)
        self.ticks([8000,7995,7988,7986,7987,7988,7990,7989,7989,7988,7985])
        now=NOW+dt.timedelta(minutes=10);self.quote(now,7985)
        maintain_all(self.cfg,now)
        signal=SignalBook(self.scfg).load()[0]
        self.assertEqual((signal['side'],signal['entry']),('SHORT',7985))

    def test_close_beyond_does_not_require_a_retest(self):
        r=self.result();r['trader']['setup']['trigger']='close_beyond'
        self.store.register(r,NOW)
        self.ticks([8000,8005,8012,8014,8013,8015])
        now=NOW+dt.timedelta(minutes=5);self.quote(now,8015)
        maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'activated')

    def test_stale_quote_waits_then_resumes_with_new_quote(self):
        self.store.register(self.result(),NOW)
        now=self.confirmed_ticks();self.quote(NOW,8015)
        maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load(),[])
        self.quote(now,8015);maintain_all(self.cfg,now)
        self.assertEqual(len(SignalBook(self.scfg).load()),1)

    def test_fresh_quote_clears_stale_feed_reason_while_waiting_for_pattern(self):
        self.store.register(self.result(),NOW)
        self.ticks([8000,8000,8000,8000,8000,8000])
        now=NOW+dt.timedelta(minutes=5)
        maintain_all(self.cfg,now)
        self.assertIn('stale',self.store.load()[0]['reason'].lower())
        self.quote(now,8000)
        maintain_all(self.cfg,now)
        row=self.store.load()[0]
        self.assertEqual(row['status'],'pending')
        self.assertNotIn('stale',row['reason'].lower())
        self.assertIn('close',row['reason'].lower())

    def test_risk_refresh_does_not_extend_traders_setup_lifetime(self):
        r=self.result()
        now=NOW+dt.timedelta(minutes=2)
        r['setup_evidence_as_of']=str(NOW)
        r['decision_evidence']['as_of']=str(now)
        self.quote(now,8000)
        row=self.store.register(r,now)
        self.assertEqual(row['valid_until'],str(NOW+dt.timedelta(minutes=60)))

    def test_wrong_contract_and_failed_retest_cancel(self):
        self.store.register(self.result(),NOW)
        now=self.confirmed_ticks();self.quote(now,8015,'2026-11-19')
        maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'cancelled')
        self.quote(NOW,8000);self.store.register(self.result(),NOW)
        self.ticks([8000,8005,8012,8014,8013,8012,8010,8008,8008,8008,8015])
        self.quote(now,8015);maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[-1]['status'],'invalidated')
        self.assertEqual(SignalBook(self.scfg).load(),[])

    def test_crash_after_signal_save_recovers_without_duplicate(self):
        self.store.register(self.result(),NOW)
        now=self.confirmed_ticks()
        with patch.object(PendingSetups,'save',side_effect=OSError('crash')):
            with self.assertRaises(OSError):maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'pending')
        maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'activated')
        self.assertEqual(len(SignalBook(self.scfg).load()),1)

    def test_new_stand_aside_keeps_pending_setup_able_to_activate(self):
        first=self.store.register(self.result(),NOW)
        result=self.result();result['trader']['setup']=None
        result['cio']['final_decision']='STAND ASIDE'
        self.store.register(result,NOW+dt.timedelta(minutes=1))
        now=self.confirmed_ticks();maintain_all(self.cfg,now)
        self.assertEqual(len(SignalBook(self.scfg).load()),1)
        self.assertEqual(self.store.load()[0]['id'],first['id'])
        self.assertEqual(self.store.load()[0]['status'],'activated')

    def test_invalid_replacement_does_not_cancel_existing_setup(self):
        first=self.store.register(self.result(),NOW)
        bad=self.result('SHORT');bad['trader']['setup']['stop']=7900
        replacement=self.store.register(bad,NOW+dt.timedelta(minutes=1))
        self.assertEqual(replacement['status'],'blocked')
        self.assertEqual(self.store.load()[0]['id'],first['id'])
        self.assertEqual(self.store.load()[0]['status'],'pending')

    def test_council_can_explicitly_cancel_pending_setup(self):
        first=self.store.register(self.result(),NOW)
        result=self.result();result['trader']['setup']=None
        result['cio'].update(final_decision='STAND ASIDE',pending_setup_action='CANCEL',
                             pending_setup_id=first['id'],pending_setup_reason='Support thesis no longer holds')
        self.store.register(result,NOW+dt.timedelta(minutes=1))
        now=self.confirmed_ticks();maintain_all(self.cfg,now)
        row=self.store.load()[0]
        self.assertEqual(row['status'],'cancelled')
        self.assertIn('Support thesis no longer holds',row['reason'])
        self.assertEqual(SignalBook(self.scfg).load(),[])

    def test_cancellation_requires_exact_id_and_successful_model_review(self):
        first=self.store.register(self.result(),NOW)
        for field in ('wrong_id','degraded'):
            result=self.result();result['trader']['setup']=None
            result['cio'].update(final_decision='STAND ASIDE',pending_setup_action='CANCEL',
                pending_setup_id=first['id'] if field=='degraded' else 'unrelated-setup',
                pending_setup_reason='Changed evidence',degraded=field=='degraded')
            self.store.register(result,NOW+dt.timedelta(minutes=1))
            self.assertEqual(self.store.load()[0]['status'],'pending')

    def test_valid_replacement_preserves_old_record_as_superseded(self):
        first=self.store.register(self.result(),NOW)
        second=self.store.register(self.result('SHORT'),NOW+dt.timedelta(minutes=1))
        self.assertEqual(second['status'],'pending')
        rows=self.store.load()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['id'],first['id'])
        self.assertEqual(rows[0]['status'],'superseded')
        self.assertEqual(rows[1]['status'],'pending')

    def test_identical_reapproval_keeps_confirmation_and_original_expiry(self):
        first=self.store.register(self.result(),NOW)
        self.ticks([8000,8005,8012,8014,8013,8012,8010,8011,8011,8012,8015])
        review_at=NOW+dt.timedelta(minutes=5);self.quote(review_at,8012)
        maintain_all(self.cfg,review_at)
        self.assertEqual(self.store.load()[0]['phase'],'waiting_retest')
        review=self.result();review['decision_evidence']['as_of']=str(review_at)
        review['report_path']='later-review.md'
        repeated=self.store.register(review,review_at)
        self.assertEqual(repeated['id'],first['id'])
        self.assertEqual(repeated['valid_until'],first['valid_until'])
        self.assertEqual(repeated['phase'],'waiting_retest')
        self.assertEqual(len(self.store.load()),1)
        now=NOW+dt.timedelta(minutes=10);self.quote(now,8015)
        maintain_all(self.cfg,now)
        signals=SignalBook(self.scfg).load()
        self.assertEqual(len(signals),1)
        self.assertEqual(signals[0]['setup_id'],first['id'])

    def test_identical_plan_does_not_reactivate_an_expired_setup(self):
        first=self.store.register(self.result(),NOW)
        now=NOW+dt.timedelta(minutes=61);self.quote(now,8000)
        maintain_all(self.cfg,now)
        review=self.result();review['decision_evidence']['as_of']=str(now)
        second=self.store.register(review,now)
        self.assertNotEqual(second['id'],first['id'])
        self.assertEqual(self.store.load()[0]['status'],'expired')

    def test_signal_saved_before_replacement_crash_still_retires_pending_plan(self):
        self.store.register(self.result(),NOW)
        now=NOW+dt.timedelta(minutes=1);self.quote(now,8000)
        result=self.result('SHORT')
        result['trader'].update(entry=8000,stop=8020,target=7950,setup=None)
        with patch.object(self.store,'save',side_effect=[None,OSError('crash after signal save')]):
            with self.assertRaises(OSError):self.store.register(result,now)
        maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'superseded')
        self.assertEqual(len(SignalBook(self.scfg).load()),1)
        self.assertEqual(SignalBook(self.scfg).load()[0]['side'],'SHORT')

    def test_reward_risk_rechecked_under_current_settings(self):
        self.store.register(self.result(),NOW)
        now=self.confirmed_ticks()
        self.cfg['strategies']={'intraday':{'min_reward_risk':3}}
        maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load(),[])
        self.assertIn('below 3',self.store.load()[0]['reason'])

    def test_macd_requirement_is_not_silently_ignored(self):
        r=self.result();r['trader']['setup']['momentum']='macd_improving'
        self.store.register(r,NOW)
        now=self.confirmed_ticks();maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load(),[])
        self.assertIn('MACD',self.store.load()[0]['reason'])

    def test_last_second_quote_change_revalidates_entry_range(self):
        self.store.register(self.result(),NOW)
        now=self.confirmed_ticks()
        quote=dict(ts=str(now),ltp=8021,expiry='2026-10-19')
        with patch('tradingagents_mcx.signals.latest_quote',return_value=(quote,'')):
            maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load(),[])
        self.assertIn('entry range',self.store.load()[0]['reason'])

    def test_cached_trade_before_completed_confirmation_cannot_be_entry(self):
        self.store.register(self.result(),NOW)
        now=self.confirmed_ticks()
        q=dict(ts=str(now),ltp=8015,expiry='2026-10-19',ltt=str(now-dt.timedelta(seconds=30)))
        (self.live/'latest_CRUDEOIL.json').write_text(json.dumps(q))
        maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load(),[])
        self.assertIn('after candle confirmation',self.store.load()[0]['reason'])
        self.quote(now,8015);maintain_all(self.cfg,now)
        self.assertEqual(len(SignalBook(self.scfg).load()),1)

    def test_preview_never_arms_or_cancels_a_live_setup(self):
        pending=self.store.register(self.result(),NOW)
        r=self.result();r['kind']='night-watch'
        self.store.register(r,NOW)
        self.assertEqual(self.store.load()[0]['id'],pending['id'])
        self.assertEqual(self.store.load()[0]['status'],'pending')

    def test_pullback_waits_outside_entry_range_then_enters_on_completed_rejection(self):
        # The live 16:00 plan needed a rebound to resistance, not a fresh sell
        # at the lower current quote. Being below the range must not cancel it.
        r=self.result('SHORT');p=r['trader']['setup']
        p.update(trigger='pullback_reclaim',level=8010,entry_min=8000,entry_max=8009,stop=8030,target=7950)
        self.quote(NOW,7980)
        pending=self.store.register(r,NOW);self.assertEqual(pending['status'],'pending')
        maintain_all(self.cfg,NOW)
        self.assertEqual(self.store.load()[0]['status'],'pending')
        self.ticks([7980,7990,8004,8011,8008,8007])
        now=NOW+dt.timedelta(minutes=5);self.quote(now,8007)
        maintain_all(self.cfg,now)
        self.assertEqual(SignalBook(self.scfg).load()[0]['entry'],8007)
        from tradingagents_mcx.paper import PaperBook
        self.assertEqual(PaperBook(self.scfg).load()[0]['status'],'open')

    def test_breakout_can_extend_outside_entry_zone_before_retesting(self):
        self.store.register(self.result(),NOW)
        self.ticks([8000,8025,8030,8028,8026,8014,8010,8011,8011,8012,8015])
        now=NOW+dt.timedelta(minutes=10);self.quote(now,8015)
        maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'activated')

    def test_pullback_without_level_touch_does_not_enter(self):
        r=self.result();r['trader']['setup']['trigger']='pullback_reclaim'
        self.store.register(r,NOW)
        self.ticks([8015,8016,8018,8019,8017,8016])
        now=NOW+dt.timedelta(minutes=5);self.quote(now,8016)
        maintain_all(self.cfg,now)
        self.assertEqual(self.store.load()[0]['status'],'pending')
        self.assertEqual(SignalBook(self.scfg).load(),[])


if __name__ == '__main__':
    unittest.main()
