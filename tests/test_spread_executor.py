"""Tests for SpreadExecutor -- atomic multi-leg spread execution.

Covers: successful 2-leg spread, auto-unwind on leg 2 failure,
all legs succeed, execute_pair_spread convenience method.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.spread_executor import (
    SpreadExecutor,
    SpreadLeg,
    SpreadOrderResult,
)
from oracle3.trader.types import OrderFailureReason, PlaceOrderResult, TradeSide


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticker(
    symbol: str, name: str = '', no_token_id: str = ''
) -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol=symbol,
        name=name or symbol,
        token_id=symbol,
        market_id=symbol,
        event_id='',
        no_token_id=no_token_id,
    )


def _make_trader(
    tickers_with_books: dict[PolyMarketTicker, tuple[Decimal, Decimal]]
    | None = None,
    cash: Decimal = Decimal('10000'),
) -> PaperTrader:
    """Create a PaperTrader with 100% fill, no commission, no risk checks."""
    md = MarketDataManager()
    pm = PositionManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=cash,
            average_cost=Decimal('1'),
            realized_pnl=Decimal('0'),
        )
    )
    if tickers_with_books:
        for ticker, (bid, ask) in tickers_with_books.items():
            ob = OrderBook()
            ob.update(
                asks=[Level(price=ask, size=Decimal('1000'))],
                bids=[Level(price=bid, size=Decimal('1000'))],
            )
            md.update_order_book(ticker, ob)

    return PaperTrader(
        market_data=md,
        risk_manager=NoRiskManager(),
        position_manager=pm,
        min_fill_rate=Decimal('1'),
        max_fill_rate=Decimal('1'),
        commission_rate=Decimal('0'),
    )


def _seed_position(
    trader: PaperTrader,
    ticker: PolyMarketTicker,
    quantity: Decimal,
    avg_cost: Decimal,
) -> None:
    """Seed a position for sell-side testing."""
    trader.position_manager.update_position(
        Position(
            ticker=ticker,
            quantity=quantity,
            average_cost=avg_cost,
            realized_pnl=Decimal('0'),
        )
    )


# ===================================================================
# SpreadLeg / SpreadOrderResult dataclass tests
# ===================================================================


class TestSpreadDataClasses:

    def test_spread_leg_creation(self):
        t = _make_ticker('A')
        leg = SpreadLeg(
            side=TradeSide.BUY,
            ticker=t,
            price=Decimal('0.50'),
            quantity=Decimal('10'),
        )
        assert leg.side == TradeSide.BUY
        assert leg.ticker == t
        assert leg.price == Decimal('0.50')
        assert leg.quantity == Decimal('10')

    def test_spread_order_result_success(self):
        r = SpreadOrderResult(success=True, leg_results=[])
        assert r.success is True
        assert r.hedged is False
        assert r.failure_reason == ''

    def test_spread_order_result_all_filled_empty(self):
        r = SpreadOrderResult(success=True, leg_results=[])
        assert r.all_filled is True  # vacuously true

    def test_spread_order_result_failure(self):
        r = SpreadOrderResult(
            success=False,
            failure_reason='leg 2 failed: insufficient_cash',
        )
        assert r.success is False
        assert 'leg 2' in r.failure_reason


# ===================================================================
# SpreadExecutor: empty legs
# ===================================================================


class TestSpreadExecutorEmpty:

    async def test_empty_legs_returns_failure(self):
        trader = _make_trader()
        executor = SpreadExecutor(trader)
        result = await executor.execute_spread([])
        assert result.success is False
        assert 'no legs' in result.failure_reason


# ===================================================================
# SpreadExecutor: successful 2-leg spread
# ===================================================================


class TestSpreadExecutorSuccess:

    async def test_two_leg_buy_buy(self):
        """Both legs are BUY orders -- both should fill."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        trader = _make_trader(
            {
                ticker_a: (Decimal('0.45'), Decimal('0.55')),
                ticker_b: (Decimal('0.35'), Decimal('0.45')),
            }
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker_a, Decimal('0.55'), Decimal('10')
            ),
            SpreadLeg(
                TradeSide.BUY, ticker_b, Decimal('0.45'), Decimal('10')
            ),
        ]

        result = await executor.execute_spread(legs)
        assert result.success is True
        assert len(result.leg_results) == 2
        assert result.all_filled is True
        assert result.hedged is False

    async def test_two_leg_buy_sell(self):
        """Buy A, sell B (requires position in B)."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        trader = _make_trader(
            {
                ticker_a: (Decimal('0.45'), Decimal('0.55')),
                ticker_b: (Decimal('0.45'), Decimal('0.55')),
            }
        )
        # Seed position in B so we can sell it
        _seed_position(trader, ticker_b, Decimal('20'), Decimal('0.40'))

        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker_a, Decimal('0.55'), Decimal('10')
            ),
            SpreadLeg(
                TradeSide.SELL, ticker_b, Decimal('0.45'), Decimal('10')
            ),
        ]

        result = await executor.execute_spread(legs)
        assert result.success is True
        assert len(result.leg_results) == 2
        assert result.all_filled is True

    async def test_three_leg_spread(self):
        """All 3 legs succeed."""
        t1 = _make_ticker('T1')
        t2 = _make_ticker('T2')
        t3 = _make_ticker('T3')

        trader = _make_trader(
            {
                t1: (Decimal('0.25'), Decimal('0.35')),
                t2: (Decimal('0.25'), Decimal('0.35')),
                t3: (Decimal('0.25'), Decimal('0.35')),
            }
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(TradeSide.BUY, t1, Decimal('0.35'), Decimal('5')),
            SpreadLeg(TradeSide.BUY, t2, Decimal('0.35'), Decimal('5')),
            SpreadLeg(TradeSide.BUY, t3, Decimal('0.35'), Decimal('5')),
        ]

        result = await executor.execute_spread(legs)
        assert result.success is True
        assert len(result.leg_results) == 3
        assert result.all_filled is True


# ===================================================================
# SpreadExecutor: auto-unwind on leg 2 failure
# ===================================================================


class TestSpreadExecutorUnwind:

    async def test_unwind_on_second_leg_failure(self):
        """Leg 1 fills, leg 2 fails (insufficient cash) => unwind leg 1."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        # Very little cash -- enough for leg 1, not leg 2
        trader = _make_trader(
            {
                ticker_a: (Decimal('0.45'), Decimal('0.55')),
                ticker_b: (Decimal('0.85'), Decimal('0.95')),
            },
            cash=Decimal('6'),
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker_a, Decimal('0.55'), Decimal('10')
            ),
            SpreadLeg(
                TradeSide.BUY, ticker_b, Decimal('0.95'), Decimal('10')
            ),
        ]

        result = await executor.execute_spread(legs)
        assert result.success is False
        assert result.hedged is True
        assert 'leg 2' in result.failure_reason
        assert result.unwind_details != ''

    async def test_no_unwind_when_disabled(self):
        """When unwind_on_partial=False, no unwind occurs."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        trader = _make_trader(
            {
                ticker_a: (Decimal('0.45'), Decimal('0.55')),
                ticker_b: (Decimal('0.85'), Decimal('0.95')),
            },
            cash=Decimal('6'),
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker_a, Decimal('0.55'), Decimal('10')
            ),
            SpreadLeg(
                TradeSide.BUY, ticker_b, Decimal('0.95'), Decimal('10')
            ),
        ]

        result = await executor.execute_spread(
            legs, unwind_on_partial=False
        )
        assert result.success is False
        assert result.hedged is False
        assert result.unwind_details == ''

    async def test_first_leg_failure_no_unwind_needed(self):
        """When the very first leg fails, no unwind is needed."""
        ticker_a = _make_ticker('A')

        trader = _make_trader(
            {ticker_a: (Decimal('0.45'), Decimal('0.55'))},
            cash=Decimal('0'),  # no cash at all
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker_a, Decimal('0.55'), Decimal('10')
            ),
        ]

        result = await executor.execute_spread(legs)
        assert result.success is False
        assert result.hedged is False
        assert len(result.leg_results) == 1

    async def test_unwind_on_third_leg_failure(self):
        """3 legs: legs 1&2 fill, leg 3 fails => unwind legs 1&2."""
        t1 = _make_ticker('T1')
        t2 = _make_ticker('T2')
        t3 = _make_ticker('T3')

        # Enough cash for 2 legs but not 3
        trader = _make_trader(
            {
                t1: (Decimal('0.25'), Decimal('0.35')),
                t2: (Decimal('0.25'), Decimal('0.35')),
                t3: (Decimal('0.85'), Decimal('0.95')),
            },
            cash=Decimal('8'),
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(TradeSide.BUY, t1, Decimal('0.35'), Decimal('10')),
            SpreadLeg(TradeSide.BUY, t2, Decimal('0.35'), Decimal('10')),
            SpreadLeg(TradeSide.BUY, t3, Decimal('0.95'), Decimal('10')),
        ]

        result = await executor.execute_spread(legs)
        assert result.success is False
        assert result.hedged is True
        assert 'leg 3' in result.failure_reason


# ===================================================================
# SpreadExecutor: execute_pair_spread convenience
# ===================================================================


class TestSpreadExecutorPairSpread:

    async def test_pair_spread_success(self):
        """Buy A, sell B using best ask/bid automatically."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        trader = _make_trader(
            {
                ticker_a: (Decimal('0.45'), Decimal('0.55')),
                ticker_b: (Decimal('0.45'), Decimal('0.55')),
            }
        )
        # Need a position in B to sell
        _seed_position(trader, ticker_b, Decimal('20'), Decimal('0.40'))

        executor = SpreadExecutor(trader)
        result = await executor.execute_pair_spread(
            buy_ticker=ticker_a,
            sell_ticker=ticker_b,
            quantity=Decimal('10'),
        )
        assert result.success is True
        assert len(result.leg_results) == 2

    async def test_pair_spread_no_ask(self):
        """When no ask is available for buy_ticker, fail gracefully."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        trader = _make_trader(
            {
                # No order book for ticker_a
                ticker_b: (Decimal('0.45'), Decimal('0.55')),
            }
        )

        executor = SpreadExecutor(trader)
        result = await executor.execute_pair_spread(
            buy_ticker=ticker_a,
            sell_ticker=ticker_b,
            quantity=Decimal('10'),
        )
        assert result.success is False
        assert 'no ask' in result.failure_reason

    async def test_pair_spread_no_bid(self):
        """When no bid is available for sell_ticker, fail gracefully."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        trader = _make_trader(
            {ticker_a: (Decimal('0.45'), Decimal('0.55'))}
        )

        executor = SpreadExecutor(trader)
        result = await executor.execute_pair_spread(
            buy_ticker=ticker_a,
            sell_ticker=ticker_b,
            quantity=Decimal('10'),
        )
        assert result.success is False
        assert 'no bid' in result.failure_reason


