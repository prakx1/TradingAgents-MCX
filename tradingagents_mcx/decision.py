"""Decision chain: bull/bear debate -> trader -> risk manager -> CIO.

English port of 优化版辩论风控决策系统.py (the production engine), with its known
bugs fixed rather than ported:
  - the original judge prompt asked for labels but the parser wanted numbers, so
    every round tied 5.0-5.0 and the winner defaulted to BULLISH -> here the judge
    must emit numeric scores on a parseable line;
  - the original risk manager and CIO had full AI prompts that were never called
    (replaced by templates "for stability") -> here they call the LLM, with a
    deterministic template as the failure fallback;
  - the original defaulted unknown direction to "cautiously bullish" -> here the
    default is NEUTRAL / stand aside.
"""
from __future__ import annotations

import re
import json

from .setups import SETUP_CONTRACT

INTRADAY_MARKER = '[MANDATORY DESK HORIZON: INTRADAY]'


def _entry_brief(prompt: str, role: str) -> str:
    """Keep time-sensitive execution reviews concise without removing checks."""
    if INTRADAY_MARKER not in prompt:
        return prompt
    if role=='trader':
        prompt=re.sub(r'\[THREE-DIMENSION JUDGING FRAMEWORK\][\s\S]*?(?=FINAL DIRECTION: BULLISH\|BEARISH\|NEUTRAL)',
            '[INTRADAY ENTRY REVIEW — at most 300 words before the machine lines]\n'
            'State the module vote and debate confidence adjustment, the freshest observed price and timestamp, '
            'the entry condition (already met or still missing), evidence for stop/target, worst-entry reward/risk, '
            'expiry and material risks. Do not rescore every debate argument or write a long research essay. '
            'Retain all required machine-readable lines, the full SETUP JSON and lessons used:\n',prompt,count=1)
    elif role=='cio':
        prompt=re.sub(r'=== CIO strategic analysis task ===[\s\S]*?(?=End with the machine-readable line:)',
            '=== INTRADAY FINAL REVIEW — at most 220 words before the machine lines ===\n'
            'Check the exact trader plan against the freshest quote, risk review and decision matrix. '
            'State the decision, evidence and execution/invalidation conditions. Confirm the matrix or '
            'explain a deviation in the required format. Review any existing pending setup explicitly. '
            'No long-term allocation essay or repeated background summary.\n\n',prompt,count=1)
        prompt=prompt.replace('stated in section 3 as','stated in your reasoning as').replace('write "MATRIX CONFIRMED" in section 3','write "MATRIX CONFIRMED"')
    elif role=='debate':
        prompt=re.sub(r'Format: 400-550 words,[^\n]*',
            'Format: 100-150 words focused on supplied evidence for this intraday entry; '
            'include the strongest counterargument. Do not invent win rates or probabilities.',prompt)
    elif role=='risk':
        prompt+='\nINTRADAY REVIEW: at most 220 words before the machine lines. Keep the numerical risk checks, '
        prompt+='decision and material risks; omit repeated background. Preserve every machine-readable line.\n'
    return prompt

# ------------------------------------------------------------------ helpers

def _module_tally(analyst_results: list[dict]) -> dict:
    tally = {"BULLISH": [], "BEARISH": [], "NEUTRAL": []}
    for r in analyst_results:
        tally[r["direction"]].append(r["module"])
    return tally


def _execution_context(trader):
    return ('[ENGINE CALCULATED SETUP CLOCK — authoritative]\n'+json.dumps(trader.get('setup_timing'))+'\n'
            'The evidence_as_of above is the original trader anchor, independent of newer quote timestamps. '
            'Use valid_until exactly. Newer review timestamps must never restart this duration. '
            'The engine owns expiry arithmetic; this clock overrides approximate expiry prose. '
            'Do not reject otherwise-valid price conditions due solely to an expiry calculation in prose.\n'
            '[ORIGINAL ENTRY CANDIDATE — background to the exact trader plan, never a new authorization]\n'
            +json.dumps(trader.get('entry_candidate'))+'\n'
            'A trend_continuation_v1 target is a predeclared 2.4-ATR projection, not observed resistance. '
            'Assess the exact proposed stop, target and current reward/risk; do not enlarge or replace them. '
            'No historical profitability or success rate is implied.\n'
            '[FIXED REGIME EXECUTION CONSTRAINTS — when a regime proposal was selected]\n'
            +json.dumps(trader.get('entry_constraints'))+'\n'
            'When present, the confirmation expiry and 1.5:1 floor after fees and slippage are enforced '
            'again at the observed entry. Do not restart its clock or change its stop/target.\n'
            '[ENGINE CALCULATED PAPER ECONOMICS — exact plan, separate from gross signal R]\n'
            +json.dumps(trader.get('paper_economics'))+'\n'
            'Discuss the supplied net reward/risk, including weak economics after known fees. '
            'This calculation is evidence for your review, not approval. Do not alter supported levels to improve the ratio.')


