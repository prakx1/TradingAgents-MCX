"""Bounded quote recovery while retaining every existing freshness check."""
import time

from .signals import latest_quote
from .strategies import entry_allowed, now_ist


def wait_for_fresh_quote(cfg, expected_contract=None, progress=lambda message:None,
                         clock=None, sleep=None, max_wait_seconds=25):
    clock=clock or now_ist;sleep=sleep or time.sleep
    budget=max(0,min(30,float(max_wait_seconds)));waited=0;checks=0
    while True:
        now=clock();checks+=1
        quote,reason=latest_quote(cfg,now)
        if quote and expected_contract and str(quote['expiry'])[:10]!=expected_contract:
            quote=None;reason='Contract changed during quote recovery; a fresh council is required'
        meta=dict(checks=checks,retry_delay_seconds=waited,reason=reason,
                  checked_at=str(now),recovered=bool(quote and waited),quote_ts=quote.get('ts') if quote else None)
        if quote:return quote,meta
        retryable=reason in ('Exchange last-trade time is stale','MCX quote is stale or has a future timestamp',
                            'No usable live MCX contract quote')
        if not retryable or waited>=budget or not cfg['strategy'].get('enabled',True) or not entry_allowed(cfg['strategy'],now):
            return None,meta
        if waited==0:progress('Waiting briefly for a fresh contract quote; existing freshness rules still apply ...')
        pause=min(5,budget-waited);sleep(pause);waited+=pause
