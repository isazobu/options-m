"""Tests for the deterministic evidence collector.

The collector is the data-gathering link in the agent chain: it must produce a
faithful pack or an honest gap, never a plausible invention. The assertions
here lean on that — a failed sub-fetch has to surface as ``NO_DATA_AVAILABLE``,
and the pack must still come back whole.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from options_m.config import Settings
from options_m.db import Database
from options_m.evidence.evidence import MISSING, EvidenceCollector
from options_m.store import Store

_SPOT = 450.0
_TODAY = datetime.now(UTC).date()
_NEAR = _TODAY + timedelta(days=14)
_FAR = _TODAY + timedelta(days=35)
_STRIKES = [440.0, 445.0, 450.0, 455.0, 460.0]


def _occ(root: str, expiry: date, right: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def _snapshot() -> dict[str, Any]:
    return {
        "latestQuote": {"bp": 449.90, "ap": 450.10, "bs": 4, "as": 6, "t": "2026-08-29T15:30:00Z"},
        "latestTrade": {"p": _SPOT},
        "dailyBar": {"o": 448.0, "h": 452.0, "l": 447.0, "c": _SPOT, "v": 1_000_000, "vw": 449.5},
        "prevDailyBar": {"c": 445.0},
    }


def _bars(n: int = 60) -> list[dict[str, Any]]:
    base = 400.0
    return [
        {
            "t": f"2026-0{1 + i // 28}-{1 + i % 28:02d}T00:00:00Z",
            "o": base + i,
            "h": base + i + 2.0,
            "l": base + i - 2.0,
            "c": base + i,
            "v": 10_000 + i,
            "vw": base + i,
        }
        for i in range(n)
    ]


def _chain() -> dict[str, dict[str, Any]]:
    chain: dict[str, dict[str, Any]] = {}
    for expiry, iv_call, iv_put in ((_NEAR, 0.20, 0.22), (_FAR, 0.25, 0.27)):
        for strike in _STRIKES:
            for right, iv in (("C", iv_call), ("P", iv_put)):
                sym = _occ("SPY", expiry, right, strike)
                chain[sym] = {
                    "latestQuote": {"bp": 2.00, "ap": 2.20, "bs": 10, "as": 12},
                    "latestTrade": {"p": 2.10},
                    "impliedVolatility": iv,
                    "greeks": {
                        "delta": 0.5 if right == "C" else -0.5,
                        "gamma": 0.02,
                        "theta": -0.05,
                        "vega": 0.10,
                        "rho": 0.01,
                    },
                }
    return chain


def _contracts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for expiry in (_NEAR, _FAR):
        for strike in _STRIKES:
            for right in ("C", "P"):
                out.append(
                    {
                        "symbol": _occ("SPY", expiry, right, strike),
                        "strike_price": str(strike),
                        "expiration_date": expiry.isoformat(),
                        "type": "call" if right == "C" else "put",
                        "open_interest": "500",
                    }
                )
    return out


class _FakeMcp:
    """Duck-typed stand-in for AlpacaMcp's read surface."""

    def __init__(self, **fail: bool) -> None:
        self._fail = fail
        self.snapshot = _snapshot()
        self.bars = _bars()
        self.chain = _chain()
        self.contracts = _contracts()
        self.positions: list[dict[str, Any]] = []
        self.news: Any = {
            "news": [
                {
                    "headline": "H" * 400,
                    "summary": "S" * 500,
                    "source": "benzinga",
                    "created_at": "2026-08-29T12:00:00Z",
                }
            ]
        }

    def _guard(self, name: str) -> None:
        if self._fail.get(name):
            msg = f"{name} is down"
            raise RuntimeError(msg)

    async def get_stock_snapshot(self, symbol: str) -> dict[str, Any]:
        self._guard("snapshot")
        return self.snapshot

    async def get_stock_bars(self, symbol: str, **_: Any) -> list[dict[str, Any]]:
        self._guard("bars")
        return self.bars

    async def get_option_chain(self, symbol: str, **_: Any) -> dict[str, dict[str, Any]]:
        self._guard("chain")
        return self.chain

    async def get_option_contracts(self, symbol: str, **_: Any) -> list[dict[str, Any]]:
        self._guard("contracts")
        return self.contracts

    async def get_all_positions(self) -> list[dict[str, Any]]:
        self._guard("positions")
        return self.positions

    async def get_news(self, symbols: Any, limit: int = 5) -> Any:
        self._guard("news")
        return self.news


def _collector(mcp: Any) -> tuple[EvidenceCollector, Store]:
    settings = Settings(database_url=None)
    store = Store(Database(settings))
    return EvidenceCollector(settings, mcp, store), store


async def test_pack_carries_the_missing_data_note_and_the_core_sections() -> None:
    collector, _store = _collector(_FakeMcp())

    pack = await collector.collect("spy")

    assert pack["symbol"] == "SPY"
    assert "Do not estimate" in pack["note"]
    assert pack["as_of"].endswith("Z")
    assert set(pack) >= {"spot", "trend", "options", "position", "untrusted_news", "lessons"}


async def test_underlying_quote_fields_are_surfaced() -> None:
    collector, _store = _collector(_FakeMcp())

    spot = (await collector.collect("SPY"))["spot"]

    assert spot["bid"] == 449.90
    assert spot["ask"] == 450.10
    assert spot["bid_size"] == 4
    assert spot["mid"] == 450.0
    assert spot["spread"] == pytest.approx(0.2, abs=1e-6)
    assert spot["prev_close"] == 445.0


