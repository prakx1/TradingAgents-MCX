"""The six analyst agents — English translations of the original Chinese prompts,
adapted from Chinese commodity futures to MCX Crude Oil.

Prompt lineage (from AI提示词完整整理文档.md):
  technical      <- enhanced_professional_technical_analysis.py (v4.0)
  fundamentals   <- 专业库存仓单AI分析系统_终极完善版.py (inventory, v8.0)
  news           <- 期货新闻AI分析系统_专业报告版.py (v4.0)
  positioning    <- 专业期货持仓AI分析系统_完美版.py (v3.0, spider-web/smart-money)
  term_structure <- ultimate_term_structure_analyzer.py (v2.0, full carry)
  basis          <- 专业AI基差分析系统_四维度框架.py (four-dimension framework)

Each analyst returns a prose report plus a machine-readable verdict line
(DIRECTION/CONFIDENCE) that the trader's module-voting iron rules consume.
The original extracted opinions by keyword-scanning the Chinese reports; the
explicit verdict line is the reliable English equivalent.
"""
from __future__ import annotations

import json
import re

VERDICT_INSTRUCTION = """
Distinguish directional bias from entry readiness. DIRECTION describes the
balance of YOUR module's evidence over the mandated horizon. Waiting for an
entry condition does not by itself make that bias NEUTRAL. A directional bias
is not permission to enter: explain confirmation, invalidation and timing
separately. Use NEUTRAL when your evidence is balanced, conflicting or lacks a
directional edge; never manufacture a bias to create a trade.
After the report, end with exactly one final line in this format (this line is
machine-parsed; everything above it must be plain professional prose):
DIRECTION: BULLISH|BEARISH|NEUTRAL | CONFIDENCE: HIGH|MEDIUM|LOW
"""


def parse_verdict(text: str) -> tuple[str, str]:
    m = re.search(r"DIRECTION:\s*(BULLISH|BEARISH|NEUTRAL)\s*\|\s*CONFIDENCE:\s*(HIGH|MEDIUM|LOW)",
                  text, re.IGNORECASE)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    low = text.lower()  # fallback: crude keyword scan, as the original did
    bull = sum(low.count(w) for w in ("bullish", "upside", "buy"))
    bear = sum(low.count(w) for w in ("bearish", "downside", "sell"))
    if bull > bear + 1:
        return "BULLISH", "LOW"
    if bear > bull + 1:
        return "BEARISH", "LOW"
    return "NEUTRAL", "LOW"


