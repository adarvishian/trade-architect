# Product Requirements Document — Risk-Adjusted Position Sizing & Trade Evaluation System

**Project:** Trading Architect
**Document:** Phase 3 — Product Requirements Document (PRD)
**Version:** 1.1
**Date:** 2026-05-26
**Owner:** Alex (sole user/operator)
**Status:** Approved for development planning
**Companion document:** *Phase 1 — Position Sizing Research & Synthesis* (evidence base; this PRD operationalizes its conclusions)

**Revision note (v1.1):** Risk is now localized to the **Opportunity** (a single thesis on a single instrument) rather than governed by a single account-level drawdown hard-cap that uniformly taxes all new sizing. The drawdown governor is **attribution-aware**: it throttles a new opportunity only insofar as that opportunity is *correlated with the cluster currently driving the drawdown*. Whether two opportunities are independent bets is **measured**, not assumed, and correlated opportunities collapse into one *effective opportunity* for portfolio-risk attribution. The 20% marked-to-market hard cap is retained as an absolute, correlation-independent catastrophic backstop. See §7.3 (FR-3.9), §7.7, and Appendix A.10.

---

## 1. Overview

### 1.1 Purpose
This document specifies a local, single-user software system that helps a directional trend/momentum trader **size positions on a risk-adjusted basis**, **evaluate the cost of past sizing decisions** ("alpha left on the table"), and **systematically select directional options contracts** to express a pre-formed thesis. The system is self-calibrating: it learns the trader's edge from his own realized results and recommends, with statistical backing, when it is appropriate to increase risk as account equity grows.

### 1.2 Problem statement
The trader's edge (thesis, entry, exit) is sound and self-managed. The failure mode is **chronic under-sizing**: sizing intuition was calibrated to a smaller account and never scaled with equity growth, leaving meaningful gains uncaptured. Prior manual/spreadsheet attempts failed because they were tedious. The system must therefore be *faster and less effortful than the current process* while quantifying and closing the sizing gap.

### 1.3 Objectives
1. Produce a transparent, defensible **recommended position size** for any new trade, across both books.
2. **Quantify "alpha left on the table"** from historical sizing, in dollars, per book and per methodology epoch.
3. Provide a **data-driven risk-appetite recommendation** — tell the trader *when* he has earned the right to size up (base risk % and Kelly fraction), with confidence intervals.
4. **Systematize options contract selection** (strike + expiry) from the trader's own price target and horizon, using the greeks.
5. Enforce **risk ceilings** (drawdown, portfolio heat, leverage) automatically.
6. Ingest broker data with **near-zero manual logging**.

### 1.4 Success criteria
The system is successful if, and only if:
- The trader uses it consistently because each workflow is faster than his current manual method (the binding usability test).
- It shows, in dollars, how much additional return better sizing would have captured historically — and that figure is credible and auditable.
- It produces position sizes the trader trusts and acts on, and tells him clearly when to step risk up or down.
- It never requires tedious manual trade entry; broker CSVs are the source of truth.

---

## 2. Scope & non-goals

### 2.1 In scope
- Ingestion and normalization of CSV transaction exports from Robinhood (individual + IRA), Schwab/thinkorswim, and Tradovate.
- Position assembly into the two siloed books, including blended stock+options positions and multi-tranche ("bite") entries.
- Position sizing engine (fractional-risk chassis, volatility normalization, fractional-Kelly target, equity scaling, heat + leverage ceilings).
- Adaptive, confidence-gated risk-appetite & Kelly-fraction recommendation engine, segmented by methodology epoch.
- Options contract-selection module driven by user-supplied target price + horizon and the option greeks.
- Historical evaluation engine (counterfactual "alpha left on the table" replay).
- **Correlation-aware opportunity grouping**: measured pairwise correlation between underlyings collapses correlated opportunities into one *effective opportunity* so independent bets are counted once and concentrated bets are not double-budgeted.
- Marked-to-market, **attribution-aware** drawdown governor with a graduated de-risk ramp localized to the drawdown's correlated source, plus an absolute hard-cap backstop.
- Local Streamlit dashboards and quick-entry workflows.

### 2.2 Hard non-goals (never violate)
- **No trade ideas, signals, directional predictions, entry/exit logic, or market views.** The system never tells the trader *whether*, *which direction*, *when*, or *at what price* to trade.
- **Options contract selection is expression optimization, not prediction.** The user supplies the thesis (direction, price target, expected horizon). The system only recommends *how to structure* that view in contract terms. It does **not** estimate the probability that the target is reached, nor recommend taking the trade.
- **No live/streaming market data and no persistent broker connections.** Current option-chain data is pulled **on demand** (point-in-time snapshot) only when the trader is actively structuring a trade.
- **No automated order placement, execution, or money movement.** The system advises; the human acts.
- **No blending across silos.** Futures and stocks+options remain fully separate books with separate equity, risk budgets, and evaluation.

### 2.3 Out of scope (v1, candidate future work)
Tax-lot accounting, multi-user support, mobile app, broker order routing, real-time P&L streaming, non-directional/spread options strategies (system assumes long single-leg directional options).

---

## 3. Users & usage context

**Primary (only) user:** an experienced, quant-literate discretionary directional trader operating his own capital. Comfortable with statistical concepts (expectancy, confidence intervals, Kelly). Trades trend/momentum across two books:

- **Stocks + options book** — long equity with optional *long, naked, out-of-the-money* calls/puts (90–365 DTE) used as **directional leverage**, not hedges. A "position" is the blended directional exposure for one underlying. The trader **always uses a defined price-based stop on the stock leg** (treated identically to futures); on the options leg, **premium paid is the stop** (bounded max loss). He frequently **scales in via smaller tranches** and **opens independent new entries** on the same underlying at fresh technical levels.
- **Futures book** — separate silo, per-contract, with price-based stops, its own margin/notional/P&L characteristics.

**Recent context:** the trader changed his methodology ~late March 2026 (~60 days before this document), expected to materially improve performance. Post-change history is therefore a small but more-relevant sample — central to the adaptive engine's design (§7).

**Usage cadence:** daily quick checks (current exposure, size a new idea, structure an option) and a weekly edge/risk-appetite review.

---

## 4. Guiding principles

1. **Usability is a first-class risk control.** A system that gets abandoned has zero risk-adjusted value. Every workflow must beat the current manual process on speed. Smart defaults, minimal entry, quick views.
2. **Transparency & auditability.** Every number is explainable and traceable to its inputs and formula. No black boxes. The trader can inspect and override any parameter.
3. **Honest statistics over folklore.** Recommendations are grounded in the geometric-growth and R-multiple math from Phase 1; the system never overstates confidence on thin samples.
4. **Conservative by construction.** Because overbetting is far more destructive than underbetting, the system sizes off the *lower* bound of estimated edge and approaches the growth optimum gradually (¼ Kelly start).
5. **Separation of concerns.** Clean layers: ingestion → normalized store → engines → UI. Two siloed books throughout.
6. **80/20, never over-engineered.** Build the highest-value capability for the least ongoing effort.

---

## 5. System architecture

### 5.1 Stack
- **Core engine:** Python 3.11+ with pandas/numpy for data and analytics; scipy for resampling/statistics.
- **UI:** Streamlit (local web app) for dashboards and quick-entry workflows; the engine is also callable headless (CLI/notebook) for power use and testing.
- **Storage:** local, self-contained — a local SQLite database (normalized store) plus the raw CSV files retained for audit. No servers, no cloud dependency.
- **Options greeks/repricing:** a local Black-Scholes(-Merton) implementation for repricing candidate contracts at the user's target; chain/greeks ingested on demand (§6.4).
- **Distribution:** runs locally on the trader's machine; `pip`/venv managed.

### 5.2 Component diagram (logical)
```
        ┌──────────────────────────────────────────────────────────┐
        │                     Streamlit UI                          │
        │  Current Positions │ Size-a-Trade │ Option Selector │      │
        │  Edge & Risk Review │ Alpha-Left-on-Table │ Settings       │
        └───────────────▲──────────────────────────▲────────────────┘
                        │                           │
        ┌───────────────┴───────────────────────────┴────────────────┐
        │                       Engine layer                          │
        │  Sizing Engine │ Adaptive Risk Engine │ Option Selector │    │
        │  Evaluation Engine │ Drawdown Governor                       │
        └───────────────▲──────────────────────────▲─────────────────┘
                        │                           │
        ┌───────────────┴───────────┐   ┌───────────┴─────────────────┐
        │   Normalized Data Store    │   │  On-demand Chain Snapshot   │
        │   (SQLite): events,        │   │  (point-in-time, greeks/IV) │
        │   positions, epochs        │   └─────────────────────────────┘
        └───────────────▲───────────┘
                        │
        ┌───────────────┴───────────────────────────────────────────┐
        │  Ingestion & Normalization (per-broker CSV adapters)        │
        │  Robinhood (indiv + IRA) │ Schwab/thinkorswim │ Tradovate    │
        └────────────────────────────────────────────────────────────┘
```

