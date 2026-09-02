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

Reconciliation polls every still-open order each tick. When the broker
rejects, cancels or expires an order it had accepted, the proposal is marked
``broker_rejected`` with a ``broker_rejected`` risk event, so the underlying
is not left blocked by a dead order.
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import ValidationError

from options_m import exposure, session, sizing, strategy_builder
from options_m.config import Settings
from options_m.evidence.occ import parse_occ_symbol
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.models import OrderPlan, Rejection, StrategyIntent
from options_m.notify import Notifier, NullNotifier, format_order
from options_m.risk import PortfolioSnapshot, RiskEngine, RiskVerdict
from options_m.store import Store

logger = logging.getLogger(__name__)

_PENDING_BATCH_SIZE = 5
# Strike band around spot for the contract/chain pulls, as a fraction of spot.
# Wider than evidence.py's 0.15 because the builder has to reach a target
# delta and may need strikes further out than an ATM IV read ever does.
_STRIKE_BAND = 0.25

# Broker order states that will not progress on their own and did not open a
# position — the broker finished with an order it had accepted without filling
# it. Reconcile releases the proposal (``broker_rejected``) rather than leaving
# it ``submitted`` and blocking its underlying forever. Compared
# case-insensitively; aligned with store._SETTLED_ORDER_STATES minus ``filled``
# and ``failed`` (a fill opened a position; ``failed`` never reaches the broker).
_TERMINAL_UNFILLED_STATES = frozenset(
    {"canceled", "cancelled", "expired", "rejected", "replaced", "done_for_day"}
)
# Keys an Alpaca order object may carry an explanation under. None is standard,
# so this is best-effort — the status string is the fallback.
_BROKER_REASON_KEYS = ("reason", "reject_reason", "rejected_reason", "cancel_reason")


def _broker_reason(broker_order: dict[str, Any]) -> str | None:
    for key in _BROKER_REASON_KEYS:
        value = broker_order.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_terminal_unfilled(status: str, filled_qty: float | None) -> bool:
    """True when the broker is done with an order that never opened a position."""
    if filled_qty is not None and filled_qty > 0:
        return False
    return status.strip().lower() in _TERMINAL_UNFILLED_STATES


def _looks_like_duplicate(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in ("duplicate", "already exists"))


def _decimal_str(value: float, places: int = 2) -> str:
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
_URGENT_CLOSE_REASONS = frozenset(
    {"stop_loss", "expiry_hard_stop", "dte_stop", "time_stop", "campaign_flatten"}
)


def _close_reason_name(thesis: str) -> str:
    return thesis.split(":", 1)[0].strip()


def _close_limit_price(
    mark: float,
    reason: str,
    *,
    nudge: float,
    attempt: int,
) -> float:
    """Return a progressively more marketable debit limit for a close."""
    initial_rung = 1 if reason in _URGENT_CLOSE_REASONS else 0
    rung = initial_rung + max(0, attempt)
    return max(0.01, mark * (1.0 + rung * nudge))