def strip_verdict(text: str) -> str:
    return re.sub(r"\n?DIRECTION:.*$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()


def _fmt(data) -> str:
    return json.dumps(data, indent=2, default=str)


# ------------------------------------------------------------------ technical

TECHNICAL_SYSTEM = (
    "You are a senior futures technical analyst with 20 years of hands-on trading "
    "experience. Write a professional technical research report strictly grounded in "
    "the real data provided — never fabricate any number. The report must be professional "
    "and readable, in plain text without markdown symbols."
)

def technical_prompt(symbol: str, snap: dict, inr_levels: dict,
                     series_label: str = "NYMEX WTI daily data (MCX settles on WTI x USDINR)",
                     price_unit: str = "$") -> str:
    return f"""As a senior futures technical analyst, produce a professional technical analysis report based on the following real data.

[CORE TECHNICAL DATA]
Instrument: MCX Crude Oil ({symbol}) — driven by NYMEX WTI (cash-settled at WTI x USDINR)
Analyzed price: {price_unit}{snap['price']:.2f}/bbl | MCX fair value: Rs {inr_levels.get('fair_value_inr', 'N/A')}/bbl
Price change: 5-day {snap['change_5d_pct']}%, 20-day {snap['change_20d_pct']}%

Technical indicators (computed on {series_label}):
- RSI14: {snap['rsi14']}
- MACD: {snap['macd']} (signal: {snap['macd_signal']}, histogram: {snap['macd_hist']})
- Moving averages: MA5({snap['ma5']}) MA20({snap['ma20']}) MA60({snap['ma60']}) — {snap['ma_alignment']}
- Bollinger bands: upper {snap['bb_upper']} / middle {snap['bb_middle']} / lower {snap['bb_lower']} (price at {snap['bb_position']:.0%} of band)
- ATR14: {snap['atr14']} (~Rs {inr_levels.get('atr_inr', 'N/A')}/bbl on MCX)
- KDJ: K {snap['kdj_k']} / D {snap['kdj_d']}
- Volume: {snap['volume']} (ratio to 20-day average: {snap['volume_ratio']})

Composite readings:
- Trend score: {snap['trend_score']}/100
- Momentum score: {snap['momentum_score']}/100
- Key supports ({price_unit}): {snap['supports']}
- Key resistances ({price_unit}): {snap['resistances']}
- USDINR: {inr_levels.get('usdinr', 'N/A')} ({inr_levels.get('usdinr_trend', '')})

Write the professional technical report with these sections:

1. Market overview and price action
   - Current price behaviour and character of the move
   - Recent volatility conditions

2. Indicator deep-dive
   - Trend indicators (moving-average system)
   - Momentum indicators (RSI, MACD, KDJ)
   - Volatility indicators (Bollinger bands, ATR)
   - Volume analysis

3. Trend analysis and direction call
   - Primary trend direction and strength
   - Potential reversal signals

4. Support and resistance analysis
   - Key supports and their validity (quote both WTI $ and approximate MCX Rs levels)
   - Key resistances and their validity

5. Risk assessment and trading guidance
   - Current market risk level
   - Concrete trading guidance with stop-loss and target placement (in MCX Rs/bbl)

6. Outlook
   - Short term (1-3 sessions)
   - Medium term (1-2 weeks)

Ground every section in the specific numbers above.
{VERDICT_INSTRUCTION}"""


# --------------------------------------------------------------- fundamentals

FUNDAMENTALS_SYSTEM = """You are a world-class oil-market fundamentals expert with 25 years of professional experience and deep reasoning ability.

Your core specialisations:
1. Deep interpretation of inventory data and inventory-price reflexivity analysis
2. Accurately distinguishing speculative stock-building from real consumption demand
3. Practical application of inventory cycle theory (active restocking - passive restocking - active destocking - passive destocking)
4. Cross-validation across multiple data dimensions
5. Market microstructure and supply-chain analysis
6. Precise investment strategy formulation grounded in inventory analysis

Your analytical style:
- Fact- and data-driven professional analysis
- Multi-dimensional cross-validation
- Digging into the causal logic behind the data
- Theory fused with trading practice
- Actionable investment recommendations
Write in clear professional English, plain text, no markdown symbols."""

def fundamentals_prompt(symbol: str, eia: dict | None, cycle: dict, price_ctx: dict) -> str:
    eia_block = _fmt(eia) if eia else "EIA API key not configured — weekly US inventory detail unavailable this run."
    return f"""You are a world-class oil fundamentals expert with 25 years of experience. Produce an institutional-grade multi-dimensional inventory and supply analysis report for MCX Crude Oil ({symbol}).

Instrument basics:
MCX Crude Oil — INR-denominated, 100 bbl lot, cash-settled against NYMEX WTI x USDINR. Its fundamentals are WTI fundamentals: US inventories (EIA weekly), OPEC+ policy, and global supply-demand; plus the rupee.

Core inventory data (EIA weekly, the analog of exchange warehouse stocks):
{eia_block}

Inventory cycle assessment (computed from the data):
- Cycle stage: {cycle.get('stage', 'unknown')}
- Stage meaning: {cycle.get('meaning', 'n/a')}
- 4-week inventory trend: {cycle.get('inv_trend', 'n/a')}
- Price trend over same window: {cycle.get('price_trend', 'n/a')}
- Inventory-price correlation (12-week): {cycle.get('correlation', 'n/a')}
- Current reflexivity phase: {cycle.get('reflexivity', 'n/a')}

Cross-validation context:
- WTI price: ${price_ctx.get('wti', 'n/a')}/bbl ({price_ctx.get('chg_20d', 'n/a')}% over 20 days)
- Term structure: {price_ctx.get('structure', 'n/a')}
- USDINR: {price_ctx.get('usdinr', 'n/a')}

Analysis requirements:
1. Open directly with the analysis — no meta commentary
2. Multi-dimensional cross-validation (inventories vs price vs curve structure)
3. Deep reading of the inventory-price reflexivity loop
4. Distinguish speculative stock-building from real consumption
5. Apply inventory cycle theory to the current stage
6. Inventory-based strategy recommendation for an MCX crude trader (INR context, rupee effect noted)

Structure: current inventory situation; cycle and reflexivity interpretation; supply-demand balance read; cross-validation against price and curve; strategy implications; risks.
{VERDICT_INSTRUCTION}"""


# ----------------------------------------------------------------------- news

NEWS_SYSTEM = (
    "You are a senior commodity-futures market analyst. Analyse only the real news items "
    "provided — never invent facts, figures or events. Plain text, no markdown symbols; "
    "professional research-desk style suitable for traders."
)

def news_prompt(symbol: str, news: list[dict], analysis_date: str,
                av_sentiment: list[dict] | None = None, briefing: str | None = None) -> str:
    if briefing:  # self-describing intelligence pack (briefing.py): cards + reading rules + [n]-numbered items
        lines = briefing
    else:  # fallback: bare numbered list
        lines = "\n".join(
            f"[{i+1}] ({n.get('published','')}) {n['title']}  — {n['source']}{' [OFFICIAL]' if n.get('lane') == 'official' else ''} (relevance {n['score']})"
            for i, n in enumerate(news))
    if av_sentiment:
        lines += "\n\n[SCORED SENTIMENT — Alpha Vantage energy topic, -1 bearish..+1 bullish]\n" + "\n".join(
            f"- ({s['published']}) {s['sentiment']} {s['score']}: {s['title'][:90]} ({s['source']})"
            for s in av_sentiment)
    return f"""You are a senior futures market analyst. Based on the real news data below, produce a professional deep-dive analysis for MCX Crude Oil ({symbol}).

[INSTRUMENT]
MCX Crude Oil futures — INR, cash-settled vs NYMEX WTI. Analysis date: {analysis_date}.

[DATA SOURCES]
{"The data below is a COUNCIL INTELLIGENCE BRIEFING: every dataset arrives under a card stating WHAT it is, its TRUST, FRESHNESS, how to USE it and its CAVEATS, followed by READING RULES. Read the cards and rules before the items and obey them — in particular weigh by lane (official > wire > regional > aggregator > social), count one story once, and never treat headline prices as current." if briefing else "50-source registry — OFFICIAL (EIA, OPEC, OFAC sanctions, UKMTO maritime incidents, CENTCOM/5th Fleet, Combined Maritime Forces), wire-grade (Bloomberg, Reuters, CNBC, FT, WSJ, MarketWatch, investingLive), Middle-East/shipping outlets, Indian commodity desks, aggregators and social — relevance-scored, trust-weighted, deduplicated. Items tagged [OFFICIAL] are primary sources: weigh them above commentary."} Total: {len(news)} items.

[NEWS DATA]
{lines}

[REQUIREMENTS]
Write a professional report in the style of a research institution:
1. Plain text, no markdown symbols
2. Strictly grounded in the news items above; cite them as [1], [2] ... where used
3. Quote specific facts and numbers from the items — no vague generalities
4. Headlines are timestamped and may be days old. A price level inside a headline is a HISTORICAL quote from its publication moment — NEVER present it as the current market price. Current prices come from the market-data modules, not the news.

Report structure:

One. Market overview
- Overall market situation and the dominant themes

Two. Supply-side analysis
- OPEC+ policy, US production, inventories, exports/sanctions — whatever the news supports

Three. Demand-side analysis
- Global and Indian demand signals, refinery activity, seasonal factors

Four. Price action analysis
- What the news says about recent moves and their drivers

Five. Policy and macro factors
- Central banks, geopolitics, sanctions, USDINR-relevant macro items

Six. Outlook
- Short-term expectation, medium-term view, key risk events ahead

Seven. Trading implications
- Strategy suggestions for MCX crude, risk warnings, what to monitor

Every section must lean on the actual items; where the news is silent, say so honestly.
{VERDICT_INSTRUCTION}"""


# ---------------------------------------------------------------- positioning

POSITIONING_SYSTEM = """You are the chief futures analyst of a top global quantitative hedge fund with 20 years of market experience. You combine positioning data, historical comparison and live market intelligence.

## Core analytical frameworks (adapted from member-seat analysis to CFTC COT)

### 1. Spider-Web strategy
Theory: capture "smart money" from week-over-week changes in managed-money positions.
Core signals: dB (change in MM longs), dS (change in MM shorts).
- dB > 0 and dS < 0: strongly bullish (strength 0.8-1.0)
- dB < 0 and dS > 0: strongly bearish (strength 0.8-1.0)
- same direction: judge from net change plus historical percentile

### 2. Smart-money analysis
Managed-money net position level and its historical percentile; long/short ratio; position efficiency versus open interest.

### 3. Retail reverse strategy
Non-reportable (small trader) net positioning as a contrarian indicator — extremes in retail positioning historically fade.

### 4. Concentration analysis
Top-4 trader gross concentration on each side; concentration divergence signals large-player conviction.

### 5. Historical context
Percentiles, extremes, and what followed similar configurations.

## Output requirements — institutional standard
Cover each framework in its own titled prose section, then a combined conclusion and trading recommendation. Plain text, no markdown symbols. Strictly data-driven: only the numbers provided; identify risks honestly; be concrete and actionable."""

def positioning_prompt(symbol: str, cot: dict, mcx_oi: dict | None = None) -> str:
    mcx_block = (f"\n[MCX PARTICIPANT DATA — PRO vs CLIENT (India-native)]\n{_fmt(mcx_oi)}\n"
                 if mcx_oi else "")
    return f"""Analyse the positioning picture for MCX Crude Oil ({symbol}). MCX crude tracks NYMEX WTI, so WTI positioning is the relevant smart-money read (CFTC Commitments of Traders, weekly).

[COT DATA — NYMEX WTI (CRUDE OIL, LIGHT SWEET), latest report]
{_fmt(cot)}
{mcx_block}

Notes on fields: mm_* = managed money (funds); retail_* = non-reportable small traders; producer_net = physical hedgers; percentiles computed over {cot.get('weeks_of_history', 'N')} weeks of history; spider_web_signal derives from dB={cot.get('mm_long_change_wow')} and dS={cot.get('mm_short_change_wow')}.

Apply your five frameworks in order (Spider-Web, Smart Money, Retail Reverse, Concentration, Historical Context), then give the combined conclusion and concrete trading recommendation for an MCX crude trader. Where the data is limited (weekly cadence, US-market positioning proxying an Indian contract), acknowledge it.
{VERDICT_INSTRUCTION}"""


# -------------------------------------------------------------- term structure

TERM_SYSTEM = (
    "You are a senior futures term-structure analyst. Write a professional report in plain "
    "text without markdown symbols, strictly grounded in the data provided."
)

def term_structure_prompt(symbol: str, curve_rows: str, metrics: dict, inv_note: str) -> str:
    return f"""Produce a professional term-structure analysis report for MCX Crude Oil ({symbol}), based on the NYMEX WTI futures strip (the settlement reference for MCX crude; MCX lists monthly contracts that mirror this curve).

Report requirements:
1. No markdown symbols
2. Reference the data tables below explicitly in the text
3. Full structure:

Part 1: Current term-structure state
- Structure type (contango / backwardation / flat) and its characteristics
- Price distribution and liquidity along the curve (reference the strip table)

Part 2: Full-carry theory validation
- Explain full carry (storage + financing + fees = theoretical spread ceiling in contango)
- Compare actual spreads against carry logic; note that WTI storage economics (Cushing) anchor this, and that MCX crude, being cash-settled, imports this structure rather than arbitraging it directly

Part 3: Convenience yield interpretation
- Explain convenience yield and read it from the current structure
- Link to the inventory picture: {inv_note}

Part 4: Spread rhythm analysis
- Front-month minus second-month spread and what it signals
- Front-to-back slope and roll yield for a position holder (MCX traders rolling monthly bear/earn this)

Part 5: Industry logic and strategy
- What this structure means for physical players and for an MCX crude futures trader
- Concrete strategy suggestions (directional bias from carry, roll timing for MCX monthly expiries around the 19th-20th)

Part 6: Conclusion
- Core view and operational guidance

[WTI STRIP DATA]
{curve_rows}

[CURVE METRICS]
{_fmt(metrics)}
{VERDICT_INSTRUCTION}"""


# ---------------------------------------------------------------------- basis

BASIS_SYSTEM = (
    "You are a professional futures basis analyst. Strictly fact-based four-dimension "
    "analysis; never fabricate any number. Plain text, no markdown symbols."
)

def basis_prompt(symbol: str, basis_data: dict, analysis_date: str) -> str:
    return f"""You are a top basis analyst with 20 years of practice. Analyse MCX Crude Oil ({symbol}) as of {analysis_date} using the four-dimension basis framework, adapted to a cash-settled INR contract.

Important principles:
1. Never fabricate data, ratios or cost parameters — use only the numbers below
2. Plain text, professional and readable
3. All conclusions grounded in the provided data

For MCX crude the classic spot-vs-futures basis is replaced by import-parity basis: MCX futures price vs fair value (WTI x USDINR). The rupee is a live basis driver — INR depreciation mechanically lifts MCX crude even when WTI is flat.

[DIMENSION 1 — Parity basis]
{_fmt(basis_data.get('parity', {}))}

[DIMENSION 2 — Seasonality]
{_fmt(basis_data.get('seasonality', {}))}

[DIMENSION 3 — Inventory-basis relationship]
{_fmt(basis_data.get('inventory_relation', {}))}

[DIMENSION 4 — Spatial spreads (global grade/geography spreads)]
{_fmt(basis_data.get('spatial', {}))}

Report structure:
1. Basis situation and characteristics (including the rupee leg)
2. Four-dimension deep analysis (one passage per dimension, in order)
3. Combined judgment and strategy suggestions for an MCX crude trader
4. Risk warnings

Where a dimension's data is marked unavailable, state that honestly and weight the others.
{VERDICT_INSTRUCTION}"""


# ------------------------------------------------------------------- registry

ANALYSTS = {
    "technical": ("Technical Analyst", TECHNICAL_SYSTEM),
    "fundamentals": ("Fundamentals (Inventory) Analyst", FUNDAMENTALS_SYSTEM),
    "news": ("News & Sentiment Analyst", NEWS_SYSTEM),
    "positioning": ("Positioning (COT) Analyst", POSITIONING_SYSTEM),
    "term_structure": ("Term Structure Analyst", TERM_SYSTEM),
    "basis": ("Basis (Import Parity) Analyst", BASIS_SYSTEM),
}


def run_analyst(llm, module: str, user_prompt: str, reasoning: bool = False) -> dict:
    title, system = ANALYSTS[module]
    if '[MANDATORY DESK HORIZON: INTRADAY]' in user_prompt:
        user_prompt+=('\nINTRADAY BRIEF: Replace the long report-format requests above with at most 250 words. '
                      'Keep the directional evidence, conflicting evidence, entry readiness, grounded levels and '
                      'invalidation conditions. Preserve the final DIRECTION/CONFIDENCE line. Do not omit material risks.\n')
    text = llm.chat(system, user_prompt, reasoning=reasoning, role="analyst")
    direction, confidence = parse_verdict(text)
    return {"module": module, "title": title, "report": strip_verdict(text),
            "direction": direction, "confidence": confidence}
