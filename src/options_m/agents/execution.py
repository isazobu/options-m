"""ExecutionAgent — turns an approved proposal into a real paper order.

No LLM. Every broker read from the point a proposal is picked up onward is
strict: an exception propagates out of :meth:`step`, exactly like
``MarketPulseAgent``, so a broker outage triggers the supervisor's backoff
instead of being read as "nothing to do". Only the deliberately-tolerant
steps (parsing the proposal's own intent, classifying a builder/risk
rejection) are caught and recorded as a normal outcome.

Order submission never fabricates a fill. A duplicate ``client_order_id`` is
reconciled as a success — Alpaca's own recovery path — never retried as a
failure; anything else that fails is written down as ``failed`` with the real
error text, and dry run never even attempts the call.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import ValidationError

from options_m import session, strategy_builder
from options_m.config import Settings
from options_m.evidence.occ import parse_occ_symbol
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.models import OrderPlan, Rejection, StrategyIntent
from options_m.risk import PortfolioSnapshot, RiskEngine, RiskVerdict
from options_m.store import Store

logger = logging.getLogger(__name__)

_PENDING_BATCH_SIZE = 5
# Strike band around spot for the contract/chain pulls, as a fraction of spot.
# Wider than evidence.py's 0.15 because the builder has to reach a target
# delta and may need strikes further out than an ATM IV read ever does.
_STRIKE_BAND = 0.25


def _looks_like_duplicate(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in ("duplicate", "already exists"))


def _decimal_str(value: float, places: int = 2) -> str:
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def _build_closing_legs(option_legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return legs with sides inverted for a sell_to_close order.

    Alpaca position sides are ``"long"``/``"short"``; order sides are
    ``"buy"``/``"sell"``. Long → sell to close, short → buy to close.
    """
    result = []
    for leg in option_legs:
        occ = str(leg.get("symbol", "")).upper()
        try:
            qty_int = max(1, abs(int(float(str(leg.get("qty", "1"))))))
        except (TypeError, ValueError):
            qty_int = 1
        entry_side = str(leg.get("side", "long")).lower()
        close_side = "sell" if entry_side == "long" else "buy"
        result.append({"symbol": occ, "side": close_side, "ratio_qty": str(qty_int)})
    return result


def _build_order_request(plan: OrderPlan) -> dict[str, Any]:
    """Build the exact kwargs ``AlpacaMcp.place_option_order`` expects.

    Every numeric is built from ``Decimal`` and sent as a string, never a
    Python float repr.
    """
    limit_price = _decimal_str(plan.limit_price)
    if len(plan.legs) == 1:
        leg = plan.legs[0]
        return {
            "qty": str(plan.qty),
            "limit_price": limit_price,
            "client_order_id": plan.client_order_id,
            "symbol": leg.symbol,
            "side": leg.side,
            "position_intent": "buy_to_open" if leg.side == "buy" else "sell_to_open",
        }
    legs = [
        {
            "symbol": leg.symbol,
            "ratio_qty": str(leg.ratio),
            "side": leg.side,
            "position_intent": "buy_to_open" if leg.side == "buy" else "sell_to_open",
        }
        for leg in plan.legs
    ]
    return {
        "qty": str(plan.qty),
        "limit_price": limit_price,
        "client_order_id": plan.client_order_id,
        "legs": legs,
    }


