from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import (
    AggressiveRiskManager,
    ConservativeRiskManager,
    NoRiskManager,
    StandardRiskManager,
)
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.types import TradeSide


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
def position_manager() -> PositionManager:
    """Create a position manager with initial cash."""
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
def market_data(test_ticker: PolyMarketTicker) -> MarketDataManager:
    """Create market data with test order book."""
    mdm = MarketDataManager()
    order_book = OrderBook()
    order_book.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
    )
    mdm.order_books[test_ticker] = order_book
    return mdm


class TestNoRiskManager:
    @pytest.mark.asyncio
    async def test_always_allows_trades(self, test_ticker: PolyMarketTicker):
        """Test that NoRiskManager always allows trades."""
        rm = NoRiskManager()

        result = await rm.check_trade(
            test_ticker, TradeSide.BUY, Decimal('1000000'), Decimal('1.0')
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_allows_sells(self, test_ticker: PolyMarketTicker):
        """Test that NoRiskManager allows sells."""
        rm = NoRiskManager()

        result = await rm.check_trade(
            test_ticker, TradeSide.SELL, Decimal('100'), Decimal('0.50')
        )
        assert result is True


class TestStandardRiskManager:
    @pytest.mark.asyncio
    async def test_trade_within_limits(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test trade that is within all limits."""
        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_single_trade_size=Decimal('1000'),
            max_position_size=Decimal('5000'),
            max_total_exposure=Decimal('50000'),
        )

        result = await rm.check_trade(
            test_ticker, TradeSide.BUY, Decimal('100'), Decimal('0.50')
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_trade_exceeds_single_trade_limit(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test trade that exceeds single trade size limit."""
        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_single_trade_size=Decimal('100'),  # Small limit
        )

        # Trade value: 500 * 0.50 = 250 > 100
        result = await rm.check_trade(
            test_ticker, TradeSide.BUY, Decimal('500'), Decimal('0.50')
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_trade_exceeds_position_limit(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test trade that would exceed position size limit."""
        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_single_trade_size=Decimal('10000'),
            max_position_size=Decimal('100'),  # Small position limit
        )

        # This would create position of 500 * 0.50 = 250 > 100
        result = await rm.check_trade(
            test_ticker, TradeSide.BUY, Decimal('500'), Decimal('0.50')
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_sell_always_passes_position_limit(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test that sells pass position limit checks."""
        # Add existing position
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('1000'),
                average_cost=Decimal('0.50'),
                realized_pnl=Decimal('0'),
            )
        )

        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_position_size=Decimal('100'),  # Very small limit
            max_single_trade_size=Decimal('1000'),  # Allow the trade size
        )

        # Sell should pass even though current position exceeds limit
        result = await rm.check_trade(
            test_ticker, TradeSide.SELL, Decimal('100'), Decimal('0.50')
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_max_positions_limit(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test maximum number of positions limit."""
        # Add existing positions and their order books
        for i in range(5):
            ticker = PolyMarketTicker(
                symbol=f'TOKEN_{i}',
                name=f'Token {i}',
                token_id=f'token{i}',
            )
            position_manager.update_position(
                Position(
                    ticker=ticker,
                    quantity=Decimal('100'),
                    average_cost=Decimal('0.50'),
                    realized_pnl=Decimal('0'),
                )
            )
            # Add order book for this ticker
            order_book = OrderBook()
            order_book.update(
                asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
                bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
            )
            market_data.order_books[ticker] = order_book

        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_positions=5,  # Already at max
        )

        # New position should be rejected
        result = await rm.check_trade(
            test_ticker, TradeSide.BUY, Decimal('10'), Decimal('0.50')
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_adding_to_existing_position_allowed(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test that adding to an existing position is allowed even at max positions."""
        # Add existing position for test ticker
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('100'),
                average_cost=Decimal('0.50'),
                realized_pnl=Decimal('0'),
            )
        )

        # Add other positions up to max
        for i in range(4):
            ticker = PolyMarketTicker(
                symbol=f'TOKEN_{i}',
                name=f'Token {i}',
                token_id=f'token{i}',
            )
            position_manager.update_position(
                Position(
                    ticker=ticker,
                    quantity=Decimal('100'),
                    average_cost=Decimal('0.50'),
                    realized_pnl=Decimal('0'),
                )
            )
            # Add order book for this ticker
            order_book = OrderBook()
            order_book.update(
                asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
                bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
            )
            market_data.order_books[ticker] = order_book

        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_positions=5,
        )

        # Adding to existing position should be allowed
        result = await rm.check_trade(
            test_ticker, TradeSide.BUY, Decimal('10'), Decimal('0.50')
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_cash_ticker_bypasses_checks(
        self,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test that cash tickers bypass risk checks."""
        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_single_trade_size=Decimal('1'),  # Very restrictive
        )

        # Cash ticker should bypass all checks
        result = await rm.check_trade(
            CashTicker.POLYMARKET_USDC, TradeSide.BUY, Decimal('1000000'), Decimal('1')
        )
        assert result is True

    def test_get_current_drawdown(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test drawdown calculation."""
        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            initial_capital=Decimal('10000'),
        )

        # Initial drawdown should be 0
        drawdown = rm.get_current_drawdown()
        assert drawdown == Decimal('0')

    def test_get_remaining_exposure(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test remaining exposure calculation."""
        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_total_exposure=Decimal('50000'),
        )

        # Cash is not considered market exposure
        remaining = rm.get_remaining_exposure()
        assert remaining == Decimal('50000')

    def test_reset_daily_tracking(
        self,
        test_ticker: PolyMarketTicker,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test resetting daily tracking."""
        rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
        )

        rm.reset_daily_tracking()
        assert rm._daily_starting_value == Decimal('10000')
        assert rm._daily_pnl == Decimal('0')


class TestConservativeRiskManager:
    def test_conservative_limits(
        self,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test that conservative manager has tighter limits."""
        rm = ConservativeRiskManager(
            position_manager=position_manager,
            market_data=market_data,
        )

        assert rm.max_single_trade_size == Decimal('500')
        assert rm.max_position_size == Decimal('2000')
        assert rm.max_total_exposure == Decimal('10000')
        assert rm.max_drawdown_pct == Decimal('0.10')
        assert rm.daily_loss_limit == Decimal('500')
        assert rm.max_positions == 5


class TestAggressiveRiskManager:
    def test_aggressive_limits(
        self,
        position_manager: PositionManager,
        market_data: MarketDataManager,
    ):
        """Test that aggressive manager has looser limits."""
        rm = AggressiveRiskManager(
            position_manager=position_manager,
            market_data=market_data,
        )

        assert rm.max_single_trade_size == Decimal('5000')
        assert rm.max_position_size == Decimal('20000')
        assert rm.max_total_exposure == Decimal('100000')
        assert rm.max_drawdown_pct == Decimal('0.30')
        assert rm.daily_loss_limit is None
        assert rm.max_positions == 20
