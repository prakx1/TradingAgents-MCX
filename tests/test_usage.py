import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from tradingagents_mcx.usage import usage_summary


class UsageTests(unittest.TestCase):
    def test_saved_costs_dates_unpriced_calls_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'llm_usage.jsonl'
            rows=[dict(ts='2026-10-12T10:00:00',model='older-model',role='trader',usd=1.25,**{'in':100,'out':50}),
                  dict(ts='2026-10-13T10:00:00',model='gpt-6-luna',provider='openai',role='analyst',usd=.05,**{'in':200,'out':30}),
                  dict(ts='2026-10-13T10:01:00',model='unknown',usd=0),
                  dict(ts='2026-10-14T10:00:00',model='gpt-6-luna',usd=99)]
            original=''.join(json.dumps(r)+'\n' for r in rows)+'{"partial":'
            path.write_text(original)
            s=usage_summary({'paths':{'cache_dir':tmp}},dt.datetime(2026,10,13,14))
            self.assertEqual(s['total_calls'],3)
            self.assertEqual(s['today_calls'],2)
            self.assertEqual(s['today_usd'],.05)
            self.assertEqual(s['total_usd'],1.3)
            self.assertEqual(s['unpriced_calls'],1)
            self.assertEqual(s['input_tokens'],300)
            self.assertEqual(path.read_text(),original)

    def test_empty_usage_is_zero_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(usage_summary({'paths':{'cache_dir':tmp}})['total_calls'],0)
