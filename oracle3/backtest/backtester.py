import asyncio
import logging
import os
from decimal import Decimal

from oracle3.core.trading_engine import TradingEngine
from oracle3.data.backtest.historical_data_source import HistoricalDataSource
from oracle3.data.live.live_data_source import LiveNewsDataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.strategy.strategy import Strategy
from oracle3.strategy.test_strategy import TestStrategy
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


async def run_live_simulation(
    live_data_source: LiveNewsDataSource,
    ticker_symbol: PolyMarketTicker,
    initial_capital: Decimal,
    strategy: Strategy,
) -> None:
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
    risk_manager = NoRiskManager()

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    engine = TradingEngine(
        data_source=live_data_source, strategy=strategy, trader=trader
    )

    # Run the trading engine
    await engine.start()
    logger.info('Live simulation stopped.')


async def run_backtest(
    history_file: str,
    ticker_symbol: PolyMarketTicker,
    initial_capital: Decimal,
    strategy: Strategy,
    spread: Decimal = Decimal('0.01'),
    *,
    include_all_markets_context: bool = False,
    allow_cross_market_trading: bool = False,
) -> None:
    data_source = HistoricalDataSource(
        history_file,
        ticker_symbol,
        include_all_markets=include_all_markets_context,
    )
    market_data = MarketDataManager(
        spread=spread,
        max_history_per_ticker=None,
        max_timeline_events=None,
    )
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_capital,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    risk_manager = NoRiskManager()

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )
    if not allow_cross_market_trading:
        tradable_tickers = [ticker_symbol]
        no_ticker = ticker_symbol.get_no_ticker()
        if no_ticker is not None:
            tradable_tickers.append(no_ticker)
        trader.set_allowed_tickers(tradable_tickers)

    engine = TradingEngine(data_source=data_source, strategy=strategy, trader=trader)

    await engine.start()
    engine._perf.print_summary()
    logger.info('Backtest complete.')


if __name__ == '__main__':
    current_dir = os.path.dirname(os.path.abspath(__file__))
    asyncio.run(
        run_backtest(
            os.path.join(current_dir, 'polymarket_data_processed_Crypto_test.jsonl'),
            PolyMarketTicker(
                symbol='poly_test',
                name='test_ticker',
                market_id='514893',
                event_id='15088',
            ),
            Decimal('10000'),
            TestStrategy(),
        )
    )
