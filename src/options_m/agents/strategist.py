"""StrategistAgent — one LLM call per iteration, zero MCP calls.

Design:

  step():
    1. Market-open check (local market_calendar cache — no MCP call).
    2. Kill switch + LLM-budget check.
    3. Candidate selection: top candidate by score that is not:
         - already open (local positions cache)
         - already held by an active proposal (pending / dry-run approved /
           submitted) or a working order for the same underlying
         - within its per-symbol proposal cooldown, or at a per-day cap
         - inside its earnings blackout window (cheap in-process check)
    4. Evidence read: get the pre-computed pack from the local evidence cache
       (written by MarketPulseAgent every 60s). Skip if missing or stale.
    5. LLM call: one ``complete_json(schema=RegimeRead)`` call — the ONLY
       outbound I/O in the whole step.
    6. Matrix decision: deterministic, no LLM.
    7. Persist: a ``pending`` proposal (actionable) or a ``no_action`` proposal
       (matrix returned "hold").

``LlmContractError`` is caught here and recorded as ``llm_failed`` — it does
not propagate to the supervisor and does not stop ExecutionAgent or
PositionManagerAgent from running normally.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from options_m import matrix, session
from options_m.config import Settings
from options_m.earnings import is_earnings_blackout
from options_m.exits import evaluate_close_proposals
from options_m.llm import FeatherlessLlm, LlmContractError
from options_m.models import RegimeRead, StrategyIntent
from options_m.notify import Notifier, NullNotifier, format_decision
from options_m.prompts import loader as prompt_loader
from options_m.store import Store

logger = logging.getLogger(__name__)

# An evidence row is "stale" if it is older than this multiple of the
# MarketPulseAgent interval. Using 2x gives one missed tick of grace before
# we stop reasoning over data that might be genuinely unavailable.
_EVIDENCE_STALENESS_FACTOR = 2.0

# Statuses that are a look, not a trade attempt. Counting them against
# max_proposals_per_day silenced the live strategist after ~2h of holds.
_NON_ACTIONABLE_PROPOSAL_STATUSES: frozenset[str] = frozenset({"no_action", "llm_failed"})


class StrategistAgent:
    """Reads the evidence cache, calls the LLM once, runs the matrix, persists."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        llm: FeatherlessLlm,
        notifier: Notifier | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._llm = llm
        self._notifier = notifier or NullNotifier()

    @property
    def name(self) -> str:
        return "strategist"

    @property
    def interval_seconds(self) -> float:
        return self._settings.strategist_interval_seconds

    async def step(self) -> None:
        """One iteration. Raises on infra failures; catches LlmContractError."""
        started = time.monotonic()
        ok = True
        error: str | None = None
        detail: dict[str, Any] = {}
        try:
            detail = await self._run()
        except LlmContractError as exc:
            # LLM failure means "no trade this tick", not a supervisor-level fault.
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            detail["llm_failed"] = True
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
        detail: dict[str, Any] = {"skipped": None}

        now = datetime.now(UTC)

        # Local cache read — no MCP call.
        state = await session.current(self._store, self._settings, now)
        if not state.is_open:
            detail["skipped"] = "market_closed"
            return detail
        detail["session_replayed"] = state.replayed

        if self._settings.kill_switch or await self._store.is_kill_switch_engaged():
            detail["skipped"] = "kill_switch"
            return detail
        if not self._llm.is_enabled:
            detail["skipped"] = "llm_not_configured"
            return detail
        if self._llm.daily_budget_exhausted:
            detail["skipped"] = "llm_budget_exhausted"
            return detail

        # 3. Candidate selection.
        candidate = await self._pick_candidate(now, detail)
        if candidate is None:
            if detail.get("skipped") is None:
                detail["skipped"] = "no_candidate"
            return detail
        symbol = str(candidate.get("symbol", "")).upper()
        detail["symbol"] = symbol

        # Evidence cache — local only, no MCP call ever.
        stale_threshold = timedelta(
            seconds=self._settings.market_pulse_interval_seconds * _EVIDENCE_STALENESS_FACTOR
        )
        evidence_row = await self._store.get_cached_evidence(symbol)
        if evidence_row is None:
            detail["skipped"] = "no_evidence_cache"
            logger.info("strategist: no cached evidence yet", extra={"symbol": symbol})
            return detail
        updated_at = evidence_row.get("updated_at")
        if updated_at is not None and hasattr(updated_at, "replace"):
            if updated_at.tzinfo is None:
                age = now - updated_at.replace(tzinfo=UTC)
            else:
                age = now - updated_at
            if age > stale_threshold:
                detail["skipped"] = "stale_evidence"
                logger.info(
                    "strategist: evidence is stale",
                    extra={"symbol": symbol, "age_seconds": age.total_seconds()},
                )
                return detail

        pack: dict[str, Any] = evidence_row.get("payload") or {}
        if not pack:
            detail["skipped"] = "empty_evidence"
            return detail

        # 5. One LLM call — the only outbound I/O in step().
        prompt = prompt_loader.load("strategist")
        user_prompt = prompt.render(
            "user",
            symbol=symbol,
            evidence_json=json.dumps(pack, default=str, indent=2),
        )
        t0 = time.monotonic()
        try:
            regime: RegimeRead = await self._llm.complete_json(
                schema=RegimeRead,
                system=prompt.render("system"),
                user=user_prompt,
                max_tokens=self._settings.llm_max_tokens,
                temperature=prompt.params.get("temperature", 0.2),
            )
        except LlmContractError:
            proposal_id = await self._store.save_proposal(
                underlying=symbol,
                intent={},
                evidence=pack,
                status="llm_failed",
            )
            detail["proposal_id"] = proposal_id
            detail["status"] = "llm_failed"
            self._announce(
                symbol=symbol, status="llm_failed", proposal_id=proposal_id,
                reason="model did not return a valid RegimeRead",
            )
            raise
        finally:
            failed = detail.get("status") == "llm_failed"
            await self._store.record_llm_call(
                agent=self.name,
                model=self._settings.featherless_model_deep,
                prompt_tokens=self._llm.last_prompt_tokens,
                completion_tokens=self._llm.last_completion_tokens,
                latency_ms=int((time.monotonic() - t0) * 1000),
                ok=not failed,
                error=self._llm.last_error if failed else None,
            )

        decision = matrix.decide(pack, regime, settings=self._settings, as_of=now.date())

        matrix_payload: dict[str, Any] = {
            "trend_classified": _trend_label(pack),
            "iv_regime_classified": _iv_regime_label(pack),
        }
        if decision == "hold":
            matrix_payload["result"] = "hold"
            proposal_id = await self._store.save_proposal(
                underlying=symbol,
                intent={"action": "hold"},
                evidence=pack,
                status="no_action",
                llm_read=regime.model_dump(),
                matrix=matrix_payload,
            )
            detail["proposal_id"] = proposal_id
            detail["status"] = "no_action"
            logger.info(
                "strategist: hold",
                extra={"symbol": symbol, "conviction": regime.conviction},
            )
            self._announce(
                symbol=symbol,
                status="no_action",
                conviction=regime.conviction,
                thesis=regime.thesis,
                proposal_id=proposal_id,
            )
        elif isinstance(decision, StrategyIntent):
            matrix_payload["result"] = decision.strategy
            proposal_id = await self._store.save_proposal(
                underlying=symbol,
                intent=decision.model_dump(mode="json"),
                evidence=pack,
                status="pending",
                llm_read=regime.model_dump(),
                matrix=matrix_payload,
            )
            detail["proposal_id"] = proposal_id
            detail["status"] = "pending"
            detail["strategy"] = decision.strategy
            logger.info(
                "strategist: proposal created",
                extra={
                    "symbol": symbol,
                    "strategy": decision.strategy,
                    "conviction": regime.conviction,
                },
            )
            self._announce(
                symbol=symbol,
                status="pending",
                strategy=decision.strategy,
                conviction=regime.conviction,
                thesis=regime.thesis,
                invalidation=regime.invalidation,
                proposal_id=proposal_id,
            )

        return detail

    def _announce(self, **fields: Any) -> None:
        """Push one decision to Telegram. Never raises, never blocks."""
        if not self._settings.telegram_notify_decisions:
            return
        self._notifier.notify(format_decision(dry_run=self._settings.dry_run, **fields))

    async def _evaluate_close_proposals(self) -> dict[str, Any]:
        """Compatibility shim; PositionManager owns the live 60-second path."""
        return await evaluate_close_proposals(
            self._store,
            self._settings,
            notifier=self._notifier,
        )

    async def _pick_candidate(
        self, now: datetime, detail: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the top candidate that passes all pre-filters, or None.

        Sets ``detail["skipped"] = "proposal_cap"`` when the global per-day
        proposal ceiling is the reason nothing is picked.
        """
        today = now.date()
        max_age = self._settings.market_pulse_interval_seconds * _EVIDENCE_STALENESS_FACTOR
        candidates = await self._store.top_candidates(
            limit=len(self._settings.universe_symbols),
            max_age_seconds=max_age,
        )
        if not candidates:
            return None

        # Build sets of symbols to exclude.
        open_positions = {row["symbol"] for row in await self._store.get_cached_positions()}
        active_symbols = await self._store.active_proposal_underlyings()

        # A symbol is re-proposed at most once per cooldown window and no more
        # than a hard cap per rolling day. Without this the top-scored name
        # gets a fresh LLM call and a near-duplicate proposal every tick for
        # the whole session — under DRY_RUN it never becomes an open position,
        # so nothing else stops it.
        recent = await self._store.proposals_since(now - timedelta(days=1))
        actionable = [
            row
            for row in recent
            if str(row.get("status") or "") not in _NON_ACTIONABLE_PROPOSAL_STATUSES
        ]
        if len(actionable) >= self._settings.max_proposals_per_day:
            detail["skipped"] = "proposal_cap"
            return None

        cooldown_cutoff = now - timedelta(seconds=self._settings.proposal_cooldown_seconds)
        per_symbol_today: dict[str, int] = {}
        cooling_down: set[str] = set()
        for row in recent:
            sym = str(row.get("underlying", "")).upper()
            status = str(row.get("status") or "")
            if status not in _NON_ACTIONABLE_PROPOSAL_STATUSES:
                per_symbol_today[sym] = per_symbol_today.get(sym, 0) + 1
            ts = row.get("ts")
            if isinstance(ts, datetime):
                ts_aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
                if ts_aware >= cooldown_cutoff:
                    cooling_down.add(sym)
        capped_today = {
            sym
            for sym, count in per_symbol_today.items()
            if count >= self._settings.max_proposals_per_symbol_per_day
        }

        for candidate in candidates:
            symbol = str(candidate.get("symbol", "")).upper()
            if not symbol:
                continue
            # Skip before reading evidence (cheap: blackout check is in-process).
            if is_earnings_blackout(symbol, today):
                continue
            if symbol in open_positions:
                continue
            if symbol in active_symbols:
                continue
            if symbol in cooling_down or symbol in capped_today:
                continue
            return candidate
        return None


# All valid literal values for StrategyIntent.strategy (duplicated here to
# avoid importing the Literal type annotation at runtime).
_VALID_STRATEGIES: frozenset[str] = frozenset(
    StrategyIntent.model_fields["strategy"].annotation.__args__  # type: ignore[union-attr]
)


# Exit families -- the exit-side counterpart of matrix._MATRIX. Both spellings
# of the debit verticals are listed because models.StrategyIntent still accepts
# the legacy names alongside the ones the matrix emits.
_CREDIT_STRATEGIES: frozenset[str] = frozenset(
    {"put_credit_spread", "call_credit_spread", "iron_condor", "iron_butterfly"}
)
_DEBIT_STRATEGIES: frozenset[str] = frozenset(
    {"call_debit_spread", "put_debit_spread", "debit_call_spread", "debit_put_spread"}
)
_LONG_STRATEGIES: frozenset[str] = frozenset({"long_call", "long_put", "long_strangle"})


def _exit_thresholds(strategy: str, settings: Settings) -> tuple[float, float]:
    """``(profit_target, stop_loss)`` for a strategy, both as positive fractions.

    An unknown or unresolved strategy falls back to the single symmetric pair,
    which is what every position was measured against before families existed.
    """
    if strategy in _CREDIT_STRATEGIES:
        return settings.exit_credit_profit_target_pct, settings.exit_credit_stop_loss_pct
    if strategy in _DEBIT_STRATEGIES:
        return settings.exit_debit_profit_target_pct, settings.exit_debit_stop_loss_pct
    if strategy in _LONG_STRATEGIES:
        return settings.exit_long_profit_target_pct, settings.exit_long_stop_loss_pct
    return settings.exit_profit_target_pct, settings.exit_stop_loss_pct


def _close_reason(payload: dict[str, Any], settings: Settings) -> str | None:
    """Return a close reason string if any exit condition is met, else None.

    Checked risk-first, first match wins: expiry, then the short-premium DTE
    stop, then P&L against the position's own family, then the calendar stop.
    A position that trips two rungs at once is reported by the more urgent one.
    """
    strategy = str(payload.get("strategy") or "")

    # 1. Expiry. Applies to every structure -- an ITM option carried into
    # expiration becomes a stock position nobody asked for.
    min_dte = payload.get("min_dte")
    if isinstance(min_dte, int):
        if min_dte <= settings.exit_dte_hard_floor:
            return "expiry_hard_stop"
        # 2. Short premium in its last weeks: gamma, not the thesis, is what
        # moves the P&L from here. Debit and long structures are holding
        # convexity they paid for, so they are left alone.
        if strategy in _CREDIT_STRATEGIES and min_dte <= settings.exit_dte_short_premium:
            return "dte_stop"

    # 3-4. P&L, against this family's thresholds.
    profit_target, stop_loss = _exit_thresholds(strategy, settings)
    pnl_pct = payload.get("pnl_pct")
    if isinstance(pnl_pct, float):
        if pnl_pct <= -stop_loss:
            return "stop_loss"
        if pnl_pct >= profit_target:
            return "profit_target"

    # 5. Calendar backstop, unchanged: capital that has sat in one structure
    # for a month is capital the rest of the pipeline cannot use.
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
            if (datetime.now(UTC) - utc_opened).days >= settings.exit_time_stop_days:
                return "time_stop"

    return None


def _trend_label(pack: dict[str, Any]) -> str:
    trend = pack.get("trend")
    if not isinstance(trend, dict):
        return "unknown"
    sma20 = trend.get("sma_20")
    sma50 = trend.get("sma_50")
    rsi = trend.get("rsi_14")
    if not (
        isinstance(sma20, (int, float))
        and isinstance(sma50, (int, float))
        and isinstance(rsi, (int, float))
    ):
        return "unknown"
    if sma20 > sma50 and rsi > 55:
        return "up"
    if sma20 < sma50 and rsi < 45:
        return "down"
    return "flat"


def _iv_regime_label(pack: dict[str, Any]) -> str:
    options = pack.get("options")
    if not isinstance(options, dict):
        return "unknown"
    iv_atm, rv = options.get("iv_atm"), options.get("realised_vol_20d")
    if isinstance(iv_atm, (int, float)) and isinstance(rv, (int, float)) and rv > 0:
        ratio = iv_atm / rv
        if ratio >= 1.40:
            return "very_expensive"
        if ratio >= 1.10:
            return "expensive"
        return "cheap"
    return "unknown"
