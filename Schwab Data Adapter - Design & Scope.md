# Schwab Data Adapter — Design & Scope (Developer Hand-off)

**Project:** Trading Architect
**Author:** Prepared for developer hand-off
**Date:** 2026-05-26
**Status:** Ready to implement; field-level mappings must be verified against live Schwab docs (see §13).
**Related:** PRD §6.4 (on-demand chain snapshot), §7.2 (exposure), §7.5 (option selector), §7.7 (drawdown governor); *Audit – Buildout vs PRD.md* findings H-1, M-1, M-2, M-3, and the INFO item on greeks.

---

## 1. Purpose

Add a Schwab Trader API data layer that supplies the three things the app currently lacks a live source for:

1. **Option chains with greeks + IV** — on demand, for the Option Selector (PRD §7.5 / M5). Today these come only from a manually imported CSV (`ingestion/chain.py`).
2. **Quotes (stock + option marks)** — to mark open positions to market, which unblocks marked-to-market equity, the drawdown governor, and the delta-notional leverage cap (audit M-2, M-3, H-1).
3. **Transaction history** — an API pull that mirrors the existing Robinhood fetch path, so Schwab data can be ingested without a manual CSV export.

Schwab is the right provider here: it is **free** for an individual developer app, it is the source the PRD already names as the priority (§6.4), and Alex already holds a Schwab/thinkorswim account. The cost is OAuth setup friction, not money.

---

## 2. Scope

### In scope
- A Schwab OAuth/session layer using **`schwab-py`** for token management.
- **ChainFetcher** — pull an option chain for an underlying and normalize to the existing `ChainSnapshot` / `ChainContract` model.
- **QuoteFetcher** — pull spot/mark quotes for a list of symbols (stocks and option contracts).
- **SchwabTransactionFetcher** — pull trade history and normalize to canonical `TradeEvent`s (reuse the assembly/dedup pipeline).
- **Dependent wiring** (per the "adapter + dependent fixes" decision):
  - MTM equity reconstruction consuming live marks (audit M-2).
  - `Position.current_delta_notional` computed at assembly using a marks/greeks provider (audit H-1).
  - Local greeks in `engines/options_pricing.py` so scoring is robust even when a source omits greeks (audit INFO item).
- CLI commands + Streamlit "fetch from Schwab" surface.
- Unit tests against recorded/mocked responses (no live calls in CI).

### Out of scope
- Order placement, execution, or any money movement (PRD §2.2 — never).
- Streaming/WebSocket feeds (PRD §2.3 — on-demand only; delayed/snapshot is sufficient).
- Replacing the existing Robinhood/Tradovate adapters. Schwab is additive.
- Caching infrastructure beyond a simple in-process/TTL cache (see §10).

---

## 3. Prerequisites (developer setup)

1. A Schwab brokerage account, **thinkorswim-enabled**.
2. A registered **Schwab Individual Developer app** (developer.schwab.com) with the Market Data and Accounts/Trading APIs added, and a registered OAuth callback URL (e.g. `https://127.0.0.1:8182`).
3. `pip install schwab-py` (add to `pyproject.toml` optional extras as `schwab`).
4. App key + app secret from the developer portal.

Rate limit to design against: **~120 requests/minute** per the developer portal. Access token lifetime ~30 min; refresh token ~7 days (verify current values — Schwab has changed these).

---

## 4. Where it fits in the existing architecture

The codebase already separates ingestion → store → engines → UI. The Schwab layer slots into `ingestion/` alongside the existing adapters and the `robinhood_fetch.py` API-pull pattern.

```
ingestion/
  base.py                  (existing — BrokerAdapter ABC, IngestionService)
  schwab.py                (existing — CSV adapter; leave as-is)
  robinhood_fetch.py       (existing — API pull pattern to MIRROR)
  chain.py                 (existing — parse_chain_csv → ChainSnapshot)
  schwab_client.py         (NEW — auth/session + thin REST wrappers)
  schwab_market_data.py    (NEW — ChainFetcher + QuoteFetcher → ChainSnapshot / marks)
  schwab_fetch.py          (NEW — SchwabTransactionFetcher → TradeEvent[], mirrors robinhood_fetch)
config/
  env.py                   (existing — extend with Schwab creds/paths)
engines/
  options_pricing.py       (extend — add greeks)
  equity.py                (extend — accept a marks provider)
  marks.py                 (NEW — MarksProvider protocol + Schwab/CSV/none impls)
assembly/
  positions.py             (extend — compute current_delta_notional via marks+greeks)
```