### 5.3 Data flow
Broker CSV exports → per-broker adapter normalizes to canonical Trade Events → dedup & persist → Position Assembly groups events into logical Positions and tags Methodology Epoch → engines read the normalized store (and, for the Option Selector, an on-demand chain snapshot) → UI renders recommendations and evaluation.

---

## 6. Data model & ingestion

### 6.1 Canonical entities

**TradeEvent** (atomic fill)
| Field | Notes |
|---|---|
| event_id | surrogate key |
| broker, account | e.g., Schwab / thinkorswim; Robinhood-IRA |
| timestamp | execution time |
| symbol / underlying | canonical ticker |
| asset_type | `stock` \| `option` \| `future` |
| side | buy / sell (open/close inferred at assembly) |
| quantity | shares / contracts |
| price | fill price |
| fees | commissions + fees |
| option_spec | nullable: right (C/P), strike, expiry, DTE_at_entry, delta_at_entry, IV_at_entry (if available) |
| stop_price | user-supplied risk basis for the entry (stock/future); nullable for options (premium = stop) |
| raw_ref | pointer to source CSV row for audit |

**Position** (logical unit — source of truth from context.md)
| Field | Notes |
|---|---|
| position_id | surrogate key |
| silo | `stock_options` \| `futures` |
| underlying | ticker |
| direction | long / short |
| status | open / closed |
| component_events[] | constituent TradeEvents (stock + option legs, all tranches) |
| blended_cost_basis | per leg and combined |
| current_delta_notional | combined stock + option delta-adjusted notional exposure |
| premium_at_risk | sum of option-leg premium outstanding |
| stop_risk | sum of stock/future leg (entry−stop)×qty×multiplier across open tranches |
| total_dollar_risk | stop_risk + premium_at_risk |
| epoch_id | methodology epoch tag |
| realized_R, open_R | R-multiples (see Appendix A) |

**MethodologyEpoch**
| Field | Notes |
|---|---|
| epoch_id | e.g., `pre-2026-03-27`, `post-2026-03-27` |
| start_date, end_date | boundaries; user-editable |
| label, notes | description of the methodology change |

**ChainSnapshot** (on-demand, point-in-time — §6.4)
| Field | Notes |
|---|---|
| snapshot_id, underlying, asof_timestamp | provenance |
| spot_price, risk_free_rate | inputs |
| contracts[] | strike, expiry, DTE, bid/ask/mid, IV, delta, gamma, theta, vega, open_interest |
| iv_rank / iv_percentile | for vega/overpay assessment (if derivable) |

**AccountEquity / SiloEquity** (time series)
Marked-to-market equity per silo and at account level, reconstructed from events + (for open positions) latest marks; basis for fractional sizing, scaling, and the MTM drawdown governor.

### 6.2 Per-broker CSV ingestion
- Each broker gets a dedicated **adapter** mapping its export columns to the canonical TradeEvent schema. Adapters are isolated and independently testable so a broker changing its export format affects only one module.
- Confirmed available: full transaction history (entries + exits), enabling closed-trade reconstruction.
- **Idempotent & deduplicating:** re-importing overlapping CSVs must not double-count; dedup on a stable natural key (broker + account + timestamp + symbol + qty + price) with conflict reporting.
- **Validation & exception queue:** rows that don't parse cleanly (unknown symbols, option-spec parsing failures, missing fields) are surfaced in a review queue rather than silently dropped.

