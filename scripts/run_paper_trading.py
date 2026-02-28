"""
Unified live paper trading script for Kalshi and Polymarket.

Uses real market data + simulated money via PaperTrader.
SimpleStrategy calls DeepSeek LLM to analyze news and make buy/sell/hold decisions.

Usage:
    python scripts/run_paper_trading.py -e kalshi -m          # Kalshi with monitor
    python scripts/run_paper_trading.py -e polymarket -m      # Polymarket with monitor
    python scripts/run_paper_trading.py -e kalshi -d 300      # Kalshi, 5 min
    python scripts/run_paper_trading.py -e polymarket          # Polymarket, log mode
"""

import asyncio
import logging
import time
from decimal import Decimal

import click
from dotenv import load_dotenv

from oracle3.cli.utils import add_monitoring_to_engine
from oracle3.core.trading_engine import TradingEngine
from oracle3.data.composite_data_source import CompositeDataSource
from oracle3.data.live.google_news_data_source import GoogleNewsDataSource
from oracle3.data.live.kalshi_data_source import LiveKalshiDataSource
from oracle3.data.live.live_data_source import (
    LivePolyMarketDataSource,
    LiveRSSNewsDataSource,
)
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import StandardRiskManager
from oracle3.strategy.simple_strategy import SimpleStrategy
from oracle3.ticker.ticker import CashTicker
from oracle3.trader.paper_trader import PaperTrader

load_dotenv()

# Exchange-specific configuration
EXCHANGE_CONFIG = {
    'kalshi': {
        'data_source_cls': LiveKalshiDataSource,
        'cash_ticker': CashTicker.KALSHI_USD,
        'cache_file': '/tmp/swm_kalshi_test_events.jsonl',
        'display_name': 'Kalshi',
    },
    'polymarket': {
        'data_source_cls': LivePolyMarketDataSource,
        'cash_ticker': CashTicker.POLYMARKET_USDC,
        'cache_file': '/tmp/swm_paper_trading_events.jsonl',
        'display_name': 'Polymarket',
    },
}


async def _main(exchange: str, monitor: bool, duration: int) -> None:
    config = EXCHANGE_CONFIG[exchange]

    # ── Logging ──────────────────────────────────────────────────────
    log_level = logging.WARNING if monitor else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )
    logger = logging.getLogger(f'{exchange}_paper_test')

    logger.info('=' * 60)
    logger.info(
        'Starting %s paper trading (SimpleStrategy + DeepSeek)',
        config['display_name'],
    )
    if duration > 0:
        logger.info('Duration: %d seconds', duration)
    else:
        logger.info('Duration: unlimited (Ctrl+C to stop)')
    logger.info('=' * 60)

    # Market data source (Polymarket or Kalshi)
    ds_kwargs: dict = {
        'event_cache_file': config['cache_file'],
        'polling_interval': 30.0,
        'reprocess_on_start': True,
    }
    # orderbook_refresh_interval is only supported by Polymarket
    if exchange == 'polymarket':
        ds_kwargs['orderbook_refresh_interval'] = 10.0
    market_source = config['data_source_cls'](**ds_kwargs)

    # Google News source for real news context
    news_source = GoogleNewsDataSource(
        queries=[
            'polymarket prediction market',
            'cryptocurrency regulation news',
            'US politics elections 2026',
            'geopolitics world events',
            'sports championship odds',
            'economic indicators forecast',
        ],
        cache_file='/tmp/swm_google_news_cache.jsonl',
        polling_interval=300.0,  # Every 5 minutes
        max_articles_per_poll=15,
        max_pages=1,
    )

    # WSJ RSS feeds for professional financial news (free, no API key needed)
    rss_source = LiveRSSNewsDataSource(
        cache_file='/tmp/swm_rss_news_cache.jsonl',
        polling_interval=300.0,
        max_articles_per_poll=10,
        categories=['world', 'business', 'finance', 'politics', 'economy', 'sports'],
    )

    # Combine all data sources
    data_source = CompositeDataSource([market_source, news_source, rss_source])

    # Market data + positions
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=config['cash_ticker'],
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

    # LLM Strategy: SimpleStrategy with DeepSeek
    strategy = SimpleStrategy(
        trade_size=Decimal('10'),
        edge_threshold=Decimal('0.10'),
    )

    # Engine (continuous mode for live trading)
    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
        continuous=True,
    )

    start_time = time.time()

    # Run with or without monitor
    try:
        if monitor:
            monitored = add_monitoring_to_engine(
                engine,
                watch=True,
                refresh_rate=2.0,
                exchange_name=f'{config["display_name"]} + News',
            )
            if duration > 0:
                await asyncio.wait_for(monitored.start(), timeout=duration)
            else:
                await monitored.start()
        else:
            monitored = add_monitoring_to_engine(
                engine,
                watch=False,
                refresh_rate=2.0,
                exchange_name=f'{config["display_name"]} + News',
            )
            if duration > 0:
                await asyncio.wait_for(monitored.start(), timeout=duration)
            else:
                await monitored.start()
    except asyncio.TimeoutError:
        logger.info('Duration limit reached (%ds), stopping...', duration)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.stop()

    # ── Summary ──────────────────────────────────────────────────
    elapsed = time.time() - start_time
    logger.info('')
    logger.info('=' * 60)
    logger.info('SESSION ENDED — %.0f seconds elapsed', elapsed)
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


@click.command()
@click.option(
    '--exchange',
    '-e',
    required=True,
    type=click.Choice(['kalshi', 'polymarket'], case_sensitive=False),
    help='Exchange to paper trade on.',
)
@click.option('--monitor', '-m', is_flag=True, help='Enable live terminal dashboard')
@click.option(
    '--duration',
    '-d',
    default=0,
    type=int,
    help='Duration in seconds (0 = run forever, default: 0)',
)
def main(exchange: str, monitor: bool, duration: int) -> None:
    """Run paper trading with DeepSeek LLM strategy on Kalshi or Polymarket."""
    try:
        asyncio.run(_main(exchange, monitor, duration))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
