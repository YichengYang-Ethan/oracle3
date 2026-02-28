from decimal import Decimal

import pytest

from oracle3.analytics.performance_analyzer import (
    EquityPoint,
    PerformanceAnalyzer,
    TradeStats,
)
from oracle3.ticker.ticker import PolyMarketTicker
from oracle3.trader.types import Trade, TradeSide


@pytest.fixture
def test_ticker() -> PolyMarketTicker:
    """Create a test ticker."""
    return PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
    )


@pytest.fixture
def analyzer() -> PerformanceAnalyzer:
    """Create a fresh performance analyzer."""
    return PerformanceAnalyzer(initial_capital=Decimal('10000'))


class TestTradeStats:
    def test_trade_stats_creation(self):
        """Test creating TradeStats."""
        stats = TradeStats(
            total_trades=10,
            winning_trades=6,
            losing_trades=4,
            win_rate=Decimal('0.60'),
            average_profit=Decimal('100'),
            average_loss=Decimal('-50'),
            max_drawdown=Decimal('0.10'),
            sharpe_ratio=Decimal('1.5'),
            profit_factor=Decimal('2.0'),
            total_pnl=Decimal('400'),
        )

        assert stats.total_trades == 10
        assert stats.win_rate == Decimal('0.60')


class TestEquityPoint:
    def test_equity_point_creation(self):
        """Test creating EquityPoint."""
        point = EquityPoint(
            timestamp=100,
            equity=Decimal('10500'),
            trade_index=5,
        )

        assert point.timestamp == 100
        assert point.equity == Decimal('10500')
        assert point.trade_index == 5


