from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.types import Trade, TradeSide


@pytest.fixture
def position_manager() -> PositionManager:
    """Create a fresh position manager for each test."""
    return PositionManager()


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
    """Create market data manager with test data."""
    mdm = MarketDataManager()
    order_book = OrderBook()
    order_book.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
    )
    mdm.order_books[test_ticker] = order_book
    return mdm


class TestPositionManager:
    def test_update_position(self, position_manager: PositionManager):
        """Test updating a position."""
        position = Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
        position_manager.update_position(position)

        retrieved = position_manager.get_position(CashTicker.POLYMARKET_USDC)
        assert retrieved is not None
        assert retrieved.quantity == Decimal('10000')

    def test_get_position_not_found(
        self, position_manager: PositionManager, test_ticker: PolyMarketTicker
    ):
        """Test getting a position that doesn't exist."""
        result = position_manager.get_position(test_ticker)
        assert result is None

    def test_apply_buy_trade(
        self, position_manager: PositionManager, test_ticker: PolyMarketTicker
    ):
        """Test applying a buy trade."""
        # Setup initial cash position
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('10000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        trade = Trade(
            side=TradeSide.BUY,
            ticker=test_ticker,
            price=Decimal('0.50'),
            quantity=Decimal('100'),
            commission=Decimal('0.50'),
        )

        position = position_manager.apply_trade(trade)

        assert position.quantity == Decimal('100')
        assert position.average_cost == Decimal('0.505')  # (50 + 0.5) / 100

        # Check cash was deducted
        cash = position_manager.get_position(CashTicker.POLYMARKET_USDC)
        assert cash.quantity == Decimal('9949.50')  # 10000 - 50 - 0.5

    def test_apply_sell_trade(
        self, position_manager: PositionManager, test_ticker: PolyMarketTicker
    ):
        """Test applying a sell trade."""
        # Setup initial positions
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('9000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('100'),
                average_cost=Decimal('0.50'),
                realized_pnl=Decimal('0'),
            )
        )

        trade = Trade(
            side=TradeSide.SELL,
            ticker=test_ticker,
            price=Decimal('0.60'),
            quantity=Decimal('50'),
            commission=Decimal('0.30'),
        )

        position = position_manager.apply_trade(trade)

        assert position.quantity == Decimal('50')
        # Realized PnL: (0.60 - 0.50) * 50 - 0.30 = 4.70
        assert position.realized_pnl == Decimal('4.70')

    def test_sell_exceeds_position_raises_error(
        self, position_manager: PositionManager, test_ticker: PolyMarketTicker
    ):
        """Test that selling more than owned raises an error."""
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('10000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('50'),
                average_cost=Decimal('0.50'),
                realized_pnl=Decimal('0'),
            )
        )

        trade = Trade(
            side=TradeSide.SELL,
            ticker=test_ticker,
            price=Decimal('0.60'),
            quantity=Decimal('100'),  # More than we own
            commission=Decimal('0'),
        )

        with pytest.raises(ValueError, match='exceeding the current position'):
            position_manager.apply_trade(trade)

    def test_get_cash_positions(
        self, position_manager: PositionManager, test_ticker: PolyMarketTicker
    ):
        """Test getting only cash positions."""
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('10000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('100'),
                average_cost=Decimal('0.50'),
                realized_pnl=Decimal('0'),
            )
        )

        cash_positions = position_manager.get_cash_positions()
        assert len(cash_positions) == 1
        assert cash_positions[0].ticker == CashTicker.POLYMARKET_USDC

    def test_get_non_cash_positions(
        self, position_manager: PositionManager, test_ticker: PolyMarketTicker
    ):
        """Test getting only non-cash positions."""
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('10000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('100'),
                average_cost=Decimal('0.50'),
                realized_pnl=Decimal('0'),
            )
        )

        non_cash = position_manager.get_non_cash_positions()
        assert len(non_cash) == 1
        assert non_cash[0].ticker == test_ticker

    def test_get_total_realized_pnl(
        self, position_manager: PositionManager, test_ticker: PolyMarketTicker
    ):
        """Test getting total realized PnL."""
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('0'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('100'),
            )
        )

        ticker2 = PolyMarketTicker(
            symbol='TEST2',
            name='Test 2',
            token_id='token2',
            market_id='market2',
            event_id='event2',
        )
        position_manager.update_position(
            Position(
                ticker=ticker2,
                quantity=Decimal('0'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('50'),
            )
        )

        total_pnl = position_manager.get_total_realized_pnl()
        assert total_pnl == Decimal('150')

    def test_get_unrealized_pnl(
        self,
        position_manager: PositionManager,
        test_ticker: PolyMarketTicker,
        market_data: MarketDataManager,
    ):
        """Test getting unrealized PnL."""
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('100'),
                average_cost=Decimal('0.40'),
                realized_pnl=Decimal('0'),
            )
        )

        # Current bid is 0.50, so unrealized PnL = (0.50 - 0.40) * 100 = 10
        unrealized = position_manager.get_unrealized_pnl(test_ticker, market_data)
        assert unrealized == Decimal('10')

    def test_get_portfolio_value(
        self,
        position_manager: PositionManager,
        test_ticker: PolyMarketTicker,
        market_data: MarketDataManager,
    ):
        """Test getting total portfolio value."""
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('5000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        position_manager.update_position(
            Position(
                ticker=test_ticker,
                quantity=Decimal('100'),
                average_cost=Decimal('0.40'),
                realized_pnl=Decimal('0'),
            )
        )

        # Cash: 5000, Position: 100 * 0.50 (bid) = 50
        portfolio = position_manager.get_portfolio_value(market_data)
        assert CashTicker.POLYMARKET_USDC.symbol in portfolio
        assert portfolio[CashTicker.POLYMARKET_USDC.symbol] == Decimal('5050')
