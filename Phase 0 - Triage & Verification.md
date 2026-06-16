# Phase 0 — Triage & Verification

**Date:** 2026-06-16  
**Runtime:** Python 3.11.11 (`/opt/homebrew/bin/python3.11`)  
**Branch:** `rebuild/p5-remediation` @ `acae574`  
**PRD reference:** §10 D-V, §11 Phase 0 tasks 0.1–0.3

---

## 0.1 — Test suite

| Item | Result |
|---|---|
| Command | `python3.11 -m pytest -q` |
| Outcome | **280 passed** in ~25s |
| Expected (PRD) | ~169 (suite has grown with P1–P5 remediation tests) |
| Failures | None |

**Verdict:** Suite green on the supported runtime. No triage required.

---

## 0.2 — D-V verification

### M-2 — Open-position MTM feeding the drawdown governor

**Status: ENGAGED (not latent). Phase 5.1 not required.**

Open-position marks flow into silo equity, which drives drawdown % and the governor in `book_context`:

1. **`equity_metrics_for_silo`** (`engines/equity.py:239–303`) computes current equity as `starting + cumulative closed P&L + open realized + open_mtm_pnl(...)`. On the latest date it uses `_marks_with_provider` when a `marks_provider` is supplied; otherwise it falls back to the latest event price per symbol (cost basis when no mark exists).

2. **`build_book_context` → `_silo_state`** (`engines/book_context.py:96–119`) calls `equity_metrics_for_silo(..., marks_provider=marks_provider)` and passes the resulting equity into `compute_drawdown_pct` → `assess_drawdown_state`.

3. **Production wiring:** `build_app_book_context` (`bootstrap.py:58–90`) passes `default_marks_provider()` (or caller override) and overlays live broker snapshot equity when snapshots exist (`_snapshot_live_equity` → `live_stock_options_equity` / `live_futures_equity`).

4. **Regression test:** `tests/test_m6_ui.py::test_open_mtm_moves_equity_before_close` asserts that a partial close with a mark event moves equity by unrealized P&L before the position fully closes.

5. **Fallback visibility:** `mark_fallback_stats_for_book` surfaces when marks fall back to cost basis (`BookContext.mark_fallbacks`).

**Caveat:** When no marks provider is available and no mark events exist, open legs price at cost basis (zero open P&L contribution). This is documented in README “Known limitations”, not a realized-only equity bug.

---

### AG-1 / AG-4 — Attribution-aware drawdown throttle

**Status: LATENT in production paths. Phase 5.2 recommended.**

The engine implements attribution-aware throttling, but call sites do not supply correlation data, so production sizing always uses the conservative uniform governor (`drawdown_correlation = 1.0`).

| Layer | State | Evidence |
|---|---|---|
| **Engine (AG-1 logic)** | Built | `drawdown.py`: `build_attribution`, `candidate_drawdown_correlation`, `attribution_aware_throttle` |
| **Sizing integration** | Built, default conservative | `sizing.py:486–516` — `recommend_size(..., drawdown_correlation=1.0)`; uses `attribution_aware_throttle` only when `cfg.attribution_aware_governor` is True (default) |
| **Open P&L per underlying (AG-1 data)** | Not computed in production | `open_pnl_by_underlying` appears only in `tests/test_correlation_governor.py`; no service/bootstrap path aggregates MTM P&L by underlying for `build_attribution` |
| **UI / CLI wiring (AG-4)** | Not wired | `app/streamlit_app.py:653` and `cli.py:381` call `recommend_size(...)` without `drawdown_correlation` |
| **Unit tests** | Green | `tests/test_correlation_governor.py` proves relaxation when correlation is supplied manually |

**Effect today:** With book underwater, an uncorrelated candidate (e.g. GLD while tech cluster drives the drawdown) is throttled identically to a correlated one. Safe (fails conservative) but the headline attribution benefit is inactive.

