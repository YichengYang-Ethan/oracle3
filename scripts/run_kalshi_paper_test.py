"""
10-minute live paper trading test with Kalshi markets + SimpleStrategy (DeepSeek LLM).

Uses real Kalshi market data + simulated money via PaperTrader.
SimpleStrategy calls DeepSeek to analyze news and make buy/sell/hold decisions.
"""

import asyncio
import logging
import time
from decimal import Decimal

from dotenv import load_dotenv

from oracle3.core.trading_engine import TradingEngine
from oracle3.data.live.kalshi_data_source import LiveKalshiDataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import StandardRiskManager
from oracle3.strategy.simple_strategy import SimpleStrategy
from oracle3.ticker.ticker import CashTicker
from oracle3.trader.paper_trader import PaperTrader

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('kalshi_paper_test')


# ── Main ─────────────────────────────────────────────────────────────
async def main():
    duration = 600  # 10 minutes
    logger.info('=' * 60)
    logger.info('Starting 10-min Kalshi paper trading test (SimpleStrategy + DeepSeek)')
    logger.info('=' * 60)

    # Data source: Kalshi public API
    data_source = LiveKalshiDataSource(
        event_cache_file='/tmp/swm_kalshi_test_events.jsonl',
        polling_interval=30.0,
    )

    # Market data + positions
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.KALSHI_USD,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    # Risk manager
    risk_manager = StandardRiskManager(
        position_manager=position_manager,
        market_data=market_data,
        max_single_trade_size=Decimal('100'),
        max_position_size=Decimal('500'),
        max_total_exposure=Decimal('5000'),
        max_drawdown_pct=Decimal('0.2'),
        initial_capital=Decimal('10000'),
    )

    # Paper trader
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.8'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # LLM Strategy: SimpleStrategy
    strategy = SimpleStrategy(
        trade_size=Decimal('10'),
        confidence_threshold=0.3,
    )

    # Engine (continuous mode for live trading)
    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
        continuous=True,
    )

    # Start data polling
    await data_source.start()
    logger.info('Kalshi data source polling started (interval=30s)')

    start_time = time.time()

    # Run with timeout
    try:
        await asyncio.wait_for(engine.start(), timeout=duration)
    except asyncio.TimeoutError:
        await engine.stop()
        logger.info('Engine stopped after %d seconds (timeout)', duration)

    await data_source.close()

    # ── Summary ──────────────────────────────────────────────────
    elapsed = time.time() - start_time
    logger.info('')
    logger.info('=' * 60)
    logger.info('TEST COMPLETE — %.0f seconds elapsed', elapsed)
    logger.info('=' * 60)
    logger.info('')

    cash = position_manager.get_cash_positions()
    non_cash = position_manager.get_non_cash_positions()
    logger.info('Final portfolio:')
    for p in cash:
        logger.info('  CASH  %s: %s', p.ticker.symbol, p.quantity)
    for p in non_cash:
        if p.quantity != 0:
            logger.info(
                '  POS   %s: qty=%s avg_cost=%s pnl=%s',
                p.ticker.symbol[:30],
                p.quantity,
                p.average_cost,
                p.realized_pnl,
            )
    logger.info('')
    logger.info('Orders placed: %d', len(trader.orders))
    for i, order in enumerate(trader.orders[:20], 1):
        logger.info(
            '  Order %d: %s %s qty=%s filled=%s @ %s status=%s',
            i,
            order.side,
            order.ticker.symbol[:25],
            order.limit_price,
            order.filled_quantity,
            order.average_price,
            order.status,
        )
    logger.info('')

    if elapsed < duration * 0.9:
        logger.warning('Engine exited early (%.0fs < %ds)', elapsed, duration)
    else:
        logger.info('Full duration reached — live trading loop ran successfully')


if __name__ == '__main__':
    asyncio.run(main())
