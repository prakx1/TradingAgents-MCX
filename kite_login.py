#!/usr/bin/env python3
"""Loopback-only Kite login callback. No credentials or tokens rendered/logged."""
import hmac
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from tradingagents_mcx.kite_data import KiteData


class LoginHandler(BaseHTTPRequestHandler):
    data = KiteData()
    pending = {}

    def log_message(self, *args):
        pass  # Callback query contains a one-time token.

    def reply(self, text, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(('<!doctype html><title>MCX · Kite connection</title><meta name="viewport" content="width=device-width">'
            '<main style="font:18px system-ui;max-width:640px;margin:10vh auto;padding:24px;line-height:1.6">'
            '<h1>MCX · Kite data</h1>' + text + '</main>').encode())

    def do_GET(self):
        if self.headers.get('Host') not in ('127.0.0.1:8000', 'localhost:8000'):
            return self.reply('Invalid local host.', 403)
        path = urlsplit(self.path); query = parse_qs(path.query)
        if path.path == '/login':
            state = secrets.token_urlsafe(32)
            self.pending = {k: v for k, v in type(self).pending.items() if time.monotonic() - v < 600}
            self.pending[state] = time.monotonic(); type(self).pending = self.pending
            try: target = self.data.login_url(state)
            except ValueError: return self.reply('Local Kite credentials have not been configured.', 503)
            self.send_response(302)
            self.send_header('Location', target)
            self.send_header('Set-Cookie', f'kite_login_state={state}; HttpOnly; SameSite=Lax; Path=/; Max-Age=600')
            self.send_header('Cache-Control', 'no-store'); self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers(); return
        if 'request_token' in query:
            state = query.get('state', [''])[0]
            try: cookie = SimpleCookie(self.headers.get('Cookie', ''))
            except CookieError: return self.reply('Invalid login cookie. <a href="/login">Start again</a>.', 403)
            browser_state = cookie.get('kite_login_state')
            created = type(self).pending.pop(state, None)
            if (not state or created is None or time.monotonic() - created > 600 or not browser_state
                    or not hmac.compare_digest(state, browser_state.value)):
                return self.reply('This login attempt expired. <a href="/login">Start again</a>.', 403)
            try: self.data.exchange_token(query['request_token'][0])
            except ValueError: return self.reply('Kite login could not finish. <a href="/login">Try again</a>.', 400)
            self.send_response(303); self.send_header('Location', '/connected')
            self.send_header('Cache-Control', 'no-store'); self.end_headers(); return
        if path.path == '/connected' and self.data.session():
            return self.reply('<p><strong>Login complete.</strong> The market-data collector can now connect.</p>'
                              '<p>Signals and trades remain a local simulation.</p><a href="http://localhost:8503">Open dashboard</a>')
        return self.reply('<p>Connect the paid Kite data feed to your local MCX desk.</p>'
                          '<p><a href="/login">Log in with Zerodha</a></p><p>This app uses market data for signals and simulation.</p>')


if __name__ == '__main__':
    print('Kite login ready at http://127.0.0.1:8000', flush=True)
    HTTPServer(('127.0.0.1', 8000), LoginHandler).serve_forever()
