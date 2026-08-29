"""Step-by-step trace of the whole decision chain for one symbol.

The agents each log a single summary line per iteration, which is right for a
service running unattended and useless when you are trying to find out *where*
a decision died. This walks the same pipeline for one symbol and reports every
stage: what was fetched, how many contracts survived each filter, what the LLM
said, what the matrix decided, and which gate refused.

It is strictly read-only. It never writes a proposal, never places an order,
and calls the same shared helpers the agents use (``session.current``,
``fetch_chain_window``, ``strategy_builder.build``, ``RiskEngine.evaluate``)
rather than reimplementing them — a trace that diverges from the real pipeline
would be worse than no trace at all.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from options_m import matrix, session, strategy_builder
from options_m.agents.execution import build_portfolio_snapshot, fetch_chain_window
from options_m.config import Settings
from options_m.earnings import is_earnings_blackout
from options_m.evidence.evidence import EvidenceCollector
from options_m.llm import FeatherlessLlm, LlmContractError
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.models import RegimeRead, Rejection, StrategyIntent
from options_m.prompts import loader as prompt_loader
from options_m.risk import RiskEngine, RiskLimits
from options_m.store import Store


@dataclass
class Step:
    """One stage of the pipeline, and what it produced."""

    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    stopped_here: bool = False


@dataclass
class Trace:
    symbol: str
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, *, ok: bool, stopped_here: bool = False, **detail: Any) -> Step:
        step = Step(name=name, ok=ok, detail=detail, stopped_here=stopped_here)
        self.steps.append(step)
        return step

    def render(self) -> str:
        lines = [f"decision trace — {self.symbol}", "=" * 60]
        for index, step in enumerate(self.steps, start=1):
            mark = "x" if step.stopped_here else ("+" if step.ok else "-")
            lines.append(f"[{mark}] {index}. {step.name}")
            for key, value in step.detail.items():
                rendered = (
                    json.dumps(value, default=str) if isinstance(value, dict | list) else value
                )
                lines.append(f"        {key}: {rendered}")
            if step.stopped_here:
                lines.append("        ^^ the chain stopped here")
        return "\n".join(lines)


async def run(
    symbol: str,
    *,
    mcp: AlpacaMcp,
    store: Store,
    settings: Settings,
    llm: FeatherlessLlm,
    use_cached_evidence: bool = True,
) -> Trace:
    """Walk the chain for ``symbol``, recording every stage."""
    trace = Trace(symbol=symbol)
    now = datetime.now(UTC)
    today = now.date()

    # 1. Session gate --------------------------------------------------------
    state = await session.current(store, settings, now)
    trace.add(
        "session gate (store.market_calendar)",
        ok=state.is_open,
        market_open=state.is_open,
        replayed=state.replayed,
        replay_flag=settings.replay_last_session,
        stopped_here=not state.is_open,
    )
    if not state.is_open:
        return trace

    # 2. Safety switches -----------------------------------------------------
    kill = settings.kill_switch or await store.is_kill_switch_engaged()
    blackout = is_earnings_blackout(symbol, today)
    trace.add(
        "safety switches",
        ok=not kill and not blackout,
        kill_switch=kill,
        earnings_blackout=blackout,
        dry_run=settings.dry_run,
        stopped_here=kill or blackout,
    )
    if kill or blackout:
        return trace

    # 3. Evidence ------------------------------------------------------------
    pack: dict[str, Any] = {}
    source = "cache (written by MarketPulseAgent)"
    if use_cached_evidence:
        row = await store.get_cached_evidence(symbol)
        pack = (row or {}).get("payload") or {}
    if not pack:
        collector = EvidenceCollector(settings, mcp, store)
        pack = await collector.collect(
            symbol, dte_min=settings.risk_dte_min, dte_max=settings.risk_dte_max
        )
        source = "collected live"
    raw_trend, raw_options = pack.get("trend"), pack.get("options")
    trend: dict[str, Any] = raw_trend if isinstance(raw_trend, dict) else {}
    options_block: dict[str, Any] = raw_options if isinstance(raw_options, dict) else {}
    trace.add(
        "evidence pack",
        ok=bool(pack),
        source=source,
        spot=trend.get("spot") or pack.get("spot"),
        sma_20=trend.get("sma_20"),
        sma_50=trend.get("sma_50"),
        rsi_14=trend.get("rsi_14"),
        realised_vol_20d=trend.get("realised_vol_20d"),
        iv_atm=options_block.get("iv_atm"),
        stopped_here=not pack,
    )
    if not pack:
        return trace

    # 4. LLM regime read -----------------------------------------------------
    if not llm.is_enabled:
        trace.add(
            "LLM regime read",
            ok=False,
            reason="llm_not_configured (FEATHERLESS_API_KEY / FEATHERLESS_MODEL_DEEP)",
            stopped_here=True,
        )
        return trace
    user_prompt = prompt_loader.load(
        "strategist", symbol=symbol, evidence_json=json.dumps(pack, default=str, indent=2)
    )
    try:
        regime: RegimeRead = await llm.complete_json(
            schema=RegimeRead,
            system=(
                "You are a quantitative options strategist. Output only valid JSON as instructed."
            ),
            user=user_prompt,
            max_tokens=settings.llm_max_tokens,
            temperature=0.2,
        )
    except LlmContractError as exc:
        trace.add("LLM regime read", ok=False, error=str(exc), stopped_here=True)
        return trace
    trace.add(
        "LLM regime read",
        ok=True,
        model=settings.featherless_model_deep,
        conviction=regime.conviction,
        thesis=regime.thesis,
        invalidation=regime.invalidation,
    )

    # 5. Deterministic matrix ------------------------------------------------
    decision = matrix.decide(pack, regime, settings=settings, as_of=today)
    if decision == "hold":
        trace.add(
            "strategy matrix",
            ok=True,
            result="hold",
            conviction_floor=settings.conviction_floor,
            stopped_here=True,
        )
        return trace
    assert isinstance(decision, StrategyIntent)  # noqa: S101 - decide returns one of the two
    intent = decision
    trace.add(
        "strategy matrix",
        ok=True,
        strategy=intent.strategy,
        target_delta=intent.target_delta,
        dte_window=[intent.dte_min, intent.dte_max],
        spread_width=intent.spread_width,
    )

    # 6. Spot ----------------------------------------------------------------
    snapshot = await mcp.get_stock_snapshot(symbol)
    latest_trade = snapshot.get("latestTrade") if isinstance(snapshot, dict) else None
    spot = finite_float(latest_trade.get("p")) if isinstance(latest_trade, dict) else None
    trace.add("spot price", ok=spot is not None, spot=spot, stopped_here=spot is None)
    if spot is None:
        return trace

    # 7. Chain fetch ---------------------------------------------------------
    contracts, snapshots = await fetch_chain_window(mcp, intent, spot=spot)
    expiries = Counter(str(c.get("expiration_date")) for c in contracts)
    trace.add(
        "chain fetch (window + strike band)",
        ok=bool(contracts),
        contracts=len(contracts),
        snapshots=len(snapshots),
        expiries={
            exp: {"dte": (date.fromisoformat(exp) - today).days, "n": n}
            for exp, n in sorted(expiries.items())
            if exp != "None"
        },
        stopped_here=not contracts,
    )
    if not contracts:
        return trace

    # 8. Normalisation and the selection funnel ------------------------------
    universe = strategy_builder.normalize_contracts(
        contracts, snapshots, spot=spot, risk_free_rate=settings.risk_free_rate
    )
    with_iv = [c for c in universe if c.implied_volatility is not None]
    with_quote = [c for c in universe if c.bid and c.ask]
    with_oi = [c for c in universe if c.open_interest is not None]
    trace.add(
        "normalise + join by OCC symbol",
        ok=bool(universe),
        parsed=len(universe),
        with_quote=len(with_quote),
        with_iv=len(with_iv),
        delta_from_chain=len([c for c in universe if c.delta is not None]),
        with_open_interest=len(with_oi),
        stopped_here=not universe,
    )
    if not universe:
        return trace

    # 9. Build ---------------------------------------------------------------
    account = await mcp.get_account_info()
    existing = await mcp.get_open_position(symbol)
    result = await strategy_builder.build(
        intent,
        contracts=contracts,
        snapshots=snapshots,
        account=account,
        existing_position=existing,
        settings=settings,
        proposal_id=0,
        spot=spot,
    )
    if isinstance(result, Rejection):
        trace.add(
            "strategy_builder.build",
            ok=False,
            rejection=result.reason,
            detail_=result.detail,
            stopped_here=True,
        )
        return trace
    plan = result
    trace.add(
        "strategy_builder.build",
        ok=True,
        legs=[f"{leg.side} {leg.symbol}" for leg in plan.legs],
        qty=plan.qty,
        limit_price=plan.limit_price,
        max_loss=plan.max_loss,
        max_profit=plan.max_profit,
        client_order_id=plan.client_order_id,
    )

    # 10. Risk gate ----------------------------------------------------------
    portfolio = await build_portfolio_snapshot(
        symbol, plan.client_order_id, account, mcp=mcp, store=store, settings=settings
    )
    verdict = RiskEngine(RiskLimits.from_settings(settings)).evaluate(plan, portfolio)
    trace.add(
        "risk gate (risk.py)",
        ok=verdict.approved,
        approved=verdict.approved,
        reasons=verdict.reasons,
        stopped_here=not verdict.approved,
    )
    if not verdict.approved:
        return trace

    # 11. What would happen next --------------------------------------------
    trace.add(
        "submission",
        ok=True,
        would_submit=not settings.dry_run,
        note=(
            "DRY_RUN is on — ExecutionAgent would mark this dry_run_approved and stop"
            if settings.dry_run
            else "ExecutionAgent would call place_option_order with this plan"
        ),
    )
    return trace