def _context_block(analyst_results: list[dict]) -> str:
    parts = []
    for r in analyst_results:
        body = r["report"]
        parts.append(f"[{r['title']} — view: {r['direction']} ({r['confidence']} confidence)]\n{body}\n")
    note = (f"\nReports supplied for this strategy: {len(analyst_results)} "
            f"({', '.join(r['module'] for r in analyst_results)}). "
            "The intraday strategy selects its own analysts; do not impose a six-module requirement. "
            "Use only supplied evidence and explicit data-quality flags; never fabricate another analyst's view.")
    return "\n".join(parts) + note


ANTI_FABRICATION = """
STRICTLY FORBIDDEN (applies equally to both debaters):
- Fabricating any data, price, or indicator
- Citing information that does not appear in the analyst reports above
- Vague sourcing like "it is understood that" or "market rumour has it"
"""


# ------------------------------------------------------------------- debate

def _bull_prompt(commodity: str, context: str, round_num: int, prev_bear: str) -> str:
    prev = f"\nThe bear's view last round (excerpt): {prev_bear[:300]}\n" if prev_bear else ""
    return f"""You are the senior BULLISH analyst for {commodity} futures, in round {round_num} of a fierce professional debate. Your mission: argue on the strength of reason — use iron-clad facts and logic to crush the bears' pessimism.

Analyst team research data ({commodity}):
{context}
{prev}
Debate from the bullish side under these core requirements:
1. Strictly data-grounded: use only the real data in the analyst reports above.
2. Full-dimension opportunity mining: systematically comb every available dimension (inventory/fundamentals, positioning, technicals, basis, news, term structure) for bullish signals.
3. Precise data support for every point — build an irrefutable foundation.
4. Construct the bullish logic chain systematically.
5. Use concrete probability language ("high win-rate setup", explicit odds).
6. Identify the single most critical growth driver and argue it hardest.
7. Strike at the bear case: expose its exaggerations, stale inputs, and logical holes.
8. Impassioned style: you may moderately mock the bears' excessive caution — passion and data as twin weapons.
9. Honesty rule: if a dimension does not support the bull case, admit it, and find strength elsewhere.
{ANTI_FABRICATION}
Format: 400-550 words, 4-5 compact paragraphs, plain text, no headings or lists, no closing data checklist. Sharp opening, forceful data-backed middle, closing probability assessment.
Begin your bullish argument:"""


def _bear_prompt(commodity: str, context: str, round_num: int, bull_argument: str) -> str:
    return f"""You are the senior BEARISH analyst for {commodity} futures, in round {round_num} of a fierce professional debate. Your mission: argue on the strength of reason — use iron-clad facts and logic to shatter the bulls' blind optimism.

Analyst team research data ({commodity}):
{context}

The bullish analyst just argued:
{bull_argument}

Debate from the bearish side under these core requirements:
1. Strictly data-grounded: use only the real data in the analyst reports above.
2. Full-dimension risk mining: systematically comb every available dimension for risk signals.
3. Precise data support for every point.
4. Construct the bearish logic chain systematically.
5. Use concrete probability language for risk scenarios.
6. Identify the single most critical risk driver and argue it hardest.
7. Strike at the bull case just delivered: expose over-optimism, blind spots, causal errors.
8. Impassioned style: you may moderately mock the bulls' wishful thinking.
9. Honesty rule: if a dimension does not support the bear case, admit it, and find risk elsewhere.
{ANTI_FABRICATION}
Format: 400-550 words, 4-5 compact paragraphs, plain text, no headings or lists, no closing data checklist — exactly symmetric in structure with the bull.
Begin your bearish argument:"""


