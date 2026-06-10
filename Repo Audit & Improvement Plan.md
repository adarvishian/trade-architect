# Trading Architect — Repository Audit & Improvement Plan

**Date:** 2026-06-09 · **Scope:** full repo, analysis only (no code modified) · **Method:** systematic read of all `src/`, `app/`, `tests/` files; test suite executed; every cited finding re-verified against source.

> **Verification note:** the audit sandbox runs Python 3.10 while the project requires ≥3.11. The 7 test failures observed there (`tests/test_schwab_transactions.py`, `tests/test_schwab_fetch.py`) are a 3.10 artifact — `datetime.fromisoformat("…+0000")` (`src/trading_architect/ingestion/schwab_transactions.py:69`) only parses offset-without-colon on 3.11+. On the supported runtime the suite is expected to pass 169/169. Claims below that could not be fully verified are labeled explicitly.

---

## 1. Executive Summary

**Overall health: B.** The domain core — sizing engine, Kelly/adaptive risk, options selector, position assembly, dedup — is genuinely well-engineered: layered cleanly, typed with Pydantic, covered by 169 mostly behavior-asserting tests including golden-number checks against textbook Black-Scholes values. What keeps it from an A is everything *around* the core: the project is **not under version control at all**, has no CI/lint/type gate, contains silent exception-swallowing in the ingestion path of a tool whose entire value proposition is auditable numbers, and ships zero tests for its two user-facing surfaces (CLI, 479 lines; Streamlit app, 1,083 lines). **Top 3 risks:** (1) no git — one bad edit or disk failure loses 10.7k lines of working code and the SQLite store with it; (2) silently dropped Robinhood option orders corrupt the very position history that sizing decisions are computed from; (3) untested CLI/UI means regressions in the only interfaces the user touches go unnoticed. **Top 3 opportunities:** (1) a half-day "safety net" milestone (git + CI + ruff) makes every future change cheap and safe; (2) finishing the half-done Schwab module migration and deleting scaffolding removes most of the confusion in `ingestion/`; (3) converting silent fallbacks (stale marks, dropped rows) into visible review-queue items directly strengthens the product's core promise of auditability.

---

## 2. Repo Map

**Purpose.** Local, single-user position-sizing and trade-evaluation system for a directional trader: ingest broker history (Robinhood, Schwab/thinkorswim, Tradovate; CSV or API), assemble positions in two strictly siloed books (stock+options vs. futures), recommend size via a six-layer engine (fractional-Kelly with bootstrap CIs, drawdown governor, correlation-aware throttle), and quantify "alpha left on the table" from past sizing. No order placement, no signals — by design (`context.md`, `role.md`).

**Stack.** Python ≥3.11, pandas/numpy/scipy, Pydantic v2, SQLite (stdlib `sqlite3`), Streamlit UI, optional extras for `schwab-py`/`httpx`/`websockets` and `robin-stocks` (`pyproject.toml:8-24`). Maturity: polished personal tool / advanced prototype — calibration target for all recommendations below.

**Architecture sketch.** Clean one-directional layering with a composition root:

```
models/entities.py (Pydantic)  →  store/ (SQLite + dedup)  →  ingestion/ (CSV adapters + Schwab/RH API)
        →  assembly/ (positions, FIFO closed-trade entries)  →  engines/ (sizing, kelly, drawdown,
        options pricing/selector, evaluation, marks)  →  cli.py / app/streamlit_app.py
bootstrap.py = composition root wiring repo + ingestion + assembly + marks providers
```

**Key directories.**

