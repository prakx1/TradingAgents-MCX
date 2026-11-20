import copy
import datetime as dt
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import scheduler
from tradingagents_mcx.config import _DEFAULTS


class SchedulerTests(unittest.TestCase):
    def test_fifteen_minute_schedule_adds_reviews_without_extending_entry_window(self):
        cfg=copy.deepcopy(_DEFAULTS)
        cfg['strategies']={'intraday':{'interval_hours':0.25}}
        jobs=scheduler.jobs_for_day(dt.date(2026,10,28),cfg)
        slots=[j['when'] for j in jobs if j['label']=='interval']
        self.assertEqual([t.strftime('%H:%M') for t in slots[:4]],['09:15','09:30','09:45','10:00'])
        self.assertEqual(slots[-1].strftime('%H:%M'),'22:45')
        self.assertTrue(all(j['strategy']=='intraday' for j in jobs))

    def test_finished_review_with_lingering_process_does_not_block_next_slot(self):
        class StopLoop(Exception): pass
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cfg=copy.deepcopy(_DEFAULTS)
            cfg['paths']=dict(reports_dir=str(root/'reports'),cache_dir=str(root/'cache'))
            child=Mock(pid=12345);child.poll.return_value=None
            times=[dt.datetime(2026,10,28,3),dt.datetime(2026,10,28,9,20),dt.datetime(2026,10,28,9,20)]
            with patch.object(scheduler,'ROOT',root),patch.object(scheduler,'load_config',return_value=cfg), \
                 patch.object(scheduler,'now_ist',side_effect=times), \
                 patch('tradingagents_mcx.opportunities.scan_once',return_value={'status':'watching'}), \
                 patch('tradingagents_mcx.runs.active_run',return_value={}), \
                 patch('tradingagents_mcx.runs.subprocess.Popen',return_value=child) as launch, \
                 patch.object(scheduler.time,'sleep',side_effect=[None,None,StopLoop]), \
                 patch('sys.stdout',new_callable=io.StringIO):
                with self.assertRaises(StopLoop):scheduler.main()
            self.assertEqual(launch.call_count,2)
            self.assertIn('--watch',launch.call_args_list[0].args[0])
            self.assertNotIn('--watch',launch.call_args_list[1].args[0])


if __name__=='__main__':unittest.main()
