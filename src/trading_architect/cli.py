"""CLI entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

import trading_architect  # noqa: F401 — loads .env on import
from trading_architect.bootstrap import create_repository, import_and_assemble


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ta",
        description="Trading Architect — position sizing & evaluation",
    )
    sub = parser.add_subparsers(dest="command")

    imp = sub.add_parser("import", help="Import broker CSV and assemble positions")
    imp.add_argument("csv", type=Path, help="Path to broker CSV export")
    imp.add_argument(
        "--broker",
        required=True,
        choices=["robinhood", "schwab", "tradovate"],
        help="Broker adapter to use",
    )
    imp.add_argument(
        "--account",
        help="Account label (e.g. robinhood-ira, robinhood-roth, schwab-tos)",
    )

    fetch = sub.add_parser(
        "fetch-robinhood",
        help="Fetch Robinhood history via API (for accounts without CSV export)",
    )
    fetch.add_argument(
        "--account",
        default="robinhood-roth",
        help="Account label (default: robinhood-roth)",
    )
    fetch.add_argument(
        "--save-csv",
        type=Path,
        help="Optional path to save fetched data as CSV for audit",
    )

    rh_portfolio = sub.add_parser(
        "fetch-robinhood-portfolio",
        help="Fetch Robinhood cash balances and holdings (Roth + individual)",
    )
    rh_portfolio.add_argument(
        "--account",
        action="append",
        dest="accounts",
        help="Account label to include (repeatable; default: all linked accounts)",
    )

    schwab_fetch = sub.add_parser(
        "fetch-schwab",
        help="Fetch Schwab/thinkorswim history via Trader API",
    )
    schwab_fetch.add_argument(
        "--account",
        default="schwab-tos",
        help="Account label or hash (default: schwab-tos)",
    )
    schwab_fetch.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    schwab_fetch.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    schwab_fetch.add_argument(
        "--login",
        action="store_true",
        help="Run interactive OAuth login (one-time setup)",
    )

    accounts_cmd = sub.add_parser("accounts", help="Schwab account balances and holdings")
    accounts_sub = accounts_cmd.add_subparsers(dest="accounts_command", required=True)
    schwab_snap = accounts_sub.add_parser(
        "snapshot",
        help="Fetch Schwab cash, MTM equity (liquidationValue), and open positions",
    )
    schwab_snap.add_argument(
        "--label",
        action="append",
        dest="labels",
        help="Account label to include (repeatable; default: all mapped accounts)",
    )

    marks_cmd = sub.add_parser("marks", help="Preview live marks / MTM for open positions")
    marks_cmd.add_argument(
        "--silo",
        choices=["stock_options", "futures", "all"],
        default="all",
    )

    sub.add_parser("status", help="Show store summary")

    sz = sub.add_parser("size", help="Recommend position size for a candidate trade (M3)")
    sz.add_argument("--silo", required=True, choices=["stock_options", "futures"])
    sz.add_argument("--underlying", required=True, help="Ticker symbol")
    sz.add_argument("--entry", type=float, required=True, help="Entry price")
    sz.add_argument("--stop", type=float, help="Stop price (stock/futures)")
    sz.add_argument("--premium", type=float, help="Option premium per contract")
    sz.add_argument("--spot", type=float, help="Underlying spot (defaults to entry)")
    sz.add_argument("--delta", type=float, help="Option delta at entry")
    sz.add_argument("--target", type=float, action="append", help="Price target (repeatable)")
    sz.add_argument("--atr", type=float, help="Instrument ATR for vol normalization")
    sz.add_argument(
        "--asset",
        choices=["stock", "option", "future"],
        default="stock",
    )
    sz.add_argument("--equity", type=float, default=100_000.0, help="Current silo equity (MTM)")
    sz.add_argument("--open-risk", type=float, default=0.0, help="Open dollar risk in silo")
    sz.add_argument("--open-notional", type=float, default=0.0, help="Open delta-notional in silo")
    sz.add_argument("--drawdown", type=float, default=0.0, help="Current drawdown fraction (0-1)")

    ev = sub.add_parser("evaluate", help="Run alpha-left-on-the-table evaluation (M2)")
    ev.add_argument(
        "--stock-equity",
        type=float,
        default=100_000.0,
        help="Starting equity for stock/options silo (default: 100000)",
    )
    ev.add_argument(
        "--futures-equity",
        type=float,
        default=50_000.0,
        help="Starting equity for futures silo (default: 50000)",
    )

    risk = sub.add_parser("risk", help="Edge & risk-appetite review (M4)")
    risk.add_argument(
        "--drawdown",
        type=float,
        default=0.0,
        help="Current drawdown fraction (0-1) for governor cap",
    )
    risk.add_argument(
        "--base-f",
        type=float,
        default=None,
        help="Current base risk f (default: config)",
    )
    risk.add_argument(
        "--kelly",
        type=float,
        default=None,
        help="Current Kelly fraction (default: config)",
    )
    risk.add_argument(
        "--silo",
        choices=["stock_options", "futures", "all"],
        default="all",
        help="Silo to review (default: all)",
    )

    opt = sub.add_parser("options", help="Rank options contracts for a thesis (M5)")
    opt.add_argument("--chain", type=Path, help="Option chain CSV export")
    opt.add_argument("--source", choices=["csv", "schwab"], default="csv")
    opt.add_argument("--underlying", required=True, help="Underlying ticker")
    opt.add_argument(
        "--spot", type=float, help="Current spot price (optional with --source schwab)"
    )
    opt.add_argument("--target", type=float, required=True, help="Price target")
    opt.add_argument("--hold-days", type=int, required=True, help="Expected hold (days)")
    opt.add_argument(
        "--direction",
        choices=["long_call", "long_put"],
        required=True,
        help="Long call or long put",
    )
    opt.add_argument("--path", choices=["fast", "gradual"], default="gradual")
    opt.add_argument("--equity", type=float, default=100_000.0, help="Silo equity (MTM)")
    opt.add_argument("--open-risk", type=float, default=0.0)
    opt.add_argument("--open-notional", type=float, default=0.0)
    opt.add_argument("--iv-rank", type=float, help="Optional IV rank 0-1 for vega scoring")
    opt.add_argument("--top", type=int, default=5, help="Number of ranked contracts to show")
    opt.add_argument("--save-snapshot", action="store_true", help="Persist chain snapshot to DB")

    args = parser.parse_args()

    if args.command == "import":
        result = import_and_assemble(args.csv, args.broker, account=args.account)
        repo = create_repository()
        print(
            f"Imported {result.imported} events "
            f"({result.skipped_duplicates} duplicates skipped, "
            f"{result.review_queue} review-queue items). "
            f"Positions: {repo.position_count()}"
        )
    elif args.command == "fetch-robinhood":
        from trading_architect.bootstrap import import_robinhood_fetch

        result = import_robinhood_fetch(
            account=args.account,
            save_csv=True,
        )
        repo = create_repository()
        print(
            f"Fetched and imported {result.imported} events for {args.account} "
            f"({result.skipped_duplicates} duplicates skipped). "
            f"Positions: {repo.position_count()}"
        )
        print("Audit CSV saved under data/raw/")
    elif args.command == "fetch-robinhood-portfolio":
        from trading_architect.ingestion.robinhood_fetch import (
            fetch_portfolio_snapshot,
            format_holding_line,
        )

        snapshot = fetch_portfolio_snapshot(accounts=args.accounts)
        print(f"Robinhood portfolio as of {snapshot.fetched_at.isoformat()}")
        print()
        print("Account balances")
        for bal in snapshot.accounts:
            print(
                f"  {bal.account}: "
                f"cash ${bal.cash:,.2f} + equivalents ${bal.cash_equivalents:,.2f} "
                f"= ${bal.cash_and_equivalents:,.2f} | "
                f"portfolio equity ${bal.portfolio_equity:,.2f}"
            )
        print(
            f"\nTotal cash & equivalents: ${snapshot.total_cash_and_equivalents:,.2f} | "
            f"total equity: ${snapshot.total_portfolio_equity:,.2f}"
        )
        if snapshot.holdings:
            print("\nHoldings (sum of parts)")
            for h in snapshot.holdings:
                print(format_holding_line(h))
        else:
            print("\nNo open holdings.")
    elif args.command == "accounts" and args.accounts_command == "snapshot":
        from trading_architect.bootstrap import fetch_schwab_portfolio_snapshot
        from trading_architect.ingestion.robinhood_fetch import format_holding_line
        from trading_architect.ingestion.schwab_auth import SchwabAuthExpired, get_client

        repo = create_repository()
        try:
            snapshot = fetch_schwab_portfolio_snapshot(
                labels=args.labels,
                repo=repo,
                client=get_client(),
            )
        except SchwabAuthExpired as exc:
            print(exc)
            return

        print(f"Schwab portfolio as of {snapshot.fetched_at.isoformat()}")
        print()
        print("Account balances (MTM equity = liquidationValue)")
        for bal in snapshot.accounts:
            print(
                f"  {bal.account}: "
                f"cash ${bal.cash:,.2f} + equivalents ${bal.cash_equivalents:,.2f} "
                f"= ${bal.cash_and_equivalents:,.2f} | "
                f"portfolio equity ${bal.portfolio_equity:,.2f} | "
                f"buying power ${bal.buying_power:,.2f}"
            )
        print(
            f"\nTotal cash & equivalents: ${snapshot.total_cash_and_equivalents:,.2f} | "
            f"total MTM equity: ${snapshot.total_portfolio_equity:,.2f}"
        )
        print("(Live equity cached for drawdown governor — refresh within 30 minutes.)")
        if snapshot.holdings:
            print("\nHoldings (sum of parts)")
            for h in snapshot.holdings:
                print(format_holding_line(h))
        else:
            print("\nNo open holdings.")
    elif args.command == "fetch-schwab":
        from datetime import date as date_cls

        from trading_architect.bootstrap import import_schwab_fetch
        from trading_architect.ingestion.schwab_auth import (
            SchwabAuthExpired,
            get_client,
            schwab_py_available,
        )

        if args.login:
            if not schwab_py_available():
                print('Install schwab-py first: pip install -e ".[schwab]"')
                return
            try:
                get_client(interactive=True)
            except SchwabAuthExpired as exc:
                print(exc)
                return
            print("Schwab OAuth login complete. Token saved.")
            return

        start = date_cls.fromisoformat(args.start) if args.start else None
        end = date_cls.fromisoformat(args.end) if args.end else None
        result = import_schwab_fetch(account=args.account, start=start, end=end)
        repo = create_repository()
        print(
            f"Fetched and imported {result.imported} events for {args.account} "
            f"({result.skipped_duplicates} duplicates skipped, "
            f"{result.review_queue} review-queue items). "
            f"Positions: {repo.position_count()}"
        )
    elif args.command == "marks":
        from trading_architect.engines.marks import default_marks_provider
        from trading_architect.models.entities import PositionStatus, Silo

        repo = create_repository()
        positions = [p for p in repo.list_positions() if p.status == PositionStatus.OPEN]
        if args.silo != "all":
            positions = [p for p in positions if p.silo == Silo(args.silo)]
        if not positions:
            print("No open positions.")
            return

        provider = default_marks_provider()
        symbols: set[str] = set()
        for p in positions:
            symbols.add(p.underlying)
            symbols.update(p.leg_net_qty.keys())
        marks = provider.marks_for(list(symbols))
        if not marks:
            print("No marks available (Schwab not configured or fetch failed).")
            return
        from trading_architect.engines.equity import compute_mark_fallback_stats

        marks_by_price = {sym: m.price for sym, m in marks.items()}
        fb = compute_mark_fallback_stats(positions, marks_by_price, marks)
        if fb.legs_at_cost_basis or fb.delta_iv_fallbacks:
            print(
                f"Mark fallbacks: {fb.legs_at_cost_basis} leg(s) at cost basis, "
                f"{fb.delta_iv_fallbacks} delta IV default(s)"
            )
            if fb.missing_mark_symbols:
                print(f"  Missing marks: {', '.join(fb.missing_mark_symbols)}")
        for sym, mark in sorted(marks.items()):
            delta_str = f" δ={mark.delta:.3f}" if mark.delta is not None else ""
            print(f"{sym:30s} ${mark.price:>10.2f}{delta_str}  @ {mark.asof.isoformat()}")
    elif args.command == "status":
        repo = create_repository()
        print(f"Events: {repo.event_count()}")
        print(f"Positions: {repo.position_count()}")
        review = repo.list_review_queue()
        if review:
            print(f"Review queue: {len(review)} items")
    elif args.command == "size":
        from trading_architect.engines.sizing import (
            CandidateTrade,
            SiloExposure,
            format_size_recommendation,
            recommend_size,
        )
        from trading_architect.models.entities import AssetType, Direction, Silo

        silo = Silo(args.silo)
        asset = AssetType(args.asset)
        if asset == AssetType.OPTION and not args.premium:
            print("Options require --premium.")
            return
        if asset != AssetType.OPTION and not args.stop:
            print("Stock/futures require --stop.")
            return

        repo = create_repository()
        candidate = CandidateTrade(
            silo=silo,
            underlying=args.underlying.upper(),
            direction=Direction.LONG,
            asset_type=asset,
            entry_price=args.entry,
            stop_price=args.stop,
            premium_per_contract=args.premium,
            spot_price=args.spot or args.entry,
            option_delta=args.delta,
            target_prices=tuple(args.target) if args.target else None,
            atr=args.atr,
            symbol=args.underlying.upper(),
        )
        exposure = SiloExposure(
            silo_equity=args.equity,
            open_dollar_risk=args.open_risk,
            open_delta_notional=args.open_notional,
            drawdown_pct=args.drawdown,
        )
        rec = recommend_size(candidate, exposure, events=repo.list_events())
        print(format_size_recommendation(rec))
    elif args.command == "evaluate":
        from trading_architect.engines.evaluation import evaluate_alpha_left, format_report_summary
        from trading_architect.models.entities import Silo

        repo = create_repository()
        events = repo.list_events()
        if not events:
            print("No events in store. Import broker CSVs first.")
            return
        report = evaluate_alpha_left(
            events,
            starting_equity={
                Silo.STOCK_OPTIONS: args.stock_equity,
                Silo.FUTURES: args.futures_equity,
            },
        )
        print(format_report_summary(report))
    elif args.command == "risk":
        from trading_architect.config.adaptive_risk import AdaptiveRiskConfig
        from trading_architect.config.sizing import DEFAULT_SIZING_CONFIG, SizingConfig
        from trading_architect.engines.adaptive_risk import (
            build_risk_review,
            format_risk_recommendation,
            format_risk_review,
            recommend_risk_appetite,
        )
        from trading_architect.models.entities import Silo

        repo = create_repository()
        events = repo.list_events()
        if not events:
            print("No events in store. Import broker CSVs first.")
            return

        cfg = AdaptiveRiskConfig()
        sizing = SizingConfig(
            base_risk_f=args.base_f
            if args.base_f is not None
            else DEFAULT_SIZING_CONFIG.base_risk_f,
            kelly_fraction=args.kelly
            if args.kelly is not None
            else DEFAULT_SIZING_CONFIG.kelly_fraction,
        )

        if args.silo == "all":
            report = build_risk_review(
                events, config=cfg, sizing=sizing, drawdown_pct=args.drawdown
            )
            print(format_risk_review(report))
        else:
            rec = recommend_risk_appetite(
                events,
                Silo(args.silo),
                config=cfg,
                sizing=sizing,
                drawdown_pct=args.drawdown,
            )
            print(format_risk_recommendation(rec))
    elif args.command == "options":
        from trading_architect.engines.options_selector import (
            OptionDirection,
            OptionSelectorInput,
            format_option_selector_result,
            rank_contracts,
        )
        from trading_architect.engines.sizing import SiloExposure
        from trading_architect.ingestion.chain import parse_chain_csv

        if args.source == "schwab":
            from trading_architect.ingestion.schwab_rest import fetch_chain_snapshot

            snapshot = fetch_chain_snapshot(args.underlying.upper())
            if args.spot:
                snapshot = snapshot.model_copy(update={"spot_price": args.spot})
        else:
            if not args.chain:
                print("CSV source requires --chain <path>.")
                return
            if not args.spot:
                print("--spot is required for CSV chain source.")
                return
            snapshot = parse_chain_csv(
                args.chain,
                args.underlying.upper(),
                args.spot,
                iv_rank=args.iv_rank,
            )
        repo = create_repository()
        if args.save_snapshot:
            repo.save_chain_snapshot(snapshot)

        result = rank_contracts(
            snapshot,
            OptionSelectorInput(
                underlying=args.underlying.upper(),
                direction=OptionDirection(args.direction),
                target_price=args.target,
                expected_hold_days=args.hold_days,
                path=args.path,
            ),
            SiloExposure(
                silo_equity=args.equity,
                open_dollar_risk=args.open_risk,
                open_delta_notional=args.open_notional,
            ),
            events=repo.list_events(),
            top_n=args.top,
        )
        print(format_option_selector_result(result))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