def _build_closing_legs(option_legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return legs with sides and position intents set for closing.

    Alpaca position sides are ``"long"``/``"short"``; order sides are
    ``"buy"``/``"sell"``. Long → sell to close, short → buy to close.
    """
    structure_qty = _closing_structure_qty(option_legs)
    result = []
    for leg in option_legs:
        occ = str(leg.get("symbol", "")).upper()
        try:
            qty_int = max(1, abs(int(float(str(leg.get("qty", "1"))))))
        except (TypeError, ValueError):
            qty_int = 1
        entry_side = str(leg.get("side", "long")).lower()
        close_side = "sell" if entry_side == "long" else "buy"
        result.append(
            {
                "symbol": occ,
                "side": close_side,
                "ratio_qty": str(max(1, qty_int // structure_qty)),
                "position_intent": (
                    "sell_to_close" if close_side == "sell" else "buy_to_close"
                ),
            }
        )
    return result


def _closing_structure_qty(option_legs: list[dict[str, Any]]) -> int:
    """Greatest common leg quantity: the parent multiplier for an MLeg close."""
    quantities: list[int] = []
    for leg in option_legs:
        try:
            quantities.append(max(1, abs(int(float(str(leg.get("qty", "1")))))))
        except (TypeError, ValueError):
            quantities.append(1)
    return math.gcd(*quantities) if quantities else 1


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


async def _book_greeks_from_snapshots(
    mcp: AlpacaMcp, option_positions: list[dict[str, Any]]
) -> dict[str, tuple[float | None, float | None]]:
    """Live delta/vega for each open OCC symbol, or empty if the feed is down.

    Same call the dashboard already makes. A failure here must not raise: the
    risk gate skips an unknown Greek instead of treating a snapshot blip as a
    hard reject.
    """
    symbols = [
        str(row["symbol"])
        for row in option_positions
        if row.get("asset_class") == "us_option" and row.get("symbol")
    ]
    if not symbols:
        return {}
    try:
        snapshots = await mcp.get_option_snapshot(symbols)
    except Exception:
        logger.warning("option snapshot for book greeks failed", exc_info=True)
        return {}
    return {occ: exposure.greeks_from_snapshot(snap) for occ, snap in snapshots.items()}


async def _projected_exposure(
    option_positions: list[dict[str, Any]],
    plan: OrderPlan | None,
    *,
    spot: float | None,
    store: Store,
    settings: Settings,
    mcp: AlpacaMcp,
) -> exposure.Exposure:
    """The open book's exposure plus the plan's, in one figure per Greek.

    Live greeks come from ``get_option_snapshot`` (the same MCP tool the
    dashboard uses). Evidence-pack ATM vol is only a Black-Scholes fallback.
    A field that is still missing after both stays unknown and the matching
    risk cap is skipped, not used as a rejection.
    """
    market: dict[str, tuple[float, float | None]] = {}
    for root in {root for root, _expiry in _group_into_structures(option_positions)}:
        row = await store.get_cached_evidence(root)
        payload = row.get("payload") if row else None
        if isinstance(payload, dict) and (found := exposure.market_from_evidence(payload)):
            market[root] = found

    book = exposure.book_exposure(
        option_positions,
        market_by_symbol=market,
        risk_free_rate=settings.risk_free_rate,
        greeks_by_symbol=await _book_greeks_from_snapshots(mcp, option_positions),
    )
    if plan is None:
        return book
    if spot is None:
        return exposure.Exposure.unknown(len(plan.legs))
    # The plan's own ATM vol comes from the same cache as the book's, so the two
    # halves of the sum are computed on consistent inputs. A missing pack leaves
    # iv None, which makes the plan's vega — and therefore the total — unknown.
    plan_row = await store.get_cached_evidence(plan.underlying)
    plan_payload = plan_row.get("payload") if plan_row else None
    plan_iv: float | None = None
    if isinstance(plan_payload, dict) and (found := exposure.market_from_evidence(plan_payload)):
        plan_iv = found[1]
    added = exposure.plan_exposure(
        plan, spot=spot, iv=plan_iv, risk_free_rate=settings.risk_free_rate
    )
    return book.combined_with(added)


async def build_portfolio_snapshot(
    underlying: str,
    client_order_id: str,
    account: dict[str, Any],
    *,
    mcp: AlpacaMcp,
    store: Store,
    settings: Settings,
    exclude_proposal_id: int | None = None,
    plan: OrderPlan | None = None,
    spot: float | None = None,
) -> PortfolioSnapshot:
    """Shared by :class:`ExecutionAgent` and ``cli.py``'s ``plan`` command.

    ``plan`` and ``spot`` are what make the portfolio-Greeks checks meaningful:
    the snapshot reports the book's exposure *including* the order under
    evaluation, because a cap that only measures what is already open would
    approve the trade that breaches it. Omitting them describes the book alone,
    which is correct for inspection but not for gating an order.
    """
    clock = await mcp.get_clock()
    positions = await mcp.get_all_positions()
    option_positions = [p for p in positions if p.get("asset_class") == "us_option"]
    existing_order = await store.order_by_client_id(client_order_id)
    equity_history = await store.recent_equity(limit=500)
    finite_equities = [
        e for row in equity_history if (e := finite_float(row.get("equity"))) is not None
    ]

    structures = _group_into_structures(option_positions)
    underlyings_with_position = {root for root, _expiry in structures}

    # A resting limit order and an approved-but-unsubmitted proposal each hold
    # a position slot that get_all_positions cannot see yet. Counting only
    # filled positions here lets the same symbol be re-proposed and
    # re-submitted while its first order is still working, beating
    # MAX_POSITIONS_PER_UNDERLYING and the concurrent cap in practice. An
    # unfilled om-<id> order must occupy the slot exactly as a filled one does.
    slot_holders = await store.working_order_underlyings()
    slot_holders |= await store.active_proposal_underlyings(
        exclude_proposal_id=exclude_proposal_id
    )
    # Drop anything already counted as a filled position so a proposal that has
    # since filled is not double-counted against the caps.
    slot_holders -= underlyings_with_position

    target = underlying.upper()
    options_buying_power, _source = sizing.resolve_options_buying_power(account)
    projected = await _projected_exposure(
        option_positions, plan, spot=spot, store=store, settings=settings, mcp=mcp
    )
    return PortfolioSnapshot(
        equity=finite_float(account.get("equity")),
        options_buying_power=options_buying_power,
        projected_beta_weighted_delta=projected.beta_weighted_delta,
        projected_net_vega=projected.net_vega,
        # last_equity is Alpaca's own "equity as of previous close" field —
        # exactly the daily-loss baseline, with no timezone-boundary guessing.
        start_of_day_equity=finite_float(account.get("last_equity")),
        high_water_mark=max(finite_equities) if finite_equities else None,
        concurrent_option_positions=len(structures) + len(slot_holders),
        positions_in_underlying=(
            sum(1 for root, _expiry in structures if root == target)
            + (1 if target in slot_holders else 0)
        ),
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
        self,
        settings: Settings,
        mcp: AlpacaMcp,
        store: Store,
        risk_engine: RiskEngine,
        notifier: Notifier | None = None,
    ) -> None:
        self._settings = settings
        self._mcp = mcp
        self._store = store
        self._risk = risk_engine
        self._notifier = notifier or NullNotifier()

    def _announce(self, **fields: Any) -> None:
        """Push one terminal order event to Telegram. Never raises, never blocks."""
        if not self._settings.telegram_notify_orders:
            return
        self._notifier.notify(format_order(dry_run=self._settings.dry_run, **fields))

    def _announce_plan(self, plan: OrderPlan, status: str, *, error: str | None = None) -> None:
        """Announce an entry order, described by the plan that produced it."""
        self._announce(
            action="open",
            underlying=plan.underlying,
            status=status,
            legs=[f"{leg.side} {leg.symbol}" for leg in plan.legs],
            qty=plan.qty,
            limit_price=plan.limit_price,
            client_order_id=plan.client_order_id,
            error=error,
        )

    def _announce_close(
        self,
        underlying: str,
        legs: list[dict[str, Any]],
        qty: int,
        limit_price: str,
        client_order_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        """Announce an exit order. Closes have no OrderPlan — legs come from the cache."""
        self._announce(
            action="close",
            underlying=underlying,
            status=status,
            legs=[f"{leg.get('side', '?')} {leg.get('symbol', '?')}" for leg in legs],
            qty=qty,
            limit_price=limit_price,
            client_order_id=client_order_id,
            error=error,
        )

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
            "broker_rejected": 0,
        }
        kill_switch_engaged = (
            self._settings.kill_switch or await self._store.is_kill_switch_engaged()
        )
        if kill_switch_engaged:
            detail["kill_switch"] = True

        pending_limit = 50 if kill_switch_engaged else _PENDING_BATCH_SIZE
        for proposal in await self._store.pending_proposals(limit=pending_limit):
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

        if intent.action == "open" and (
            self._settings.kill_switch or await self._store.is_kill_switch_engaged()
        ):
            detail["open_blocked_by_kill_switch"] = (
                detail.get("open_blocked_by_kill_switch", 0) + 1
            )
            return

        if intent.action != "open":
            status = "held" if intent.action == "hold" else "deferred_close"
            await self._store.update_proposal_status(proposal_id, status)
            return

        # Another proposal for this underlying is already pending, dry-run
        # approved, or resting as a working order. The risk gate's idempotency
        # check is exact-client_order_id only, so a fresh proposal_id slips past
        # it — reject here before spending any broker calls on a plan that must
        # not be placed.
        active_underlyings = await self._store.active_proposal_underlyings(
            exclude_proposal_id=proposal_id
        )
        if intent.underlying.upper() in active_underlyings:
            await self._store.update_proposal_status(
                proposal_id,
                "rejected",
                error="another proposal for this underlying is already in flight",
            )
            await self._store.record_risk_event(
                proposal_id=proposal_id,
                rule="duplicate_underlying_in_flight",
                detail={"underlying": intent.underlying.upper()},
            )
            detail["rejected"] += 1
            self._announce(
                action="open",
                underlying=intent.underlying.upper(),
                status="rejected",
                client_order_id=f"om-{proposal_id}",
                error="another proposal for this underlying is already in flight",
            )
            return

        # From here on every broker read is strict: a failure must reach the
        # supervisor, not be swallowed as "this proposal has no plan".
        account = await self._mcp.get_account_info()
        snapshot = await self._mcp.get_stock_snapshot(intent.underlying)
        spot = _spot_from_snapshot(snapshot)
        if spot is None:
            rejection = Rejection(proposal_id=proposal_id, reason="no_spot_price")
            await self._reject(proposal_id, rejection, intent.underlying)
            detail["rejected"] += 1
            return

        contracts, snapshots = await fetch_chain_window(self._mcp, intent, spot=spot)
        existing_position = await self._mcp.get_open_position(intent.underlying)

        sizing_state = await sizing.build_sizing_state(
            account, store=self._store, settings=self._settings
        )
        result = await strategy_builder.build(
            intent,
            contracts=contracts,
            snapshots=snapshots,
            account=account,
            existing_position=existing_position,
            settings=self._settings,
            proposal_id=proposal_id,
            spot=spot,
            sizing_state=sizing_state,
        )
        if isinstance(result, Rejection):
            await self._reject(proposal_id, result, intent.underlying)
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
            exclude_proposal_id=proposal_id,
            plan=plan,
            spot=spot,
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
            self._announce_plan(plan, "rejected", error="; ".join(verdict.reasons))
            return

        if self._settings.dry_run:
            await self._store.update_proposal_status(
                proposal_id,
                "dry_run_approved",
                plan=plan.model_dump(mode="json"),
                verdict=verdict.model_dump(),
            )
            self._announce_plan(plan, "dry_run_approved")
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
            leg for leg in legs if _OCC_RE.match(str(leg.get("symbol", "")).upper())
        ]
        if not option_legs:
            await self._store.update_proposal_status(
                proposal_id, "rejected", error="no_option_legs_in_position"
            )
            detail["rejected"] += 1
            return

        closing_legs = _build_closing_legs(option_legs)

        # Limit price from the position mark, per structure: |value| / qty / 100.
        # The net is what closing the structure actually costs or pays -- the
        # gross market_value double-counts a spread's two legs and would send a
        # credit spread out at roughly twice the price it can be bought back at.
        net_value = payload.get("net_value")
        raw_value = net_value if net_value is not None else payload.get("market_value")
        mark_value = abs(float(raw_value or 0))
        struct_qty = _closing_structure_qty(option_legs)
        raw_price = mark_value / (struct_qty * 100) if mark_value > 0 else 0.01
        exit_reason = _close_reason_name(intent.thesis)
        initial_price = _close_limit_price(
            raw_price,
            exit_reason,
            nudge=self._settings.limit_price_spread_nudge_pct,
            attempt=0,
        )
        limit_price = _decimal_str(initial_price)

        client_order_id = f"omc-{proposal_id}"
        close_request: dict[str, Any] = {
            "action": "close",
            "underlying": intent.underlying,
            "legs": closing_legs,
            "exit_reason": exit_reason,
            "mark_price": raw_price,
            "limit_price": limit_price,
        }

        if self._settings.dry_run:
            await self._store.update_proposal_status(
                proposal_id, "dry_run_approved", plan=close_request
            )
            self._announce_close(
                intent.underlying, closing_legs, struct_qty, limit_price,
                client_order_id, "dry_run_approved",
            )
            return

        if len(closing_legs) == 1:
            leg = closing_legs[0]
            kwargs: dict[str, Any] = {
                "qty": str(struct_qty),
                "limit_price": limit_price,
                "client_order_id": client_order_id,
                "position_intent": leg["position_intent"],
                "symbol": leg["symbol"],
                "side": leg["side"],
            }
        else:
            kwargs = {
                "qty": str(struct_qty),
                "limit_price": limit_price,
                "client_order_id": client_order_id,
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
            self._announce_close(
                intent.underlying, closing_legs, struct_qty, limit_price,
                client_order_id, "failed", error=f"{type(exc).__name__}: {exc}",
            )
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
            self._announce_close(
                intent.underlying, closing_legs, struct_qty, limit_price,
                client_order_id, "failed", error=error_text,
            )
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
        self._announce_close(
            intent.underlying, closing_legs, struct_qty, limit_price,
            client_order_id, "close_submitted",
        )

    async def _reject(self, proposal_id: int, rejection: Rejection, underlying: str = "") -> None:
        await self._store.update_proposal_status(proposal_id, "rejected", error=rejection.reason)
        await self._store.record_risk_event(
            proposal_id=proposal_id,
            rule=f"strategy:{rejection.reason}",
            detail=rejection.detail,
        )
        self._announce(
            action="open",
            underlying=underlying or "?",
            status="rejected",
            client_order_id=f"om-{proposal_id}",
            error=rejection.reason,
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
            self._announce_plan(plan, "failed", error=f"{type(exc).__name__}: {exc}")
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
            self._announce_plan(plan, "failed", error=error_text)
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
        self._announce_plan(plan, "submitted")

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
        if _is_terminal_unfilled(status, finite_float(broker_order.get("filled_qty"))):
            # The pre-existing order is already dead: release the proposal now
            # rather than parking it in ``submitted`` where reconcile — which
            # only polls non-terminal orders — would never revisit it.
            await self._mark_broker_rejected(
                proposal_id, plan.client_order_id, broker_order, status, detail
            )
            return
        await self._store.update_proposal_status(proposal_id, "submitted")
        detail["submitted"] += 1

    async def _read_active_order(
        self, order: dict[str, Any], client_order_id: str
    ) -> dict[str, Any] | None:
        stored_response = order.get("response")
        active_order_id = (
            stored_response.get("_active_order_id")
            if isinstance(stored_response, dict)
            else None
        )
        if isinstance(active_order_id, str) and active_order_id:
            return await self._mcp.get_order_by_id(active_order_id)
        return await self._mcp.get_order_by_client_id(client_order_id)

    async def _maybe_reprice_close(
        self,
        order: dict[str, Any],
        broker_order: dict[str, Any],
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        client_order_id = str(order["client_order_id"])
        if (
            not client_order_id.startswith("omc-")
            or self._settings.close_reprice_max_attempts == 0
        ):
            return broker_order
        request = order.get("request")
        submitted_at = order.get("submitted_at")
        if not isinstance(request, dict) or not isinstance(submitted_at, datetime):
            return broker_order
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=UTC)
        elapsed = max(0.0, (datetime.now(UTC) - submitted_at).total_seconds())
        attempt = min(
            self._settings.close_reprice_max_attempts,
            int(elapsed // self._settings.close_reprice_seconds),
        )
        if attempt <= 0:
            return broker_order

        mark = finite_float(request.get("mark_price"))
        reason = str(request.get("exit_reason") or "")
        order_id = str(broker_order.get("id") or "")
        if mark is None or mark <= 0 or not order_id:
            return broker_order
        target = _close_limit_price(
            mark,
            reason,
            nudge=self._settings.limit_price_spread_nudge_pct,
            attempt=attempt,
        )
        current = finite_float(broker_order.get("limit_price"))
        if current is not None and current >= target:
            return broker_order

        replacement = await self._mcp.replace_order_by_id(
            order_id,
            limit_price=_decimal_str(target),
        )
        if "error" in replacement:
            logger.warning(
                "close reprice rejected",
                extra={"client_order_id": client_order_id, "error": replacement["error"]},
            )
            return broker_order
        replacement["_active_order_id"] = str(replacement.get("id") or order_id)
        detail["repriced"] = detail.get("repriced", 0) + 1
        return replacement

    async def _reconcile(self, detail: dict[str, Any]) -> None:
        for order in await self._store.orders_in_flight():
            client_order_id = str(order["client_order_id"])
            try:
                broker_order = await self._read_active_order(order, client_order_id)
            except Exception:
                logger.warning(
                    "reconciliation read failed; leaving order status untouched",
                    extra={"client_order_id": client_order_id},
                    exc_info=True,
                )
                continue
            if broker_order is None:
                continue
            broker_order = await self._maybe_reprice_close(order, broker_order, detail)
            new_status = str(broker_order.get("status", order["status"]))
            filled_qty = finite_float(broker_order.get("filled_qty"))
            await self._store.update_order_status(
                client_order_id,
                status=new_status,
                response=broker_order,
                filled_qty=filled_qty,
                filled_avg_price=finite_float(broker_order.get("filled_avg_price")),
            )
            detail["reconciled"] += 1

            # orders_in_flight only ever returns non-terminal orders, so a
            # status that is now terminal is a first-and-only observation of it.
            if new_status.lower() in ("filled", "partially_filled"):
                self._announce(
                    action="close" if client_order_id.startswith("omc-") else "open",
                    underlying=str(broker_order.get("symbol") or "?"),
                    status=new_status.lower(),
                    qty=int(filled_qty) if filled_qty is not None else None,
                    limit_price=finite_float(broker_order.get("filled_avg_price")),
                    client_order_id=client_order_id,
                )

            proposal_id = order.get("proposal_id")
            if proposal_id is not None and _is_terminal_unfilled(new_status, filled_qty):
                await self._mark_broker_rejected(
                    int(proposal_id), client_order_id, broker_order, new_status, detail
                )

    async def _mark_broker_rejected(
        self,
        proposal_id: int,
        client_order_id: str,
        broker_order: dict[str, Any],
        order_status: str,
        detail: dict[str, Any],
    ) -> None:
        """The broker rejected / canceled / expired an order it had accepted.

        Without this the proposal stays ``submitted`` after the broker is done
        with its order, so ``active_proposal_underlyings`` — and therefore the
        candidate gate and the portfolio snapshot — keep the underlying blocked
        indefinitely for an order that will never fill.
        """
        reason = _broker_reason(broker_order) or order_status
        await self._store.update_proposal_status(
            proposal_id, "broker_rejected", error=reason
        )
        await self._store.record_risk_event(
            proposal_id=proposal_id,
            rule="broker_rejected",
            detail={"order_status": order_status, "reason": reason},
        )
        detail["broker_rejected"] = detail.get("broker_rejected", 0) + 1
        logger.warning(
            "execution: order broker-rejected",
            extra={
                "client_order_id": client_order_id,
                "proposal_id": proposal_id,
                "reason": reason,
            },
        )
        self._announce(
            action="close" if client_order_id.startswith("omc-") else "open",
            underlying=str(broker_order.get("symbol") or "?"),
            status="broker_rejected",
            client_order_id=client_order_id,
            error=reason,
        )
