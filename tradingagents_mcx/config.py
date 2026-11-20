"""Configuration loader.

Reads config.json (copy config.example.json), with environment-variable overrides.
The original repo used a Chinese-named JSON (期货TradingAgents系统_配置文件.json)
with DeepSeek + Serper keys; this keeps the same shape in English and adds the
free data-source keys (EIA, NewsAPI/GNews are optional).
"""
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
EXAMPLE_PATH = PROJECT_ROOT / "config.example.json"

_DEFAULTS = {
    "llm": {
        # Any OpenAI-compatible endpoint works: DeepSeek, OpenAI, Groq, OpenRouter, Ollama.
        "api_key": "",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "reasoning_model": "deepseek-reasoner",
        "max_tokens": 4000,
        "temperature": 0.1,
    },
    "data_keys": {
        "eia_api_key": "",       # free: https://www.eia.gov/opendata/register.php
        "alpha_vantage_key": "", # optional free: scored oil news sentiment (25 req/day)
        "newsapi_key": "",       # optional free tier: https://newsapi.org
        "gnews_key": "",         # optional free tier: https://gnews.io
        "serper_key": "",        # optional, 2500 free searches: https://serper.dev
    },
    "angel_one": {               # free broker API for real MCX candles/LTP
        "api_key": "", "client_id": "", "pin": "", "totp_secret": "",
    },
    "telegram": {                # decision push: @BotFather token + your chat id
        "bot_token": "", "chat_id": "",
    },
    "analysis": {
        "symbol": "CRUDEOIL",
        "lookback_days": 365,
        "news_days_back": 3,
        "max_news": 40,
        "debate_rounds": 2,
        "modules": ["technical", "fundamentals", "news", "positioning", "term_structure", "basis"],
    },
    "paths": {
        "cache_dir": str(PROJECT_ROOT / "cache"),
        "reports_dir": str(PROJECT_ROOT / "reports"),
    },
}

_ENV_MAP = {
    ("llm", "api_key"): "LLM_API_KEY",
    ("llm", "base_url"): "LLM_BASE_URL",
    ("llm", "model"): "LLM_MODEL",
    ("llm", "reasoning_model"): "LLM_REASONING_MODEL",
    ("data_keys", "eia_api_key"): "EIA_API_KEY",
    ("data_keys", "alpha_vantage_key"): "ALPHA_VANTAGE_KEY",
    ("data_keys", "newsapi_key"): "NEWSAPI_KEY",
    ("data_keys", "gnews_key"): "GNEWS_KEY",
    ("data_keys", "serper_key"): "SERPER_KEY",
    ("telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
    ("telegram", "chat_id"): "TELEGRAM_CHAT_ID",
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(_DEFAULTS))  # deep copy
    for path in (CONFIG_PATH, EXAMPLE_PATH):
        if path.exists():
            with open(path) as f:
                user = json.load(f)
            for section, values in user.items():
                if isinstance(values, dict):
                    cfg.setdefault(section, {}).update(values)
                else:
                    cfg[section] = values
            break
    # switchable LLM providers: llm.providers holds named profiles; llm.active_provider
    # picks one, whose fields overlay the flat llm block (base_url, api_key, models...)
    active = cfg["llm"].get("active_provider")
    prof = (cfg["llm"].get("providers") or {}).get(active)
    if prof:
        cfg["llm"].update({k: v for k, v in prof.items() if v not in (None, "")})
    for (section, key), env in _ENV_MAP.items():
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]
    Path(cfg["paths"]["cache_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["paths"]["reports_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg
