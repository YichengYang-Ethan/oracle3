#!/usr/bin/env python3
"""
Performance Analysis Example

This example demonstrates how to use the PerformanceAnalyzer to track
and analyze trading performance. It shows how to:
1. Track trades and build an equity curve
2. Calculate various performance metrics
3. Analyze win/loss statistics
4. Monitor drawdown and risk metrics
"""

from decimal import Decimal

from oracle3.analytics.performance_analyzer import PerformanceAnalyzer
from oracle3.ticker.ticker import PolyMarketTicker
from oracle3.trader.types import Trade, TradeSide


def create_test_ticker() -> PolyMarketTicker:
    """Create a test ticker."""
    return PolyMarketTicker(
        symbol='TEST_MARKET',
        name='Test Market',
        token_id='test123',
    )


def example_basic_analysis():
    """Basic usage of PerformanceAnalyzer."""
    print('=' * 60)
    print('Basic Performance Analysis')
    print('=' * 60)

    # Create analyzer with initial capital
    analyzer = PerformanceAnalyzer(initial_capital=Decimal('10000'))
    ticker = create_test_ticker()

    print(f'\nInitial Capital: ${analyzer.initial_capital:,.2f}')
    print(f'Initial Equity: ${analyzer.get_current_equity():,.2f}')

    # Simulate some trades
    trades = [
        # Buy 100 shares at $0.50 = -$50 (cost)
        Trade(
            side=TradeSide.BUY,
            ticker=ticker,
            price=Decimal('0.50'),
            quantity=Decimal('100'),
            commission=Decimal('0.50'),
        ),
        # Sell 100 shares at $0.60 = +$60 (revenue) - winning trade
        Trade(
            side=TradeSide.SELL,
            ticker=ticker,
            price=Decimal('0.60'),
            quantity=Decimal('100'),
            commission=Decimal('0.60'),
        ),
        # Buy 150 shares at $0.55 = -$82.50
        Trade(
            side=TradeSide.BUY,
            ticker=ticker,
            price=Decimal('0.55'),
            quantity=Decimal('150'),
            commission=Decimal('0.75'),
        ),
        # Sell 150 shares at $0.52 = +$78 - losing trade
        Trade(
            side=TradeSide.SELL,
            ticker=ticker,
            price=Decimal('0.52'),
            quantity=Decimal('150'),
            commission=Decimal('0.78'),
        ),
    ]

    print('\nAdding trades...')
    for i, trade in enumerate(trades, 1):
        analyzer.add_trade(trade)
        print(
            f'  Trade {i}: {trade.side.value.upper()} {trade.quantity} @ ${trade.price}'
        )

    # Print analysis
    analyzer.print_summary()


