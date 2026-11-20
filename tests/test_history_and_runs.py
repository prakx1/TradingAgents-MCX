import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tradingagents_mcx.config import _DEFAULTS
from tradingagents_mcx.history import history_records, learning_context
from tradingagents_mcx.runs import RunTracker, active_run, launch_council, read_state

NOW = dt.datetime(2026,10,12,14)


class HistoryAndRunTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cfg=copy.deepcopy(_DEFAULTS)
        self.cfg['paths']={'reports_dir':str(self.root/'reports'),'cache_dir':str(self.root/'cache')}

    def write(self,path,rows):
        file=self.root/path;file.parent.mkdir(parents=True,exist_ok=True)
        file.write_text(''.join(json.dumps(r)+'\n' for r in rows));return file

    def trade(self,ident='old-1',**changes):
        return dict(id=ident,trade_id=ident,symbol='CRUDEOIL',side='LONG',entry=8000,stop=7950,
                    target=8100,close_price=8100,filled_at='2026-10-09 10:00:00',
                    opened_ts='2026-10-09 10:00:00',status='target_hit',closed='2026-10-09',**changes)

    def test_learning_excludes_overnight_unfinished_and_future_outcomes(self):
        good=self.trade();overnight=self.trade('overnight');overnight['closed']='2026-10-10'
        future=self.trade('future');future.update(filled_at='2026-10-13 10:00',closed='2026-10-13')
        pending=self.trade('unresolved');pending.update(status='open',closed=None)
        same_day_unknown_time=self.trade('today');same_day_unknown_time.update(filled_at='2026-10-12 10:00',closed='2026-10-12')
        file=self.write('ledger/calls.jsonl',[good,overnight,future,pending,same_day_unknown_time])
        before=file.read_bytes()
        result=learning_context(self.cfg,NOW)
        self.assertEqual(result['outcome_ids'],['old-1'])
        self.assertEqual(result['eligible_outcomes'],1)
        self.assertEqual(len(history_records(self.cfg)),5)
        self.assertEqual(file.read_bytes(),before)

    def test_lessons_must_be_resolved_and_written_before_decision(self):
        self.write('ledger/calls.jsonl',[self.trade()])
        lesson=dict(id='allowed',trade_id='old-1',resolved_at='2026-10-09 14:00',written_at='2026-10-09 15:00',
                    state='active',lesson='Wait for confirmation.')
        future={**lesson,'id':'future','written_at':'2026-10-13 15:00'}
        unrelated={**lesson,'id':'unknown-trade','trade_id':'not-a-closed-trade'}
        retired={**lesson,'id':'retired','state':'retired'}
        file=self.write('memory/lessons.jsonl',[lesson,future,unrelated,retired]);before=file.read_bytes()
        result=learning_context(self.cfg,NOW)
        self.assertEqual(result['lesson_ids'],['allowed'])
        self.assertEqual(file.read_bytes(),before)

    def test_history_normalizes_R_without_account_fields_and_deduplicates_learning(self):
        one=self.trade();duplicate={**one,'id':'repeated-decision'}
        self.write('ledger/calls.jsonl',[one,duplicate])
        record=history_records(self.cfg)[0]
        self.assertEqual(record['outcome_r'],2)
        self.assertFalse({'capital','lots','pnl_inr','position_pct'} & set(record))
        self.assertEqual(learning_context(self.cfg,NOW)['eligible_outcomes'],1)

    def test_tracker_exposes_progress_then_finishes(self):
        tracker=RunTracker(self.cfg,'intraday',False)
        tracker.progress('[2/4] Analysts ready: 1/3')
        self.assertEqual(active_run(self.cfg)['stage'],2)
        tracker.finish(result={'cio':{'final_decision':'STAND ASIDE'},'signal':{'status':'stand_aside'}})
        self.assertEqual(active_run(self.cfg),{})
        self.assertEqual(read_state(self.cfg)['status'],'completed')

    def test_launch_rejects_running_process_without_spawning(self):
        tracker=RunTracker(self.cfg,'intraday',False)
        with patch('tradingagents_mcx.runs.subprocess.Popen') as spawn:
            started,message=launch_council(self.cfg,watch=True)
        self.assertFalse(started);self.assertIn('already running',message);spawn.assert_not_called()

    def test_launch_checks_actual_lock_even_without_metadata(self):
        with (self.root/'.council.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch('tradingagents_mcx.runs.subprocess.Popen') as spawn:
                started,message=launch_council(self.cfg)
        self.assertFalse(started);spawn.assert_not_called()

    def test_dead_process_is_visible_as_interrupted(self):
        tracker=RunTracker(self.cfg,'intraday',False)
        with patch('tradingagents_mcx.runs.alive',return_value=False):
            self.assertEqual(read_state(self.cfg)['status'],'interrupted')
            self.assertEqual(active_run(self.cfg),{})


if __name__=='__main__':unittest.main()
