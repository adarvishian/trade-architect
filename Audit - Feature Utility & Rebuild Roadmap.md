# Trading Architect — Feature Utility Audit & Rebuild Roadmap

**Date:** 2026-06-11 · **Scope:** every feature, audited against actual daily-use needs. Complements the existing code audits (`Repo Audit & Improvement Plan.md`, `Audit - Buildout vs PRD.md`), which cover code quality and PRD conformance — this one covers *whether the product is useful*.

## 1. Root cause

The app is **ledger-first**: it rebuilds positions from imported transaction CSVs/fetches and computes equity as *manual starting equity + reconstructed P&L*. Daily use needs it to be **account-first**: what do I hold right now, what is it worth, what cash do I have, what can I deploy.

Evidence of the mismatch in the live DB: starting equity still at defaults ($100k / $50k) while actual Schwab liquidation value was $114,306 — cached once on May 29 and stale since. Every number downstream (drawdown, heat %, sizing) inherits the error. Broker balance/holdings snapshots are fetched, displayed, and discarded — never persisted. Deposits, dividends, and interest are explicitly skipped on import, so cash is invisible by design.

## 2. Decisions locked (2026-06-11)

1. **Broker holdings are the source of truth** for current positions and equity. The transaction ledger remains only for historical evaluation (alpha-left, edge, R-distribution).
2. **Refresh:** auto-sync on app open + every ~15 min while open. Schwab unattended (OAuth refresh token); Robinhood once per session after MFA login; Tradovate/banks manual. Everything timestamped "as of".
3. **Cashflow:** two settings — avg monthly income after tax, avg monthly expenses. Net feeds deployable capital and projections. No recurring entry.
4. **Analysis pages folded into dashboard/trade flow.** Engines kept; standalone pages retired.

## 3. Feature-by-feature verdicts

### Pages (9 → 4)

