# Trading Architect — Build Audit

> **Resolution status (2026-06-09):** This document reflects the codebase as of **2026-05-26**. Several findings below were **fixed after this audit**. Do not re-implement them — see the resolution table.

| Finding | Severity | Status | Fixed in |
|---|---|---|---|
| H-1 — `current_delta_notional` never computed | HIGH | **Fixed** | `assembly/positions.py` — `enrich_positions_with_marks` (docstring cites H-1, M-1); tested in `tests/test_position_enrichment.py` |
| M-1 — Closed positions retain stale risk | MEDIUM | **Fixed** | `assembly/positions.py` — risk zeroed on leg close |
| M-2 — Equity realized-only, not MTM | MEDIUM | **Fixed** | `engines/equity.py` — `open_mtm_pnl`, `_marks_with_provider`, live marks wired |
| M-3 — Account peak = sum of silo peaks | MEDIUM | **Fixed** | `engines/equity.py` — `combined_account_equity_curve`; `book_context.py` uses `account_equity_metrics` |
| M-4 — In-sample Kelly look-ahead | MEDIUM | **Fixed** | `engines/evaluation.py` — `_prior_epoch_optimal_f` for out-of-sample Kelly counterfactuals |
| M-5 option selector greek fallback | Done caveat | **Fixed** | `engines/options_selector.py` — `_contract_greeks` fills via Black-Scholes |
| AG-1 — Attribution needs MTM | Open gap | **Partially unblocked** | M-2 fix enables MTM; attribution wiring (AG-4) still pending |
| L-1–L-4, AG-2–AG-4 | LOW / gap | **Open** | See §4 recommended fix order below |

**Current source of truth for repo health:** [`Repo Audit & Improvement Plan.md`](Repo%20Audit%20%26%20Improvement%20Plan.md) (2026-06-09).

---

**Scope:** Adherence to *Phase 3 PRD v1.0* and code correctness of the current `src/trading_architect`, `app/`, and `tests/` tree.
**Date:** 2026-05-26
**Method:** Full read of every source module; ran the test suite (57 passed); independently verified Black–Scholes, optimal-f, and reproduced the flagged defects with live objects.
**Verdict:** The system is broadly faithful to the PRD and the hard non-goals are well respected. The math that exists is correct. The main problems are *exposure/equity state that is never fully computed* — which quietly disables the leverage ceiling and weakens the drawdown governor — plus a handful of smaller correctness and consistency issues. None are catastrophic; several are material because they affect the two ceilings and the MTM drawdown logic the PRD treats as core risk controls.

---

## 1. Summary scorecard (PRD §9 milestones)

| Milestone | PRD intent | State | Notes |
|---|---|---|---|
| M1 Ingestion & assembly | adapters, dedup, epochs, MTM equity | **Partial** | Adapters, dedup, review queue, epoch tagging all present and tested. `current_delta_notional` never computed; equity is realized-P&L only, not MTM. |
| M2 Evaluation (alpha) | counterfactual replay, R-dist, per silo/epoch | **Done, one caveat** | Correct linear-scaling counterfactual. Kelly rules use in-sample optimal-f (look-ahead). |
| M3 Sizing (6 layers) | fractional → vol → Kelly → equity → ceilings | **Done, one defect** | Layers compose correctly and rationale names the binding constraint. Leverage ceiling under-counts existing book exposure (see HIGH-1). |
| M4 Adaptive Kelly | bootstrap CIs, confidence-gated step-up | **Done** | Lower-CI gating, step-up ladder, "N more trades" messaging, drawdown override all implemented and tested. |
| M5 Option selector | on-demand chain, BS reprice, greek scoring | **Done, one dependency** | BS repricing exact. Greek scoring depends on the chain CSV carrying greeks; no local greek fallback. |
| M6 Streamlit UI | all views, no-manual-entry path, governor | **Done** | All nine pages present; governor surfaced; settings editor + override log working. |

Hard non-goals (PRD §2.2) are respected: no signals/direction/timing output, the option selector carries an explicit "not a probability / not a recommendation" disclaimer, silos are never blended, chain data is on-demand only, and there is no order placement. Good.

---

## 2. Correctness findings (by severity)

### HIGH

**H-1 — `current_delta_notional` is never computed, so the delta-notional leverage cap is effectively inert.**
`assembly/positions.py:builder_to_position` never sets `current_delta_notional`; it stays at the model default `0.0`, is persisted as `0.0`, and is summed back as `0.0` in `aggregate_open_exposure` (`sizing.py:179`) and `book_context.py:91`. Reproduced: an assembled 100-share + 5-call AAPL position reports `current_delta_notional = 0.0`.
Consequences:
- PRD FR-2.3 ("compute and display combined delta-adjusted notional, delta marked dynamically") is unmet for stored positions.
- FR-3.6's leverage cap — described in the PRD as *"the binding constraint for the options book"* — only sees the *candidate's* own notional; existing open exposure always reads as 0, so the cap will almost never bind in practice.
- Dashboard and Current Positions leverage columns always show 0×.
Why the tests miss it: `test_sizing.py:154` hand-sets `current_delta_notional=10_000.0` on a fixture, so the engine's cap path is tested with a value the assembly layer never actually produces.