Design rule from the PRD/NFR-6: **a new data source is a new module, not edits scattered through engines.** Keep all Schwab HTTP/field-shape knowledge inside `ingestion/schwab_*`. Engines consume canonical models (`ChainSnapshot`, `TradeEvent`, a `Marks` dict), never raw Schwab JSON.

---

## 5. Auth design — standardize on `schwab-py`

Use `schwab-py`'s built-in token management; do **not** hand-roll the OAuth dance.

- **First-time login (interactive, one-off):** `schwab.auth.client_from_login_flow(api_key, app_secret, callback_url, token_path)` (or `client_from_manual_flow` for headless machines). This writes a token file.
- **Subsequent runs (non-interactive):** `schwab.auth.client_from_token_file(token_path, api_key, app_secret)` — auto-refreshes the access token from the stored refresh token.
- **Token file location:** store under the app data dir, not the repo. Add to config:
  - `SCHWAB_API_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL` in `.env` (extend `config/env.py`, mirror the existing `RH_*` handling).
  - `SCHWAB_TOKEN_PATH` defaulting to `data/.schwab_token.json`; **add to `.gitignore`**.
- **`schwab_client.py`** exposes a single `get_client()` that returns a ready `schwab-py` client (login flow only if no valid token file). All other modules depend on this, never on `schwab.auth` directly.

Security notes for the dev: token file contains a live refresh token — treat as a secret, never commit, never log. Credentials are read from env only; do not persist app key/secret in the SQLite store.

---

## 6. Component A — ChainFetcher (Option Selector)

**Goal:** replace/augment `parse_chain_csv` with a live fetch that returns the **same `ChainSnapshot`** so the selector engine is untouched.

**Schwab call (via schwab-py):** `client.get_option_chain(symbol, contract_type=ALL|CALL|PUT, strike_count=N, from_date, to_date, include_underlying_quote=True, strategy=SINGLE)`.

**Response shape (verify against live docs):** Schwab returns `callExpDateMap` and `putExpDateMap`, each keyed by `"YYYY-MM-DD:DTE"` → strike → list of contracts. The underlying quote arrives in an `underlying` block.

**Field mapping → `ChainContract` (models/entities.py).** *Confirm each Schwab field name against current docs before coding.*

| `ChainContract` field | Schwab field (verify) | Notes |
|---|---|---|
| `strike` | `strikePrice` | |
| `expiry` | from exp-date-map key / `expirationDate` | parse to `date` |
| `dte` | `daysToExpiration` | else compute `expiry − asof` |
| `bid` / `ask` | `bid` / `ask` | |
| `mid` | `mark` | fallback `(bid+ask)/2` |
| `iv` | `volatility` | Schwab reports IV in **percent** (e.g. 32.5). Normalize to decimal — the selector's `_resolve_iv` already divides by 100 when >3, but normalize at the source for clarity. |
| `delta` | `delta` | |
| `gamma` / `theta` / `vega` | `gamma` / `theta` / `vega` | |
| `open_interest` | `openInterest` | |
| `right` | `putCall` | map `CALL`→`C`, `PUT`→`P` |

`ChainSnapshot` fields: `spot_price` ← underlying `last`/`mark`; `asof_timestamp` ← now (or Schwab quote time); `risk_free_rate` ← config default (0.05); `iv_rank`/`iv_percentile` ← not provided by Schwab, leave `None` (selector already degrades gracefully).

**Interface:**
```python
# ingestion/schwab_market_data.py
def fetch_chain_snapshot(
    underlying: str, *, strike_count: int = 20,
    min_dte: int = 90, max_dte: int = 400, client=None,
) -> ChainSnapshot: ...
```
DTE window should cover the selector's 90–365 band plus buffer. The result flows straight into `repo.save_chain_snapshot(...)` (already exists) and `rank_contracts(...)` (unchanged).