### 6.3 Position assembly
- Group TradeEvents by silo + underlying + direction into logical Positions; attach stock and option legs of the same directional view to one Position.
- **Multi-tranche handling:** each "bite" is preserved as its own TradeEvent (for independent per-entry evaluation) **but** rolled into the Position for exposure aggregation. Independent same-direction re-entries on one underlying aggregate into the Position's exposure for heat/leverage purposes (they are one concentrated bet), while remaining individually scored by the evaluation engine.
- Infer open/close and compute realized vs. open R per leg and blended.
- Tag each Position (and event) with its MethodologyEpoch by date.

### 6.4 On-demand option-chain snapshot
- Triggered only when the trader uses the Option Selector. Two supported sources, in priority order: (a) a **simple broker API call** (e.g., Schwab/thinkorswim or a comparable low-maintenance endpoint) where configured; (b) **manual import/paste** of a chain export. No streaming, no persistent connection.
- Each snapshot is stored with its `asof_timestamp` for auditability of any recommendation it produced.

---

## 7. Functional requirements

> Requirements are written as **FR-x.y**. Each is independently testable. Formulas referenced as **[A.n]** are defined in Appendix A.

### 7.1 Ingestion & normalization
- **FR-1.1** The system shall import CSV exports from each supported broker/account via its adapter and normalize to canonical TradeEvents.
- **FR-1.2** Imports shall be idempotent and deduplicated; re-importing shall not double-count.
- **FR-1.3** Unparseable or ambiguous rows shall be routed to a review queue with the reason and source reference; none shall be silently dropped.
- **FR-1.4** The system shall retain raw CSVs and a row-level audit link from every TradeEvent.

### 7.2 Position assembly & exposure
- **FR-2.1** The system shall assemble TradeEvents into logical Positions per the two-silo model, attaching stock and option legs of one underlying/direction to a single Position.
- **FR-2.2** The system shall aggregate all tranches and independent same-direction re-entries on an underlying into the Position's exposure totals, while preserving per-entry records for evaluation.
- **FR-2.3** For each Position the system shall compute, and display, **both** (a) total dollar risk (stop_risk + premium_at_risk) and (b) combined delta-adjusted notional exposure, with option delta marked dynamically (not frozen at entry). [A.4, A.5]
- **FR-2.4** The system shall reconstruct marked-to-market silo and account equity time series. [A.6]

### 7.3 Sizing engine (six layers)
The engine computes a recommended size for a candidate trade by composing the Phase 1 layers with the confirmed parameters.

- **FR-3.1 (Layer 1 — risk & exposure basis).** Given a candidate entry, the system shall compute dollar risk from the **user-supplied price stop** for stock/futures legs `(entry − stop) × qty × multiplier`, and from **premium-at-risk** for option legs; and shall compute the candidate's delta-adjusted notional. [A.1, A.4]
- **FR-3.2 (Layer 2 — fractional-risk baseline).** The system shall size so dollar risk equals `f × current_silo_equity`, using marked-to-market current equity so size scales automatically with account growth. [A.2]
- **FR-3.3 (Layer 3 — volatility normalization).** The system shall express and compare positions on a risk-per-unit-volatility basis (ATR/σ) for cross-instrument comparability; volatility is used for normalization only, never as a timing/alpha signal.
- **FR-3.4 (Layer 4 — fractional-Kelly target).** The system shall compute the growth-optimal fraction (optimal-f) from the trader's epoch-segmented realized R-distribution and apply the **current Kelly fraction (default ¼)** to set the target size, sized off the *lower confidence bound* of edge. [A.3, A.7]
- **FR-3.5 (Layer 5 — equity scaling).** The recommended size shall scale with current equity, banded by tiers to avoid churn; the system shall support the trader's independent-entry behavior by sizing each entry on its own merits while checking it against aggregate Position and silo exposure.
- **FR-3.6 (Layer 6 — hard ceilings).** The system shall enforce two ceilings per silo and throttle any recommendation that breaches either: (a) **portfolio heat** — total open dollar risk and per-correlation-cluster risk; and (b) **delta-notional leverage cap** — total combined notional exposure as a multiple of silo equity (the binding constraint for the options book). The leverage cap is a **structural** silo-level bound (shared margin/notional) and is retained independent of the drawdown governor. [A.4, A.8]
- **FR-3.6a (correlation-aware heat budget).** Heat shall be computed with correlated opportunities summed as **one effective opportunity** (cluster) so that N opportunities that are really one bet consume one budget, and N genuinely independent opportunities are not penalized as if they were one. Clusters are formed by the same measured-correlation grouping as FR-3.9. [A.8]
- **FR-3.7** Every recommended size shall be accompanied by a plain-English rationale citing the binding constraint (e.g., "capped by 2× leverage limit, not by per-trade risk").
- **FR-3.8** All parameters (f, Kelly fraction, ATR multiple, heat cap, leverage cap, tier widths, correlation threshold, assumed correlation) shall be user-inspectable and user-overridable, with overrides logged.
- **FR-3.9 (opportunity as the localized risk unit).** The system shall treat the **Opportunity** — a single directional thesis on a single instrument, comprising all same-thesis tranches/legs/re-entries — as the unit of risk localization. Each Opportunity carries a **bounded max loss fixed at entry** (premium-at-risk for options; `(entry − stop) × qty × multiplier` for stock/futures), and is sized bottom-up on its own edge and its own bounded loss; **portfolio drawdown does not enter an Opportunity's standalone size equation** except via the attribution-aware governor (§7.7) and the correlation-aware heat budget (FR-3.6a). For portfolio-level risk, Opportunities whose underlyings are **measurably correlated at or above the configured threshold** shall be grouped into one *effective opportunity* (cluster) and budgeted as a single bet; independence is measured from return history, and when return data is unavailable distinct underlyings are assumed correlated (conservative default) until data proves otherwise.