| Path | Role |
|---|---|
| `src/trading_architect/models/` | Canonical entities (TradeEvent, Position, ChainSnapshot); natural-key + content-fingerprint dedup identity |
| `src/trading_architect/store/` | SQLite schema/migrations (`database.py`), persistence + dedup index (`repository.py`) |
| `src/trading_architect/ingestion/` | 3 CSV adapters + Schwab OAuth/REST/streamer/transactions + Robinhood API fetcher |
| `src/trading_architect/assembly/` | Event → position building, mark enrichment, FIFO closed-trade extraction |
| `src/trading_architect/engines/` | All financial math; mostly pure functions + frozen dataclasses |
| `src/trading_architect/config/` | Dataclass configs + single-source constants (`defaults.py`) |
| `app/` | Streamlit UI (single 1,083-line file, 9 pages) |
| `tests/` | 169 tests + scrubbed Schwab JSON fixtures (no live network) |
| `data/` | SQLite DB, archived raw CSVs, OAuth token (all correctly `.gitignore`d) |

**Surprises found during mapping.**
- **No `.git` directory** — the `.gitignore` is well-curated for a repo that doesn't exist.
- Three `schwab_*` modules are labeled "backward-compatible re-exports" yet one still holds canonical logic that production code imports (detail in §3).
- A previous self-audit (`Audit - Buildout vs PRD.md`, 2026-05-26) lists open HIGH/MEDIUM gaps — but code inspection shows most were since fixed, making the document stale in the *good* direction (detail in §3, Documentation).
- `repository.py` defines `class Repository:` **twice**, back to back.

---

## 3. Audit Report

Severity scale: **Critical / High / Medium / Low**. Each finding is labeled **[Fact]** (verified at the cited lines) or **[Judgment]**.

### 3.1 DevEx & Operations — the ugliest part

**OPS-1 · Critical · [Fact] — No version control.** There is no `.git` directory anywhere in the project (verified; `git rev-parse` fails at the filesystem boundary). A 10.7k-line codebase with real money-adjacent logic has no history, no diffability, no rollback, and no offsite copy. Combined with the un-backed-up `data/trading_architect.db`, a single bad edit, a misfired "cleanup," or disk failure is unrecoverable. This is the single highest-leverage fix in the entire audit.

**OPS-2 · High · [Fact] — No CI, no lint/format/type enforcement.** No CI config exists; `pyproject.toml` has no ruff/black/mypy sections. The 169-test suite only protects you if something runs it. Consequence: regressions land silently, and style/typing drift accumulates in a single-author codebase that an AI assistant also edits — exactly the setup where automated gates pay for themselves fastest.

**OPS-3 · Medium · [Fact] — No dependency lockfile; `requirements.txt` duplicates and can drift from `pyproject.toml`.** Both declare loose `>=` ranges (`pyproject.toml:8-16`, `requirements.txt:1-7`); `requirements.txt` additionally omits the schwab/robinhood extras. A pandas or pydantic major-version jump can break parsing behavior with no record of the last-known-good set.

**OPS-4 · Low · [Fact] — No DB backup story.** `data/trading_architect.db` is the system of record for all imported history; nothing in repo or docs creates copies (raw CSVs in `data/raw/` are a partial mitigation for re-import).

### 3.2 Code Quality

**CQ-1 · High · [Fact] — Duplicate `Repository` class definition.** `src/trading_architect/store/repository.py:41-45` defines `Repository.__init__`, immediately shadowed by the real definition at `repository.py:47`. Harmless at runtime (last definition wins) but it is dead code at the heart of the persistence layer, confuses navigation ("go to definition" lands on the corpse), and suggests an interrupted edit that was never reviewed — a direct symptom of OPS-1/OPS-2.

**CQ-2 · High · [Fact] — Robinhood ingestion silently drops orders.** `src/trading_architect/ingestion/robinhood_fetch.py:433-435` (`except Exception: resolved = None` when resolving a ticker) and `:531-533` (`except Exception: continue` when resolving an option leg). A failed lookup makes the entire order vanish from the import with **no log line and no review-queue entry**. For a system whose outputs (Kelly-f, drawdown, alpha-left) are computed from this history, silently missing trades is a correctness issue, not a style issue. The Schwab path does this right — malformed items go to a persisted review queue (`schwab_transactions.py`, surfaced in UI/CLI).

