"""Intraday signals with visible run progress and preserved historical learning."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tradingagents_mcx.config import load_config, CONFIG_PATH
from tradingagents_mcx.signals import SignalBook, latest_quote
from tradingagents_mcx.setups import PendingSetups
from tradingagents_mcx.paper import PaperBook, paper_settings
from tradingagents_mcx.usage import usage_summary
from tradingagents_mcx.strategies import NAMES, now_ist, profiles, strategy_config, entry_allowed
from tradingagents_mcx.runs import active_run, read_state, launch_council
from tradingagents_mcx.history import history_records, learning_context, read_lines, decision_history, decision_reason
from tradingagents_mcx.dashboard_ui import (
    theme, header, metric, banner, services, trade_card, styled_table,
    badge, render, money, pnl_tone, status_tone, metric_strip, recent_trade, display_time, safe,
)

ROOT = Path(__file__).resolve().parent
LABELS = {'intraday': 'Intraday', 'long_term': 'Long term'}
COLORS = {'intraday': '#00b8a9', 'long_term': '#8199ff'}


def kite_login_panel(cfg):
    from tradingagents_mcx.kite_data import KiteData
    root = Path(strategy_config(cfg, 'intraday')['paths']['project_root'])
    status = KiteData(root).session_status(now_ist())
    state = status['state']
    labels = dict(active=('ACTIVE', 'positive'), expiring_soon=('EXPIRING SOON', 'warning'),
                  expired=('EXPIRED', 'negative'), login_required=('LOGIN NEEDED', 'warning'),
                  not_configured=('NOT CONFIGURED', 'neutral'))
    label, tone = labels[state]
    if status['expires_at']:
        expiry = pd.Timestamp(status['expires_at']).strftime('%d %b %Y at %H:%M IST')
        if state == 'expired':
            detail = f'Expired {expiry}. Click Log in to Kite to start a new session.'
        else:
            minutes = max(1, (status['seconds_remaining'] + 59) // 60)
            remaining = f'{minutes // 60}h {minutes % 60}m' if minutes >= 60 else f'{minutes}m'
            detail = f'Expires {expiry} · {remaining} remaining.'
    else:
        detail = ('Click Log in to Kite to connect live market data.' if status['configured'] else
                  'Kite credentials need to be configured before signing in.')
    if state in ('active', 'expiring_soon'):
        banner(f'KITE LOGIN · {label}', detail, tone)
        return
    summary, action = st.columns([4, 1], vertical_alignment='center')
    with summary: banner(f'KITE LOGIN · {label}', detail, tone)
    with action:
        st.link_button('Log in to Kite', 'http://127.0.0.1:8000/login', key='kite_login_top',
                       type='primary', width='stretch', disabled=not status['configured'],
                       help='Opens Zerodha in a new tab. Finish signing in, then return to this dashboard.')
        st.caption('Status updates automatically after login.')


def desk_status(cfg, scfg, quote, compact=False):
    now = now_ist()
    usable, reason = latest_quote(scfg, now)
    root = Path(scfg['paths']['project_root'])
    guard = read_json(root / 'events/position_guard.json', {})
    try:
        age = (now - dt.datetime.fromisoformat(guard['ts'])).total_seconds()
        guarded = 0 <= age < 45
    except (KeyError, TypeError, ValueError):
        guarded = False
    busy = active_run(cfg)
    allowed = entry_allowed(scfg['strategy'], now)
    market_open = (now.weekday() < 5 and now.date().isoformat() not in scfg['strategy'].get('holidays', [])
                   and dt.time.fromisoformat(scfg['strategy']['session_open']) <= now.time()
                   <= dt.time.fromisoformat(scfg['strategy']['session_close']))
    paper_on = paper_settings(cfg)['enabled'] and not (root/'events/KILL').exists()
    if busy:
        council_state, council_note, council_tone = 'ANALYZING', busy.get('message', 'Council running'), 'info'
    else:
        state = read_state(cfg)
        failed = state.get('status') in ('failed', 'interrupted')
        council_state = 'LAST RUN FAILED' if failed else 'READY' if allowed else 'ENTRIES CLOSED'
        council_note = state.get('message', 'Waiting for the next scheduled review') if failed else 'Automatic council + manual run available' if allowed else f"New entries close at {scfg['strategy']['entry_cutoff']} IST"
        council_tone = 'negative' if failed else 'info' if allowed else 'neutral'
    services([
        ('Feed', 'MARKET CLOSED' if not market_open else 'FRESH' if usable else 'AWAITING QUOTE',
         f"{quote.get('source', 'Saved feed')} · Trade {quote.get('ltt', '—')}" if usable or not market_open else reason,
         'neutral' if not market_open else 'positive' if usable else 'warning'),
        ('Exit monitor', 'RUNNING' if guarded else 'NOT REPORTING',
         f"Checked {guard['ts'][11:19]} IST · every 10s" if guarded else 'Check Health for the last heartbeat', 'positive' if guarded else 'negative'),
        ('Auto-entry', 'ON' if paper_on else 'PAUSED',
         'Approved signals enter automatically' if paper_on else 'New simulated entries are paused', 'active' if paper_on else 'warning'),
        ('AI council', council_state, council_note, council_tone),
    ], compact=compact)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def reports(cfg, name='intraday', previews=False):
    directory = Path(strategy_config(cfg, name)['paths']['reports_dir'])
    return sorted(directory.glob('**/*.json' if previews else '*.json'), key=lambda p: p.stat().st_mtime, reverse=True)


def report_stamp(report, path):
    stamp = report.get('report_timestamp') or '_'.join(path.stem.split('_')[-2:])
    try:
        return dt.datetime.strptime(stamp, '%Y%m%d_%H%M%S' if len(stamp)==15 else '%Y%m%d_%H%M').strftime('%d %b %H:%M:%S IST')
    except ValueError:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).strftime('%d %b %H:%M:%S IST')


def run_feedback(cfg):
    busy = active_run(cfg)
    if busy:
        elapsed = max(0, int((now_ist()-dt.datetime.fromisoformat(busy['started_at'])).total_seconds()))
        mode = 'Analysis preview' if busy.get('watch') else 'Intraday council'
        st.info(f"{mode} running · {elapsed//60}m {elapsed%60}s elapsed · {busy.get('message', 'Starting')}")
        stage = busy.get('stage', 0)
        st.progress(max(0,stage-1)/4, text=f'Stage {stage}/4 · Data → Analysts → Debate → Decision')
        with st.expander('Live council activity', expanded=True):
            for event in busy.get('events', [])[-5:]:
                st.caption(f"{event['ts'][11:19]} · {event['message']}")
            st.caption('This view refreshes every 15 seconds. Model responses can take several minutes.')
    else:
        state = read_state(cfg)
        if state.get('status') in ('failed', 'interrupted'):
            st.error(f"Last run {state['status']}: {state['message']}")
        elif state.get('finished_at'):
            st.caption(f"Last run finished {state['finished_at'].replace('T', ' ')} IST · {state.get('message', '')}")
    from scheduler import jobs_for_day
    now = now_ist()
    upcoming = next((job for day in range(8) for job in jobs_for_day(now.date()+dt.timedelta(days=day),cfg)
                     if not job['watch'] and job['when']>now), None)
    if upcoming:
        st.caption(f"Next scheduled intraday council: {upcoming['when']:%d %b %H:%M} IST · valid signals are monitored every 10 seconds")
    else:
        st.caption('Automatic intraday council is disabled in settings.')
    scan=read_json(ROOT/'events/opportunity_scanner.json',{})
    if scan:
        stamp=pd.Timestamp(scan.get('checked_at'))
        fresh=not pd.isna(stamp) and 0<=(now-stamp.to_pydatetime()).total_seconds()<60
        settings_=scan.get('settings',{})
        tone='active' if fresh and scan.get('status') in ('watching','launched','launching') else 'warning'
        title='OPPORTUNITY SCANNER' if fresh else 'OPPORTUNITY SCANNER · NOT REPORTING'
        banner(title,scan.get('reason','Waiting for a scan'),tone)
        st.caption(f"Checks every 15 seconds · extra council reviews today {scan.get('launches_today',0)} / "
                   f"{settings_.get('max_extra_reviews_per_day',6)} · full council and risk checks still required")


def settings(cfg):
    p = profiles(cfg)
    from tradingagents_mcx.opportunities import scanner_settings
    with st.sidebar:
        st.title('Signal settings')
        st.caption('Intraday only · prices in INR per barrel · exit within the same session.')
        with st.form('signal_settings'):
            intra_on = st.checkbox('Intraday enabled', value=p['intraday']['enabled'])
            intra_rr = st.number_input('Intraday minimum reward / risk', min_value=0.1, value=float(p['intraday']['min_reward_risk']), step=0.1)
            cutoff = st.time_input('Intraday entry cutoff (IST)', value=dt.time.fromisoformat(p['intraday']['entry_cutoff']))
            square = st.time_input('Intraday square-off (IST)', value=dt.time.fromisoformat(p['intraday']['square_off']))
            paper_on = st.checkbox('Automatic trade entries',value=paper_settings(cfg)['enabled'],
                                   help='Automatically simulate valid council signals using virtual money, without manual approval. Existing trades keep their stop and target when new entries are paused.')
            scan_on = st.checkbox('Scan opportunities between councils',value=scanner_settings(cfg)['enabled'],
                                  help='Watch completed candles every 15 seconds and request a full review for ready setups, with a daily extra-review budget.')
            st.caption('Every new trade requires AI council approval. Rejected plans are tracked separately under What if?')
            if st.form_submit_button('Save signal settings', width='stretch'):
                raw = read_json(CONFIG_PATH, {})
                raw.setdefault('strategies', {})
                raw['strategies'].setdefault('intraday', {}).update(enabled=intra_on, min_reward_risk=intra_rr,
                    entry_cutoff=cutoff.strftime('%H:%M'), square_off=square.strftime('%H:%M'))
                raw['strategies'].setdefault('long_term', {})['enabled'] = False
                raw.setdefault('paper',{})['enabled'] = paper_on
                raw.setdefault('opportunities',{})['enabled'] = scan_on
                raw.setdefault('mock_experiment',{})['enabled'] = False
                try:
                    profiles(raw)
                except ValueError as e:
                    st.error(str(e))
                else:
                    tmp = CONFIG_PATH.with_suffix('.tmp'); tmp.write_text(json.dumps(raw, indent=2)); tmp.replace(CONFIG_PATH)
                    st.success('Saved. New signals use these rules; active signals retain their recorded deadlines.')
        st.caption('1R is the entry-to-stop distance. Outcomes use observed prices and exclude fees and slippage.')
        st.caption('Simulation uses a virtual wallet and estimated transaction costs.')


def trade_date(value):
    try:
        stamp=pd.Timestamp(value)
        if pd.isna(stamp):return None
        if stamp.tzinfo:stamp=stamp.tz_convert('Asia/Kolkata')
        return stamp.date()
    except (ValueError,TypeError):
        return None


def live_trades(cfg):
    book=PaperBook(cfg);rows=book.all_trades(include_experimental=True)
    opened=[r for r in rows if r['status']=='open']
    closed=sorted((r for r in rows if r['status']=='closed'),key=lambda r:r.get('closed_at') or '',reverse=True)
    today=[r for r in closed if trade_date(r.get('closed_at'))==now_ist().date()]
    today_pnl=sum(float(r.get('pnl_inr') or 0) for r in today)
    open_pnl=sum(float(r.get('unrealized_inr') or 0) for r in opened)
    st.subheader('Live trades')
    metric_strip([
        ('Open now',str(len(opened)),'active' if opened else 'neutral','Currently in a trade' if opened else 'No position open'),
        ('Open P&L',money(open_pnl,True),pnl_tone(open_pnl),'After estimated trade costs'),
        ('Booked today',money(today_pnl,True),pnl_tone(today_pnl),'Closed today · after costs'),
        ('Closed today',str(len(today)),'info',f'{len(closed)} completed · all simulation tracks'),
    ])
    scfg=strategy_config(cfg,'intraday')
    from tradingagents_mcx.opportunities import scanner_settings
    cadence=int(round(scfg['strategy']['interval_hours']*60))
    st.caption(f"AI council required for every entry · reviews every {cadence} minutes · "
               f"up to {scanner_settings(cfg)['max_extra_reviews_per_day']} extra setup reviews/day · "
               f"up to {book.policy['max_new_trades_per_day']} new trades/day, subject to risk limits")
    st.caption('Rejected or cancelled a setup? Open What if? to see how its original plan would have performed.')
    paused=not book.policy['enabled'] or (Path(scfg['paths']['project_root'])/'events/KILL').exists()
    if paused:st.warning('Automatic entries are paused. Existing trades keep their stop, target and exit deadline.')
    if any(r.get('origin')=='experimental' for r in rows):
        banner('EXPERIMENTAL HISTORY',
               'New entries require council approval. Historical experimental records remain visible and existing exits stay monitored.','info')
    for trade in opened:
        trade_card(trade)
        if trade.get('exit_pending'):st.error(trade['exit_pending'])
        try:
            stamp=pd.Timestamp(trade.get('last_observed_at'))
            if stamp.tzinfo:stamp=stamp.tz_convert('Asia/Kolkata').tz_localize(None)
            age=(now_ist()-stamp.to_pydatetime()).total_seconds()
        except (ValueError,TypeError):
            age=float('nan')
        if not 0<=age<=scfg['strategy']['max_quote_age_seconds']:
            st.warning('This trade’s price is awaiting an update. P&L uses the last recorded price shown above.')
    if not opened:
        signal=next((r for r in reversed(SignalBook(scfg).load()) if r['status']=='active'),None)
        pending=next((r for r in reversed(PendingSetups(scfg).load()) if r['status']=='pending'),None)
        reason=None
        if paused:
            banner('No open trades · entries paused','Enable automatic trade entries in the sidebar to resume new entries. Check Health for any stop switch.','warning')
        elif signal:
            skipped=next((r for r in rows if r.get('signal_id')==signal['id'] and r['status']=='skipped'),None)
            if skipped:banner('No open trades · entry skipped',skipped['reason'],'warning')
            else:banner('Signal ready · entry pending','The entry monitor checks every 10 seconds. The trade appears here once recorded.','info')
        elif pending:
            banner(f"No open trades · watching a {pending['side'].lower()} setup",'Waiting for price confirmation. A valid setup enters automatically; no manual approval is needed.','warning')
            reason=pending['reason']
        else:
            files=reports(cfg,'intraday')
            latest=read_json(files[0],{}) if files else {}
            banner('No open trades right now',
                   'The council is waiting for a valid entry. Every new trade will appear here automatically.','info')
            if latest:reason=decision_reason(latest)
        if reason:
            st.write('**Why no entry:** ' + reason)
            st.caption('See Signals for the complete decision and setup conditions.')
    if cfg.get('entry_models',{}).get('regime_enabled'):
        scan=read_json(Path(scfg['paths']['project_root'])/'events/opportunity_scanner.json',{})
        pool=scan.get('entry_models',{})
        regime=pool.get('regime',{}).get('regime','awaiting scan').replace('_',' ').upper()
        banner('STRATEGY SCAN · '+regime,
               'Trend continuation and range rejection are enabled for council review. Accepted signals enter automatically.', 'info')
        if pool:
            st.caption('Last candidate evaluation '+str(pool['as_of'])[:19]+' IST · '+scan.get('reason',''))
            st.dataframe(pd.DataFrame([dict(Strategy='Trend / range' if name=='regime' else 'Continuation',
                Status='Ready for review' if c['status']=='ready' else 'Waiting',
                Detail=(f"{c['side']} · entry {c['entry']:,.0f} · stop {c['stop']:,.0f} · target {c['target']:,.0f}"
                        if c['status']=='ready' else c.get('reason','')))
                for name,c in pool.get('candidates',{}).items()]),hide_index=True,width='stretch')
        st.caption('Trend/range is experimental; historical testing has not established a profit edge.')
    st.caption('Stops and targets checked every 10s · prices and P&L refresh every 15s · all times IST')
    if closed:
        st.subheader('Recently closed')
        for col,trade in zip(st.columns(min(3,len(closed))),closed[:3]):
            with col:recent_trade(trade)
        st.caption('Open Trade history for every trade, including older records, filters and downloads.')
    with st.expander('What is the system doing?',expanded=not opened):
        run_feedback(cfg)


def paper_trades(cfg):
    book=PaperBook(cfg);a=book.account();p=book.policy
    st.subheader('Trade history')
    st.caption('Every trade in one place · newest first · all older records included')
    rows=book.all_trades(include_experimental=True)
    track=st.radio('Trade track',['All tracks','Council + older','Experimental'],horizontal=True)
    if track!='All tracks':rows=[r for r in rows if (r.get('origin')=='experimental')==(track=='Experimental')]
    completed=[r for r in rows if r['status']=='closed'];wins=sum(float(r.get('pnl_inr') or 0)>0 for r in completed)
    booked=sum(float(r.get('pnl_inr') or 0) for r in completed)
    metric_strip([
        ('Completed trades',str(len(completed)),'info','All time · '+track),
        ('Booked P&L',money(booked,True),pnl_tone(booked),'Selected tracks · after recorded costs'),
        ('Win rate',f"{100*wins/len(completed):.1f}%" if completed else '—','info',f"{wins} wins / {len(completed)} completed"),
        ('Trading fees',money(sum(float(r.get('fees_inr') or 0) for r in completed)),'warning','Selected tracks · API cost is in Health'),
    ])
    with st.expander('Simulation settings and accounting'):
        st.write('Experimental trades use one fixed simulated lot, independent of the council wallet. They enter from numerical rules without council approval, and include the fees and adverse slippage saved with each trade. Use the track filter to compare their results separately.')
        st.write(f"Original virtual capital ₹{a['initial_capital_inr']:,.0f}; saved historical results remain in the balance. "
                 f"Risk budget {p['risk_per_trade_pct']:g}% per trade; up to {p['max_lots']} lots of {p['lot_size_bbl']} barrels. "
                 f"Assumed margin ₹{p['margin_per_lot_inr']:,.0f} and round-trip cost ₹{p['roundtrip_cost_per_lot_inr']:g} per lot.")
        st.write('Council trades use the observed signal price with no slippage. Mini-contract sizing uses the main CRUDEOIL price feed as a proxy. These are simulation assumptions, not broker fills or current broker margin quotes.')
        st.write('Booked P&L is net of recorded costs. Open P&L deducts estimated round-trip costs. Model/API spending is shown separately in Health.')
        st.write('Older records remain unchanged. Any recovered old exit is labelled and stored separately with its observed price and recovery time.')
        st.caption(f"Virtual balance {money(a['balance_inr'])} · Equity {money(a['equity_inr'])} · Available funds {money(a['available_inr'])}")
    if not rows:
        st.info('No trades recorded for this track yet. Automatic entries appear in Live trades.')
        return
    from tradingagents_mcx.economics import recorded_economics
    df=pd.DataFrame([{**r,'track':'Experimental' if r.get('origin')=='experimental' else 'Council / older',
                     'planned_net_reward_risk':(recorded_economics(r) or {}).get('net_reward_risk')} for r in rows])
    c1,c2,c3=st.columns([1,1.3,2])
    period=c1.selectbox('Period',['All time','Today','Last 7 days'])
    statuses=c2.multiselect('Status',list(df.status.unique()),default=list(df.status.unique()),format_func=str.title)
    query=c3.text_input('Find a trade',placeholder='Date, direction, trade ID or signal ID')
    visible=df[df.status.isin(statuses)]
    if period!='All time':
        start=now_ist().date()-dt.timedelta(days=6 if period=='Last 7 days' else 0)
        dates=visible.apply(lambda r:trade_date(r.get('closed_at') if r['status']=='closed' else r.get('opened_at')),axis=1)
        visible=visible[dates.map(lambda d:d is not None and start<=d<=now_ist().date())]
    if query:visible=visible[visible.astype(str).agg(' '.join,axis=1).str.contains(query,case=False,regex=False)]
    all_columns=st.checkbox('Show all trade columns',value=False)
    columns=['opened_at','track','side','status','pnl_inr','entry','exit','stop','target','close_reason','lots']
    if all_columns:columns+=['unrealized_inr','last_price','reward_risk','planned_net_reward_risk','closed_at','fees_inr',
                             'trade_id','signal_id','origin','lot_size_bbl','reason']
    table=visible.reindex(columns=columns).copy()
    table['status']=table['status'].astype('string').str.title()
    table['close_reason']=table['close_reason'].astype('string').str.replace('_',' ',regex=False).str.capitalize()
    st.dataframe(styled_table(table),hide_index=True,width='stretch',column_config={
        'trade_id':st.column_config.TextColumn('Trade'), 'side':'Direction', 'status':'Status',
        'unrealized_inr':st.column_config.NumberColumn('Open P&L ₹',format='%.0f'),
        'pnl_inr':st.column_config.NumberColumn('Booked P&L ₹',format='%.0f'),
        'entry':st.column_config.NumberColumn('Entry',format='%.0f'),
        'exit':st.column_config.NumberColumn('Exit',format='%.0f'),
        'last_price':st.column_config.NumberColumn('Last price',format='%.0f'),
        'stop':st.column_config.NumberColumn('Stop-loss',format='%.0f'),
        'target':st.column_config.NumberColumn('Target',format='%.0f'),
        'reward_risk':st.column_config.NumberColumn('R/R before fees',format='%.2f'),
        'planned_net_reward_risk':st.column_config.NumberColumn('R/R after fees',format='%.2f'), 'lots':'Lots',
        'opened_at':'Opened (IST)', 'closed_at':'Closed (IST)', 'close_reason':'Exit reason',
        'fees_inr':'Fees ₹', 'signal_id':'Signal', 'origin':'Source', 'lot_size_bbl':'Barrels / lot', 'reason':'Entry notes',
    })
    st.caption(f'Showing {len(visible)} of {len(df)} trade records, including the older ledger. Date filters use the exit date for closed trades and entry date otherwise.')
    if visible.empty:st.info('No trades match these filters. Choose All time or clear the search to see older trades.')
    st.download_button('Download these trade records',df.to_csv(index=False),'trades.csv','text/csv')
    if not visible.empty:
        with st.expander('Trade details · entry, exit and original record'):
            selected=st.selectbox('Select a trade',visible.to_dict('records'),
                format_func=lambda r:f"{display_time(r['opened_at'])} · {r['side']} · {r['status']} · {r['trade_id']}")
            if selected.get('recovery_note'):st.write(selected['recovery_note'])
            st.json(next(r for r in rows if r['id']==selected['id']))
    closed=sorted((r for r in rows if r['status']=='closed' and r.get('closed_at')),key=lambda r:r['closed_at'])
    if closed:
        curve=pd.DataFrame(dict(closed_at=[r['closed_at'] for r in closed],
             booked_pnl=pd.Series([r.get('pnl_inr') or 0 for r in closed]).cumsum()))
        st.line_chart(curve.set_index('closed_at'),y='booked_pnl',y_label='Cumulative booked P&L (INR)')


def indicator_tape(cfg):
    st.subheader('Intraday indicator tape')
    st.caption('The earlier fast BUY/SELL indicator is still recorded. Council trades follow approved signals; Experimental trades use completed-candle rules.')
    symbol=cfg['analysis'].get('symbol','CRUDEOIL')
    files=sorted((Path(cfg['paths']['cache_dir'])/'live').glob(f'signals_{symbol}_*.jsonl'),reverse=True)
    if not files:
        st.info('No saved indicator tape yet.');return
    path=st.selectbox('Indicator session',files,format_func=lambda p:p.stem.rsplit('_',1)[-1])
    rows=read_lines(path)
    if not rows:return
    last=rows[-1];c1,c2,c3=st.columns(3)
    with c1:metric('Latest indicator',last.get('label','—'),status_tone(last.get('label')))
    with c2:metric('Indicator score',f"{last.get('composite',0):+.1f}",pnl_tone(last.get('composite')))
    with c3:metric('Observed price',f"{last.get('ltp',0):,.0f}",'info')
    st.caption(f"Recorded {last.get('ts')} IST · {last.get('confluence','')}")
    for component,note in last.get('notes',{}).items():st.caption(f'{component}: {note}')
    frame=pd.DataFrame(rows)
    st.line_chart(frame.set_index('ts'),y='composite',y_label='Indicator score')
    st.dataframe(styled_table(frame.reindex(columns=['ts','label','composite','ltp','confluence'])),hide_index=True,width='stretch')
    st.download_button('Download original indicator tape',path.read_bytes(),path.name,'application/x-ndjson')


def signal_card(cfg, name):
    scfg = strategy_config(cfg, name)
    book = SignalBook(scfg)
    active = next((r for r in reversed(book.load()) if r['status'] == 'active'), None)
    p = scfg['strategy']
    st.subheader(LABELS[name])
    st.caption((f"Same session · entries until {p['entry_cutoff']} · exit {p['square_off']} IST" if name == 'intraday'
                else f"Multi-session · up to {p['max_hold_days']} days · expires with contract"))
    if active:
        banner(f"ACTIVE SIGNAL · {active['side']}",f"{active['confidence']} confidence · expires {active['valid_until']} IST",status_tone(active['side']))
        cols = st.columns(3)
        for col, key, tone in zip(cols, ('entry', 'stop', 'target'), ('info','negative','positive')):
            with col:metric(key.title(), money(active[key]),tone)
        a, b, c = st.columns(3)
        a.metric('Risk : reward', f"1 : {active['reward_risk']:.2f}")
        b.metric('Risk / reward points', f"{active['risk_points']:g} / {active['reward_points']:g}")
        with c:metric('Current move', f"{active.get('current_r', 0):+.2f}R",pnl_tone(active.get('current_r',0)))
        st.caption(f"Issued {active['issued_at']} · {active['confidence']} confidence · valid until {active['valid_until']} IST")
        st.caption(f"Contract expiry {active['contract_expiry']} · observed entry, not a broker fill")
        if active.get('entry_method')=='trend_continuation_v1':
            st.caption('Trend-continuation entry · target is a volatility projection from ATR, not an observed resistance level.')
        if active.get('exit_pending'):
            st.error(active['exit_pending'])
    else:
        banner('NO ACTIVE SIGNAL','Waiting for a valid setup. The council decision and any conditions are shown below.','warning')
    setups = PendingSetups(scfg).load()
    pending = next((s for s in reversed(setups) if s['status']=='pending'),None)
    if pending:
        plan = pending['plan']
        banner(f"WATCHING {pending['side']} SETUP",pending['reason'],'warning')
        a,b,c = st.columns(3)
        a.metric('Conditional entry range',f"{plan['entry_min']:,.0f}–{plan['entry_max']:,.0f}")
        b.metric('Planned stop',f"{plan['stop']:,.0f}")
        c.metric('Planned target',f"{plan['target']:,.0f}")
        st.caption(f"Checks every 10 seconds · expires {pending['valid_until']} IST · activates automatically when the market conditions below are met; no manual approval needed")
        with st.expander('Setup conditions and progress'):
            if plan['trigger']=='pullback_reclaim':
                st.write(f"Wait for price to test within {plan['retest_tolerance']:g} points of {plan['level']:,.0f}, then close back on the signal side.")
            st.write(f"A completed five-minute candle must close {'above' if pending['side']=='LONG' else 'below'} {plan['level']:,.0f}.")
            if plan['trigger']=='breakout_retest':
                st.write(f"A later completed candle must retest within {plan['retest_tolerance']:g} points of that level and close on the breakout side.")
            if plan['momentum']=='macd_improving':
                st.write('Completed-bar MACD momentum must also improve in the signal direction.')
            st.write('The current price must still be inside the entry range and meet the minimum risk–reward.')
            for event in pending['events']:
                st.caption(f"{event['at']} · {event['reason']}")
    elif setups:
        recent = setups[-1]
        st.caption(f"Last conditional setup: {recent['status']} · {recent['reason']}")
    files = reports(cfg, name)
    last = read_json(files[0], {}) if files else {}
    if last:
        outcome = last.get('signal', {})
        banner(f"LATEST COUNCIL · {last['cio']['final_decision']}",decision_reason(last),status_tone(outcome.get('status') or last['cio']['final_decision']))
        st.caption(f"Saved {report_stamp(last,files[0])} · {outcome.get('status', 'analysis only')} · check Live trades for current positions")
        tally = last.get('trader', {}).get('module_tally', {})
        if tally:
            render('<div class="desk-legend">'+badge(f"{tally.get('BULLISH',0)} BULLISH",'positive')+' '+badge(f"{tally.get('BEARISH',0)} BEARISH",'negative')+' '+badge(f"{tally.get('NEUTRAL',0)} NEUTRAL",'neutral')+'</div>')
        with st.expander('Why this decision?'):
            st.text(last.get('cio', {}).get('statement', 'No explanation saved.'))
    all_reports = reports(cfg, name, previews=True)
    if all_reports and all_reports[0].parent.name == 'night':
        preview = read_json(all_reports[0], {})
        st.caption(f"Latest analysis preview: {preview.get('cio',{}).get('final_decision','Unavailable')} · {report_stamp(preview,all_reports[0])} · preview does not issue signals")
    busy = bool(active_run(cfg))
    c1, c2 = st.columns(2)
    run = c1.button('Run intraday council', key=f'run_{name}', width='stretch', disabled=busy, type='primary',
                    help='Analyze current data and issue a signal only when the setup passes validation.')
    preview_run = c2.button('Preview analysis', key=f'watch_{name}', width='stretch', disabled=busy,
                           help='Research only. Saves a report without issuing a signal.')
    if run or preview_run:
        started, message = launch_council(cfg, watch=preview_run)
        if started:
            st.toast(message)
            st.rerun()
        else:
            st.warning(message)
    stats = book.stats()
    s1, s2, s3 = st.columns(3)
    s1.metric('New intraday outcomes', stats['closed'])
    s2.metric('Win rate', f"{stats['win_rate']:.1f}%" if stats['win_rate'] is not None else '—')
    s3.metric('Average outcome', f"{stats['mean_r']:+.2f}R" if stats['mean_r'] is not None else '—')
    st.caption('Earlier trades and decisions are visible in History & learning; these metrics measure the new intraday signals.')
    return active


def session_chart(cfg, active):
    symbol = cfg['analysis'].get('symbol', 'CRUDEOIL')
    path = Path(cfg['paths']['cache_dir']) / 'live' / f'{symbol}_{now_ist():%Y%m%d}.csv'
    try:
        ticks = pd.read_csv(path, parse_dates=['ts']).sort_values('ts')
    except (OSError, ValueError, pd.errors.EmptyDataError):
        st.info('The collector has no ticks for this session yet.')
        return
    if ticks.empty:
        return
    bars = ticks.set_index('ts').ltp.resample('5min').ohlc().dropna()
    fig = go.Figure(go.Candlestick(x=bars.index, open=bars.open, high=bars.high, low=bars.low, close=bars.close, name='MCX 5m',
                                 increasing_line_color='#15945d', decreasing_line_color='#d33a58'))
    for name, signal in active.items():
        if signal:
            for level, dash in [('entry', 'solid'), ('stop', 'dash'), ('target', 'dot')]:
                color = {'entry':'#245bcc','stop':'#bb2445','target':'#087443'}[level]
                fig.add_hline(y=signal[level], line_color=color, line_dash=dash, line_width=2,
                              annotation_text=f'{level.upper()} {signal[level]:,.0f}', annotation_position='top left',
                              annotation_font_color=color, annotation_bgcolor='white')
            if signal.get('last_price') is not None:
                fig.add_trace(go.Scatter(x=[bars.index[-1]],y=[signal['last_price']],mode='markers',
                                        marker=dict(color='#142742',size=10,line=dict(color='white',width=2)),name='Last observed price'))
    fig.update_layout(height=470, margin=dict(l=0, r=0, t=20, b=0), xaxis_rangeslider_visible=False,
                      yaxis_title='INR / barrel',paper_bgcolor='white',plot_bgcolor='white',font_color='#31435c')
    fig.update_xaxes(gridcolor='#eef1f6');fig.update_yaxes(gridcolor='#eef1f6')
    st.plotly_chart(fig, width='stretch')
    render('<div class="desk-legend">'+badge('ENTRY','info')+' '+badge('STOP-LOSS','negative')+' '+badge('TAKE-PROFIT','positive')+'</div>')
    st.caption('Five-minute candles from sampled MCX prints, with the active intraday levels.')


def history(cfg):
    decisions = decision_history(cfg)
    st.subheader('Council decision history')
    st.caption('Every completed review appears here, including WAIT and rejected setups. BUY/SELL signal outcomes appear below.')
    if decisions:
        frame = pd.DataFrame(decisions)
        st.dataframe(styled_table(frame),hide_index=True,width='stretch')
        st.download_button('Download council decision history',frame.to_csv(index=False),'council_decisions.csv','text/csv')
    else:
        st.info('No council decisions saved yet. Run the intraday council from the Signals tab.')
    st.subheader('Issued intraday signals')
    setups = PendingSetups(strategy_config(cfg,'intraday')).load()
    if setups:
        with st.expander('Conditional setup history',expanded=True):
            st.caption('Pending, confirmed, missed and expired plans are retained. These are not issued signals until activated.')
            table = [{**{k:s.get(k) for k in ('id','created_at','side','status','phase','reason','valid_until','signal_id')},
                      **s['plan']} for s in reversed(setups)]
            st.dataframe(styled_table(pd.DataFrame(table)),hide_index=True,width='stretch')
            st.download_button('Download conditional setup history',
                               '\n'.join(json.dumps(s) for s in setups)+'\n','conditional_setups.jsonl','application/x-ndjson')
    all_rows = []
    for name in ('intraday',):
        all_rows.extend(SignalBook(strategy_config(cfg, name)).load())
    if not all_rows:
        waits = sum(r['status']=='stand_aside' for r in decisions)
        blocked = sum(r['status']=='blocked' for r in decisions)
        watching = sum(s['status']=='pending' for s in setups)
        st.info(f'No BUY/SELL signal issued yet. {watching} setup(s) being monitored. Saved reviews: {waits} WAIT, {blocked} blocked. See the reasons above.')
        old_count = len(history_records(cfg))
        st.caption(f'{old_count:,} older trade/decision records remain visible in History & learning.')
        return
    rows = all_rows
    columns = ['strategy', 'side', 'issued_at', 'entry', 'stop', 'target', 'reward_risk', 'status', 'valid_until', 'closed_at', 'close_reason', 'points', 'outcome_r', 'current_r']
    frame = pd.DataFrame(sorted(rows, key=lambda r: r['issued_at'], reverse=True)).reindex(columns=columns)
    st.dataframe(styled_table(frame), hide_index=True, width='stretch')
    st.download_button('Download signal history', frame.to_csv(index=False), 'signals.csv', 'text/csv')
    fig = go.Figure()
    for name in ('intraday',):
        done = sorted([r for r in rows if r['strategy'] == name and r['status'] == 'closed'], key=lambda r: r['closed_at'])
        if done:
            fig.add_trace(go.Scatter(x=[r['closed_at'] for r in done], y=pd.Series([r['outcome_r'] for r in done]).cumsum(),
                                    mode='lines+markers', name=LABELS[name], line_color=COLORS[name]))
    if len(fig.data):
        fig.update_layout(height=300, yaxis_title='Cumulative signal outcome (R)', margin=dict(t=20, b=10))
        st.plotly_chart(fig, width='stretch')
    st.caption('R normalizes each result by its original entry-to-stop distance. This is signal quality, not an account return.')


def council(cfg):
    name = 'intraday'
    folder = Path(strategy_config(cfg, name)['paths']['reports_dir'])
    files = reports(cfg, name, previews=True)
    if not files:
        st.info('Run a council from the Signals tab to generate a report.')
        return
    choice = st.selectbox('Report', ['Latest available', *files], format_func=lambda p: str(p.relative_to(folder)) if isinstance(p,Path) else p)
    selected = files[0] if choice == 'Latest available' else choice
    r = read_json(selected, {})
    if not r:
        st.warning('Report is temporarily unavailable.')
        return
    banner(r['cio']['final_decision'],decision_reason(r),status_tone(r.get('signal',{}).get('status') or r['cio']['final_decision']))
    st.caption(f"Technical source: {r['data']['technical_source']}")
    st.caption(f"Saved {report_stamp(r,selected)} · {'Analysis preview' if r.get('kind') else 'Signal council'}")
    if r.get('entry_candidate'):
        with st.expander('Continuation entry evaluated by the council'):
            c=r['entry_candidate']
            st.write(c.get('reason') or f"{c['side']} proposal: entry {c['entry']:,.0f}, stop {c['stop']:,.0f}, target {c['target']:,.0f} · reward/risk {c['reward_risk']:.2f}:1")
            if c.get('target_basis'):st.caption(c['target_basis'])
            st.caption('This is a rule-based proposal. The council and live-price checks determine whether it becomes a signal.')
    if r.get('historical_learning'):
        with st.expander('Historical evidence used by this council'):
            st.text(r['historical_learning']['context'])
    for analyst in r['analysts']:
        render(badge(f"{analyst['title']} · {analyst['direction']} · {analyst['confidence']}",status_tone(analyst['direction'])))
        with st.expander(f"{analyst['title']} · {analyst['direction']} ({analyst['confidence']})"):
            st.text(analyst['report'])
    for label, value in [('Debate', r['debate']['summary']), ('Trader', r['trader']['reasoning']),
                         ('Risk review', r['risk']['opinion']), ('CIO', r['cio']['statement'])]:
        with st.expander(label):
            st.text(value)
    md = selected.with_suffix('.md')
    if md.exists():
        st.download_button('Download full report', md.read_text(), md.name)


def entry_research(cfg):
    root=Path(strategy_config(cfg,'intraday')['paths']['project_root'])
    files=sorted((root/'research').glob('entries_*.json'),reverse=True)
    files=[p for p in files if not p.name.endswith('.protocol.json')]
    if not files:return
    st.subheader('Entry experiments')
    st.caption('Historical replay · separate from actual trades · no automatic strategy changes')
    selected=st.selectbox('Experiment run',files,format_func=lambda p:p.stem.removeprefix('entries_'))
    result=read_json(selected,{})
    if result.get('purpose')!='research_only':
        st.warning('This experiment report is unavailable. Original file retained.');return
    banner('RESEARCH · NOT LIVE PERFORMANCE',
           'Mechanical entry rules are compared on the same saved prices, with fees and a daily trade cap. '
           'AI council approvals and execution delays are not reproduced. A smaller loss or zero trades is not proof of improvement.','info')
    st.caption(f"Sample {result['start']} to {result['end']} · data through {result['as_of']} IST · "
               f"round-trip fee {result['fee_points']:g} price points · "
               f"up to {result['daily_entry_limit']} entries per model per day")
    period=st.radio('Comparison period',['all','before_split','after_split'],horizontal=True,
                    format_func=lambda x:{'all':'Full sample','before_split':f"Before {result['split_date']}",
                                          'after_split':f"From {result['split_date']}"}[x])
    frame=pd.DataFrame([r for r in result['summaries'] if r['period']==period])
    frame=frame.reindex(columns=['label','entries','closed','wins','win_rate_pct','gross_points','fees_points','net_points','mean_net_r','gap_affected','unresolved'])
    st.dataframe(styled_table(frame),hide_index=True,width='stretch',column_config={
        'label':'Entry rule','entries':'Entries','closed':'Completed','wins':'Winners','win_rate_pct':'Win rate %',
        'gross_points':'Gross points','fees_points':'Fee points','net_points':'Net points after fees',
        'mean_net_r':'Average net R','gap_affected':'Exits with observation gaps','unresolved':'Unresolved',
    })
    st.caption('Points are INR per barrel at one unit, not account P&L. Models have different risk distances; compare net R too. Calendar slices are retrospective, not untouched validation data.')
    with st.expander('Fixed rules, data limits and replay trades'):
        st.json(result['protocol'])
        model=st.selectbox('Inspect experiment trades',list(result['trades']))
        st.dataframe(pd.DataFrame(result['trades'][model]),hide_index=True,width='stretch')
    st.download_button('Download experiment evidence',selected.read_bytes(),selected.name,'application/json')


def regime_research_view(cfg):
    from tradingagents_mcx.economics import configured_fee_points
    from tradingagents_mcx.regimes import MODELS, regime_candidates
    from tradingagents_mcx.strategies import intraday_data
    scfg = strategy_config(cfg, 'intraday'); now = now_ist()
    st.subheader('Trend or range?')
    st.caption('Available to the council for simulated entries · completed five-minute candles' if cfg.get('entry_models',{}).get('regime_enabled')
               else 'Research preview · completed five-minute candles · no automatic change to trade rules')
    bars, detail = intraday_data(scfg, cfg['analysis'].get('symbol', 'CRUDEOIL'), now)
    quote, quote_reason = latest_quote(scfg, now)
    state, proposals = regime_candidates(bars, quote, scfg['strategy'], now, configured_fee_points(cfg))
    labels = dict(trend_up='UPTREND', trend_down='DOWNTREND', range='RANGE', unstable='VOLATILITY SHOCK',
                  transition='MIXED CONDITIONS', warming_up='WARMING UP', unavailable='DATA NOT READY')
    tone = 'positive' if state['regime'] == 'trend_up' else 'negative' if state['regime'] == 'trend_down' else 'info' if state['regime'] == 'range' else 'warning'
    candidate = proposals['regime_switch']
    banner(labels[state['regime']], state['reason'], tone)
    if candidate['status'] == 'ready':
        st.write(f"Regime proposal: **{candidate['side']}** · Entry {candidate['entry']:,.0f} · "
                 f"Stop {candidate['stop']:,.0f} · Target {candidate['target']:,.0f} · "
                 f"R/R after assumed costs **{candidate['economics']['net_reward_risk']:.2f}:1**")
    else: st.info('Regime decision: WAIT · ' + candidate.get('reason', quote_reason))
    st.caption(detail + ' Observed tick gaps can hide price moves. This preview does not issue a signal.')
    files = sorted((Path(scfg['paths']['project_root']) / 'research').glob('regimes_*.json'), reverse=True)
    files = [p for p in files if not p.name.endswith('.protocol.json')]
    if not files: return
    selected = st.selectbox('Regime experiment run', files, format_func=lambda p:p.stem.removeprefix('regimes_'))
    result = read_json(selected, {})
    if result.get('purpose') != 'research_only':
        st.warning('Experiment report unavailable. Original file retained.'); return
    p = result['protocol']
    st.caption(f"{result['start']} to {result['end']} · {p['execution_delay_seconds']}s entry delay · "
               f"{p['fee_points']:g} fee points + {p['slippage_points_each_way']:g} slippage point each way · "
               f"maximum {p['daily_entry_limit']} entries per model/day")
    period = st.radio('Regime comparison period', ['all','before_split','after_split'], horizontal=True,
        format_func=lambda value:{'all':'Full sample','before_split':f"Before {result['split_date']}",
                                  'after_split':f"From {result['split_date']}"}[value])
    rows = [r for r in result['summaries'] if r['period'] == period]
    frame = pd.DataFrame(rows).reindex(columns=['label','entries','closed','win_rate_pct','net_points','mean_net_r',
                'realized_drawdown_points','gap_affected','unresolved'])
    st.dataframe(styled_table(frame), hide_index=True, width='stretch', column_config={
        'label':'Rule','entries':'Entries','closed':'Completed','win_rate_pct':'Win rate %',
        'net_points':'Net points after fees + slippage','mean_net_r':'Average net R',
        'realized_drawdown_points':'Realized drawdown (points)','gap_affected':'Gap-affected exits','unresolved':'Unresolved'})
    st.caption('Points are INR per barrel at one unit, not account P&L. This is a mechanical historical comparison; '
               'council selection and real fills are not reproduced. Fewer trades or a smaller loss does not establish an edge.')
    with st.expander('Regime rules, wait reasons and replay trades'):
        st.write(result['limitations']); st.json(result['rules'])
        st.write('Observed regimes'); st.json(result['regime_counts'])
        st.dataframe(pd.DataFrame(result['execution_rejections']), hide_index=True, width='stretch')
        model = st.selectbox('Inspect regime trades', list(MODELS), format_func=lambda m:MODELS[m])
        st.dataframe(pd.DataFrame(result['trades'][model]), hide_index=True, width='stretch')
        st.dataframe(pd.DataFrame([dict(as_of=r['as_of'], regime=r['regime'], **r['models'][model])
                                  for r in result['decisions'][-100:]]), hide_index=True, width='stretch')
    st.download_button('Download regime experiment evidence', selected.read_bytes(), selected.name, 'application/json')


@st.cache_data(ttl=15, show_spinner=False)
def what_if_results(cfg, cases, refresh_bucket):
    from tradingagents_mcx.what_if import evaluate_cases
    return evaluate_cases(cfg, cases)


def what_if_view(cfg):
    from tradingagents_mcx.what_if import load_cases
    st.subheader('What if we took the trade?')
    st.caption('Rejected, blocked, cancelled and replaced plans, followed using their original entry conditions, stop, target and expiry. Hypothetical outcomes only; accepted trades and wallet P&L stay separate.')
    cases=load_cases(cfg)
    period=st.radio('What-if period',['Today','Last 7 days','All history'],index=1,horizontal=True,key='what_if_period')
    day=now_ist().date()
    cutoff=day if period=='Today' else day-dt.timedelta(days=6) if period=='Last 7 days' else None
    if cutoff: cases=[r for r in cases if trade_date(r['decision_at'])>=cutoff]
    origins=st.multiselect('Council actions',['Rejected','Blocked','Cancelled','Replaced'],default=['Rejected','Blocked','Cancelled','Replaced'],key='what_if_actions')
    cases=[r for r in cases if r['origin'] in origins]
    rows=what_if_results(cfg,cases,int(now_ist().timestamp()//15))
    scored=[r for r in rows if r['comparison_eligible']]
    missed=sum(r['net_points']>0 for r in scored);avoided=sum(r['net_points']<0 for r in scored)
    tracking=sum(r['outcome']=='Still tracking' for r in rows)
    metric_strip([
        ('Avoided losses',str(avoided),'positive','Hypothetical closed losers after costs'),
        ('Missed winners',str(missed),'negative','Hypothetical closed winners after costs'),
        ('Still tracking',str(tracking),'active' if tracking else 'neutral','Waiting for an entry or exit'),
        ('Other decisions',str(len(rows)-len(scored)-tracking),'info','Unscored, repeated, or also taken'),
    ])
    st.caption('Saved price observations can miss moves between samples. Fees from the original proposal, or an explicit 8-point round-trip assumption when unavailable; plus 1 point slippage each way. R includes these costs. Gaps are unscored. Identical hypothetical fills count once; matching accepted trades are marked Also taken. Other calls can overlap, so these results are not a portfolio return or proof of an edge.')
    if not rows:
        st.info('No rejected or cancelled council plans in this selection. Older records are available under All history.')
        return
    outcome=st.selectbox('What-if outcomes',['All outcomes','Missed winner','Avoided loss','Still tracking','No entry','Not testable','Incomplete data','Also taken','Same opportunity'],key='what_if_outcome')
    visible=rows if outcome=='All outcomes' else [r for r in rows if r['outcome']==outcome]
    frame=pd.DataFrame(visible)
    columns=['decision_at','origin','outcome','side','entry','stop','target','exit','net_points','outcome_r',
             'opened_at','closed_at','note','reason','id','source','cost_basis']
    if len(frame):
        def color_outcome(value):
            color={'Avoided loss':'#118d67','Missed winner':'#dd4b50','Still tracking':'#1379b5',
                   'Incomplete data':'#a76a00','No entry':'#7a8495','Not testable':'#7a8495'}.get(value)
            return f'color: {color}; font-weight: 700' if color else ''
        styled=frame.reindex(columns=columns).style.map(color_outcome,subset=['outcome'])
        st.dataframe(styled,hide_index=True,width='stretch',column_config={
            'decision_at':'Council decision (IST)','origin':'Action','outcome':'What happened',
            'entry':'Hypothetical entry','exit':'Exit / latest mark','net_points':st.column_config.NumberColumn('Net points',format='%.2f'),
            'outcome_r':st.column_config.NumberColumn('Net R',format='%.2f'),
            'reason':'Why council declined / cancelled','note':'Replay result','cost_basis':'Fee assumption',
        })
        selected=st.selectbox('Inspect what-if plan',visible,format_func=lambda r:f"{r['decision_at']} · {r['origin']} · {r['outcome']} · {r['id']}",key='what_if_detail')
        with st.expander('Original plan and hypothetical outcome'):
            st.write('**Council reason:** '+selected['reason'])
            st.write('**Replay:** '+selected['note'])
            st.json(selected)
            source=Path(selected['report'])
            if source.exists(): st.download_button('Download source council report',source.read_bytes(),source.name,'application/json',key='what_if_source')
    else: st.info('No plans match this outcome filter.')
    st.download_button('Download what-if evidence',json.dumps(rows,indent=2),'intraday_what_if.json','application/json',key='what_if_download')
    st.caption(f'{len(rows)} preserved decisions in this selection. Recomputed from saved reports and prices every 15 seconds; original records are never rewritten.')


def archive(cfg):
    folder = Path(cfg['paths'].get('base_reports_dir', cfg['paths']['reports_dir']))
    files = sorted(folder.glob('**/*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    ledger = folder.parent / 'ledger' / 'calls.jsonl'
    st.subheader('History & learning')
    st.caption('All original records are preserved, including older long-term work. These files are read without changing them.')
    records = history_records(cfg)
    evidence = learning_context(cfg)
    feedback = evidence['intraday_feedback']; summary = feedback['summary']; entries = feedback['entries']
    st.subheader('What the system has learned')
    st.caption('Automatically refreshed from completed intraday trades and entry events. The next council receives this evidence; each new report saves a snapshot. No extra AI calls.')
    cols=st.columns(4)
    with cols[0]:metric('New intraday closed trades',str(summary['closed']),'info',f"{summary['wins']} net winners · legacy results separate")
    with cols[1]:metric('Net intraday P&L',money(summary['net_pnl_inr'],True),pnl_tone(summary['net_pnl_inr']),'After recorded trading fees · excludes API spend')
    with cols[2]:metric('Intraday trading fees',money(summary['fees_inr']),'warning',f"Gross P&L {money(summary['gross_pnl_inr'],True)}")
    with cols[3]:metric('Stale-quote blocks',str(entries['stale_quote_blocks']),'warning',f"Across {entries['review_count']} recent council reviews")
    if summary['closed']:
        banner('REVIEW COSTS BEFORE ENTRY',
               f"Recorded gross P&L {money(summary['gross_pnl_inr'],True)} becomes {money(summary['net_pnl_inr'],True)} after fees. "
               'The trader, risk reviewer and CIO now receive calculated reward/risk after configured trading costs.','warning')
        feedback_columns=['closed_at','side','net_pnl_inr','fees_inr','gross_pnl_inr','planned_net_reward_risk',
                          'holding_seconds','rapid_stop','stop_gap_points','close_reason','entry_method','net_outcome_r','trade_id','signal_id','id','opened_at']
        st.dataframe(styled_table(pd.DataFrame(list(reversed(feedback['trades']))).reindex(columns=feedback_columns)),
                     hide_index=True,width='stretch',column_config={
                         'net_pnl_inr':st.column_config.NumberColumn('Net P&L ₹',format='%.0f'),
                         'fees_inr':st.column_config.NumberColumn('Fees ₹',format='%.0f'),
                         'gross_pnl_inr':st.column_config.NumberColumn('Gross P&L ₹',format='%.0f'),
                         'planned_net_reward_risk':st.column_config.NumberColumn('Planned R/R after fees',format='%.2f'),
                         'holding_seconds':'Seconds in trade', 'rapid_stop':'Stop within 5 minutes',
                         'stop_gap_points':'Observed stop gap (points)', 'side':'Direction', 'closed_at':'Closed at',
                     })
        st.caption(f"Entry quality: {summary['rapid_stops']} stops within five minutes · "
                   f"{summary['below_one_net_rr']} trades entered below 1:1 planned reward/risk after fees. "
                   'The next council receives these diagnostics and checks stop placement against recent swing levels.')
    st.write(f"Since {entries['since']} IST: {sum(entries['setup_statuses'].values())} conditional plans; "
             f"{entries['setup_statuses'].get('activated',0)} activated and {entries['target_before_entry']} reached a target before entry.")
    st.caption('Missed targets are not profits. These observations do not prove an edge or automatically change thresholds. Confirmation, quote freshness and existing exit rules still apply.')
    if summary['excluded_records']:st.warning(f"{summary['excluded_records']} inconsistent records excluded from feedback; original data retained.")
    st.download_button('Download current learning evidence',json.dumps(feedback,indent=2),'intraday_learning.json','application/json')
    regime_research_view(cfg)
    entry_research(cfg)
    st.divider()
    a,b,c = st.columns(3)
    a.metric('All trade / decision records', len(records))
    b.metric('Council reports', len(files))
    c.metric('Completed same-day outcomes', evidence['eligible_outcomes'])
    st.caption('Same-day legacy results use recorded simulation prices. Multi-day and unresolved records remain visible, but are excluded from intraday learning outcomes.')
    if records:
        frame = pd.DataFrame(records)
        sources = st.multiselect('History sources', list(frame.source.unique()), default=list(frame.source.unique()))
        query = st.text_input('Filter history by date, ID or symbol', placeholder='For example: 2026-10-09')
        visible = frame[frame.source.isin(sources)]
        if query:
            visible = visible[visible.astype(str).agg(' '.join,axis=1).str.contains(query,case=False,regex=False)]
        columns = ['source','horizon','id','opened_at','side','entry','stop','target','reward_risk','status','closed_at','exit','points','outcome_r']
        st.dataframe(styled_table(visible.reindex(columns=columns)), hide_index=True, width='stretch')
        st.caption(f'Showing {len(visible):,} of {len(records):,} records. R is recomputed from entry, stop and exit, excluding costs.')
        st.download_button('Download combined history', frame.reindex(columns=columns).to_csv(index=False), 'complete_history.csv', 'text/csv')
    with st.expander('Learning evidence for the next council', expanded=True):
        st.text(evidence['context'])
    st.subheader('All council reports')
    if files:
        selected = st.selectbox('Archived report', files, format_func=lambda p: str(p.relative_to(folder)))
        md = selected.with_suffix('.md')
        if md.exists():
            with st.expander('Read original report'):
                st.text(md.read_text())
            st.download_button('Download archived report', md.read_bytes(), md.name, 'text/markdown')
        st.download_button('Download original report data', selected.read_bytes(), selected.name, 'application/json')
    notes = sorted((p for p in folder.glob('**/*.md') if not p.with_suffix('.json').exists()),key=lambda p:p.stat().st_mtime,reverse=True)
    if notes:
        with st.expander('Historical review notes'):
            note = st.selectbox('Review document',notes,format_func=lambda p:str(p.relative_to(folder)))
            st.text(note.read_text())
            st.download_button('Download original review',note.read_bytes(),note.name,'text/markdown')
    if ledger.exists():
        content = ledger.read_bytes()
        st.download_button('Download original trade history', content, ledger.name, 'application/x-ndjson')
    for name in NAMES:
        path = folder.parent/'signals'/f'{name}.jsonl'
        if path.exists():
            st.download_button(f'Download original {LABELS[name].lower()} records', path.read_bytes(), path.name, 'application/x-ndjson')
    memory = folder.parent/'memory/lessons.jsonl'
    lessons = read_lines(memory)
    if lessons:
        with st.expander(f'All saved lessons ({len(lessons)})'):
            st.dataframe(pd.DataFrame(lessons).reindex(columns=['id','resolved_at','setup_tag','mistake_tag','lesson']),hide_index=True,width='stretch')
            st.download_button('Download original lessons',memory.read_bytes(),memory.name,'application/x-ndjson')
    with st.expander('Historical market ticks'):
        ticks = sorted((Path(cfg['paths']['cache_dir'])/'live').glob('*_2*.csv'),reverse=True)
        if ticks:
            selected = st.selectbox('Market session',ticks,format_func=lambda p:p.stem)
            if st.checkbox('Load selected session ticks'):
                data = pd.read_csv(selected)
                st.line_chart(data.set_index('ts')['ltp'])
                st.dataframe(data,hide_index=True,width='stretch')
                st.download_button('Download original session ticks',selected.read_bytes(),selected.name,'text/csv')


def news():
    digest = read_json(ROOT / 'events/news_digest.json', {})
    if digest.get('digest'):
        st.write(digest['digest'])
    snapshot = read_json(ROOT / 'events/news_latest.json', {})
    st.caption(f"News updated: {snapshot.get('ts', 'not yet available')}")
    for item in snapshot.get('items', [])[:20]:
        st.write(item.get('title', ''))
        st.caption(f"{item.get('source', '')} · {item.get('published', '')}")
    radar_path = ROOT / 'events' / f'radar_{now_ist():%Y%m%d}.jsonl'
    if radar_path.exists():
        st.subheader('Event radar')
        for line in radar_path.read_text().splitlines()[-15:][::-1]:
            try:
                event = json.loads(line)
                st.write(f"{event.get('ts', '')} · {event.get('msg', '')}")
            except ValueError:
                continue


def health(cfg):
    from tradingagents_mcx.kite_data import KiteData, cached_quotes
    st.subheader('Market data')
    root = Path(strategy_config(cfg, 'intraday')['paths']['project_root'])
    kite = read_json(root / 'events/kite_feed.json', {})
    auth = KiteData(root).session_status(now_ist())
    session = auth['state'] in ('active', 'expiring_soon')
    fresh = cached_quotes(cfg['paths']['cache_dir'], cfg['analysis'].get('symbol','CRUDEOIL'))
    saved = read_json(Path(cfg['paths']['cache_dir']) / 'live' / f"latest_{cfg['analysis'].get('symbol','CRUDEOIL')}.json", {})
    now = now_ist(); policy = profiles(cfg)['intraday']
    market_open = (now.weekday() < 5 and now.date().isoformat() not in policy.get('holidays', [])
                   and dt.time.fromisoformat(policy['session_open']) <= now.time() <= dt.time.fromisoformat(policy['session_close']))
    if auth['state'] == 'expired':
        expiry = pd.Timestamp(auth['expires_at']).strftime('%d %b %Y at %H:%M IST')
        banner('KITE LOGIN EXPIRED', f'Expired {expiry}. Use Log in to Kite above to reconnect.', 'negative')
    elif session and not market_open:
        banner('KITE AUTHORIZED · MARKET CLOSED', 'Last prices remain visible. Fresh trade quotes resume with the market session.', 'info')
    elif fresh is not None:
        banner('KITE DATA LIVE', f"{fresh.iloc[0]['tradingsymbol']} · exchange update {fresh.iloc[0]['exchange_timestamp']} IST", 'positive')
    elif not session:
        banner('KITE LOGIN NEEDED', 'The existing feed remains the fallback. Log in to authorize a fresh Kite session.', 'warning')
    else:
        banner('WAITING FOR FRESH KITE DATA', kite.get('reason', 'Checking the stream'), 'warning')
    if not session:
        st.link_button('Connect / renew Kite login', 'http://127.0.0.1:8000/login')
    if session:
        st.caption('Login expires ' + pd.Timestamp(auth['expires_at']).strftime('%d %b %Y at %H:%M IST') + '.')
    st.caption(f"Collector source: {saved.get('source','Saved feed')} · last saved quote {saved.get('ts','unavailable')} IST. "
               'Kite sessions expire at 06:00 IST; renew through the local browser login. Orders remain simulated.')
    candles = read_json(ROOT / 'events/kite_history.json', {})
    if candles.get('status') == 'updated':
        st.caption(f"Kite history: {candles['completed_candles']:,} completed five-minute candles · last candle {candles['last_completed_candle']} IST")
    elif candles: st.caption(candles.get('reason', 'Historical candles pending'))
    with st.expander('Kite connection details'):
        st.json(kite)
    guard = read_json(ROOT / 'events/position_guard.json', {})
    stamp = guard.get('ts')
    age = (now_ist() - dt.datetime.fromisoformat(stamp)).total_seconds() if stamp else float('inf')
    (st.success if age < 45 else st.error)(f"Signal guard: {'running' if age < 45 else 'not reporting'}" + (f' · last check {stamp}' if stamp else ''))
    st.json(guard.get('strategies', {}))
    for name in ('intraday',):
        p = profiles(cfg)[name]
        st.write(f"{LABELS[name]}: every {p['interval_hours']:g} hours; minimum reward/risk {p['min_reward_risk']}:1")
    st.caption('Signals require a fresh contract quote. During a feed outage, overdue exits are flagged; the next observed price records the delayed exit.')
    st.subheader('Models & API cost')
    usage=usage_summary(cfg)
    c1,c2,c3=st.columns(3)
    with c1:metric('API cost today',f"${usage['today_usd']:,.4f}",'warning','Recorded token-usage estimates')
    with c2:metric('API cost all time',f"${usage['total_usd']:,.2f}",'info','Includes recorded historical usage')
    with c3:metric('API calls today / total',f"{usage['today_calls']:,} / {usage['total_calls']:,}",'active')
    quote=read_json(Path(cfg['paths']['cache_dir'])/'live'/f"latest_{cfg['analysis'].get('symbol','CRUDEOIL')}.json",{})
    fx=quote.get('usdinr')
    if isinstance(fx,(int,float)) and fx>0:
        st.caption(f"Approximate INR: ₹{usage['today_usd']*fx:,.2f} today · ₹{usage['total_usd']*fx:,.2f} all time, "
                   f"using USDINR {fx:.3f} from {quote.get('ts','the latest saved quote')} IST.")
    st.caption('Recorded estimates from API token usage and the rates saved at call time; provider invoices may differ. Historical costs are not repriced. API spending is separate from simulated trade P&L.')
    if usage['unpriced_calls']:st.caption(f"{usage['unpriced_calls']:,} calls have no known recorded price; displayed cost excludes those calls.")
    llm=cfg.get('llm',{})
    st.write(f"Configured provider: {llm.get('active_provider','default')}")
    routes=llm.get('routes') or llm.get('models') or {'default':llm.get('model','unspecified')}
    st.dataframe(pd.DataFrame([{'role':k,'configured model / route':v} for k,v in routes.items()]),hide_index=True,width='stretch')
    frame=usage['rows']
    if len(frame):
        st.caption(f"{usage['input_tokens']:,} input tokens · {usage['output_tokens']:,} output tokens recorded")
        daily=frame.pivot_table(index='date',columns='provider',values='usd',aggfunc='sum').fillna(0)
        daily['total USD']=frame.groupby('date').usd.sum()
        daily['calls']=frame.groupby('date').size()
        st.write('Daily API spend by provider')
        st.dataframe(daily.sort_index(ascending=False),width='stretch')
        with st.expander('Usage by model and role'):
            period=st.radio('API usage period',['Today','All time'],horizontal=True)
            subset=usage['today'] if period=='Today' else frame
            breakdown=subset.groupby(['provider','model','role']).agg(calls=('ts','size'),input_tokens=('input_tokens','sum'),
                output_tokens=('output_tokens','sum'),estimated_usd=('usd','sum')).reset_index()
            st.dataframe(breakdown,hide_index=True,width='stretch')
        st.download_button('Download API usage history',frame.to_csv(index=False),'api_usage_history.csv','text/csv')
    else:st.info('No API usage recorded yet.')


@st.fragment(run_every='15s')
def terminal():
    cfg = load_config()
    symbol = cfg['analysis'].get('symbol', 'CRUDEOIL')
    quote = read_json(Path(cfg['paths']['cache_dir']) / 'live' / f'latest_{symbol}.json', {})
    scfg = strategy_config(cfg,'intraday')
    render(f'<div class="desk-ticker"><span>{safe(symbol)} <strong>{money(quote.get("ltp"))}</strong>'
           f' · Exchange trade {safe(quote.get("ltt","unavailable"))}</span>'
           f'<span>Updated {now_ist():%H:%M:%S} IST · refreshes every 15s</span></div>')
    desk_status(cfg, scfg, quote, compact=True)
    kite_login_panel(cfg)
    tabs = st.tabs(['Live trades', 'Trade history', 'What if?', 'Signals', 'Market', 'Health', 'History & learning'])
    with tabs[0]:
        live_trades(cfg)
    with tabs[1]:
        paper_trades(cfg)
    with tabs[2]:
        what_if_view(cfg)
    with tabs[3]:
        signal_tabs=st.tabs(['Current signal','Signal history','AI Council'])
        with signal_tabs[0]:
            run_feedback(cfg)
            active = {'intraday':signal_card(cfg, 'intraday')}
        with signal_tabs[1]:history(cfg)
        with signal_tabs[2]:council(cfg)
    with tabs[4]:
        market_tabs=st.tabs(['Price chart','Indicator tape','News & radar'])
        with market_tabs[0]:
            metric_strip([(label,f'{quote[key]:,.2f}' if isinstance(quote.get(key),(int,float)) else '—','info','')
                          for label,key in [('MCX','ltp'),('WTI','wti'),('USDINR','usdinr'),('Parity reference','fair')]])
            st.caption(f"MCX print: {quote.get('ts', 'unavailable')} IST · contract expiry {quote.get('expiry', '—')}")
            session_chart(cfg, active)
        with market_tabs[1]:indicator_tape(cfg)
        with market_tabs[2]:news()
    with tabs[5]:
        desk_status(cfg,scfg,quote)
        health(cfg)
    with tabs[6]:
        archive(cfg)


def main():
    st.set_page_config(page_title='MCX · Intraday', page_icon='🛢️', layout='wide',initial_sidebar_state='collapsed')
    theme()
    header()
    settings(load_config())
    terminal()


if __name__ == '__main__':
    main()