class TestPerformanceAnalyzer:
    def test_initial_state(self, analyzer: PerformanceAnalyzer):
        """Test initial analyzer state."""
        assert analyzer.initial_capital == Decimal('10000')
        assert len(analyzer.trades) == 0
        assert len(analyzer.equity_curve) == 1
        assert analyzer.equity_curve[0].equity == Decimal('10000')

    def test_get_current_equity_initial(self, analyzer: PerformanceAnalyzer):
        """Test getting current equity with no trades."""
        assert analyzer.get_current_equity() == Decimal('10000')

    def test_get_return_pct_initial(self, analyzer: PerformanceAnalyzer):
        """Test getting return percentage with no trades."""
        assert analyzer.get_return_pct() == Decimal('0')

    def test_add_buy_trade(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test adding a buy trade."""
        trade = Trade(
            side=TradeSide.BUY,
            ticker=test_ticker,
            price=Decimal('0.50'),
            quantity=Decimal('100'),
            commission=Decimal('0.50'),
        )

        analyzer.add_trade(trade)

        assert len(analyzer.trades) == 1
        # Buy trade reduces equity: -(0.50 * 100 + 0.50) = -50.50
        assert analyzer.get_current_equity() == Decimal('10000') - Decimal('50.50')

    def test_add_sell_trade(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test adding a sell trade."""
        trade = Trade(
            side=TradeSide.SELL,
            ticker=test_ticker,
            price=Decimal('0.60'),
            quantity=Decimal('100'),
            commission=Decimal('0.60'),
        )

        analyzer.add_trade(trade)

        assert len(analyzer.trades) == 1
        # Sell trade increases equity: 0.60 * 100 - 0.60 = 59.40
        assert analyzer.get_current_equity() == Decimal('10000') + Decimal('59.40')

    def test_winning_and_losing_trades(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test tracking winning and losing trades."""
        # Add some winning sells
        for _ in range(3):
            analyzer.add_trade(
                Trade(
                    side=TradeSide.SELL,
                    ticker=test_ticker,
                    price=Decimal('0.60'),
                    quantity=Decimal('100'),
                    commission=Decimal('0'),
                )
            )

        # Add some losing buys
        for _ in range(2):
            analyzer.add_trade(
                Trade(
                    side=TradeSide.BUY,
                    ticker=test_ticker,
                    price=Decimal('0.50'),
                    quantity=Decimal('100'),
                    commission=Decimal('0'),
                )
            )

        stats = analyzer.get_stats()
        assert stats.total_trades == 5
        assert stats.winning_trades == 3  # Sells are winning (positive cash flow)
        assert stats.losing_trades == 2  # Buys are losing (negative cash flow)

    def test_win_rate_calculation(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test win rate calculation."""
        # 6 winning trades
        for _ in range(6):
            analyzer.add_trade(
                Trade(
                    side=TradeSide.SELL,
                    ticker=test_ticker,
                    price=Decimal('0.60'),
                    quantity=Decimal('100'),
                    commission=Decimal('0'),
                )
            )

        # 4 losing trades
        for _ in range(4):
            analyzer.add_trade(
                Trade(
                    side=TradeSide.BUY,
                    ticker=test_ticker,
                    price=Decimal('0.50'),
                    quantity=Decimal('100'),
                    commission=Decimal('0'),
                )
            )

        stats = analyzer.get_stats()
        assert stats.win_rate == Decimal('0.6')

    def test_max_drawdown_calculation(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test max drawdown calculation."""
        # Start with gains to establish peak
        analyzer.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=test_ticker,
                price=Decimal('1.00'),
                quantity=Decimal('1000'),
                commission=Decimal('0'),
            )
        )
        # Equity now 11000

        # Then losses
        analyzer.add_trade(
            Trade(
                side=TradeSide.BUY,
                ticker=test_ticker,
                price=Decimal('1.00'),
                quantity=Decimal('2000'),
                commission=Decimal('0'),
            )
        )
        # Equity now 9000

        stats = analyzer.get_stats()
        # Drawdown = (11000 - 9000) / 11000 ≈ 0.1818
        assert stats.max_drawdown > Decimal('0.18')
        assert stats.max_drawdown < Decimal('0.19')

    def test_consecutive_streaks(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test consecutive wins/losses tracking."""
        # 3 consecutive wins
        for _ in range(3):
            analyzer.add_trade(
                Trade(
                    side=TradeSide.SELL,
                    ticker=test_ticker,
                    price=Decimal('0.60'),
                    quantity=Decimal('100'),
                    commission=Decimal('0'),
                )
            )

        # 2 consecutive losses
        for _ in range(2):
            analyzer.add_trade(
                Trade(
                    side=TradeSide.BUY,
                    ticker=test_ticker,
                    price=Decimal('0.50'),
                    quantity=Decimal('100'),
                    commission=Decimal('0'),
                )
            )

        # 1 more win
        analyzer.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=test_ticker,
                price=Decimal('0.60'),
                quantity=Decimal('100'),
                commission=Decimal('0'),
            )
        )

        stats = analyzer.get_stats()
        assert stats.max_consecutive_wins == 3
        assert stats.max_consecutive_losses == 2

    def test_profit_factor(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test profit factor calculation."""
        # Gross profit: 200
        for _ in range(2):
            analyzer.add_trade(
                Trade(
                    side=TradeSide.SELL,
                    ticker=test_ticker,
                    price=Decimal('1.00'),
                    quantity=Decimal('100'),
                    commission=Decimal('0'),
                )
            )

        # Gross loss: 100
        analyzer.add_trade(
            Trade(
                side=TradeSide.BUY,
                ticker=test_ticker,
                price=Decimal('1.00'),
                quantity=Decimal('100'),
                commission=Decimal('0'),
            )
        )

        stats = analyzer.get_stats()
        # Profit factor = 200 / 100 = 2
        assert stats.profit_factor == Decimal('2')

    def test_get_equity_curve(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker
    ):
        """Test getting equity curve."""
        analyzer.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=test_ticker,
                price=Decimal('0.50'),
                quantity=Decimal('100'),
                commission=Decimal('0'),
            )
        )

        curve = analyzer.get_equity_curve()
        assert len(curve) == 2
        assert curve[0].equity == Decimal('10000')
        assert curve[1].equity == Decimal('10050')  # 10000 + 50

    def test_reset(self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker):
        """Test resetting the analyzer."""
        analyzer.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=test_ticker,
                price=Decimal('0.50'),
                quantity=Decimal('100'),
                commission=Decimal('0'),
            )
        )

        analyzer.reset()

        assert len(analyzer.trades) == 0
        assert len(analyzer.equity_curve) == 1
        assert analyzer.get_current_equity() == Decimal('10000')

    def test_empty_stats(self, analyzer: PerformanceAnalyzer):
        """Test stats with no trades."""
        stats = analyzer.get_stats()

        assert stats.total_trades == 0
        assert stats.winning_trades == 0
        assert stats.losing_trades == 0
        assert stats.win_rate == Decimal('0')
        assert stats.max_drawdown == Decimal('0')

    def test_print_summary_runs(
        self, analyzer: PerformanceAnalyzer, test_ticker: PolyMarketTicker, capsys
    ):
        """Test that print_summary executes without error."""
        analyzer.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=test_ticker,
                price=Decimal('0.50'),
                quantity=Decimal('100'),
                commission=Decimal('0'),
            )
        )

        analyzer.print_summary()

        captured = capsys.readouterr()
        assert 'PERFORMANCE SUMMARY' in captured.out
        assert 'Total Trades' in captured.out