def _judge_prompt(commodity: str, round_num: int, bull: str, bear: str, bear_first: bool = False) -> str:
    a, b = f"Bull argument:\n{bull}", f"Bear argument:\n{bear}"
    return f"""You are the professional debate judge for {commodity} futures, scoring round {round_num}.
(The order in which the two arguments appear below alternates between rounds and carries no meaning.)

{b if bear_first else a}

{a if bear_first else b}

Score each side 0-10 on: data grounding (30%), logical consistency (25%), market understanding (25%), risk awareness (20%). Bonus up to +1 each for multi-dimensional data use, precise rebuttal of the opponent, exceptional logic chain, professional probabilistic phrasing.

Output EXACTLY this format (the score lines are machine-parsed):
BULL SCORE: <number 0-10, one decimal>
BEAR SCORE: <number 0-10, one decimal>
WINNER: BULL|BEAR|DRAW
RATIONALE: <2-3 sentences on the scoring, noting data integration quality>
KEY DISPUTES: <the 2-3 core points of contention this round>"""


def _parse_judge(text: str) -> tuple[float, float, str]:
    def grab(label, default):
        m = re.search(rf"{label}\s*:\s*([\d.]+)", text, re.IGNORECASE)
        try:
            return float(m.group(1)) if m else default
        except ValueError:
            return default
    bull, bear = grab("BULL SCORE", 5.0), grab("BEAR SCORE", 5.0)
    m = re.search(r"WINNER\s*:\s*(BULL|BEAR|DRAW)", text, re.IGNORECASE)
    winner = m.group(1).upper() if m else ("BULL" if bull > bear else "BEAR" if bear > bull else "DRAW")
    return bull, bear, winner


def run_debate(llm, commodity: str, analyst_results: list[dict], rounds: int = 2, evidence: str = '') -> dict:
    context = evidence + '\n\n' + _context_block(analyst_results)
    round_records, bull_total, bear_total = [], 0.0, 0.0
    prev_bear = ""
    for rn in range(1, rounds + 1):
        bull_arg = llm.chat("You are a passionate but rigorous bullish futures debater.",
                            _entry_brief(_bull_prompt(commodity, context, rn, prev_bear),'debate'), role="debate")
        bear_arg = llm.chat("You are a passionate but rigorous bearish futures debater.",
                            _entry_brief(_bear_prompt(commodity, context, rn, bull_arg),'debate'), role="debate")
        judge_text = llm.chat("You are an impartial professional debate judge.",  # bear-first on even rounds: kills position bias
                              _judge_prompt(commodity, rn, bull_arg, bear_arg, bear_first=(rn % 2 == 0)), role="debate")
        b, s, w = _parse_judge(judge_text)
        bull_total += b
        bear_total += s
        round_records.append({"round": rn, "bull": bull_arg, "bear": bear_arg, "judge_order": "bear-first" if rn % 2 == 0 else "bull-first",
                              "judge": judge_text, "bull_score": b, "bear_score": s, "winner": w})
        prev_bear = bear_arg
    # decide from the rounded totals so the displayed 17.3-17.3 can never contradict the winner
    bull_total, bear_total = round(bull_total, 1), round(bear_total, 1)
    winner = "BULL" if bull_total > bear_total else "BEAR" if bear_total > bull_total else "DRAW"
    summary = (f"Bull {bull_total:.1f} · Bear {bear_total:.1f} · {winner}\n"+round_records[-1]['judge']
               if INTRADAY_MARKER in evidence else llm.chat(
        "You are a vivid financial-markets commentator.",
        f"Write a lively 150-220 word summary of this {rounds}-round bull-vs-bear debate on {commodity} "
        f"futures — as exciting as sports commentary but professionally grounded. "
        f"Final scores: bull {bull_total:.1f} vs bear {bear_total:.1f} ({winner} wins). "
        f"Core bull case: {round_records[-1]['bull'][:400]}. Core bear case: {round_records[-1]['bear'][:400]}. "
        "Plain text, no markdown.", role="debate"))
    return {"rounds": round_records, "bull_score": round(bull_total, 1), "bear_score": round(bear_total, 1),
            "winner": winner, "summary": summary}