### 7.4 Adaptive risk & Kelly-fraction recommendation engine (headline)
- **FR-4.1** The system shall estimate the trader's edge per silo and **per methodology epoch** from the realized R-distribution, producing point estimates **and confidence intervals** via bootstrap resampling. [A.7]
- **FR-4.2** The live recommended base risk `f` and Kelly fraction shall be derived from the **lower confidence bound** of edge, making recommendations conservative by construction.
- **FR-4.3** The system shall track **evidence accumulated since the most recent methodology epoch** (trade count / statistical power) and shall recommend **stepping the Kelly fraction up** (¼ → ⅓ → ½ …) or raising `f` **only when** the post-change lower confidence bound clears a configured threshold.
- **FR-4.4** When the sample is insufficient, the system shall say so explicitly and quantify what's needed (e.g., "≈N more closed trades at current expectancy before I'd recommend ⅓ Kelly").
- **FR-4.5** Recommendations to change risk appetite shall be presented in plain English with the supporting statistics and the *why*.
- **FR-4.6** The engine shall never recommend exceeding the drawdown-implied ceiling (§7.7) regardless of edge estimates.

### 7.5 Options contract-selection module (new)
Turns the trader's heuristic ("strike or two under target; 90–365 DTE by feel") into a transparent, greeks-based recommendation. **Boundary:** all directional inputs are user-supplied; the system optimizes expression only and does not estimate probability of success or advise whether to trade.

- **FR-5.1 (inputs).** The trader shall supply: underlying, direction (long call / long put), **target price**, **expected horizon to target** (and optionally an expected path qualifier, e.g., fast vs. gradual). The system shall fetch/import an on-demand ChainSnapshot.
- **FR-5.2 (candidate set).** The system shall enumerate candidate contracts: strikes around/under the target consistent with the trader's heuristic, and expiries within a DTE band derived from the stated horizon plus a configurable buffer (so expiry comfortably exceeds the expected hold, limiting end-of-life theta/gamma risk).
- **FR-5.3 (repricing at target).** For each candidate, the system shall reprice the option at the user's target price and reduced DTE using Black-Scholes, under a **transparent, user-toggleable IV assumption** (default: hold IV constant; optional modest IV decline), and report projected value, projected return %, projected R-multiple if target is hit, and max loss if the thesis fails (premium). This is a modeling aid, explicitly **not** a probability or prediction. [A.9]
- **FR-5.4 (greek-based scoring).** The system shall score and rank candidates on transparent, **user-weighted** dimensions: cost/leverage efficiency (premium vs. delta-notional), projected payoff at target, theta drag over the expected hold, IV/vega risk (IV rank/percentile and vega), delta/gamma responsiveness, and breakeven cushion vs. target.
- **FR-5.5 (caps integration).** Each recommended contract's resulting delta-notional and premium-at-risk shall be checked against the silo's leverage cap and heat budget; the module shall surface the sizing (number of contracts) consistent with §7.3.
- **FR-5.6 (explanation).** The output shall be a ranked shortlist with the full greek profile and an explanation of each tradeoff (e.g., "lower strike = higher delta and cost, less leverage; higher strike = cheaper and more leverage but more theta risk and needs a larger move"), replacing "feel" with auditable reasoning the trader controls.

