"""Radar — "anything happens, we know first". Source-agnostic detectors that run every
few minutes 24x7 (the news sentinel hosts the loop):

  price_shock     WTI (trades ~23h) or MCX moves >= threshold within a short window
  release_watch   scheduled data/events (EIA WPSR, API, OPEC, FOMC...) — pre-alert and
                  'released' detection when the EIA number actually changes
  headline_burst  unusual volume of fresh crude headlines vs baseline (before any LLM)
  official_item   any item from an 'official' lane source (EIA/OPEC/OFAC/UKMTO) is an event
                  by definition — no lexicon gate

Every detection is appended to events/radar_YYYYMMDD.jsonl and mirrored into the
dashboard event feed (events_YYYYMMDD.jsonl) with a sound where warranted. State is
durable so restarts never re-alert.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd

SEV = {"info": 0, "watch": 1, "alert": 2, "critical": 3}

# ---- scheduled releases (IST). EIA WPSR is Wed 20:00 IST (21:00 after US DST ends, Nov 1);
#      holiday weeks shift to Thursday. API (private inventories) Tue 02:00 IST (Wed early).
RELEASES = [
    {"name": "EIA Weekly Petroleum Status Report", "weekday": 2, "time": "20:00", "kind": "eia", "pre_min": 20},
    {"name": "API weekly crude inventories", "weekday": 1, "time": "02:00", "kind": "api", "pre_min": 15},
    {"name": "Baker Hughes rig count", "weekday": 4, "time": "22:30", "kind": "rigs", "pre_min": 10},
    {"name": "CFTC Commitments of Traders", "weekday": 4, "time": "01:00", "kind": "cot", "pre_min": 0},
]
ADHOC_RELEASES_FILE = "events/radar_calendar.json"  # [{name, when: 'YYYY-MM-DD HH:MM', kind, pre_min}] — OPEC meetings, FOMC, holiday-shifted EIA


class Radar:
    def __init__(self, root: str | Path, wti_shock_pct: float = 0.6, mcx_shock_pct: float = 0.5,
                 shock_window_min: int = 5, burst_multiple: float = 3.0):
        self.root = Path(root)
        self.events_dir = self.root / "events"; self.events_dir.mkdir(exist_ok=True)
        self.state_p = self.events_dir / "radar_state.json"
        self.state = self._load(self.state_p, {"alerted": {}, "last_eia": None, "burst_baseline": []})
        self.wti_shock_pct, self.mcx_shock_pct = wti_shock_pct, mcx_shock_pct
        self.shock_window_min, self.burst_multiple = shock_window_min, burst_multiple

    # ------------------------------------------------------------ plumbing
    @staticmethod
    def _load(p: Path, default):
        try:
            return json.loads(p.read_text())
        except Exception:
            return default

    def _save(self):
        al = self.state["alerted"]
        if len(al) > 2000:  # bounded
            self.state["alerted"] = dict(sorted(al.items(), key=lambda kv: kv[1])[-1500:])
        self.state_p.write_text(json.dumps(self.state))

    def _once(self, key: str, ttl_min: float = 720) -> bool:
        """True the first time `key` is seen within ttl_min (dedupe across restarts)."""
        now = time.time()
        last = self.state["alerted"].get(key)
        if last and now - last < ttl_min * 60:
            return False
        self.state["alerted"][key] = now
        return True

    def emit(self, kind: str, severity: str, msg: str, source: str = "radar", sound: str | None = None, extra: dict | None = None) -> dict:
        now = dt.datetime.now()
        ev = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "severity": severity, "msg": msg, "source": source, **(extra or {})}
        with open(self.events_dir / f"radar_{now:%Y%m%d}.jsonl", "a") as f:
            f.write(json.dumps(ev, default=str) + "\n")
        icon = {"info": "📡", "watch": "👀", "alert": "🚨", "critical": "🔥"}[severity]
        with open(self.events_dir / f"events_{now:%Y%m%d}.jsonl", "a") as f:  # dashboard feed
            f.write(json.dumps({"ts": now.strftime("%H:%M:%S"), "kind": "news_alert" if severity in ("alert", "critical") else "info",
                                "msg": f"{icon} {msg}", "icon": icon}) + "\n")
        if sound:
            (self.events_dir / "pending_sound.json").write_text(json.dumps({"kind": sound, "ts": time.time()}))
        return ev

    # ------------------------------------------------------------ detectors
    def price_shock(self, wti: pd.Series | None, mcx_ticks: pd.DataFrame | None) -> list[dict]:
        out = []
        now = dt.datetime.now()
        for label, series, thr in (("WTI", wti, self.wti_shock_pct), ("MCX", None if mcx_ticks is None or mcx_ticks.empty else mcx_ticks.set_index("ts")["ltp"], self.mcx_shock_pct)):
            if series is None or len(series) < 3:
                continue
            s = series.dropna()
            try:
                recent = s[s.index >= s.index[-1] - pd.Timedelta(minutes=self.shock_window_min)]
            except Exception:
                continue
            if len(recent) < 2:
                continue
            chg = float(recent.iloc[-1] / recent.iloc[0] - 1) * 100
            if abs(chg) >= thr:
                stamp = f"{label}:{now:%Y%m%d%H}:{int(now.minute // 15)}"  # one alert per 15-min bucket per instrument
                if self._once(stamp, ttl_min=20):
                    sev = "critical" if abs(chg) >= 2 * thr else "alert"
                    unit = f"${recent.iloc[-1]:.2f}" if label == "WTI" else f"₹{recent.iloc[-1]:,.0f}"
                    out.append(self.emit("price_shock", sev, f"{label} {'jumped' if chg > 0 else 'dropped'} {chg:+.2f}% in {self.shock_window_min} min → {unit}",
                                         source=label, sound="alert", extra={"pct": round(chg, 2), "price": float(recent.iloc[-1])}))
        return out

    def _upcoming(self, now: dt.datetime) -> list[tuple[dt.datetime, dict]]:
        items = []
        for r in RELEASES:
            for d in range(0, 8):
                day = now.date() + dt.timedelta(days=d)
                if day.weekday() != r["weekday"]:
                    continue
                hh, mm = map(int, r["time"].split(":"))
                items.append((dt.datetime.combine(day, dt.time(hh, mm)), r))
        for a in self._load(self.root / ADHOC_RELEASES_FILE, []):
            try:
                items.append((dt.datetime.strptime(a["when"], "%Y-%m-%d %H:%M"), {"name": a["name"], "kind": a.get("kind", "adhoc"), "pre_min": a.get("pre_min", 15)}))
            except Exception:
                continue
        return sorted((w, r) for w, r in items if w >= now - dt.timedelta(minutes=5))

    def release_watch(self, eia_snapshot: dict | None = None) -> list[dict]:
        out = []
        now = dt.datetime.now()
        for when, r in self._upcoming(now)[:6]:
            mins = (when - now).total_seconds() / 60
            if 0 < mins <= r.get("pre_min", 15) and self._once(f"pre:{r['name']}:{when:%Y%m%d%H%M}", ttl_min=600):
                out.append(self.emit("release_pre", "watch", f"{r['name']} in {mins:.0f} min ({when:%H:%M}) — no new positions into the print", source="calendar"))
            if -5 <= mins <= 0 and self._once(f"due:{r['name']}:{when:%Y%m%d%H%M}", ttl_min=600):
                out.append(self.emit("release_due", "alert", f"{r['name']} due now ({when:%H:%M})", source="calendar", sound="alert"))
        # EIA print detection: the weekly number changed -> released
        if eia_snapshot:
            k = eia_snapshot.get("us_crude_stocks_ex_spr_kbbl") or {}
            sig = f"{k.get('latest')}|{k.get('week_change')}"
            if k and self.state.get("last_eia") and sig != self.state["last_eia"]:
                out.append(self.emit("release_out", "critical", f"EIA crude stocks OUT: {k.get('latest'):,.0f} kbbl, weekly {k.get('week_change'):+,.0f} kbbl", source="EIA", sound="alert", extra={"wow": k.get("week_change")}))
            if k:
                self.state["last_eia"] = sig
        return out

    def headline_burst(self, fresh_count: int) -> list[dict]:
        base = self.state.setdefault("burst_baseline", [])
        base.append(fresh_count); del base[:-48]  # ~last 48 cycles
        if len(base) >= 12:
            med = sorted(base[:-1])[len(base[:-1]) // 2]
            if fresh_count >= max(6, self.burst_multiple * max(med, 1)) and self._once(f"burst:{dt.datetime.now():%Y%m%d%H}", ttl_min=60):
                return [self.emit("headline_burst", "alert", f"News burst: {fresh_count} fresh crude headlines this cycle vs typical {med} — something is happening", source="newsfeeds", sound="alert", extra={"count": fresh_count, "median": med})]
        return []

    def official_items(self, items: list[dict]) -> list[dict]:
        out = []
        for it in items:
            if it.get("lane") == "official" and self._once(f"official:{it['title'][:80].lower()}", ttl_min=1440):
                out.append(self.emit("official", "alert", f"{it['source']}: {it['title'][:140]}", source=it["source"], sound="alert", extra={"link": it.get("link", "")}))
        return out

    def tick(self, wti=None, mcx_ticks=None, eia=None, fresh_count: int | None = None, items: list[dict] | None = None) -> list[dict]:
        ev = []
        for fn, args in ((self.price_shock, (wti, mcx_ticks)), (self.release_watch, (eia,)),
                         (self.headline_burst, (fresh_count,) if fresh_count is not None else None),
                         (self.official_items, (items or [],))):
            if args is None:
                continue
            try:
                ev += fn(*args)
            except Exception as e:
                ev.append({"kind": "radar_error", "msg": f"{fn.__name__}: {str(e)[:80]}"})
        self._save()
        return ev

    def timeline(self, hours: float = 24) -> list[dict]:
        cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
        rows = []
        for d in (cutoff.date(), dt.date.today()):
            p = self.events_dir / f"radar_{d:%Y%m%d}.jsonl"
            if p.exists():
                for l in p.read_text().splitlines():
                    try:
                        r = json.loads(l)
                        if dt.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S") >= cutoff:
                            rows.append(r)
                    except Exception:
                        pass
        return sorted(rows, key=lambda r: r["ts"], reverse=True)

    def next_releases(self, n: int = 5) -> list[dict]:
        return [{"when": w.strftime("%a %d %b %H:%M"), "name": r["name"], "in_min": round((w - dt.datetime.now()).total_seconds() / 60)} for w, r in self._upcoming(dt.datetime.now())[:n]]


if __name__ == "__main__":  # self-check with synthetic data
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "events").mkdir()
        r = Radar(d)
        idx = pd.date_range(end=pd.Timestamp.now(), periods=6, freq="1min")
        wti = pd.Series([90, 90, 90, 90.2, 90.5, 91.0], index=idx)            # +1.1% in 5 min -> alert
        ev = r.price_shock(wti, None); assert ev and ev[0]["kind"] == "price_shock" and ev[0]["severity"] == "alert", ev
        assert r.price_shock(wti, None) == []                                   # deduped within the bucket
        r.state["last_eia"] = "428910.0|95.0"
        ev = r.release_watch({"us_crude_stocks_ex_spr_kbbl": {"latest": 425000.0, "week_change": -3910.0}})
        assert any(e["kind"] == "release_out" for e in ev), ev
        for _ in range(12): r.headline_burst(2)
        assert r.headline_burst(9) and not r.headline_burst(9)                  # burst fires once per hour
        assert r.official_items([{"lane": "official", "source": "OFAC", "title": "Treasury sanctions Iranian oil network", "link": ""}])
        assert len(r.timeline()) >= 4 and r.next_releases(2)
        print("radar self-check OK (price shock, EIA out, burst, official, timeline, calendar)")
