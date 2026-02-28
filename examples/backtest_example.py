#!/usr/bin/env python3
"""
Backtest Example

This example demonstrates how to run a backtest with the Oracle3 framework.
It shows how to:
1. Set up a historical data source
2. Configure a trading strategy
3. Run a backtest simulation
4. Analyze the results
"""

import asyncio
import os
from decimal import Decimal

from oracle3.core.trading_engine import TradingEngine
from oracle3.data.backtest.historical_data_source import HistoricalDataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager, StandardRiskManager
from oracle3.strategy.test_strategy import TestStrategy
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader


async def run_basic_backtest():
    """Run a basic backtest with default settings."""
    print('=' * 60)
    print('Running Basic Backtest')
    print('=' * 60)

    # Configuration
    initial_capital = Decimal('10000')

    # Create a test ticker for the market we're trading
    ticker = PolyMarketTicker(
        symbol='poly_test',
        name='Test Market',
        market_id='514893',
        event_id='15088',
    )

    # Set up the historical data source
    # Note: You'll need to have historical data in this file
    current_dir = os.path.dirname(os.path.abspath(__file__))
    data_file = os.path.join(
        current_dir,
        '..',
        'oracle3',
        'backtest',
        'polymarket_data_processed_Crypto_test.jsonl',
    )

    if not os.path.exists(data_file):
        print(f'Warning: Data file not found at {data_file}')
        print('Please ensure you have historical data available.')
        return

    data_source = HistoricalDataSource(data_file, ticker)

    # Set up market data manager
    market_data = MarketDataManager()

    # Set up position manager with initial capital
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_capital,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    # Set up risk manager (using no restrictions for this example)
    risk_manager = NoRiskManager()

    # Set up paper trader for simulation
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # Set up strategy
    strategy = TestStrategy()

    # Create trading engine
    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
    )

    # Run the backtest
    print('\nStarting backtest...')
    await engine.start()

    # Print results
    print('\n' + '=' * 60)
    print('Backtest Results')
    print('=' * 60)
    print(f'\nInitial Capital: ${initial_capital:,.2f}')

    cash_positions = position_manager.get_cash_positions()
    for pos in cash_positions:
        print(f'Final Cash ({pos.ticker.name}): ${pos.quantity:,.2f}')

    non_cash = position_manager.get_non_cash_positions()
    print(f'\nOpen Positions: {len(non_cash)}')
    for pos in non_cash:
        print(
            f'  {pos.ticker.symbol}: {pos.quantity} @ avg cost {pos.average_cost:.4f}'
        )

    total_realized = position_manager.get_total_realized_pnl()
    print(f'\nTotal Realized PnL: ${total_realized:,.2f}')

    print('\nBacktest complete!')


async def run_backtest_with_risk_management():
    """Run a backtest with risk management enabled."""
    print('\n' + '=' * 60)
    print('Running Backtest with Risk Management')
    print('=' * 60)

    initial_capital = Decimal('10000')

    ticker = PolyMarketTicker(
        symbol='poly_test',
        name='Test Market',
        market_id='514893',
        event_id='15088',
    )

    current_dir = os.path.dirname(os.path.abspath(__file__))
    data_file = os.path.join(
        current_dir,
        '..',
        'oracle3',
        'backtest',
        'polymarket_data_processed_Crypto_test.jsonl',
    )

    if not os.path.exists(data_file):
        print(f'Warning: Data file not found at {data_file}')
        return

    data_source = HistoricalDataSource(data_file, ticker)
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

    # Use StandardRiskManager with conservative limits
    risk_manager = StandardRiskManager(
        position_manager=position_manager,
        market_data=market_data,
        max_single_trade_size=Decimal('500'),  # Max $500 per trade
        max_position_size=Decimal('2000'),  # Max $2000 per position
        max_total_exposure=Decimal('8000'),  # Max 80% of capital exposed
        max_drawdown_pct=Decimal('0.15'),  # Stop trading at 15% drawdown
        max_positions=5,  # Max 5 open positions
        initial_capital=initial_capital,
    )

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.01'),  # 1% commission
    )

    strategy = TestStrategy()

    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
    )

    print('\nStarting backtest with risk management...')
    print('Max trade size: $500')
    print('Max position size: $2,000')
    print('Max total exposure: $8,000')
    print('Max drawdown: 15%')

    await engine.start()

    print('\n' + '=' * 60)
    print('Backtest Results (with Risk Management)')
    print('=' * 60)

    cash_positions = position_manager.get_cash_positions()
    for pos in cash_positions:
        print(f'Final Cash: ${pos.quantity:,.2f}')

    total_realized = position_manager.get_total_realized_pnl()
    print(f'Total Realized PnL: ${total_realized:,.2f}')

    # Check drawdown
    current_drawdown = risk_manager.get_current_drawdown()
    print(f'Current Drawdown: {current_drawdown * 100:.2f}%')

    print('\nBacktest complete!')


if __name__ == '__main__':
    print('Oracle3 - Backtest Example\n')

    asyncio.run(run_basic_backtest())
    asyncio.run(run_backtest_with_risk_management())
