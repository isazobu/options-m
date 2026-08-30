"""``options-m`` command line — a second, independent piece of evidence that
this service actually drives the Alpaca Trading API and MCP server, alongside
the running service itself.

Cheap by design: every subcommand is a thin wrapper over the modules that
already hold the logic. ``plan`` in particular never calls
``place_option_order`` — there is no call site for it anywhere on that path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pprint
import sys
from typing import Any

from options_m import strategy_builder
from options_m.agents.execution import (
    ExecutionAgent,
    build_portfolio_snapshot,
    fetch_chain_window,
)
from options_m.api import jsonable
from options_m.config import Settings
from options_m.db import Database
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.migrate import apply as apply_migrations
from options_m.models import Rejection, StrategyIntent
from options_m.risk import RiskEngine, RiskLimits
from options_m.store import Store

_STRATEGIES = (
    "long_call",
    "long_put",
    "debit_call_spread",
    "debit_put_spread",
    "covered_call",
    "cash_secured_put",
    "long_strangle",
    "put_credit_spread",
    "call_credit_spread",
    "iron_condor",
    "iron_butterfly",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="options-m")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="market clock and account info")
    sub.add_parser("positions", help="all current positions")

    chain_p = sub.add_parser("chain", help="raw option chain for a symbol")
    chain_p.add_argument("--symbol", required=True)

    plan_p = sub.add_parser("plan", help="build and risk-check a plan; never submits")
    plan_p.add_argument("--symbol", required=True)
    plan_p.add_argument("--strategy", required=True, choices=_STRATEGIES)
    plan_p.add_argument("--delta", type=float, required=True)
    plan_p.add_argument("--dte", type=int, required=True)
    plan_p.add_argument(
        "--dte-band",
        type=int,
        default=7,
        help="dte_min/dte_max = dte -/+ this many days (floored at 0)",
    )
    plan_p.add_argument("--spread-width", type=float, default=None)

    trade_p = sub.add_parser("trade", help="run one ExecutionAgent iteration")
    trade_p.add_argument("--once", action="store_true", required=True)

    return parser


async def _run_plan(
    args: argparse.Namespace, *, mcp: AlpacaMcp, store: Store, settings: Settings
) -> dict[str, Any]:
    intent = StrategyIntent(
        action="open",
        strategy=args.strategy,
        underlying=args.symbol.upper(),
        target_delta=args.delta,
        spread_width=args.spread_width,
        dte_min=max(0, args.dte - args.dte_band),
        dte_max=args.dte + args.dte_band,
        conviction=1.0,
        thesis="cli plan",
        invalidation="cli plan",
    )
    account = await mcp.get_account_info()
    snapshot = await mcp.get_stock_snapshot(intent.underlying)
    trade = snapshot.get("latestTrade") if isinstance(snapshot, dict) else None
    spot = finite_float(trade.get("p")) if isinstance(trade, dict) else None
    if spot is None:
        return {"rejection": {"reason": "no_spot_price"}}

    contracts, snapshots = await fetch_chain_window(mcp, intent, spot=spot)
    existing_position = await mcp.get_open_position(intent.underlying)

    result = await strategy_builder.build(
        intent,
        contracts=contracts,
        snapshots=snapshots,
        account=account,
        existing_position=existing_position,
        settings=settings,
        proposal_id=0,
        spot=spot,
    )
    if isinstance(result, Rejection):
        return {"rejection": result.model_dump()}

    portfolio = await build_portfolio_snapshot(
        intent.underlying, result.client_order_id, account, mcp=mcp, store=store, settings=settings
    )
    verdict = RiskEngine(RiskLimits.from_settings(settings)).evaluate(result, portfolio)
    return {"plan": result.model_dump(), "verdict": verdict.model_dump()}


async def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    async with Database(settings) as db, AlpacaMcp(settings) as mcp:
        await apply_migrations(db)
        store = Store(db)

        if args.command == "status":
            return {"clock": await mcp.get_clock(), "account": await mcp.get_account_info()}
        if args.command == "positions":
            return {"positions": await mcp.get_all_positions()}
        if args.command == "chain":
            return {"chain": await mcp.get_option_chain(args.symbol.upper())}
        if args.command == "plan":
            return await _run_plan(args, mcp=mcp, store=store, settings=settings)
        if args.command == "trade":
            risk_engine = RiskEngine(RiskLimits.from_settings(settings))
            agent = ExecutionAgent(settings, mcp, store, risk_engine)
            await agent.step()
            return {"ran": True}
        # pragma: no cover - argparse's `required=True` subparsers make this unreachable
        raise ValueError(f"unknown command {args.command!r}")


def _print(result: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(jsonable(result), indent=2))
    else:
        pprint.pprint(result)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(_dispatch(args))
    except Exception as exc:  # a CLI reports failure to the operator, it does not stack-trace it
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print(result, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
