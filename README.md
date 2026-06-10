# Trading Architect

Local, single-user system for risk-adjusted position sizing, historical sizing evaluation ("alpha left on the table"), and options contract selection.

Stock/options data can come from **CSV imports** or a live **Schwab Trader API** connection (OAuth). Futures stay on Tradovate CSV only — silos are never blended.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock.txt
pip install -e . --no-deps

# Or editable install with extras (unpinned — prefer lockfile above)
# pip install -e ".[dev,schwab]"

# Copy credentials to .env (see .env.example) — loaded automatically
cp .env.example .env

# Import broker CSV
ta import "sample csv/Robinhood Sample.csv" --broker robinhood --account robinhood-individual
ta import "sample csv/Charles Schwab Transactions May 26 2026 Sample.csv" --broker schwab
ta import "sample csv/Tradovate Performance Sample.csv" --broker tradovate

# Check store
ta status

# Size a candidate trade (M3)
ta size --silo stock_options --underlying AAPL --entry 100 --stop 95 --equity 100000

# Edge & risk-appetite review (M4)
ta risk
ta risk --silo stock_options --drawdown 0.12

# Options contract selector (M5) — chain CSV or live Schwab chain
ta options --chain path/to/chain.csv --underlying NVDA --spot 200 --target 210 \
  --hold-days 60 --direction long_call --equity 100000 --save-snapshot

ta options --source schwab --underlying NVDA --target 210 \
  --hold-days 60 --direction long_call --equity 100000 --save-snapshot

# Launch UI
streamlit run app/streamlit_app.py
```

## Schwab Trader API (live connection)

Install the optional Schwab extra, then add credentials to `.env`:

```bash
pip install -e ".[schwab]"
```

```bash
# .env
SCHWAB_API_KEY=your_app_key
SCHWAB_APP_SECRET=your_app_secret
# SCHWAB_CALLBACK_URL=https://127.0.0.1:8182   # default
# SCHWAB_TOKEN_PATH=data/.schwab_token.json     # default; git-ignored
```

**One-time OAuth** (opens browser; token saved locally with `chmod 600`):

```bash
ta fetch-schwab --login
```

Refresh tokens expire after **7 days** — re-run `--login` when prompted.

### Schwab CLI commands

| Command | Purpose |
|---------|---------|
| `ta fetch-schwab --login` | Interactive OAuth setup |
| `ta accounts snapshot [--label schwab-tos]` | Cash, buying power, open positions, **MTM equity** (`liquidationValue`) |
| `ta fetch-schwab --account schwab-tos --start YYYY-MM-DD --end YYYY-MM-DD` | Pull transactions → deduped `TradeEvent`s + review queue |
| `ta marks [--silo stock_options\|futures\|all]` | Preview live marks (Streamer + REST fallback) |
| `ta options --source schwab --underlying NVDA …` | Fetch live option chain for the selector |

The Streamlit app mirrors this under **Import Data → Schwab API** (portfolio snapshot, transaction import) and **Option Selector → Fetch live chain (Schwab)**. Settings shows connection status, token expiry countdown, and account label↔hash mapping.

**Scope limits (by design):** marks, balances, positions, and transaction history only — no order placement or money movement.

## Robinhood API (optional)

Robinhood Roth IRA and similar accounts without CSV export:

```bash
pip install -e ".[robinhood]"
# RH_USERNAME / RH_PASSWORD in .env

ta fetch-robinhood --account robinhood-roth
ta fetch-robinhood-portfolio
```

## Development phases

### Core product (PRD §9)

| Phase | Status | Scope |
|-------|--------|-------|
| M1 | Done | Ingestion, normalized store, position assembly |
| M2 | Done | Evaluation engine — alpha left on the table |
| M3 | Done | Six-layer sizing engine |
| M4 | Done | Adaptive Kelly / risk appetite |
| M5 | Done | Options contract selector |
| M6 | Done | Streamlit UI — dashboard, settings, drawdown governor |

### Schwab Trader API integration

| Phase | Status | Scope |
|-------|--------|-------|
| P0 — Auth | Done | OAuth session, token file, auto-refresh, reconnect UX |
| P1 — Accounts | Done | Hash resolver, balances/positions snapshot, live MTM equity → drawdown governor |
| P2 — Streamer | Done | WebSocket marks (equities + options w/ greeks), reconnect, delta-notional enrichment |
| P3 — REST market data | Done | `/chains` + `/quotes` fallback, live chain in Option Selector, local greeks fill |
| P4 — Transactions | Done | Transaction fetch → idempotent `TradeEvent` ingest, review queue, audit CSV |

## Project layout

```
src/trading_architect/
  models/       Canonical entities (TradeEvent, Position, ChainSnapshot, …)
  store/        SQLite persistence + dedup
  ingestion/    CSV adapters + API fetchers
                  schwab_auth.py       OAuth session (single accessor)
                  schwab_accounts.py   Balances, positions, MTM equity snapshot
                  schwab_streamer.py   WebSocket Level 1 marks
                  schwab_rest.py       Option chains + quote fallback
                  schwab_transactions.py  Transaction history → TradeEvent[]
                  schwab_symbols.py    OCC ↔ internal option symbology
                  robinhood_fetch.py   Robinhood API (Roth / no CSV)
  assembly/     Position assembly + mark enrichment
  engines/      Sizing, evaluation, equity/drawdown, marks, options selector
app/            Streamlit UI
tests/          Unit tests + scrubbed Schwab fixtures (no live network in CI)
data/           Local DB, archived raw CSVs, .schwab_token.json (git-ignored)
```

## Development

Requires **Python 3.11+**. CI runs on 3.11.

```bash
pip install -e ".[dev,schwab]"   # or use requirements.lock.txt (see Quick start)

ruff check .                     # lint
ruff format --check .            # format check
ruff format .                    # apply formatting
pytest -q                        # 169 tests, no live network
```

Regenerate the lockfile after changing dependencies in `pyproject.toml`:

```bash
pip install pip-tools
pip-compile pyproject.toml --extra=dev --extra=schwab -o requirements.lock.txt
```

GitHub Actions (`.github/workflows/ci.yml`) runs ruff + pytest on every push/PR to `main`.

## Database backup

The SQLite store (`data/trading_architect.db`) is git-ignored and is the system of record. Back it up periodically:

```bash
python scripts/backup_db.py
# → data/backups/trading_architect_YYYYMMDDTHHMMSSZ.db
```

Uses `sqlite3.Connection.backup()` with a WAL checkpoint for a consistent copy. Schedule via cron if desired, e.g. daily at 2am:

```bash
0 2 * * * cd /path/to/trading-architect && python scripts/backup_db.py
```