### 7.6 Evaluation engine — "alpha left on the table"
- **FR-6.1** The system shall replay historical closed Positions holding the trader's **actual entry/exit timing and prices fixed**, varying only **size**, to compute counterfactual P&L. It shall never alter, second-guess, or generate entry/exit decisions.
- **FR-6.2** It shall compare realized P&L at actual size vs. counterfactual sizing rules (mechanical fractional at various `f`, ¼/⅓/½-Kelly-capped, leverage-capped) and report the **dollar gap** ("alpha left on the table"), per silo and per epoch.
- **FR-6.3** For the options book it shall isolate and report the dominant source of missed gains — being too small on the rare large winners — distinctly from the stock book.
- **FR-6.4** It shall report realized expectancy and the full R-distribution (mean, dispersion, skew, win rate, tail) per silo and per epoch.
- **FR-6.5** All counterfactuals shall be reproducible and inspectable down to the per-trade contribution.

### 7.7 Drawdown governor (attribution-aware)
- **FR-7.1** The system shall compute **marked-to-market** peak-to-trough drawdown per silo and at the account level.
- **FR-7.2** It shall implement a **graduated de-risk ramp** against the 20% account tolerance: soft alert ~10–12%, automatic size throttling beginning ~15%, hard de-risk approaching 20% (thresholds configurable). The ramp applies to whichever level (silo or account) trips first.
- **FR-7.3 (attribution-aware throttling).** While in a throttled state, the governor shall **attribute** the drawdown to the correlation cluster(s) currently driving it (clusters with net-negative marked-to-market open P&L), and shall throttle a *new* opportunity **only in proportion to that opportunity's correlation to the drawdown source**. A measurably **uncorrelated** opportunity (correlation to the source ≈ 0) shall be sized at its full standalone edge even while the book is underwater; a **fully correlated** opportunity (≈ 1) receives the full uniform ramp. Formally, `effective_throttle = uniform_throttle + (1 − uniform_throttle) × (1 − corr_to_source)` within the soft/throttle bands (Appendix A.10).
- **FR-7.3a (conservative default).** When the drawdown cannot be attributed to any open losing cluster, or when correlation data is unavailable, the candidate's correlation to the source shall default to **1.0** — i.e., the governor falls back to the uniform ramp. Relaxation is permitted only on **positive evidence** of independence.
- **FR-7.4 (absolute hard-cap backstop).** Approaching/at the 20% hard cap, **all** new risk shall be suspended **regardless of correlation**. The hard cap is not subject to attribution relaxation; it is the catastrophic-loss backstop.
- **FR-7.5** While throttled, risk-appetite recommendations (§7.4) shall be reduced/suspended accordingly, with the reason shown. The governor's current state, the attributed drawdown source, and any per-candidate relaxation shall be visible in the UI, with the binding reason in plain English (e.g., "drawdown 17% from the semis cluster; this energy idea is uncorrelated → sized at full edge").

### 7.8 UI & workflow (Streamlit)
- **FR-8.1 Current Positions view:** every open Position with blended exposure, dollar risk, delta-notional, and status against heat/leverage caps and the drawdown ramp.
- **FR-8.2 Size-a-Trade view:** minimal input (underlying, entry, stop or premium, direction) → recommended size in seconds with rationale and binding constraint.
- **FR-8.3 Option Selector view:** the §7.5 workflow — inputs → ranked contract shortlist with greeks and tradeoffs.
- **FR-8.4 Edge & Risk Review view:** weekly readout of epoch-segmented expectancy, confidence, and the current risk-appetite recommendation ("hold / step up to ⅓ Kelly / de-risk").
- **FR-8.5 Alpha-Left-on-the-Table view:** the evaluation engine's dollar-gap output with drill-down.
- **FR-8.6 Settings view:** all parameters, epoch boundaries, broker adapters, and override log.
- **FR-8.7** The import-to-insight path shall require no manual trade entry: drop CSVs → normalized → views update.

---

