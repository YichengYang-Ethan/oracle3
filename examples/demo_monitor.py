"""Demonstration of the monitoring CLI with a simulated trading session.

This script creates a paper trading session with simulated market data
to demonstrate the monitoring capabilities.

Usage:
    python examples/demo_monitor.py              # Single snapshot after simulation
    python examples/demo_monitor.py --watch      # Live monitoring during simulation
    python examples/demo_monitor.py -w -r 1.0    # Live with 1 second refresh
"""

import asyncio
import random
from decimal import Decimal

import click

from oracle3.cli.monitor import TradingMonitor
from oracle3.ticker.ticker import CashTicker, Ticker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.types import TradeSide


async def simulate_trading_session(trader: PaperTrader, num_trades: int = 10) -> None:
    """Simulate a trading session with random trades.

    Args:
        trader: Paper trader instance
        num_trades: Number of simulated trades to execute
    """
    # Define some demo tickers
    tickers = [
        Ticker(
            symbol='TRUMP-YES-2024',
            token_id='1',
            condition_id='cond1',
            collateral=CashTicker('USDC'),
        ),
        Ticker(
            symbol='BIDEN-NO-2024',
            token_id='2',
            condition_id='cond2',
            collateral=CashTicker('USDC'),
        ),
        Ticker(
            symbol='BTC-100K',
            token_id='3',
            condition_id='cond3',
            collateral=CashTicker('USDC'),
        ),
    ]

    print(f'\nSimulating {num_trades} trades...\n')

    for i in range(num_trades):
        # Random trade parameters
        ticker = random.choice(tickers)
        side = random.choice([TradeSide.BUY, TradeSide.SELL])

        # Only sell if we have a position
        if side == TradeSide.SELL:
            pos = trader.position_manager.get_position(ticker)
            if pos is None or pos.quantity <= 0:
                side = TradeSide.BUY  # Switch to buy if no position

        # Random price between 0.1 and 0.9
        price = Decimal(str(round(random.uniform(0.1, 0.9), 4)))

        # Random quantity between 10 and 100
        quantity = Decimal(str(random.randint(10, 100)))

        print(
            f'Trade {i + 1}: {side.value.upper()} {quantity} {ticker.symbol} @ ${price}'
        )

        # Place order
        result = await trader.place_order(
            side=side, ticker=ticker, limit_price=price, quantity=quantity
        )

        if result.order:
            print(
                f'  → Order {result.order.status.value}: Filled {result.order.filled_quantity}'
            )
        else:
            print(
                f'  → Order FAILED: {result.failure_reason.value if result.failure_reason else "Unknown"}'
            )

        # Simulate some delay between trades
        await asyncio.sleep(0.5)

    print('\nTrading simulation complete!\n')


async def run_demo(watch: bool, refresh_rate: float) -> None:
    """Run the monitoring demo.

    Args:
        watch: Enable live watch mode
        refresh_rate: Refresh rate for watch mode
    """
    # Initialize paper trader with initial cash
    initial_cash = {CashTicker('USDC'): Decimal('10000')}
    trader = PaperTrader(
        initial_cash=initial_cash,
        min_fill_rate=0.8,  # 80-100% fill rate
        max_fill_rate=1.0,
        commission_rate=Decimal('0.01'),  # 1% commission
    )

    # Create monitor
    monitor = TradingMonitor(trader=trader, position_manager=trader.position_manager)

    if watch:
        # For watch mode, we need to run simulation in background
        print('Starting live monitoring mode...')
        print('Trading simulation will run in the background.\n')

        # Start simulation task
        simulation_task = asyncio.create_task(simulate_trading_session(trader, 20))

        # Give simulation a moment to start
        await asyncio.sleep(1)

        try:
            # Run monitor in foreground (will update as trades execute)
            # Note: This is a simplified version. For production, you'd want
            # proper task coordination
            monitor.display_live(refresh_rate=refresh_rate)
        except KeyboardInterrupt:
            print('\nMonitoring stopped by user')
        finally:
            # Wait for simulation to complete
            await simulation_task
    else:
        # Run simulation then show snapshot
        await simulate_trading_session(trader, 10)

        print('=== Trading Session Complete ===\n')
        print('Displaying final portfolio snapshot:\n')
        monitor.display_snapshot()


@click.command()
@click.option(
    '--watch',
    '-w',
    is_flag=True,
    help='Enable live monitoring mode during simulation',
)
@click.option(
    '--refresh',
    '-r',
    default=2.0,
    type=float,
    help='Refresh rate in seconds for watch mode (default: 2.0)',
)
def main(watch: bool, refresh: float) -> None:
    """Run a trading monitoring demonstration with simulated trades.

    This demo creates a paper trading session with random trades to showcase
    the monitoring capabilities.

    Examples:
        python examples/demo_monitor.py              # Snapshot after trades
        python examples/demo_monitor.py --watch      # Live monitoring
        python examples/demo_monitor.py -w -r 1.0    # Live with 1s refresh
    """
    print('=== Oracle3 Trading Monitor Demo ===')
    print('This demo simulates a trading session with random trades.')
    print('The monitor will display portfolio, positions, orders, and statistics.\n')

    asyncio.run(run_demo(watch, refresh))


if __name__ == '__main__':
    main()