# ------------------------------------------------------------------- trader

TRADER_SYSTEM = ("You are a senior futures trading expert with 15 years of experience — absolutely "
                 "objective and rational, data-driven, master of trading strategies, with extremely "
                 "strong risk awareness. Plain text, no markdown symbols.")

def _trader_prompt(commodity: str, contract_spec: dict, price_block: str,
                   analyst_results: list[dict], debate: dict) -> str:
    tally = _module_tally(analyst_results)
    votes = (f"Bullish modules: {len(tally['BULLISH'])} {tally['BULLISH']}\n"
             f"Bearish modules: {len(tally['BEARISH'])} {tally['BEARISH']}\n"
             f"Neutral modules: {len(tally['NEUTRAL'])} {tally['NEUTRAL']}\n"
             f"Total modules: {len(analyst_results)}")
    per_module = "\n".join(f"- {r['title']}: {r['direction']} ({r['confidence']})" for r in analyst_results)
    debate_text = "\n\n".join(
        f"--- Round {r['round']} ---\nBULL: {r['bull'][:900]}\nBEAR: {r['bear'][:900]}\nJUDGE: {r['judge'][:400]}"
        for r in debate["rounds"])
    return f"""You are the senior trading expert judging the bull/bear debate and formulating the trading strategy for {commodity}.

[CONTRACT SPECIFICATION — MCX]
{contract_spec['name']}: price in INR per barrel, tick Rs {contract_spec['tick_size_inr']}/bbl, hours {contract_spec['trading_hours_ist']}. Signals only; no position sizing or account calculations.

{price_block}

[ANALYST TEAM VOTE STATISTICS — MUST BE STRICTLY OBEYED]
{votes}

Per-module views:
{per_module}

[COMPLETE ANALYST REPORTS — including indicators and conditional setups]
{_context_block(analyst_results)}

[DEBATE TRANSCRIPT]
{debate_text}
Debate totals: bull {debate['bull_score']} vs bear {debate['bear_score']} — {debate['winner']} ahead.

[CORE RULES]
{SETUP_CONTRACT}
Iron law of direction (absolutely inviolable):
- Bullish modules > bearish modules -> MUST be bullish
- Bearish modules > bullish modules -> MUST be bearish
- Bullish = bearish -> MUST be neutral
Confidence: module advantage >=3 -> HIGH; 2 -> MEDIUM; <=1 -> LOW.
Risk/reward: provide a numeric entry, stop and single primary target. Do not invent a target solely to pass the risk/reward threshold.
Evaluate readiness against the NEWEST supplied completed bars and live quote. When
the required pattern has already confirmed and current reward/risk passes, give a
numeric ENTRY; do not keep asking for another future candle by default. WAIT must
name a specific unsatisfied condition. Use pullback_reclaim for support pullbacks
or resistance rejection plans; do not force every plan into a new breakout.
Older analyst/debate prices are background if refreshed evidence supersedes them.
The debate verdict reflects ARGUMENT QUALITY, not directional correctness. It may never change the direction — it only adjusts confidence one notch (agreement keeps confidence; disagreement downgrades it).

[THREE-DIMENSION JUDGING FRAMEWORK]
Score each module's bull vs bear case 0-10:
Dimension 1 — evidence quality (40%): authenticity x0.4, timeliness x0.2, relevance x0.25, completeness x0.15
Dimension 2 — logical rigour (35%): causality x0.3, reasoning x0.3, rebuttal effectiveness x0.2, self-consistency x0.2
Dimension 3 — risk awareness (25%): uncertainty recognition x0.3, respect for opposing view x0.2, scenario analysis x0.25, risk identification x0.25
Composite = D1x40% + D2x35% + D3x25%. Gap >=1.5 strong; >=0.8 leaning; <0.8 neutral.
Use neutral evaluative language: "positive expectation with data support" not "blindly optimistic"; "cautious expectation with data support" not "over-pessimistic".

[OUTPUT — exactly these six sections, in order, each exactly once, then STOP]
1. [Debate Judging Analysis] — per-module three-dimension scoring with 2-3 quoted arguments per side
2. [Analyst View Assessment] — restate the module vote statistics
3. [Argument Quality Verdict] — tabulate strong/leaning/neutral modules per side, overall debate tilt
4. [Combined Direction Conclusion] — apply the iron law step by step: copy the vote counts, derive the mandatory direction, derive base confidence from the advantage count, apply the debate adjustment, state FINAL direction + confidence
5. [Trading Strategy Recommendation] — strategy (outright long / outright short / stand aside), entry, stop-loss, primary target — all in MCX Rs/bbl; numeric reward/risk = abs(target-entry)/abs(entry-stop); holding period matching the mandated strategy. Signals only: never calculate capital, margin, lots or position sizing
6. [Risk Warning] — main risks, uncertainties, monitoring points

After section 6, also output these machine-readable lines and then stop:
FINAL DIRECTION: BULLISH|BEARISH|NEUTRAL
FINAL CONFIDENCE: HIGH|MEDIUM|LOW
ENTRY: <single numeric Rs/bbl price or WAIT>
STOP: <Rs/bbl or N/A>
TARGET: <single numeric primary target in Rs/bbl or N/A>
SETUP: <one-line conditional setup JSON, or null>
LESSONS USED: <comma-separated lesson ids that influenced this decision, or none>"""


