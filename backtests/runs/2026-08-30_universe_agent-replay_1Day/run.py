"""Replay the options-m autonomous agent over 24-28 August 2026.

Runs the *real* pipeline — ``EvidenceCollector``, ``matrix.decide``,
``strategy_builder.build``, ``RiskEngine.evaluate`` — against historical Alpaca
data served through ``backtests.asof.AsOfMcp``. Nothing in the decision path is
reimplemented here; this file only supplies the clock, the data, the stubbed
regime read, and the fill/mark accounting the live system does not yet have.

Timing, kept deliberately separate:
  signal  -> day D's close (evidence sees bars and option closes up to D only)
  fill    -> day D+1's option bar OPEN  (the skill's `next_open` model)
  mark    -> Friday 28 August close, mid price, no exit commission modelled

Consequences of that choice: a signal on Friday has no fill day, so entries
happen Tue-Fri from Mon-Thu signals.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from backtests.asof import AsOfMcp, StubStore  # noqa: E402
from backtests.clock import frozen_at  # noqa: E402

from options_m import matrix, strategy_builder  # noqa: E402
from options_m.agents.execution import fetch_chain_window  # noqa: E402
from options_m.config import Settings  # noqa: E402
from options_m.evidence.evidence import EvidenceCollector  # noqa: E402
from options_m.models import OrderPlan, RegimeRead, Rejection  # noqa: E402
from options_m.risk import PortfolioSnapshot, RiskEngine, RiskLimits  # noqa: E402

logging.disable(logging.WARNING)

WEEK = [
    date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
    date(2026, 8, 27), date(2026, 8, 28),
]
MARK_DATE = WEEK[-1]
UNIVERSE = ["SPY", "QQQ", "IWM", "AAPL", "MSFT",
            "NVDA", "AMD", "TSLA", "META", "GOOGL"]
STARTING_EQUITY = 100_000.0
CONTRACT_MULTIPLIER = 100.0


@dataclass
class OpenPosition:
    symbol: str
    strategy: str
    signal_date: date
    fill_date: date
    qty: int
    legs: list[dict[str, Any]]
    entry_value: float          # per contract, signed: >0 debit paid, <0 credit received
    max_loss: float
    thesis_conviction: float
    mark_value: float | None = None
    pnl: float | None = None
    mark_note: str = ""


@dataclass
class DayLog:
    day: date
    decisions: list[dict[str, Any]] = field(default_factory=list)


def leg_sign(side: str) -> int:
    """+1 for a leg we are long, -1 for a leg we are short."""
    return 1 if side == "buy" else -1


class OptionPrices:
    """Daily option OHLC lookup, keyed by OCC symbol and date."""

    def __init__(self, raw_dir: Path) -> None:
        bars = json.loads((raw_dir / "option_bars.json").read_text())
        self._by_symbol_date: dict[str, dict[str, dict[str, Any]]] = {}
        for occ, rows in bars.items():
            self._by_symbol_date[occ] = {row["t"][:10]: row for row in rows}

    def bar(self, occ: str, day: date) -> dict[str, Any] | None:
        return self._by_symbol_date.get(occ, {}).get(day.isoformat())


def stub_regime(symbol: str, conviction: float) -> RegimeRead:
    """A fixed regime read, standing in for the Featherless call.

    The matrix uses the LLM for exactly one thing — the conviction floor veto —
    and re-derives trend and IV regime itself from the evidence pack. Holding
    conviction constant therefore isolates the deterministic half of the system
    (evidence -> matrix -> builder -> risk) and makes the run reproducible. It
    also means these results are an *upper bound* on trade count: a real LLM
    would veto some of these on conviction.
    """
    return RegimeRead(
        thesis=f"Replay stub for {symbol}: matrix-driven, conviction held constant.",
        invalidation="Not evaluated in replay — the LLM leg is stubbed.",
        conviction=conviction,
    )


async def run(
    spread_pct: float,
    conviction: float,
    *,
    overrides: dict[str, Any] | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """``overrides`` patches ``Settings`` fields (e.g. ``max_concurrent_positions``).

    The override goes through ``Settings`` itself rather than through
    ``RiskLimits``, so a swept limit reaches the builder's sizing as well as the
    risk gate — exactly as changing ``.env`` would.
    """
    settings = Settings()
    if overrides:
        settings = settings.model_copy(update=overrides)
    prices = OptionPrices(HERE / "raw")
    engine = RiskEngine(RiskLimits.from_settings(settings))

    open_positions: list[OpenPosition] = []
    day_logs: list[DayLog] = []
    proposal_id = 0

    for index, day in enumerate(WEEK):
        log = DayLog(day=day)
        fill_day = WEEK[index + 1] if index + 1 < len(WEEK) else None

        for symbol in UNIVERSE:
            proposal_id += 1
            record: dict[str, Any] = {"symbol": symbol, "proposal_id": proposal_id}

            with frozen_at(day):
                mcp = AsOfMcp(HERE / "raw", as_of=day, spread_pct=spread_pct)
                try:
                    pack = await EvidenceCollector(settings, mcp, StubStore()).collect(symbol)
                except Exception as exc:
                    record |= {"stage": "evidence", "outcome": f"error: {exc}"}
                    log.decisions.append(record)
                    continue

                options_block = pack.get("options")
                if isinstance(options_block, dict):
                    iv = options_block.get("iv_atm")
                    rv = options_block.get("realised_vol_20d")
                    record["iv_atm"] = iv
                    record["rv_20d"] = rv
                    usable = isinstance(iv, float) and isinstance(rv, float) and rv
                    record["iv_rv"] = round(iv / rv, 3) if usable else None
                    record["atm_dte"] = options_block.get("atm_dte")

                intent = matrix.decide(
                    pack, stub_regime(symbol, conviction), settings=settings, as_of=day
                )
                if intent == "hold":
                    record |= {"stage": "matrix", "outcome": "hold"}
                    log.decisions.append(record)
                    continue
                record["strategy"] = intent.strategy

                snapshot = await mcp.get_stock_snapshot(symbol)
                spot = float(snapshot["latestTrade"]["p"])
                record["spot"] = spot

                contracts, snapshots = await fetch_chain_window(mcp, intent, spot=spot)
                record["chain_contracts"] = len(contracts)

                plan_or_rejection = await strategy_builder.build(
                    intent,
                    contracts=contracts,
                    snapshots=snapshots,
                    account={"equity": STARTING_EQUITY, "options_trading_level": 3},
                    existing_position=None,
                    settings=settings,
                    proposal_id=proposal_id,
                    spot=spot,
                )

            if isinstance(plan_or_rejection, Rejection):
                record |= {
                    "stage": "builder",
                    "outcome": f"rejected: {plan_or_rejection.reason}",
                    "detail": plan_or_rejection.detail,
                }
                log.decisions.append(record)
                continue

            plan: OrderPlan = plan_or_rejection
            record |= {
                "limit_price": plan.limit_price,
                "max_loss": plan.max_loss,
                "qty": plan.qty,
                "legs": [f"{leg.side} {leg.symbol}" for leg in plan.legs],
            }

            portfolio = PortfolioSnapshot(
                equity=STARTING_EQUITY,
                start_of_day_equity=STARTING_EQUITY,
                high_water_mark=STARTING_EQUITY,
                concurrent_option_positions=len(open_positions),
                positions_in_underlying=sum(1 for p in open_positions if p.symbol == symbol),
                # Matches production's ``build_portfolio_snapshot``: the sum of
                # each *leg's* absolute market value, not the net value of the
                # structure. For a credit spread the two differ by a lot — net
                # is credit received, per-leg is the gross of both sides — and
                # this is the figure ``_check_total_premium`` is written
                # against. Entry prices stand in for market value; the live
                # service re-reads it from the broker every iteration.
                total_open_option_premium=sum(
                    abs(leg["entry_price"]) * leg["ratio"] * p.qty * CONTRACT_MULTIPLIER
                    for p in open_positions
                    for leg in p.legs
                ),
                market_is_open=True,
                minutes_to_close=120.0,
                kill_switch_engaged=False,
                already_submitted=False,
            )
            with frozen_at(day):
                verdict = engine.evaluate(plan, portfolio)

            if not verdict.approved:
                record |= {"stage": "risk", "outcome": f"rejected: {','.join(verdict.reasons)}"}
                log.decisions.append(record)
                continue

            qty = verdict.adjusted_qty or plan.qty
            if fill_day is None:
                record |= {"stage": "fill", "outcome": "approved but no fill day (last session)"}
                log.decisions.append(record)
                continue

            # Fill each leg at the next session's OPEN, crossing the modelled
            # spread in the direction that costs us: buy at ask, sell at bid.
            entry_value = 0.0
            legs_out: list[dict[str, Any]] = []
            missing = None
            for leg in plan.legs:
                bar = prices.bar(leg.symbol, fill_day)
                if bar is None:
                    missing = leg.symbol
                    break
                open_mid = float(bar["o"])
                half = open_mid * spread_pct / 2
                price = open_mid + half if leg.side == "buy" else open_mid - half
                price = max(price, 0.01)
                entry_value += leg_sign(leg.side) * price * leg.ratio
                legs_out.append({
                    "symbol": leg.symbol, "side": leg.side, "ratio": leg.ratio,
                    "strike": leg.strike, "expiry": leg.expiry.isoformat(),
                    "option_type": leg.option_type, "entry_price": round(price, 4),
                    "delta": leg.delta,
                })
            if missing:
                record |= {"stage": "fill",
                           "outcome": f"no {fill_day} bar for {missing} — unfilled"}
                log.decisions.append(record)
                continue

            open_positions.append(OpenPosition(
                symbol=symbol, strategy=plan.strategy, signal_date=day, fill_date=fill_day,
                qty=qty, legs=legs_out, entry_value=round(entry_value, 4),
                max_loss=plan.max_loss, thesis_conviction=conviction,
            ))
            record |= {"stage": "filled", "outcome": "FILLED", "fill_date": fill_day.isoformat(),
                       "entry_value": round(entry_value, 4)}
            log.decisions.append(record)

        day_logs.append(log)

    # Mark every open position at Friday's close.
    for position in open_positions:
        mark_value, missing = 0.0, None
        for leg in position.legs:
            bar = prices.bar(leg["symbol"], MARK_DATE)
            if bar is None:
                missing = leg["symbol"]
                break
            mark_value += leg_sign(leg["side"]) * float(bar["c"]) * leg["ratio"]
        if missing:
            position.mark_note = f"no {MARK_DATE} bar for {missing} — held at entry"
            position.mark_value = position.entry_value
            position.pnl = 0.0
            continue
        position.mark_value = round(mark_value, 4)
        move = mark_value - position.entry_value
        position.pnl = round(move * position.qty * CONTRACT_MULTIPLIER, 2)

    return {"day_logs": day_logs, "positions": open_positions, "settings": {
        "spread_pct": spread_pct, "conviction": conviction, "starting_equity": STARTING_EQUITY}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spread-pct", type=float, default=0.02)
    parser.add_argument("--conviction", type=float, default=0.70)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(run(args.spread_pct, args.conviction))
    positions = result["positions"]

    if args.json:
        print(json.dumps({
            "config": result["settings"],
            "days": [
                {"date": log.day.isoformat(), "decisions": log.decisions}
                for log in result["day_logs"]
            ],
            "positions": [
                vars(p) | {"signal_date": p.signal_date.isoformat(),
                           "fill_date": p.fill_date.isoformat()}
                for p in positions
            ],
        }, indent=2, default=str))
        return

    header = (f"REPLAY 24-28 Aug 2026 | spread_pct={args.spread_pct:.1%} "
              f"conviction={args.conviction}")
    rule = "=" * 88
    print(f"\n{rule}\n{header}\n{rule}")
    for log in result["day_logs"]:
        outcomes: dict[str, int] = {}
        for decision in log.decisions:
            key = decision.get("outcome", "?").split(":")[0].strip()
            outcomes[key] = outcomes.get(key, 0) + 1
        print(f"\n{log.day}  " + "  ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
        for decision in log.decisions:
            if decision.get("outcome") not in (None, "hold"):
                ivrv = decision.get("iv_rv")
                print(f"    {decision['symbol']:6s} IV/RV={ivrv if ivrv else '  -  '}"
                      f"  {decision.get('strategy','-'):20s} {decision.get('outcome')}")

    print(f"\n{'='*88}\nPOSITIONS\n{'='*88}")
    if not positions:
        print("  none")
    total = 0.0
    for position in positions:
        total += position.pnl or 0.0
        print(f"  {position.symbol:6s} {position.strategy:20s} signal {position.signal_date} "
              f"fill {position.fill_date} qty {position.qty:2d}  "
              f"entry {position.entry_value:+8.2f} mark {position.mark_value:+8.2f}  "
              f"P&L {position.pnl:+9.2f}  maxloss {position.max_loss:8.2f} {position.mark_note}")
    print(f"\n  TOTAL P&L: {total:+.2f} on {STARTING_EQUITY:,.0f} = {total/STARTING_EQUITY:+.3%}")


if __name__ == "__main__":
    main()