# ===================================================================
# SpreadExecutor: all_filled property
# ===================================================================


class TestSpreadOrderResultAllFilled:

    async def test_all_filled_true_on_full_fills(self):
        ticker_a = _make_ticker('A')
        trader = _make_trader(
            {ticker_a: (Decimal('0.45'), Decimal('0.55'))}
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker_a, Decimal('0.55'), Decimal('10')
            ),
        ]
        result = await executor.execute_spread(legs)
        assert result.all_filled is True

    def test_all_filled_false_on_no_fill(self):
        """Manually construct a result with a None order."""
        result = SpreadOrderResult(
            success=False,
            leg_results=[
                PlaceOrderResult(
                    order=None,
                    failure_reason=OrderFailureReason.INSUFFICIENT_CASH,
                )
            ],
        )
        assert result.all_filled is False


# ===================================================================
# Edge cases
# ===================================================================


class TestSpreadExecutorEdgeCases:

    async def test_single_leg_spread(self):
        """A single-leg 'spread' should work fine."""
        ticker = _make_ticker('X')
        trader = _make_trader(
            {ticker: (Decimal('0.45'), Decimal('0.55'))}
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker, Decimal('0.55'), Decimal('5')
            ),
        ]
        result = await executor.execute_spread(legs)
        assert result.success is True
        assert len(result.leg_results) == 1

    async def test_position_updated_after_spread(self):
        """Verify that positions are correctly updated after a spread."""
        ticker_a = _make_ticker('A')
        ticker_b = _make_ticker('B')

        trader = _make_trader(
            {
                ticker_a: (Decimal('0.45'), Decimal('0.55')),
                ticker_b: (Decimal('0.25'), Decimal('0.35')),
            }
        )
        executor = SpreadExecutor(trader)
        legs = [
            SpreadLeg(
                TradeSide.BUY, ticker_a, Decimal('0.55'), Decimal('10')
            ),
            SpreadLeg(
                TradeSide.BUY, ticker_b, Decimal('0.35'), Decimal('10')
            ),
        ]

        result = await executor.execute_spread(legs)
        assert result.success is True

        # Check positions were created
        pos_a = trader.position_manager.get_position(ticker_a)
        pos_b = trader.position_manager.get_position(ticker_b)
        assert pos_a is not None
        assert pos_a.quantity == Decimal('10')
        assert pos_b is not None
        assert pos_b.quantity == Decimal('10')