def _parse_trader(text: str, tally: dict) -> dict:
    def grab(label, pattern=r"([^\n]+)"):
        # the machine block is the tail of the answer; prose above it may also say "entry:" — take the LAST match
        ms = re.findall(rf"(?m)^\s*{label}\s*:\s*{pattern}", text, re.IGNORECASE) or \
             re.findall(rf"{label}\s*:\s*{pattern}", text, re.IGNORECASE)
        return ms[-1].strip() if ms else None
    nb, ns = len(tally["BULLISH"]), len(tally["BEARISH"])
    direction = "BULLISH" if nb > ns else "BEARISH" if ns > nb else "NEUTRAL"
    confidence = grab("FINAL CONFIDENCE", r"(HIGH|MEDIUM|LOW)")
    if not confidence:
        adv = abs(len(tally["BULLISH"]) - len(tally["BEARISH"]))
        confidence = "HIGH" if adv >= 3 else "MEDIUM" if adv == 2 else "LOW"
    try:
        setup = json.loads(grab('SETUP') or 'null')
    except ValueError:
        setup = None
    return {"direction": direction.upper(), "confidence": confidence.upper(),
            "entry": grab("ENTRY"), "stop": grab("STOP"), "target": grab("TARGET"),
            "setup": setup,
            "lessons_used": list(dict.fromkeys(re.findall(r"\b(?:L-[A-Za-z0-9][A-Za-z0-9_-]*|DD-\d{8})\b", grab("LESSONS USED") or ""))),
            "reasoning": re.sub(r"\nFINAL DIRECTION:[\s\S]*$", "", text).strip()}


def run_trader(llm, commodity: str, contract_spec: dict, price_block: str,
               analyst_results: list[dict], debate: dict) -> dict:
    tally = _module_tally(analyst_results)
    text = llm.chat(TRADER_SYSTEM,
                    _entry_brief(_trader_prompt(commodity, contract_spec, price_block, analyst_results, debate),'trader'),
                    max_tokens=14000, role="trader")
    out = _parse_trader(text, tally)
    out["module_tally"] = {k: len(v) for k, v in tally.items()}
    return out


# ------------------------------------------------------------- risk manager

RISK_SYSTEM = """You are a professional risk-control director with 15 years of experience, responsible for independent, objective risk assessment of trading strategies.
Principles: independent and objective, unaffected by trade direction; based on actual data; focused on risk control, not investment advice; honest about data limitations. Plain text, no markdown."""

