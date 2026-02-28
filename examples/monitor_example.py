"""Example of integrating the trading monitor with a live trading engine.

This example shows how to run a trading engine with live monitoring.
"""

from oracle3.cli.monitor import TradingMonitor
from oracle3.core.trading_engine import TradingEngine
from oracle3.data.data_source import DataSource
from oracle3.strategy.strategy import Strategy
from oracle3.trader.trader import Trader


async def run_trading_with_monitor(
    data_source: DataSource,
    strategy: Strategy,
    trader: Trader,
    watch_mode: bool = False,
    refresh_rate: float = 2.0,
) -> None:
    """Run trading engine with monitoring.

    Args:
        data_source: Data source for market events
        strategy: Trading strategy
        trader: Trader implementation (paper or live)
        watch_mode: Enable live monitoring mode
        refresh_rate: Refresh rate for watch mode in seconds
    """
    # Create trading engine
    TradingEngine(data_source=data_source, strategy=strategy, trader=trader)

    # Create monitor
    monitor = TradingMonitor(trader=trader, position_manager=trader.position_manager)

    if watch_mode:
        # Run trading engine in background with live monitoring
        # This would require running the engine in a separate thread/task
        # For now, we'll just show the monitor
        print('Starting live monitoring mode...')
        print('Note: Integrate with your trading engine loop for full functionality')
        monitor.display_live(refresh_rate=refresh_rate)
    else:
        # Display a single snapshot
        print('Displaying current trading state...')
        monitor.display_snapshot()


def main() -> None:
    """Main example entry point."""
    # Example setup (you would replace this with your actual configuration)

    # 1. Initialize your data source
    # from oracle3.data.live.polymarket_data_source import PolymarketDataSource
    # data_source = PolymarketDataSource(...)

    # 2. Initialize your strategy
    # from oracle3.strategy.simple_strategy import SimpleStrategy
    # strategy = SimpleStrategy(...)

    # 3. Initialize your trader
    # For paper trading:
    # from oracle3.ticker.ticker import CashTicker
    # from oracle3.trader.paper_trader import PaperTrader
    # initial_cash = {CashTicker('USDC'): Decimal('10000')}
    # trader = PaperTrader(initial_cash=initial_cash)

    # For live trading:
    # from oracle3.trader.polymarket_trader import PolymarketTrader
    # trader = PolymarketTrader(...)

    # 4. Run with monitoring
    # asyncio.run(run_trading_with_monitor(
    #     data_source=data_source,
    #     strategy=strategy,
    #     trader=trader,
    #     watch_mode=True,  # Enable live monitoring
    #     refresh_rate=2.0  # Update every 2 seconds
    # ))

    print('This is an example integration script.')
    print('Uncomment and configure the code above to run with your trading setup.')
    print('\nFor CLI usage:')
    print('  oracle3 monitor              # Single snapshot')
    print('  oracle3 monitor --watch      # Live updates')
    print('  oracle3 monitor -w -r 1.0    # Live with 1s refresh')


if __name__ == '__main__':
    main()
