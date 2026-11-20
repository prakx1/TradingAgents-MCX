"""Multi-source news ingestion with per-source latency measurement.

A registry of feeds (RSS/Atom via feedparser, or JSON with a simple item path), each
with a lane (official / wire / regional / social / aggregator), a trust weight, and
its own poll interval. fetch_all() returns deduped headlines with the source that
saw them FIRST and records first-seen timestamps, so the dashboard can show a live
latency scoreboard (published -> first seen) and we learn which sources are fastest
in practice instead of trusting claims.  Keyless and free unless a key is set.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

LANES = ("official", "wire", "regional", "social", "aggregator")

# name, url, fmt (rss|json|opec_html|ofac_delta), lane, trust (0-1), poll_min, optional ua/json spec.
# Verified live 21 Sep 2026 by the source sweep; lanes: official (bypass lexicon gate) · wire · regional · social · aggregator
SOURCES: list[dict] = [
    # ---- official / primary
    {"name": "EIA press releases", "url": "https://www.eia.gov/rss/press_rss.xml", "fmt": "rss", "lane": "official", "trust": 1.0, "poll_min": 30},
    {"name": "EIA Today in Energy", "url": "https://www.eia.gov/rss/todayinenergy.xml", "fmt": "rss", "lane": "official", "trust": 1.0, "poll_min": 60},
    {"name": "OPEC press releases", "url": "https://www.opec.org/press-releases.html", "fmt": "opec_html", "lane": "official", "trust": 1.0, "poll_min": 3},
    {"name": "OFAC sanctions delta", "url": "https://sanctionslistservice.ofac.treas.gov/changes/latest", "fmt": "ofac_delta", "lane": "official", "trust": 1.0, "poll_min": 5},
    {"name": "UKMTO incidents", "url": "events/ukmto_latest.json", "fmt": "file", "lane": "official", "trust": 1.0, "poll_min": 5},
    {"name": "CENTCOM (DVIDS)", "url": "https://www.dvidshub.net/rss/unit/72?type=news", "fmt": "rss", "lane": "official", "trust": 1.0, "poll_min": 10},
    {"name": "US 5th Fleet (DVIDS)", "url": "https://www.dvidshub.net/rss/unit/164?type=news", "fmt": "rss", "lane": "official", "trust": 1.0, "poll_min": 15},
    {"name": "Combined Maritime Forces", "url": "https://combinedmaritimeforces.com/feed/", "fmt": "rss", "lane": "official", "trust": 0.9, "poll_min": 30},
    # ---- wire-grade headlines
    {"name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 2},
    {"name": "Bloomberg Politics", "url": "https://feeds.bloomberg.com/politics/news.rss", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 3},
    {"name": "Bloomberg Economics", "url": "https://feeds.bloomberg.com/economics/news.rss", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 3},
    {"name": "Bloomberg Industries", "url": "https://feeds.bloomberg.com/industries/news.rss", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 3},
    {"name": "CNBC Energy", "url": "https://www.cnbc.com/id/19836768/device/rss/rss.html", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 2},
    {"name": "CNBC World", "url": "https://www.cnbc.com/id/100727362/device/rss/rss.html", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 3},
    {"name": "investingLive", "url": "https://www.investinglive.com/feed/news", "fmt": "rss", "lane": "wire", "trust": 0.75, "poll_min": 2},
    {"name": "Investing.com Commodities", "url": "https://www.investing.com/rss/news_11.rss", "fmt": "rss", "lane": "wire", "trust": 0.7, "poll_min": 3},
    {"name": "FT Commodities", "url": "https://www.ft.com/commodities?format=rss", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 5},
    {"name": "WSJ Markets", "url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "fmt": "rss", "lane": "wire", "trust": 0.8, "poll_min": 5},
    {"name": "MarketWatch Bulletins", "url": "https://feeds.content.dowjones.io/public/rss/mw_bulletins", "fmt": "rss", "lane": "wire", "trust": 0.75, "poll_min": 3},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "fmt": "rss", "lane": "wire", "trust": 0.6, "poll_min": 5},
    {"name": "OilPrice.com", "url": "https://oilprice.com/rss/main", "fmt": "rss", "lane": "wire", "trust": 0.7, "poll_min": 5},
    {"name": "Rigzone", "url": "https://www.rigzone.com/news/rss/rigzone_latest.aspx", "fmt": "rss", "lane": "wire", "trust": 0.65, "poll_min": 10},
    {"name": "Reuters (Bluesky)", "url": "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=reuters.com&limit=25&filter=posts_no_replies", "fmt": "json", "lane": "wire", "trust": 0.85, "poll_min": 2,
     "json": {"path": "feed", "item": "post.record", "title": "text", "time": "createdAt", "link": "uri"}},
    {"name": "Bloomberg (Bluesky)", "url": "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=bloomberg.com&limit=25&filter=posts_no_replies", "fmt": "json", "lane": "wire", "trust": 0.85, "poll_min": 2,
     "json": {"path": "feed", "item": "post.record", "title": "text", "time": "createdAt", "link": "uri"}},
    # ---- aggregators
    {"name": "Google News · crude 1h", "url": "https://news.google.com/rss/search?q=%22crude%20oil%22%20OR%20Hormuz%20OR%20OPEC%20when%3A1h&hl=en-US&gl=US&ceid=US:en", "fmt": "rss", "lane": "aggregator", "trust": 0.6, "poll_min": 3},
    {"name": "Google News · crude IN 2h", "url": "https://news.google.com/rss/search?q=crude%20oil%20when%3A2h&hl=en-IN&gl=IN&ceid=IN:en", "fmt": "rss", "lane": "aggregator", "trust": 0.6, "poll_min": 3},
    {"name": "GDELT crude/Hormuz", "url": "https://api.gdeltproject.org/api/v2/doc/doc?query=(Hormuz%20OR%20%22crude%20oil%22%20OR%20OPEC%20OR%20tanker)&mode=artlist&format=rss&timespan=2h", "fmt": "rss", "lane": "aggregator", "trust": 0.5, "poll_min": 15},
    # ---- regional / geopolitics / shipping
    {"name": "Iran International", "url": "https://www.iranintl.com/en/feed", "fmt": "rss", "lane": "regional", "trust": 0.65, "poll_min": 2},
    {"name": "Arab News", "url": "https://www.arabnews.com/rss.xml", "fmt": "rss", "lane": "regional", "trust": 0.65, "poll_min": 5, "ua": "curl/8.4.0"},
    {"name": "Middle East Eye", "url": "https://www.middleeasteye.net/rss", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 5},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "fmt": "rss", "lane": "regional", "trust": 0.65, "poll_min": 5},
    {"name": "BBC Middle East", "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml", "fmt": "rss", "lane": "regional", "trust": 0.7, "poll_min": 5},
    {"name": "Guardian Middle East", "url": "https://www.theguardian.com/world/middleeast/rss", "fmt": "rss", "lane": "regional", "trust": 0.7, "poll_min": 5},
    {"name": "Haaretz", "url": "https://www.haaretz.com/srv/haaretz-latest-headlines", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 5},
    {"name": "Middle East Monitor", "url": "https://www.middleeastmonitor.com/feed/", "fmt": "rss", "lane": "regional", "trust": 0.55, "poll_min": 5},
    {"name": "Anadolu Agency", "url": "https://www.aa.com.tr/en/rss/default?cat=guncel", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 3},
    {"name": "TASS", "url": "https://tass.com/rss/v2.xml", "fmt": "rss", "lane": "regional", "trust": 0.5, "poll_min": 5},
    {"name": "The National (UAE)", "url": "https://www.thenationalnews.com/arc/outboundfeeds/rss/?outputType=xml", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 5},
    {"name": "Splash247", "url": "https://splash247.com/feed/", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 5},
    {"name": "Seatrade Maritime", "url": "https://www.seatrade-maritime.com/rss.xml", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 5},
    {"name": "MarineLink", "url": "https://www.marinelink.com/news/rss", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 5},
    {"name": "TradeWinds", "url": "https://services.tradewindsnews.com/api/feed/rss", "fmt": "rss", "lane": "regional", "trust": 0.7, "poll_min": 3},
    {"name": "Hellenic Shipping News", "url": "https://www.hellenicshippingnews.com/feed/", "fmt": "rss", "lane": "regional", "trust": 0.55, "poll_min": 5},
    {"name": "gCaptain", "url": "https://gcaptain.com/feed/", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 10},
    {"name": "Maritime Executive", "url": "https://maritime-executive.com/articles.rss", "fmt": "rss", "lane": "regional", "trust": 0.6, "poll_min": 30},
    {"name": "Dryad Channel16", "url": "https://channel16.dryadglobal.com/rss.xml", "fmt": "rss", "lane": "regional", "trust": 0.7, "poll_min": 60},
    {"name": "Moneycontrol Commodities", "url": "https://www.moneycontrol.com/rss/commodities.xml", "fmt": "rss", "lane": "regional", "trust": 0.5, "poll_min": 5},
    {"name": "ET Commodities", "url": "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms", "fmt": "rss", "lane": "regional", "trust": 0.5, "poll_min": 5},
    {"name": "Business Standard Commodities", "url": "https://www.business-standard.com/rss/markets/commodities-10608.rss", "fmt": "rss", "lane": "regional", "trust": 0.5, "poll_min": 5},
    # ---- social (fast, noisy)
    {"name": "Reddit r/oil+energy+commodities", "url": "https://www.reddit.com/r/oil+energy+commodities/new.rss?limit=50", "fmt": "rss", "lane": "social", "trust": 0.35, "poll_min": 3,
     "ua": "tradingagents-mcx-newsradar/1.0 (paper trading desk; contact via repo)"},
    {"name": "ZeroHedge", "url": "https://feeds.feedburner.com/zerohedge/feed", "fmt": "rss", "lane": "social", "trust": 0.35, "poll_min": 5},
]

_MEMO: dict = {}          # name -> (fetched_ts, items)
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def _norm(title: str) -> str:
    t = re.split(r"\s+[-|–—]\s+[A-Z][\w .&']{2,30}$", title)[0]  # drop ' - Reuters' / ' | Al Jazeera' suffixes
    return re.sub(r"[^a-z0-9 ]+", "", t.lower())[:70]


def _key(title: str) -> str:
    return hashlib.md5(_norm(title).encode()).hexdigest()[:12]


def _to_dt(value) -> dt.datetime | None:
    """Best-effort publish time -> naive IST."""
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value) if isinstance(value, str) else value
    except Exception:
        try:
            d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).replace(tzinfo=None)


def _fetch_rss(url: str) -> list[dict]:
    import feedparser
    feed = feedparser.parse(url, request_headers=_UA)
    out = []
    for e in feed.entries[:40]:
        pub = e.get("published") or e.get("updated") or ""
        out.append({"title": (e.get("title") or "").strip(), "link": e.get("link", ""),
                    "published": _to_dt(pub), "published_raw": pub})
    return [o for o in out if o["title"]]


def _fetch_json(url: str, spec: dict) -> list[dict]:
    import requests
    r = requests.get(url, headers=_UA, timeout=10)
    r.raise_for_status()
    data = r.json()
    for k in spec.get("path", "").split("."):
        if k:
            data = data[int(k)] if k.isdigit() else data.get(k, [])
    out = []
    for it in data[:40]:
        node = it
        for k in spec.get("item", "").split("."):
            if k:
                node = node.get(k, {})
        title = node.get(spec.get("title", "title"), "")
        ts = node.get(spec.get("time", "created_utc"))
        pub = dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc) if isinstance(ts, (int, float)) else ts
        out.append({"title": str(title).strip(), "link": node.get(spec.get("link", "url"), ""), "published": _to_dt(pub), "published_raw": str(ts)})
    return [o for o in out if o["title"]]


def _fetch_opec_html(url: str) -> list[dict]:
    """OPEC has no RSS: diff the press-release cards on the listing page (first-seen is the timestamp)."""
    import requests
    r = requests.get(url, headers=_UA, timeout=20); r.raise_for_status()
    html = r.text
    seen, out = set(), []
    for m in re.finditer(r'href="\.?/?(press-releases/)?(pr-detail/[^"]+)"', html):
        link = "https://www.opec.org/press-releases/" + m.group(2)
        if link in seen:
            continue
        seen.add(link)
        before = html[max(0, m.start() - 2500):m.start()]
        heads = re.findall(r"<h[1-4][^>]*>(.*?)</h[1-4]>", before, re.S)
        title = re.sub(r"<[^>]+>|\s+", " ", heads[-1]).strip() if heads else ""
        date = re.findall(r"\b(\d{1,2} [A-Z][a-z]+ 20\d{2})\b", before)
        if len(title) > 12:
            out.append({"title": f"OPEC: {title}", "link": link, "published": _to_dt(date[-1] + " 12:00:00 +0000") if date else None, "published_raw": date[-1] if date else ""})
    return out[:20]


def _fetch_ofac_delta(url: str) -> list[dict]:
    """OFAC Sanctions List Service latest delta -> one item summarising new designations by program."""
    import requests
    r = requests.get(url, headers=_UA, timeout=20, allow_redirects=True); r.raise_for_status()
    x = r.text
    pub = re.search(r"<datePublished>([^<]+)</datePublished>", x)
    programs = sorted(set(re.findall(r"<program>([^<]+)</program>", x)))
    names = re.findall(r"<formattedFullName>([^<]+)</formattedFullName>", x)[:6]
    n_ent = len(re.findall(r"<entity ", x))
    when = _to_dt(pub.group(1)) if pub else None
    if not n_ent and not names:
        return []
    title = f"OFAC sanctions update: {n_ent} entities [{', '.join(programs[:5])}]" + (f" — {', '.join(names[:3])}" if names else "")
    return [{"title": title, "link": url, "published": when, "published_raw": pub.group(1) if pub else ""}]


def fetch_source(src: dict, force: bool = False) -> tuple[list[dict], str | None]:
    """Respect each source's poll interval via memo; returns (items, error)."""
    hit = _MEMO.get(src["name"])
    if hit and not force and time.time() - hit[0] < src.get("poll_min", 3) * 60:
        return hit[1], None
    try:
        fmt = src.get("fmt", "rss")
        if fmt in ("rss", "atom"):
            import feedparser
            hdr = dict(_UA, **({"User-Agent": src["ua"]} if src.get("ua") else {}))
            feed = feedparser.parse(src["url"], request_headers=hdr)
            items = []
            for e in feed.entries[:40]:
                pub = e.get("published") or e.get("updated") or ""
                t = (e.get("title") or "").strip()
                if t:
                    items.append({"title": t, "link": e.get("link", ""), "published": _to_dt(pub), "published_raw": pub})
            if getattr(feed, "bozo", 0) and not items:
                raise RuntimeError(str(getattr(feed, "bozo_exception", "empty feed"))[:60])
        elif fmt == "json":
            items = _fetch_json(src["url"], src.get("json", {}))
        elif fmt == "opec_html":
            items = _fetch_opec_html(src["url"])
        elif fmt == "ofac_delta":
            items = _fetch_ofac_delta(src["url"])
        elif fmt == "file":  # written by a watcher daemon (e.g. the UKMTO headless watcher)
            fp = Path(__file__).resolve().parent.parent / src["url"]
            d = json.loads(fp.read_text()) if fp.exists() else {"items": []}
            items = []
            for it in d.get("items", []):  # day-granular incidents: our first-seen time is the clock (if the incident is recent)
                inc_day = _to_dt(it["published"] + " +0530") if it.get("published") else None
                fs = _to_dt(it["first_seen"] + " +0530") if it.get("first_seen") else None
                recent_day = inc_day is not None and (dt.datetime.now() - inc_day).days <= 3
                items.append({"title": it["title"], "link": it.get("link", ""), "published": (fs if (fs and recent_day) else inc_day),
                              "published_raw": it.get("first_seen") or it.get("published") or ""})
        else:
            raise RuntimeError(f"unknown fmt {fmt}")
        _MEMO[src["name"]] = (time.time(), items)
        return items, None
    except Exception as e:
        _MEMO[src["name"]] = (time.time(), hit[1] if hit else [])  # back off a full poll interval
        return (hit[1] if hit else []), f"{type(e).__name__}: {str(e)[:60]}"


