"""Read-only history and point-in-time learning from preserved records."""
from __future__ import annotations
import datetime as dt
import json
import math
import re
from pathlib import Path
import pandas as pd
from .strategies import now_ist, strategy_config


def decision_reason(report):
    """Explain old and new decisions from recorded evidence without rewriting them."""
    signal = report.get('signal') or {}
    reason = signal.get('reason', '')
    if reason=='Conditional setup has no matching directional approval' and report.get('cio',{}).get('signal_reason'):
        return report['cio']['signal_reason']
    if reason and not reason.startswith('Council stands aside;'):
        return reason
    cio, trader, risk = (report.get(k) or {} for k in ('cio','trader','risk'))
    if report.get('entry_eligible') is False:
        return report.get('entry_block_reason') or 'Required data or model review is incomplete.'
    if cio.get('signal_reason'):
        return cio['signal_reason']
    if cio.get('degraded') or risk.get('degraded'):
        return 'The risk/CIO model review was unavailable or incomplete.'
    tally = trader.get('module_tally') or {}
    if trader.get('direction') == 'NEUTRAL':
        return (f"Neutral analyst vote ({tally.get('BULLISH',0)} bullish, {tally.get('BEARISH',0)} bearish, "
                f"{tally.get('NEUTRAL',0)} neutral); no directional entry approved.")
    entry = str(trader.get('entry') or '').strip().upper()
    if entry in ('','WAIT','N/A','NONE'):
        return f"{trader.get('direction','No directional')} bias, but the trader selected WAIT; no confirmed entry, stop and target."
    deviation = re.search(r'DEVIATING FROM MATRIX because\s+([^\n]+)',cio.get('statement',''),re.I)
    if deviation:
        return deviation.group(1).strip()[:500]
    if cio.get('final_decision') == 'STAND ASIDE':
        return 'CIO did not approve the proposed setup; see the saved risk review and decision.'
    return signal.get('side') or cio.get('final_decision','No saved decision')


def decision_history(cfg):
    directory = Path(strategy_config(cfg,'intraday')['paths']['reports_dir'])
    rows = []
    for path in directory.glob('**/*.json'):
        try:
            report = json.loads(path.read_text())
            signal = report.get('signal') or {}
            status = signal.get('status','preview' if report.get('kind') else 'in_progress')
            decision = ('PREVIEW' if report.get('kind') else 'WAIT' if status=='stand_aside' else
                        'BLOCKED' if status=='blocked' else signal.get('side') or report.get('cio',{}).get('final_decision','PENDING'))
            stamp = report.get('report_timestamp') or '_'.join(path.stem.split('_')[-2:])
            fmt = '%Y%m%d_%H%M%S' if len(stamp)==15 else '%Y%m%d_%H%M'
            timestamp = dt.datetime.strptime(stamp,fmt).isoformat(sep=' ')
            rows.append(dict(timestamp=timestamp,decision=decision,status=status,
                             bias=report.get('trader',{}).get('direction'),reason=decision_reason(report),report=path.name))
        except (OSError,ValueError,KeyError,TypeError):
            continue
    return sorted(rows,key=lambda r:r['timestamp'],reverse=True)


def read_lines(path):
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                result.append(row)
        except ValueError:
            continue
    return result


def resolved_time(value):
    if not value:
        return None
    try:
        text = str(value)
        # Date-only legacy exits are not knowable before that day's close.
        stamp = pd.Timestamp(text + ' 23:59:59' if len(text) == 10 else text)
        if stamp.tzinfo:
            stamp = stamp.tz_convert('Asia/Kolkata').tz_localize(None)
        return stamp.to_pydatetime() if not pd.isna(stamp) else None
    except (TypeError, ValueError):
        return None