**CQ-3 · Medium · [Fact] — Half-finished module migration: "compat shims" are still load-bearing.** `schwab_fetch.py` (8 lines), `schwab_client.py` (29 lines), `schwab_market_data.py` (44 lines) all say "Backward-compatible re-exports — prefer X for new code", but: `schwab_market_data.py:18-33` contains the *only* definition of `parse_synthetic_option`, imported by production code at `schwab_accounts.py:14` and `robinhood_fetch.py:366`; `schwab_client.py` is imported by `engines/marks.py:248` and `app/streamlit_app.py:34`; `schwab_fetch.py` is used only by `tests/test_schwab_fetch.py` (a duplicate of `test_schwab_transactions.py` coverage). Canonical logic living in a module documented as deprecated is a trap for future edits.

**CQ-4 · Medium · [Fact] — Broad `except Exception` handlers in the UI surface raw exception text.** `app/streamlit_app.py:261, 294, 392, 476, 664, 1058` all render `st.error(f"... {exc}")` for any exception type. Auth failures, file paths, and library internals get shown verbatim; actionable cases (e.g. `SchwabAuthExpired`, handled specially only at `:476`) are mixed with genuine bugs that should be loud, not prettified.

**CQ-5 · Medium · [Fact] — Hardcoded option contract multiplier `100` scattered across layers.** Examples: `engines/formulas.py:39`, `engines/equity.py:85`, `assembly/entries.py` (P&L math), `assembly/positions.py` (delta-notional), `engines/options_selector.py`. One constant in `config/defaults.py` would document intent and centralize the assumption.

**CQ-6 · Medium · [Judgment] — Complexity hotspot: `recommend_size()`.** `src/trading_architect/engines/sizing.py:169-400` (~230 lines): input validation, six sizing layers, ceiling application, and a six-way binding-constraint classification chain in one function. It is well-commented and tested at the behavior level, but the binding-constraint branches (~`:350-386`) are hard to verify as mutually exclusive by inspection. Worth splitting when next touched; not urgent.

**CQ-7 · Low · [Fact] — Uploaded-file temp files never deleted.** `app/streamlit_app.py:146` and `:668` use `NamedTemporaryFile(delete=False)`; no cleanup follows. Slow disk accumulation on a long-lived machine.

**CQ-8 · Low · [Fact] — `st.cache_resource.clear()` nukes the whole resource cache to bust one entry.** `app/streamlit_app.py:151, 250, 468` clear globally after imports, recreating the Repository (and re-running migrations) on next access. Works, but defeats the `@st.cache_resource` on `get_repository()` (`:52`); a scoped invalidation or `session_state` handle would be cleaner.

### 3.3 Correctness & financial-math edge cases (engines)

The math core is in good shape — pure functions, frozen dataclass outputs, bootstrap CIs on optimal-f, conservative defaults. Remaining findings are edge-hardening, not bugs found in the happy path:

**EN-1 · Medium · [Fact] — Missing marks silently fall back to cost basis in MTM equity.** `src/trading_architect/engines/equity.py:82`: `mark = marks_by_symbol.get(symbol, cost)` — an illiquid or failed-fetch leg contributes exactly 0 to open P&L with no indication. The drawdown governor consumes this equity curve, so stale marks can mask a real drawdown below a throttle threshold. Same pattern: IV fallback hardcoded to 0.2 in `engines/marks.py:91` for delta estimation.

**EN-2 · Medium · [Fact] — Option repricing truncates expired-at-horizon contracts to intrinsic with no flag.** `engines/options_selector.py:~158`: `dte_remaining = max(0, contract.dte - expected_hold_days)` — a contract that expires *before* the intended hold is priced at t=0 intrinsic and still ranked, rather than being excluded or flagged. (Line ± a few; behavior verified by read.)

**EN-3 · Low · [Fact] — Degenerate-input guards return quiet zeros.** `engines/drawdown.py:41-44` returns 0.0 drawdown when `peak_equity <= 0`; `engines/correlation.py:89-90` returns `None` on zero-variance series and the caller falls back to the assumed default (0.6) without logging. Both are defensible defaults; the issue is silence, consistent with EN-1.

