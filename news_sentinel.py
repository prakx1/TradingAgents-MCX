#!/usr/bin/env python3
"""News Sentinel — always-on, store-first news pipeline.

Runs 24x7. Every 3 min during market hours (Mon-Fri 08:30-23:30 IST), every 60 min
off-hours and weekends. Each cycle:
  1. FETCH  fresh crude headlines (RSS — public internet, no VPN/LLM needed) and append
            them to a durable inbox (events/news_inbox.jsonl) BEFORE anything else.
  2. SHOW   write events/news_latest.json so the dashboard always has live headlines,
            even with the LLM gateway down.
  3. PROCESS only if the LLM gateway is reachable: newsroom digest + materiality check
            over the whole unprocessed backlog. If the gateway is down (VPN), the backlog
            simply waits — nothing is dropped — and is processed in one catch-up pass
            when the connection returns.
  4. ACT    material + market open -> convene the Council (with cooldown);
            material + market closed -> alert now, hold for the 09:02 opening bell.

  nohup .venv/bin/python news_sentinel.py >> sentinel.log 2>&1 &
  python news_sentinel.py --selftest
"""
import datetime as dt
import glob
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from tradingagents_mcx.config import load_config
from tradingagents_mcx.live import llm_gateway_ok, news_pulse, get_wti_intraday, load_ticks
from tradingagents_mcx.radar import Radar

socket.setdefaulttimeout(25)
ROOT = Path(__file__).parent
EVENTS = ROOT / "events"
CFG = load_config()
INTERVAL = int(CFG["analysis"].get("sentinel_interval_min", 3)) * 60
INTERVAL_OFF = int(CFG["analysis"].get("sentinel_offhours_interval_min", 60)) * 60
COOLDOWN = int(CFG["analysis"].get("council_cooldown_min", 10)) * 60
RADAR_EVERY = int(CFG["analysis"].get("radar_interval_min", 5)) * 60
LAST_FRESH = {"n": 0, "items": []}

# durable state (all under events/)
SEEN_P = EVENTS / "sentinel_seen.json"          # headline hashes already fetched
INBOX_P = EVENTS / "news_inbox.jsonl"           # every fresh headline, append-only
PROCESSED_P = EVENTS / "news_processed.json"    # inbox ids the LLM has processed
STATE_P = EVENTS / "news_inbox_state.json"      # {llm_offline_since, queued, last_processed}
LATEST_P = EVENTS / "news_latest.json"          # LLM-free headline snapshot for the UI
DIGEST_P = EVENTS / "news_digest.json"


