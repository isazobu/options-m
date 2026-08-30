"""The only module that talks to Alpaca.

Alpaca is reached exclusively through its official MCP server, spawned as a
stdio subprocess and held open for the lifetime of the process. Everything else
in the codebase calls typed methods here, which means there is exactly one place
where a broker call can be made, timed out, retried, or refused.

Two invariants matter more than anything else in this module:

* **Nothing is ever fabricated.** A failed or unparseable response raises. A
  numeric field that is missing or not finite reads as ``None`` ("unavailable"),
  never as ``0.0``. A demo that quietly invents an account balance lies to the
  person watching it.
* **Write tools cannot reach Alpaca while ``DRY_RUN`` is true.** The guard lives
  in :meth:`AlpacaMcp.call`, not at the call sites, so a forgotten call site
  cannot place a real order.
* **Live agents is unreachable, not merely discouraged.** Alpaca's own
  paper-agents skill is explicit that unattended automation "must assert paper
  itself, at startup, and exit if it cannot — construct the client with
  ``paper=True`` as a literal rather than reading the endpoint from
  configuration". We do exactly that; see :func:`assert_paper_intent`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from types import TracebackType
from typing import Any, Self

from fastmcp.client import Client, StdioTransport
from fastmcp.exceptions import ToolError

from options_m.config import Settings

logger = logging.getLogger(__name__)

# Tools that change state at the broker. Refused outright while dry_run is on.
# Kept deliberately broad: a tool missing from this set is a tool that can trade.
WRITE_TOOLS = frozenset(
    {
        "place_stock_order",
        "place_crypto_order",
        "place_option_order",
        "replace_order_by_id",
        "cancel_order_by_id",
        "cancel_all_orders",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "do_not_exercise_options_position",
        "update_account_config",
    }
)

# Unscoped or irreversible tools, refused in every mode including live-armed
# runs. Alpaca's paper-agents skill requires explicit human confirmation for
# each of these because they act on holdings the caller may never have created.
# An unattended service has nobody to ask, so the only correct answer is no.
# Scoped exits (close_position) stay available — they are how positions close.
FORBIDDEN_TOOLS = frozenset(
    {
        "cancel_all_orders",
        "close_all_positions",
        "exercise_options_position",
        "do_not_exercise_options_position",
    }
)

# Exactly the values the MCP server accepts as "paper", mirroring server.py:
#     os.environ.get("ALPACA_PAPER_TRADE", "true").lower() in ("true", "1", "yes")
# There is no .strip() there, so "true " with a trailing space selects LIVE, as
# does "paper" or "yes!". Anything outside this set is a live endpoint.
PAPER_VALUES = frozenset({"true", "1", "yes"})

# Every tool result arrives wrapped in a security envelope:
#     {"_alpaca_mcp_security": {"trust": "untrusted_tool_output",
#                               "risk": "api_structured" | "external_text", ...},
#      "data": <the actual payload>}
# The envelope is the server telling us the payload is data, not instructions.
# We unwrap it here and keep the risk classification, which matters from phase 3
# on: anything marked external_text is attacker-influencable prose (news
# headlines, corporate filings) heading for an LLM prompt.
SECURITY_KEY = "_alpaca_mcp_security"
PAYLOAD_KEY = "data"
RISK_EXTERNAL_TEXT = "external_text"

# Corroborating signals for a paper account. Alpaca documents neither as a
# guarantee, so they can support the startup assertion but never replace it:
# live and paper accounts return the same response shape.
_PAPER_ACCOUNT_PREFIX = "PA"
_PAPER_ACCOUNT_STATUS = "PAPER_ONLY"

# The command that starts the server. `python -m alpaca_mcp_server.cli` does NOT
# work: cli.py defines a click command but has no __main__ guard, so -m would
# import it, run nothing, and exit 0 — leaving the client on a dead pipe.
# Importing main() explicitly avoids that and needs no PATH lookup, so it behaves
# identically on Windows and inside the container (venv at /opt/venv).
_SERVER_BOOTSTRAP = "from alpaca_mcp_server.cli import main; main()"


class McpUnavailableError(RuntimeError):
    """Raised when a broker call is attempted without a configured session."""


class DryRunViolation(RuntimeError):
    """Raised when a state-changing tool is called while dry_run is engaged."""


class McpProtocolError(RuntimeError):
    """Raised when a tool result cannot be understood. Never softened."""


class LiveTradingRefused(RuntimeError):
    """Raised at startup when paper mode cannot be proven from the environment.

    Treated as fatal on purpose. Alpaca's guidance is that unproven means live,
    and a live account returns the same response shape as a paper one — so
    nothing later in the run would surface the mistake.
    """


class ForbiddenToolError(RuntimeError):
    """Raised when a permanently disabled tool is called."""


def assert_paper_intent() -> None:
    """Refuse to start if anything in the environment selects live agents.

    The MCP server picks its agents endpoint from ``ALPACA_PAPER_TRADE`` alone
    — there is no base-URL override for agents — so pinning that one variable
    makes the live endpoint unreachable rather than merely unused.

    Raises:
        LiveTradingRefused: The variable is present and not a paper value.
    """
    raw = os.environ.get("ALPACA_PAPER_TRADE")
    if raw is None or raw.lower() in PAPER_VALUES:
        return
    msg = (
        f"ALPACA_PAPER_TRADE={raw!r} selects LIVE agents. This service is "
        f"paper-only: unset the variable or set it to one of "
        f"{sorted(PAPER_VALUES)}. Note the server does not strip whitespace, "
        f"so a trailing space also selects live."
    )
    raise LiveTradingRefused(msg)


def _looks_like_not_found(message: str) -> bool:
    """Whether a ToolError plainly means "no such position/order".

    Deliberately conservative: only a matched substring reads as "not found".
    Anything else propagates, which is the safe-by-construction direction to
    be wrong in — a real outage can never be misread as flat.
    """
    lowered = message.lower()
    return any(phrase in lowered for phrase in ("does not exist", "not found"))


def finite_float(value: object) -> float | None:
    """Coerce a broker numeric to a float, or ``None`` when unusable.

    NaN, infinity, empty strings and non-numeric payloads all read as
    "unavailable". They must never read as a passing value: a NaN high-water
    mark that silently becomes 0.0 disables a drawdown breaker permanently.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


