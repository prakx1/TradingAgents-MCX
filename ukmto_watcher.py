#!/usr/bin/env python3
"""UKMTO headless watcher daemon — the primary source for Gulf/Red Sea maritime incidents.

Every 5 min (24x7) render ukmto.org/recent-incidents in one persistent headless browser,
diff against the seen-set, write events/ukmto_latest.json (consumed by the news registry
as an official-lane source), and on any NEW incident emit a radar alert + sound
immediately. Cloudflare 403 / timeouts -> back off 30 min. Restart-safe.

  nohup caffeinate -i .venv/bin/python ukmto_watcher.py >> ukmto.log 2>&1 &
"""
import datetime as dt
import json
import time
from pathlib import Path

from tradingagents_mcx.radar import Radar
from tradingagents_mcx.ukmto import UA, fetch_incidents

ROOT = Path(__file__).parent
EVENTS = ROOT / "events"; EVENTS.mkdir(exist_ok=True)
SEEN_P = EVENTS / "ukmto_seen.json"
LATEST_P = EVENTS / "ukmto_latest.json"
INTERVAL, BACKOFF = 5 * 60, 30 * 60


def load_seen() -> dict:
    try:
        return json.loads(SEEN_P.read_text())
    except Exception:
        return {}


def main():
    from playwright.sync_api import sync_playwright
    print(f"[ukmto] started {dt.datetime.now():%H:%M:%S} · every {INTERVAL//60}m · headless chromium", flush=True)
    radar = Radar(ROOT)
    seen = load_seen()
    first_run = not seen
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        while True:
            now = dt.datetime.now(); wait = INTERVAL
            try:
                inc, status = fetch_incidents(page)
                if status and status >= 400 or not inc:
                    print(f"{now:%H:%M:%S} blocked/empty (HTTP {status}, {len(inc)} parsed) — backing off {BACKOFF//60}m", flush=True)
                    wait = BACKOFF
                    try:  # fresh context after a block
                        page.close(); ctx.close(); ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}); page = ctx.new_page()
                    except Exception:
                        pass
                else:
                    new = [i for i in inc if str(i["num"]) not in seen]
                    for i in new:
                        seen[str(i["num"])] = now.strftime("%Y-%m-%d %H:%M:%S")
                        if not first_run:  # never re-alert history on first start
                            radar.emit("maritime_incident", "critical" if i["kind"].lower().startswith("attack") else "alert",
                                       f"UKMTO #{i['num']} {i['kind']}{' — ' + i['place'] if i['place'] else ''}: {i['desc'][:220]}",
                                       source="UKMTO", sound="alert", extra={"num": i["num"], "kind": i["kind"], "place": i["place"], "date": i["date"]})
                    items = [{"title": i["title"], "link": "https://www.ukmto.org/recent-incidents",
                              "published": i["when"].strftime("%Y-%m-%d %H:%M:%S") if i["when"] else None,
                              "first_seen": seen.get(str(i["num"])),
                              "kind": i["kind"], "place": i["place"], "num": i["num"], "desc": i["desc"]} for i in inc]
                    LATEST_P.write_text(json.dumps({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "items": items}))
                    SEEN_P.write_text(json.dumps(seen))
                    print(f"{now:%H:%M:%S} {len(inc)} reports · newest #{inc[0]['num']} {inc[0]['kind']} {inc[0]['date']}"
                          + (f" · {len(new)} NEW{' (history, not alerted)' if first_run else ' → ALERTED'}" if new else ""), flush=True)
                    first_run = False
            except Exception as e:
                print(f"{now:%H:%M:%S} error: {str(e)[:120]} — backing off {BACKOFF//60}m", flush=True)
                wait = BACKOFF
                try:
                    page.close(); ctx.close(); ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}); page = ctx.new_page()
                except Exception:
                    pass
            target = time.time() + wait
            while time.time() < target:
                time.sleep(min(30, max(1, target - time.time())))


if __name__ == "__main__":
    main()
