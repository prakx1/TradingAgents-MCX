import datetime as dt
import hashlib
from http.server import HTTPServer
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import requests

from kite_login import LoginHandler
from tradingagents_mcx.kite_data import KiteData, atomic_json, cached_quotes, normalize_quote, select_contracts
from tradingagents_mcx.kite_stream import QuoteStore, decode_packets
from tradingagents_mcx.kite_history import refresh_candles, historical_bars
from tradingagents_mcx.strategies import IST


NOW = dt.datetime(2026, 10, 27, 16)
INSTRUMENT = dict(name='CRUDEOIL', exchange='MCX', instrument_type='FUT', expiry='2026-11-19',
                  instrument_token=123, tradingsymbol='CRUDEOIL26NOVFUT')


def quote():
    return dict(last_price=8000, timestamp=NOW, last_trade_time=NOW, volume=1000, oi=500,
                average_price=7990, ohlc=dict(open=7900, high=8050, low=7850, close=7950))


class KiteDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.data = KiteData(self.root)
        atomic_json(self.data.credentials_path, dict(api_key='test-key', api_secret='test-secret', user_id='TEST'), private=True)

    def test_token_exchange_private_storage_and_daily_expiry(self):
        response = Mock(ok=True); response.json.return_value = dict(status='success', data=dict(access_token='test-token', user_id='TEST'))
        with patch('tradingagents_mcx.kite_data.requests.post', return_value=response) as post, patch('tradingagents_mcx.kite_data.now_ist', return_value=NOW):
            result = self.data.exchange_token('test-request')
        self.assertNotIn('access_token', result)
        self.assertEqual(post.call_args.kwargs['data']['checksum'], hashlib.sha256(b'test-keytest-requesttest-secret').hexdigest())
        self.assertEqual(self.data.session_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.data.private.stat().st_mode & 0o777, 0o700)
        self.assertTrue(self.data.session(NOW))
        self.assertFalse(self.data.session(NOW.replace(day=28, hour=6)))

    def test_no_orders_and_errors_do_not_expose_secrets(self):
        with patch('tradingagents_mcx.kite_data.requests.get') as request:
            for path in ['/orders', '/orders/regular', '/user/profile', '/portfolio/positions']:
                with self.assertRaises(ValueError): self.data.get(path)
            request.assert_not_called()
        with patch('tradingagents_mcx.kite_data.requests.post', side_effect=requests.ConnectionError('test-secret in request')):
            with self.assertRaises(ValueError) as error: self.data.exchange_token('test-request')
        self.assertNotIn('test-secret', str(error.exception))

    def test_login_status_expiry_boundary_is_read_only_and_secret_free(self):
        expires = NOW.replace(day=28, hour=6)
        atomic_json(self.data.session_path, dict(api_key='test-key', access_token='test-token',
                    expires_at=expires.isoformat()), private=True)
        before = self.data.session_path.read_bytes()
        with patch('tradingagents_mcx.kite_data.requests.get') as request:
            for remaining, state in [(3601, 'active'), (3600, 'expiring_soon'),
                                     (1, 'expiring_soon'), (0, 'expired'), (-1, 'expired')]:
                with self.subTest(remaining=remaining):
                    status = self.data.session_status(expires - dt.timedelta(seconds=remaining))
                    self.assertEqual(status['state'], state)
                    self.assertEqual(status['seconds_remaining'], max(0, remaining))
                    self.assertEqual(set(status), {'state', 'expires_at', 'seconds_remaining', 'configured'})
                    for secret in ('test-key', 'test-secret', 'test-token'):
                        self.assertNotIn(secret, json.dumps(status))
            aware = (expires - dt.timedelta(hours=2)).replace(tzinfo=IST).astimezone(dt.timezone.utc)
            self.assertEqual(self.data.session_status(aware)['seconds_remaining'], 7200)
            request.assert_not_called()
        self.assertEqual(self.data.session_path.read_bytes(), before)

    def test_login_status_requires_a_complete_matching_session(self):
        self.assertEqual(self.data.session_status(NOW)['state'], 'login_required')
        for value in [[], {}, dict(api_key='other-key', access_token='test-token', expires_at='2026-10-28T06:00:00'),
                      dict(api_key='test-key', expires_at='2026-10-28T06:00:00'),
                      dict(api_key='test-key', access_token='test-token', expires_at='invalid')]:
            atomic_json(self.data.session_path, value, private=True)
            self.assertEqual(self.data.session_status(NOW)['state'], 'login_required')
        self.data.session_path.write_text('{broken')
        self.assertEqual(self.data.session_status(NOW)['state'], 'login_required')
        self.data.credentials_path.unlink()
        self.assertEqual(self.data.session_status(NOW)['state'], 'not_configured')

    def test_dashboard_login_status_updates_after_a_new_session(self):
        from streamlit.testing.v1 import AppTest
        cfg = dict(paths=dict(project_root=str(self.root), reports_dir=str(self.root/'reports')),
                   analysis={})
        script = 'from tradingagents_mcx.config import load_config\nfrom live_dashboard import kite_login_panel\nkite_login_panel(load_config())'
        atomic_json(self.data.session_path, dict(api_key='test-key', access_token='test-token',
                    expires_at='2026-10-27T06:00:00'), private=True)
        with patch('tradingagents_mcx.config.load_config', return_value=cfg), \
             patch('live_dashboard.now_ist', return_value=NOW):
            app = AppTest.from_string(script).run()
            self.assertFalse(app.exception, list(app.exception))
            self.assertIn('KITE LOGIN · EXPIRED', '\n'.join(x.value for x in app.markdown))
            link = app.get('link_button')[0].proto
            self.assertEqual(link.label, 'Log in to Kite')
            self.assertEqual(link.url, 'http://127.0.0.1:8000/login')
            self.assertFalse(link.disabled)
            atomic_json(self.data.session_path, dict(api_key='test-key', access_token='new-test-token',
                        expires_at='2026-10-28T06:00:00'), private=True)
            app.run()
            self.assertFalse(app.exception, list(app.exception))
            rendered = '\n'.join(x.value for x in app.markdown)
            self.assertIn('KITE LOGIN · ACTIVE', rendered)
            self.assertIn('28 Oct 2026 at 06:00 IST', rendered)
            self.assertIn('14h 0m remaining', rendered)
            self.assertNotIn('test-token', rendered)

    def test_instruments_keep_main_futures_and_expiry(self):
        rows = [INSTRUMENT, dict(INSTRUMENT, expiry='2026-10-19'),
                dict(INSTRUMENT, name='CRUDEOILM'), dict(INSTRUMENT, instrument_type='CE'),
                dict(INSTRUMENT, expiry='2026-12-18', instrument_token=124)]
        result = select_contracts(pd.DataFrame(rows), NOW)
        self.assertEqual([r['instrument_token'] for r in result], [123, 124])

    def test_freshness_checks_all_clocks_and_never_promotes_back_contract(self):
        row = normalize_quote(INSTRUMENT, quote(), NOW)
        path = self.root / 'kite/latest.json'
        atomic_json(path, dict(quotes=[row])); self.assertEqual(cached_quotes(self.root, now=NOW).iloc[0]['close'], 8000)
        for field, seconds in [('received_at', 31), ('exchange_timestamp', 121), ('last_trade', 121), ('received_at', -1)]:
            bad = dict(row, **{field: str(NOW - dt.timedelta(seconds=seconds))})
            later = dict(row, expiry='2026-12-18', instrument_token=124)
            atomic_json(path, dict(quotes=[bad, later]))
            self.assertIsNone(cached_quotes(self.root, now=NOW), field)

    def test_full_packet_decodes_prices_timestamps_and_depth(self):
        packet = bytearray(184)
        values = {0:123, 4:800012, 8:5, 12:799000, 16:1000, 20:300, 24:400,
                  28:790000, 32:805000, 36:785000, 40:795000,
                  44:int(NOW.replace(tzinfo=IST).timestamp()), 48:500, 52:550, 56:400,
                  60:int(NOW.replace(tzinfo=IST).timestamp()), 64:20, 68:800000}
        for offset, value in values.items(): struct.pack_into('>I', packet, offset, value)
        struct.pack_into('>H', packet, 72, 3)
        message = struct.pack('>HH', 1, 184) + packet
        rows = decode_packets(message, {123: INSTRUMENT})
        self.assertEqual(rows[0][1]['last_price'], 8000.12)
        self.assertEqual(rows[0][1]['timestamp'], NOW)
        self.assertEqual(rows[0][1]['depth']['buy'][0], dict(quantity=20, price=8000, orders=3))
        self.assertEqual(decode_packets(message, {}), [])
        self.assertEqual(decode_packets(b'\x00', {123: INSTRUMENT}), [])
        self.assertEqual(decode_packets('{"type":"order"}', {123: INSTRUMENT}), [])
        with self.assertRaises(ValueError): decode_packets(message[:-1], {123: INSTRUMENT})
        with self.assertRaises(ValueError): decode_packets(message + b'x', {123: INSTRUMENT})

    def test_tick_archive_appends_and_out_of_order_does_not_replace_latest(self):
        store = QuoteStore(self.root); store.record(INSTRUMENT, quote(), NOW)
        path = self.root / 'kite/ticks/CRUDEOIL_20261027.jsonl'; original = path.read_bytes()
        old = dict(quote(), timestamp=NOW - dt.timedelta(seconds=5), last_price=7900)
        store.record(INSTRUMENT, old, NOW + dt.timedelta(seconds=1)); store.publish(NOW)
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(len(path.read_text().splitlines()), 2)
        self.assertEqual(cached_quotes(self.root, now=NOW).iloc[0]['close'], 8000)

    def test_login_nonce_cookie_and_one_time_use(self):
        data = Mock(); data.login_url.side_effect = lambda state: 'https://example.invalid/?state=' + state
        handler = type('TestLoginHandler', (LoginHandler,), dict(data=data, pending={}))
        server = HTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'; headers = {'Host':'127.0.0.1:8000'}
        try:
            browser = requests.Session()
            r = browser.get(base + '/login', headers=headers, allow_redirects=False)
            state = parse_qs(urlsplit(r.headers['Location']).query)['state'][0]
            r = browser.get(base + '/?request_token=one&state=' + state, headers=headers, allow_redirects=False)
            self.assertEqual(r.status_code, 303); self.assertEqual(r.headers['Location'], '/connected')
            data.exchange_token.assert_called_once_with('one')
            r = browser.get(base + '/?request_token=one&state=' + state, headers=headers)
            self.assertEqual(r.status_code, 403); data.exchange_token.assert_called_once()
            r = browser.get(base + '/login', headers=headers, allow_redirects=False)
            state = parse_qs(urlsplit(r.headers['Location']).query)['state'][0]
            r = requests.get(base + '/?request_token=two&state=' + state, headers=headers)
            self.assertEqual(r.status_code, 403); data.exchange_token.assert_called_once()
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_live_collection_keeps_legacy_csv_columns_and_audits_source(self):
        from tradingagents_mcx.live import append_tick
        frame = pd.DataFrame([normalize_quote(INSTRUMENT, quote(), NOW)])
        with patch('tradingagents_mcx.live.get_quote_cached', return_value=frame), \
             patch('tradingagents_mcx.live.get_wti_intraday', return_value=None), \
             patch('tradingagents_mcx.live.get_fx_live', return_value=None), \
             patch('tradingagents_mcx.strategies.now_ist', return_value=NOW):
            first = append_tick(str(self.root)); path = next((self.root / 'live').glob('CRUDEOIL_*.csv'))
            original = path.read_bytes(); append_tick(str(self.root))
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(len(pd.read_csv(path)), 2)
        self.assertNotIn('source', pd.read_csv(path).columns)
        self.assertEqual(first['source'], 'kite')
        self.assertEqual(first['instrument_token'], 123)
        self.assertEqual(len(next((self.root / 'live').glob('*.observations.jsonl')).read_text().splitlines()), 2)

    def test_broker_candles_preserve_revisions_and_exclude_future_and_wrong_contract(self):
        data = Mock()
        complete = ['2026-10-27T15:55:00+0530', 8000, 8020, 7990, 8010, 100, 50]
        future = ['2026-10-27T16:00:00+0530', 8010, 8030, 8000, 8020, 50, 50]
        data.get.return_value = dict(candles=[complete, future])
        stats = refresh_candles(data, self.root, INSTRUMENT, NOW)
        self.assertEqual(stats['completed_candles'], 1)
        audit = next((self.root/'kite/candles').glob('*.audit.jsonl')); original = audit.read_bytes()
        complete[4] = 8015
        refresh_candles(data, self.root, INSTRUMENT, NOW + dt.timedelta(seconds=1))
        self.assertTrue(audit.read_bytes().startswith(original)); self.assertEqual(len(audit.read_text().splitlines()), 2)
        bars = historical_bars(self.root, 'CRUDEOIL', '2026-11-19', NOW + dt.timedelta(seconds=2))
        self.assertEqual(len(bars), 1); self.assertEqual(bars.iloc[0]['close'], 8015)
        self.assertTrue(historical_bars(self.root, 'CRUDEOIL', '2026-12-18', NOW).empty)
        self.assertTrue(historical_bars(self.root, 'CRUDEOIL', '2026-11-19', NOW).empty)

    def test_intraday_indicators_prefer_broker_ohlcv_without_changing_saved_ticks(self):
        from tradingagents_mcx.strategies import intraday_data
        data = Mock(); data.get.return_value = dict(candles=[
            [(NOW - dt.timedelta(minutes=5 * i)).isoformat() + '+05:30',8000,8040,7980,8010,100,50]
            for i in range(80, 0, -1)])
        refresh_candles(data, self.root, INSTRUMENT, NOW)
        live = self.root / 'live'; live.mkdir()
        path = live / 'CRUDEOIL_20261027.csv'
        path.write_text('ts,ltp,vol_cum,expiry\n2026-10-27 15:55:10,8000,10,2026-11-19\n2026-10-27 15:57:10,8001,20,2026-11-19\n2026-10-27 15:59:10,8002,30,2026-11-19\n')
        original = path.read_bytes()
        bars, detail = intraday_data(dict(paths=dict(cache_dir=str(self.root))), 'CRUDEOIL', NOW)
        self.assertEqual(bars.iloc[-1]['high'], 8040); self.assertEqual(bars.iloc[-1]['volume'], 100)
        self.assertEqual(bars.attrs['contract_expiry'], '2026-11-19')
        self.assertEqual(bars.attrs['broker_candles'], 80)
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__': unittest.main()