**EN-4 · Low · [Fact] — `scipy.stats.skew` precision warning on near-identical R samples.** `engines/r_distribution.py:53` emits `RuntimeWarning: Precision loss…` during tests. Cosmetic today; guard for `len(set) ≈ 1` samples when convenient.

**Corrections to the record (claims checked and rejected):** the suspected unguarded `optimal_f` access in sizing is in fact guarded (`engines/sizing.py:269` `if optimal_f is not None:`); drawdown thresholds are *not* duplicated — both configs import the same constants from `config/defaults.py:42-44` (`config/adaptive_risk.py:46-48`, `config/sizing.py:46-48`); `delta_adjusted_notional` (`formulas.py:29-40`) only multiplies, so `spot=0` cannot divide-by-zero.

### 3.4 Security

Healthy for its threat model (local, single-user, read-only API scopes). Specifics:

**SEC-1 · Medium · [Fact] — Plaintext broker credentials in `.env`, including a Robinhood password.** Real values are present (verified key lengths only; contents not read into this report). `.gitignore` covers `.env` and the token file — but with no git repo, the protection is currently theoretical, and the moment OPS-1 is fixed an accidental `git add .env` becomes the risk. The Schwab token file is correctly written then `chmod 600` (`ingestion/schwab_auth.py:87-92`; a brief pre-chmod window exists — Low).

**SEC-2 · Low · [Fact] — UI accepts Robinhood password as a text input and threads it through function args** (`app/streamlit_app.py:~240-250`, `ingestion/robinhood_fetch.py` entry points). Acceptable locally; prefer env-only to keep secrets out of Streamlit widget state.

No injection vectors: every SQL statement in `store/` is parameterized (verified across `repository.py`); no `eval`/pickle/unsafe deserialization found. Dependencies are mainstream and current-generation; no known-CVE pins spotted (not exhaustively scanned — no lockfile to scan, see OPS-3).

### 3.5 Testing

**TS-1 · High · [Fact] — Zero tests for both user-facing surfaces.** No test imports `cli.py` (479 lines: arg parsing, command dispatch, output formatting) or `app/streamlit_app.py` (1,083 lines). `tests/test_m6_ui.py` is mislabeled — it tests drawdown/equity/book-context backend logic and never imports streamlit. The interfaces the user actually touches every day have no regression net.

**TS-2 · Medium · [Fact] — No direct tests for the persistence layer.** `store/repository.py` (459 lines incl. the dedup index and multi-statement upsert) and `store/database.py` (migrations) are exercised only incidentally via CSV-import and settings-roundtrip tests.

**TS-3 · Low · [Fact] — A few assertions are weaker than they look.** `tests/test_options_pricing.py` "put-call parity" test only asserts both prices are positive, not the parity identity; `tests/test_sizing.py:154` hand-sets `current_delta_notional=10_000.0` instead of asserting the assembly/enrichment pipeline produces it (the pipeline *is* separately covered in `tests/test_position_enrichment.py`).

**Strengths:** golden-number tests against textbook values (`tests/test_greeks.py:4-11`, ATM delta ≈ 0.6368; `tests/test_assembly.py:49-52`, explicit delta-notional formula `100×150 + 5×0.45×100×150 = 48,750`); idempotent-reimport tests for all three brokers; deterministic seeds for bootstrap tests; Schwab API fully fixture-mocked (no live network in tests).

### 3.6 Performance

Adequate for scale (one user, thousands of events). Two real items: **PF-1 · Medium · [Fact]** — SQLite opened with defaults: no `journal_mode=WAL`, no `busy_timeout` (`store/database.py:132-143`). Streamlit reruns + a concurrently open CLI can hit `database is locked`. Two pragmas fix it. **PF-2 · Low · [Fact]** — fingerprint backfill migration loads every row into Python (`store/database.py:165-181`) and runs on each init; fine now, slow at 100k+ events. Everything else (blocking fetches under `st.spinner`, full reassembly after import) is proportionate to the tool's size.