## 8. Non-functional requirements
- **NFR-1 Usability/speed:** the size-a-trade and option-selector workflows must each complete in seconds with minimal input; this is the system's primary acceptance gate.
- **NFR-2 Local & private:** all data and computation local; no cloud storage; broker API use limited to on-demand, low-maintenance calls.
- **NFR-3 Auditability:** every recommendation reproducible from stored inputs (events, snapshots, parameters) and documented formulas.
- **NFR-4 Robustness:** ingestion tolerates malformed rows without data loss (review queue); engines degrade gracefully on thin data (widen CIs, withhold step-up recommendations).
- **NFR-5 Testability:** engine callable headless; deterministic given inputs; unit + regression tests on adapters and formulas (Appendix A) with known fixtures.
- **NFR-6 Extensibility:** new broker = new adapter only; sizing layers and scoring weights are configuration, not code changes.
- **NFR-7 Maintainability:** clean separation of ingestion / store / engine / UI; no engine logic in the UI layer.

---

## 9. Build phases & milestones
Ordered so value lands early and live dependencies come last.

1. **M1 — Ingestion & position assembly.** Broker adapters, normalized store, dedup, position assembly, epoch tagging, MTM equity reconstruction. *Exit:* one year of history for all accounts imported and reconciled.
2. **M2 — Evaluation engine.** R-distribution analytics and counterfactual "alpha left on the table," epoch-segmented. *Exit:* credible dollar figure for historical under-sizing, per silo/epoch. (Highest-value, no live data — delivered before sophistication.)
3. **M3 — Sizing engine.** Six layers with confirmed parameters; heat + leverage ceilings; rationale output. *Exit:* trusted size recommendation for a new trade in either silo.
4. **M4 — Adaptive risk & Kelly engine.** Bootstrap CIs, confidence-gated step-up logic, plain-English risk-appetite recommendations. *Exit:* system tells the trader when to change risk, with statistics.
5. **M5 — Options contract selector.** On-demand chain snapshot, BS repricing at target, greek scoring/ranking. *Exit:* ranked contract shortlist from target + horizon inputs.
6. **M6 — Streamlit UI & workflow polish.** All views; the no-manual-entry path; speed tuning to pass NFR-1.

---

## 10. Acceptance criteria (mapped to success criteria §1.4)
- **AC-1** From raw broker CSVs, the system reconstructs closed trades and reports historical "alpha left on the table" in dollars, per silo and per epoch, with per-trade drill-down. *(Obj 2)*
- **AC-2** For a new trade in either silo, the system returns a recommended size with rationale in seconds from minimal input. *(Obj 1, NFR-1)*
- **AC-3** The system produces a current base-`f` and Kelly-fraction recommendation from the lower confidence bound of epoch-segmented edge, and correctly withholds step-up recommendations on insufficient post-change data. *(Obj 3)*
- **AC-4** Given a target price + horizon and an on-demand chain, the system returns a ranked contract shortlist with greeks, projected payoff at target, and tradeoff explanations — without asserting any probability of success. *(Obj 4, §2.2)*
- **AC-5** Heat, leverage, and the MTM drawdown ramp automatically throttle recommendations, with the binding constraint always shown. *(Obj 5)*
- **AC-6** The full import-to-insight path requires zero manual trade entry. *(Obj 6, NFR-1)*
- **AC-7** No system output ever constitutes a trade idea, direction, timing, or price recommendation. *(§2.2)*

---

## Appendix A — Methodology & formulas

**A.1 Dollar risk (stock/futures leg).** `risk = (entry − stop) × qty × multiplier` (multiplier = 1 for stock; point value for futures). Sign handled by direction.

**A.2 Fractional-risk size.** `size = (f × current_silo_equity_MTM) / risk_per_unit`, where `f` is the base risk fraction and equity is marked-to-market.

**A.3 R-multiple.** Initial risk on a position = `R` (A.1 for stock/futures; premium paid for options). Outcome expressed as realized P&L / R. Expectancy = mean realized R.

**A.4 Delta-adjusted notional (exposure).** `exposure = shares × spot + Σ(option_contracts × delta × 100 × spot)`; delta marked dynamically. Optionally beta-weighted to a benchmark: multiply each name's exposure by its beta for portfolio-level aggregation.