class FirstSeen:
    """Durable first-seen registry: headline key -> {source, seen, published} (bounded)."""
    def __init__(self, path: str | Path):
        self.path = Path(path); self.data = {}
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            pass

    def note(self, key: str, source: str, published: dt.datetime | None) -> bool:
        if key in self.data:
            return False
        self.data[key] = {"source": source, "seen": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "published": published.strftime("%Y-%m-%d %H:%M:%S") if published else None}
        return True

    def save(self):
        if len(self.data) > 4000:  # keep newest 3000
            self.data = dict(sorted(self.data.items(), key=lambda kv: kv[1]["seen"])[-3000:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))

    def scoreboard(self, hours: float = 24) -> list[dict]:
        """Per source: headlines seen first, median publish->seen lag (min)."""
        cutoff = (dt.datetime.now() - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        by: dict = {}
        for v in self.data.values():
            if v["seen"] < cutoff:
                continue
            d = by.setdefault(v["source"], {"first": 0, "lags": []})
            d["first"] += 1
            if v.get("published"):
                lag = (dt.datetime.strptime(v["seen"], "%Y-%m-%d %H:%M:%S") - dt.datetime.strptime(v["published"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
                if -5 <= lag <= 24 * 60:
                    d["lags"].append(lag)
        out = []
        for s, d in by.items():
            lags = sorted(d["lags"])
            out.append({"source": s, "first_seen": d["first"], "median_lag_min": round(lags[len(lags) // 2], 1) if lags else None})
        return sorted(out, key=lambda r: -r["first_seen"])


def fetch_all(first_seen: FirstSeen | None = None, sources: list[dict] | None = None,
              max_age_h: float = 6, force: bool = False) -> tuple[list[dict], dict]:
    """All sources -> deduped headlines (newest first) tagged with lane/trust/first source.
    Returns (items, status) where status[source] = {'n': entries, 'error': ..., 'fetched': ts}."""
    items, status, seen = {}, {}, set()
    now = dt.datetime.now()
    from concurrent.futures import ThreadPoolExecutor
    srcs = list(sources or SOURCES)
    with ThreadPoolExecutor(10) as ex:
        fetched = list(ex.map(lambda sr: fetch_source(sr, force=force), srcs))
    for src, (got, err) in zip(srcs, fetched):
        status[src["name"]] = {"n": len(got), "error": err, "lane": src["lane"]}
        for it in got:
            pub = it["published"]
            if pub and (now - pub).total_seconds() > max_age_h * 3600:
                continue
            k = _key(it["title"])
            if first_seen is not None:
                first_seen.note(k, src["name"], pub)
            if k in seen:
                continue
            seen.add(k)
            items[k] = {"title": it["title"], "link": it["link"], "source": src["name"], "lane": src["lane"],
                        "trust": src["trust"], "published": pub.strftime("%Y-%m-%d %H:%M") if pub else "",
                        "published_dt": pub, "first_source": (first_seen.data.get(k, {}).get("source") if first_seen else src["name"])}
    if first_seen is not None:
        first_seen.save()
    out = sorted(items.values(), key=lambda x: x["published_dt"] or dt.datetime.min, reverse=True)
    for o in out:
        o.pop("published_dt", None)
    return out, status


if __name__ == "__main__":  # self-check on the live registry
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        fs = FirstSeen(Path(d) / "first_seen.json")
        items, status = fetch_all(fs, force=True)
        live = {k: v for k, v in status.items() if not v["error"]}
        print(f"{len(items)} unique headlines from {len(live)}/{len(status)} live sources")
        for k, v in status.items():
            print(f"  {k:32s} {v['lane']:10s} n={v['n']:3d} {('ERR ' + v['error']) if v['error'] else ''}")
        assert items and len(live) >= 30, f"only {len(live)} live"
        assert fetch_all(fs)[0] and (Path(d) / "first_seen.json").exists()   # memo path + persisted first-seen
        print("scoreboard:", fs.scoreboard()[:3])
        print("newsfeeds self-check OK")