### 3.7 Dependencies

Healthy in one sentence: a small, mainstream, well-chosen stack with optional extras correctly gating broker SDKs — the only gaps are the missing lockfile and the `requirements.txt`/`pyproject.toml` duplication already covered in OPS-3. One watch-item **[Judgment]**: `robin-stocks` wraps Robinhood's private API and breaks whenever Robinhood changes internals; treat `robinhood_fetch.py` (633 lines, the largest ingestion module) as inherently fragile and keep the CSV path first-class.

### 3.8 Documentation

**DOC-1 · Medium · [Fact] — `Audit - Buildout vs PRD.md` (2026-05-26) is stale and now *understates* the codebase.** Its headline gaps have since been fixed in code: H-1 and M-1 by `enrich_positions_with_marks` (`assembly/positions.py:299-339` — docstring literally cites "H-1, M-1"; covered by `tests/test_position_enrichment.py`); M-4 (in-sample Kelly look-ahead) by `_prior_epoch_optimal_f` (`engines/evaluation.py:121-141`); M-3 by a combined MTM account curve (`engines/equity.py:293`). Anyone (human or AI assistant) reading that document today would re-fix solved problems or distrust correct code. It needs a resolution header or archiving.

**DOC-2 · Low · [Fact] — README is accurate and unusually good.** All quick-start commands verified to exist in `cli.py`; sample CSV paths exist; milestone table matches code reality post-fixes. Gap: no "known limitations" section (e.g., stale-mark fallback EN-1, Robinhood API fragility).

### 3.9 Strengths to preserve

The layering and composition root (`bootstrap.py`) keep UI, CLI, and engines honestly separated — business logic never lives in Streamlit. Dedup design (natural key + content fingerprint, idempotent reimports) is the best part of the data layer. Engines are pure functions returning frozen dataclasses with human-readable `detail` strings per sizing layer — transparency is a product feature here, and the code structure delivers it. Conservative-by-default risk math (lower-CI Kelly, 0.6 assumed correlation, throttle ladder) matches the stated ¼-Kelly / 20% MTM-DD profile. The Schwab integration's review-queue pattern for malformed data is exactly right — the fix for CQ-2 is to spread that pattern, not invent a new one.

---

## 4. Improvement Strategy

**Theme 1 — Working core, zero safety rails (OPS-1/2/3/4, CQ-1).**
*Target state:* the project is a git repo with an initial tagged commit; ruff (lint+format) and pytest run on every change via a lightweight gate (pre-commit or GitHub Actions); dependencies have a lockfile; the DB has a scheduled copy.
*Principle:* protect the asset before improving it — every other task in this plan becomes reversible and reviewable once this exists.

**Theme 2 — Silence is the enemy of auditability (CQ-2, CQ-4, EN-1/2/3).**
*Target state:* nothing that affects position history or equity can fail invisibly. Failed Robinhood resolutions land in the same review queue Schwab uses; the equity/marks layer reports how many legs used fallback marks and the UI shows it; expired-at-horizon contracts are flagged or excluded.
*Principle:* the product's promise is "credible and auditable" numbers (PRD FR-6.2); the codebase should be structurally incapable of quiet data loss.

**Theme 3 — Finish the moves you started (CQ-3, CQ-1, DOC-1, TS-3 duplicate test file).**
*Target state:* shim modules deleted with imports repointed (`parse_synthetic_option` relocated to `schwab_symbols.py`), duplicate class removed, stale audit doc archived with a resolution table, duplicate `test_schwab_fetch.py` removed.
*Principle:* half-done migrations cost more than either finishing or never starting — especially in an AI-assisted workflow where stale signposts mislead every future session.

