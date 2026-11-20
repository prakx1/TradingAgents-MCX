"""UKMTO recent-incidents watcher (headless). The page is client-rendered and Cloudflare
blocks plain HTTP clients, so a real browser renders it; the parser works on the page's
text, which is stable: '<Type> UKMTO #<n>' / '<DD Month YYYY>' / description paragraph.
Incident dates are day-granular, so OUR first-seen time is the effective timestamp."""
from __future__ import annotations

import datetime as dt
import re

URL = "https://www.ukmto.org/recent-incidents"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
_BLOCK = re.compile(r"^([A-Z][A-Za-z /&-]{2,40}?) UKMTO #(\d+)\s*\n(\d{1,2} [A-Z][a-z]+ \d{4})\s*\n+(.+?)(?=\n\s*\n[A-Z][A-Za-z /&-]{2,40}? UKMTO #\d+\s*\n|\Z)", re.S | re.M)
_PLACES = ("Strait of Hormuz", "Hormuz", "Red Sea", "Gulf of Aden", "Bab al-Mandab", "Bab-el-Mandeb", "Persian Gulf", "Arabian Gulf",
           "Gulf of Oman", "Arabian Sea", "Indian Ocean", "Somalia", "Yemen", "Aden", "Sudan", "Eritrea", "Fujairah", "Jeddah", "Hodeidah", "Suez")


def parse_text(body_text: str) -> list[dict]:
    out, seen = [], set()
    for m in _BLOCK.finditer(body_text):
        kind, num, date, desc = m.group(1).strip(), int(m.group(2)), m.group(3), " ".join(m.group(4).split())
        if num in seen:
            continue
        seen.add(num)
        try:
            when = dt.datetime.strptime(date, "%d %B %Y")
        except ValueError:
            when = None
        place = next((p for p in _PLACES if p.lower() in desc.lower()), "")
        gist = re.sub(r"^UKMTO has received (a |an )?(verified, )?(time-late )?report of (an incident |a suspicious approach |a )?", "", desc)
        gist = gist[:1].upper() + gist[1:]
        out.append({"num": num, "kind": kind, "date": date, "when": when, "place": place, "desc": desc,
                    "title": f"UKMTO #{num} {kind}{' — ' + place if place else ''}: {gist[:150]}"})
    return out


def fetch_incidents(page=None, timeout_ms: int = 60000) -> tuple[list[dict], int | None]:
    """Render with Playwright; returns (incidents, http_status). Pass an existing page to reuse a context."""
    from playwright.sync_api import sync_playwright
    def _grab(pg):
        resp = pg.goto(URL, wait_until="domcontentloaded", timeout=timeout_ms)
        status = resp.status if resp else None
        try:
            pg.wait_for_function("document.body && document.body.innerText.includes('UKMTO #')", timeout=25000)
        except Exception:
            pass
        return parse_text(pg.inner_text("body")), status
    if page is not None:
        return _grab(page)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}).new_page()
        try:
            return _grab(pg)
        finally:
            b.close()


if __name__ == "__main__":
    sample = """Recent Incidents\n\n60 reports\n\nAttack UKMTO #140\n21 September 2026\n\nUKMTO has received a report of an incident involving a vessel transiting the Strait of Hormuz. Military authorities have informed UKMTO that a tanker conducting an inbound transit was struck by an unknown projectile. Two crew have sustained minor injuries.\n\nAttack UKMTO #139\n18 September 2026\n\nUKMTO has received a verified, time-late report of an incident occurring on the 16th September 2026, of a Tanker struck by an unknown projectile while transiting outbound of the Strait of Hormuz. Crew have been reported as safe.\n\nSuspicious Approach UKMTO #138\n15 September 2026\n\nUKMTO has received a report of a suspicious approach 40NM south of Aden, Yemen. The master reports two small craft.\n"""
    inc = parse_text(sample)
    assert [i["num"] for i in inc] == [140, 139, 138], inc
    assert inc[0]["kind"] == "Attack" and inc[0]["place"] == "Strait of Hormuz" and inc[0]["when"] == dt.datetime(2026, 9, 21)
    assert inc[2]["kind"] == "Suspicious Approach" and inc[2]["place"] == "Yemen"
    assert inc[0]["title"].startswith("UKMTO #140 Attack — Strait of Hormuz: ")
    print("ukmto parser self-check OK:", inc[0]["title"][:90])