def _risk_prompt(commodity: str, trader: dict, debate: dict, atr_stop_note: str) -> str:
    return f"""=== Actual trading strategy information ===
Instrument: {commodity} (MCX)
Trader direction: {trader['direction']} (confidence {trader['confidence']})
Entry: {trader['entry']} | Stop: {trader['stop']} | Target: {trader['target']}
Conditional setup JSON: {json.dumps(trader.get('setup'))}
{_execution_context(trader)}
{SETUP_CONTRACT}

=== Complete trader analysis ===
{trader['reasoning']}

=== Debate environment ===
Result: {debate['winner']} ahead (bull {debate['bull_score']} vs bear {debate['bear_score']}), score gap {abs(debate['bull_score'] - debate['bear_score']):.1f}

=== Volatility reference ===
{atr_stop_note}

=== Requirements ===
Strictly fact-based: assess only the actual strategy above; stop-loss advice must match the strategy's direction; every assessment needs explicit data support; never fabricate numbers or probabilities.
Compute reward-to-risk using the primary target; never use a farther secondary target to rescue an inadequate primary setup.
If a datum is absent from the text above, write "not provided" — do not claim the plan lacks something the strategy line or analysis states.

Output exactly this structure:
[Operational Confidence] HIGH|MEDIUM|LOW
one-line professional basis

[Risk Level Analysis]
- Initial price risk: <entry-to-stop distance in INR/bbl and percent of entry>
- Reward/risk ratio: <target distance divided by stop distance; state uncertainty without invented probabilities>
- Composite risk grade: LOW RISK|MEDIUM RISK|HIGH RISK

[Main Risk Factors]
- Strategy execution risk: ...
- Market environment risk: ...
- Data quality risk: ...

[Risk Control Logic]
detailed reasoning for the confidence judgment

[Professional Risk Control Recommendations]
- Signal eligibility: <accept or stand aside; no capital or position sizing>
- Stop-loss execution: <requirement, consistent with direction; reference the ATR-based stop>
- Key monitoring indicators: <specific signals — EIA report, OPEC meetings, USDINR, MCX expiry day>

End with the machine-readable line:
SETUP APPROVAL: APPROVE|REJECT
Approve only if a supplied conditional JSON fully represents the plan, is grounded,
and passes risk/reward across its entry range. Otherwise REJECT. An approved setup
is permission to monitor those exact conditions, not an immediate signal.
OPERATIONAL CONFIDENCE: HIGH|MEDIUM|LOW"""


