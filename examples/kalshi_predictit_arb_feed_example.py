#!/usr/bin/env python3
"""
Kalshi <-> PredictIt Arbitrage Feed Example
===========================================

Feeds an external cross-venue arbitrage scanner into the Oracle3 engine as
``NewsEvent`` signals and records them with a strategy that places no orders.

  ArbScanner --> KalshiPredictItArbDataSource --> TradingEngine
                                                     |
                                                     v
                                          ArbFeedSignalStrategy
                                        (records decisions only)

Runs offline by default: without ``X402_WALLET_KEY`` the scanner returns
bundled SAMPLE data (``data/kalshi_predictit_arb_sample.json``, not real
market data) and makes no network calls.

Setting ``X402_WALLET_KEY`` opts in to the paid live feed (x402 v2, USDC on
Base, at most ``max_usd_per_call`` per request). Only use a dedicated
low-balance wallet.

Usage
-----
    python examples/kalshi_predictit_arb_feed_example.py
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from examples.strategies.kalshi_predictit_arb_feed import (
    ArbFeedSignalStrategy,
    ArbScanner,
    KalshiPredictItArbDataSource,
)
from oracle3.core.trading_engine import TradingEngine
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.ticker.ticker import CashTicker
from oracle3.trader.paper_trader import PaperTrader

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s  %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


def _build_paper_trader() -> PaperTrader:
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.KALSHI_USD,
            quantity=Decimal('1000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return PaperTrader(
        market_data=MarketDataManager(),
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )


async def main() -> None:
    scanner = ArbScanner(max_usd_per_call=0.02)
    if scanner.demo:
        print('Demo mode: SAMPLE data, no network calls (set X402_WALLET_KEY for live)')
    else:
        print('LIVE mode: each poll pays up to $0.02 USDC on Base')
    data_source = KalshiPredictItArbDataSource(
        scanner=scanner,
        mode='all',
        limit=10,
        polling_interval=300.0,
        max_polls=1,  # one call, then the engine stops
    )
    strategy = ArbFeedSignalStrategy(min_net_yield_c=1.0)
    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=_build_paper_trader(),
        continuous=False,
    )
    await engine.start()

    print()
    for decision in strategy.get_decisions():
        print(f'  {decision.action:<10} {decision.signal_values} {decision.reasoning}')
    print(f'\n  Stats: {strategy.get_decision_stats()}')


if __name__ == '__main__':
    asyncio.run(main())
