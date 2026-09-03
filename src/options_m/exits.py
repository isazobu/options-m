"""Deterministic exit classification and close-proposal creation."""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from options_m.config import Settings
from options_m.models import StrategyIntent
from options_m.notify import Notifier, NullNotifier, format_decision
from options_m.store import Store

logger = logging.getLogger(__name__)

_VALID_STRATEGIES: frozenset[str] = frozenset(
    StrategyIntent.model_fields["strategy"].annotation.__args__  # type: ignore[union-attr]
)
_CREDIT_STRATEGIES: frozenset[str] = frozenset(
    {"put_credit_spread", "call_credit_spread", "iron_condor", "iron_butterfly"}
)
_DEBIT_STRATEGIES: frozenset[str] = frozenset(
    {"call_debit_spread", "put_debit_spread", "debit_call_spread", "debit_put_spread"}
)
_LONG_STRATEGIES: frozenset[str] = frozenset({"long_call", "long_put", "long_strangle"})
# Opened for a debit (paid premium): closing means selling, which nets a
# credit. Everything else in _VALID_STRATEGIES (credit spreads, iron condors/
# butterflies, covered calls, cash-secured puts) was opened by selling
# premium, so closing it means buying back, a debit.
_RECEIVES_CREDIT_ON_CLOSE: frozenset[str] = _DEBIT_STRATEGIES | _LONG_STRATEGIES


def closing_pays_a_debit(strategy: str) -> bool:
    """Does closing a position of this strategy cost a debit or bring in a credit?

    This decides which direction is *more marketable* for a close's limit
    price: a pay-to-close structure gets more marketable at a higher price
    (willing to pay more); a receive-a-credit-to-close structure gets more
    marketable at a lower price (willing to accept less). Treating every
    close as pay-to-close would push a losing long call/put/strangle's exit
    further from the market on every reprice, exactly when a stop-loss needs
    the opposite.
    """
    return strategy not in _RECEIVES_CREDIT_ON_CLOSE


# Every reason string this module (and _campaign_flatten_active's caller,
# evaluate_close_proposals) can emit that means "get out now", as opposed to
# "profit_target" (a natural exit, no rush). The single source of truth for
# which close reasons ExecutionAgent should price aggressively — duplicating
# this list at the call site risks a new reason silently defaulting to
# non-urgent.
URGENT_CLOSE_REASONS: frozenset[str] = frozenset(
    {"stop_loss", "expiry_hard_stop", "dte_stop", "time_stop", "campaign_flatten"}
)


def exit_thresholds(strategy: str, settings: Settings) -> tuple[float, float]:
    """Return the configured profit target and stop loss for a strategy family."""
    if strategy in _CREDIT_STRATEGIES:
        return settings.exit_credit_profit_target_pct, settings.exit_credit_stop_loss_pct
    if strategy in _DEBIT_STRATEGIES:
        return settings.exit_debit_profit_target_pct, settings.exit_debit_stop_loss_pct
    if strategy in _LONG_STRATEGIES:
        return settings.exit_long_profit_target_pct, settings.exit_long_stop_loss_pct
    return settings.exit_profit_target_pct, settings.exit_stop_loss_pct


def close_reason(
    payload: dict[str, Any], settings: Settings, *, now: datetime | None = None
) -> str | None:
    """Return the highest-priority threshold exit currently tripped."""
    strategy = str(payload.get("strategy") or "")
    min_dte = payload.get("min_dte")
    if isinstance(min_dte, int):
        if min_dte <= settings.exit_dte_hard_floor:
            return "expiry_hard_stop"
        if strategy in _CREDIT_STRATEGIES and min_dte <= settings.exit_dte_short_premium:
            return "dte_stop"

    profit_target, stop_loss = exit_thresholds(strategy, settings)
    pnl_pct = payload.get("pnl_pct")
    if isinstance(pnl_pct, float):
        if pnl_pct <= -stop_loss:
            return "stop_loss"
        if pnl_pct >= profit_target:
            return "profit_target"

    opened_at_raw = payload.get("opened_at")
    if opened_at_raw is not None:
        opened_at: datetime | None = None
        if isinstance(opened_at_raw, datetime):
            opened_at = opened_at_raw
        elif isinstance(opened_at_raw, str):
            with contextlib.suppress(ValueError):
                opened_at = datetime.fromisoformat(opened_at_raw)
        if opened_at is not None:
            utc_opened = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=UTC)
            if ((now or datetime.now(UTC)) - utc_opened).days >= settings.exit_time_stop_days:
                return "time_stop"
    return None


async def _campaign_flatten_active(
    store: Store, settings: Settings, now: datetime
) -> bool:
    start = settings.campaign_start_date
    if start is None:
        return False
    today = store.session_day(now)
    elapsed = await store.sessions_between(start, today)
    # Exactly the final session, not "the campaign has ended by now": elapsed
    # keeps growing past campaign_days on every later day, and a bare `<`
    # check would then flatten every open position at every close forever.
    if elapsed != settings.campaign_days:
        return False
    session_close = await store.last_session_close(now)
    if session_close is None or store.session_day(session_close) != today:
        return False
    flatten_at = session_close - timedelta(
        minutes=settings.campaign_flatten_minutes_before_close
    )
    return flatten_at <= now <= session_close


async def evaluate_close_proposals(
    store: Store,
    settings: Settings,
    *,
    notifier: Notifier | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one close proposal per position whose deterministic exit has fired."""
    positions = await store.get_cached_positions()
    if not positions:
        return {}

    reference = now or datetime.now(UTC)
    flatten = await _campaign_flatten_active(store, settings, reference)
    pending = await store.recent_proposals(limit=50, status="pending")
    pending_close = {
        str(proposal.get("underlying", "")).upper()
        for proposal in pending
        if isinstance(proposal.get("intent"), dict)
        and proposal["intent"].get("action") == "close"
    }
    sink = notifier or NullNotifier()
    close_count = 0
    for row in positions:
        underlying = str(row["symbol"]).upper()
        if underlying in pending_close:
            continue
        payload: dict[str, Any] = row.get("payload") or {}
        reason = "campaign_flatten" if flatten else close_reason(
            payload, settings, now=reference
        )
        if reason is None:
            continue

        pnl_pct = payload.get("pnl_pct")
        thesis = (
            f"{reason}: {pnl_pct:+.1%} unrealized"
            if reason != "campaign_flatten" and isinstance(pnl_pct, float)
            else reason
        )
        raw_strategy = payload.get("strategy") or ""
        strategy = raw_strategy if raw_strategy in _VALID_STRATEGIES else "long_call"
        intent = StrategyIntent(
            action="close",
            strategy=strategy,  # type: ignore[arg-type]
            underlying=underlying,
            target_delta=0.5,
            dte_min=0,
            dte_max=365,
            conviction=1.0,
            thesis=thesis,
            invalidation="",
        )
        await store.save_proposal(
            underlying=underlying,
            intent=intent.model_dump(mode="json"),
            evidence=payload,
            status="pending",
        )
        pending_close.add(underlying)
        close_count += 1
        logger.info(
            "position_manager: close proposal",
            extra={"underlying": underlying, "reason": reason},
        )
        if settings.telegram_notify_decisions:
            sink.notify(
                format_decision(
                    dry_run=settings.dry_run,
                    symbol=underlying,
                    status="close",
                    strategy=strategy,
                    reason=reason,
                    thesis=thesis,
                )
            )
    return {"close_proposals": close_count} if close_count else {}