def run_risk(llm, commodity: str, trader: dict, debate: dict, atr_stop_note: str) -> dict:
    degraded = False
    fallback_conf = {"HIGH": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW"}[trader["confidence"]]
    try:
        text = llm.chat(RISK_SYSTEM, _entry_brief(_risk_prompt(commodity, trader, debate, atr_stop_note),'risk'), role="risk")
    except Exception:
        degraded = True
        text = (f"[Operational Confidence] {fallback_conf}\nDeterministic fallback (LLM unavailable): "
                "confidence capped by trader confidence; "
                f"strict stop-loss per plan; monitor EIA weekly report, OPEC decisions, USDINR.\n"
                f"OPERATIONAL CONFIDENCE: {fallback_conf}")
    m = re.search(r"OPERATIONAL CONFIDENCE\s*:\s*(HIGH|MEDIUM|LOW)", text, re.IGNORECASE)
    approval = re.findall(r'(?m)^\s*SETUP APPROVAL:\s*(APPROVE|REJECT)\s*$',text,re.I)
    return {"degraded": degraded or m is None, "opinion": re.sub(r"\nOPERATIONAL CONFIDENCE:.*$", "", text).strip(),
            "setup_approval": approval[-1].upper() if approval else 'REJECT',
            "operational_confidence": (m.group(1).upper() if m else fallback_conf)}


# ---------------------------------------------------------------------- CIO

CIO_SYSTEM = """You are a world-class chief investment officer (CIO) of a top investment institution: 20+ years of investment decisions, expert in macro, market cycles and asset allocation, skilled at synthesizing viewpoints into a final decision, experienced in crisis handling and contrarian investing.
Decision principles: based on data but beyond data; balance return and risk; consider timing and trend; maintain independent judgment. Plain text only — no asterisks, hashes or underscores."""

def _cio_prompt(commodity: str, trader: dict, risk: dict, debate: dict, analysis_date: str, memory_block: str = "", matrix_decision: str = "") -> str:
    return f"""=== Investment decision subject ===
Instrument: {commodity} (MCX crude oil futures)
Analysis date: {analysis_date}
Trader recommendation: {trader['direction']} ({trader['confidence']} confidence). Signals only, no capital or sizing.
Proposed entry: {trader.get('entry')} | Stop: {trader.get('stop')} | Primary target: {trader.get('target')}
Conditional setup JSON: {json.dumps(trader.get('setup'))}
{_execution_context(trader)}
Risk conditional approval: {risk.get('setup_approval','REJECT')}
{SETUP_CONTRACT}
Risk control operational confidence: {risk['operational_confidence']}
Debate: {debate['winner']} ahead by {abs(debate['bull_score'] - debate['bear_score']):.1f} points

=== Desk decision matrix (deterministic: 60% risk-control confidence + 40% direction confidence) ===
Trader {trader['direction']}/{trader['confidence']} x risk-control {risk['operational_confidence']} -> {matrix_decision or 'n/a'}
This is the desk's default decision. Confirm it or deviate — but a deviation needs a reason the numbers above do not already contain, stated in section 3 as "DEVIATING FROM MATRIX because ...". If you confirm, write "MATRIX CONFIRMED" in section 3.
{memory_block}

=== Core decision information ===
Complete trader analysis:
{trader['reasoning']}

Complete risk opinion:
{risk['opinion']}

=== CIO strategic analysis task ===
1. Strategic opportunity assessment (120-160 words): strategic value of this opportunity; fit to the mandated holding period; market timing and cycle position; overlooked long-term drivers (for crude: OPEC spare capacity, USDINR trajectory, Indian demand growth).
2. Comprehensive risk judgment (120-160 words): systemic risks beyond the technicals; macro shifts; liquidity, policy and geopolitical risk; whether risk-reward meets institutional standards.
3. Decision logic (150-200 words): the final decision logic balancing the trader and risk views; time frame and expected objectives; consistency with the specified intraday or long-term signal horizon.
4. Execution strategy (120-160 words): concrete execution plan and timing; key monitoring indicators and adjustment triggers (EIA Wednesdays, OPEC meetings, MCX expiry ~19th-20th, RBI reference rate); scenario responses; authorization limits.

End with the machine-readable line:
SETUP APPROVAL: APPROVE|REJECT
Approve only a supplied, risk-approved conditional JSON that fully captures every
condition you require. Otherwise REJECT. Do not authorize an incomplete JSON or
add extra requirements only in prose. For an approved conditional plan use a
directional FINAL DECISION with ENTRY still WAIT; activation awaits the monitor.
SIGNAL REASON: <one sentence explaining the specific setup confirmation or rejection; cite supplied evidence>
PENDING SETUP ACTION: KEEP|CANCEL
PENDING SETUP ID: <exact ID from existing pending setups, or NONE>
PENDING SETUP REASON: <evidence-based reason for cancellation, or NONE>
KEEP is the default for existing plans when you have no new executable setup. CANCEL requires an exact supplied ID and a reason why that plan is no longer valid. Do not cancel solely because the current price is outside its entry range before confirmation. Never invent a setup ID. New valid signals/setups automatically replace existing pending plans.
FINAL DECISION: AGGRESSIVE LONG|MODERATE LONG|CAUTIOUS LONG|STAND ASIDE|CAUTIOUS SHORT|MODERATE SHORT|AGGRESSIVE SHORT"""


def _decision_matrix(direction: str, dir_conf: str, op_conf: str) -> str:
    """Deterministic fallback/check: op-confidence weighted 0.6, direction 0.4."""
    score = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    total = score[op_conf] * 0.6 + score[dir_conf] * 0.4
    if direction == "NEUTRAL":
        return "STAND ASIDE"
    side = "LONG" if direction == "BULLISH" else "SHORT"
    if total >= 2.6:
        return f"AGGRESSIVE {side}"
    if total >= 2.0:
        return f"MODERATE {side}"
    return f"CAUTIOUS {side}"


def run_cio(llm, commodity: str, trader: dict, risk: dict, debate: dict, analysis_date: str, memory_block: str = "") -> dict:
    degraded = False
    matrix_decision = _decision_matrix(trader["direction"], trader["confidence"], risk["operational_confidence"])
    try:
        text = llm.chat(CIO_SYSTEM, _entry_brief(_cio_prompt(commodity, trader, risk, debate, analysis_date, memory_block, matrix_decision),'cio'), role="cio")
    except Exception:
        degraded = True
        text = (f"Deterministic decision (LLM unavailable): {matrix_decision}, from trader "
                f"{trader['direction']}/{trader['confidence']} and risk confidence {risk['operational_confidence']}.\n"
                f"FINAL DECISION: {matrix_decision}")
    ms = re.findall(r"FINAL DECISION\s*:\s*(AGGRESSIVE LONG|MODERATE LONG|CAUTIOUS LONG|STAND ASIDE|CAUTIOUS SHORT|MODERATE SHORT|AGGRESSIVE SHORT)",
                    text, re.IGNORECASE)
    m = ms[-1] if ms else None  # last match: the closing machine line, not a mention in the prose
    final = m.upper() if m else matrix_decision
    if (trader["direction"] == "NEUTRAL"
            or (trader["direction"] == "BULLISH" and "SHORT" in final)
            or (trader["direction"] == "BEARISH" and "LONG" in final)):
        final = "STAND ASIDE"  # iron-law backstop: CIO cannot invent a direction
    reason = re.search(r'(?m)^\s*SIGNAL REASON:\s*([^\n]+)',text,re.I)
    approval = re.findall(r'(?m)^\s*SETUP APPROVAL:\s*(APPROVE|REJECT)\s*$',text,re.I)
    pending={}
    for key,label in (('pending_setup_action','ACTION'),('pending_setup_id','ID'),('pending_setup_reason','REASON')):
        matches=re.findall(rf'(?m)^[ \t]*PENDING SETUP {label}:[ \t]*([^\r\n]+)',text,re.I)
        pending[key]=matches[-1].strip() if matches else None
    pending['pending_setup_action']='CANCEL' if (pending['pending_setup_action'] or '').upper()=='CANCEL' else 'KEEP'
    if not pending['pending_setup_reason'] or pending['pending_setup_reason'].upper()=='NONE':
        pending['pending_setup_action']='KEEP'
    return {"degraded": degraded or m is None, "statement": re.sub(r"\nFINAL DECISION:.*$", "", text).strip(),
            **pending,
            "setup_approval": approval[-1].upper() if approval else 'REJECT',
            "signal_reason": reason.group(1).strip() if reason else None,
            "final_decision": final, "matrix_decision": matrix_decision,
            # agreed only when the CIO wrote a decision itself and it matches the matrix without a stated deviation
            "matrix_agreed": bool(m) and final == matrix_decision and not re.search(r"DEVIATING FROM MATRIX", text, re.IGNORECASE),
            "deviation_reason": (m2.group(1).strip()[:300] if (m2 := re.search(r"DEVIATING FROM MATRIX because\s*([^\n]+)", text, re.IGNORECASE)) else None)}


if __name__ == "__main__":
    assert _decision_matrix("BULLISH", "HIGH", "HIGH") == "AGGRESSIVE LONG"
    assert _decision_matrix("BEARISH", "LOW", "LOW") == "CAUTIOUS SHORT"
    assert _decision_matrix("NEUTRAL", "HIGH", "HIGH") == "STAND ASIDE"
    assert _decision_matrix("BULLISH", "LOW", "HIGH") == "MODERATE LONG"
    b, s, w = _parse_judge("BULL SCORE: 7.5\nBEAR SCORE: 6.0\nWINNER: BULL\nRATIONALE: x")
    assert (b, s, w) == (7.5, 6.0, "BULL")
    t = _parse_trader("...\nFINAL DIRECTION: BEARISH\nFINAL CONFIDENCE: MEDIUM\nPOSITION PCT: 5-10%\nENTRY: 8100-8150\nSTOP: 8300\nTARGET: 7800",
                      {"BULLISH": [], "BEARISH": ["a", "b"], "NEUTRAL": []})
    assert t["direction"] == "BEARISH" and "position_pct" not in t
    t2 = _parse_trader("no machine lines here", {"BULLISH": ["a"], "BEARISH": ["b"], "NEUTRAL": []})
    assert t2["direction"] == "NEUTRAL" and t2["confidence"] == "LOW"
    print("decision self-check OK")