def _spot_from_snapshot(snapshot: dict[str, Any]) -> float | None:
    trade = snapshot.get("latestTrade")
    if isinstance(trade, dict):
        price = finite_float(trade.get("p"))
        if price is not None:
            return price
    quote = snapshot.get("latestQuote")
    if isinstance(quote, dict):
        bid, ask = finite_float(quote.get("bp")), finite_float(quote.get("ap"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2
    return None


def _minutes_until(timestamp: Any) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when - datetime.now(UTC)).total_seconds() / 60


async def fetch_chain_window(
    mcp: AlpacaMcp, intent: StrategyIntent, *, spot: float
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Pull the contracts and snapshots for exactly the window ``intent`` wants.

    Alpaca returns nearest expiry first, so asking "from today" spends the
    early pages on 2-6 DTE contracts and, before the client paginated, never
    reached a 21-38 DTE target — every proposal then died in the builder as
    ``no_contracts_in_window``. Starting the window at ``dte_min`` narrows it
    to what the intent actually needs; the client now follows
    ``next_page_token``, so a wide chain is read in full rather than truncated.

    Shared with ``cli.py``'s ``plan`` and ``trace`` so the manual diagnostic
    path and the running agent cannot drift into fetching different data.
    """
    today = date.today()
    gte = (today + timedelta(days=intent.dte_min)).isoformat()
    lte = (today + timedelta(days=intent.dte_max)).isoformat()
    strike_gte = spot * (1 - _STRIKE_BAND)
    strike_lte = spot * (1 + _STRIKE_BAND)
    contracts = await mcp.get_option_contracts(
        intent.underlying,
        expiration_gte=gte,
        expiration_lte=lte,
        strike_gte=strike_gte,
        strike_lte=strike_lte,
    )
    snapshots = await mcp.get_option_chain(
        intent.underlying,
        expiration_gte=gte,
        expiration_lte=lte,
        strike_gte=strike_gte,
        strike_lte=strike_lte,
    )
    return contracts, snapshots


def _group_into_structures(option_positions: list[dict[str, Any]]) -> set[tuple[str, date]]:
    """Collapse individual option legs into the structures they belong to.

    Alpaca reports one position per leg, so an iron condor reads as four. The
    position limits are written in structures — "at most one open trade per
    underlying", "at most five open trades" — and counting legs against them
    would let a single condor consume the entire portfolio budget.

    Legs of one structure share an underlying and an expiry, which is what the
    key is. A symbol that will not parse as OCC counts as a structure of its
    own: the limits exist to cap exposure, so an unrecognised position must
    never make the count smaller.
    """
    structures: set[tuple[str, date]] = set()
    for position in option_positions:
        symbol = str(position.get("symbol", ""))
        occ = parse_occ_symbol(symbol)
        structures.add((occ.underlying, occ.expiry) if occ is not None else (symbol, date.min))
    return structures


async def build_portfolio_snapshot(
    underlying: str,
    client_order_id: str,
    account: dict[str, Any],
    *,
    mcp: AlpacaMcp,
    store: Store,
    settings: Settings,
) -> PortfolioSnapshot:
    """Shared by :class:`ExecutionAgent` and ``cli.py``'s ``plan`` command."""
    clock = await mcp.get_clock()
    positions = await mcp.get_all_positions()
    option_positions = [p for p in positions if p.get("asset_class") == "us_option"]
    existing_order = await store.order_by_client_id(client_order_id)
    equity_history = await store.recent_equity(limit=500)
    finite_equities = [
        e for row in equity_history if (e := finite_float(row.get("equity"))) is not None
    ]

    structures = _group_into_structures(option_positions)

    return PortfolioSnapshot(
        equity=finite_float(account.get("equity")),
        # last_equity is Alpaca's own "equity as of previous close" field —
        # exactly the daily-loss baseline, with no timezone-boundary guessing.
        start_of_day_equity=finite_float(account.get("last_equity")),
        high_water_mark=max(finite_equities) if finite_equities else None,
        concurrent_option_positions=len(structures),
        positions_in_underlying=sum(1 for root, _expiry in structures if root == underlying),
        total_open_option_premium=sum(
            abs(finite_float(p.get("market_value")) or 0.0) for p in option_positions
        ),
        # The session gate, not the raw clock: under REPLAY_LAST_SESSION the
        # broker correctly reports closed while the agents are deliberately
        # replaying the last real session. minutes_to_close still comes from
        # the clock — out of hours next_close is the *next* session's close, so
        # the end-of-day blackout passes rather than rejecting on a negative.
        market_is_open=(await session.current(store, settings, datetime.now(UTC))).is_open,
        minutes_to_close=_minutes_until(clock.get("next_close")),
        kill_switch_engaged=settings.kill_switch or await store.is_kill_switch_engaged(),
        already_submitted=existing_order is not None,
    )


class ExecutionAgent:
    """Picks up pending proposals, builds a plan, risk-gates it, submits it."""

    def __init__(
        self, settings: Settings, mcp: AlpacaMcp, store: Store, risk_engine: RiskEngine
    ) -> None:
        self._settings = settings
        self._mcp = mcp
        self._store = store
        self._risk = risk_engine

    @property
    def name(self) -> str:
        return "execution"

    @property
    def interval_seconds(self) -> float:
        return self._settings.execution_agent_interval_seconds

    async def step(self) -> None:
        """One iteration. Raises on a real failure so the supervisor backs off."""
        started = time.monotonic()
        ok = True
        error: str | None = None
        detail: dict[str, Any] = {}
        try:
            detail = await self._run()
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await self._store.record_agent_run(
                self.name,
                duration_ms=int((time.monotonic() - started) * 1000),
                ok=ok,
                error=error,
                detail=detail or None,
            )

    async def _run(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "processed": 0,
            "submitted": 0,
            "rejected": 0,
            "failed": 0,
            "reconciled": 0,
        }
        kill_switch_engaged = (
            self._settings.kill_switch or await self._store.is_kill_switch_engaged()
        )
        if kill_switch_engaged:
            await self._store.record_risk_event(
                proposal_id=None, rule="kill_switch_engaged", detail={}
            )
            detail["kill_switch"] = True
            return detail

        for proposal in await self._store.pending_proposals(limit=_PENDING_BATCH_SIZE):
            await self._process_one(proposal, detail)
            detail["processed"] += 1

        await self._reconcile(detail)
        return detail

    async def _process_one(self, proposal: dict[str, Any], detail: dict[str, Any]) -> None:
        proposal_id = int(proposal["id"])
        try:
            intent = StrategyIntent.model_validate(proposal["intent"])
        except ValidationError as exc:
            await self._store.update_proposal_status(proposal_id, "rejected", error=str(exc))
            await self._store.record_risk_event(
                proposal_id=proposal_id, rule="invalid_intent", detail={"error": str(exc)}
            )
            detail["rejected"] += 1
            return

        if intent.action == "close":
            await self._execute_close(proposal_id, intent, detail)
            return

        if intent.action != "open":
            status = "held" if intent.action == "hold" else "deferred_close"
            await self._store.update_proposal_status(proposal_id, status)
            return

        # From here on every broker read is strict: a failure must reach the
        # supervisor, not be swallowed as "this proposal has no plan".
        account = await self._mcp.get_account_info()
        snapshot = await self._mcp.get_stock_snapshot(intent.underlying)
        spot = _spot_from_snapshot(snapshot)
        if spot is None:
            rejection = Rejection(proposal_id=proposal_id, reason="no_spot_price")
            await self._reject(proposal_id, rejection)
            detail["rejected"] += 1
            return

        contracts, snapshots = await fetch_chain_window(self._mcp, intent, spot=spot)
        existing_position = await self._mcp.get_open_position(intent.underlying)

        result = await strategy_builder.build(
            intent,
            contracts=contracts,
            snapshots=snapshots,
            account=account,
            existing_position=existing_position,
            settings=self._settings,
            proposal_id=proposal_id,
            spot=spot,
        )
        if isinstance(result, Rejection):
            await self._reject(proposal_id, result)
            detail["rejected"] += 1
            return
        plan = result

        portfolio = await build_portfolio_snapshot(
            intent.underlying,
            plan.client_order_id,
            account,
            mcp=self._mcp,
            store=self._store,
            settings=self._settings,
        )
        verdict = self._risk.evaluate(plan, portfolio)
        if not verdict.approved:
            for reason in verdict.reasons:
                await self._store.record_risk_event(
                    proposal_id=proposal_id, rule=reason, detail={}
                )
            await self._store.update_proposal_status(
                proposal_id,
                "rejected",
                plan=plan.model_dump(mode="json"),
                verdict=verdict.model_dump(),
                error="; ".join(verdict.reasons),
            )
            detail["rejected"] += 1
            return

        if self._settings.dry_run:
            await self._store.update_proposal_status(
                proposal_id,
                "dry_run_approved",
                plan=plan.model_dump(mode="json"),
                verdict=verdict.model_dump(),
            )
            return

        await self._submit(proposal_id, plan, verdict, detail)

    async def _execute_close(
        self, proposal_id: int, intent: StrategyIntent, detail: dict[str, Any]
    ) -> None:
        """Execute a close proposal: build sell-to-close order from the positions
        cache and submit it via place_option_order. No chain/snapshot fetch needed
        — the positions cache already has the open legs and the mark price."""
        positions = await self._store.get_cached_positions()
        position_row = next(
            (r for r in positions if r["symbol"] == intent.underlying), None
        )
        if position_row is None:
            # Position was already closed externally between proposal creation and now.
            await self._store.update_proposal_status(
                proposal_id, "close_missed", error="position_not_found_in_cache"
            )
            return

        payload = position_row.get("payload") or {}
        legs: list[dict[str, Any]] = payload.get("legs", [])
        option_legs = [
            leg for leg in legs
            if re.match(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", str(leg.get("symbol", "")).upper())
        ]
        if not option_legs:
            await self._store.update_proposal_status(
                proposal_id, "rejected", error="no_option_legs_in_position"
            )
            detail["rejected"] += 1
            return

        closing_legs = _build_closing_legs(option_legs)

        # Limit price from the position mark: market_value / struct_qty / 100.
        market_value = abs(float(payload.get("market_value") or 0))
        try:
            struct_qty = max(1, abs(int(float(str(option_legs[0].get("qty", "1"))))))
        except (TypeError, ValueError):
            struct_qty = 1
        raw_price = market_value / (struct_qty * 100) if market_value > 0 else 0.01
        limit_price = _decimal_str(max(0.01, raw_price))

        client_order_id = f"omc-{proposal_id}"
        close_request: dict[str, Any] = {
            "action": "close",
            "underlying": intent.underlying,
            "legs": closing_legs,
            "exit_reason": intent.thesis,
        }

        if self._settings.dry_run:
            await self._store.update_proposal_status(
                proposal_id, "dry_run_approved", plan=close_request
            )
            return

        if len(closing_legs) == 1:
            leg = closing_legs[0]
            kwargs: dict[str, Any] = {
                "qty": str(struct_qty),
                "limit_price": limit_price,
                "client_order_id": client_order_id,
                "position_intent": "sell_to_close",
                "symbol": leg["symbol"],
                "side": leg["side"],
            }
        else:
            kwargs = {
                "qty": str(struct_qty),
                "limit_price": limit_price,
                "client_order_id": client_order_id,
                "position_intent": "sell_to_close",
                "legs": closing_legs,
            }

        try:
            response = await self._mcp.place_option_order(**kwargs)
        except Exception as exc:
            await self._store.record_order(
                proposal_id=proposal_id,
                client_order_id=client_order_id,
                status="failed",
                request=close_request,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._store.update_proposal_status(proposal_id, "failed", error=str(exc))
            detail["failed"] += 1
            return

        if isinstance(response, dict) and "error" in response:
            error_text = str(response["error"])
            await self._store.record_order(
                proposal_id=proposal_id,
                client_order_id=client_order_id,
                status="failed",
                request=close_request,
                response=response,
                error=error_text,
            )
            await self._store.update_proposal_status(proposal_id, "failed", error=error_text)
            detail["failed"] += 1
            return

        await self._store.record_order(
            proposal_id=proposal_id,
            client_order_id=client_order_id,
            status="close_submitted",
            request=close_request,
            response=response,
        )
        await self._store.update_proposal_status(proposal_id, "close_submitted")
        detail["submitted"] += 1
        logger.info(
            "execution: close submitted",
            extra={"underlying": intent.underlying, "client_order_id": client_order_id},
        )

    async def _reject(self, proposal_id: int, rejection: Rejection) -> None:
        await self._store.update_proposal_status(proposal_id, "rejected", error=rejection.reason)
        await self._store.record_risk_event(
            proposal_id=proposal_id,
            rule=f"strategy:{rejection.reason}",
            detail=rejection.detail,
        )

    async def _submit(
        self, proposal_id: int, plan: OrderPlan, verdict: RiskVerdict, detail: dict[str, Any]
    ) -> None:
        request = _build_order_request(plan)
        try:
            response = await self._mcp.place_option_order(**request)
        except Exception as exc:
            if _looks_like_duplicate(str(exc)):
                await self._reconcile_duplicate(proposal_id, plan, request, detail)
                return
            await self._store.record_order(
                proposal_id=proposal_id,
                client_order_id=plan.client_order_id,
                status="failed",
                request=request,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._store.update_proposal_status(proposal_id, "failed", error=str(exc))
            detail["failed"] += 1
            return

        if isinstance(response, dict) and "error" in response:
            error_text = str(response["error"])
            if _looks_like_duplicate(error_text):
                await self._reconcile_duplicate(proposal_id, plan, request, detail)
                return
            await self._store.record_order(
                proposal_id=proposal_id,
                client_order_id=plan.client_order_id,
                status="failed",
                request=request,
                response=response,
                error=error_text,
            )
            await self._store.update_proposal_status(proposal_id, "failed", error=error_text)
            detail["failed"] += 1
            return

        await self._store.record_order(
            proposal_id=proposal_id,
            client_order_id=plan.client_order_id,
            status="submitted",
            request=request,
            response=response,
        )
        await self._store.update_proposal_status(
            proposal_id,
            "submitted",
            plan=plan.model_dump(mode="json"),
            verdict=verdict.model_dump(),
        )
        detail["submitted"] += 1

    async def _reconcile_duplicate(
        self, proposal_id: int, plan: OrderPlan, request: dict[str, Any], detail: dict[str, Any]
    ) -> None:
        """A duplicate client_order_id is Alpaca's documented success path.

        The order already exists; reconcile it instead of treating the
        rejection as a failure.
        """
        broker_order = await self._mcp.get_order_by_client_id(plan.client_order_id)
        if broker_order is None:
            # Ambiguous: the broker calls it a duplicate but has no record
            # under this id. Record the ambiguity plainly rather than guess.
            await self._store.record_order(
                proposal_id=proposal_id,
                client_order_id=plan.client_order_id,
                status="ambiguous",
                request=request,
                error="broker reported a duplicate but the order was not found by client_order_id",
            )
            await self._store.update_proposal_status(proposal_id, "ambiguous")
            return
        status = str(broker_order.get("status", "submitted"))
        await self._store.record_order(
            proposal_id=proposal_id,
            client_order_id=plan.client_order_id,
            status=status,
            request=request,
            response=broker_order,
        )
        await self._store.update_proposal_status(proposal_id, "submitted")
        detail["submitted"] += 1

    async def _reconcile(self, detail: dict[str, Any]) -> None:
        for order in await self._store.orders_in_flight():
            client_order_id = str(order["client_order_id"])
            try:
                broker_order = await self._mcp.get_order_by_client_id(client_order_id)
            except Exception:
                logger.warning(
                    "reconciliation read failed; leaving order status untouched",
                    extra={"client_order_id": client_order_id},
                    exc_info=True,
                )
                continue
            if broker_order is None:
                continue
            await self._store.update_order_status(
                client_order_id,
                status=str(broker_order.get("status", order["status"])),
                response=broker_order,
                filled_qty=finite_float(broker_order.get("filled_qty")),
                filled_avg_price=finite_float(broker_order.get("filled_avg_price")),
            )
            detail["reconciled"] += 1
