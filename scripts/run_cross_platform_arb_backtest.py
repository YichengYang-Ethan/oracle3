#!/usr/bin/env python3
"""Backtest the CrossPlatformArbStrategy using real Polymarket data + synthetic Kalshi prices.

Since Kalshi does not expose historical price data through its public API,
we simulate Kalshi prices by adding realistic noise, lag, and mean-reverting
spread to Polymarket prices.  This models the empirical observation that
cross-platform prediction markets:

  1. Track the same underlying probability but with different liquidity profiles.
  2. Have a mean-reverting spread that widens during fast-moving events.
  3. Show 1-5 minute lag on the less-liquid platform (Kalshi).

Usage:
    python scripts/run_cross_platform_arb_backtest.py [--data data/cross_platform/matched.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracle3.core.trading_engine import TradingEngine
from oracle3.data.data_source import DataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import Event, PriceChangeEvent
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.ticker.ticker import CashTicker, KalshiTicker, PolyMarketTicker, Ticker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

from examples.strategies.cross_platform_arb_strategy import (
    CompositeTrader,
    CrossPlatformArbStrategy,
    MarketMatcher,
    MatchedMarket,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-7s %(name)s  %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Synthetic Kalshi price generator
# ---------------------------------------------------------------------------


def generate_synthetic_kalshi_prices(
    poly_series: list[dict],
    *,
    base_spread: float = 0.02,
    spread_vol: float = 0.01,
    lag_steps: int = 3,
    noise_std: float = 0.008,
    seed: int = 42,
) -> list[dict]:
    """Generate synthetic Kalshi YES prices from Polymarket YES prices.

    Model:
        kalshi_yes[t] = poly_yes[t - lag] + spread[t] + noise[t]
        spread[t] = base_spread * mean_revert + random_walk

    Parameters
    ----------
    poly_series:  [{"t": epoch, "p": float}, ...]
    base_spread:  Mean cross-platform spread (positive = Kalshi > Poly).
    spread_vol:   Volatility of the spread process.
    lag_steps:    How many data points Kalshi lags behind Polymarket.
    noise_std:    Idiosyncratic noise standard deviation.
    seed:         Random seed for reproducibility.
    """
    rng = random.Random(seed)
    n = len(poly_series)
    if n == 0:
        return []

    result = []
    spread = base_spread
    mean_revert_rate = 0.05  # speed at which spread reverts to base

    for i in range(n):
        # Kalshi follows Polymarket with a lag
        src_idx = max(0, i - lag_steps)
        poly_p = poly_series[src_idx]['p']

        # Mean-reverting spread + noise
        innovation = rng.gauss(0, spread_vol)
        spread = spread + mean_revert_rate * (base_spread - spread) + innovation
        noise = rng.gauss(0, noise_std)

        kalshi_p = poly_p + spread + noise
        # Clamp to valid range [0.01, 0.99]
        kalshi_p = max(0.01, min(0.99, kalshi_p))

        result.append(
            {
                't': poly_series[i]['t'],
                'p': round(kalshi_p, 4),
            }
        )

    return result


# ---------------------------------------------------------------------------
# Cross-platform historical data source
# ---------------------------------------------------------------------------


class CrossPlatformHistoricalDataSource(DataSource):
    """Replay interleaved Polymarket + Kalshi PriceChangeEvents."""

    def __init__(
        self,
        events: list[Event],
    ) -> None:
        self.events = events
        self.index = 0

    async def start(self) -> None:
        pass

    async def get_next_event(self) -> Event | None:
        if self.index < len(self.events):
            ev = self.events[self.index]
            self.index += 1
            return ev
        return None


def build_events_from_data(
    data_path: str,
    *,
    base_spread: float = 0.02,
    spread_vol: float = 0.01,
    lag_steps: int = 3,
    noise_std: float = 0.008,
    max_matches: int = 20,
    seed: int = 42,
) -> tuple[list[Event], MarketMatcher, dict]:
    """Load crawled data, generate synthetic Kalshi prices, build event list.

    Returns (events, matcher, stats).
    """
    rows = []
    with open(data_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    # Group by match_id
    by_match: dict[str, dict] = {}
    for row in rows:
        mid = row.get('match_id', '')
        if mid not in by_match:
            by_match[mid] = {}
        by_match[mid][row['platform']] = row

    events: list[Event] = []
    matcher = MarketMatcher(min_similarity=0.0)  # We'll manually add matches
    stats = {'matches': 0, 'poly_points': 0, 'kalshi_points': 0, 'arb_windows': 0}

    for match_idx, (mid, sides) in enumerate(by_match.items()):
        if match_idx >= max_matches:
            break
        poly_row = sides.get('polymarket')
        kalshi_row = sides.get('kalshi')
        if not poly_row:
            continue

        poly_series = poly_row.get('time_series', {}).get('Yes', [])
        if not poly_series or len(poly_series) < 10:
            continue

        # Build tickers
        pt = poly_row.get('ticker', {})
        poly_ticker = PolyMarketTicker(
            symbol=pt.get('token_id', pt.get('symbol', f'POLY_{match_idx}')),
            name=poly_row.get('question', ''),
            token_id=pt.get('token_id', ''),
            no_token_id=pt.get('no_token_id', ''),
            market_id=pt.get('market_id', str(match_idx)),
            event_id=pt.get('event_id', str(match_idx)),
        )

        kt_data = (kalshi_row or {}).get('ticker', {})
        kalshi_ticker = KalshiTicker(
            symbol=kt_data.get(
                'market_ticker', kt_data.get('symbol', f'KALSHI_{match_idx}')
            ),
            name=(kalshi_row or {}).get('question', poly_row.get('question', '')),
            market_ticker=kt_data.get('market_ticker', ''),
            event_ticker=kt_data.get('event_ticker', ''),
        )

        # Register match in the matcher
        matched = MatchedMarket(
            poly_ticker=poly_ticker,
            kalshi_ticker=kalshi_ticker,
            similarity=1.0,
            label=poly_row.get('question', '')[:60],
        )
        matcher._matches[poly_ticker.symbol] = matched
        stats['matches'] += 1

        # Generate synthetic Kalshi prices
        kalshi_series = generate_synthetic_kalshi_prices(
            poly_series,
            base_spread=base_spread,
            spread_vol=spread_vol,
            lag_steps=lag_steps,
            noise_std=noise_std,
            seed=seed + match_idx,
        )

        # Build interleaved events
        for pt_data in poly_series:
            ts = pt_data['t']
            price = pt_data['p']
            dt = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if isinstance(ts, (int, float))
                else ts
            )
            events.append(
                PriceChangeEvent(
                    ticker=poly_ticker,
                    price=Decimal(str(price)),
                    timestamp=dt,
                )
            )
        stats['poly_points'] += len(poly_series)

        for kt_data_point in kalshi_series:
            ts = kt_data_point['t']
            price = kt_data_point['p']
            dt = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if isinstance(ts, (int, float))
                else ts
            )
            events.append(
                PriceChangeEvent(
                    ticker=kalshi_ticker,
                    price=Decimal(str(price)),
                    timestamp=dt,
                )
            )
        stats['kalshi_points'] += len(kalshi_series)

        # Count arb windows (where poly + kalshi YES < 0.98 or > 1.02)
        for i in range(len(poly_series)):
            if i < len(kalshi_series):
                total = poly_series[i]['p'] + kalshi_series[i]['p']
                if total < 0.98 or total > 1.02:
                    stats['arb_windows'] += 1

    # Sort all events by timestamp
    events.sort(
        key=lambda e: (
            e.timestamp.timestamp()
            if isinstance(e.timestamp, datetime)
            else float(e.timestamp)
        )
    )

    return events, matcher, stats


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------


async def run_backtest(
    data_path: str,
    *,
    initial_capital: Decimal = Decimal('10000'),
    min_edge: float = 0.02,
    trade_size: Decimal = Decimal('10'),
    cooldown: int = 0,
    base_spread: float = 0.02,
    spread_vol: float = 0.01,
    lag_steps: int = 3,
    noise_std: float = 0.008,
    max_matches: int = 20,
    seed: int = 42,
) -> dict:
    """Run cross-platform arb backtest and return metrics."""

    print('Loading data and generating synthetic Kalshi prices...')
    events, matcher, data_stats = build_events_from_data(
        data_path,
        base_spread=base_spread,
        spread_vol=spread_vol,
        lag_steps=lag_steps,
        noise_std=noise_std,
        max_matches=max_matches,
        seed=seed,
    )

    if not events:
        print('ERROR: No events loaded.')
        return {'error': 'No events loaded'}

    print(f'  Matched markets:   {data_stats["matches"]}')
    print(f'  Polymarket points: {data_stats["poly_points"]:,}')
    print(f'  Kalshi points:     {data_stats["kalshi_points"]:,}')
    print(f'  Total events:      {len(events):,}')
    print(f'  Arb windows (2%):  {data_stats["arb_windows"]:,}')
    print()

    # Build components
    data_source = CrossPlatformHistoricalDataSource(events)
    market_data = MarketDataManager(spread=Decimal('0.01'))

    # Two paper traders — one per platform
    def make_trader(cash_ticker: CashTicker) -> PaperTrader:
        pm = PositionManager()
        pm.update_position(
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
            position_manager=pm,
            min_fill_rate=Decimal('0.8'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.0'),
        )

    poly_trader = make_trader(CashTicker.POLYMARKET_USDC)
    kalshi_trader = make_trader(CashTicker.KALSHI_USD)
    composite_trader = CompositeTrader(
        poly_trader=poly_trader,
        kalshi_trader=kalshi_trader,
    )

    strategy = CrossPlatformArbStrategy(
        matcher=matcher,
        min_edge=min_edge,
        trade_size=trade_size,
        cooldown_seconds=cooldown,
    )

    print(f'Strategy: {strategy.name} v{strategy.version}')
    print(f'  min_edge={min_edge}, trade_size={trade_size}, cooldown={cooldown}s')
    print(
        f'  Synthetic spread: base={base_spread}, vol={spread_vol}, lag={lag_steps}, noise={noise_std}'
    )
    print(f'  Capital/platform: ${initial_capital:,.2f}')
    print()

    # Run engine
    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=composite_trader,
        continuous=False,
    )

    print('Running backtest...')
    await engine.start()
    print('Backtest complete.\n')

    # Collect results
    decisions = strategy.get_decisions()
    arb_trades = [d for d in decisions if d.action == 'ARB_BOTH_YES']
    reverse_arbs = [d for d in decisions if d.action == 'ARB_REVERSE']
    holds = [d for d in decisions if d.action == 'HOLD']

    # Calculate PnL from positions
    poly_positions = list(poly_trader.position_manager.positions.values())
    kalshi_positions = list(kalshi_trader.position_manager.positions.values())

    poly_cash = Decimal('0')
    kalshi_cash = Decimal('0')
    poly_market_positions = 0
    kalshi_market_positions = 0

    for pos in poly_positions:
        if isinstance(pos.ticker, CashTicker):
            poly_cash = pos.quantity
        else:
            poly_market_positions += 1

    for pos in kalshi_positions:
        if isinstance(pos.ticker, CashTicker):
            kalshi_cash = pos.quantity
        else:
            kalshi_market_positions += 1

    total_cash = poly_cash + kalshi_cash
    pnl = total_cash - (
        initial_capital * 2
    )  # started with initial_capital on each side

    # Edge statistics from arb trades
    edges = []
    for d in arb_trades:
        sv = d.signal_values
        if isinstance(sv, dict):
            e = sv.get('edge')
            if e is not None:
                edges.append(float(e))

    avg_edge = sum(edges) / len(edges) if edges else 0.0
    max_edge_val = max(edges) if edges else 0.0
    min_edge_val = min(edges) if edges else 0.0

    # Orders executed
    poly_orders = poly_trader.orders
    kalshi_orders = kalshi_trader.orders

    metrics = {
        'total_events': len(events),
        'total_decisions': len(decisions),
        'arb_trades': len(arb_trades),
        'reverse_arb_signals': len(reverse_arbs),
        'hold_decisions': len(holds),
        'poly_orders': len(poly_orders),
        'kalshi_orders': len(kalshi_orders),
        'total_orders': len(poly_orders) + len(kalshi_orders),
        'poly_cash_remaining': str(poly_cash),
        'kalshi_cash_remaining': str(kalshi_cash),
        'total_cash': str(total_cash),
        'total_pnl': str(pnl),
        'pnl_pct': str(round(float(pnl) / float(initial_capital * 2) * 100, 4)),
        'poly_market_positions': poly_market_positions,
        'kalshi_market_positions': kalshi_market_positions,
        'avg_edge': round(avg_edge, 6),
        'max_edge': round(max_edge_val, 6),
        'min_edge': round(min_edge_val, 6),
        'data_stats': data_stats,
        'params': {
            'initial_capital': str(initial_capital),
            'min_edge': min_edge,
            'trade_size': str(trade_size),
            'cooldown': cooldown,
            'base_spread': base_spread,
            'spread_vol': spread_vol,
            'lag_steps': lag_steps,
            'noise_std': noise_std,
            'max_matches': max_matches,
            'seed': seed,
        },
    }

    return metrics


def print_report(metrics: dict) -> None:
    """Print a formatted backtest report."""
    print('=' * 64)
    print('  Cross-Platform Arbitrage Backtest Report')
    print('=' * 64)
    print()

    ds = metrics.get('data_stats', {})
    print(f'  Markets matched:         {ds.get("matches", 0)}')
    print(f'  Total events replayed:   {metrics["total_events"]:,}')
    print(f'  Arb windows in data:     {ds.get("arb_windows", 0):,}')
    print()

    print('  --- Trading Activity ---')
    print(f'  Total decisions:         {metrics["total_decisions"]:,}')
    print(f'  Arb trades executed:     {metrics["arb_trades"]}')
    print(f'  Reverse arb signals:     {metrics["reverse_arb_signals"]}')
    print(f'  Hold (no arb):           {metrics["hold_decisions"]:,}')
    print(f'  Orders placed (Poly):    {metrics["poly_orders"]}')
    print(f'  Orders placed (Kalshi):  {metrics["kalshi_orders"]}')
    print()

    print('  --- PnL ---')
    print(f'  Polymarket cash:         ${metrics["poly_cash_remaining"]}')
    print(f'  Kalshi cash:             ${metrics["kalshi_cash_remaining"]}')
    print(f'  Total cash:              ${metrics["total_cash"]}')
    pnl = Decimal(metrics['total_pnl'])
    sign = '+' if pnl >= 0 else ''
    print(
        f'  PnL:                     {sign}${metrics["total_pnl"]} ({sign}{metrics["pnl_pct"]}%)'
    )
    print(f'  Open positions (Poly):   {metrics["poly_market_positions"]}')
    print(f'  Open positions (Kalshi): {metrics["kalshi_market_positions"]}')
    print()

    if metrics['arb_trades'] > 0:
        print('  --- Edge Statistics ---')
        print(f'  Avg edge per arb:        {metrics["avg_edge"]:.4f}')
        print(f'  Max edge:                {metrics["max_edge"]:.4f}')
        print(f'  Min edge:                {metrics["min_edge"]:.4f}')
        print()

    params = metrics.get('params', {})
    print('  --- Parameters ---')
    print(f'  Capital/platform:        ${params.get("initial_capital", "?")}')
    print(f'  min_edge:                {params.get("min_edge", "?")}')
    print(f'  trade_size:              {params.get("trade_size", "?")} shares')
    print(
        f'  Synthetic spread:        base={params.get("base_spread")}, vol={params.get("spread_vol")}'
    )
    print(f'  Lag steps:               {params.get("lag_steps")}')
    print(f'  Noise std:               {params.get("noise_std")}')
    print()
    print('=' * 64)


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------


async def run_sweep(
    data_path: str, initial_capital: Decimal = Decimal('10000')
) -> None:
    """Run the backtest with multiple parameter combinations."""
    configs = [
        {'base_spread': 0.01, 'min_edge': 0.01, 'label': 'Tight spread, low threshold'},
        {
            'base_spread': 0.02,
            'min_edge': 0.02,
            'label': 'Medium spread, medium threshold',
        },
        {
            'base_spread': 0.03,
            'min_edge': 0.02,
            'label': 'Wide spread, medium threshold',
        },
        {
            'base_spread': 0.04,
            'min_edge': 0.03,
            'label': 'Very wide spread, high threshold',
        },
        {
            'base_spread': 0.02,
            'min_edge': 0.01,
            'label': 'Medium spread, low threshold',
        },
        {
            'base_spread': 0.02,
            'min_edge': 0.03,
            'label': 'Medium spread, high threshold',
        },
    ]

    all_results = []
    for i, cfg in enumerate(configs):
        print(f'\n{"#" * 64}')
        print(f'  Scenario {i + 1}/{len(configs)}: {cfg["label"]}')
        print(f'{"#" * 64}\n')

        metrics = await run_backtest(
            data_path,
            initial_capital=initial_capital,
            min_edge=cfg['min_edge'],
            base_spread=cfg['base_spread'],
            cooldown=0,
        )
        metrics['scenario'] = cfg['label']
        all_results.append(metrics)
        print_report(metrics)

    # Summary table
    print('\n' + '=' * 80)
    print('  PARAMETER SWEEP SUMMARY')
    print('=' * 80)
    print(
        f'  {"Scenario":<45} {"Arb Trades":>10} {"PnL":>12} {"PnL%":>8} {"Avg Edge":>10}'
    )
    print(f'  {"-" * 45} {"-" * 10} {"-" * 12} {"-" * 8} {"-" * 10}')
    for r in all_results:
        pnl = r.get('total_pnl', '0')
        pnl_pct = r.get('pnl_pct', '0')
        print(
            f'  {r.get("scenario", ""):<45} '
            f'{r["arb_trades"]:>10} '
            f'{"$" + pnl:>12} '
            f'{pnl_pct + "%":>8} '
            f'{r["avg_edge"]:>10.4f}'
        )
    print('=' * 80)

    # Save results
    out = Path('data/cross_platform/backtest_results.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f'\nResults saved to {out}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Cross-platform arb strategy backtest',
    )
    parser.add_argument(
        '--data',
        default='data/cross_platform/matched.jsonl',
        help='Path to crawled cross-platform data JSONL',
    )
    parser.add_argument(
        '--capital', type=float, default=10000, help='Capital per platform'
    )
    parser.add_argument('--min-edge', type=float, default=0.02, help='Minimum arb edge')
    parser.add_argument(
        '--trade-size', type=float, default=10, help='Shares per arb leg'
    )
    parser.add_argument(
        '--cooldown', type=int, default=0, help='Cooldown between arbs (seconds)'
    )
    parser.add_argument(
        '--base-spread', type=float, default=0.02, help='Mean Kalshi-Poly spread'
    )
    parser.add_argument(
        '--spread-vol', type=float, default=0.01, help='Spread volatility'
    )
    parser.add_argument('--lag', type=int, default=3, help='Kalshi lag in data points')
    parser.add_argument(
        '--noise', type=float, default=0.008, help='Idiosyncratic noise std'
    )
    parser.add_argument('--max-matches', type=int, default=20, help='Max market pairs')
    parser.add_argument('--seed', type=int, default=42, help='Random seed')
    parser.add_argument('--sweep', action='store_true', help='Run parameter sweep')
    args = parser.parse_args()

    if not Path(args.data).exists():
        print(f'ERROR: Data file not found: {args.data}')
        print('Run: python scripts/crawl_cross_platform_data.py first')
        sys.exit(1)

    if args.sweep:
        asyncio.run(run_sweep(args.data, Decimal(str(args.capital))))
    else:
        metrics = asyncio.run(
            run_backtest(
                args.data,
                initial_capital=Decimal(str(args.capital)),
                min_edge=args.min_edge,
                trade_size=Decimal(str(args.trade_size)),
                cooldown=args.cooldown,
                base_spread=args.base_spread,
                spread_vol=args.spread_vol,
                lag_steps=args.lag,
                noise_std=args.noise,
                max_matches=args.max_matches,
                seed=args.seed,
            )
        )
        print_report(metrics)

        # Save single run
        out = Path('data/cross_platform/backtest_results.json')
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, 'w') as f:
            json.dump(metrics, f, indent=2, default=str)
        print(f'Results saved to {out}')


if __name__ == '__main__':
    main()
