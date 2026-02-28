from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager, StandardRiskManager
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.types import OrderFailureReason, OrderStatus, TradeSide


@pytest.fixture
def test_ticker() -> PolyMarketTicker:
    """Create a test ticker."""
    return PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
        market_id='market123',
        event_id='event123',
    )


@pytest.fixture
def market_data(test_ticker: PolyMarketTicker) -> MarketDataManager:
    """Create market data with test order book."""
    mdm = MarketDataManager()
    order_book = OrderBook()
    order_book.update(
        asks=[
            Level(price=Decimal('0.55'), size=Decimal('1000')),
            Level(price=Decimal('0.56'), size=Decimal('500')),
        ],
        bids=[
            Level(price=Decimal('0.50'), size=Decimal('1000')),
            Level(price=Decimal('0.49'), size=Decimal('500')),
        ],
    )
    mdm.order_books[test_ticker] = order_book
    return mdm


@pytest.fixture
def position_manager(test_ticker: PolyMarketTicker) -> PositionManager:
    """Create position manager with initial positions."""
    pm = PositionManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return pm


@pytest.fixture
def paper_trader(
    market_data: MarketDataManager, position_manager: PositionManager
) -> PaperTrader:
    """Create a paper trader."""
    return PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),  # 100% fill for predictable tests
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.01'),  # 1% commission
    )


class TestPaperTrader:
    @pytest.mark.asyncio
    async def test_buy_order_filled(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test a buy order that gets filled."""
        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('100'),
        )

        assert result.order is not None
        assert result.failure_reason is None
        assert result.order.status in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]
        assert result.order.filled_quantity > 0

    @pytest.mark.asyncio
    async def test_sell_order_filled(
        self,
        paper_trader: PaperTrader,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
    ):
        """Test a sell order that gets filled."""
        # First add a position to sell
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('200'),
                average_cost=Decimal('0.45'),
                realized_pnl=Decimal('0'),
            )
        )

        result = await paper_trader.place_order(
            side=TradeSide.SELL,
            ticker=test_ticker,
            limit_price=Decimal('0.50'),
            quantity=Decimal('100'),
        )

        assert result.order is not None
        assert result.failure_reason is None
        assert result.order.status in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]

    @pytest.mark.asyncio
    async def test_invalid_quantity_rejected(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that zero quantity is rejected."""
        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('0'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_negative_quantity_rejected(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that negative quantity is rejected."""
        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('-100'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_zero_price_rejected(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that zero price is rejected."""
        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0'),
            quantity=Decimal('100'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_short_selling_rejected(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that short selling is rejected (no position)."""
        result = await paper_trader.place_order(
            side=TradeSide.SELL,
            ticker=test_ticker,
            limit_price=Decimal('0.50'),
            quantity=Decimal('100'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_sell_more_than_owned_rejected(
        self,
        paper_trader: PaperTrader,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
    ):
        """Test that selling more than owned is rejected."""
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('50'),
                average_cost=Decimal('0.45'),
                realized_pnl=Decimal('0'),
            )
        )

        result = await paper_trader.place_order(
            side=TradeSide.SELL,
            ticker=test_ticker,
            limit_price=Decimal('0.50'),
            quantity=Decimal('100'),  # More than owned
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_insufficient_cash_rejected(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that order is rejected when insufficient cash."""
        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('100000'),  # Way more than we can afford
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INSUFFICIENT_CASH

    @pytest.mark.asyncio
    async def test_risk_check_failure(
        self,
        market_data: MarketDataManager,
        position_manager: PositionManager,
        test_ticker: PolyMarketTicker,
    ):
        """Test that risk check failure is reported."""
        risk_manager = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_single_trade_size=Decimal('10'),  # Very small limit
        )

        paper_trader = PaperTrader(
            market_data=market_data,
            risk_manager=risk_manager,
            position_manager=position_manager,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0'),
        )

        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('100'),  # 55 > 10 limit
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.RISK_CHECK_FAILED

    @pytest.mark.asyncio
    async def test_trade_scope_rejects_unapproved_market(
        self,
        paper_trader: PaperTrader,
        market_data: MarketDataManager,
        test_ticker: PolyMarketTicker,
    ):
        other_ticker = PolyMarketTicker(
            symbol='OTHER_TOKEN',
            name='Other Market',
            token_id='other-token',
            market_id='market456',
            event_id='event456',
        )
        other_book = OrderBook()
        other_book.update(
            asks=[Level(price=Decimal('0.40'), size=Decimal('1000'))],
            bids=[Level(price=Decimal('0.35'), size=Decimal('1000'))],
        )
        market_data.order_books[other_ticker] = other_book
        paper_trader.set_allowed_tickers([test_ticker])

        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=other_ticker,
            limit_price=Decimal('0.40'),
            quantity=Decimal('10'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.MARKET_NOT_ALLOWED

    @pytest.mark.asyncio
    async def test_position_updated_after_trade(
        self,
        paper_trader: PaperTrader,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
    ):
        """Test that position is updated after a trade."""
        initial_cash = position_manager.get_position(
            CashTicker.POLYMARKET_USDC
        ).quantity

        await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('100'),
        )

        # Check that we now have a position
        position = position_manager.get_position(test_ticker)
        assert position is not None
        assert position.quantity > 0

        # Check that cash was reduced
        new_cash = position_manager.get_position(CashTicker.POLYMARKET_USDC).quantity
        assert new_cash < initial_cash

    @pytest.mark.asyncio
    async def test_order_stored(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that orders are stored."""
        assert len(paper_trader.orders) == 0

        await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('100'),
        )

        assert len(paper_trader.orders) == 1

    @pytest.mark.asyncio
    async def test_commission_applied(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that commission is applied to trades."""
        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('100'),
        )

        assert result.order is not None
        assert result.order.commission > 0

    @pytest.mark.asyncio
    async def test_no_liquidity_order_placed(
        self,
        market_data: MarketDataManager,
        position_manager: PositionManager,
        test_ticker: PolyMarketTicker,
    ):
        """Test order when price is too aggressive (no liquidity at limit)."""
        paper_trader = PaperTrader(
            market_data=market_data,
            risk_manager=NoRiskManager(),
            position_manager=position_manager,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0'),
        )

        # Try to buy at price below all asks
        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.40'),  # Below lowest ask of 0.55
            quantity=Decimal('100'),
        )

        assert result.order is not None
        assert result.order.status == OrderStatus.PLACED
        assert result.order.filled_quantity == Decimal('0')


class TestPaperTraderVariableFillRate:
    @pytest.mark.asyncio
    async def test_partial_fill(
        self,
        market_data: MarketDataManager,
        position_manager: PositionManager,
        test_ticker: PolyMarketTicker,
    ):
        """Test partial fill with variable fill rate."""
        paper_trader = PaperTrader(
            market_data=market_data,
            risk_manager=NoRiskManager(),
            position_manager=position_manager,
            min_fill_rate=Decimal('0.5'),
            max_fill_rate=Decimal('0.5'),  # 50% fill rate
            commission_rate=Decimal('0'),
        )

        result = await paper_trader.place_order(
            side=TradeSide.BUY,
            ticker=test_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('100'),
        )

        assert result.order is not None
        # With 50% fill rate on 1000 liquidity, should partially fill
        assert result.order.status in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]
