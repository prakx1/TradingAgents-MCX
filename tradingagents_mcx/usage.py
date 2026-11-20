"""Read the existing token/cost journal without repricing or rewriting history."""
from pathlib import Path
import math

import pandas as pd

from .history import read_lines
from .llm import PRICES
from .strategies import now_ist


def usage_summary(cfg,now=None):
    now=now or now_ist()
    rows=[]
    for raw in read_lines(Path(cfg['paths']['cache_dir'])/'llm_usage.jsonl'):
        try:
            stamp=pd.Timestamp(raw['ts'])
            if stamp.tzinfo:stamp=stamp.tz_convert('Asia/Kolkata').tz_localize(None)
            if pd.isna(stamp) or stamp>now:continue
            usd=float(raw['usd']) if raw.get('usd') is not None else None
            if usd is not None and (not math.isfinite(usd) or usd<0):usd=None
            model=raw.get('model','unspecified')
            priced=usd is not None and (usd>0 or model in PRICES or raw.get('pricing_known') is True)
            rows.append(dict(ts=str(stamp),date=str(stamp.date()),provider=raw.get('provider') or 'legacy / unspecified',
                             model=model,role=raw.get('role') or 'unspecified',input_tokens=int(raw.get('in') or 0),
                             output_tokens=int(raw.get('out') or 0),usd=usd if priced else None,priced=priced))
        except (ValueError,TypeError,KeyError):continue
    frame=pd.DataFrame(rows,columns=['ts','date','provider','model','role','input_tokens','output_tokens','usd','priced'])
    today=frame[frame.date==now.date().isoformat()]
    return dict(rows=frame,today=today,today_usd=float(today.usd.sum()),total_usd=float(frame.usd.sum()),
                today_calls=len(today),total_calls=len(frame),unpriced_calls=int(sum(not x for x in frame.priced)),
                input_tokens=int(frame.input_tokens.sum()),output_tokens=int(frame.output_tokens.sum()))