**Acceptance:** a fetched snapshot produces the same `OptionSelectorResult` structure as a CSV snapshot, with greeks populated.

---

## 7. Component B — QuoteFetcher (marks for MTM)

**Goal:** provide a `Marks` lookup for open positions so equity, drawdown, and delta-notional become marked-to-market.

**Schwab call:** `client.get_quotes(symbols=[...])` — accepts stock tickers and OCC option symbols; returns `last`/`mark`, and for options also `delta` (and IV).

**Canonical output:** a small value object, no Schwab types leaking out:
```python
# engines/marks.py
@dataclass(frozen=True)
class Mark:
    symbol: str
    price: float          # last/mark
    delta: float | None   # for options, from quote or computed (§9c)
    asof: datetime

class MarksProvider(Protocol):
    def marks_for(self, symbols: list[str]) -> dict[str, Mark]: ...
```
Provide three implementations: `SchwabMarksProvider` (live), `StaticMarksProvider` (from a CSV/manual entry — keeps the no-brokerage path alive), and `NullMarksProvider` (current behavior: realized-only, no marks). The engine picks one; default `Null` so nothing breaks if Schwab isn't configured.

**Symbol mapping:** open positions store `symbol` per leg. Stock legs use the ticker; option legs use the synthetic `TICKER_YYYY-MM-DD_R_strike` symbol built in `parsing.py`. Add a helper to convert that synthetic symbol to the **OCC symbol** Schwab expects (`AAPL  260918C00110000` format). This converter belongs in `schwab_market_data.py`.

---

## 8. Component C — SchwabTransactionFetcher (history pull)

**Goal:** mirror `ingestion/robinhood_fetch.py` so a Schwab pull lands canonical `TradeEvent`s through the same dedup/assembly path — no manual CSV needed.

**Schwab calls:** `client.get_account_numbers()` → account hash; `client.get_transactions(account_hash, start_date, end_date, transaction_types=TRADE)`.

**Normalization:** reuse `parsing.parse_option_from_text` where possible; map Schwab transaction `instrument`/`assetType` to `AssetType`, `BUY/SELL` to `Side`, fees, timestamps. Route unparseable rows to the **review queue** (`ReviewQueueItem`) exactly as the CSV adapters do — do not drop (FR-1.3).

**Interface (mirror `import_robinhood_fetch` in `bootstrap.py`):**
```python
def fetch_all(account, start, end, client=None) -> tuple[list[TradeEvent], list[ReviewQueueItem]]: ...
```
`bootstrap.import_schwab_fetch(...)` then: fetch → `resolve_epoch_id` per event → `repo.upsert_events` (dedup via existing natural key) → `repo.add_review_items` → `assemble_positions` → `repo.replace_positions`. Optionally `save_events_as_csv` for the raw-CSV audit trail (FR-1.4). **Idempotency is already handled** by the `natural_key` UNIQUE constraint — re-pulling overlapping windows is safe (FR-1.2).

---

## 9. Dependent app changes (the audit fixes this data unblocks)

These are the reason the PRD's risk controls are currently inert; the Schwab data makes them real.

**(a) MTM equity — fixes audit M-2.** `engines/equity.py:equity_metrics_for_silo` and `reconstruct_silo_equity_curve` currently sum realized P&L only. Change them to accept a `MarksProvider` and add the open-leg mark value (stock at spot, option at mark × 100 × contracts) to realized cash, per PRD A.6. When the provider is `Null`, behavior is unchanged (current realized-only curve). Also fix audit **M-3** here: compute account drawdown from a *combined* equity curve, not the sum of per-silo peaks (`book_context.py:121`).