def market_open(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    return now.weekday() < 5 and dt.time(9, 0) <= now.time() <= dt.time(23, 30)


def scan_window(now: dt.datetime | None = None) -> bool:
    """Fast cadence window (pre-market included)."""
    now = now or dt.datetime.now()
    return now.weekday() < 5 and dt.time(8, 30) <= now.time() <= dt.time(23, 30)


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _h(title: str) -> str:
    return hashlib.md5(title.lower()[:80].encode()).hexdigest()[:12]


def _council_recent() -> float:
    files = glob.glob(str(ROOT / "reports" / "**" / "*.json"), recursive=True)
    return time.time() - max(Path(f).stat().st_mtime for f in files) if files else float("inf")


def _log_event(kind: str, msg: str, icon: str):
    EVENTS.mkdir(exist_ok=True)
    with open(EVENTS / f"events_{dt.date.today():%Y%m%d}.jsonl", "a") as f:
        f.write(json.dumps({"ts": dt.datetime.now().strftime("%H:%M:%S"), "kind": kind, "msg": msg, "icon": icon}) + "\n")


def _queue_sound(kind: str):
    (EVENTS / "pending_sound.json").write_text(json.dumps({"kind": kind, "ts": time.time()}))


# ------------------------------------------------------------------ inbox
def inbox_append(items: list[dict]) -> int:
    EVENTS.mkdir(exist_ok=True)
    with open(INBOX_P, "a") as f:
        for it in items:
            f.write(json.dumps({"id": _h(it["title"]), "fetched": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "title": it["title"], "source": it.get("source", ""), "published": it.get("published", ""),
                                "score": float(it.get("score", 0))}) + "\n")
    # bound the file: keep the newest 3000 lines
    try:
        lines = INBOX_P.read_text().splitlines()
        if len(lines) > 5000:
            INBOX_P.write_text("\n".join(lines[-3000:]) + "\n")
    except Exception:
        pass
    return len(items)


def inbox_backlog() -> list[dict]:
    done = set(_load_json(PROCESSED_P, []))
    if not INBOX_P.exists():
        return []
    out, seen = [], set()
    for l in INBOX_P.read_text().splitlines():
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r["id"] in done or r["id"] in seen:
            continue
        seen.add(r["id"]); out.append(r)
    return out


def inbox_mark_processed(ids: list[str]):
    done = _load_json(PROCESSED_P, [])
    done = (done + list(ids))[-3000:]
    PROCESSED_P.write_text(json.dumps(done))


def _set_state(**kw):
    st = _load_json(STATE_P, {})
    st.update(kw)
    STATE_P.write_text(json.dumps(st))


# ------------------------------------------------------------------ LLM steps
def check_material(items: list[dict], llm=None) -> dict | None:
    from tradingagents_mcx.llm import LLMClient
    llm = llm or LLMClient(CFG)
    top = sorted(items, key=lambda it: (-abs(it.get("score", 0)), it.get("fetched", "")))[:10]
    lines = "\n".join(f"- ({it['score']:+.2f}, {it.get('fetched', '')[11:16]}) {it['title']} [{it.get('source', '')}]" for it in top)
    out = llm.chat(
        "You are a crude-oil news desk editor. Judge ONLY materiality for crude PRICES "
        "in the next few hours. Supply shocks, OPEC decisions, war/shipping escalation, "
        "surprise inventory numbers, major demand news = material. Routine price recaps, "
        "analyst opinions, old news rehashed = NOT material. Be strict: most news is noise.",
        f"Crude headlines collected since the last check (lexicon direction scores and fetch time attached):\n{lines}\n\n"
        "Output EXACTLY:\nMATERIAL: YES|NO\nDIRECTION: BULLISH|BEARISH|MIXED\n"
        "REASON: <one sentence naming the specific headline that matters>",
        role="sentinel", max_tokens=1200)
    m = re.search(r"MATERIAL:\s*(YES|NO)", out, re.IGNORECASE)
    if m and m.group(1).upper() == "YES":
        d = re.search(r"DIRECTION:\s*(\w+)", out); r = re.search(r"REASON:\s*(.*)", out)
        return {"direction": d.group(1) if d else "?", "reason": (r.group(1).strip() if r else "")[:140]}
    return None


def _latest_eia_line() -> str:
    try:
        f = max(glob.glob(str(ROOT / "reports" / "**" / "*.json"), recursive=True))
        eia = (json.loads(Path(f).read_text())["data"].get("eia") or {})
        k = eia.get("us_crude_stocks_ex_spr_kbbl") or {}; cu = eia.get("cushing_stocks_kbbl") or {}
        if not k:
            return ""
        return (f"US commercial crude stocks {k.get('latest'):,.0f} kbbl, weekly change {k.get('week_change'):+,.0f} kbbl"
                + (f"; Cushing {cu.get('latest'):,.0f} ({cu.get('week_change'):+,.0f})" if cu else "") + ". (EIA reports weekly, Wednesdays.)")
    except Exception:
        return ""


def write_digest(items: list[dict], llm=None, catch_up_note: str = ""):
    """4-bullet luna newsroom digest (3 news + 1 inventory)."""
    from tradingagents_mcx.llm import LLMClient
    llm = llm or LLMClient(CFG)
    top = sorted(items, key=lambda it: (-abs(it.get("score", 0)), it.get("fetched", it.get("published", ""))))[:10]
    lines = "\n".join(f"- ({it['score']:+.2f}) {it['title']} [{it.get('source', '')}]" for it in top)
    out = llm.chat(
        "You are a crude-oil newsroom editor for an Indian MCX crude trader. Write EXACTLY 4 bullets: "
        "the first 3 summarize the current oil-news picture (most market-moving first); the 4th covers "
        "the latest EIA inventory data provided. Each bullet: one plain-language sentence, starting "
        "with 🟢 (bullish for crude), 🔴 (bearish) or ⚪ (neutral). No preamble, no extra text.",
        f"Headlines (lexicon direction scores attached):\n{lines}\n\n[LATEST EIA WEEKLY INVENTORY DATA]\n{_latest_eia_line() or 'not available this cycle'}",
        role="sentinel", max_tokens=1200)
    EVENTS.mkdir(exist_ok=True)
    DIGEST_P.write_text(json.dumps({"ts": time.time(), "when": dt.datetime.now().strftime("%H:%M"),
                                    "digest": out.strip(), "note": catch_up_note, "n_items": len(items)}))


def _digest_age() -> float:
    return time.time() - _load_json(DIGEST_P, {}).get("ts", 0)


# ------------------------------------------------------------------ cycle
def cycle(pulse_fn=news_pulse, gateway_fn=None, llm=None) -> str:
    now = dt.datetime.now()
    gateway_fn = gateway_fn or (lambda: llm_gateway_ok(CFG["llm"]["base_url"], ttl=0))
    # 1. FETCH + STORE (no LLM involved)
    pulse = pulse_fn(ttl=0)
    seen = set(_load_json(SEEN_P, []))
    fresh = [it for it in pulse["items"] if _h(it["title"]) not in seen]
    LAST_FRESH["n"], LAST_FRESH["items"] = len(fresh), pulse["items"]
    if fresh:
        inbox_append(fresh)
        seen |= {_h(it["title"]) for it in fresh}
        EVENTS.mkdir(exist_ok=True); SEEN_P.write_text(json.dumps(list(seen)[-3000:]))
    # 2. SHOW — LLM-free snapshot for the dashboard
    try:
        LATEST_P.write_text(json.dumps({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "score": pulse.get("score", 0),
                                        "items": pulse["items"][:20], "status": pulse.get("status", {}),
                                        "n_sources_live": pulse.get("n_sources_live", 0)}, default=str))
    except Exception:
        pass
    backlog = inbox_backlog()
    if not backlog:
        # keep the digest fresh on quiet stretches (every 30 min) when online
        if pulse["items"] and _digest_age() > 1800 and gateway_fn():
            try:
                write_digest(pulse["items"], llm)
            except Exception as e:
                return f"no fresh headlines · digest refresh failed: {str(e)[:60]}"
        return "no fresh headlines"
    # 3. PROCESS — only with the gateway up; otherwise queue and wait
    if not gateway_fn():
        st = _load_json(STATE_P, {})
        _set_state(llm_offline_since=st.get("llm_offline_since") or now.strftime("%Y-%m-%d %H:%M:%S"), queued=len(backlog))
        return f"{len(backlog)} headlines queued — LLM gateway offline (VPN?), will process when it returns"
    st = _load_json(STATE_P, {})
    offline_since = st.get("llm_offline_since")
    catch_up = f"catch-up: {len(backlog)} headlines collected since {offline_since[11:16]}" if offline_since else ""
    try:
        write_digest(backlog + [it for it in pulse["items"] if _h(it["title"]) not in {b["id"] for b in backlog}], llm, catch_up)
        strong = [it for it in backlog if abs(it["score"]) >= 0.4]
        cluster = [it for it in backlog if abs(it["score"]) >= 0.2]
        verdict = None
        if strong or len(cluster) >= 3:
            verdict = check_material(backlog, llm)
    except Exception as e:  # LLM died mid-way: keep the backlog, mark offline, retry next cycle
        _set_state(llm_offline_since=offline_since or now.strftime("%Y-%m-%d %H:%M:%S"), queued=len(backlog))
        return f"LLM error, {len(backlog)} headlines kept in queue: {str(e)[:80]}"
    inbox_mark_processed([b["id"] for b in backlog])
    _set_state(llm_offline_since=None, queued=0, last_processed=now.strftime("%Y-%m-%d %H:%M:%S"), last_batch=len(backlog))
    tag = f"{len(backlog)} processed" + (f" ({catch_up})" if catch_up else "")
    if not verdict:
        return f"{tag}, not material"
    # 4. ACT
    if not market_open(now):
        since = _council_recent()
        if since < COOLDOWN * 3:  # night desk already looked recently
            _log_event("news_alert", f"🚨 MATERIAL off-hours ({verdict['direction']}): {verdict['reason']} — night desk viewed {since/60:.0f}m ago; opening bell will act", "🚨")
            return f"{tag}, MATERIAL off-hours, night desk recent ({since/60:.0f}m) → held for opening bell"
        _log_event("news_alert", f"🚨 MATERIAL off-hours ({verdict['direction']}): {verdict['reason']} — NIGHT DESK convening (view only; the 09:02 bell acts)", "🚨")
        _queue_sound("alert")
        subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--watch"], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"{tag}, MATERIAL off-hours → NIGHT DESK run triggered: {verdict['reason'][:60]}"
    since = _council_recent()
    if since < COOLDOWN:
        _log_event("news_alert", f"🚨 MATERIAL ({verdict['direction']}): {verdict['reason']} — Council ran {since/60:.0f}m ago, holding", "🚨")
        return f"{tag}, material but council cooldown"
    _log_event("news_alert", f"🚨 MATERIAL ({verdict['direction']}): {verdict['reason']} — CONVENING COUNCIL NOW", "🚨")
    _queue_sound("alert")
    subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"{tag}, TRIGGERED council: {verdict['reason']}"


def radar_tick(radar: Radar) -> str:
    """Price shocks (WTI ~23h + MCX), release calendar/EIA-out, headline bursts, official items."""
    try:
        wti = get_wti_intraday()
    except Exception:
        wti = None
    try:
        ticks = load_ticks(CFG["paths"]["cache_dir"])
    except Exception:
        ticks = None
    eia = None
    try:  # live EIA only around the scheduled print; otherwise the newest Council report's snapshot
        nxt = radar.next_releases(3)
        near = any(r["name"].startswith("EIA") and -40 <= r["in_min"] <= 5 for r in nxt)
        if near:
            from tradingagents_mcx import data_providers as dp
            eia = dp.get_eia_fundamentals(CFG["data_keys"].get("eia_api_key", ""))
        else:
            f = max(glob.glob(str(ROOT / "reports" / "**" / "*.json"), recursive=True), default=None)
            eia = json.loads(Path(f).read_text())["data"].get("eia") if f else None
    except Exception:
        pass
    ev = radar.tick(wti=wti, mcx_ticks=ticks, eia=eia, fresh_count=LAST_FRESH["n"], items=LAST_FRESH["items"])
    return f"radar: {len(ev)} event(s)" + ("".join(f" · {e.get('kind')}" for e in ev) if ev else "")


def main():
    print(f"[sentinel] started {dt.datetime.now():%H:%M:%S} · always on: news every {INTERVAL//60}m in market hours, "
          f"{INTERVAL_OFF//60}m off-hours · radar every {RADAR_EVERY//60}m 24x7 · council cooldown {COOLDOWN//60}m · store-first, LLM optional", flush=True)
    radar = Radar(ROOT)
    next_news = 0.0
    next_radar = 0.0
    while True:
        now = dt.datetime.now(); t = time.time()
        if t >= next_news:
            try:
                print(f"{now:%H:%M:%S} {cycle()}", flush=True)
            except Exception as e:
                print(f"{now:%H:%M:%S} error: {e}", flush=True)
            next_news = t + (INTERVAL if scan_window() else INTERVAL_OFF)
        if t >= next_radar:
            try:
                r = radar_tick(radar)
                if not r.endswith("0 event(s)"):
                    print(f"{now:%H:%M:%S} {r}", flush=True)
            except Exception as e:
                print(f"{now:%H:%M:%S} radar error: {e}", flush=True)
            next_radar = t + RADAR_EVERY
        time.sleep(15)  # short, App Nap-safe


def _selftest():
    import tempfile
    global EVENTS, SEEN_P, INBOX_P, PROCESSED_P, STATE_P, LATEST_P, DIGEST_P
    with tempfile.TemporaryDirectory() as d:
        EVENTS = Path(d); SEEN_P = EVENTS / "seen.json"; INBOX_P = EVENTS / "inbox.jsonl"; PROCESSED_P = EVENTS / "proc.json"
        STATE_P = EVENTS / "state.json"; LATEST_P = EVENTS / "latest.json"; DIGEST_P = EVENTS / "digest.json"
        items = [{"title": "Tankers attacked in Strait of Hormuz, supply disruption feared", "source": "x", "published": "", "score": 0.8},
                 {"title": "Crude futures edge higher in quiet trade", "source": "y", "published": "", "score": 0.3}]
        pulse = lambda ttl=0: {"score": 0.5, "items": items}
        class Stub:
            def chat(self, s, u, **k):
                return "MATERIAL: YES\nDIRECTION: BULLISH\nREASON: Hormuz attack" if "MATERIAL" in u else "- 🟢 a\n- 🟢 b\n- ⚪ c\n- ⚪ d"
        # offline: fetched + stored, nothing processed, nothing lost
        r1 = cycle(pulse, gateway_fn=lambda: False, llm=Stub())
        assert "queued" in r1 and len(inbox_backlog()) == 2 and json.loads(STATE_P.read_text())["llm_offline_since"], r1
        assert json.loads(LATEST_P.read_text())["items"][0]["title"].startswith("Tankers")   # UI snapshot written offline
        r2 = cycle(pulse, gateway_fn=lambda: False, llm=Stub())                                # same headlines: no duplicates
        assert len(inbox_backlog()) == 2, r2
        # back online: one catch-up pass processes the backlog, digest notes the catch-up, verdict held (off-hours in test)
        r3 = cycle(pulse, gateway_fn=lambda: True, llm=Stub())
        assert "2 processed" in r3 and "catch-up" in r3 and len(inbox_backlog()) == 0, r3
        dg = json.loads(DIGEST_P.read_text()); assert "catch-up" in dg["note"] and dg["digest"].startswith("- 🟢")
        assert json.loads(STATE_P.read_text())["llm_offline_since"] is None
        print("sentinel self-test OK: store-first, offline queue, no duplicates, catch-up processing, LLM-free UI snapshot")


if __name__ == "__main__":
    _selftest() if "--selftest" in sys.argv else main()