def example_extended_analysis():
    """Extended analysis with more trades."""
    print('\n' + '=' * 60)
    print('Extended Performance Analysis')
    print('=' * 60)

    analyzer = PerformanceAnalyzer(initial_capital=Decimal('50000'))
    ticker = create_test_ticker()

    # Simulate a longer trading session with mixed results
    # Pattern: B, S (win), B, S (loss), B, S (win), B, S (win), B, S (loss)

    trade_sequences = [
        # Win: Buy at 0.40, sell at 0.48 (+20% gain)
        (Decimal('0.40'), Decimal('0.48'), Decimal('200')),
        # Loss: Buy at 0.50, sell at 0.45 (-10% loss)
        (Decimal('0.50'), Decimal('0.45'), Decimal('150')),
        # Win: Buy at 0.42, sell at 0.50 (+19% gain)
        (Decimal('0.42'), Decimal('0.50'), Decimal('180')),
        # Win: Buy at 0.48, sell at 0.55 (+15% gain)
        (Decimal('0.48'), Decimal('0.55'), Decimal('160')),
        # Loss: Buy at 0.52, sell at 0.48 (-8% loss)
        (Decimal('0.52'), Decimal('0.48'), Decimal('100')),
        # Win: Buy at 0.45, sell at 0.52 (+16% gain)
        (Decimal('0.45'), Decimal('0.52'), Decimal('220')),
        # Win: Buy at 0.50, sell at 0.58 (+16% gain)
        (Decimal('0.50'), Decimal('0.58'), Decimal('200')),
        # Loss: Buy at 0.55, sell at 0.50 (-9% loss)
        (Decimal('0.55'), Decimal('0.50'), Decimal('140')),
    ]

    print('\nSimulating trades...')
    for buy_price, sell_price, quantity in trade_sequences:
        # Buy
        analyzer.add_trade(
            Trade(
                side=TradeSide.BUY,
                ticker=ticker,
                price=buy_price,
                quantity=quantity,
                commission=Decimal('1.00'),
            )
        )
        # Sell
        analyzer.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=ticker,
                price=sell_price,
                quantity=quantity,
                commission=Decimal('1.00'),
            )
        )

        result = 'WIN' if sell_price > buy_price else 'LOSS'
        pnl = (sell_price - buy_price) * quantity - Decimal('2.00')
        print(
            f'  {result}: Buy @ ${buy_price}, Sell @ ${sell_price}, Qty: {quantity}, PnL: ${pnl:.2f}'
        )

    # Print detailed analysis
    analyzer.print_summary()

    # Access individual metrics
    stats = analyzer.get_stats()
    print('\nDetailed Metrics:')
    print(f'  Total PnL: ${stats.total_pnl:,.2f}')
    print(f'  Win Rate: {stats.win_rate * 100:.1f}%')
    print(f'  Profit Factor: {stats.profit_factor:.2f}')
    print(f'  Max Consecutive Wins: {stats.max_consecutive_wins}')
    print(f'  Max Consecutive Losses: {stats.max_consecutive_losses}')


def example_equity_curve():
    """Demonstrate equity curve tracking."""
    print('\n' + '=' * 60)
    print('Equity Curve Analysis')
    print('=' * 60)

    analyzer = PerformanceAnalyzer(initial_capital=Decimal('10000'))
    ticker = create_test_ticker()

    # Simulate trades that create an interesting equity curve
    trades_data = [
        (TradeSide.SELL, Decimal('0.50'), Decimal('100')),  # +$50
        (TradeSide.SELL, Decimal('0.55'), Decimal('100')),  # +$55
        (TradeSide.BUY, Decimal('0.60'), Decimal('200')),  # -$120
        (TradeSide.SELL, Decimal('0.65'), Decimal('150')),  # +$97.50
        (TradeSide.BUY, Decimal('0.52'), Decimal('100')),  # -$52
        (TradeSide.SELL, Decimal('0.58'), Decimal('200')),  # +$116
    ]

    for side, price, quantity in trades_data:
        analyzer.add_trade(
            Trade(
                side=side,
                ticker=ticker,
                price=price,
                quantity=quantity,
                commission=Decimal('0'),
            )
        )

    # Print equity curve
    print('\nEquity Curve:')
    print('-' * 40)
    curve = analyzer.get_equity_curve()
    for point in curve:
        bar_length = int((float(point.equity) / 10000) * 40)
        bar = '#' * bar_length
        print(f'Trade {point.trade_index:2d}: ${point.equity:>10,.2f} |{bar}')

    print('-' * 40)
    print(f'\nPeak Equity: ${max(p.equity for p in curve):,.2f}')
    print(f'Min Equity: ${min(p.equity for p in curve):,.2f}')

    stats = analyzer.get_stats()
    print(f'Max Drawdown: {stats.max_drawdown * 100:.2f}%')


