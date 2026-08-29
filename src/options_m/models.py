"""Domain types every later module speaks.

The core safety principle lives here: an LLM (or, in this phase, a
hand-written proposal) only ever states a :class:`StrategyIntent` — direction,
target delta, a DTE window, a structure. It never names a contract. A
deterministic builder (:mod:`options_m.strategy_builder`) turns that intent
into real :class:`Leg` objects pulled from the live chain, and the result is
an :class:`OrderPlan` only if every field, including a finite ``max_loss``,
can be computed for real. Anything less comes back as a :class:`Rejection`.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrategyIntent(BaseModel):
    """What the caller is allowed to decide. It never names a contract."""

    model_config = ConfigDict(strict=True, frozen=True)

    action: Literal["open", "hold", "close"]
    strategy: Literal[
        "long_call",
        "long_put",
        "debit_call_spread",
        "debit_put_spread",
        "covered_call",
        "cash_secured_put",
    ]
    underlying: str
    target_delta: float = Field(gt=0.0, le=1.0)
    spread_width: float | None = Field(default=None, gt=0.0)
    dte_min: int = Field(ge=0)
    dte_max: int = Field(ge=0)
    conviction: float = Field(ge=0.0, le=1.0)
    thesis: str
    invalidation: str

    @field_validator("underlying")
    @classmethod
    def _upper(cls, value: str) -> str:
        upper = value.strip().upper()
        if not upper.isalnum():
            msg = f"underlying must be a bare ticker, got {value!r}"
            raise ValueError(msg)
        return upper

    @field_validator("dte_max")
    @classmethod
    def _dte_order(cls, value: int, info: Any) -> int:
        dte_min = info.data.get("dte_min")
        if dte_min is not None and value < dte_min:
            msg = f"dte_max ({value}) must be >= dte_min ({dte_min})"
            raise ValueError(msg)
        return value


class Leg(BaseModel):
    """One real, live contract selected from the chain — never constructed."""

    model_config = ConfigDict(strict=True, frozen=True)

    symbol: str
    side: Literal["buy", "sell"]
    ratio: int = Field(default=1, ge=1, le=4)
    strike: float = Field(gt=0.0)
    expiry: date
    option_type: Literal["call", "put"]
    delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    delta_source: Literal["chain", "black_scholes"] | None = None
    bid: float | None = Field(default=None, ge=0.0)
    ask: float | None = Field(default=None, ge=0.0)
    open_interest: int | None = Field(default=None, ge=0)


class OrderPlan(BaseModel):
    """A fully-priced, risk-computable order. Never speculative."""

    model_config = ConfigDict(strict=True, frozen=True)

    proposal_id: int
    underlying: str
    strategy: str
    legs: list[Leg] = Field(min_length=1, max_length=4)
    qty: int = Field(gt=0)
    limit_price: float
    max_loss: float
    max_profit: float | None = None
    breakeven: float | None = None
    client_order_id: str

    @field_validator("max_loss")
    @classmethod
    def _max_loss_finite(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            msg = "max_loss must be a finite, positive number — undefined risk is never a plan"
            raise ValueError(msg)
        return value

    @field_validator("client_order_id")
    @classmethod
    def _client_order_id_shape(cls, value: str, info: Any) -> str:
        proposal_id = info.data.get("proposal_id")
        expected = f"om-{proposal_id}"
        if proposal_id is not None and value != expected:
            msg = f"client_order_id must be {expected!r}, got {value!r}"
            raise ValueError(msg)
        return value


class Rejection(BaseModel):
    """Why the builder or risk engine refused to produce/approve a plan."""

    model_config = ConfigDict(strict=True, frozen=True)

    proposal_id: int
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)
