"""ReflectionAgent — learns from both closed trades and pipeline decisions.

Two periodic passes, run together each iteration:

  Pass A — Closed trades:
    For each recently filled order that has not yet been reflected on, ask the
    LLM to produce a 1-2 sentence lesson about what happened, what the
    evidence said at entry, and what can be taken forward. Saves the lesson to
    the ``lessons`` table so StrategistAgent's next iteration for the same
    symbol will see it in its evidence pack.

  Pass B — Hold / rejected proposals:
    For each recent ``no_action`` or ``rejected`` proposal that has not yet
    been reflected on, ask the LLM whether the decision to hold or reject was
    vindicated or a miss. A held proposal that the underlying then moved
    strongly on is a miss worth recording; a rejected one that would have been
    catastrophic is a save. Both are learning signals.

Rationale: a system that only learns from realized P/L is blind to the
decisions it never executed — which are often the most instructive ones. A
``hold`` that kept capital safe through a gap-down is a decision worth
remembering, not just ignoring until the next position.

LLM failures in either pass are logged and skipped; they must not stop
PositionManagerAgent or ExecutionAgent from running.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from options_m.config import Settings
from options_m.llm import FeatherlessLlm, LlmError
from options_m.store import Store

logger = logging.getLogger(__name__)

_PASS_B_LOOKBACK_PROPOSALS = 20


class ReflectionAgent:
    """Writes lessons from closed trades and from held/rejected proposals."""

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
        return "reflection"

    @property
    def interval_seconds(self) -> float:
        return self._settings.reflection_interval_seconds

    async def step(self) -> None:
        """One iteration. LLM failures per item are caught and logged."""
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
        pass_a_count = await self._pass_a_closed_trades()
        pass_b_count = await self._pass_b_held_rejected()
        detail = {"pass_a_lessons": pass_a_count, "pass_b_lessons": pass_b_count}
        logger.info("reflection pulse", extra=detail)
        return detail

    async def _pass_a_closed_trades(self) -> int:
        """For each filled order not yet reflected on, generate and save a lesson."""
        if not self._llm.is_enabled:
            return 0

        filled_orders = await self._store.recent_orders(limit=50)
        written = 0
        for order in filled_orders:
            if str(order.get("status", "")) != "filled":
                continue
            order_id = str(order.get("id", ""))
            reflected_key = f"order:{order_id}"

            lesson = await self._generate_trade_lesson(order)
            if lesson is None:
                continue

            proposal_id = order.get("proposal_id")
            underlying = ""
            if proposal_id is not None:
                proposal = await self._store.get_proposal(int(proposal_id))
                if proposal:
                    underlying = str(proposal.get("underlying", ""))

            try:
                await self._store.save_lesson(
                    symbol=underlying or None,
                    lesson=lesson,
                    source="closed_trade",
                    reflected_on=reflected_key,
                )
                written += 1
            except Exception:
                logger.warning(
                    "reflection: could not save pass-A lesson",
                    extra={"reflected_on": reflected_key},
                    exc_info=True,
                )
        return written

    async def _pass_b_held_rejected(self) -> int:
        """For each held/rejected proposal not yet reflected on, generate a lesson."""
        if not self._llm.is_enabled:
            return 0

        no_action = await self._store.recent_proposals(
            limit=_PASS_B_LOOKBACK_PROPOSALS, status="no_action"
        )
        rejected = await self._store.recent_proposals(
            limit=_PASS_B_LOOKBACK_PROPOSALS, status="rejected"
        )
        candidates = no_action + rejected

        written = 0
        for proposal in candidates:
            proposal_id = str(proposal.get("id", ""))
            reflected_key = f"proposal:{proposal_id}"

            full = await self._store.get_proposal(int(proposal_id))
            if full is None:
                continue

            lesson = await self._generate_proposal_lesson(full)
            if lesson is None:
                continue

            underlying = str(full.get("underlying", ""))
            try:
                await self._store.save_lesson(
                    symbol=underlying or None,
                    lesson=lesson,
                    source=(
                        "held_proposal"
                        if full.get("status") == "no_action"
                        else "rejected_proposal"
                    ),
                    reflected_on=reflected_key,
                )
                written += 1
            except Exception:
                logger.warning(
                    "reflection: could not save pass-B lesson",
                    extra={"reflected_on": reflected_key},
                    exc_info=True,
                )
        return written

    async def _generate_trade_lesson(self, order: dict[str, Any]) -> str | None:
        """Ask the LLM for a 1-2 sentence lesson about a filled order."""
        filled_qty = order.get("filled_qty")
        filled_price = order.get("filled_avg_price")
        request = order.get("request") or {}
        try:
            result = await self._llm.chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a trading post-mortem analyst. "
                            "Write one concise lesson (1-2 sentences) from a filled options trade. "
                            "Focus on what the outcome suggests about the setup quality or timing."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Order filled: qty={filled_qty}, avg_price={filled_price}, "
                            f"legs={request.get('legs') or request.get('symbol')}. "
                            "Write the lesson."
                        ),
                    },
                ],
                max_tokens=120,
                temperature=0.3,
            )
        except LlmError as exc:
            logger.warning("reflection: pass-A LLM call failed: %s", exc)
            return None
        return (result.content or "").strip() or None

    async def _generate_proposal_lesson(self, proposal: dict[str, Any]) -> str | None:
        """Ask the LLM for a lesson about a held or rejected proposal."""
        underlying = proposal.get("underlying", "")
        status = proposal.get("status", "")
        error = proposal.get("error", "")
        llm_read = proposal.get("llm_read") or proposal.get("arguments") or {}
        thesis = llm_read.get("thesis", "") if isinstance(llm_read, dict) else ""
        conviction = llm_read.get("conviction", "") if isinstance(llm_read, dict) else ""
        try:
            result = await self._llm.chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a trading post-mortem analyst. "
                            "Write one concise lesson (1-2 sentences) from a proposal that was "
                            f"{'held (no trade taken)' if status == 'no_action' else 'rejected'}. "
                            "Focus on whether the decision looks correct in retrospect."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Underlying: {underlying}\n"
                            f"Status: {status}\n"
                            f"Original thesis: {thesis}\n"
                            f"Conviction: {conviction}\n"
                            f"Rejection reason: {error or 'N/A'}\n"
                            "Write the lesson."
                        ),
                    },
                ],
                max_tokens=120,
                temperature=0.3,
            )
        except LlmError as exc:
            logger.warning("reflection: pass-B LLM call failed: %s", exc)
            return None
        return (result.content or "").strip() or None
