"""Turn a replay result into the run's artifacts: equity curve, trades, summary."""
from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import statistics
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("runmod", HERE / "run.py")
runmod = importlib.util.module_from_spec(spec)
sys.modules["runmod"] = runmod  # the dataclasses in run.py need the module resolvable
spec.loader.exec_module(runmod)

# 24 Aug is the pre-entry baseline: no position is filled until the 25th open,
# so anchoring the curve there is what makes total return equal realised P&L.
BASE_DAY = date(2026, 8, 24)
MARK_DAYS = [
    BASE_DAY, date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28),
]
EQUITY = runmod.STARTING_EQUITY
MULT = runmod.CONTRACT_MULTIPLIER


def position_value(position, prices, day):
    """Mid-price value of the position on ``day``; None if any leg has no bar."""
    total = 0.0
    for leg in position.legs:
        bar = prices.bar(leg["symbol"], day)
        if bar is None:
            return None
        total += runmod.leg_sign(leg["side"]) * float(bar["c"]) * leg["ratio"]
    return total


async def main() -> None:
    spread_pct, conviction = 0.02, 0.70
    result = await runmod.run(spread_pct, conviction)
    positions = result["positions"]
    prices = runmod.OptionPrices(HERE / "raw")

    # --- equity curve: cash unchanged, positions marked daily -----------------
    rows = []
    last_carried = {}
    for day in MARK_DAYS:
        open_pnl = 0.0
        for i, position in enumerate(positions):
            if day < position.fill_date:
                continue
            value = position_value(position, prices, day)
            if value is None:
                value = last_carried.get(i, position.entry_value)
            last_carried[i] = value
            open_pnl += (value - position.entry_value) * position.qty * MULT
        rows.append({"date": day.isoformat(), "equity": round(EQUITY + open_pnl, 2),
                     "open_pnl": round(open_pnl, 2)})

    # --- benchmark: SPY buy-and-hold, same entry and mark dates ---------------
    stock = json.loads((HERE / "raw" / "bars_universe.json").read_text())["bars"]["SPY"]
    by_date = {b["t"][:10]: b for b in stock}
    entry_px = float(by_date["2026-08-25"]["o"])
    # The benchmark is flat on the baseline day for the same reason the strategy is.
    bench = []
    for day in MARK_DAYS:
        if day == BASE_DAY:
            bench.append({"date": day.isoformat(), "equity": EQUITY})
            continue
        close = float(by_date[day.isoformat()]["c"])
        bench.append({"date": day.isoformat(),
                      "equity": round(EQUITY * close / entry_px, 2)})

    def stats(series):
        eq = [r["equity"] for r in series]
        total = eq[-1] / eq[0] - 1
        peak, mdd = eq[0], 0.0
        for value in eq:
            peak = max(peak, value)
            mdd = min(mdd, value / peak - 1)
        daily = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
        sd = statistics.stdev(daily) if len(daily) > 1 else 0.0
        # Reported for completeness only. Three daily observations cannot
        # support a Sharpe ratio; the report says so rather than dressing it up.
        sharpe = (statistics.mean(daily) / sd * (252 ** 0.5)) if sd else None
        return {"total_return": total, "max_drawdown": mdd, "final_equity": eq[-1],
                "daily_sharpe_annualised": sharpe, "n_daily_obs": len(daily)}

    strategy_stats, bench_stats = stats(rows), stats(bench)

    wins = [p for p in positions if (p.pnl or 0) > 0]
    gross_win = sum(p.pnl for p in positions if (p.pnl or 0) > 0)
    gross_loss = -sum(p.pnl for p in positions if (p.pnl or 0) < 0)

    reasons = {}
    for log in result["day_logs"]:
        for decision in log.decisions:
            outcome = decision.get("outcome", "")
            if outcome.startswith("rejected"):
                for why in outcome.split(":", 1)[1].split(","):
                    why = why.strip()
                    reasons[why] = reasons.get(why, 0) + 1
            elif outcome == "hold":
                reasons["matrix_hold"] = reasons.get("matrix_hold", 0) + 1

    summary = {
        "run": "2026-08-30_universe_agent-replay_1Day",
        "period": {"signal_days": [d.isoformat() for d in runmod.WEEK],
                   "fill_days": [d.isoformat() for d in MARK_DAYS[1:]],
                   "mark_date": MARK_DAYS[-1].isoformat()},
        "config": {"spread_pct": spread_pct, "conviction": conviction,
                   "starting_equity": EQUITY, "fill_model": "next_open",
                   "universe": runmod.UNIVERSE},
        "strategy": strategy_stats,
        "benchmark": {"name": "SPY buy-and-hold", **bench_stats},
        "trades": {"decisions_evaluated": sum(len(log.decisions) for log in result["day_logs"]),
                   "filled": len(positions), "wins": len(wins),
                   "win_rate": len(wins) / len(positions) if positions else None,
                   "gross_profit": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
                   "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
                   "total_pnl": round(sum(p.pnl or 0 for p in positions), 2)},
        "blocking_reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])),
        "fees_modelled": 0.0,
    }

    (HERE / "summary.json").write_text(json.dumps(summary, indent=2))
    with (HERE / "equity.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "equity", "open_pnl"])
        writer.writeheader()
        writer.writerows(rows)
    with (HERE / "benchmark_equity.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "equity"])
        writer.writeheader()
        writer.writerows(bench)
    with (HERE / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol","strategy","signal_date","fill_date","qty","leg_side","leg_symbol",
                    "strike","option_type","entry_price","delta"])
        for p in positions:
            for leg in p.legs:
                w.writerow([p.symbol, p.strategy, p.signal_date, p.fill_date, p.qty,
                            leg["side"], leg["symbol"], leg["strike"], leg["option_type"],
                            leg["entry_price"], leg["delta"]])
    with (HERE / "round_trips.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol","strategy","signal_date","fill_date","qty","entry_value",
                    "mark_value","pnl","max_loss","closed"])
        for p in positions:
            w.writerow([p.symbol, p.strategy, p.signal_date, p.fill_date, p.qty,
                        p.entry_value, p.mark_value, p.pnl, round(p.max_loss, 2),
                        "NO — marked, never exited"])

    print(json.dumps(summary, indent=2))


asyncio.run(main())