**(b) `current_delta_notional` — fixes audit H-1.** `assembly/positions.py:builder_to_position` never sets this field, so the leverage cap (PRD's "binding constraint for the options book") never engages and the UI shows 0×. Compute it during/after assembly using marks + greeks: `shares × spot + Σ(contracts × delta × 100 × spot)` (formula already exists in `engines/formulas.delta_adjusted_notional`). Because assembly shouldn't do network I/O, do this as a post-assembly enrichment step that takes a `MarksProvider` (e.g., `enrich_positions_with_marks(positions, marks)`), called from `bootstrap` after `assemble_positions`. With `Null` marks, leave `0.0` (document the limitation). **Add a test asserting it is non-zero for a stock+option position** — the current `test_sizing.py` hand-sets this value, which is why H-1 slipped through.

**(c) Local greeks — fixes audit INFO item.** `engines/options_pricing.py` has BS price + IV solve but no greeks, so the selector's theta/vega/delta scoring goes inert if a chain lacks them. Add `bs_greeks(spot, strike, t_years, rate, vol, right) -> {delta, gamma, theta, vega}` (closed-form). Then in `options_selector.py`, when a `ChainContract` greek is missing, compute it locally from spot + resolved IV instead of defaulting to `delta≈0.4 / theta=0 / vega=0`. This also makes a Schwab outage or a thin CSV degrade gracefully, and it future-proofs any cheaper/free source.

---

## 10. Rate limiting, caching, error handling

- **Rate limit:** wrap the client in a simple token-bucket or min-interval throttle targeting <120 req/min. Chain + quote fetches are user-initiated and low-volume, so this is mostly a safety net.
- **Caching:** option chains are on-demand and point-in-time — do **not** cache chains (each is an audit artifact, already persisted via `save_chain_snapshot`). Quotes for marks may be cached with a short TTL (e.g., 60–300s) to avoid hammering on a dashboard refresh.
- **Errors:** auth failure, expired refresh token, HTTP 429/5xx, and empty chains must surface as clear, actionable messages (the UI already has patterns for this in the Robinhood tab). Never crash the dashboard if marks are unavailable — fall back to `Null` marks and show a banner.
- **Determinism for tests:** all network behind the `get_client()` seam so tests inject a fake client.

---

## 11. CLI & UI surface

**CLI (`cli.py`):**
- `ta fetch-schwab --account <hash-or-label> [--start ... --end ...]` — transaction pull (mirrors `fetch-robinhood`).
- `ta options ... --source schwab` — let the existing `options` command fetch a live chain instead of `--chain <csv>`.
- (optional) `ta marks --silo ...` to preview current marks/MTM equity.

**Streamlit (`app/streamlit_app.py`):**
- **Option Selector page:** add a "Fetch live chain (Schwab)" button next to the CSV uploader; on click, `fetch_chain_snapshot(...)` → same downstream code.
- **Import Data page:** add a "Schwab API" tab alongside the Robinhood tab.
- **Settings page:** show Schwab connection status (token present/valid), mirroring the existing Robinhood status block.

---

## 12. New / changed files checklist

| File | Change |
|---|---|
| `ingestion/schwab_client.py` | NEW — `get_client()` via schwab-py token management |
| `ingestion/schwab_market_data.py` | NEW — `fetch_chain_snapshot`, `fetch_quotes`, OCC symbol converter |
| `ingestion/schwab_fetch.py` | NEW — `fetch_all` transactions → `TradeEvent[]` + review items |
| `engines/marks.py` | NEW — `Mark`, `MarksProvider` protocol, Schwab/Static/Null impls |
| `engines/options_pricing.py` | EXTEND — `bs_greeks(...)` |
| `engines/equity.py` | EXTEND — accept `MarksProvider`; MTM curve (M-2) |
| `engines/book_context.py` | EXTEND — combined-curve account drawdown (M-3) |
| `assembly/positions.py` | EXTEND — `enrich_positions_with_marks` for `current_delta_notional` (H-1); also zero closed-leg risk (M-1, small) |
| `engines/options_selector.py` | EXTEND — local greeks fallback |
| `config/env.py` | EXTEND — `SCHWAB_*` env vars + token path |
| `bootstrap.py` | EXTEND — `import_schwab_fetch`, marks wiring |
| `cli.py` | EXTEND — `fetch-schwab`, `options --source schwab` |
| `app/streamlit_app.py` | EXTEND — Schwab fetch buttons + status |
| `pyproject.toml` | EXTEND — `schwab = ["schwab-py>=…"]` extra |
| `.gitignore` | EXTEND — token file |
| `tests/` | NEW — `test_schwab_market_data.py`, `test_schwab_fetch.py`, `test_marks.py`, `test_greeks.py`, plus a position-enrichment test |

---

## 13. Testing strategy

- **No live calls in CI.** Record 1–2 real Schwab JSON responses (chain, quotes, transactions), scrub account numbers, and commit as fixtures under `tests/fixtures/schwab/`.
- **Chain mapping:** assert a recorded chain JSON → `ChainSnapshot` with greeks/IV populated and IV normalized to decimal.
- **Transactions:** assert recorded JSON → correct `TradeEvent`s, with malformed rows going to the review queue; re-import is idempotent (dedup).
- **Greeks:** unit-test `bs_greeks` against known textbook values (the existing BS price test already anchors S=100,K=100,T=1,r=.05,σ=.2 → call 10.4506; add delta≈0.6368, etc.).
- **Enrichment:** assert `current_delta_notional` is non-zero for a stock+option position given marks (closes the H-1 gap that the current fixture hides).
- **Field-name verification:** the single biggest implementation risk. Before wiring mappings, the dev should hit the live endpoints once and diff actual field names against §6/§8 tables — Schwab's naming has shifted across revisions.

---

## 14. Suggested sequencing (each independently shippable)

1. **Auth + client** (`schwab_client.py`, env, token file, `.gitignore`) — prove `get_client()` returns a live, refreshing client.
2. **ChainFetcher** → live Option Selector. Highest user-visible value, smallest blast radius (selector code unchanged).
3. **Local greeks** in `options_pricing.py` + selector fallback — independent, improves robustness immediately.
4. **QuoteFetcher + MarksProvider**, then wire MTM equity (M-2), account drawdown (M-3), and `current_delta_notional` enrichment (H-1). Ship the M-1 closed-leg-risk fix alongside.
5. **Transaction fetch** (`schwab_fetch.py`, `import_schwab_fetch`, CLI/UI) — mirrors Robinhood; lowest urgency since CSV import already works for Schwab.

---

## 15. Acceptance criteria

- **AC-1** With a valid token file, `fetch_chain_snapshot("NVDA")` returns a `ChainSnapshot` whose contracts carry greeks + IV, and the Option Selector ranks them with no code change to the engine.
- **AC-2** With Schwab marks configured, open positions show non-zero `current_delta_notional`, and the leverage cap can bind in `recommend_size` (verifiable on a constructed over-levered book).
- **AC-3** MTM silo/account equity reflects open-position marks; account drawdown is computed from a combined curve (not summed silo peaks).
- **AC-4** `ta fetch-schwab` ingests transactions idempotently into the existing store; malformed rows appear in the review queue.
- **AC-5** With Schwab **not** configured, every workflow still runs on CSV/Null-marks exactly as today (no hard dependency).
- **AC-6** No order-placement or money-movement code path exists anywhere in the adapter (PRD §2.2).
- **AC-7** No secrets are committed or logged; token file is git-ignored.

---

## 16. Risks & open questions

- **Field-name drift (high):** Schwab endpoint field names/IV units must be verified live (§13). Treat §6/§8 tables as provisional.
- **OAuth on a headless box:** the initial login flow needs a browser/redirect once; document `client_from_manual_flow` for machines without one.
- **Token expiry UX:** refresh tokens expire (~7 days — verify); the app needs a clean "re-authenticate" prompt rather than a silent failure.
- **Quote entitlements:** confirm the individual developer app returns option greeks on the quotes endpoint (chains definitely include them); if not, rely on chain greeks + local `bs_greeks` for marks.
- **Account hash vs label:** Schwab keys transactions by an account hash, not a friendly label — store the mapping in settings so the user picks a readable account name.

---

*This document specifies design and scope only; no code has been written. Mappings marked "verify" are reconstructed from Schwab documentation summaries and must be confirmed against the live API before implementation.*