class AlpacaMcp:
    """A long-lived MCP session against the official Alpaca MCP server.

    Mirrors :class:`~options_m.db.Database`: it tolerates being unconfigured so
    the process still boots without credentials, reporting ``is_enabled`` as
    ``False`` instead of crash-looping.
    """

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.alpaca_api_key
        self._secret_key = settings.alpaca_secret_key
        self._paper = settings.alpaca_paper_trade
        self._toolsets = settings.alpaca_toolsets
        self._timeout = settings.mcp_call_timeout_seconds
        self._max_retries = settings.mcp_max_retries
        self._dry_run = settings.dry_run
        self._client: Client[Any] | None = None
        self._tool_names: frozenset[str] = frozenset()
        # None means "not yet checked"; False means the account did not look
        # like a paper account. Neither is treated as proof of paper.
        self._paper_corroborated: bool | None = None
        self._options_trading_level: int | None = None
        # Risk classification reported by the server, per tool, from its last call.
        self._tool_risk: dict[str, str] = {}
        # Serialises reconnects so a burst of failing agents cannot spawn a
        # subprocess each. Agents run concurrently; this is not theoretical.
        self._lock = asyncio.Lock()

    @property
    def is_enabled(self) -> bool:
        """Whether credentials are configured at all."""
        return bool(self._api_key and self._secret_key)

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def tool_names(self) -> frozenset[str]:
        """Tools the server actually exposed, empty until connected."""
        return self._tool_names

    @property
    def paper_corroborated(self) -> bool | None:
        """Whether the account *looks* like a paper account.

        Supporting evidence only. Paper mode is guaranteed by pinning
        ``ALPACA_PAPER_TRADE``; this catches the case where the pin is right but
        the keys belong somewhere unexpected.
        """
        return self._paper_corroborated

    def tool_risk(self, tool: str) -> str | None:
        """Risk class the server reported for ``tool``, if it has been called.

        ``external_text`` means the payload carries text we did not author and
        cannot vouch for. Phase 3 must label it as such inside the evidence pack
        rather than letting it read as trusted context.
        """
        return self._tool_risk.get(tool)

    def is_external_text(self, tool: str) -> bool:
        return self._tool_risk.get(tool) == RISK_EXTERNAL_TEXT

    @property
    def options_trading_level(self) -> int | None:
        """Effective options level, or None when not yet read.

        This is ``options_trading_level`` — the minimum of the approved level
        and the account's configured maximum — which is the one to gate on.
        ``options_approved_level`` can overstate what the account may do.
        """
        return self._options_trading_level

    def _build_transport(self) -> StdioTransport:
        # Inherit the parent environment rather than replacing it. A bare env
        # dict breaks subprocess creation on Windows (SystemRoot) and strips
        # PATH inside the image.
        env = dict(os.environ)
        env.update(
            {
                "ALPACA_API_KEY": self._api_key or "",
                "ALPACA_SECRET_KEY": self._secret_key or "",
                # Pinned literally. Configuration cannot select live: this is
                # the server's only switch for the agents endpoint.
                "ALPACA_PAPER_TRADE": "true",
                "ALPACA_TOOLSETS": self._toolsets,
            }
        )
        return StdioTransport(
            command=sys.executable,
            args=["-c", _SERVER_BOOTSTRAP],
            env=env,
        )

    async def connect(self) -> None:
        """Open the MCP session. No-op when credentials are unset.

        Raises:
            LiveTradingRefused: The environment selects live agents.
        """
        # Before anything else, and whether or not credentials are configured:
        # a live-selecting environment is a misconfiguration to fix, not to
        # route around.
        assert_paper_intent()
        if not self._paper:
            msg = (
                "alpaca_paper_trade is False. This service is paper-only and "
                "cannot be configured into live agents."
            )
            raise LiveTradingRefused(msg)
        if not self.is_enabled:
            logger.warning(
                "ALPACA_API_KEY/ALPACA_SECRET_KEY are not set; "
                "running without a broker session"
            )
            return
        async with self._lock:
            await self._connect_locked()
        # Deliberately outside the lock: this issues a tool call, and a failing
        # call reconnects, which needs the same lock. asyncio.Lock is not
        # reentrant, so doing it inside would deadlock the boot path.
        await self._corroborate_paper_account()

    async def _connect_locked(self) -> None:
        if self._client is not None:
            return
        client: Client[Any] = Client(self._build_transport())
        await client.__aenter__()  # type: ignore[no-untyped-call]
        self._client = client
        # One list_tools at boot turns a toolset misconfiguration into a clear
        # startup log line instead of a confusing failure mid-session.
        try:
            tools = await client.list_tools()
        except Exception:
            logger.exception("failed to list MCP tools")
            raise
        self._tool_names = frozenset(tool.name for tool in tools)
        logger.info(
            "alpaca mcp session ready",
            extra={
                "tool_count": len(self._tool_names),
                "dry_run": self._dry_run,
                "toolsets": self._toolsets,
            },
        )

    async def _corroborate_paper_account(self) -> None:
        """Check the account looks like a paper account. Never fatal.

        A broker outage at boot must not crash-loop the container, so a failed
        read leaves the flag unknown — and unknown blocks write tools, which is
        the fail-closed half of the same rule.
        """
        try:
            account = await self.get_account_info()
        except Exception:
            logger.warning(
                "could not read the account to corroborate paper mode; "
                "write tools stay blocked until it succeeds",
                exc_info=True,
            )
            self._paper_corroborated = None
            return

        number = str(account.get("account_number") or "")
        status = str(account.get("status") or "")
        self._paper_corroborated = (
            number.startswith(_PAPER_ACCOUNT_PREFIX) or status == _PAPER_ACCOUNT_STATUS
        )
        level = account.get("options_trading_level")
        self._options_trading_level = int(level) if isinstance(level, int | str | float) else None

        if not self._paper_corroborated:
            logger.warning(
                "account does not look like a paper account",
                extra={"account_status": status, "account_number_prefix": number[:2]},
            )
        logger.info(
            "paper account corroborated",
            extra={
                "corroborated": self._paper_corroborated,
                "options_trading_level": self._options_trading_level,
            },
        )

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)  # type: ignore[no-untyped-call]
        except Exception:
            logger.warning("error while closing mcp session", exc_info=True)
        else:
            logger.info("alpaca mcp session closed")

    async def _reconnect(self) -> None:
        async with self._lock:
            await self._close_locked()
            await self._connect_locked()

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Invoke one MCP tool and return its decoded JSON payload.

        Raises:
            ForbiddenToolError: ``tool`` is permanently disabled.
            DryRunViolation: ``tool`` changes state and dry_run is engaged.
            LiveTradingRefused: ``tool`` writes and paper mode is uncorroborated.
            McpUnavailableError: No credentials configured.
            McpProtocolError: The result could not be decoded.
        """
        if tool in FORBIDDEN_TOOLS:
            msg = (
                f"{tool!r} is permanently disabled: it is unscoped or "
                f"irreversible, and an unattended service has nobody to confirm it"
            )
            raise ForbiddenToolError(msg)
        if tool in WRITE_TOOLS and self._dry_run:
            msg = f"dry run is engaged; refusing to call write tool {tool!r}"
            raise DryRunViolation(msg)
        if tool in WRITE_TOOLS and not self._paper_corroborated:
            # Unproven reads as live. A live account returns the same shape as a
            # paper one, so nothing downstream would catch the mistake.
            msg = (
                f"refusing {tool!r}: the account has not been corroborated as a "
                f"paper account (corroborated={self._paper_corroborated!r})"
            )
            raise LiveTradingRefused(msg)
        if not self.is_enabled:
            msg = f"no alpaca credentials configured; cannot call {tool!r}"
            raise McpUnavailableError(msg)

        payload = dict(args or {})
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            if self._client is None:
                await self._reconnect()
            client = self._client
            if client is None:  # pragma: no cover - _reconnect raises instead
                msg = "mcp session unavailable"
                raise McpUnavailableError(msg)
            try:
                async with asyncio.timeout(self._timeout):
                    result = await client.call_tool(tool, payload)
            except asyncio.CancelledError:
                raise
            except ToolError as exc:
                # The server is healthy and answered; the tool itself failed
                # (bad credentials, a rejected parameter, a rate limit).
                # Respawning it would fix nothing and costs a subprocess.
                last_error = exc
                logger.warning(
                    "mcp tool call failed",
                    extra={
                        "tool": tool,
                        "attempt": attempt + 1,
                        "attempts_allowed": self._max_retries + 1,
                    },
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                continue
            except Exception as exc:  # transport or timeout
                last_error = exc
                logger.warning(
                    "mcp transport failed",
                    extra={
                        "tool": tool,
                        "attempt": attempt + 1,
                        "attempts_allowed": self._max_retries + 1,
                    },
                    exc_info=True,
                )
                # The session may be poisoned (broken pipe, dead subprocess).
                # Drop it so the next attempt spawns a fresh one.
                await self._reconnect_quietly()
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return self._as_json(tool, result)

        assert last_error is not None  # noqa: S101 - loop always sets it
        raise last_error

    async def _reconnect_quietly(self) -> None:
        try:
            async with self._lock:
                await self._close_locked()
        except Exception:  # pragma: no cover - defensive
            logger.warning("failed to tear down mcp session", exc_info=True)

    def _as_json(self, tool: str, result: Any) -> Any:
        """Decode a tool result into plain Python data.

        Raises rather than substituting a default. A tool whose output we cannot
        read is a tool whose output we must not act on.
        """
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            # FastMCP wraps a non-object payload under "result".
            if set(structured) == {"result"}:
                return self._unwrap(tool, structured["result"])
            return self._unwrap(tool, structured)

        blocks = getattr(result, "content", None) or []
        texts = [block.text for block in blocks if getattr(block, "text", None) is not None]
        if not texts:
            msg = f"tool {tool!r} returned no readable content"
            raise McpProtocolError(msg)

        joined = "".join(texts)
        try:
            decoded = json.loads(joined)
        except json.JSONDecodeError as exc:
            msg = f"tool {tool!r} returned content that is not JSON"
            raise McpProtocolError(msg) from exc
        return self._unwrap(tool, decoded)

    def _unwrap(self, tool: str, payload: Any) -> Any:
        """Strip the server's security envelope and remember its risk class.

        Raises rather than guessing when the envelope is present but malformed:
        reading the wrapper as though it were the payload is how every field
        silently becomes None.
        """
        if not isinstance(payload, dict) or SECURITY_KEY not in payload:
            return payload
        meta = payload.get(SECURITY_KEY)
        if isinstance(meta, dict):
            risk = meta.get("risk")
            if isinstance(risk, str):
                self._tool_risk[tool] = risk
        if PAYLOAD_KEY not in payload:
            msg = f"tool {tool!r} returned a security envelope with no {PAYLOAD_KEY!r} key"
            raise McpProtocolError(msg)
        return payload[PAYLOAD_KEY]

    # ---- Typed convenience methods ------------------------------------
    # Added as phases need them, so every broker interaction stays greppable.

    async def get_clock(self) -> dict[str, Any]:
        """Market state, straight from the broker.

        Kept for an optional startup sanity-check against the local
        ``market_calendar`` cache. Per the 2026-08-29 design change, the normal
        agent loops no longer call this every iteration -- they read the cache
        (populated from :meth:`get_calendar`) instead. Do not add a new call site
        for this outside ``market_pulse.py`` and tests.
        """
        return self._expect_mapping("get_clock", await self.call("get_clock"))

    async def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        """Trading-day calendar entries for ``[start, end]`` (ISO date strings).

        The one live call that populates the local ``market_calendar`` cache.
        ``MarketPulseAgent`` is the only caller.
        """
        return self._expect_sequence(
            "get_calendar", await self.call("get_calendar", {"start": start, "end": end})
        )

    async def get_account_info(self) -> dict[str, Any]:
        return self._expect_mapping("get_account_info", await self.call("get_account_info"))

    async def get_account_config(self) -> dict[str, Any]:
        """Account configuration, including the options agents level."""
        return self._expect_mapping("get_account_config", await self.call("get_account_config"))

    async def get_portfolio_history(
        self,
        *,
        period: str = "1M",
        timeframe: str | None = None,
        extended_hours: bool = False,
    ) -> dict[str, Any]:
        """Account equity/P&L over time, straight from Alpaca.

        The source for the dashboard's headline equity curve — distinct from
        our own ``equity_curve`` table, which only holds what our polling has
        actually observed since this process started.
        """
        args: dict[str, Any] = {"period": period, "extended_hours": extended_hours}
        if timeframe is not None:
            args["timeframe"] = timeframe
        return self._expect_mapping(
            "get_portfolio_history", await self.call("get_portfolio_history", args)
        )

    async def get_all_positions(self) -> list[dict[str, Any]]:
        return self._expect_sequence("get_all_positions", await self.call("get_all_positions"))

    async def get_market_movers(self, top: int = 25, market_type: str = "stocks") -> dict[str, Any]:
        """Top gainers and losers. ``market_type`` is required by the tool."""
        return self._expect_mapping(
            "get_market_movers",
            await self.call("get_market_movers", {"market_type": market_type, "top": top}),
        )

    async def get_most_active_stocks(self, top: int = 25, by: str = "volume") -> dict[str, Any]:
        return self._expect_mapping(
            "get_most_active_stocks",
            await self.call("get_most_active_stocks", {"by": by, "top": top}),
        )

    async def get_news(self, symbols: tuple[str, ...] | list[str], limit: int = 20) -> Any:
        return await self.call("get_news", {"symbols": ",".join(symbols), "limit": limit})

    async def get_option_snapshot(self, symbols: str | list[str]) -> dict[str, dict[str, Any]]:
        """Greeks/IV/latest quote for one or more OCC option symbols, keyed by symbol.

        Callers here typically want several open contracts at once (one row
        per position), unlike :meth:`get_stock_snapshot` which is always
        called with a single symbol and unwraps to one object. Prefer
        :meth:`get_option_chain` when the contracts share an underlying and a
        DTE/strike window — this method exists for the case where the caller
        already has specific OCC symbols (e.g. open positions) in hand.
        """
        joined = symbols if isinstance(symbols, str) else ",".join(symbols)
        payload = self._expect_mapping(
            "get_option_snapshot", await self.call("get_option_snapshot", {"symbols": joined})
        )
        snapshots = payload.get("snapshots", payload)
        if not isinstance(snapshots, dict):
            msg = "get_option_snapshot returned no snapshots object"
            raise McpProtocolError(msg)
        return {key: value for key, value in snapshots.items() if isinstance(value, dict)}

    async def get_open_position(self, symbol: str) -> dict[str, Any] | None:
        """The strict path: a genuine "no position" is ``None``; an outage raises.

        Only a message that plainly says the position does not exist may read
        as flat. Anything else — a timeout, an auth failure, an unfamiliar
        shape — propagates, because a broker outage must never look like flat.
        """
        try:
            payload = await self.call("get_open_position", {"symbol_or_asset_id": symbol})
        except ToolError as exc:
            if _looks_like_not_found(str(exc)):
                return None
            raise
        return self._expect_mapping("get_open_position", payload)

    async def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        """The documented recovery for an ambiguous submission.

        ``None`` only for a genuine "no such order" — see
        :meth:`get_open_position` for why every other failure propagates.
        """
        try:
            payload = await self.call(
                "get_order_by_client_id", {"client_order_id": client_order_id}
            )
        except ToolError as exc:
            if _looks_like_not_found(str(exc)):
                return None
            raise
        return self._expect_mapping("get_order_by_client_id", payload)

    async def close_position(
        self, symbol: str, *, qty: str | None = None, percentage: str | None = None
    ) -> dict[str, Any]:
        """Typed wrapper for the already-whitelisted write tool.

        This is how an open option position closes — market order, respects
        market hours, unknown fill price until it settles. See
        ``agents/position_manager.py`` for the monitoring that follows.
        """
        args: dict[str, Any] = {"symbol_or_asset_id": symbol}
        if qty is not None:
            args["qty"] = qty
        if percentage is not None:
            args["percentage"] = percentage
        return self._expect_mapping("close_position", await self.call("close_position", args))

    async def place_option_order(
        self,
        *,
        qty: str,
        limit_price: str,
        client_order_id: str,
        time_in_force: str = "day",
        order_type: str = "limit",
        symbol: str | None = None,
        side: str | None = None,
        position_intent: str | None = None,
        legs: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Place an options order. Built from the tool's own schema, never the
        REST schema — the override reshapes the body and rejects extra fields.

        Every numeric argument here must already be a string, built from
        ``Decimal`` by the caller, never from a Python float repr.

        Returns the unwrapped payload as-is, *including* a possible
        ``{"error": ...}`` dict: the override validates locally and returns an
        error object rather than raising, so callers must check for an
        ``"error"`` key themselves — a returned dict is not proof of a
        submitted order.
        """
        args: dict[str, Any] = {
            "qty": qty,
            "type": order_type,
            "time_in_force": time_in_force,
            "limit_price": limit_price,
            "client_order_id": client_order_id,
        }
        if legs is not None:
            args["legs"] = legs
        else:
            if symbol is None or side is None:
                msg = "single-leg place_option_order requires symbol and side"
                raise ValueError(msg)
            args["symbol"] = symbol
            args["side"] = side
        if position_intent is not None:
            args["position_intent"] = position_intent
        return self._expect_mapping(
            "place_option_order", await self.call("place_option_order", args)
        )

    # ---- Read tools for the evidence pack (phase 2) ------------------

    async def get_stock_snapshot(self, symbol: str) -> dict[str, Any]:
        """Latest trade, quote, minute bar, daily bar and previous daily bar.

        The tool keys its response by symbol (and, against some server builds,
        nests it under ``snapshots``); this unwraps both shapes so callers get
        the one symbol's snapshot object directly.
        """
        payload = self._expect_mapping(
            "get_stock_snapshot", await self.call("get_stock_snapshot", {"symbols": symbol})
        )
        inner = payload.get("snapshots")
        if isinstance(inner, dict):
            payload = inner
        snapshot = payload.get(symbol) or payload.get(symbol.upper())
        if isinstance(snapshot, dict):
            return snapshot
        # A single-symbol call may already be the snapshot itself.
        if {"latestQuote", "latestTrade", "dailyBar"} & set(payload):
            return payload
        msg = f"get_stock_snapshot returned no snapshot for {symbol!r}"
        raise McpProtocolError(msg)

    async def get_stock_bars(
        self, symbol: str, *, timeframe: str = "1Day", limit: int = 252
    ) -> list[dict[str, Any]]:
        """Historical OHLCV bars for one symbol, oldest first.

        ``days`` is sized generously from ``limit`` so weekends and holidays do
        not starve a daily-bar request; the API still caps the result at
        ``limit``.
        """
        payload = self._expect_mapping(
            "get_stock_bars",
            await self.call(
                "get_stock_bars",
                {
                    "symbols": symbol,
                    "timeframe": timeframe,
                    "limit": limit,
                    "days": int(limit * 1.6) + 15,
                    "sort": "asc",
                },
            ),
        )
        bars = payload.get("bars", payload)
        if isinstance(bars, dict):
            bars = bars.get(symbol) or bars.get(symbol.upper()) or []
        if not isinstance(bars, list):
            msg = "get_stock_bars returned no bar list"
            raise McpProtocolError(msg)
        return [bar for bar in bars if isinstance(bar, dict)]

    async def get_option_chain(
        self,
        underlying: str,
        *,
        option_type: str | None = None,
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 1000,
        max_pages: int = 25,
        feed: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Per-contract snapshots (quote, trade, IV, greeks) for an underlying.

        Returns the ``{occ_symbol: snapshot}`` mapping. The chain is large: a
        single wide-band expiry can exceed ``limit`` on its own, so this follows
        the server's ``next_page_token`` until the chain is exhausted. Hitting
        ``max_pages`` first is logged, not swallowed — a truncated chain here
        skews which expiry the evidence pack reads its ATM IV from.
        """
        args: dict[str, Any] = {"underlying_symbol": underlying, "limit": limit}
        if option_type is not None:
            args["type"] = option_type
        if expiration_gte is not None:
            args["expiration_date_gte"] = expiration_gte
        if expiration_lte is not None:
            args["expiration_date_lte"] = expiration_lte
        if strike_gte is not None:
            args["strike_price_gte"] = strike_gte
        if strike_lte is not None:
            args["strike_price_lte"] = strike_lte
        if feed is not None:
            args["feed"] = feed

        snapshots: dict[str, dict[str, Any]] = {}
        page_token: str | None = None
        for _page in range(max_pages):
            if page_token:
                args["page_token"] = page_token
            payload = self._expect_mapping(
                "get_option_chain", await self.call("get_option_chain", args)
            )
            chunk = payload.get("snapshots", payload)
            if not isinstance(chunk, dict):
                msg = "get_option_chain returned no snapshots object"
                raise McpProtocolError(msg)
            snapshots.update(
                {key: value for key, value in chunk.items() if isinstance(value, dict)}
            )
            # When the server hands back the bare OCC mapping (no envelope), it
            # carries no token and there is nothing more to page.
            page_token = payload.get("next_page_token") if payload is not chunk else None
            if not page_token:
                break
        else:
            logger.warning(
                "get_option_chain stopped at the %d-page ceiling for %s; "
                "the chain may be truncated",
                max_pages,
                underlying,
            )
        return snapshots

    async def get_option_contracts(
        self,
        underlying: str,
        *,
        option_type: str | None = None,
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 1000,
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        """Reference data for an underlying's contracts — carries open interest,
        which the market-data chain does not.

        Follows ``next_page_token`` to the end of the list so open interest is
        still attached for strikes that fall past the first page.
        """
        args: dict[str, Any] = {
            "underlying_symbols": underlying,
            "limit": limit,
            "status": "active",
        }
        if option_type is not None:
            args["type"] = option_type
        if expiration_gte is not None:
            args["expiration_date_gte"] = expiration_gte
        if expiration_lte is not None:
            args["expiration_date_lte"] = expiration_lte
        if strike_gte is not None:
            args["strike_price_gte"] = strike_gte
        if strike_lte is not None:
            args["strike_price_lte"] = strike_lte

        contracts: list[dict[str, Any]] = []
        page_token: str | None = None
        for _page in range(max_pages):
            if page_token:
                args["page_token"] = page_token
            payload = await self.call("get_option_contracts", args)
            if isinstance(payload, dict):
                chunk = payload.get("option_contracts", payload.get("data"))
                page_token = payload.get("next_page_token")
            else:
                chunk = payload
                page_token = None
            if not isinstance(chunk, list):
                msg = "get_option_contracts returned no contract list"
                raise McpProtocolError(msg)
            contracts.extend(item for item in chunk if isinstance(item, dict))
            if not page_token:
                break
        else:
            logger.warning(
                "get_option_contracts stopped at the %d-page ceiling for %s",
                max_pages,
                underlying,
            )
        return contracts

    @staticmethod
    def _expect_mapping(tool: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            msg = f"tool {tool!r} returned {type(payload).__name__}, expected an object"
            raise McpProtocolError(msg)
        return payload

    @staticmethod
    def _expect_sequence(tool: str, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            # FastMCP nests a bare list under "result"; the Alpaca tools use
            # their own plural keys. Check every shape we have actually seen.
            for key in ("result", "positions", "data", "results"):
                nested = payload.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
        if not isinstance(payload, list):
            msg = f"tool {tool!r} returned {type(payload).__name__}, expected a list"
            raise McpProtocolError(msg)
        return [item for item in payload if isinstance(item, dict)]

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
