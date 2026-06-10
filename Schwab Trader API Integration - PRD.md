# Schwab Trader API Integration — PRD

**Project:** Trading Architect
**Component:** Schwab Trader API — Individual (Market Data + Accounts & Trading)
**Date:** 2026-05-29
**Status:** Draft for review. Developer portal access **granted**. REST endpoints and the account balances/positions schema are now **confirmed** from the OpenAPI Specifications docs; remaining unknowns are limited to a few leaf field names and Streamer numeric-field indices (see §13).
**Author:** Quant Trading Systems Architect (for Alex)
**Supersedes:** *Schwab Data Adapter — Design & Scope.md* (written pre-access, REST/on-demand assumption). This PRD is net-new and assumes a **Streamer-based** market-data model.

---

## 1. Objective

Stand up two live Schwab Trader API connections so Trading Architect can mark the stock/options book to market without manual CSV exports:

1. **Market Data — Streamer (WebSocket).** Real-time Level 1 quotes for **stocks** (`LEVELONE_EQUITIES`) and **options with full greeks** (`LEVELONE_OPTIONS`), driving marked-to-market (MTM) equity, the drawdown governor, and the delta-notional leverage cap.
2. **Accounts (REST).** A live **balances + positions snapshot** (current cash, buying power, open positions) and **transaction history** pulled into canonical `TradeEvent`s.

This directly unblocks the risk controls that are currently inert because the system has no live mark: the 20% MTM drawdown governor, the long-OTM-options leverage cap (delta-notional), and accurate "alpha left on the table" attribution against true equity.

This integration serves the **stock/options silo only.** Futures remain siloed on Tradovate (CSV), per the project's never-blend constraint.

---

## 2. Goals & Non-Goals

### Goals

- Live stock and option marks (incl. delta/gamma/theta/vega/IV) via the Streamer, on demand and low-friction.
- Live account balances and open positions snapshot for true MTM equity per silo and a combined account drawdown curve.
- Hands-off transaction ingestion that lands in the existing dedup/assembly pipeline.
- A single, auditable OAuth session layer with a clean re-authentication path (refresh tokens expire weekly — see §4).
- Graceful degradation: with Schwab unconfigured or disconnected, every existing CSV/offline workflow still runs unchanged.

### Non-Goals (never)

- **No order placement, execution, modification, or cancellation.** The Accounts & Trading product *can* place orders; this system must contain **no** POST/PUT/DELETE order code path. (Reaffirms project constraint and the existing PRD §2.2.)
- **No money movement / transfers.**
- **No trade signals, entries, exits, or directional views.** Marks and balances only.
- **No futures via Schwab** — futures stay on Tradovate.
- **No multi-user / multi-tenant** — single local user.

---

## 3. Background & Current State

Alex was approved for the Schwab Developer Portal (Trader API — Individual), giving production access to the **Market Data** and **Accounts & Trading** products. Two documentation files are in the project:

- **Market Data Documentation** → the **Streamer (WebSocket) API** spec: streaming services, connection/command protocol, response codes, and Level 1 field definitions for equities and options.
- **Market Data Specs** (OpenAPI) → the **REST** market-data endpoints (server `https://api.schwabapi.com/marketdata/v1`): `/quotes`, `/chains`, `/expirationchain`, `/pricehistory`, `/movers`, `/markets`, `/instruments`, plus all quote/chain schemas.
- **Accounts Documentation** → the **OAuth 2 security model**, token lifecycle, options symbology, and order-placement samples (the latter explicitly out of scope here).
- **Account App Specs** (OpenAPI) → the **REST** accounts endpoints (server `https://api.schwabapi.com/trader/v1`): `/accounts/accountNumbers`, `/accounts`, `/accounts/{accountNumber}`, `/accounts/{accountNumber}/transactions`, `/userPreference` — with the full balances/positions schema. (Order endpoints exist here but are deliberately excluded — §2.)

The endpoints and the account schema are now **confirmed**. Remaining unknowns are narrow: the exact leaf field names inside the `/quotes` and `/chains` responses (the Swagger schema names are known — `QuoteOption`, `OptionChain`, `OptionContract` — but the rendered spec did not expand every leaf), and the Streamer's numeric field indices. Both should be locked against a recorded live response before coding (§13).

The existing code scaffold is intentionally **disregarded** for this PRD (per decision). Treat this as a clean specification; reconciliation with any existing modules is an implementation detail left to the build phase.

---

## 4. Authentication & Security (confirmed from Accounts doc)