async def test_a_failed_snapshot_is_no_data_not_a_fabricated_price() -> None:
    collector, _store = _collector(_FakeMcp(snapshot=True))

    pack = await collector.collect("SPY")

    assert pack["spot"] == MISSING
    # The rest of the pack still comes back.
    assert pack["options"] != MISSING


async def test_trend_block_is_computed_from_the_bars() -> None:
    collector, _store = _collector(_FakeMcp())

    trend = (await collector.collect("SPY"))["trend"]

    assert isinstance(trend["sma_20"], float)
    assert isinstance(trend["rsi_14"], float)
    assert isinstance(trend["atr_14"], float)
    assert trend["bars_used"] == 60


async def test_trend_is_no_data_when_bars_fail() -> None:
    collector, _store = _collector(_FakeMcp(bars=True))

    assert (await collector.collect("SPY"))["trend"] == MISSING


async def test_options_block_summarises_the_chain_with_real_symbols() -> None:
    collector, _store = _collector(_FakeMcp())

    options = (await collector.collect("SPY"))["options"]

    assert isinstance(options["iv_atm"], float)
    # ATM contracts are taken from the chain, never constructed.
    assert options["atm_call"]["symbol"] in _chain()
    assert options["atm_put"]["symbol"] in _chain()
    assert options["atm_call"]["strike"] == 450.0
    # Put IV (0.22) sits above call IV (0.20) in the fixture.
    assert options["put_call_skew"] == pytest.approx(0.02, abs=1e-6)
    # Far IV (0.25) above near IV (0.20).
    assert options["term_structure"] == pytest.approx(0.05, abs=1e-6)
    assert options["total_open_interest"] == 500 * len(_chain())


async def test_options_block_carries_realised_vol_and_the_iv_minus_rv_premium() -> None:
    collector, _store = _collector(_FakeMcp())

    pack = await collector.collect("SPY")
    trend_rv = pack["trend"]["realised_vol_20d"]
    options = pack["options"]

    assert options["realised_vol_20d"] == trend_rv
    assert options["iv_minus_rv"] == pytest.approx(options["iv_atm"] - trend_rv, abs=1e-9)


async def test_iv_minus_rv_is_missing_when_the_bars_fail() -> None:
    collector, _store = _collector(_FakeMcp(bars=True))

    options = (await collector.collect("SPY"))["options"]

    assert options["realised_vol_20d"] == MISSING
    assert options["iv_minus_rv"] == MISSING


async def test_open_interest_is_missing_when_the_contract_fetch_fails() -> None:
    collector, _store = _collector(_FakeMcp(contracts=True))

    options = (await collector.collect("SPY"))["options"]

    assert options["atm_call"]["open_interest"] == MISSING
    assert options["total_open_interest"] == MISSING


async def test_iv_rank_is_missing_on_the_first_pull_then_populates() -> None:
    collector, _store = _collector(_FakeMcp())

    first = (await collector.collect("SPY"))["options"]
    assert first["iv_rank"] == MISSING  # only one reading exists

    second = (await collector.collect("SPY"))["options"]
    assert isinstance(second["iv_rank"], float)


async def test_a_flat_book_reports_none_while_an_unread_book_reports_missing() -> None:
    collector, _store = _collector(_FakeMcp())
    assert (await collector.collect("SPY"))["position"] is None

    collector, _store = _collector(_FakeMcp(positions=True))
    assert (await collector.collect("SPY"))["position"] == MISSING


async def test_an_existing_option_leg_is_attributed_to_the_underlying() -> None:
    mcp = _FakeMcp()
    mcp.positions = [
        {
            "symbol": _occ("SPY", _NEAR, "C", 450.0),
            "side": "long",
            "qty": "1",
            "avg_entry_price": "2.05",
            "market_value": "210.0",
            "unrealized_pl": "5.0",
            "unrealized_plpc": "0.024",
        }
    ]
    collector, _store = _collector(mcp)

    position = (await collector.collect("SPY"))["position"]

    assert isinstance(position, list) and position[0]["kind"] == "option"
    assert position[0]["option_type"] == "call"
    assert position[0]["strike"] == 450.0


async def test_news_is_truncated_and_kept_under_the_untrusted_key() -> None:
    collector, _store = _collector(_FakeMcp())

    news = (await collector.collect("SPY"))["untrusted_news"]

    assert len(news) == 1
    assert len(news[0]["headline"]) <= 200
    assert news[0]["headline"].endswith("…")
    assert news[0]["source"] == "benzinga"


async def test_collect_returns_a_whole_pack_even_when_every_fetch_fails() -> None:
    mcp = _FakeMcp(
        snapshot=True, bars=True, chain=True, contracts=True, positions=True, news=True
    )
    collector, _store = _collector(mcp)

    pack = await collector.collect("SPY")

    assert pack["spot"] == MISSING
    assert pack["trend"] == MISSING
    assert pack["options"] == MISSING
    assert pack["position"] == MISSING
    assert pack["untrusted_news"] == MISSING
    assert pack["lessons"] == []
    assert "Do not estimate" in pack["note"]
