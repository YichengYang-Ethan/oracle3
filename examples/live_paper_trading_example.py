#!/usr/bin/env python3
"""
Live Paper Trading Example

This example demonstrates how to run live paper trading (simulated) with
the Oracle3 framework. It shows how to:
1. Set up live data sources (RSS news feed)
2. Configure paper trading with risk management
3. Run a live simulation
4. Monitor positions and performance
"""

import asyncio
from decimal import Decimal

from oracle3.data.live.live_data_source import LiveRSSNewsDataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.live.live_trader import run_live_paper_trading
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import ConservativeRiskManager
from oracle3.strategy.test_strategy import TestStrategy
from oracle3.ticker.ticker import CashTicker


async def run_rss_paper_trading():
    """Run paper trading with RSS news data."""
    print('=' * 60)
    print('Live Paper Trading with RSS News Feed')
    print('=' * 60)

    # Configuration
    initial_capital = Decimal('10000')
    duration = 60  # Run for 60 seconds (adjust as needed)

    # Create RSS data source
    # This will fetch news from various WSJ feeds
    data_source = LiveRSSNewsDataSource(
        cache_file='rss_news_cache.jsonl',
        polling_interval=30.0,  # Poll every 30 seconds
        max_articles_per_poll=5,
        categories=['finance', 'business'],  # Filter by categories
    )

    # Create strategy
    strategy = TestStrategy()

    print('\nConfiguration:')
    print(f'  Initial Capital: ${initial_capital:,.2f}')
    print(f'  Duration: {duration} seconds')
    print('  Polling Interval: 30 seconds')
    print('  Categories: finance, business')

    print('\nStarting live paper trading...')
    print('(Press Ctrl+C to stop early)\n')

    try:
        await run_live_paper_trading(
            data_source=data_source,
            strategy=strategy,
            initial_capital=initial_capital,
            duration=duration,
        )
    except KeyboardInterrupt:
        print('\nStopped by user')


async def run_paper_trading_with_risk_management():
    """Run paper trading with risk management."""
    print('\n' + '=' * 60)
    print('Live Paper Trading with Risk Management')
    print('=' * 60)

    initial_capital = Decimal('10000')
    duration = 60

    # Set up components manually for more control
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_capital,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    # Use conservative risk manager
    risk_manager = ConservativeRiskManager(
        position_manager=position_manager,
        market_data=market_data,
        initial_capital=initial_capital,
    )

    data_source = LiveRSSNewsDataSource(
        polling_interval=30.0,
        max_articles_per_poll=3,
    )

    strategy = TestStrategy()

    print('\nRisk Management Settings:')
    print(f'  Max Trade Size: ${risk_manager.max_single_trade_size:,.2f}')
    print(f'  Max Position Size: ${risk_manager.max_position_size:,.2f}')
    print(f'  Max Total Exposure: ${risk_manager.max_total_exposure:,.2f}')
    print(f'  Max Drawdown: {risk_manager.max_drawdown_pct * 100:.0f}%')
    print(f'  Daily Loss Limit: ${risk_manager.daily_loss_limit:,.2f}')
    print(f'  Max Positions: {risk_manager.max_positions}')

    print('\nStarting live paper trading with risk management...')
    print('(Press Ctrl+C to stop early)\n')

    try:
        await run_live_paper_trading(
            data_source=data_source,
            strategy=strategy,
            initial_capital=initial_capital,
            risk_manager=risk_manager,
            duration=duration,
        )
    except KeyboardInterrupt:
        print('\nStopped by user')


async def main():
    """Run all examples."""
    print('Oracle3 - Live Paper Trading Examples\n')
    print('This example will run paper trading simulations using live RSS news feeds.')
    print('No real money is involved - all trades are simulated.\n')

    # Run basic example
    await run_rss_paper_trading()

    # Run with risk management
    await run_paper_trading_with_risk_management()


if __name__ == '__main__':
    asyncio.run(main())