### MEDIUM

**M-1 — Closed positions retain stale `stop_risk` / `premium_at_risk`, so `total_dollar_risk` is wrong after close.**
In `apply_event`, risk is only recomputed in branches guarded by `leg.net_qty != 0` / `> 0`. When a leg closes (`new_qty == 0`) the prior risk value is never zeroed. Reproduced: a fully closed 100-share MSFT position still reports `stop_risk = 500` and `total_dollar_risk = 500`. Open-book heat is unaffected (it filters `status == "open"`), but the Current Positions "closed/all" view and the persisted field are misleading.

**M-2 — Equity is reconstructed from realized P&L only; the drawdown governor is not truly marked-to-market.**
`equity.py:equity_metrics_for_silo` / `reconstruct_silo_equity_curve` sum realized P&L from closed positions and explicitly defer open-position marks (code comment: "until spot/chain inputs are wired"). PRD FR-7.1 and A.6 require **marked-to-market** peak-to-trough drawdown. As built, an open position bleeding 18% would not move the governor at all until it closes. This is a known deferral in the code, but M6 is marked "Done" and the governor is a core risk control, so it should be tracked as an open gap, not a finished feature.

**M-3 — Account-level peak equity is the *sum of per-silo peaks*, which overstates the true account peak.**
`book_context.py:121` sets `account_peak = stock.peak_equity + fut.peak_equity`. Silo peaks generally occur at different times, so their sum exceeds the real combined-curve peak, inflating account-level drawdown (FR-7.1). Account drawdown should be computed from a combined equity curve, then its running max taken.

**M-4 — Evaluation Kelly counterfactuals use optimal-f estimated in-sample from the same trades being replayed.**
`evaluation.py` computes `optimal_f` from the segment's own realized R-distribution (`r_dist.optimal_f`) and then sizes the ¼/⅓/½-Kelly counterfactuals off it. This is look-ahead/overfit: it asks "what if you'd sized at the Kelly fraction derived from knowing these exact outcomes." It will systematically *inflate* the "alpha left on the table" Kelly figures. PRD §1.4 and FR-6.2 require the dollar figure to be "credible and auditable." Recommend either deriving Kelly-f out-of-sample (e.g., prior-epoch or walk-forward) or labelling these rows clearly as in-sample/ceiling estimates. The fixed fractional rules (1%/2%) are unaffected and are the honest comparators.

### LOW

**L-1 — `optimal_f` floor of 0.005.** `_estimate_optimal_f` grid starts at 0.005, so a negative-edge segment returns 0.005 rather than 0 ("don't bet"). Reproduced with an all-losers sample → 0.005. Harmless today because the adaptive engine gates step-ups on the lower-CI threshold, but the reported per-segment optimal-f is slightly misleading for losing segments.

**L-2 — Evaluation options leverage proxy uses `premium × 100` as notional**, inconsistent with the delta-notional used by the live engine. Minor, but two different leverage definitions across modules undermines auditability.

**L-3 — Position assembly groups by `(silo, underlying)` only, dropping `direction`** (PRD §6.3 lists silo + underlying + direction). A long→short reversal on one name merges into a single position. Relatedly, `entries.py` FIFO matching ignores the overshoot when a single fill flips net direction (closes available lots, never opens the new opposite lot). Rare in real broker exports (usually separate fills), but worth a guard.

**L-4 — "Live f" is duplicated across two configs.** `SizingConfig.base_risk_f`/`kelly_fraction` (used by the sizing engine) and `AdaptiveRiskConfig.current_base_f`/`current_kelly_fraction` (used by the risk engine) are independent. Editing "Kelly fraction" under Settings → *Sizing & caps* does not change what the *Edge & Risk Review* treats as current Kelly, and vice versa. They can silently diverge; consider one source of truth.

### INFO / by-design

