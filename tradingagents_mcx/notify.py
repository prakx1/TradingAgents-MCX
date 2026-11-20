"""Telegram notifications.

Setup (2 minutes, free):
1. Message @BotFather on Telegram -> /newbot -> copy the bot token
2. Put it in config.json under "telegram": {"bot_token": "..."}
3. Send your bot any message, then run: python main.py chatid
   -> it prints your chat_id; put that in config.json too
"""
from __future__ import annotations

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def send_telegram(cfg: dict, text: str) -> bool:
    tg = cfg.get("telegram", {})
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if not (token and chat_id):
        return False
    try:
        r = requests.post(API.format(token=token, method="sendMessage"),
                          json={"chat_id": chat_id, "text": text[:4000]}, timeout=20)
        return bool(r.json().get("ok"))
    except Exception:
        return False


def discover_chat_id(cfg: dict) -> str | None:
    """After the user messages the bot once, getUpdates reveals their chat id."""
    token = cfg.get("telegram", {}).get("bot_token")
    if not token:
        return None
    try:
        updates = requests.get(API.format(token=token, method="getUpdates"), timeout=20).json()
        for u in reversed(updates.get("result", [])):
            chat = (u.get("message") or u.get("edited_message") or {}).get("chat")
            if chat:
                return str(chat["id"])
    except Exception:
        pass
    return None


def format_decision(result: dict) -> str:
    t, c = result["trader"], result["cio"]
    tally = t["module_tally"]
    signal = result.get("signal") or {}
    lines = [
        f"🛢️ {result['contract']} — {result['analysis_date']}",
        f"DECISION: {c['final_decision']} | {signal.get('status', 'view only')}",
        f"Reward/risk: {signal.get('reward_risk', '—')} | {signal.get('reason', '')}",
        f"Strategy: {result.get('strategy', 'long_term')} | Direction: {t['direction']} ({t['confidence']})",
        f"Entry: {t['entry']} | Stop: {t['stop']} | Target: {t['target']}",
        f"Votes 🐂{tally['BULLISH']} 🐻{tally['BEARISH']} ⚪{tally['NEUTRAL']} | "
        f"Debate: {result['debate']['winner']} {result['debate']['bull_score']}-{result['debate']['bear_score']}",
        f"WTI ${result['data']['wti']} | USDINR {result['data']['usdinr']:.2f} | "
        f"Fair ₹{result['data']['mcx_fair_value_inr']}",
        "— Educational analysis, not investment advice.",
    ]
    return "\n".join(lines)
