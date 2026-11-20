# Intraday signal desk

The only active strategy is intraday. All new decisions specify direction, entry, stop, target, risk/reward and same-session expiry. A separate virtual-money simulator executes approved signals with deterministic sizing; account funds never alter signal eligibility. No broker orders are placed. Legacy strategy profiles exist solely for reading old records and respecting their recorded exit rules; they cannot enable new long-term issuance.

## Decision flow

Market feeds → completed five-minute/session context → analyst vote → debate → historical evidence → trader levels → risk → CIO → deterministic validation → intraday signal book.

Intraday analysis uses concise briefs to reduce the interval between observed prices and the entry decision. Bull, bear and judge reviews remain; a separate narrative-summary call is unnecessary because the judge's result already records the debate. Machine-readable decisions, full supplied evidence and numerical risk checks are preserved.

Direction follows the analyst vote. The debate can adjust confidence. Deterministic checks require a fresh quote for the same contract, a valid session, meaningful intraday history, properly bracketed levels and sufficient reward/risk. The observed quote determines entry and the R denominator.

Quotes and completed-bar evidence refresh before the trader, risk manager and CIO. When a new candle completes during the debate, the technical analyst reassesses its vote before the entry decision. The original report, revised report and stage-specific evidence are retained in the saved council result. Analysts distinguish directional bias from whether an entry has confirmed; waiting alone does not force a neutral vote.

The setup lifetime stays anchored to the trader's evidence timestamp; subsequent risk/CIO refreshes cannot extend it. Temporary quote errors clear once the feed recovers, even when the monitor is still waiting for a price pattern.

The engine computes the conditional setup clock once and supplies its original anchor and capped expiry to both reviewers. Refreshed market evidence has a separate timestamp. Reviewers use the canonical clock rather than rejecting a valid plan because they recalculated its duration from newer evidence.

The trader also receives a deterministic `trend_continuation_v1` candidate when completed candles show aligned MA5/20/60 and MACD, plus a three-bar breakout or MA5 reclaim. The fresh quote must belong to that contract, follow the completed candle and remain within 0.5 ATR of its close. A stop beyond the candle and at least 1.2 ATR away, together with a fixed 2.4-ATR projected target, must satisfy the existing reward/risk floor. The target is explicitly labelled a projection, never fabricated resistance; increasing the minimum reward/risk cannot extend it. This is an unoptimized proposal for the same council review, not a bypass or a proven profitable strategy. Reports retain the candidate; adopted signals and paper trades record their entry method.

A separate guard monitors stops, targets and deadlines every ten seconds. Gaps use observed prices rather than invented stop fills. Missing quotes produce an overdue-exit state and an explicitly labelled delayed exit. A new contract price never closes an older contract signal.

The paper simulator uses signal IDs for idempotent entries. It freezes each trade's lot size, margin, costs and risk budget, follows the signal's observed exits, and records skipped entries if the virtual wallet cannot afford a lot. Signal generation remains independent. Account balance includes booked net P&L; equity adds open net P&L. The original virtual capital is persisted once, with no reset on restart. Sizing during recovery only uses outcomes known at entry time.

The old ledger is never rewritten. A combined read view shows all original filled trades plus new paper trades. Existing legacy positions can have outcomes recovered from saved contract ticks, stored as separate records with recovery timestamps. Historical learning cannot use a recovered outcome before that timestamp. The dashboard separately exposes indicator history and recorded API usage/costs.

Conditional plans have a separate durable lifecycle. The trader supplies an exact JSON schema; risk and CIO independently approve it. Unsupported or incomplete conditions fail validation. Only full five-minute candles starting at/after approval can confirm a close-beyond, breakout-and-retest, or pullback/rejection trigger. The latter requires a level touch and a directional close without a prior breakout. The guard issues at a fresh observed quote inside the approved range, rechecking contract, session and reward/risk. It never backfills an earlier price. Version 2 plans can wait outside their entry range before confirmation; the missed-entry check considers only observations after confirmation. Stops and targets still cancel at any stage. Records without a rule version retain the original behavior. Plans expire from their evidence timestamp and follow their saved replacement policy. Preview runs cannot change them.

