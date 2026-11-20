"""Read-only visual components for the intraday dashboard."""
from html import escape
import math

import pandas as pd
import streamlit as st


PALETTE = {
    'positive': ('#087443', '#e6f6ed'),
    'negative': ('#bb2445', '#fff0f3'),
    'warning': ('#935b06', '#fff5dc'),
    'info': ('#245bcc', '#edf3ff'),
    'active': ('#087c87', '#e5f7fa'),
    'neutral': ('#526078', '#eef1f6'),
}


def safe(value):
    return escape(str(value), quote=True)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def money(value, signed=False):
    value = number(value)
    return '—' if value is None else ('₹' + (f'{value:+,.0f}' if signed else f'{value:,.0f}'))


def pnl_tone(value):
    value = number(value)
    return 'positive' if value is not None and value > 0 else 'negative' if value is not None and value < 0 else 'neutral'


def status_tone(value):
    value = str(value).upper().replace('_', ' ')
    if value in ('OPEN', 'ACTIVE', 'ISSUED', 'CONFIRMED', 'TRIGGERED', 'REAFFIRMED'):
        return 'active'
    if any(word in value for word in ('BLOCK', 'REJECT', 'ERROR', 'FAIL', 'INVALID', 'STOP HIT')):
        return 'negative'
    if any(word in value for word in ('WAIT', 'PENDING', 'STAND ASIDE', 'WATCH', 'SKIP', 'MISS', 'EXPIRE')):
        return 'warning'
    if any(word in value for word in ('LONG', 'BULL', 'BUY', 'TARGET HIT')):
        return 'positive'
    if any(word in value for word in ('SHORT', 'BEAR', 'SELL')):
        return 'negative'
    return 'neutral'


def badge(label, tone='neutral'):
    tone = tone if tone in PALETTE else 'neutral'
    return f'<span class="desk-badge {tone}">{safe(label)}</span>'


def render(html):
    st.markdown(html, unsafe_allow_html=True)


