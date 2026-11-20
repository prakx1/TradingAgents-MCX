"""Council Intelligence Briefing — a self-describing data pack for the AI committee.

Nothing reaches the analysts as a raw dump. Every data family arrives under a CARD that
tells the model WHAT it is, WHERE it comes from, HOW FRESH and HOW TRUSTWORTHY it is,
its timestamp precision, how to use it and its known caveats — followed by READING RULES.
The same pack is saved in machine-readable form with each report, so the dashboard can
show exactly what the Council was told.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

# ---- data-family glossary: the words the model reads before any item
LANE_CARDS = {
    "official": {
        "label": "OFFICIAL / PRIMARY SOURCES",
        "what": "Statements and records published by the institutions themselves: EIA (US energy statistics), OPEC secretariat press releases, "
                "US Treasury OFAC sanctions designations, UKMTO maritime-incident reports, US CENTCOM / 5th Fleet and Combined Maritime Forces releases.",
        "trust": "Highest. These are facts at the source, not reporting about facts. One official item outweighs many headlines repeating it.",
        "freshness": "Polled every 3-30 min. OFAC and OPEC items surface within minutes of publication; EIA press items are scheduled releases.",
        "use": "Treat as ground truth for what happened. Assess market impact yourself — an official release is not a price forecast.",
        "caveats": "Official releases carry no opinion; do not infer direction from their tone. UKMTO dates are day-granular (see maritime card).",
    },
    "wire": {
        "label": "WIRE-GRADE HEADLINES",
        "what": "Newsroom feeds of Bloomberg, Reuters (via its Bluesky account), CNBC, FT, WSJ, MarketWatch, investingLive, Investing.com, OilPrice, Rigzone.",
        "trust": "High for facts, medium for framing. Editorial desks with correction policies; headlines can still front-run confirmation.",
        "freshness": "Typically 2-15 min behind publication. Fetch time is when we saw it; the stamp shown is the outlet's own publish time (converted to IST).",
        "use": "Primary evidence of what the market is reacting to right now. Prefer the earliest-seen version of a story.",
        "caveats": "A price quoted inside a headline is historical — it was true when the headline was written, not now. Multiple outlets carrying one story is ONE piece of evidence, not several.",
    },
    "regional": {
        "label": "REGIONAL, GEOPOLITICAL AND SHIPPING OUTLETS",
        "what": "Middle-East and Gulf outlets (Iran International, Al Jazeera, Arab News, The National, Haaretz, Anadolu, TASS, BBC/Guardian Middle East), "
                "shipping trade press (TradeWinds, Splash247, Seatrade, MarineLink, gCaptain, Hellenic, Maritime Executive, Dryad advisories) and Indian commodity desks.",
        "trust": "Medium. Often earliest on Gulf and tanker events; some are state-linked (TASS, Anadolu, Iran International) — read their framing as a position, not a fact.",
        "freshness": "3-30 min polling; publication lag varies widely.",
        "use": "Early warning on supply-route and geopolitical risk; corroborate with an official or wire item before treating as confirmed.",
        "caveats": "Non-oil regional news is filtered by keyword, so some items are only tangentially about crude. Weigh by relevance, not by count.",
    },
    "aggregator": {
        "label": "AGGREGATORS",
        "what": "Google News search feeds (US 1h and India 2h windows) and GDELT. They re-surface stories from thousands of outlets.",
        "trust": "Inherits the underlying outlet's trust; the aggregator adds no verification.",
        "freshness": "Slowest lane: Google adds roughly 5-60 minutes of aggregation delay on top of the outlet's own timing.",
        "use": "Coverage backstop — catches outlets we do not poll directly. Never the first place a story appears.",
        "caveats": "Timestamps come from Google and may be GMT; duplicates of wire items are common and are already de-duplicated here.",
    },
    "social": {
        "label": "SOCIAL / UNVERIFIED",
        "what": "Reddit (r/oil, r/energy, r/commodities) and ZeroHedge.",
        "trust": "Low. Fast and opinionated; frequently wrong, sometimes early. Never treat as confirmation.",
        "freshness": "Minutes.",
        "use": "Sentiment and rumour radar only. A social item may justify checking official/wire lanes, never a position on its own.",
        "caveats": "Headline text often editorialises; discount the language, keep only verifiable claims.",
    },
}

FAMILY_CARDS = {
    "maritime": {
        "label": "MARITIME INCIDENTS — UKMTO",
        "what": "Incident reports from the UK Maritime Trade Operations centre, the primary authority merchant vessels report to in the Gulf, Red Sea and Indian Ocean. "
                "Types: Attack (projectile/drone/boarding), Suspicious Approach, Advisory/Warning. Rendered from ukmto.org every 5 minutes.",
        "trust": "Highest for the fact that an incident occurred. Details (crew status, damage) are as reported by the vessel and may be updated later.",
        "freshness": "Dates are DAY-granular. 'first seen HH:MM' is when our watcher first saw the report — treat that as the effective timestamp. 'time-late' means the incident happened earlier than the report.",
        "use": "Direct evidence of supply-route risk (Strait of Hormuz, Red Sea, Bab al-Mandab). Escalating frequency or attacks on tankers is a supply-risk premium signal; a lull is a fading one.",
        "caveats": "A report does not state cargo, flag or outcome for the oil market; do not invent them. Count incidents, do not extrapolate a barrel impact.",
    },
    "radar": {
        "label": "DESK RADAR EVENTS (last 24h)",
        "what": "Deterministic detectors run by our own system every 5 minutes: price_shock (WTI move >= 0.6% or MCX >= 0.5% within 5 min), release_pre/release_due/release_out "
                "(scheduled data prints: EIA WPSR Wed 20:00 IST, API inventories, Baker Hughes, CFTC), headline_burst (fresh-headline count >= 3x its baseline), official (new item on an official feed), maritime_incident.",
        "trust": "These are measurements of OUR feeds, not news. A price shock is a fact about the tape; a headline burst is a fact about news volume (something is happening), not about direction.",
        "freshness": "Timestamps are exact (IST).",
        "use": "Context for regime and urgency. A burst plus an official item plus a price shock in the same hour is a live event; a lone burst may be noise.",
        "caveats": "Severity is mechanical (critical/alert/watch/info). No detector knows why a move happened — that is your job.",
    },
    "digest": {
        "label": "NEWSROOM DIGEST (our own summary — NOT a source)",
        "what": "A 4-bullet summary written by the desk's own cheap model every few minutes from the same headline stream you are reading, with the latest EIA numbers appended.",
        "trust": "Low as evidence: it is a model's paraphrase and may mislabel direction. Useful only as an orientation of what the stream has been about.",
        "freshness": "Refreshed every 3-30 min.",
        "use": "Skim for themes, then verify against the items themselves. Never cite the digest as a fact.",
        "caveats": "Because it is generated from the same items, it is not independent corroboration of anything below.",
    },
}

READING_RULES = """READING RULES
1. Weigh by lane, not by volume: one OFFICIAL item > several wire items > many regional/social items on the same point.
2. The same story carried by several outlets is ONE piece of evidence (items are already de-duplicated; near-duplicates that remain are the same event).
3. Every item shows the outlet's own publish stamp in IST; 'first seen via' names which of our feeds delivered it first. Older items are context, not news.
4. Prices inside headlines are historical quotes — the live price is in the market data you also receive. Never present a headline price as current.
5. SOCIAL items are never confirmation. REGIONAL state-linked outlets show a position; read their framing as such.
6. Cite items by their [n] id so your reasoning can be audited. Say explicitly when the evidence is thin or one-sided."""


def _ist(ts) -> str:
    return str(ts or "")[:16]


def build_briefing(news: list[dict], maritime: list[dict], radar: list[dict], digest: str,
                   registry: list[dict], now: dt.datetime | None = None) -> dict:
    """Returns {'text': prompt block, 'json': machine-readable pack, 'summary': 3-line brief for trader/CIO}."""
    now = now or dt.datetime.now()
    by_lane: dict = {}
    for i, n in enumerate(news, 1):
        by_lane.setdefault(n.get("lane", "wire"), []).append((i, n))
    live_by_lane = Counter(s["lane"] for s in registry)
    parts = [f"=== COUNCIL INTELLIGENCE BRIEFING · compiled {now:%Y-%m-%d %H:%M} IST ===",
             f"Coverage: {len(registry)} registered feeds ({', '.join(f'{k} {v}' for k, v in live_by_lane.items())}). "
             f"This pack holds {len(news)} de-duplicated items, {len(maritime)} maritime incidents (3 days), {len(radar)} radar events (24h).",
             "", READING_RULES, ""]
    order = ["official", "wire", "regional", "aggregator", "social"]
    for lane in order:
        items = by_lane.get(lane, [])
        card = LANE_CARDS[lane]
        parts.append(f"--- {card['label']} · {len(items)} item(s) ---")
        parts.append(f"WHAT: {card['what']}")
        parts.append(f"TRUST: {card['trust']}")
        parts.append(f"FRESHNESS: {card['freshness']}")
        parts.append(f"USE: {card['use']}")
        parts.append(f"CAVEATS: {card['caveats']}")
        if items:
            for i, n in items:
                src = n.get("source", "")
                first = n.get("first_source")
                parts.append(f"[{i}] {_ist(n.get('published'))} IST · {src}{' (first seen via ' + first + ')' if first and first != src else ''} · trust {n.get('trust', 0.5):.2f} · relevance {n.get('score', 0)} · {n['title']}")
        else:
            parts.append("(no items in this lane this run)")
        parts.append("")
    c = FAMILY_CARDS["maritime"]
    parts += [f"--- {c['label']} · {len(maritime)} report(s) in the last 3 days ---", f"WHAT: {c['what']}", f"TRUST: {c['trust']}",
              f"FRESHNESS: {c['freshness']}", f"USE: {c['use']}", f"CAVEATS: {c['caveats']}"]
    for m in maritime[:8]:
        parts.append(f"- UKMTO #{m.get('num')} {m.get('kind')} · date {str(m.get('published',''))[:10]} · first seen {str(m.get('first_seen',''))[11:16] or 'n/a'} · {m.get('place') or 'location not stated'} · {(m.get('desc') or '')[:240]}")
    if not maritime:
        parts.append("(no incidents reported in the last 3 days)")
    parts.append("")
    c = FAMILY_CARDS["radar"]
    parts += [f"--- {c['label']} · {len(radar)} event(s) ---", f"WHAT: {c['what']}", f"TRUST: {c['trust']}", f"FRESHNESS: {c['freshness']}", f"USE: {c['use']}", f"CAVEATS: {c['caveats']}"]
    for r in radar[:12]:
        parts.append(f"- {str(r.get('ts',''))[11:16]} IST · {r.get('severity','')} · {r.get('kind','')} · {r.get('source','')} · {(r.get('msg') or '')[:180]}")
    if not radar:
        parts.append("(no detector events in the last 24h)")
    parts.append("")
    c = FAMILY_CARDS["digest"]
    parts += [f"--- {c['label']} ---", f"WHAT: {c['what']}", f"TRUST: {c['trust']}", f"USE: {c['use']}", f"CAVEATS: {c['caveats']}", (digest or "(no digest available)")[:900], ""]
    text = "\n".join(parts)
    official_titles = [n["title"][:120] for _, n in by_lane.get("official", [])]
    summary = (f"DESK BRIEF: {len(news)} items across {len(by_lane)} lanes; official items: {len(official_titles)}"
               + (f" — {official_titles[0]}" if official_titles else "")
               + f"; maritime incidents (3d): {len(maritime)}"
               + (f", newest {maritime[0].get('kind')} {maritime[0].get('place') or ''} {str(maritime[0].get('published',''))[:10]}" if maritime else "")
               + f"; radar events (24h): {len(radar)}"
               + (f" — latest {radar[0].get('kind')}: {(radar[0].get('msg') or '')[:90]}" if radar else "") + ".")
    js = {"compiled": now.strftime("%Y-%m-%d %H:%M:%S"), "coverage": dict(live_by_lane), "counts": {k: len(v) for k, v in by_lane.items()},
          "official_items": official_titles, "maritime": [{k: m.get(k) for k in ("num", "kind", "published", "first_seen", "place")} for m in maritime[:8]],
          "radar": [{k: r.get(k) for k in ("ts", "severity", "kind", "source", "msg")} for r in radar[:12]],
          "digest_present": bool(digest), "reading_rules": READING_RULES, "lane_cards": {k: v["label"] for k, v in LANE_CARDS.items()}}
    return {"text": text, "json": js, "summary": summary}


if __name__ == "__main__":
    news = [{"title": "UKMTO #140 Attack — Strait of Hormuz: tanker struck", "source": "UKMTO incidents", "lane": "official", "trust": 1.0, "score": 3.0, "published": "2026-09-21 18:56"},
            {"title": "Oil falls as supply fears ease", "source": "Bloomberg Markets", "lane": "wire", "trust": 0.8, "score": 4.0, "published": "2026-09-21 18:40", "first_source": "Bloomberg (Bluesky)"},
            {"title": "Iran warns of retaliation", "source": "Iran International", "lane": "regional", "trust": 0.65, "score": 2.5, "published": "2026-09-21 18:10"},
            {"title": "Crude going to zero lol", "source": "Reddit r/oil", "lane": "social", "trust": 0.35, "score": 1.0, "published": "2026-09-21 18:00"}]
    mar = [{"num": 140, "kind": "Attack", "published": "2026-09-21 00:00:00", "first_seen": "2026-09-21 18:56:26", "place": "Strait of Hormuz", "desc": "tanker struck by projectile"}]
    rad = [{"ts": "2026-09-21 19:08:01", "severity": "alert", "kind": "official", "source": "UKMTO", "msg": "UKMTO #140 Attack"}]
    reg = [{"lane": "official"}] * 8 + [{"lane": "wire"}] * 16
    b = build_briefing(news, mar, rad, "- 🟢 a\n- 🔴 b", reg)
    t = b["text"]
    for must in ("READING RULES", "OFFICIAL / PRIMARY SOURCES", "[1] 2026-09-21 18:56 IST · UKMTO incidents", "first seen via Bloomberg (Bluesky)",
                 "SOCIAL / UNVERIFIED", "MARITIME INCIDENTS — UKMTO", "DESK RADAR EVENTS", "NOT a source", "(no items in this lane this run)"):
        assert must in t, must
    assert b["json"]["counts"] == {"official": 1, "wire": 1, "regional": 1, "social": 1} and "official items: 1" in b["summary"]
    print("briefing self-check OK ·", len(t), "chars · summary:", b["summary"][:120])
