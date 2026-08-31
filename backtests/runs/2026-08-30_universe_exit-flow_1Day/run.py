"""Replay the options-m agent over 24-28 August 2026 *with the exit flow*.

Same replay as ``2026-08-30_universe_agent-replay_1Day`` — the real
``EvidenceCollector`` / ``matrix.decide`` / ``strategy_builder.build`` /
``RiskEngine.evaluate`` path against cached Alpaca data — plus the two things
that run did not have:

  1. **Exits.** PR #16 gave the system a close path: ``PositionManagerAgent``
     marks each position and pre-computes ``pnl_pct``; ``StrategistAgent``
     reads that cache and writes a ``close`` proposal when
     ``_close_reason`` fires (profit target / stop loss / time stop). This
     harness calls those two shipped functions directly rather than restating
     the thresholds, so the exit rule under test is the shipped one.
  2. **A sweepable position limit**, via ``Settings`` overrides.

Timing, kept deliberately separate:
  entry signal -> day D close     entry fill -> day D+1 OPEN
  close signal -> day D close     exit  fill -> day D+1 OPEN
  final mark   -> 28 August close, mid, for whatever is still open

A position with a close signalled on day D still occupies its slot until the
exit fills on D+1, which is what the broker would report and therefore what
``concurrent_option_positions`` counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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
# The exit rule under test. Imported, not restated: the thresholds live in
# Settings and the precedence between them lives in _close_reason.
from options_m.agents.position_manager import _compute_pnl_pct  # noqa: E402
from options_m.agents.strategist import _close_reason  # noqa: E402
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
class Position:
    symbol: str
    strategy: str
    signal_date: date
    fill_date: date
    qty: int
    legs: list[dict[str, Any]]
    entry_value: float          # per contract, signed: >0 debit paid, <0 credit received
    max_loss: float
    thesis_conviction: float
    # Exit state
    close_signal_date: date | None = None
    close_reason: str | None = None
    exit_date: date | None = None
    exit_value: float | None = None
    mark_value: float | None = None
    pnl: float | None = None
    mark_note: str = ""

    @property
    def closed(self) -> bool:
        return self.exit_date is not None


@dataclass
class DayLog:
    day: date
    decisions: list[dict[str, Any]] = field(default_factory=list)
    exits: list[dict[str, Any]] = field(default_factory=list)
    close_signals: list[dict[str, Any]] = field(default_factory=list)


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

    Note this stub only covers the *entry* leg. The close path does not consult
    the LLM at all — ``_close_reason`` is pure arithmetic over the position
    cache — so exits in this replay are the real decision, not a stand-in.
    """
    return RegimeRead(
        thesis=f"Replay stub for {symbol}: matrix-driven, conviction held constant.",
        invalidation="Not evaluated in replay — the LLM leg is stubbed.",
        conviction=conviction,
    )


def mid_value(position: Position, prices: OptionPrices, day: date) -> float | None:
    """Signed mid value of the position, per contract, on ``day``."""
    total = 0.0
    for leg in position.legs:
        bar = prices.bar(leg["symbol"], day)
        if bar is None:
            return None
        total += leg_sign(leg["side"]) * float(bar["c"]) * leg["ratio"]
    return total


def position_payload(position: Position, mark: float) -> dict[str, Any]:
    """The cache row ``PositionManagerAgent`` would have written for this mark.

    ``market_value`` and ``unrealized_pl`` are the two fields the shipped
    ``_compute_pnl_pct`` divides, and ``opened_at`` is what the time stop dates
    from — so this is the whole of the input surface the exit rule reads.
    """
    scale = position.qty * CONTRACT_MULTIPLIER
    market_value = mark * scale
    unrealized_pl = (mark - position.entry_value) * scale
    payload: dict[str, Any] = {
        "market_value": market_value,
        "unrealized_pl": unrealized_pl,
        "opened_at": datetime.combine(position.fill_date, datetime.min.time(), tzinfo=UTC),
        "strategy": position.strategy,
    }
    payload["pnl_pct"] = _compute_pnl_pct(payload)
    return payload


def exit_fill_value(position: Position, prices: OptionPrices, day: date,
                    spread_pct: float) -> tuple[float | None, str | None]:
    """Value realised closing at ``day``'s OPEN, crossing the spread against us.

    A leg we are long is sold at the bid; a leg we are short is bought back at
    the ask. Mirror image of the entry fill, and the half-spread the original
    run never paid.
    """
    total = 0.0
    for leg in position.legs:
        bar = prices.bar(leg["symbol"], day)
        if bar is None:
            return None, leg["symbol"]
        open_mid = float(bar["o"])
        half = open_mid * spread_pct / 2
        # Closing a long leg = selling it -> hit the bid. Closing a short leg =
        # buying it back -> pay the ask.
        price = open_mid - half if leg["side"] == "buy" else open_mid + half
        price = max(price, 0.01)
        total += leg_sign(leg["side"]) * price * leg["ratio"]
    return total, None