**Theme 4 — Test the surfaces the user touches (TS-1, TS-2).**
*Target state:* smoke-level CLI tests (each subcommand parses args and runs against a tmp DB with fixture CSVs) and direct repository-layer tests (upsert/dedup/rollback). Streamlit gets `streamlit.testing.v1.AppTest` smoke coverage of page rendering, not pixel tests.
*Principle:* coverage should follow user exposure, not code interestingness — the engines are over-covered relative to the CLI.

**Explicitly NOT recommended (effort > payoff at this maturity):** splitting `streamlit_app.py` into a multipage app *now* (do it opportunistically when a page next grows — the god file is organized and business-logic-free); async/non-blocking UI fetches; multi-user/Postgres; secrets vault or OS keychain (env-file hygiene suffices locally); exhaustive UI e2e tests (Playwright); mypy strict-mode across the board (type hints are already good; a CI `ruff` + light `mypy` on `engines/` is the 80/20).

**"Done" looks like:** repo under git with CI green on 3.11; zero Critical and zero High findings open; `grep -rn "except Exception" src/ | wc -l` reduced to handlers that log/route (no bare swallow-and-continue in ingestion); review queue captures 100% of dropped rows across all brokers; CLI + repository test files exist and pass; the stale audit doc carries a resolution header; lockfile present.

---

## 5. Task Plan

### Milestone 0 — Safety net (do first, ~1 day total)

| # | Task | Files/areas | Acceptance criteria | Effort | Risk | Depends |
|---|---|---|---|---|---|---|
| 0.1 | `git init`, initial commit, tag `v0.1.0-audit` | repo root | History exists; `.env`, `data/` confirmed excluded by `.gitignore` before first commit | S | None | — |
| 0.2 | Add ruff (lint+format) config to `pyproject.toml`; fix/silence baseline | `pyproject.toml`, mechanical touch-ups | `ruff check .` and `ruff format --check .` pass | S | Low (format-only diffs) | 0.1 |
| 0.3 | CI gate: run ruff + pytest on Python 3.11 (GitHub Actions if remoted; else pre-commit hook) | `.github/workflows/` or `.pre-commit-config.yaml` | A failing test or lint blocks the commit/PR | S | None | 0.1, 0.2 |
| 0.4 | Lockfile: `pip-compile` (or `uv lock`) from `pyproject.toml`; delete or generate `requirements.txt` from it | `requirements*.txt` | Clean-room install from lockfile runs the suite green | S | Low | 0.1 |
| 0.5 | DB backup: script or cron copying `data/trading_architect.db` (SQLite `.backup`) to a dated file | `scripts/` | Restorable dated copies exist; documented in README | S | None | — |

### Milestone 1 — Critical & correctness fixes

| # | Task | Files/areas | Acceptance criteria | Effort | Risk | Depends |
|---|---|---|---|---|---|---|
| 1.1 | Route Robinhood resolution failures to the review queue instead of silent drop | `ingestion/robinhood_fetch.py:433-435, 531-533` | Forced-failure test shows a `ReviewQueueItem` per dropped order; zero silent `continue` in ingestion | M | Med (touches import path — cover with test first) | 0.1–0.3 |
| 1.2 | Remove duplicate `Repository` class | `store/repository.py:41-45` | Single definition; suite green | S | None | 0.1 |
| 1.3 | SQLite pragmas: `journal_mode=WAL`, `busy_timeout=5000` | `store/database.py:132-143` | Concurrent CLI+UI access test (two connections) passes; no `database is locked` | S | Low | 0.1 |
| 1.4 | Surface mark fallbacks: count legs priced at cost basis / default IV; expose in `ta marks`, Dashboard, and equity metrics | `engines/equity.py:82`, `engines/marks.py:91`, UI/CLI display | Stale-mark count visible wherever MTM equity is shown; unit test for fallback counting | M | Med | 0.1–0.3 |
| 1.5 | Archive stale self-audit: add resolution table (H-1/M-1/M-3/M-4 → fixed at cited lines) or move to `docs/archive/` | `Audit - Buildout vs PRD.md` | Document states current truth; README links it | S | None | — |
| 1.6 | Flag/exclude contracts expiring before intended hold in selector | `engines/options_selector.py:~158` | Candidate with `dte < hold_days` is excluded or carries a visible flag; test added | S | Low | 0.1–0.3 |

