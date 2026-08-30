"""StrategistAgent — one LLM call per iteration, zero MCP calls.

Design:

  step():
    1. Market-open check (local market_calendar cache — no MCP call).
    2. Kill switch + LLM-budget check.
    3. Candidate selection: top candidate by score that is not:
         - already open (local positions cache)
         - already in-flight as a pending proposal
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
from options_m.llm import FeatherlessLlm, LlmContractError
from options_m.models import RegimeRead, StrategyIntent
from options_m.prompts import loader as prompt_loader
from options_m.store import Store

logger = logging.getLogger(__name__)

# An evidence row is "stale" if it is older than this multiple of the
# MarketPulseAgent interval. Using 2x gives one missed tick of grace before
# we stop reasoning over data that might be genuinely unavailable.
_EVIDENCE_STALENESS_FACTOR = 2.0


class StrategistAgent:
    """Reads the evidence cache, calls the LLM once, runs the matrix, persists."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        llm: FeatherlessLlm,
    ) -> None:
        self._settings = settings
        self._store = store
        self._llm = llm

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

        # Close evaluation runs unconditionally — not gated by kill switch or
        # LLM budget, because exits must work even when new entries are frozen.
        now = datetime.now(UTC)
        close_detail = await self._evaluate_close_proposals()
        detail.update(close_detail)

        # 1. Market-open check (local cache, no MCP call).
        state = await session.current(self._store, self._settings, now)
        if not state.is_open:
            detail["skipped"] = "market_closed"
            return detail
        detail["session_replayed"] = state.replayed

        # 2. Kill switch + LLM budget.
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
        candidate = await self._pick_candidate(now)
        if candidate is None:
            detail["skipped"] = "no_candidate"
            return detail
        symbol = str(candidate.get("symbol", "")).upper()
        detail["symbol"] = symbol

        # 4. Evidence cache read (local only — no MCP call ever).
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
        user_prompt = prompt_loader.load(
            "strategist",
            symbol=symbol,
            evidence_json=json.dumps(pack, default=str, indent=2),
        )
        t0 = time.monotonic()
        try:
            regime: RegimeRead = await self._llm.complete_json(
                schema=RegimeRead,
                system=(
                    "You are a quantitative options strategist. "
                    "Output only valid JSON as instructed."
                ),
                user=user_prompt,
                max_tokens=self._settings.llm_max_tokens,
                temperature=0.2,
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
            raise
        finally:
            await self._store.record_llm_call(
                agent=self.name,
                model=self._settings.featherless_model_deep,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                ok="status" not in detail or detail.get("status") != "llm_failed",
            )

        # 6. Deterministic matrix decision.
        decision = matrix.decide(pack, regime, settings=self._settings, as_of=now.date())

        # 7. Persist proposal.
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

        return detail

    async def _evaluate_close_proposals(self) -> dict[str, Any]:
        """Check every open position for exit conditions; write a close proposal
        when one is met. Deterministic — no LLM call, no MCP call."""
        positions = await self._store.get_cached_positions()
        if not positions:
            return {}

        # Collect underlyings that already have a pending close proposal so we
        # don't create duplicates within the same cycle.
        pending = await self._store.recent_proposals(limit=50, status="pending")
        pending_close = {
            str(p.get("underlying", "")).upper()
            for p in pending
            if isinstance(p.get("intent"), dict) and p["intent"].get("action") == "close"
        }

        close_count = 0
        for row in positions:
            underlying: str = row["symbol"]
            if underlying in pending_close:
                continue

            payload: dict[str, Any] = row.get("payload") or {}
            reason = _close_reason(payload, self._settings)
            if reason is None:
                continue

            pnl_pct: float | None = payload.get("pnl_pct")
            thesis = (
                f"{reason}: {pnl_pct:+.1%} unrealized" if pnl_pct is not None else reason
            )

            # strategy from enrichment — fall back to a valid placeholder so the
            # StrategyIntent validates; ExecutionAgent reads actual legs from cache.
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
            await self._store.save_proposal(
                underlying=underlying,
                intent=intent.model_dump(mode="json"),
                evidence=payload,
                status="pending",
            )
            pending_close.add(underlying)
            close_count += 1
            logger.info(
                "strategist: close proposal",
                extra={"underlying": underlying, "reason": reason},
            )

        return {"close_proposals": close_count} if close_count else {}

    async def _pick_candidate(self, now: datetime) -> dict[str, Any] | None:
        """Return the top candidate that passes all pre-filters, or None."""
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
        pending = await self._store.recent_proposals(limit=20, status="pending")
        in_flight_symbols = {str(p.get("underlying", "")).upper() for p in pending}

        for candidate in candidates:
            symbol = str(candidate.get("symbol", "")).upper()
            if not symbol:
                continue
            # Skip before reading evidence (cheap: blackout check is in-process).
            if is_earnings_blackout(symbol, today):
                continue
            if symbol in open_positions:
                continue
            if symbol in in_flight_symbols:
                continue
            return candidate
        return None


# All valid literal values for StrategyIntent.strategy (duplicated here to
# avoid importing the Literal type annotation at runtime).
_VALID_STRATEGIES: frozenset[str] = frozenset(
    StrategyIntent.model_fields["strategy"].annotation.__args__  # type: ignore[union-attr]
)


def _close_reason(payload: dict[str, Any], settings: Settings) -> str | None:
    """Return a close reason string if any exit condition is met, else None."""
    pnl_pct = payload.get("pnl_pct")
    if isinstance(pnl_pct, float):
        if pnl_pct >= settings.exit_profit_target_pct:
            return "profit_target"
        if pnl_pct <= -settings.exit_stop_loss_pct:
            return "stop_loss"

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