Schwab uses **OAuth 2 three-legged authorization** (Consent-and-Grant via Schwab's Login Micro Site). Confirmed facts:

| Item | Value (from doc) |
|---|---|
| Base host | `https://api.schwabapi.com` |
| Authorize URL | `GET https://api.schwabapi.com/v1/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={CALLBACK}` |
| Token URL | `POST https://api.schwabapi.com/v1/oauth/token` |
| Token auth | HTTP Basic header: `Authorization: Basic base64(client_id:client_secret)` |
| Grant (initial) | `grant_type=authorization_code&code={CODE}&redirect_uri={CALLBACK}` |
| Grant (refresh) | `grant_type=refresh_token&refresh_token={RT}` |
| **Access token TTL** | **30 minutes** |
| **Refresh token TTL** | **7 days** (hard expiry → full CAG/LMS re-auth required) |
| API call auth | `Authorization: Bearer {access_token}` |
| Callback URL | Must be HTTPS; `https://127.0.0.1` permitted for local |
| Auth code note | The returned `code` is URL-encoded; must be URL-**decoded** before the token exchange (`%40` → `@`). |

### FR-AUTH

- **FR-AUTH-1** First-time interactive login: open the authorize URL, user completes CAG/LMS, capture `code` from the redirect landing URL, exchange for the initial access + refresh token pair.
- **FR-AUTH-2** Persist the token bundle (access + refresh + expiry) to a single token file outside the repo (app data dir), `chmod 600`, **git-ignored**. Never log token values.
- **FR-AUTH-3** Auto-refresh the access token using the refresh token *before* expiry on every session start and proactively on the 30-min boundary during a long session.
- **FR-AUTH-4** **7-day re-auth UX (critical).** When the refresh token is expired/invalidated (or a refresh returns an auth error), surface a clear, actionable "Reconnect Schwab" prompt in CLI and Streamlit — never a silent failure or a stack trace. Track and display "token expires in N days" in Settings.
- **FR-AUTH-5** Credentials (`client_id`, `client_secret`, `callback_url`) read from environment/`.env` only; never persisted to the app's SQLite store.
- **FR-AUTH-6** A single session accessor is the only thing that touches OAuth; all market-data and account modules depend on it, never on the raw OAuth flow.

- **FR-AUTH-7** On connect, call `GET /trader/v1/userPreference` once and cache the `StreamerInfo` block (streamer socket URL, `schwabClientCustomerId`, `schwabClientCorrelId`, channel, function id). The Streamer LOGIN (§5.A) depends entirely on these values.

**Security requirements:** token file is the only secret at rest and must be treated as such; no secrets in logs, error messages, fixtures, or committed files; redact account numbers everywhere they surface.

---

## 5. Scope — Two Connections

### Connection A — Market Data (Streamer / WebSocket)

The Streamer is a single authenticated WebSocket carrying multiple subscribable services. Connection parameters (`schwabClientCustomerId`, `schwabClientCorrelId`, channel, function id) come from the **GET User Preference** endpoint; the LOGIN command authenticates with the OAuth access token.

**Services in scope:**

| Service | Use |
|---|---|
| `LEVELONE_EQUITIES` | Live stock/ETF marks (bid/ask/last/mark, volume, net change). |
| `LEVELONE_OPTIONS` | Live option marks **with greeks** (delta, gamma, theta, vega, rho), IV, open interest, bid/ask/last. |

**Out of scope services:** `NYSE_BOOK`/`NASDAQ_BOOK`/`OPTIONS_BOOK` (Level 2), `CHART_*`, `SCREENER_*`, `LEVELONE_FUTURES*` (futures excluded), `ACCT_ACTIVITY` (order-fill streaming — not needed without order placement).

**Protocol (confirmed from doc):**

- Commands: `LOGIN` (must succeed first), `SUBS` (replace subscription set), `ADD` (append symbols), `UNSUBS`, `VIEW` (change field set), `LOGOUT`.
- Each request carries `service`, `command`, `requestid`, `SchwabClientCustomerId`, `SchwabClientCorrelId`, and `parameters` (`keys`, `fields`).
- Responses are one of: `response` (ack), `notify` (heartbeat), `data` (streaming payload). Data payloads use **numeric field keys** that must be mapped to names (see §8).
- **One Streamer connection per user maximum** (response code 12 `CLOSE_CONNECTION`). The app must hold at most one connection process-wide.
- Entitlement signal: LOGIN response `status=PN|NP|PP`; **if no entitlements, quotes are delayed/NFL.** Each data row carries `delayed: true|false`.

**Supporting REST market-data endpoints (server `https://api.schwabapi.com/marketdata/v1`, confirmed):**

| Endpoint | Use |
|---|---|
| `GET /chains` | Enumerate an option chain (strikes/expiries with greeks/IV) for the Option Selector. Schema: `OptionChain` → `OptionContractMap` → `OptionContract`, plus `Underlying`. |
| `GET /quotes` (and `GET /{symbol_id}/quotes`) | On-demand snapshot marks for stocks **and** options (`QuoteEquity`/`QuoteOption`). Serves as the **non-streaming marks fallback** (§7) and for one-off candidate pricing without opening the Streamer. |
| `GET /expirationchain` | Lightweight expiry list for an underlying (cheaper than a full chain). |

**Chain discovery caveat:** the Streamer pushes quotes only for **symbols you already know**. Use `GET /chains` to discover contracts, then subscribe selected OCC symbols on the Streamer for live marks.

#### FR — Market Data

- **FR-MD-1** Establish, authenticate (LOGIN), and maintain exactly one Streamer connection; handle heartbeats and detect staleness.
- **FR-MD-2** Subscribe to `LEVELONE_EQUITIES` for the distinct stock tickers across open positions and any user-entered candidate; request only the fields the app consumes (§8).
- **FR-MD-3** Subscribe to `LEVELONE_OPTIONS` for the OCC option symbols across open option legs and selector candidates; capture greeks + IV.
- **FR-MD-4** Maintain an in-memory latest-value cache per symbol (last `data` wins, merged across partial `change` updates); expose a canonical `Mark` lookup to engines.
- **FR-MD-5** **Reconnect logic:** on `LOGIN_DENIED (3)`, `STREAM_CONN_NOT_FOUND (20)`, `STOP_STREAMING (30)`, transport drop, or heartbeat gap → reconnect with fresh access token and re-subscribe the current symbol set, with capped exponential backoff.
- **FR-MD-6** **Delayed-data handling:** propagate the `delayed` flag to the UI; clearly badge marks as delayed vs real-time so risk numbers aren't silently computed off stale data.
- **FR-MD-7** **Chain snapshot (REST):** fetch a full option chain for an underlying (calls/puts, strike window, DTE window) → normalize to the canonical chain model with greeks/IV for the Option Selector.
- **FR-MD-8** **Lifecycle:** clean `LOGOUT` and socket close on app shutdown / when no consumers remain.

### Connection B — Accounts (REST)

Server: `https://api.schwabapi.com/trader/v1` (confirmed). The `{accountNumber}` path parameter takes the **encrypted hash**, not the plain number.

| Capability | Endpoint (confirmed) | Purpose |
|---|---|---|
| Account hash resolution | `GET /accounts/accountNumbers` | Map plain account number → its **encrypted hash value** (`AccountNumberHash`), which all other account calls require. Stored as a label↔hash mapping in settings. |
| Balances & positions snapshot | `GET /accounts/{accountNumber}?fields=positions` (or `GET /accounts?fields=positions` for all linked) | Cash, buying power, **MTM equity**, and **open positions** for the drawdown governor. |
| Transaction history | `GET /accounts/{accountNumber}/transactions` (date window + type filter) | Trade history → canonical `TradeEvent`s through the existing dedup/assembly pipeline. |

> **High-value finding:** the balances payload already carries marked-to-market account value directly — `currentBalances.liquidationValue` / `currentBalances.equity` — so **P1 alone delivers true MTM equity for the drawdown governor**, before the Streamer is even built. Streamer marks (P2) are then needed mainly for per-position `current_delta_notional` and live candidate/selector pricing.

#### FR — Accounts

- **FR-ACCT-1** Resolve and cache the account-number→hash map; let the user assign a friendly label (e.g. `schwab-tos`) per hash in Settings. Account numbers must be redacted in any display/log.
- **FR-ACCT-2** Pull a balances+positions snapshot on demand; normalize to a canonical account snapshot (cash, buying power, per-position symbol/qty/avg price/market value).
- **FR-ACCT-3** Pull transactions over a date window; normalize **trades** to `TradeEvent`s (asset type, side, qty, price, fees, timestamp, account). Route unparseable rows to the **review queue** — never drop.
- **FR-ACCT-4** Ingestion is **idempotent**: re-pulling an overlapping window must not duplicate events (existing natural-key uniqueness).
- **FR-ACCT-5** Optionally archive each raw pull as a CSV for the audit trail.

---

## 6. Symbology (confirmed from doc)

Schwab option symbols are **21 chars**, format `RRRRRRYYMMDDsWWWWWddd`:

- `RRRRRR` — root symbol, **space-padded to 6 chars**
- `YYMMDD` — expiration
- `s` — `C`/`P`
- `WWWWWddd` — strike: 5 whole digits + 3 decimal digits (strike × 1000, zero-padded to 8)

Example: `AAPL  251219C00200000` = AAPL 2026-12-19 $200.00 Call.

**FR-SYM-1** Provide a bidirectional converter between the app's internal option key and Schwab's OCC symbol, used for both Streamer subscriptions and quote/transaction parsing. Unit-test round-trips on multiple roots, strikes with fractional dollars, and short/long root symbols.

---

## 7. Architecture (clean separation of concerns)

Design rule (NFR): **a new data source is a new, self-contained module; engines consume canonical models, never raw Schwab JSON.** All HTTP/WebSocket/field-shape knowledge stays in the Schwab modules.

```
auth          → OAuth session accessor (one entry point; token file mgmt)
market data   → Streamer client (connection, subs, field decode, latest-value cache)
              → REST chain fetcher (chain discovery → canonical chain snapshot)
              → Marks provider (canonical Mark lookup; Streamer-backed / static / null)
accounts      → account hash resolver + balances/positions snapshot
              → transaction fetcher (→ canonical TradeEvent[] + review items)
engines       → consume Mark / snapshot / TradeEvent only
UI/CLI        → connect/disconnect, status, fetch buttons
```

**Provider fallback chain:** `Streamer marks` → `static/manual marks` → `null (realized-only)`. Default to the safe terminal state so nothing breaks when Schwab is unconfigured (AC-5).

---

## 8. Data Models & Field Mapping

### 8.1 Streamer field decode — `LEVELONE_EQUITIES` (numeric keys → canonical)

| Key | Schwab field | Canonical use |
|---|---|---|
| 1 / 2 | Bid / Ask | bid / ask |
| 3 | Last Price | last |
| 33 | Mark Price | **mark** (preferred price) |
| 8 | Total Volume | volume |
| 18 | Net Change | day change |
| 32 | Security Status | trading status |
| 35 | Trade Time (epoch ms) | `asof` |
| `delayed` | bool | real-time vs delayed badge |

### 8.2 Streamer field decode — `LEVELONE_OPTIONS`

| Key | Schwab field | Canonical use |
|---|---|---|
| 2 / 3 | Bid / Ask | bid / ask |
| 4 | Last | last |
| 9 | Open Interest | open_interest |
| 10 | Volatility (IV) | **iv** — confirm units (likely %; normalize to decimal) |
| 20 | Strike Price | strike |
| 21 | Contract Type | right (C/P) |
| 27 | Days to Expiration | dte |
| 28 / 29 / 30 / 31 / 32 | Delta / Gamma / Theta / Vega / Rho | greeks |
| 34 | Theoretical Option Value | theo |
| 33 | Security Status | status |

> Field indices above are taken from the Streamer doc's Level 1 definition tables. The **VIEW/SUBS `fields` list must request exactly these keys**, and the decode map must be unit-tested against a recorded `data` frame.

### 8.3 Account snapshot (REST — schema confirmed)

Response shape: `securitiesAccount` → `{ accountNumber, positions[], initialBalances, currentBalances, projectedBalances }`.

| Canonical | Schwab field (confirmed) | Notes |
|---|---|---|
| MTM equity | `currentBalances.liquidationValue` (and `currentBalances.equity`) | Direct true equity → drawdown governor. |
| Cash | `currentBalances` cash fields / `initialBalances.cashBalance` | |
| Buying power | `currentBalances.buyingPower`, `optionBuyingPower`, `stockBuyingPower` | |
| Long option mkt value | `initialBalances.longOptionMarketValue` | Options book exposure cross-check. |
| Long stock value | `initialBalances.longStockValue` | |
| Position symbol | `positions[].instrument.symbol` (+ `cusip`, `type`) | stock ticker or OCC option. |
| Position qty | `positions[].longQuantity` / `shortQuantity` | signed exposure. |
| Avg price | `positions[].averagePrice` (`averageLongPrice`/`averageShortPrice`) | |
| Market value | `positions[].marketValue` | |
| Open P/L | `positions[].longOpenProfitLoss` / `shortOpenProfitLoss` | |

Map `instrument.type` / asset type (`EQUITY`, `ETF`, `OPTION`, `INDEX`, `COLLECTIVE_INVESTMENT`, `SWEEP_VEHICLE`, …) → internal asset type; futures excluded (stay on Tradovate).

### 8.4 Transactions → `TradeEvent` (REST — schemas confirmed)

Schemas: `Transaction` → `TransferItem[]` with typed instruments (`TransactionEquity`, `TransactionOption`, `TransactionMutualFund`, …), plus `TransactionType` and `instruction`. Map → `TradeEvent` (symbol/OCC, side BUY/SELL incl. open/close/cover variants, quantity, price, fees/commission via `CommissionAndFee`/`Fees`, timestamp, account label, asset type). Filter to trade-type transactions; route unparseable rows → review queue.

### 8.5 Greeks robustness

Compute local Black-Scholes greeks as a fallback for any contract where the source omits a greek (thin chain, delayed data, or a future cheaper source), so selector/leverage scoring never silently zeroes out. Anchor to a known textbook value in tests.

---

## 9. Non-Functional Requirements

- **NFR-1 Rate / connection limits.** One Streamer connection max (code 12). REST: stay well under documented throttles; the doc notes order endpoints throttle 0–120/min and **GET requests are unthrottled**, but apply a conservative client-side min-interval as a safety net. Cache account snapshots with a short TTL to avoid hammering on dashboard refresh.
- **NFR-2 Reliability.** Auto-reconnect + re-subscribe; never crash the dashboard when marks are unavailable — fall back to null marks and show a banner.
- **NFR-3 Security.** §4 token handling; redact account numbers; no secrets in logs/fixtures/commits.
- **NFR-4 Determinism / testability.** All network behind seams so tests inject fakes; **no live calls in CI.**
- **NFR-5 Degradation.** Schwab-not-configured path is first-class; offline/static-marks path preserved.
- **NFR-6 Auditability.** Chain snapshots and raw transaction pulls persisted as point-in-time artifacts.
- **NFR-7 Usability.** Connect/refresh in ≤2 clicks; clear status (connected, delayed-data, token-expiry countdown). The integration must feel faster than manual CSV export or it fails the project's usability mandate.

---

## 10. Phased Delivery Plan (gated)

Each phase is independently shippable. **Stop at each gate and wait for green light before advancing** (per the project's gated workflow).

| Phase | Deliverable | Gate / acceptance |
|---|---|---|
| **P0 — Auth** | OAuth session accessor, token file, refresh, re-auth UX, Settings status. | Live token obtained; access auto-refreshes; expired-refresh shows reconnect prompt (AC-1). |
| **P1 — Account snapshot** | Hash resolver + balances/positions snapshot → canonical snapshot; Settings label mapping. **Wire `liquidationValue` into MTM equity + drawdown governor immediately.** | Snapshot returns real cash/positions; MTM equity & drawdown governor go live off `liquidationValue`; numbers reconcile vs Schwab UI (AC-2). |
| **P2 — Streamer marks** | Streamer client (equities + options), field decode, latest-value cache, Marks provider, reconnect. | Live (or correctly-badged delayed) marks for held symbols; MTM equity, combined-account drawdown, and delta-notional leverage cap all compute non-zero and bind correctly (AC-3, AC-4). |
| **P3 — Chain snapshot (REST)** | Chain fetcher → canonical chain w/ greeks/IV; Option Selector consumes it; local-greeks fallback. | Fetched chain ranks in the selector with no engine change; missing greeks filled locally (AC-6). |
| **P4 — Transactions** | Transaction fetcher → `TradeEvent`s via dedup/assembly; CLI/UI; raw-CSV archive. | Idempotent ingest; malformed rows in review queue (AC-7). |

Suggested order rationale: auth is the universal dependency; the account snapshot delivers MTM value fastest with the smallest blast radius; Streamer is the richest but highest-complexity piece; transactions are lowest urgency since CSV import already works for Schwab.

---

## 11. CLI & UI Surface

- **CLI:** `schwab connect` (interactive auth), `schwab status`, `fetch-schwab` (transactions, date window), `accounts snapshot`, `marks --silo ...` (preview MTM), and an option to source a live chain for the selector.
- **Streamlit:** Settings shows Schwab connection status (token valid, expires-in-N-days, entitlement/delayed state) with a Reconnect button; Import page gets a Schwab tab (snapshot + transactions); Option Selector gets "Fetch live chain (Schwab)"; the dashboard badges marks as real-time vs delayed.

---

## 12. Testing Strategy

- **No live network in CI.** Record and scrub (account numbers, tokens) 1–2 real responses per surface as fixtures: a Streamer `data` frame (equity + option), a chain response, an account snapshot, a transactions page.
- **Streamer decode:** assert numeric-key frames → canonical Mark with greeks/IV; IV normalized to decimal; `delayed` propagated.
- **Reconnect:** simulate codes 3/20/30 and transport drop → assert re-LOGIN + re-SUBS of the current symbol set.
- **Symbology:** round-trip OCC ↔ internal across roots/strikes.
- **Account snapshot / transactions:** assert canonical mapping; malformed rows → review queue; re-import idempotent.
- **Greeks:** `bs_greeks` vs textbook anchor (e.g. S=K=100, T=1, r=.05, σ=.2 → call ≈ 10.4506, delta ≈ 0.6368).
- **Degradation:** with Schwab unconfigured, full suite passes on null/static marks.
- **Live field-name verification (manual, pre-build):** hit each live endpoint once, diff actual field names/units against §8 tables, and lock the fixtures.

---

## 13. Risks & Open Questions

- **~~REST schemas not in docs~~ (RESOLVED).** Endpoints + the account balances/positions schema are now confirmed from the OpenAPI specs. Residual: expand the `QuoteOption`/`OptionChain` leaf fields against a live `/quotes` and `/chains` response (low effort).
- **Streamer field indices/units (high).** Confirm the exact `fields` keys to subscribe and IV units against a live `data` frame before locking the decode map.
- **Order endpoints are present in the Accounts product (compliance).** `POST/PUT/DELETE /accounts/{acct}/orders` and `/previewOrder` exist in the same API surface. The implementation must include a guard/lint ensuring no order, preview-order, or money-movement call is ever wired (AC-8).
- **7-day refresh expiry (high UX risk).** Weekly forced re-auth via browser CAG/LMS. Needs a frictionless, well-signposted reconnect — the single biggest day-to-day annoyance.
- **Entitlements / delayed data (medium).** If the individual app returns `NP`/delayed quotes, risk numbers must be badged delayed; decide whether delayed marks are acceptable for the drawdown governor or whether to gate it.
- **Single Streamer connection (medium).** Only one connection per user — coordinate if anything else (e.g. thinkorswim) competes for it.
- **User Preference dependency (medium, confirmed).** `GET /trader/v1/userPreference` returns the `StreamerInfo` (socket URL, customer/correl IDs, channel, function id) required for Streamer LOGIN — wire first in P2 (FR-AUTH-7).
- **Headless auth (low/medium).** Initial CAG needs a browser redirect once; document a manual-code paste path for headless machines.
- **Account hash, not number (low).** All account calls key on the encrypted hash; the label↔hash map must be resilient to Schwab rotating hashes.

---

## 14. Acceptance Criteria

- **AC-1** A valid token is obtained interactively, auto-refreshes within a session, and an expired refresh token produces a clear reconnect prompt (no silent failure). Token file is git-ignored; no secrets logged.
- **AC-2** The account snapshot returns real cash, buying power, and open positions that reconcile against the Schwab UI.
- **AC-3** With Streamer marks live, open positions show non-zero `current_delta_notional`; the leverage cap can bind in sizing on a constructed over-levered book.
- **AC-4** MTM silo and **combined-account** equity reflect open-position marks; account drawdown is computed from a combined curve (not summed per-silo peaks); delayed data is badged.
- **AC-5** With Schwab **not** configured, every existing workflow runs on CSV/null-marks exactly as before — no hard dependency.
- **AC-6** A fetched chain ranks in the Option Selector with no engine change; missing greeks are filled by local Black-Scholes.
- **AC-7** `fetch-schwab` ingests transactions **idempotently**; malformed rows appear in the review queue.
- **AC-8** **No order-placement or money-movement code path exists anywhere** in the integration.

---

## 15. Success Metrics

- Time to a fresh MTM equity + drawdown read drops from "manual CSV export + import" to ≤2 clicks.
- The 20% MTM drawdown governor and delta-notional leverage cap are *live* (non-zero, binding) rather than inert.
- Zero manual CSV steps required for routine Schwab marks/transactions.
- Re-auth, when required, takes <60 seconds and is self-explanatory.

---

## 16. Implementation Plan (agent build guide)

This section is written for an autonomous coding agent. It maps the PRD onto the **existing** Trading Architect codebase, names exact files and function signatures, and sequences the work into the gated phases from §10. Build phases in order; **stop at each gate** and run the listed checks before advancing.

### 16.0 Conventions, dependencies, and seams

**Stack:** Python ≥3.11, pydantic v2, pandas, Streamlit, pytest. Source under `src/trading_architect/`, layered `models → store → ingestion → assembly → engines → app/cli`.

**Dependency:** add `httpx` and `websockets` to the `schwab` optional extra in `pyproject.toml` (the `schwab-py` extra already exists; the Streamer is hand-rolled over `websockets` because `schwab-py`'s streaming surface is thin — keep all socket code in one module). Gate all imports so the app runs without the extra installed (mirror `schwab_py_available()` / `robin_stocks_available()`).

**Env / secrets** (extend `config/env.py`, mirror `RH_*`):
- `SCHWAB_API_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL` (default `https://127.0.0.1:8182`), `SCHWAB_TOKEN_PATH` (default `data/.schwab_token.json`).
- Add `SCHWAB_*` to `.env.example`; add the token path to `.gitignore`. Never log/persist secrets to SQLite.

**Stable seams to reuse (do NOT reinvent):**

| Need | Existing symbol (verified present) |
|---|---|
| Canonical trade | `models.entities.TradeEvent`, `OptionSpec` |
| Position + leverage field | `models.entities.Position.current_delta_notional` |
| Chain models | `models.entities.ChainSnapshot`, `ChainContract` |
| Account balance / holdings | `BrokerAccountBalance`, `HoldingLeg`, `ConsolidatedHolding` (mirror the `RobinhoodPortfolioSnapshot` pattern) |
| Review queue / import result | `ReviewQueueItem`, `ImportResult` |
| Marks | `engines.marks.Mark`, `MarksProvider`, `NullMarksProvider`, `StaticMarksProvider`, `SchwabMarksProvider` |
| Greeks (already implemented) | `engines.options_pricing.bs_greeks(...)` |
| MTM equity / drawdown | `engines.equity.{open_mtm_pnl, reconstruct_silo_equity_curve, equity_metrics_for_silo, combined_account_equity_curve, account_equity_metrics}` (already accept a marks provider) |
| Book/governor state | `engines.book_context.build_book_context` |
| Delta-notional enrichment | `assembly.positions.enrich_positions_with_marks(...)`, `_compute_delta_notional(...)` |
| Persistence | `store.repository.Repository.{upsert_events, add_review_items, replace_positions, save_chain_snapshot, list/get_chain_snapshot, load_app_settings, save_app_settings, archive_raw_csv}` |
| Wiring entry points | `bootstrap.{create_repository, import_schwab_fetch, _assemble_and_enrich, _finalize_import, resolve_epoch_id}` |
| Pattern to mirror for API pulls | `ingestion/robinhood_fetch.py` (esp. `fetch_portfolio_snapshot`, `fetch_all`, `save_events_as_csv`) |

> Per the "net-new" decision, treat any pre-existing `ingestion/schwab_client.py`, `schwab_market_data.py`, `schwab_fetch.py` as **replaceable scaffolds** — rewrite their contents to this plan; keep the stable engine/store/model seams above untouched except where a phase explicitly says to extend them.

**New modules to create:**

```
ingestion/schwab_auth.py        # OAuth session, token file, refresh, userPreference/StreamerInfo
ingestion/schwab_accounts.py    # hash resolution + balances/positions snapshot
ingestion/schwab_rest.py        # REST /quotes + /chains (chain snapshot, quote marks)
ingestion/schwab_streamer.py    # WebSocket client (LEVELONE_EQUITIES/_OPTIONS)
ingestion/schwab_transactions.py# transactions → TradeEvent[] + review items
ingestion/schwab_symbols.py     # internal <-> OCC symbol conversion (§6)
models/entities.py              # EXTEND: SchwabAccountSnapshot (mirror RobinhoodPortfolioSnapshot)
```

**Global guardrail (compliance):** add a unit test that greps the `ingestion/schwab_*` and `engines/marks.py` sources and **fails if any of** `previewOrder`, `/orders`, `placeOrder`, `POST`/`PUT`/`DELETE` against a trader path appear (AC-8). No order/transfer code may exist.

---

### 16.1 Phase 0 — OAuth session layer

**Files:** `ingestion/schwab_auth.py`, `config/env.py`, `.env.example`, `.gitignore`, `pyproject.toml`.

**Build:**
- `schwab_credentials_configured() -> bool` (already exists — verify it checks key+secret).
- `get_client() -> SchwabSession` — single accessor. Loads token file; if missing/invalid refresh, run interactive login (authorize URL → capture `code` → exchange). Auto-refresh access token when within ~5 min of the 30-min expiry. Use `schwab-py` token management if it simplifies; otherwise implement directly against `POST https://api.schwabapi.com/v1/oauth/token` with `Authorization: Basic base64(key:secret)`.
- `token_status() -> {present: bool, access_expires_at, refresh_expires_at, days_left}` for Settings UI.
- `reauthenticate()` — restart the three-legged flow when the 7-day refresh token is dead; raise a typed `SchwabAuthExpired` that the CLI/UI catches to show "Reconnect Schwab" (FR-AUTH-4).
- `get_user_preference() -> StreamerInfo` — `GET /trader/v1/userPreference`; cache `{streamerSocketUrl, schwabClientCustomerId, schwabClientCorrelId, schwabClientChannel, schwabClientFunctionId}` (FR-AUTH-7). Define a small `StreamerInfo` dataclass here.

**Gate (P0 done):** `get_client()` returns a session that performs a live authenticated `GET /userPreference`; access token auto-refreshes; killing the refresh token surfaces `SchwabAuthExpired` with a reconnect message. Token file is `chmod 600` and git-ignored. **Checks:** `token_status()` populated; no secrets in logs; compliance grep test passes.

---

### 16.2 Phase 1 — Account snapshot (delivers MTM equity immediately)

**Files:** `ingestion/schwab_accounts.py`, `models/entities.py` (extend), `store/repository.py` (settings for label↔hash), `bootstrap.py`, `cli.py`, `app/streamlit_app.py`.

**Models:** add `SchwabAccountSnapshot(BaseModel)` mirroring `RobinhoodPortfolioSnapshot`: `fetched_at: datetime`, `accounts: list[BrokerAccountBalance]`, `holdings: list[ConsolidatedHolding]`.

**Build (`schwab_accounts.py`):**
- `list_accounts(client=None) -> list[tuple[label, hash]]` — `GET /accounts/accountNumbers`; map `accountNumber → hashValue`. Persist label↔hash via `repo.save_app_settings(...)`; redact account numbers in any display.
- `_account_hash_for_label(client, label) -> str`.
- `fetch_account_balance(label, client=None) -> BrokerAccountBalance` — `GET /accounts/{hash}` → map `currentBalances.liquidationValue` → `portfolio_equity` (true MTM equity), `buyingPower`, cash fields (§8.3).
- `fetch_holdings(label, client=None) -> list[HoldingLeg]` — parse `securitiesAccount.positions[]`: `instrument.symbol`(+OCC for options), `longQuantity-shortQuantity`, `averagePrice`, `asset_type` from `instrument.type`; exclude futures.
- `fetch_portfolio_snapshot(labels, client=None) -> SchwabAccountSnapshot` — compose; reuse `consolidate_holdings` pattern from robinhood_fetch.

**Wire MTM now:** pass the snapshot's `portfolio_equity` into the equity/drawdown path. The drawdown governor (`build_book_context`) and `account_equity_metrics` already accept marks/equity — feed the live `liquidationValue` so the 20% MTM governor and combined-account drawdown go live this phase.

**CLI/UI:** `ta accounts snapshot [--label ...]`; Streamlit Import → "Schwab" tab showing balances + holdings; Settings shows connection status + label mapping editor.

**Gate (P1 done):** `fetch_portfolio_snapshot` returns real cash/buying-power/positions reconciling against the Schwab UI; MTM equity + drawdown governor compute off `liquidationValue`. **Checks:** new `tests/test_schwab_accounts.py` maps a recorded (scrubbed) `/accounts` fixture → snapshot; futures excluded; account numbers redacted.

---

### 16.3 Phase 2 — Streamer marks (stocks + options w/ greeks)

**Files:** `ingestion/schwab_streamer.py`, `ingestion/schwab_symbols.py`, `engines/marks.py` (extend `SchwabMarksProvider`).

**Build (`schwab_symbols.py`):** `to_occ(internal_symbol) -> str` and `from_occ(occ) -> internal_symbol` per §6 (6-char space-padded root, `YYMMDD`, `C/P`, strike×1000 zero-padded to 8). Round-trip tested.

**Build (`schwab_streamer.py`):** an async/threaded `StreamerClient` holding **one** connection (NFR-1):
- `connect()` — open WS to `StreamerInfo.streamerSocketUrl`; send `ADMIN/LOGIN` with the access token + customer/correl IDs (§5.A protocol); await `code:0`.
- `subscribe(equities: list[str], options: list[str])` — `SUBS`/`ADD` `LEVELONE_EQUITIES` and `LEVELONE_OPTIONS` requesting only consumed `fields` (decode map below). Track the active symbol set.
- `_on_data(frame)` — merge numeric-keyed `content[]` into a latest-value cache keyed by symbol; preserve partial `change` updates; record `delayed` flag.
- `_decode_equity` / `_decode_option` — numeric→canonical per §8.1/§8.2 (equity: 1 bid,2 ask,3 last,33 mark,35 tradeTime; option: 2 bid,3 ask,4 last,9 OI,10 IV,20 strike,21 right,27 dte,28 delta,29 gamma,30 theta,31 vega,32 rho). **Normalize IV to decimal.** ⚠ Indices are provisional — lock against a recorded `data` frame (§13) before merging.
- `reconnect()` — on codes 3/20/30, transport drop, or heartbeat gap: re-`get_client()` for a fresh token, reconnect, re-subscribe current set, capped exp-backoff (FR-MD-5).
- `latest(symbol) -> Mark | None`; `close()` sends `LOGOUT`.

**Build (`engines/marks.py`):** finish `SchwabMarksProvider.marks_for(symbols)`:
1. ensure subscribed (convert option symbols via `to_occ`), 2. read latest cache, 3. **fallback to REST `/quotes`** (Phase 3 `schwab_rest.fetch_quotes`) for any symbol the Streamer hasn't filled, 4. return `dict[symbol -> Mark]` with greeks where available, else compute via `bs_greeks`. Keep `Null`/`Static` providers as the no-Schwab path.

**Wire delta-notional:** call `enrich_positions_with_marks(positions, marks)` in `bootstrap._assemble_and_enrich` when a Schwab provider is active so `current_delta_notional` is non-zero and the leverage cap can bind.

**Gate (P2 done):** held stock + option symbols return live (or correctly-badged delayed) marks with greeks; `current_delta_notional` non-zero; leverage cap binds on a constructed over-levered book; reconnect re-subscribes. **Checks:** `tests/test_schwab_streamer.py` (decode fixture frame, reconnect simulation), `tests/test_schwab_symbols.py` (round-trip), enrichment test asserting non-zero delta-notional.

---

### 16.4 Phase 3 — Chain snapshot + quotes (REST)

**Files:** `ingestion/schwab_rest.py`, `engines/options_selector.py` (extend, local-greeks fallback), `cli.py`, `app/streamlit_app.py`.

**Build (`schwab_rest.py`):** server `https://api.schwabapi.com/marketdata/v1`.
- `fetch_chain_snapshot(underlying, *, strike_count=20, min_dte=90, max_dte=400, client=None) -> ChainSnapshot` — `GET /chains` (`OptionChain → OptionContractMap → OptionContract`, `Underlying`). Map `strikePrice→strike`, exp-map key/`expirationDate→expiry`, `daysToExpiration→dte`, `bid/ask`, `mark→mid`, `volatility→iv` (normalize), `delta/gamma/theta/vega→greeks`, `openInterest`, `putCall→right`. `spot_price` from `Underlying`. DTE window covers the selector's 90–365 band. Persist via `repo.save_chain_snapshot`.
- `fetch_quotes(symbols, client=None) -> dict[symbol -> Mark]` — `GET /quotes` (`QuoteEquity`/`QuoteOption`); the non-streaming marks fallback used by `SchwabMarksProvider`.

**Selector robustness:** in `options_selector.py`, when a `ChainContract` greek is missing, compute it from spot + resolved IV via `bs_greeks` instead of defaulting (so thin chains / outages degrade gracefully).

**CLI/UI:** add a chain source to the existing `options` command (live Schwab vs CSV); Option Selector page gets "Fetch live chain (Schwab)".

**Gate (P3 done):** `fetch_chain_snapshot("NVDA")` → a `ChainSnapshot` with greeks/IV that the selector ranks with **no engine change**; missing greeks filled locally. **Checks:** `tests/test_schwab_rest.py` maps a recorded chain fixture; selector produces the same result structure as a CSV snapshot.

---

### 16.5 Phase 4 — Transactions

**Files:** `ingestion/schwab_transactions.py`, `bootstrap.py` (`import_schwab_fetch` already present — point it at this module), `cli.py` (`fetch-schwab` parser already present).

**Build (`schwab_transactions.py`), mirroring `robinhood_fetch.fetch_all`:**
- `fetch_all(label, start, end, client=None) -> tuple[list[TradeEvent], list[ReviewQueueItem]]` — `GET /accounts/{hash}/transactions` (date window; filter to trade types). Map `Transaction → TransferItem[]` typed instruments (`TransactionEquity`/`TransactionOption`/…), `instruction`→`Side` (incl. open/close/cover variants), qty, price, fees via `CommissionAndFee`/`Fees`, timestamp, account label. Options → `OptionSpec` + OCC via `schwab_symbols.from_occ`. Unparseable rows → `ReviewQueueItem` (never drop).

**Wiring (already stubbed in `bootstrap.import_schwab_fetch`):** fetch → `resolve_epoch_id` per event → `repo.upsert_events` (dedup via natural key → idempotent) → `repo.add_review_items` → `_assemble_and_enrich` → `repo.replace_positions`; optionally `save_events_as_csv` to `data/raw/` for audit.

**Gate (P4 done):** `ta fetch-schwab --label schwab-tos --start … --end …` ingests transactions idempotently (re-run adds zero dupes); malformed rows land in the review queue. **Checks:** `tests/test_schwab_transactions.py` maps a recorded fixture; re-import idempotency asserted.

---

### 16.6 Testing & fixtures (all phases)

- **No live network in CI** (NFR-4). Record + scrub one real response per surface under `tests/fixtures/schwab/`: `user_preference.json`, `accounts.json`, `streamer_equity.json`, `streamer_option.json`, `chains.json`, `quotes.json`, `transactions.json`. Scrub account numbers and tokens.
- Inject a fake client/socket behind `get_client()` and the Streamer transport so tests are deterministic.
- Greeks anchor (already have `bs_greeks`): S=K=100, T=1, r=.05, σ=.2 → call ≈ 10.4506, delta ≈ 0.6368.
- Run: `pip install -e ".[dev,schwab]"` then `pytest -q`. Each phase's gate requires its new tests + the full suite green.

### 16.7 Manual pre-build verification (do once, before P2/P3 mapping)

With a live token, hit `GET /userPreference`, open the Streamer and capture one `LEVELONE_EQUITIES` + one `LEVELONE_OPTIONS` `data` frame, and call `GET /quotes` and `GET /chains` once. Diff actual field indices/names/units against §8 and lock the fixtures. This closes the only remaining unknowns in §13.

---

*This PRD specifies design, scope, acceptance, and a phased implementation plan — but no production code. REST endpoints and the account balances/positions schema are confirmed from the OpenAPI specs. The only items still marked "confirm/verify" are the Streamer numeric field indices and the `/quotes` & `/chains` leaf fields, which must be locked against a recorded live response (§16.7) before implementation.*
