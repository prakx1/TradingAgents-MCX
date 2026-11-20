"""LLM client — OpenAI-compatible chat completions with per-role routing across providers.

llm.routes maps a role to "provider:model": decision roles (trader, cio, deep) go to the strongest
model, bulk roles (analysts, debate, sentinel) to the cheapest. Any OpenAI-compatible endpoint works
(OpenAI, a LiteLLM gateway, DeepSeek, Groq, Ollama...). When a role's provider is unreachable or keeps
failing, the call fails over along llm.fallback_providers using THAT provider's own model for the role.
"""
import datetime as _dt
import json as _json
import time
from pathlib import Path

from openai import OpenAI, APIConnectionError, APITimeoutError, AuthenticationError, PermissionDeniedError, NotFoundError


class GatewayDown(RuntimeError):
    """The LLM endpoint is unreachable (VPN-gated gateway dark): callers abort, they do not retry."""


# $ per 1M tokens (input, output) — for the live cost meter
PRICES = {  # standard tier, developers.openai.com/api/docs/pricing as of 2026-09-29
    "gpt-6-astra": (10.0, 50.0), "(paid) gpt-6-astra": (10.0, 50.0), "gpt-6-sol": (2.0, 10.0), "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-sol": (4.0, 20.0), "gpt-5.6-terra": (2.0, 12.0), "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5-mini": (0.25, 2.0), "gpt-5": (1.25, 10.0),
}


def _split(spec: str, default_provider: str) -> tuple[str, str]:
    """'provider:model' -> (provider, model); a bare model name belongs to the default provider."""
    if spec and ":" in spec:
        p, m = spec.split(":", 1)
        return p, m
    return default_provider, spec


class LLMClient:
    def __init__(self, cfg: dict, override_model: str | None = None):
        llm = cfg["llm"]
        self.provider = llm.get("active_provider") or "default"  # default provider for unprefixed model names
        self._providers = {k: dict(v) for k, v in (llm.get("providers") or {}).items()}
        # the flat block is always a usable profile (single-provider configs, env-var overrides)
        self._providers.setdefault(self.provider, {"base_url": llm.get("base_url"), "api_key": llm.get("api_key"),
                                                   "models": llm.get("models") or {}})
        if not any(p.get("api_key") for p in self._providers.values()):
            raise RuntimeError("No LLM API key configured. Copy config.example.json to config.json "
                               "and set llm.api_key, or export LLM_API_KEY.")
        self.routes = llm.get("routes") or {}  # role -> "provider:model"
        self.model = llm["model"]
        self.reasoning_model = llm.get("reasoning_model") or llm["model"]
        self.role_models = llm.get("models") or {}  # per-role model map of the default provider
        self.override_model = override_model  # deep-tier runs force one model (spec) for every role
        self.max_tokens = llm.get("max_tokens", 4000)
        self.temperature = llm.get("temperature", 0.1)
        self._fallbacks = list(llm.get("fallback_providers") or [])  # ponytail: in-order list, no health scoring
        self._timeout = float(llm.get("timeout_s", 120))
        self._clients: dict = {}
        self._no_temperature: set = set()  # models that reject a custom temperature (gpt-6 family, o-series)
        self._mct: set = set()  # models that want max_completion_tokens instead of max_tokens
        self._down_until: dict = {}  # provider -> epoch until which it is skipped (dead key, dark endpoint)
        try:
            self.usage_path = Path(cfg["paths"]["cache_dir"]) / "llm_usage.jsonl"
        except Exception:
            self.usage_path = None

    # ------------------------------------------------------------ routing
    def _client(self, name: str) -> OpenAI:
        if name not in self._clients:
            prof = self._providers.get(name) or {}
            if not prof.get("api_key"):
                raise RuntimeError(f"LLM provider '{name}' is not configured (no api_key)")
            # our loop retries; a dead gateway must fail in seconds, not hang on the SDK's 10-min default x 2 retries
            self._clients[name] = OpenAI(api_key=prof["api_key"], base_url=prof.get("base_url") or None,
                                         timeout=self._timeout, max_retries=0)
        return self._clients[name]

    def attempts(self, role: str | None, reasoning: bool = False) -> list[tuple[str, str]]:
        """[(provider, model), ...]: the routed pair first, then each fallback provider with ITS model for the role."""
        if self.override_model:
            spec = self.override_model
        elif role and self.routes.get(role):
            spec = self.routes[role]
        elif role and self.role_models.get(role):
            spec = self.role_models[role]
        else:
            spec = self.reasoning_model if reasoning else self.model
        prov, model = _split(spec, self.provider)
        out = [(prov, model)]
        for fb in self._fallbacks:
            if fb == prov or fb not in self._providers or any(p == fb for p, _ in out):
                continue
            fm = self._providers[fb].get("models") or {}
            out.append((fb, fm.get("deep" if self.override_model else role) or model))
        now = time.time()
        live = [a for a in out if self._down_until.get(a[0], 0) <= now]  # skip providers known to be down right now
        return live or out

    # -------------------------------------------------------------- usage
    def _log_usage(self, provider: str, model: str, role: str | None, usage) -> None:
        if not (self.usage_path and usage):
            return
        try:
            pin, pout = PRICES.get(model, (0.0, 0.0))
            usd = (usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1e6
            with open(self.usage_path, "a") as f:
                f.write(_json.dumps({"ts": _dt.datetime.now().isoformat(timespec="seconds"),
                                     "model": model, "role": role or "-", "provider": provider,
                                     "in": usage.prompt_tokens, "out": usage.completion_tokens,
                                     "usd": round(usd, 5)}) + "\n")
        except Exception:
            pass

    # --------------------------------------------------------------- chat
    def chat(self, system: str, user: str, reasoning: bool = False,
             max_tokens: int | None = None, retries: int = 3,
             role: str | None = None) -> str:
        """One chat completion for a role; fails over provider by provider when an endpoint is dead or keeps failing."""
        attempts = self.attempts(role, reasoning)
        last: Exception | None = None
        for i, (prov, model) in enumerate(attempts):
            try:
                return self._chat_once(prov, model, system, user, max_tokens, retries, role)
            except RuntimeError as e:  # GatewayDown or 'failed after N attempts'
                last = e
                if i + 1 < len(attempts):
                    nxt = attempts[i + 1]
                    print(f"    ↪ {prov}:{model} unavailable ({str(e)[:70]}) — failing over to {nxt[0]}:{nxt[1]}", flush=True)
                    if getattr(self, 'progress', None):
                        self.progress(f'Provider {prov} unavailable; using {nxt[0]} for {role or "analysis"}.')
        raise last  # type: ignore[misc]

    def _chat_once(self, prov: str, model: str, system: str, user: str,
                   max_tokens: int | None, retries: int, role: str | None) -> str:
        client = self._client(prov)
        base_url = (self._providers.get(prov) or {}).get("base_url") or ""
        budget_key = "max_completion_tokens" if model in self._mct else "max_tokens"
        kwargs = {budget_key: max_tokens or self.max_tokens}
        if model not in self._no_temperature:
            kwargs["temperature"] = self.temperature
        last_err = None
        for attempt in range(retries + 2):  # +2 headroom for budget escalations
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    **kwargs,
                )
                content = (resp.choices[0].message.content or "").strip()
                if content:
                    self._log_usage(prov, model, role, getattr(resp, "usage", None))
                    return content
                # Reasoning models can burn the whole completion budget on hidden reasoning
                # and return empty content — escalate the budget.
                bk = "max_completion_tokens" if "max_completion_tokens" in kwargs else "max_tokens"
                kwargs[bk] = min(kwargs[bk] * 2, 32000)
                last_err = RuntimeError(f"empty content, escalating budget to {kwargs[bk]}")
                continue
            except (APIConnectionError, APITimeoutError) as e:  # transport failure: is the endpoint even there?
                from .live import llm_gateway_ok
                if base_url and not llm_gateway_ok(base_url, ttl=0):
                    self._down_until[prov] = time.time() + 300
                    raise GatewayDown(f"LLM endpoint unreachable at {base_url} — VPN down?") from e
                last_err = e
                time.sleep(2 * (attempt + 1))
            except (AuthenticationError, PermissionDeniedError, NotFoundError) as e:  # a bad key or unknown model never heals by retrying
                self._down_until[prov] = time.time() + 600
                raise RuntimeError(f"{prov} rejected the call ({type(e).__name__}: {str(e)[:90]})") from e
            except Exception as e:  # ponytail: blanket retry, provider errors vary too much to enumerate
                msg = str(e)
                # parameter-shape differences are remembered per model: one 400, never again
                if "max_tokens" in msg and "max_completion_tokens" in msg and "max_tokens" in kwargs:
                    kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                    self._mct.add(model)
                    continue
                if "temperature" in msg and "unsupported" in msg.lower() and "temperature" in kwargs:
                    kwargs.pop("temperature")
                    self._no_temperature.add(model)
                    continue
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"LLM call failed after {retries} attempts on {prov}:{model}: {last_err}")