| Page | Verdict | Why |
|---|---|---|
| Dashboard | **Rebuild** | Shows internals (event counts, reconstructed equity, caps tables). Must answer: where am I, where's risk, what can I allocate, how much per trade. |
| Import Data | **Refit → "Accounts"** | Becomes the account hub: linked accounts auto-sync, manual accounts (banks, Tradovate cash) CRUD, sync status/history. CSV import demoted to a "backfill history" expander (feeds evaluation only). |
| Size a Trade | **Keep + fix** | Core flow works. Wire to live holdings book context; absorb Option Selector as the options path. |
| Option Selector | **Fold into Size a Trade** | Engine kept (chain fetch + ranking). Becomes the options branch of the trade flow instead of a separate page. |
| Current Positions | **Fold into Dashboard** | Live holdings table with per-account breakdown is dashboard content, not a separate page. |
| Alpha Left on Table | **Fold** | One "sizing efficiency" card on dashboard (actual vs ¼-Kelly counterfactual, trailing). Engine kept, runs on ledger. Drill-down behind an expander. |
| Edge & Risk Review | **Fold** | Its only actionable output — step-up/hold/de-risk recommendation per silo — becomes a card feeding the per-trade number. Bootstrap details behind an expander. |
| Review Queue | **Fold into Accounts** | Currently read-only (can't resolve/dismiss). Becomes a badge + actions inside Accounts. |
| Settings | **Keep + fix** | Add cashflow + reserve fields. Starting equity demoted to fallback (used only when no broker sync exists). Fix the draft bug that silently drops account mappings before Save. |

### Subsystems

| Subsystem | Verdict | Notes |
|---|---|---|
| Schwab integration (OAuth, accounts, transactions, quotes, streamer) | **Keep — strongest asset** | Add: persist balance/holdings snapshots, scheduled refresh. Already handles hash mapping, OCC parsing, throttling. |
| Robinhood API (robin-stocks) | **Keep, eyes open** | Unofficial + fragile; MFA per session. Refresh per session only; degrade gracefully to last persisted snapshot with timestamp. |
| Tradovate CSV | **Keep** | Futures silo. Cash/equity handled via manual account entry. |
| 6-layer sizing engine | **Keep + rewire** | Feed with live silo equity instead of reconstruction. L3 vol layer is inert (`reference_atr` never set, no UI) — either add the field or remove the layer. `stop_price` never populated → stock heat invisible: add optional stop input at sync/position level. |
| Drawdown governor | **Keep + rebase** | Compute on synced equity high-water mark. Deposits inflate HWM — detect cash jumps at sync and prompt "deposit?" to adjust baseline (Phase 3). |
| Adaptive risk (Kelly step-up) | **Keep** | Output surfaces as dashboard card. |
| Alpha-left / R-distribution / equity reconstruction | **Keep, demoted** | Historical evaluation only. Reconstruction no longer drives current state. |
| Ingestion + dedup | **Keep** | Powers the ledger backfill. |
| Correlation governor | **Dormant — drop from roadmap** | Fully built, never wired, no price-history data source. Revisit only if a real need appears. |
| Vercel deployment (`asgi.py`, `vercel.json`) | **Cut** | Stateless deploy excludes the DB and token; OAuth callback is localhost-only. App is local single-user. Delete the config. |
| Dead code (`EquitySnapshot`, `strikes_above_target`) | **Delete** | |

## 4. New data model (additions)

- **`accounts`** — id, kind (schwab / robinhood / tradovate / manual), label, silo, institution, include_in_deployable (bool).
- **`balance_snapshots`** — account_id, ts, cash, equity_value, buying_power, source (api / manual). History persisted → real equity curve over time, deposit detection later.
- **`holdings_snapshots`** — account_id, ts, symbol/OCC, qty, mark, mtm_value, cost_basis, asset_type. Latest snapshot per account = current positions.
- **Settings additions** — monthly_income_after_tax, monthly_expenses, cash_reserve_months (default 6), refresh_interval_min (default 15).

## 5. Capital math (proposed, tunable)

- **Total capital** = Σ latest balance_snapshots (all accounts).
- **Trading equity (stock/options silo)** = Schwab + Robinhood equity value, live-synced. **Futures silo** = manual/Tradovate balance.
- **Monthly net cashflow** = income after tax − expenses.
- **Reserve** = cash_reserve_months × monthly expenses (held out of deployable, kept in bank/cash accounts).
- **Deployable capital** = brokerage cash + non-brokerage cash above reserve (flagged "transferable") + next month's expected net cashflow.
- **Per-trade number** = existing 6-layer engine on live silo equity, capped by available brokerage cash. Sizing uses brokerage equity only; transferable cash shown as headroom, not auto-counted.
- **Projection** = equity path forward at monthly net contribution + user-set return assumption → "your standard trade size in 6/12 months."

## 6. New Dashboard spec

Four blocks, top to bottom, every figure timestamped:

1. **Where am I** — total capital; per-account cards (value, cash, as-of, sync state); silo split.
2. **Where is risk** — open heat $ and % vs cap, drawdown state vs HWM, governor status, top 3 concentrations; per-position **open-R** (vs initial stop) with **"no stop" flags**; options book **theta bleed $/day** and **expiry runway** (premium expiring <30/60/90 DTE).
3. **What can I allocate** — deployable capital with its breakdown (brokerage cash / transferable / monthly inflow), reserve held back.
4. **What per trade** — standard size at current equity (¼ Kelly path), Kelly step-up status card, sizing-efficiency card, **adherence card** (recommended vs actually-taken size, trailing gap cost), **heat utilization** (trailing 90d avg heat used vs cap), one-click jump to Size a Trade.

## 7. Roadmap — 4 gated phases

**Phase 1 — Truth (foundation).** Accounts model + persisted balance/holdings snapshots; Schwab auto-sync on open + 15-min TTL; Robinhood per-session sync; manual accounts CRUD; holdings-first Current Positions data path; equity wiring into sizing/governor; Settings mapping bug fix. *Exit: open the app → accurate aggregate state, no clicks.*

**Phase 2 — Capital.** Cashflow settings; deployable capital + reserve math; projections; sizing capped by available cash; futures silo manual balance path. *Exit: "what can I allocate / what per trade" answers are correct and explainable.*

**Phase 3 — Surface.** New 4-block dashboard incl. open-R, theta/expiry runway, heat utilization, adherence card; fold Positions/Alpha/Edge/Option-Selector/Review-Queue per §3; deposit detection for HWM; delete Vercel config + dead code; page count 9 → 4. *Exit: daily use = open app, read dashboard, size trade.*

**Phase 4 — Edge & awareness.** Honest scoreboard + blind-spot risk, built on data Phases 1–3 accumulate: TWR vs SPY (net-deposit-adjusted), concentration clusters + stress line, earnings-date flags on held names, slippage-honest stop risk, exit-efficiency (MFE/MAE) analysis. *Exit: the app tells you whether the edge is real and where the book is fragile.*

Each phase ends with tests green + a working app, and **stops for explicit green light** before the next.

## 8. Out of scope (unchanged constraints)

No trade ideas or signals. No order placement or money movement. Futures and stock/options stay siloed. No tedious recurring data entry.

---

## 9. Execution plan — implementation-agent handoff

This section is self-contained instructions for an AI coding agent (Cursor). Execute **one phase at a time**; stop at each gate for Alex's review.

### 9.0 Operating rules (read first, apply always)

1. **Required reading before any code:** `role.md`, `context.md`, this document (§1–§8), `Audit - Buildout vs PRD.md` (known correctness issues), `README.md`.
2. **Stack & commands:** Python ≥3.11, Streamlit + SQLite. Run app: `streamlit run app/streamlit_app.py`. Lint: `ruff check .` Tests: `pytest`. CI (`.github/workflows/ci.yml`) runs both — keep green. Add tests for everything new; follow existing patterns (scrubbed fixtures in `tests/fixtures/schwab/`, **never live network in tests**).
3. **Branching:** one branch per phase off `dev` (`rebuild/p1-truth`, `rebuild/p2-capital`, `rebuild/p3-surface`, `rebuild/p4-awareness`). Commit in small task-sized increments.
4. **Live user data:** `data/trading_architect.db` contains real history. Run `python scripts/backup_db.py` before first migration. Migrations must be **additive** (follow the existing `content_fingerprint` migration pattern in `store/database.py`). Never drop/wipe tables.
5. **Secrets:** credentials live in `.env` and `data/.schwab_token.json`. Never commit, print, or log them. Never display full account numbers — reuse redaction helpers in `ingestion/schwab_accounts.py`.
6. **Hard product constraints:** no trade signals/predictions; read-only broker access (no orders, no transfers); futures and stock_options silos never blend; no recurring manual-entry workflows; every broker-derived figure carries an as-of timestamp; all sizing logic stays transparent (binding constraint always named).
7. **Don't break the ledger.** Ingestion/dedup/assembly/evaluation code paths stay functional — they power historical evaluation. You are adding an account-first current-state layer, not replacing history.
8. **Graceful degradation everywhere:** broker auth expired or network down → show last persisted snapshot + timestamp + warning, never crash. Map Schwab failures via existing `SchwabAuthExpired` handling in `ingestion/schwab_auth.py`.
9. **Gate protocol:** at phase end — tests green, app runs, exit criteria demonstrably met — STOP. Summarize what changed, list deviations from this spec, wait for explicit approval.

### 9.1 Phase 1 — Truth (branch `rebuild/p1-truth`)

**T1.1 — Schema + repository.** In `store/database.py` add tables: `accounts` (id, kind ∈ schwab|robinhood|tradovate|manual, label UNIQUE, silo ∈ stock_options|futures, institution, include_in_deployable INT, created_at), `balance_snapshots` (id, account_id FK, as_of, cash, equity_value, buying_power, source ∈ api|manual), `holdings_snapshots` (id, account_id FK, as_of, symbol, occ_symbol NULL, asset_type, qty, mark, mtm_value, cost_basis). In `store/repository.py` add: `upsert_account`, `list_accounts`, `record_balance_snapshot`, `record_holdings_snapshots` (batch, one as_of per sync), `latest_balance(account_id)`, `latest_balances()`, `latest_holdings(account_id | all)`, `balance_history(account_id)`. New test file `tests/test_accounts_store.py`.

**T1.2 — Persist Schwab snapshots.** Extend the existing snapshot path (`bootstrap.fetch_schwab_portfolio_snapshot` → `ingestion/schwab_accounts.py`) so every fetch upserts one `accounts` row per mapped account (kind=schwab, silo=stock_options) and writes balance + holdings snapshots. Reuse existing parsing (`SchwabAccountSnapshot`, `BrokerAccountBalance`, `HoldingLeg`); do not re-parse.

**T1.3 — Sync service.** New `src/trading_architect/services/sync.py`: `sync_all(repo, ttl_min) -> list[SyncResult]` where `SyncResult` = (account_label, status ∈ ok|stale|auth_required|error, as_of, message). Schwab: unattended via stored token; skip if latest snapshot younger than TTL. Robinhood: only if a session login exists (see T1.4), else status=stale with last snapshot age. Manual accounts: always status=ok with last manual as_of. Wire into the Streamlit app: run on startup and re-check TTL on rerun (use `st.fragment(run_every=...)` or a timestamp check — keep it simple); add `refresh_interval_min` (default 15) to `AppSettings` (`config/user_settings.py`). **Delete** the 30-min scalar cache `sync_schwab_live_equity` / `schwab_live_equity` settings key; all reads go through snapshots.

**T1.4 — Robinhood per-session sync.** After the existing UI login flow (`ingestion/robinhood_fetch.py`), persist balance + holdings snapshots (kind=robinhood, silo=stock_options) exactly as T1.2. Keep creds session-only. While logged in, include Robinhood in the 15-min cycle; after session ends, surface last-snapshot age.

**T1.5 — Accounts page.** Rename Import Data → **Accounts**. Top: per-account cards (label, institution, value, cash, as-of, sync status) + "Sync now" button. Middle: manual accounts CRUD — add account (label, institution, silo or cash-only, include_in_deployable) and "update balance" (amount + as-of date, source=manual). This is how banks and Tradovate cash enter the system. Bottom: existing CSV import + Robinhood/Schwab fetch forms collapsed into an expander titled "Backfill history (for evaluation)".

**T1.6 — Holdings-first positions.** New read path (suggest `services/current_state.py`): `current_positions(repo)` returns consolidated open positions from latest holdings snapshots across accounts (reuse `ConsolidatedHolding` consolidation logic), enriched with live marks via the existing marks provider (`engines/marks.py`). Current Positions page and `build_app_book_context` (in `bootstrap.py`) switch to this path. Ledger-assembled positions remain only for evaluation pages/engines.

**T1.7 — Equity wiring.** Silo equity = Σ latest `equity_value` of that silo's accounts (stock_options: Schwab+Robinhood snapshots; futures: manual/Tradovate accounts). Feed this into sizing (`recommend_size` call sites in `app/streamlit_app.py`), the drawdown governor, and the sidebar widget (`ui_helpers.render_governor_sidebar`). `starting_equity_*` settings become fallback only when a silo has zero snapshots — label them "(fallback)" in Settings.

**T1.8 — Bug fix.** Settings page: `st.session_state.settings_draft` is written by "Refresh account list" and "Add mapping" but never read back, so mappings are lost on `st.rerun()` unless saved first. Fix so draft state survives rerun or is persisted immediately.

**T1.9 — Ops hardening.** (a) Global banner (all pages, not just Settings) when the Schwab refresh token has <2 days left or any sync errored — if the token lapses silently, the entire truth layer goes stale. (b) Auto DB backup on app start: reuse `scripts/backup_db.py` logic, daily rotation, keep 14. Run before any migration too.

**Phase 1 exit criteria:** open app fresh → Schwab syncs automatically (or shows clear auth-required state); Dashboard/sidebar equity equals broker-reported values with as-of timestamps; manual account with balance can be created and appears in totals; positions list matches actual broker holdings (spot-check vs Schwab UI); `pytest` + `ruff` green; backfill/evaluation flows still work. **STOP for review.**

### 9.2 Phase 2 — Capital (branch `rebuild/p2-capital`)

**T2.1 — Cashflow settings.** Add to `AppSettings` + Settings UI: `monthly_income_after_tax`, `monthly_expenses`, `cash_reserve_months` (default 6). Existing override-log diffing covers audit automatically.

**T2.2 — Capital engine.** New `engines/capital.py`: `monthly_net_cashflow(settings)`, `reserve(settings)`, `deployable_capital(repo, settings) -> DeployableBreakdown` (brokerage cash + non-brokerage cash above reserve where `include_in_deployable` + next month's net cashflow; fields per §5). Pure functions, no I/O beyond repo reads. Unit tests incl. negative net cashflow, zero accounts, reserve > cash.

**T2.3 — Projection.** `project_equity(current_equity, monthly_contribution, annual_return_assumption, months) -> series`; UI shows 6/12-month projected equity and the standard trade size at those equities (reuse `recommend_size` with projected equity). Return assumption is a user input defaulting to 0% — **never** estimate or imply expected returns from edge history.

**T2.4 — Cash-capped sizing.** Cap recommended quantity by available brokerage cash in the relevant silo (options: premium×100×qty ≤ cash; stock: notional ≤ cash + margin buying power from latest snapshot). Surface as binding constraint `cash_available` in the existing breakdown. Implement as a post-layer in `engines/sizing.py` consistent with the 6-layer pattern.

**T2.5 — Stock stops → real heat + open-R.** `TradeEvent.stop_price` is never populated, so stock legs carry `stop_risk = 0` and portfolio heat understates risk. Add a lightweight per-position stop: editable stop column on the positions table, persisted (new `position_overrides` table: symbol, silo, initial_stop, current_stop, updated_at), feeding `stop_risk` → heat in book context. From it compute **open-R** per position: (mark − avg_entry) / (avg_entry − initial_stop), direction-aware; options legs use premium as R basis. No stop set → flagged "no stop" in UI, counted at zero risk but loudly warned.

**T2.6 — Inert L3 cleanup.** The volatility-normalization layer never fires (`reference_atr` defaults None, no UI). Remove it from the `recommend_size` flow (keep formula in `engines/formulas.py`); update the layer breakdown accordingly. Note removal in the phase summary for Alex.

**T2.7 — Recommendation capture (adherence groundwork).** Every "Recommend size" run persists a row in new table `size_recommendations` (ts, silo, underlying, asset_type, recommended_qty, dollar_risk, binding_constraint, inputs_json, silo_equity). Zero extra user action. Matching against actual fills and the dashboard card land in T3.6.

**Phase 2 exit criteria:** Settings hold income/expenses/reserve; deployable capital shows with full breakdown and matches hand calculation; projections render; a sizing run against low cash returns `cash_available` as binding constraint; stock stops set in UI move portfolio heat and produce open-R; recommendations persist; tests green. **STOP for review.**

### 9.3 Phase 3 — Surface (branch `rebuild/p3-surface`)

**T3.1 — New Dashboard** per §6: four blocks (Where am I / Where is risk / What can I allocate / What per trade), every figure timestamped, per-account cards, governor status, deployable breakdown, standard-size card with one-click jump to Size a Trade. Risk block includes: open-R column + no-stop flags (from T2.5), options **theta bleed $/day** (Σ position theta × 100 × qty from the marks provider's greeks) and **expiry runway** (premium-at-risk bucketed <30/<60/<90/≥90 DTE).

**T3.1a — Daily book metrics.** New table `book_metrics_daily` (date, silo, equity, open_heat, leverage, theta_day): written once per day at first sync. Powers **heat utilization** (trailing 90d avg heat/cap on the What-per-trade block) and future analysis. Optionally backfill heat history from the ledger if cheap; otherwise accumulate forward only.

**T3.2 — Folds.** Positions table (with stop editing + per-account breakdown) becomes a Dashboard section — retire Current Positions page. Option Selector becomes the options branch inside Size a Trade (chain fetch/ranking engine unchanged) — retire page. Alpha Left → "sizing efficiency" card + expander drill-down on Dashboard — retire page. Edge & Risk Review → step-up/hold/de-risk card (recommendation + reason + "N more trades needed") on Dashboard near the per-trade number — retire page. Review Queue → badge + expander in Accounts with **resolve/dismiss** actions (add repository methods; currently read-only).

**T3.3 — Deposit detection.** On each balance snapshot write, if cash jumps vs prior snapshot beyond a threshold (default: >$1,000 and >2%), queue a one-click prompt: classify deposit / withdrawal / market move. Classified deposits adjust the drawdown high-water-mark baseline so contributions don't mask drawdowns or inflate HWM. Store classifications (new small table `cash_events`).

**T3.4 — Cuts.** Delete: `asgi.py`, `vercel.json`, `[tool.vercel]` in `pyproject.toml`; dead `EquitySnapshot` class (`engines/equity.py`); dead `strikes_above_target` knob (`config/options_selector.py`). Final `PAGES` list = Dashboard, Size a Trade, Accounts, Settings.

**T3.5 — Docs.** Update `README.md` page descriptions and regenerate the user-guide section list (or mark `Trading Architect - User Guide.docx` outdated in README).

**T3.6 — Adherence matching + card.** Match `size_recommendations` (T2.7) to subsequent fills (same underlying + silo, first fill within N days, default 5) from the synced ledger. Compute taken-vs-recommended ratio and estimated gap cost (recommended qty × realized per-unit P&L − actual). Dashboard card: trailing 90d adherence % + "$ left on table from under-sizing". Unmatched recommendations expire silently — passing on a trade is a decision, not a violation; never nag.

**Phase 3 exit criteria:** 4 pages total; daily loop = open app → read 4 dashboard blocks → size a trade, zero imports/clicks required; review queue items resolvable; deposit prompt fires on a simulated cash jump (test with fixture); theta/expiry/open-R/heat-utilization/adherence visible; all prior evaluation functionality reachable via folds; tests + ruff green. **STOP for review.**

### 9.4 Phase 4 — Edge & awareness (branch `rebuild/p4-awareness`)

Built on data accumulated by Phases 1–3 (balance/holdings snapshots, cash_events, stops, book_metrics_daily). No new manual entry anywhere.

**T4.1 — TWR vs SPY.** Compute time-weighted return per silo and combined from `balance_snapshots`, using `cash_events` (T3.3) to neutralize deposits/withdrawals. Benchmark: fetch SPY close at each daily sync via existing Schwab quotes (`schwab_rest.py`), store in new `benchmark_prices` table. Dashboard card: YTD + trailing-12m TWR vs SPY total return. No annualizing under 3 months of data — label "insufficient history" instead.

**T4.2 — Concentration clusters + stress line.** Group open positions by underlying; optional sector tag per underlying (one-time, editable, default = underlying itself — no external data dependency). Risk block gains: top cluster % of book and "top cluster −15% gap ≈ −$X (−Y% of equity)" computed from delta notional. Arithmetic only; the gap % is a user-editable parameter, never a forecast.

**T4.3 — Earnings flags.** For held underlyings, show next earnings date when available. Data source is the weak point: try Schwab fundamentals endpoint first; if unavailable, use a manual per-ticker date field and mark the feature degraded. Do **not** add a scraping dependency. Display only — "NVDA earnings in 4d, 3 calls held" — no suggested action.

**T4.4 — Slippage-honest stops.** From the ledger, compare exit fills against recorded `current_stop` at exit time (requires stops history from T2.5 onward — accumulates forward). Maintain median slippage % per asset type; apply as a pad on `stop_risk` in heat ("heat (slippage-adjusted)" shown alongside raw). Until ≥10 observations, pad = 0 and label "calibrating".

**T4.5 — Exit efficiency (MFE/MAE).** For closed round-trips after Phase 1, reconstruct max favorable/adverse excursion from `holdings_snapshots` marks history while held. Report per closed trade: exit-R vs max-R reached, % of move captured. Aggregate: "trailing 20 closed trades captured median X% of MFE." This is the exit-side twin of Alpha Left; evaluation only, no exit rules suggested.

**Phase 4 exit criteria:** TWR vs SPY card correct against hand-checked numbers incl. one deposit; cluster stress line reacts to position changes; earnings dates render or degrade gracefully; slippage pad activates only with sufficient data; exit-efficiency report runs on post-rebuild closed trades; tests + ruff green. **STOP for final review.**

### 9.5 Kickoff prompt (paste into Cursor per phase)

> Read `role.md`, `context.md`, and `Audit - Feature Utility & Rebuild Roadmap.md` fully — §9.0 rules are binding. Execute **Phase N only** (§9.{N}) on branch `rebuild/pN-...`. Back up the DB before migrations. Work task by task with tests; keep `pytest` and `ruff check .` green. When exit criteria are met, stop and produce a summary of changes + deviations for review. Do not begin the next phase.

