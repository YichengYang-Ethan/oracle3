#!/usr/bin/env python3
"""
Cross-Platform Arbitrage Example
=================================

Demonstrates how to run a cross-platform arbitrage strategy between
Polymarket and Kalshi using the Oracle3 framework.

Architecture
------------
  LivePolyMarketDataSource ─┐
                             ├─ CompositeDataSource ─> TradingEngine
  LiveKalshiDataSource ──────┘                              │
                                                            ▼
                                              CrossPlatformArbStrategy
                                                            │
                                                            ▼
  PaperTrader(poly) ─────┐                          CompositeTrader
  PaperTrader(kalshi) ───┘                        (routes by ticker type)

Requirements
------------
- Polymarket API access (CLOB client, no auth needed for public data)
- Kalshi API key (set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH env vars)
- ``pip install oracle3``

Usage
-----
    python examples/cross_platform_arb_example.py

No real money is involved — all trades are simulated via PaperTrader.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from examples.strategies.cross_platform_arb_strategy import (
    CompositeTrader,
    CrossPlatformArbStrategy,
    MarketMatcher,
)
from oracle3.core.trading_engine import TradingEngine
from oracle3.data.composite_data_source import CompositeDataSource
from oracle3.data.live.kalshi_data_source import LiveKalshiDataSource
from oracle3.data.live.live_data_source import LivePolyMarketDataSource
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


def _build_paper_trader(
    market_data: MarketDataManager,
    initial_capital: Decimal,
    cash_ticker: CashTicker,
) -> PaperTrader:
    """Create a PaperTrader seeded with initial capital."""
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=cash_ticker,
            quantity=initial_capital,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.8'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )


async def run_cross_platform_arb(duration: float = 300) -> None:
    """Run the cross-platform arbitrage demo.

    Parameters
    ----------
    duration:
        How long to run the paper trading session (seconds).
        Default 300 s (5 min).
    """
    print('=' * 64)
    print('  Cross-Platform Arbitrage — Paper Trading Demo')
    print('=' * 64)

    initial_capital = Decimal('5000')

    # ── Data sources ──────────────────────────────────────────────
    poly_ds = LivePolyMarketDataSource(
        event_cache_file='arb_poly_cache.jsonl',
        polling_interval=30.0,
        orderbook_refresh_interval=10.0,
    )
    kalshi_ds = LiveKalshiDataSource(
        event_cache_file='arb_kalshi_cache.jsonl',
        polling_interval=60.0,
    )
    composite_ds = CompositeDataSource(sources=[poly_ds, kalshi_ds])

    # ── Shared MarketDataManager ──────────────────────────────────
    market_data = MarketDataManager()

    # ── Paper traders (one per platform) ──────────────────────────
    poly_trader = _build_paper_trader(
        market_data, initial_capital, CashTicker.POLYMARKET_USDC
    )
    kalshi_trader = _build_paper_trader(
        market_data, initial_capital, CashTicker.KALSHI_USD
    )
    composite_trader = CompositeTrader(
        poly_trader=poly_trader,
        kalshi_trader=kalshi_trader,
    )

    # ── Strategy ──────────────────────────────────────────────────
    matcher = MarketMatcher(min_similarity=0.55)
    strategy = CrossPlatformArbStrategy(
        matcher=matcher,
        min_edge=0.02,  # 2 cent minimum edge
        trade_size=Decimal('10'),  # 10 shares per leg
        cooldown_seconds=30,  # 30 s between arb attempts
    )

    print()
    print(f'  Strategy:        {strategy.name} v{strategy.version}')
    print(f'  Min edge:        {strategy.min_edge}')
    print(f'  Trade size:      {strategy.trade_size} shares/leg')
    print(f'  Cooldown:        {strategy.cooldown_seconds}s')
    print(f'  Capital/platform: ${initial_capital:,.2f}')
    print(f'  Duration:        {duration}s')
    print()

    # ── Engine ────────────────────────────────────────────────────
    engine = TradingEngine(
        data_source=composite_ds,
        strategy=strategy,
        trader=composite_trader,
        continuous=True,
    )

    print('Starting engine — streaming events from Polymarket + Kalshi …')
    print('(Press Ctrl+C to stop early)\n')

    try:
        await asyncio.wait_for(engine.start(), timeout=duration)
    except asyncio.TimeoutError:
        engine.running = False
        print(f'\nSession ended after {duration}s')
    except KeyboardInterrupt:
        engine.running = False
        print('\nStopped by user')
    finally:
        # ── Summary ───────────────────────────────────────────────
        print()
        print('─' * 64)
        print('  Session Summary')
        print('─' * 64)

        decisions = strategy.get_decisions()
        arbs = [d for d in decisions if d.get('action') == 'ARB_BOTH_YES']
        holds = [d for d in decisions if d.get('action') == 'HOLD']

        print(f'  Total decisions:   {len(decisions)}')
        print(f'  Arb opportunities: {len(arbs)}')
        print(f'  Hold (no arb):     {len(holds)}')

        poly_positions = poly_trader.position_manager.get_all_positions()
        kalshi_positions = kalshi_trader.position_manager.get_all_positions()
        print(f'  Poly positions:    {len(poly_positions)}')
        print(f'  Kalshi positions:  {len(kalshi_positions)}')
        print()


async def main() -> None:
    print('Oracle3 — Cross-Platform Arbitrage Example\n')
    print('This example streams live data from both Polymarket and Kalshi,')
    print('matches equivalent markets by name, and places simulated arb')
    print('trades when a price discrepancy exceeds the minimum edge.\n')
    print('No real money is involved — all trades use PaperTrader.\n')

    await run_cross_platform_arb(duration=300)


if __name__ == '__main__':
    asyncio.run(main())