def theme():
    variants = ''.join(f'.desk-badge.{tone}, .desk-tone-{tone} {{ --accent:{fg}; --tint:{bg}; }}'
                       for tone, (fg, bg) in PALETTE.items())
    render('''<style>
    .stApp { background: #f4f6fb; color: #17243c; }
    [data-testid="stHeader"] { background: #f4f6fb; }
    [data-testid="stSidebar"] { background: #eaf0f8; }
    .block-container { padding-top: 3rem; padding-bottom: 3rem; max-width: 1560px; }
    [data-testid="stMetric"] { background: white; border: 1px solid #dce4ef; border-radius: 12px;
        border-top: 3px solid #869bbd; padding: 14px 18px; }
    [data-testid="stMetricValue"] { font-size: 1.9rem; }
    [role="tablist"] { gap: 8px; border-bottom: 1px solid #cbd6e7; }
    [data-testid="stTab"] { padding: 10px 15px; border-radius: 9px 9px 0 0; }
    [data-testid="stTab"][aria-selected="true"] { background: #dce9ff; color: #184fae; font-weight: 750; }
    [data-testid="stTab"] .react-aria-SelectionIndicator { background: #2868dc; }
    .desk-header { background: #142742; color: #fff; border-radius: 14px; padding: 16px 22px;
        margin-bottom: 0; display:flex; align-items:center; justify-content:space-between; gap:16px; }
    .desk-header h1 { color: #fff; padding: 0; font-size: 1.65rem; letter-spacing: -.04em; }
    .desk-header p { color: #bed0e9; margin: 0; font-size: .95rem; }
    .desk-eyebrow { text-transform: uppercase; font-size: .72rem; letter-spacing: .12em; font-weight: 750; }
    .desk-header .desk-eyebrow { color: #7fe0d3; }
    .desk-badge { display:inline-flex; align-items:center; gap:6px; background:var(--tint); color:var(--accent);
        padding:5px 10px; border-radius:7px; font-size:.76rem; line-height:1.4; font-weight:800; letter-spacing:.02em; }
    .desk-card { background: white; border: 1px solid #dce4ef; border-top: 4px solid var(--accent);
        border-radius: 12px; padding: 15px 18px; height:100%; margin: 4px 0 12px; }
    .desk-card .desk-label { color:#526078; font-size:.8rem; font-weight:650; }
    .desk-card .desk-value { color:var(--accent); font-size:1.85rem; font-weight:800; line-height:1.35; letter-spacing:-.04em; }
    .desk-card .desk-note { color:#61708a; font-size:.77rem; margin-top:5px; }
    .desk-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:0 0 12px; }
    .desk-metrics .desk-card { margin:0; height:auto; }
    .desk-ticker { display:flex; justify-content:space-between; flex-wrap:wrap; align-items:center;
        gap:8px 20px; font-size:.82rem; color:#526078; padding:4px 0; }
    .desk-ticker strong { color:#17243c; font-size:1.05rem; }
    .desk-status-strip { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 4px; }
    .desk-status-item { background:var(--tint); color:var(--accent); border-radius:6px;
        font-size:.75rem; padding:5px 9px; font-weight:650; }
    .desk-recent { border:1px solid #dce4ef; border-left:4px solid var(--accent); border-radius:10px;
        background:white; padding:14px 16px; margin:0 0 8px; }
    .desk-recent-head { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .desk-recent-head strong { color:var(--accent); font-size:1.25rem; }
    .desk-recent p { color:#526078; font-size:.82rem; margin:9px 0 0; }
    .desk-services { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:0 0 18px; }
    .desk-service { border:1px solid #dce4ef; border-left:4px solid var(--accent); background:white; padding:12px 14px; border-radius:10px; }
    .desk-service strong { display:block; color:#17243c; font-size:.83rem; margin-bottom:6px; }
    .desk-service small { color:#526078; display:block; margin-top:7px; font-size:.75rem; }
    .desk-banner { background:var(--tint); border:1px solid #dce4ef; border-left:5px solid var(--accent);
        border-radius:12px; padding:16px 20px; margin:10px 0 15px; }
    .desk-banner strong { display:block; color:var(--accent); font-size:1.06rem; margin-bottom:5px; }
    .desk-banner p { color:#31435c; margin:0; font-size:.9rem; }
    .desk-trade { border:1px solid #b5d9df; border-top:5px solid #087c87; background:#fff; border-radius:16px; padding:22px 25px; margin:8px 0 18px; }
    .desk-trade-head { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; }
    .desk-trade h2 { margin:8px 0 4px; padding:0; font-size:1.65rem; letter-spacing:-.03em; color:#17243c; }
    .desk-trade .desk-sub { color:#526078; font-size:.85rem; }
    .desk-pnl { background:var(--tint); padding:12px 22px; border-radius:12px; min-width:205px; text-align:right; }
    .desk-pnl span { display:block; color:var(--accent); font-size:.76rem; font-weight:750; }
    .desk-pnl strong { color:var(--accent); display:block; font-size:2rem; }
    .desk-levels { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-top:18px; }
    .desk-level { border-left:3px solid var(--accent); background:var(--tint); padding:10px 14px; border-radius:8px; }
    .desk-level span { color:var(--accent); font-size:.76rem; font-weight:750; display:block; }
    .desk-level strong { color:var(--accent); font-size:1.55rem; }
    .desk-trade-foot { display:flex; gap:12px; justify-content:space-between; flex-wrap:wrap; margin-top:15px; color:#526078; font-size:.8rem; }
    .desk-rail { position:relative; height:10px; border-radius:6px; margin:28px 8px 9px; }
    .desk-rail-mark { position:absolute; top:-4px; width:4px; height:18px; background:#245bcc; border-radius:2px; }
    .desk-rail-now { position:absolute; top:-4px; width:18px; height:18px; background:#142742; border:3px solid white;
        outline:1px solid #142742; border-radius:50%; transform:translateX(-50%); }
    .desk-rail-labels { display:flex; justify-content:space-between; font-size:.76rem; color:#526078; }
    .desk-legend { display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 16px; }
    @media (max-width:850px) { .desk-services,.desk-levels,.desk-metrics {grid-template-columns:repeat(2,minmax(0,1fr));}
        .desk-trade-head {align-items:flex-start; flex-direction:column;} .desk-pnl {width:100%;text-align:left;min-width:0;}
        .desk-header {padding:14px 16px; flex-wrap:wrap;} .desk-header h1 {font-size:1.35rem;} .desk-trade {padding:16px;}
        .desk-card {padding:12px;} .desk-card .desk-value {font-size:1.55rem;}
        .desk-header .desk-mode {display:flex;gap:10px;align-items:center;}
        .desk-level {padding:8px 10px;} .desk-level strong {font-size:1.35rem;}
        .desk-rail-labels {gap:10px; font-size:.67rem;} }
    ''' + variants + '</style>')