### Milestone 2 — High-leverage improvements

| # | Task | Files/areas | Acceptance criteria | Effort | Risk | Depends |
|---|---|---|---|---|---|---|
| 2.1 | Finish Schwab module migration: move `parse_synthetic_option` into `schwab_symbols.py`, repoint 6 import sites, delete 3 shims + duplicate `test_schwab_fetch.py` | `ingestion/schwab_{market_data,client,fetch}.py`, `schwab_accounts.py:14`, `robinhood_fetch.py:366`, `engines/marks.py:248`, `app/streamlit_app.py:34`, tests | Shims deleted; `grep -rn "schwab_client\|schwab_fetch\|schwab_market_data" src app tests` returns nothing; suite green | M | Med (import surgery — trivially CI-verified) | 0.3 |
| 2.2 | CLI smoke tests: each subcommand against tmp DB + fixture CSVs; assert exit codes and key output strings | new `tests/test_cli.py` | Every subcommand covered at parse+run level | M | None | 0.3 |
| 2.3 | Repository-layer tests: upsert/dedup/cross-key fingerprint, rollback on mid-import failure, settings roundtrip, migration idempotency | new `tests/test_repository.py` | Persistence behaviors asserted directly | M | None | 0.3 |
| 2.4 | Typed UI error handling: catch known exceptions (`SchwabAuthExpired`, network, parse) with friendly messages; unknown exceptions logged with traceback to a log file, generic message shown | `app/streamlit_app.py:261, 294, 392, 476, 664, 1058`; small `ui_helpers.py` wrapper | No raw `{exc}` for unknown errors; auth-expiry shows reconnect CTA everywhere | M | Low | 0.1 |
| 2.5 | Extract `OPTION_CONTRACT_MULTIPLIER = 100` to `config/defaults.py`; replace all literals | `formulas.py:39`, `equity.py:85`, `assembly/*`, `options_selector.py` | `grep -n "\* 100" src/` shows no contract-multiplier literals; suite green | S | Low | 0.3 |
| 2.6 | Streamlit `AppTest` smoke tests: each page renders without exception against seeded tmp DB | new `tests/test_streamlit_smoke.py` | 9/9 pages render in CI | M | Low | 2.3 |

### Milestone 3 — Quality & polish

| # | Task | Files/areas | Acceptance | Effort | Risk | Depends |
|---|---|---|---|---|---|---|
| 3.1 | Clean temp files after upload parsing (`try/finally` unlink) | `app/streamlit_app.py:146, 668` | No orphaned tmp files after import | S | None | — |
| 3.2 | Replace global `st.cache_resource.clear()` with scoped invalidation | `app/streamlit_app.py:151, 250, 468` | Repository survives imports; data still refreshes | S | Low | 2.6 |
| 3.3 | Strengthen math tests: true put-call parity identity; optimal-f floor; zero-variance skew guard (EN-4) | `tests/test_options_pricing.py`, `tests/test_evaluation.py`, `engines/r_distribution.py:53` | Parity asserted to tolerance; no RuntimeWarning in suite | S | None | 0.3 |
| 3.4 | Refactor `recommend_size()`: extract layer pipeline + binding-constraint resolver | `engines/sizing.py:169-400` | Same outputs on existing tests (golden behavior lock first); function < ~80 lines | L | Med | 2.x tests |
| 3.5 | Config-ify IV fallback (0.2) and correlation fallback logging | `engines/marks.py:91`, `engines/correlation.py:89` | Values in `config/defaults.py`; fallback use logged | S | Low | — |
| 3.6 | README "Known limitations" section + document degenerate-input behaviors (EN-3) | `README.md`, docstrings | Stale-mark, RH fragility, expiry-truncation documented | S | None | 1.4 |
| 3.7 | Opportunistic: split Streamlit into `st.Page` multipage when next adding a page | `app/` | Only when touched anyway | XL→defer | Med | — |