async def run(
    spread_pct: float,
    conviction: float,
    *,
    overrides: dict[str, Any] | None = None,
    exits_enabled: bool = True,
) -> dict[str, Any]:
    settings = Settings()
    if overrides:
        settings = settings.model_copy(update=overrides)
    prices = OptionPrices(HERE / "raw")
    engine = RiskEngine(RiskLimits.from_settings(settings))

    positions: list[Position] = []
    day_logs: list[DayLog] = []
    proposal_id = 0

    for index, day in enumerate(WEEK):
        log = DayLog(day=day)
        fill_day = WEEK[index + 1] if index + 1 < len(WEEK) else None

        # --- 1. Exit fills at today's OPEN, for closes signalled yesterday ----
        for position in positions:
            if position.closed or position.close_signal_date is None:
                continue
            if position.close_signal_date >= day:
                continue
            value, missing = exit_fill_value(position, prices, day, spread_pct)
            if value is None:
                log.exits.append({"symbol": position.symbol,
                                  "outcome": f"no {day} bar for {missing} — still open"})
                continue
            position.exit_date = day
            position.exit_value = round(value, 4)
            position.pnl = round(
                (value - position.entry_value) * position.qty * CONTRACT_MULTIPLIER, 2)
            log.exits.append({"symbol": position.symbol, "reason": position.close_reason,
                              "exit_value": position.exit_value, "pnl": position.pnl})

        open_book = [p for p in positions if not p.closed]

        # --- 2. Entry decisions at today's CLOSE -----------------------------
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
                    usable = isinstance(iv, float) and isinstance(rv, float) and rv
                    record["iv_rv"] = round(iv / rv, 3) if usable else None

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
                contracts, snapshots = await fetch_chain_window(mcp, intent, spot=spot)

                plan_or_rejection = await strategy_builder.build(
                    intent,
                    contracts=contracts,
                    snapshots=snapshots,
                    account={
                        "equity": STARTING_EQUITY,
                        # Sizing refuses to guess at collateral, so the harness
                        # has to declare the account's options capacity. A cash
                        # account holding the starting equity is the shape the
                        # replay assumes elsewhere.
                        "cash": STARTING_EQUITY,
                        "options_buying_power": STARTING_EQUITY,
                        "options_trading_level": 3,
                    },
                    existing_position=None,
                    settings=settings,
                    proposal_id=proposal_id,
                    spot=spot,
                )

            if isinstance(plan_or_rejection, Rejection):
                record |= {"stage": "builder",
                           "outcome": f"rejected: {plan_or_rejection.reason}"}
                log.decisions.append(record)
                continue

            plan: OrderPlan = plan_or_rejection
            record |= {"limit_price": plan.limit_price, "max_loss": plan.max_loss,
                       "qty": plan.qty}

            portfolio = PortfolioSnapshot(
                equity=STARTING_EQUITY,
                # No margin modelling in the replay: the harness never closes a
                # position mid-run, so options buying power stays at the
                # starting equity rather than tracking collateral posted.
                options_buying_power=STARTING_EQUITY,
                # The replay does not model portfolio greeks: it never holds a
                # book long enough for aggregate delta/vega to matter, and the
                # evidence packs it replays carry no position side. Zero is a
                # measured "no exposure", which is what an unbuilt book is here.
                projected_beta_weighted_delta=0.0,
                projected_net_vega=0.0,
                start_of_day_equity=STARTING_EQUITY,
                high_water_mark=STARTING_EQUITY,
                concurrent_option_positions=len(open_book),
                positions_in_underlying=sum(1 for p in open_book if p.symbol == symbol),
                total_open_option_premium=sum(
                    abs(leg["entry_price"]) * leg["ratio"] * p.qty * CONTRACT_MULTIPLIER
                    for p in open_book
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
                })
            if missing:
                record |= {"stage": "fill",
                           "outcome": f"no {fill_day} bar for {missing} — unfilled"}
                log.decisions.append(record)
                continue

            position = Position(
                symbol=symbol, strategy=plan.strategy, signal_date=day, fill_date=fill_day,
                qty=qty, legs=legs_out, entry_value=round(entry_value, 4),
                max_loss=plan.max_loss, thesis_conviction=conviction,
            )
            positions.append(position)
            # A position filled at tomorrow's open already holds its slot for
            # tomorrow's decisions; the live gate counts working orders too.
            open_book.append(position)
            record |= {"stage": "filled", "outcome": "FILLED",
                       "fill_date": fill_day.isoformat(),
                       "entry_value": round(entry_value, 4)}
            log.decisions.append(record)

        # --- 3. Close decisions at today's CLOSE, over the marked book -------
        # StrategistAgent runs its close sweep from the PositionManager cache;
        # the mark is today's close, and the decision is the shipped one.
        if exits_enabled:
            for position in positions:
                if position.closed or position.close_signal_date is not None:
                    continue
                if position.fill_date > day:
                    continue          # not filled yet, nothing to mark
                mark = mid_value(position, prices, day)
                if mark is None:
                    continue
                payload = position_payload(position, mark)
                with frozen_at(day):
                    reason = _close_reason(payload, settings)
                if reason is None:
                    continue
                position.close_signal_date = day
                position.close_reason = reason
                log.close_signals.append({
                    "symbol": position.symbol, "reason": reason,
                    "pnl_pct": round(payload["pnl_pct"], 4) if payload["pnl_pct"] else None,
                })

        day_logs.append(log)

    # --- 4. Mark whatever is still open at Friday's close --------------------
    for position in positions:
        if position.closed:
            continue
        mark = mid_value(position, prices, MARK_DATE)
        if mark is None:
            position.mark_note = f"no {MARK_DATE} bar — held at entry"
            position.mark_value = position.entry_value
            position.pnl = 0.0
            continue
        position.mark_value = round(mark, 4)
        position.pnl = round(
            (mark - position.entry_value) * position.qty * CONTRACT_MULTIPLIER, 2)

    return {"day_logs": day_logs, "positions": positions, "settings": {
        "spread_pct": spread_pct, "conviction": conviction,
        "starting_equity": STARTING_EQUITY, "exits_enabled": exits_enabled,
        "max_concurrent_positions": settings.max_concurrent_positions,
        "exit_profit_target_pct": settings.exit_profit_target_pct,
        "exit_stop_loss_pct": settings.exit_stop_loss_pct,
        "exit_time_stop_days": settings.exit_time_stop_days,
    }}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spread-pct", type=float, default=0.02)
    parser.add_argument("--conviction", type=float, default=0.70)
    parser.add_argument("--max-concurrent", type=int, default=None)
    parser.add_argument("--no-exits", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    overrides = ({"max_concurrent_positions": args.max_concurrent}
                 if args.max_concurrent else None)
    result = asyncio.run(run(args.spread_pct, args.conviction, overrides=overrides,
                             exits_enabled=not args.no_exits))
    positions = result["positions"]

    if args.json:
        print(json.dumps({
            "config": result["settings"],
            "days": [{"date": log.day.isoformat(), "decisions": log.decisions,
                      "exits": log.exits, "close_signals": log.close_signals}
                     for log in result["day_logs"]],
            "positions": [vars(p) for p in positions],
        }, indent=2, default=str))
        return

    cfg = result["settings"]
    rule = "=" * 92
    print(f"\n{rule}\nEXIT-FLOW REPLAY 24-28 Aug 2026 | spread={args.spread_pct:.1%} "
          f"max_concurrent={cfg['max_concurrent_positions']} "
          f"exits={'on' if cfg['exits_enabled'] else 'OFF'} "
          f"(+{cfg['exit_profit_target_pct']:.0%} / -{cfg['exit_stop_loss_pct']:.0%} / "
          f"{cfg['exit_time_stop_days']}d)\n{rule}")

    for log in result["day_logs"]:
        outcomes: dict[str, int] = {}
        for decision in log.decisions:
            key = decision.get("outcome", "?").split(":")[0].strip()
            outcomes[key] = outcomes.get(key, 0) + 1
        print(f"\n{log.day}  " + "  ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
        for exit_row in log.exits:
            print(f"    EXIT FILL  {exit_row['symbol']:6s} {exit_row.get('reason','-'):14s}"
                  f" P&L {exit_row.get('pnl', 0):+9.2f}")
        for signal in log.close_signals:
            pnl_pct = signal.get("pnl_pct")
            shown = f"{pnl_pct:+.1%}" if pnl_pct is not None else "n/a"
            print(f"    CLOSE SIGNAL {signal['symbol']:6s} {signal['reason']:14s} {shown}")

    print(f"\n{'='*92}\nPOSITIONS\n{'='*92}")
    total = 0.0
    for position in positions:
        total += position.pnl or 0.0
        state = (f"CLOSED {position.exit_date} ({position.close_reason})"
                 if position.closed else "open, marked 28 Aug")
        print(f"  {position.symbol:6s} {position.strategy:20s} fill {position.fill_date} "
              f"qty {position.qty:2d} entry {position.entry_value:+8.2f} "
              f"P&L {(position.pnl or 0):+9.2f}  {state}")
    closed = [p for p in positions if p.closed]
    print(f"\n  {len(positions)} positions, {len(closed)} closed by the exit rule")
    print(f"  TOTAL P&L: {total:+.2f} on {STARTING_EQUITY:,.0f} = {total/STARTING_EQUITY:+.3%}")


if __name__ == "__main__":
    main()