The setup lock serializes council replacement and monitor activation. New setups carry an explicit replacement policy: WAIT/STAND ASIDE or a rejected replacement cannot silently supersede a valid pending plan. Existing pending conditions reach trader, risk and CIO; explicit cancellation requires the exact setup ID, a reason and a successful model review. A validated new setup or issued signal supersedes prior plans. Previously stored plans retain their original replacement behavior. Previews never cancel plans. Signal records carry a setup ID for idempotent recovery if the process exits after saving a signal but before saving setup completion. Setup records and event histories remain available for later review, separately from issued-signal performance.

## Run visibility

The scheduler also runs a cheap opportunity scan every 15 seconds using the existing completed-bar continuation candidate. Ready candidates with a target that covers configured paper fees can launch the complete council between scheduled slots. A five-minute review cooldown, persistent candle/contract/direction deduplication and a budget of six extra reviews per day limit additional API usage. Existing active signals suppress extra entry reviews. The scanner never writes signals or paper trades; its current status and append-only event journal are stored under `events/`. Scheduled reviews continue when the extra-review budget is exhausted. No win rate, indicator threshold, stop or target was fitted to the small recorded sample.

The council can wait up to 25 seconds for a usable quote before the trader evidence refresh and immediately before entry registration. Each retry uses the existing quote validator. Final registration checks the actual current quote and all original session, confirmation, contract and reward/risk conditions. A contract change aborts the authorization; continued data failure does not extend plan validity or permit a stale entry. Reports record the recovery attempts; no paid model calls are repeated to recover the quote.

The OS council lock serializes analysis. A launch lock prevents double-click startup races, while per-run state exposes stages, completed analysts, fallbacks, failure and completion. UI requests use unbuffered detached processes. The dashboard checks process liveness rather than treating every button click as a successful run. Reports from previews and signal councils are ordered by save time and labelled separately.

## Historical evidence

Original ledgers, reports, signal books, market ticks and lesson files remain in place. The history screen reads all generations of records and provides original downloads. Legacy paper outcomes are normalized to points and R in memory only; the source files are never rewritten by retrieval.

Only completed same-day outcomes resolved before the new decision can support intraday outcome learning. A date-only exit becomes available after the entire recorded day, avoiding premature use. Saved lessons must have both resolution and writing times before the decision. Unfinished, future and multi-day outcomes are excluded; general decision-day reviews are labelled as historical reviews. Each report records selected evidence IDs for audit. New intraday metrics remain separate from approximate legacy execution history.

No automated rule tuning or claim of optimized profits is made. The stored evidence is available for future review and evaluation without losing the original record.

New intraday paper feedback is computed without model calls or source-file mutations. Only completed same-session trades, recorded and resolved before the decision, contribute. Each result includes its original frozen fees, net P&L, fee-adjusted planned reward/risk, net R (initial stop risk plus fees), observed stop gaps and source IDs. Malformed or inconsistent economics are excluded and counted. Legacy returns remain separate; signal and paper views of the same trade are never represented as independent evidence. Deterministic `L-P-...` evidence IDs and legacy lesson IDs can be cited by the trader and are archived in every new council report.

Entry feedback uses a bounded recent window and reconstructs setup state from events known at decision time. Missed targets are explicitly not counterfactual profits. The council receives counts of activated, missed and invalidated plans plus stale-quote review blocks. None of this bypasses confirmation or freshness checks.

Trader, risk and CIO receive configured per-barrel paper fees. The engine calculates net reward/risk for the exact proposal at fresh prices (or the worst conditional range endpoint) and refreshes this evidence for risk and CIO. New paper rows freeze that calculation; older rows are displayed using their recorded fees without rewriting them. These calculations inform council review and are separate from the existing gross signal floor; no new threshold, extra model call, target extension or fitted strategy rule is introduced. The History & learning tab exposes the current feedback snapshot and a download.