def example_risk_metrics():
    """Demonstrate risk metric calculation."""
    print('\n' + '=' * 60)
    print('Risk Metrics Analysis')
    print('=' * 60)

    analyzer = PerformanceAnalyzer(initial_capital=Decimal('100000'))
    ticker = create_test_ticker()

    # Generate many trades to get meaningful Sharpe ratio
    import random

    random.seed(42)  # For reproducibility

    print('\nSimulating 100 trades...')
    for _i in range(100):
        # Random price between 0.40 and 0.60
        price = Decimal(str(round(0.40 + random.random() * 0.20, 2)))
        quantity = Decimal(str(random.randint(50, 200)))

        # Slightly biased towards winning trades
        if random.random() < 0.55:
            side = TradeSide.SELL  # Winning
        else:
            side = TradeSide.BUY  # Losing (in our simple model)

        analyzer.add_trade(
            Trade(
                side=side,
                ticker=ticker,
                price=price,
                quantity=quantity,
                commission=Decimal('1.00'),
            )
        )

    # Print risk analysis
    stats = analyzer.get_stats()

    print('\nRisk Analysis:')
    print('-' * 40)
    print(f'Total Trades: {stats.total_trades}')
    print(f'Winning Trades: {stats.winning_trades} ({stats.win_rate * 100:.1f}%)')
    print(f'Losing Trades: {stats.losing_trades}')
    print('')
    print(f'Total Return: {analyzer.get_return_pct():.2f}%')
    print(f'Max Drawdown: {stats.max_drawdown * 100:.2f}%')
    print(f'Sharpe Ratio: {stats.sharpe_ratio:.4f}')
    print(f'Profit Factor: {stats.profit_factor:.2f}')
    print('')
    print(f'Average Profit: ${stats.average_profit:,.2f}')
    print(f'Average Loss: ${stats.average_loss:,.2f}')
    print('')
    print(f'Current Equity: ${analyzer.get_current_equity():,.2f}')


def example_reset_and_compare():
    """Demonstrate resetting and comparing different strategies."""
    print('\n' + '=' * 60)
    print('Strategy Comparison')
    print('=' * 60)

    ticker = create_test_ticker()

    # Strategy A: Conservative (smaller positions)
    analyzer_a = PerformanceAnalyzer(initial_capital=Decimal('10000'))
    for _ in range(20):
        analyzer_a.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=ticker,
                price=Decimal('0.52'),
                quantity=Decimal('50'),  # Smaller positions
                commission=Decimal('0.50'),
            )
        )

    # Strategy B: Aggressive (larger positions)
    analyzer_b = PerformanceAnalyzer(initial_capital=Decimal('10000'))
    for _ in range(20):
        analyzer_b.add_trade(
            Trade(
                side=TradeSide.SELL,
                ticker=ticker,
                price=Decimal('0.52'),
                quantity=Decimal('200'),  # Larger positions
                commission=Decimal('0.50'),
            )
        )

    print('\nStrategy A (Conservative):')
    print(f'  Final Equity: ${analyzer_a.get_current_equity():,.2f}')
    print(f'  Return: {analyzer_a.get_return_pct():.2f}%')
    print(f'  Max Drawdown: {analyzer_a.get_stats().max_drawdown * 100:.2f}%')

    print('\nStrategy B (Aggressive):')
    print(f'  Final Equity: ${analyzer_b.get_current_equity():,.2f}')
    print(f'  Return: {analyzer_b.get_return_pct():.2f}%')
    print(f'  Max Drawdown: {analyzer_b.get_stats().max_drawdown * 100:.2f}%')

    # Reset and reuse
    analyzer_a.reset()
    print(f'\nAfter reset, Strategy A equity: ${analyzer_a.get_current_equity():,.2f}')


def main():
    """Run all performance analysis examples."""
    print('Oracle3 - Performance Analysis Examples\n')

    example_basic_analysis()
    example_extended_analysis()
    example_equity_curve()
    example_risk_metrics()
    example_reset_and_compare()

    print('\n' + '=' * 60)
    print('All examples completed!')
    print('=' * 60)


if __name__ == '__main__':
    main()