- **Greek scoring depends on the chain CSV providing greeks.** `options_pricing.py` implements price + implied-vol solve but **no greeks**; `options_selector.py` falls back to `delta≈0.4`, `theta=0`, `vega=0` when absent. So theta/vega/delta-fit scoring is inert unless the imported chain already carries those columns. PRD §6.4 does say greeks are "ingested on demand," so this is defensible, but a local BS greek computation would make FR-5.4 robust to thin chains.
- **Selector can surface ITM strikes.** Strike selection is anchored to the target ("strike or two under," per the trader's heuristic), which for a call with target > spot can include strikes below spot (ITM), slightly at odds with the "long OTM" profile. Matches the stated heuristic; flagging only for awareness.
- **`requires-python >=3.11`** in `pyproject.toml`; the repo `.venv` is 3.14, fine on your machine. (The sandbox only had 3.10, so I ran tests against `src` directly — all 57 pass.)

---

## 3. What is correct and well-built (verified)

- **Black–Scholes** matches textbook to 4 dp (ATM 1y call 10.4506, put 5.5735); handles T=0 intrinsic and zero-vol forward cases.
- **Optimal-f / Kelly** geometric-growth grid search is correct (recovers 0.25 for a +2/−1 even-money bet).
- **Bootstrap CIs**, lower-bound gating, step-up ladder, and "≈N more trades" messaging implement FR-4.1–4.5 faithfully and are seed-deterministic.
- **FIFO round-trip extraction** preserves each tranche, computes per-leg P&L with correct option ×100 and futures multipliers, and derives R-multiples with sensible risk-basis fallbacks (stop → median-loser proxy → fractional proxy).
- **Ingestion** is idempotent via a stable natural key (UNIQUE constraint), routes unparseable rows to a review queue instead of dropping them, archives raw CSVs, and isolates each broker in its own adapter (FR-1.1–1.4, NFR-6).
- **Separation of concerns** holds: no engine logic in the Streamlit layer; engines are headless-callable and tested (NFR-5, NFR-7).
- **Override log + epoch editor + settings persistence** satisfy FR-3.8 and §8.6.

---

## 4. Recommended fix order

1. **H-1** — compute `current_delta_notional` during assembly (shares×spot + Σ contracts×delta×100×spot) so the leverage cap and UI work on real data. Add a test asserting it is non-zero for a stock+option position. *(Restores a core risk control.)*
2. **M-2 / M-3** — feed open-position marks into equity and compute account drawdown from a combined curve, so the governor is genuinely MTM and account-level. *(Core risk control.)*
3. **M-1** — zero `stop_risk` / `premium_at_risk` on leg close so closed positions report `total_dollar_risk = 0`.
4. **M-4** — make the Kelly counterfactual out-of-sample (or label it explicitly) so the headline "alpha left on the table" figure is defensible.
5. **L-1–L-4** — opportunistic cleanup; **L-4** (single source of truth for live f) is the most user-visible.

---

*Findings only — no code was modified. Severity reflects impact on the PRD's stated risk controls and the credibility of the headline alpha figure, not implementation effort.*

---

## 5. Addendum — v1.1 methodology change (attribution-aware governor)

**Date:** 2026-05-26. **Change:** risk localized to the **Opportunity**; the drawdown governor is now **attribution-aware** (PRD v1.1 §7.3 FR-3.9, §7.7, A.10). Implemented and tested.

**Landed in code:**
- `engines/correlation.py` — `CorrelationProvider` protocol, `ConservativeCorrelationProvider` (assume-correlated default), `ReturnsCorrelationProvider` (Pearson with thin-data fallback), `cluster_underlyings` (connected-components), `max_correlation_to_group`.
- `engines/drawdown.py` — `build_attribution`, `candidate_drawdown_correlation`, `attribution_aware_throttle` (relaxes the uniform ramp by `(1 − corr_to_source)`; hard cap absolute).
- `engines/sizing.py` — `recommend_size(..., drawdown_correlation=1.0)`; uniform throttle replaced. **Default 1.0 = identical to the prior uniform governor**, so all prior behavior is preserved unless attribution is supplied.
- `config/sizing.py` / `config/defaults.py` — `attribution_aware_governor`, `correlation_threshold` (0.5), `assumed_correlation` (0.6).
- Tests: `tests/test_correlation_governor.py` (14 cases) + all 8 `test_sizing.py` still green.

**New open gaps created by this change (track alongside §4):**
- **AG-1 (depends on M-2).** Attribution needs *marked-to-market open P&L per underlying* to identify the losing cluster. Equity is still realized-only (M-2), so until open marks are wired, `build_attribution` has no losing cluster to find and `candidate_drawdown_correlation` returns the conservative `1.0` → governor behaves uniformly. **The attribution relaxation does not actually engage until M-2 is fixed.** This is safe (fails conservative) but means the headline benefit is latent until marks land.
- **AG-2 (data dependency).** `ReturnsCorrelationProvider` needs a per-underlying return series. No ingestion path supplies daily closes yet; an on-demand price-history snapshot (mirroring §6.4's chain snapshot) is the natural source. Until then the `ConservativeCorrelationProvider` is the active default (everything assumed correlated → uniform governor).
- **AG-3.** FR-3.6a (correlation-aware heat budget) is specified but `formulas.portfolio_heat` still sums all open risk naively. Cluster-aware heat is not yet implemented; clustering primitive exists, wiring does not.
- **AG-4 (UI).** `recommend_size` accepts `drawdown_correlation`, but `book_context` / the Size-a-Trade view do not yet compute and pass it. Wiring = build the attribution from open marks + provider at the call site (FR-7.5 plain-English reason in UI).

**Recommended sequencing:** fix **M-2** (MTM marks) first — it unblocks AG-1 and AG-4 — then AG-2 (price-history snapshot), then AG-3/AG-4. Until then the system is safe and unchanged in behavior by construction.
