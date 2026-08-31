"""Configuration that, when wrong, fails silently rather than loudly.

The MCP server ignores an unrecognised toolset name instead of rejecting it,
so a typo or a missing entry removes tools with no error anywhere: the client
connects, reports a healthy session, and only fails later at the call site
with "Unknown tool". These tests make that failure loud at build time instead.
"""

from __future__ import annotations

from pathlib import Path

from options_m.config import Settings

# Mirrored from ../alpaca-mcp-server/src/alpaca_mcp_server/toolsets.py, which
# is the source of truth. Anything outside this set is silently dropped by the
# server.
_VALID_TOOLSETS = frozenset(
    {
        "account",
        "trading",
        "watchlists",
        "assets",
        "stock-data",
        "crypto-data",
        "options-data",
        "corporate-actions",
        "news",
        "fixed-income-data",
        "locates",
    }
)

# Toolset -> the tools AlpacaMcp calls from it. Losing any one of these breaks
# an agent at runtime, not at startup.
_REQUIRED_TOOLSETS = {
    "account": "get_account_info / get_account_config",
    "trading": "get_all_positions, get_open_position, get_order_by_client_id, "
    "place_option_order, close_position",
    "assets": "get_option_contracts, get_calendar, get_clock",
    "stock-data": "get_stock_bars, get_stock_snapshot",
    "options-data": "get_option_chain, get_option_snapshot",
    "news": "get_news",
}


def _configured(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def test_the_default_toolsets_are_all_real_toolset_names() -> None:
    """An invented name is not an error — it just quietly removes nothing."""
    unknown = _configured(Settings().alpaca_toolsets) - _VALID_TOOLSETS

    assert not unknown, f"not real toolsets, so silently ignored: {sorted(unknown)}"


def test_the_default_toolsets_cover_every_tool_the_client_calls() -> None:
    configured = _configured(Settings().alpaca_toolsets)

    missing = {name: tools for name, tools in _REQUIRED_TOOLSETS.items() if name not in configured}

    assert not missing, f"these tools would be unreachable: {missing}"


def test_the_deployment_blueprint_matches_the_default() -> None:
    """render.yaml overrides the default, so a stale value there wins.

    This is not hypothetical: the deployed service ran with a toolset list
    that omitted `trading`, so PositionManagerAgent failed every tick with
    "Unknown tool: 'get_all_positions'" while /health stayed green.
    """
    blueprint = Path(__file__).resolve().parent.parent / "render.yaml"
    lines = blueprint.read_text().splitlines()
    values = [
        lines[index + 1].split("value:", 1)[1].strip()
        for index, line in enumerate(lines)
        if "ALPACA_TOOLSETS" in line and index + 1 < len(lines)
    ]

    assert values, "render.yaml no longer sets ALPACA_TOOLSETS"
    for value in values:
        assert _configured(value) == _configured(Settings().alpaca_toolsets)


def test_a_credit_stop_may_exceed_one_hundred_percent() -> None:
    """The exit thresholds are fractions, but not all of them are fractions of
    one. A credit structure's stop is a multiple of the credit received and a
    long option's target is a double, so the bounds that fit the original
    symmetric pair would reject a perfectly ordinary configuration at boot —
    and a Settings() that raises takes the whole process with it.
    """
    settings = Settings(
        exit_credit_stop_loss_pct=2.0,
        exit_long_profit_target_pct=3.0,
        exit_debit_profit_target_pct=1.5,
    )

    assert settings.exit_credit_stop_loss_pct == 2.0
    assert settings.exit_long_profit_target_pct == 3.0
    assert settings.exit_debit_profit_target_pct == 1.5