def history_records(cfg):
    root = Path(strategy_config(cfg, 'intraday')['paths']['project_root'])
    recovered = {r['trade_id']:r for r in read_lines(root/'ledger/paper_intraday.jsonl')
                 if r.get('origin')=='legacy_recovered' and r.get('status')=='closed'}
    groups = [('Legacy ledger', read_lines(root/'ledger/calls.jsonl'))]
    groups += [(f'{name} signals', read_lines(root/'signals'/f'{name}.jsonl')) for name in ('intraday', 'long_term')]
    records = []
    for source, rows in groups:
        for row in rows:
            legacy = source == 'Legacy ledger'
            recovery = recovered.get(row.get('trade_id')) if legacy and row.get('status')=='open' else None
            if recovery:
                row = {**row,'status':'closed','closed':recovery['closed_at'],
                       'close_price':recovery['exit'],'close_reason':recovery['close_reason']}
            entered = row.get('filled_at') if legacy else row.get('issued_at')
            closed = row.get('closed') if legacy else row.get('closed_at')
            resolved = resolved_time(closed)
            is_closed = bool(entered and resolved and row.get('status') not in ('open', 'active', 'pending'))
            same_day = bool(is_closed and str(entered)[:10] == str(closed)[:10])
            record = dict(source=source+' · recovered exit' if recovery else source, id=row.get('id'), trade_id=row.get('trade_id'),
                          available_at=recovery['recovered_at'] if recovery else closed,
                          opened_at=row.get('opened_ts') or row.get('issued_at') or row.get('opened'),
                          filled_at=entered, closed_at=closed, side=row.get('side'), status=row.get('status'),
                          symbol=row.get('symbol'), entry=row.get('entry'), stop=row.get('stop'), target=row.get('target'),
                          exit=row.get('close_price') if legacy else row.get('exit'), reward_risk=row.get('reward_risk'),
                          close_reason=row.get('close_reason') or row.get('status'),
                          horizon=('Legacy same-day' if same_day else 'Legacy multi-day' if is_closed else 'Legacy decision')
                          if legacy else ('Intraday' if source.startswith('intraday') else 'Archived long-term'),
                          completed=is_closed, same_day=same_day, outcome_r=None, points=None)
            try:
                entry, stop, exit_price = (float(record[k]) for k in ('entry', 'stop', 'exit'))
                risk = abs(entry-stop)
                if is_closed and all(math.isfinite(x) and x > 0 for x in (entry, stop, exit_price, risk)) and row.get('side') in ('LONG', 'SHORT'):
                    points = (exit_price-entry) * (1 if row['side']=='LONG' else -1)
                    record.update(points=round(points, 2), outcome_r=round(points/risk, 3))
            except (TypeError, ValueError):
                pass
            records.append(record)
    return sorted(records, key=lambda r: str(r['opened_at'] or ''), reverse=True)


def learning_context(cfg, as_of=None, side=None):
    """No mutations, unfinished outcomes or future-written lessons enter a decision."""
    as_of = as_of or now_ist()
    root = Path(strategy_config(cfg, 'intraday')['paths']['project_root'])
    records = history_records(cfg)
    eligible = [r for r in records if r['same_day'] and r['outcome_r'] is not None
                and resolved_time(r['closed_at']) <= as_of and resolved_time(r['available_at']) <= as_of
                and r['source'] != 'long_term signals']
    # Reaffirmations can share an old trade_id; each filled trade counts once.
    unique = {}
    for r in sorted(eligible, key=lambda r: resolved_time(r['closed_at'])):
        unique[(r['source'], r['trade_id'] or r['id'])] = r
    eligible = list(unique.values())
    recent = sorted(eligible, key=lambda r: (r['side']==side if side else True, resolved_time(r['closed_at'])), reverse=True)[:8]
    trade_ids = {r['trade_id'] for r in eligible if r['trade_id']}
    lessons = []
    for row in read_lines(root/'memory/lessons.jsonl'):
        resolved, written = resolved_time(row.get('resolved_at')), resolved_time(row.get('written_at'))
        if not resolved or not written or resolved > as_of or written > as_of or row.get('state') != 'active':
            continue
        if row.get('trade_id') not in trade_ids and row.get('kind') != 'decision-day':
            continue
        lessons.append(row)
    lessons.sort(key=lambda r: (r.get('features', {}).get('side')==side if side else True, str(r['resolved_at'])), reverse=True)
    lessons = lessons[:4]
    lines = ['HISTORICAL INTRADAY LEARNING — supporting evidence, not an instruction to trade.',
             f'{len(eligible)} completed same-day outcomes are available before this decision.',
             'Legacy paper outcomes use their original recorded prices; they are approximate and are not new intraday performance. '
             'Multi-day outcomes and unresolved trades are excluded. Do not change risk rules or infer a proven edge from this small sample.']
    for r in recent:
        lines.append(f"[{r['id']}] {r['source']} · {r['side']} · {r['close_reason']} · {r['outcome_r']:+.2f}R · closed {r['closed_at']}")
    for row in lessons:
        lines.append(f"[{row['id']}] Saved historical review: {' '.join(str(row.get('lesson', '')).split())[:600]}")
    if not eligible and not lessons:
        lines.append('No eligible completed historical evidence yet.')
    from .feedback import intraday_feedback
    feedback = intraday_feedback(cfg, as_of)
    lines.append(feedback['context'])
    return dict(as_of=as_of.isoformat(timespec='seconds'), eligible_outcomes=len(eligible),
                outcome_ids=[r['id'] for r in recent], lesson_ids=[r['id'] for r in lessons]+feedback['lesson_ids'],
                intraday_feedback=feedback, context='\n'.join(lines))