**A.5 Blended position risk.** `total_dollar_risk = stop_risk(stock/future legs) + premium_at_risk(option legs)`.

**A.6 Marked-to-market equity.** Realized cash from closed events + current marks of open legs (stock at spot, options at mark/model price), per silo and aggregated.

**A.7 Edge estimation with confidence.** From the epoch-segmented realized R-distribution, compute optimal-f (the fraction maximizing the geometric mean of outcomes) and expectancy; obtain confidence intervals by **bootstrap resampling** the R-distribution. Live sizing uses the **lower CI bound**; step-ups gate on the lower bound clearing thresholds with adequate post-epoch sample size.

**A.8 Portfolio heat & leverage.** `heat = Σ open dollar risk / silo_equity`, with correlated opportunities in a cluster summed as one effective opportunity (correlation-aware, per A.10 clustering). `leverage = Σ delta-adjusted notional / silo_equity`, capped at the user-set multiple (a structural silo bound).

**A.9 Option repricing at target.** Black-Scholes(-Merton) value of each candidate at `S = target_price`, `T = max(0, DTE_at_decision − expected_hold)`, under the selected IV assumption (default constant IV; optional decline). Reports projected value, return %, R-multiple if target hit, and premium as max loss. Explicitly a deterministic *what-if at the user's stated target*, not a probability-weighted expectation.

**A.10 Opportunity correlation, clustering & attribution-aware throttle.**
- *Measured correlation.* Pairwise correlation `ρ(a,b)` between two underlyings is the Pearson correlation of their aligned periodic return series. When a series is missing or has fewer than a configured minimum of overlapping observations, `ρ` falls back to the **assumed correlation** (default 0.6) rather than asserting independence.
- *Clustering.* Underlyings form a cluster (one *effective opportunity*) when connected by pairwise `ρ ≥ correlation_threshold` (default 0.5) — i.e., connected components of the threshold graph.
- *Attribution.* A cluster drives the drawdown when its aggregate marked-to-market open P&L `< 0`. The set of underlyings in such clusters is the **drawdown source**.
- *Candidate correlation to source.* `corr_to_source = max ρ(candidate, s)` over members `s` of the drawdown source; `1.0` if the drawdown is unattributable (conservative).
- *Attribution-aware throttle.* Let `u = uniform_throttle(drawdown_pct)` be the graduated band multiplier (FR-7.2). Within the soft/throttle bands the applied multiplier is `m = u + (1 − u) × (1 − corr_to_source)`, bounded to `[u, 1]`. At/above the hard cap, `m = 0` unconditionally (FR-7.4). With `corr_to_source = 1` this reduces exactly to the uniform governor `u`.

---

## Appendix B — Glossary
- **Silo:** one of the two fully separate books (stocks+options; futures).
- **Position:** the logical, blended directional exposure for one underlying, assembled from one or more tranches and legs.
- **Opportunity:** the unit of localized risk — a single directional thesis on a single instrument, comprising all same-thesis tranches/legs/re-entries, with a bounded max loss fixed at entry. Operationally a Position; the term emphasizes that risk is owned and sized at this thesis level. Two distinct theses on the same instrument over time are two Opportunities.
- **Effective opportunity (cluster):** a group of Opportunities whose underlyings are measured-correlated at/above the threshold, budgeted as one bet for heat and drawdown attribution.
- **Attribution-aware governor:** a drawdown governor that throttles a new Opportunity only in proportion to its correlation with the cluster driving the current drawdown; uncorrelated Opportunities are sized at full edge, with the 20% hard cap retained as an absolute backstop.
- **Epoch:** a methodology regime used to segment performance (pre/post the ~late-March-2026 change).
- **R / R-multiple:** initial dollar risk on a trade / outcome expressed in units of that risk.
- **Optimal-f / Kelly fraction:** growth-optimal bet fraction / the conservative multiple of it actually used (default ¼).
- **Heat:** total open risk as a % of silo equity. **Leverage cap:** total delta-notional as a multiple of silo equity.
- **Premium-at-risk:** max loss on a long option (the premium paid) — the options-leg stop.
- **Alpha left on the table:** the dollar gap between realized P&L at actual size and P&L under a better sizing rule, holding the trader's entry/exit decisions fixed.

---

*End of PRD v1.0. Ready for development per the phased plan in §9.*