### Quick wins (start today, all S-effort, high impact)
**0.1 git init** · **1.2 delete duplicate class** · **1.3 WAL pragmas** · **1.5 archive stale audit doc** · **3.1 temp-file cleanup**. Combined: under half a day, removes one Critical, one High, and the most misleading artifact in the repo.

### Implementation sketches — top 3 tasks

**0.1–0.3 Safety net.** Order matters: verify `.gitignore` covers `.env`, `data/`, `*.egg-info`, `.pytest_cache` (it does — confirm nothing else sensitive: `git status --ignored` before first commit, eyeball for the `.mhtml` docs if you consider them private). `git init && git add -A && git status` → review → commit → tag. Then ruff: start with `[tool.ruff] line-length = 100` + default rules, run `ruff format`, commit the mechanical diff separately so it never pollutes a logic diff. CI: if the repo stays local-only, use pre-commit (`pytest -q` may be too slow per-commit; run lint per-commit, tests pre-push). Gotcha: run CI on Python 3.11+ only — 3.10 reproduces the spurious `fromisoformat` failures documented above.

**1.1 Robinhood review-queue routing.** Mirror the Schwab pattern: `parse_transactions_response` returns `(events, review_items)` (`schwab_transactions.py`). Change `_resolve_option_leg` callers so the `except Exception` branch appends `ReviewQueueItem(source_file="robinhood-api", reason=f"option leg unresolved: {exc}", raw_row=order)` instead of `continue`; same for ticker resolution returning `""`. Thread the review list through `import_robinhood_fetch` → `repo` the same way the Schwab fetch does, so the existing Review Queue page (`streamlit_app.py:902-911`) displays them for free. Gotcha: the resolver calls the network per-leg — wrap only the resolution call, not the whole loop body, so one bad leg doesn't queue the whole batch. Write the failing test first (mock `rh.get_symbol_by_url` to raise).

**1.3 + 2.3 WAL + repository tests.** In `Database.connect()` after `sqlite3.connect(self.path)`: `conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA busy_timeout=5000")`. WAL is persistent per-database but harmless to re-issue. Gotchas: WAL creates `-wal`/`-shm` sidecar files — add to `.gitignore` and include in the backup script (or checkpoint before copy: `PRAGMA wal_checkpoint(TRUNCATE)`); the backup task 0.5 should use `sqlite3 .backup`, not file copy, once WAL is on. Repository tests then get a clean harness: `Database(tmp_path / "t.db")` per test, no shared state.

---

## 6. Open Questions

1. **Version control intent** — is the absence of git deliberate (privacy concern about cloud remotes)? A local-only repo with no remote still captures 90% of the value; a private GitHub remote adds offsite backup. Which do you want?
2. **Robinhood API path** — `robinhood_fetch.py` is the largest, most fragile ingestion module (private-API wrapper). Is the Roth account's API fetch still essential, or has CSV/manual cadence made it deprecable? This decides whether task 1.1 is a must or a nice-to-have.
3. **The `.mhtml` Schwab documentation files** (~4 large captures in the repo root) — keep in-repo (and in git) as reference, or move to a `docs/vendor/` folder / exclude from version control?
4. **Streamlit vs CLI priority** — which surface do you actually use daily? That should pull forward either 2.2 (CLI tests) or 2.4/2.6 (UI hardening) within Milestone 2.
5. **Performance envelope** — current DB is small; do you expect >50k trade events (e.g., importing full multi-year futures history)? If yes, PF-2 (migration scan) and full-reassembly-on-import deserve promotion from Low.

---

*Areas receiving lighter review (disclosed per audit rules): `schwab_streamer.py` (448 lines — WebSocket reconnect logic read at summary level, not line-audited), the three `.mhtml` vendor docs, the `.docx` user guide, and pixel-level Streamlit layout code. Nothing in the lighter-reviewed set feeds the sizing math directly.*