if __name__ == "__main__":
    cfg = {"llm": {"api_key": "x", "base_url": "https://a.example", "model": "m0", "active_provider": "openai",
                   "fallback_providers": ["pm", "openai"],
                   "routes": {"analyst": "pm:gpt-6-luna", "cio": "openai:gpt-6-astra"},
                   "providers": {"openai": {"api_key": "x", "base_url": "https://a.example",
                                            "models": {"analyst": "gpt-6-luna", "cio": "gpt-6-astra", "deep": "gpt-6-astra"}},
                                 "pm": {"api_key": "y", "base_url": "https://b.example",
                                        "models": {"analyst": "gpt-6-luna", "cio": "gpt-6-sol", "deep": "gpt-6-sol"}}}},
           "paths": {"cache_dir": "/tmp"}}
    c = LLMClient(cfg)
    assert c.attempts("analyst") == [("pm", "gpt-6-luna"), ("openai", "gpt-6-luna")], c.attempts("analyst")
    assert c.attempts("cio") == [("openai", "gpt-6-astra"), ("pm", "gpt-6-sol")], c.attempts("cio")
    assert c.attempts("risk") == [("openai", "m0"), ("pm", "m0")], c.attempts("risk")  # unrouted role: default provider, flat model
    d = LLMClient(cfg, override_model="openai:gpt-6-astra")
    assert d.attempts("analyst") == [("openai", "gpt-6-astra"), ("pm", "gpt-6-sol")], d.attempts("analyst")  # deep run follows the fallback's deep model
    c._down_until["pm"] = time.time() + 60
    assert c.attempts("analyst") == [("openai", "gpt-6-luna")], c.attempts("analyst")  # a down provider is skipped
    c._down_until["openai"] = time.time() + 60
    assert c.attempts("analyst") == [("pm", "gpt-6-luna"), ("openai", "gpt-6-luna")]  # ...unless nothing else is left
    print("llm routing self-check OK")