def header():
    render('<div class="desk-header"><div><div class="desk-eyebrow">MCX / CRUDEOIL</div>'
           '<h1>Intraday trading desk</h1></div>'
           '<div class="desk-mode">' + badge('Simulation', 'active') + '<p style="font-size:.75rem">No broker orders</p></div></div>')


def metric(label, value, tone='info', note=''):
    render(metric_html(label,value,tone,note))


def metric_html(label, value, tone='info', note=''):
    return (f'<div class="desk-card desk-tone-{tone}"><div class="desk-label">{safe(label)}</div>'
            f'<div class="desk-value">{safe(value)}</div><div class="desk-note">{safe(note)}</div></div>')


def metric_strip(items):
    render('<div class="desk-metrics">'+''.join(metric_html(*item) for item in items)+'</div>')


def display_time(value):
    try:
        stamp=pd.Timestamp(value)
        if pd.isna(stamp):return 'Not recorded'
        if stamp.tzinfo:stamp=stamp.tz_convert('Asia/Kolkata')
        return stamp.strftime('%d %b · %H:%M:%S')
    except (TypeError,ValueError):
        return 'Not recorded'


def recent_trade(trade):
    pnl=trade.get('pnl_inr')
    reason=str(trade.get('close_reason') or 'Closed').replace('_',' ').capitalize()
    render(f'<div class="desk-recent desk-tone-{pnl_tone(pnl)}"><div class="desk-recent-head">'
           f'<span>{badge(trade["side"],status_tone(trade["side"]))} {badge(reason,status_tone(reason))}'
           f' {badge("Experimental","warning") if trade.get("origin")=="experimental" else ""}</span>'
           f'<strong>{money(pnl,True)}</strong></div>'
           f'<p>{safe(trade.get("symbol","CRUDEOIL"))} · {safe(trade["lots"])} lots · '
           f'{money(trade.get("entry"))} → {money(trade.get("exit"))}</p>'
           f'<p>Closed {safe(display_time(trade.get("closed_at")))} IST · after costs</p></div>')


def banner(title, detail, tone='info'):
    render(f'<div class="desk-banner desk-tone-{tone}"><strong>{safe(title)}</strong><p>{safe(detail)}</p></div>')


def services(items, compact=False):
    if compact:
        render('<div class="desk-status-strip">'+''.join(
            f'<span class="desk-status-item desk-tone-{tone}" title="{safe(note)}">{safe(label)} · {safe(state)}</span>'
            for label,state,note,tone in items)+'</div>')
        return
    render('<div class="desk-services">' + ''.join(
        f'<div class="desk-service desk-tone-{tone}"><strong>{safe(label)}</strong>{badge(state,tone)}<small>{safe(note)}</small></div>'
        for label, state, note, tone in items) + '</div>')