**Phase 5.2 scope (if scheduled):** Compute `open_pnl_by_underlying` from marked open positions, build attribution via `CorrelationProvider`, pass `candidate_drawdown_correlation(...)` into `recommend_size` from CLI/Streamlit, and surface the plain-English throttle reason in the sizing rationale.

---

### Silent row loss — ingestion audit

**Status: `parsing.py` fallback is the only true silent-loss path. Other skips are intentional filters or counted.**

| Path | Behavior | Silent? |
|---|---|---|
| **`ingestion/parsing.py:79`** | When manual `csv.DictReader` yields zero rows, falls back to `pd.read_csv(..., on_bad_lines="skip")` | **Yes** — malformed lines dropped with no review-queue entry |
| **Primary CSV path (`parsing.py:67–77`)** | Manual DictReader; footer/disclaimer rows skipped | Intentional (non-data rows) |
| **Robinhood / Schwab / Tradovate adapters** | Non-trade rows (`SKIP_CODES`, non-`TRADE_ACTIONS`) `continue` without review | Intentional (dividends, transfers, etc.) |
| **Adapter parse exceptions** | Caught → `ReviewQueueItem` | Auditable |
| **Robinhood API (`robinhood_fetch.py`)** | Failures → review queue | Auditable (CQ-2 resolved) |
| **Schwab transactions (`schwab_transactions.py`)** | Unparseable → review queue | Auditable |
| **Dedup (`dedup.py`, `repository.py`)** | Duplicates skipped | Counted in `ImportResult.skipped_duplicates` |

**Verdict:** Confirms PRD §5.2 / Phase 4.2 target — route `parsing.py` skipped lines to the review queue. No other silent-loss paths found in ingestion.

---

## 0.3 — D4 risk anchors vs `config/defaults.py`

PRD §10 D4 recommendation: keep current anchors. Comparison:

| Anchor | D4 recommendation | `defaults.py` | `SizingConfig` default | Match? |
|---|---|---|---|---|
| Base risk f | 1% | `DEFAULT_BASE_RISK_F = 0.01` | 0.01 | ✓ |
| Kelly target | ¼ (½ via ladder) | `DEFAULT_KELLY_FRACTION = 0.25` | 0.25 | ✓ |
| Heat cap | 10% | `DEFAULT_HEAT_CAP = 0.10` | 0.10 | ✓ |
| Leverage cap | 2× | `DEFAULT_LEVERAGE_CAP = 2.0` | 2.0 | ✓ |
| Drawdown soft | 10% | `DRAWDOWN_SOFT_ALERT = 0.10` | 0.10 | ✓ |
| Drawdown throttle start | 15% | `DRAWDOWN_THROTTLE_START = 0.15` | 0.15 | ✓ |
| Drawdown hard | 20% | `DRAWDOWN_HARD_CAP = 0.20` | 0.20 | ✓ |
| Kelly lower-CI | (D3: live default) | — | `use_kelly_lower_ci: bool = True` | ✓ (already default ON) |
| Equity tiers | 0 / 150k / 250k / 500k | — | `EquityTier` list in `sizing.py:38–44` | ✓ |

**Deltas for owner sign-off:** None. All D4 anchors match shipped defaults. No config changes required before Phase 1.

**Note (D3, Phase 4 scope):** Live Kelly still uses in-sample optimal-f with a ≥3 trade gate (`sizing.py:_optimal_f_for_silo`). D3 recommends prior-epoch lower-CI Kelly with a 15-trade floor — tracked for Phase 4, not Phase 0.

---

## Milestone B / Phase 5 gate decision

| Phase 5 task | Gate | Decision |
|---|---|---|
| **5.1** — Wire MTM into equity for drawdown governor | M-2 latent? | **Skip** — M-2 engaged |
| **5.2** — Wire attribution-aware throttle in production | AG-1/AG-4 latent? | **Schedule** — engine built, production wiring missing |
| **4.2** — `parsing.py` silent skip → review queue | Confirmed only silent path | **Schedule** (Phase 4) |

**Suggested order:** Milestone A (Phases 1 → 2 → 3) first; Milestone B = Phase 4 + Phase 5.2 only.
