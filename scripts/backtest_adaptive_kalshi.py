#!/usr/bin/env python3
"""Backtest AdaptiveOnChainStrategy on Solana prediction market data.

Supports PredictionMarketBench episodes and recorded DFlow WS sessions.

Usage:
    python scripts/backtest_adaptive_kalshi.py /tmp/PredictionMarketBench/episodes/KXBTCD-26JAN2017
    python scripts/backtest_adaptive_kalshi.py data/episodes/dflow_session --max-events 5000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from oracle3.core.trading_engine import TradingEngine
from oracle3.data.backtest.kalshi_replay_data_source import SolanaReplayDataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.strategy.contrib.adaptive_onchain_strategy import AdaptiveOnChainStrategy
from oracle3.ticker.ticker import CashTicker
from oracle3.trader.paper_trader import PaperTrader

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s  %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


async def run(
    episode_dir: str,
    max_events: int | None = None,
    initial_capital: float = 10000.0,
    **strategy_kwargs,
) -> None:
    # Data source
    data_source = SolanaReplayDataSource(episode_dir, max_events=max_events)
    if not data_source.events:
        logger.error('No events loaded from %s', episode_dir)
        return

    # Market data (no synthetic spread — we have real OB)
    market_data = MarketDataManager(
        spread=Decimal('0'),
        max_history_per_ticker=None,
        max_timeline_events=None,
    )

    # Portfolio
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.DFLOW_USDC,
            quantity=Decimal(str(initial_capital)),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    # Trader
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.8'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # Strategy
    strategy = AdaptiveOnChainStrategy(**strategy_kwargs)

    # Engine
    engine = TradingEngine(data_source=data_source, strategy=strategy, trader=trader)

    logger.info(
        'Starting backtest: %d events, %d tickers, $%.0f capital',
        len(data_source.events), len(data_source.get_tickers()), initial_capital,
    )
    await engine.start()

    # Results
    engine._perf.print_summary()
    stats = strategy.get_decision_stats()
    logger.info('Decision stats: %s', stats)

    # Print adaptation history
    if strategy._trade_outcomes:
        wins = sum(1 for o in strategy._trade_outcomes if o['profitable'])
        total = len(strategy._trade_outcomes)
        logger.info(
            'Trade outcomes: %d/%d profitable (%.1f%%)',
            wins, total, wins / total * 100 if total else 0,
        )
    logger.info(
        'Final weights: w_ob=%.3f w_momentum=%.3f threshold=%.3f pos_size=%s',
        strategy.w_ob, strategy.w_momentum,
        strategy.composite_threshold, strategy.position_size,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Backtest adaptive strategy on Solana market data')
    parser.add_argument('episode_dir', help='Path to PredictionMarketBench episode directory')
    parser.add_argument('--max-events', type=int, default=None, help='Limit events to process')
    parser.add_argument('--capital', type=float, default=10000.0, help='Initial capital')
    parser.add_argument('--composite-threshold', type=float, default=0.10)
    parser.add_argument('--adapt-window', type=int, default=10)
    parser.add_argument('--position-size', type=float, default=10.0)
    parser.add_argument('--w-ob', type=float, default=0.6)
    parser.add_argument('--w-momentum', type=float, default=0.4)
    args = parser.parse_args()

    asyncio.run(run(
        episode_dir=args.episode_dir,
        max_events=args.max_events,
        initial_capital=args.capital,
        composite_threshold=args.composite_threshold,
        adapt_window=args.adapt_window,
        position_size=args.position_size,
        w_ob=args.w_ob,
        w_momentum=args.w_momentum,
    ))


if __name__ == '__main__':
    main()