def trade_card(trade):
    from .economics import recorded_economics
    side = trade['side']
    pnl = trade.get('unrealized_inr')
    pnl_value = number(pnl)
    pnl_label = 'OPEN PROFIT' if pnl_value is not None and pnl_value > 0 else 'OPEN LOSS' if pnl_value is not None and pnl_value < 0 else 'OPEN P&L'
    stop, entry, target, current = (number(trade.get(k)) for k in ('stop', 'entry', 'target', 'last_price'))
    levels = ''.join(f'<div class="desk-level desk-tone-{tone}"><span>{label}</span><strong>{money(value)}</strong></div>'
                     for label, value, tone in [('ENTRY',entry,'info'),('CURRENT PRICE',current,'active'),
                                                 ('STOP-LOSS',stop,'negative'),('TAKE-PROFIT',target,'positive')])
    rail = ''
    if all(v is not None for v in (stop, entry, target, current)) and stop != target:
        lo, hi = sorted((stop, target))
        pos = lambda value: max(0, min(100, (value-lo)/(hi-lo)*100))
        gradient = '#f3abb9, #a0dfbf' if stop < target else '#a0dfbf, #f3abb9'
        left, right = ('Stop-loss', 'Take-profit') if stop < target else ('Take-profit', 'Stop-loss')
        rail = (f'<div class="desk-rail" style="background:linear-gradient(90deg,{gradient})" '
                f'role="img" aria-label="Last observed {current:g}; stop {stop:g}; entry {entry:g}; target {target:g}">'
                f'<span class="desk-rail-mark" style="left:{pos(entry):.2f}%"></span>'
                f'<span class="desk-rail-now" style="left:{pos(current):.2f}%"></span></div>'
                f'<div class="desk-rail-labels"><span>{left} · {lo:,.0f}</span><span>● Observed price · │ Entry</span><span>{right} · {hi:,.0f}</span></div>')
    rr = number(trade.get('reward_risk'))
    rr_label = f'1 : {rr:.2f}' if rr is not None else '—'
    economics = recorded_economics(trade)
    net_label = f"{economics['net_reward_risk']:.2f}:1" if economics else 'unavailable'
    deadline = display_time(trade.get('valid_until'))
    observed = display_time(trade.get('last_observed_at'))
    render(f'<section class="desk-trade" aria-label="Open trade"><div class="desk-trade-head"><div>'
           f'{badge("OPEN","active")} {badge(side,status_tone(side))} '
           f'{badge("Experimental","warning") if trade.get("origin")=="experimental" else badge("Council / older","info")}'
           f'<h2>{safe(trade.get("symbol","CRUDEOIL"))} · {safe(trade["lots"])} {"lot" if trade["lots"]==1 else "lots"}</h2>'
           f'<div class="desk-sub">Opened {safe(display_time(trade["opened_at"]))} IST · Risk : reward {rr_label} before costs</div>'
           f'<div class="desk-sub">Planned reward / risk after recorded costs: {net_label}</div></div>'
           f'<div class="desk-pnl desk-tone-{pnl_tone(pnl)}"><span>{pnl_label}</span><strong>{money(pnl,signed=True)}</strong>'
           '<span>After estimated trade costs</span></div></div>'
           f'<div class="desk-levels">{levels}</div>{rail}<div class="desk-trade-foot">'
           f'<span>Price observed {safe(observed)} IST</span><span>Square-off / deadline {safe(deadline)} IST</span>'
           f'</div></section>')


def styled_table(frame):
    """Style a view only; keep the original data and downloads untouched."""
    styler = frame.style.format(precision=2, na_rep='—', thousands=',')
    def cell_tone(tone):
        fg, bg = PALETTE[tone]
        return f'color: {fg}; background-color: {bg}; font-weight: bold'
    for col in frame.columns:
        if col in ('side', 'direction', 'label', 'status', 'decision', 'final_decision', 'close_reason'):
            styler = styler.map(lambda value: '' if pd.isna(value) else cell_tone(status_tone(value)), subset=[col])
        elif col in ('pnl_inr', 'unrealized_inr', 'net_pnl_inr', 'gross_pnl_inr', 'net_outcome_r', 'points', 'net_points', 'gross_points', 'mean_net_r', 'outcome_r', 'current_r', 'composite'):
            styler = styler.map(lambda value: '' if number(value) is None else cell_tone(pnl_tone(value)), subset=[col])
    return styler
