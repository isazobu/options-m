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
* **Live trading is unreachable, not merely discouraged.** Alpaca's own
  paper-trading skill is explicit that unattended automation "must assert paper
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
# runs. Alpaca's paper-trading skill requires explicit human confirmation for
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
    """Refuse to start if anything in the environment selects live trading.

    The MCP server picks its trading endpoint from ``ALPACA_PAPER_TRADE`` alone
    — there is no base-URL override for trading — so pinning that one variable
    makes the live endpoint unreachable rather than merely unused.

    Raises:
        LiveTradingRefused: The variable is present and not a paper value.
    """
    raw = os.environ.get("ALPACA_PAPER_TRADE")
    if raw is None or raw.lower() in PAPER_VALUES:
        return
    msg = (
        f"ALPACA_PAPER_TRADE={raw!r} selects LIVE trading. This service is "
        f"paper-only: unset the variable or set it to one of "
        f"{sorted(PAPER_VALUES)}. Note the server does not strip whitespace, "
        f"so a trailing space also selects live."
    )
    raise LiveTradingRefused(msg)


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
                # the server's only switch for the trading endpoint.
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
            LiveTradingRefused: The environment selects live trading.
        """
        # Before anything else, and whether or not credentials are configured:
        # a live-selecting environment is a misconfiguration to fix, not to
        # route around.
        assert_paper_intent()
        if not self._paper:
            msg = (
                "alpaca_paper_trade is False. This service is paper-only and "
                "cannot be configured into live trading."
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
                return structured["result"]
            return structured

        blocks = getattr(result, "content", None) or []
        texts = [block.text for block in blocks if getattr(block, "text", None) is not None]
        if not texts:
            msg = f"tool {tool!r} returned no readable content"
            raise McpProtocolError(msg)

        joined = "".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError as exc:
            msg = f"tool {tool!r} returned content that is not JSON"
            raise McpProtocolError(msg) from exc

    # ---- Typed convenience methods ------------------------------------
    # Added as phases need them, so every broker interaction stays greppable.

    async def get_clock(self) -> dict[str, Any]:
        """Market state. The single source of truth — never a hardcoded calendar."""
        return self._expect_mapping("get_clock", await self.call("get_clock"))

    async def get_account_info(self) -> dict[str, Any]:
        return self._expect_mapping("get_account_info", await self.call("get_account_info"))

    async def get_account_config(self) -> dict[str, Any]:
        """Account configuration, including the options trading level."""
        return self._expect_mapping("get_account_config", await self.call("get_account_config"))

    async def get_all_positions(self) -> list[dict[str, Any]]:
        return self._expect_sequence("get_all_positions", await self.call("get_all_positions"))

    async def get_market_movers(self, top: int = 10) -> dict[str, Any]:
        return self._expect_mapping(
            "get_market_movers", await self.call("get_market_movers", {"top": top})
        )

    async def get_most_active_stocks(self, top: int = 10) -> dict[str, Any]:
        return self._expect_mapping(
            "get_most_active_stocks", await self.call("get_most_active_stocks", {"top": top})
        )

    async def get_news(self, symbols: tuple[str, ...] | list[str], limit: int = 20) -> Any:
        return await self.call("get_news", {"symbols": ",".join(symbols), "limit": limit})

    @staticmethod
    def _expect_mapping(tool: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            msg = f"tool {tool!r} returned {type(payload).__name__}, expected an object"
            raise McpProtocolError(msg)
        return payload

    @staticmethod
    def _expect_sequence(tool: str, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            for key in ("positions", "data", "results"):
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
