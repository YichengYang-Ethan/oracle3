"""Run a backtest using recorded DFlow Solana data + AdaptiveOnChainStrategy.

Saves results to data/backtest_solana_results.json for the preview dashboard.

Usage: python3 run_dflow_backtest.py
"""

import asyncio
import json
import logging
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
    format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

EPISODE_DIR = 'data/episodes/dflow_15min'
INITIAL_CAPITAL = Decimal('10000')
OUTPUT_FILE = 'data/backtest_solana_results.json'


async def main() -> None:
    # Load DFlow episode data
    data_source = SolanaReplayDataSource(EPISODE_DIR)
    logger.info('Loaded %d events from %s', len(data_source.events), EPISODE_DIR)

    # Set up paper trading
    market_data = MarketDataManager(
        spread=Decimal('0.01'),
        max_history_per_ticker=None,
        max_timeline_events=None,
    )
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.DFLOW_USDC,
            quantity=INITIAL_CAPITAL,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # Strategy: conservative — only trade strong signals, hold longer
    strategy = AdaptiveOnChainStrategy(
        composite_threshold=0.35,  # high threshold → only strong signals
        position_size=20.0,
        max_position_pct=0.20,
        stop_loss_pct=0.15,
        max_hold_events=200,       # hold through entire episode
        adapt_window=5,
        w_ob=0.3,
        w_momentum=0.7,           # lean on momentum
    )

    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
    )

    # Run backtest
    logger.info('Starting DFlow backtest...')
    await engine.start()

    # Collect results
    snap = engine.get_snapshot()
    perf = getattr(engine, '_perf', None) or getattr(engine, 'analyzer', None)

    equity_curve = []
    performance = {}
    if perf:
        try:
            perf.print_summary()
            curve = perf.get_equity_curve()
            equity_curve = [{'equity': str(pt.equity)} for pt in curve]
            stats = perf.get_stats()
            performance = {
                'total_trades': stats.total_trades,
                'winning_trades': stats.winning_trades,
                'losing_trades': stats.losing_trades,
                'win_rate': str(stats.win_rate),
                'average_profit': str(stats.average_profit),
                'average_loss': str(stats.average_loss),
                'max_drawdown': str(stats.max_drawdown),
                'sharpe_ratio': str(stats.sharpe_ratio),
                'profit_factor': str(stats.profit_factor),
                'total_pnl': str(stats.total_pnl),
                'max_consecutive_wins': stats.max_consecutive_wins,
                'max_consecutive_losses': stats.max_consecutive_losses,
            }
        except Exception as e:
            logger.warning('Failed to get perf stats: %s', e)

    # Trades
    trades = [
        {
            'time': t.time,
            'side': t.side,
            'name': t.ticker_name,
            'symbol': t.ticker_symbol,
            'price': str(t.price),
            'qty': str(t.quantity),
            'status': t.status,
        }
        for t in snap.recent_trades
    ]

    # Decisions
    decisions = []
    if strategy:
        try:
            raw_decisions = list(strategy.get_decisions())
            for d in raw_decisions[-50:]:
                decisions.append({
                    'timestamp': d.timestamp,
                    'action': d.action,
                    'ticker_name': (d.ticker_name or '')[:50],
                    'confidence': float(getattr(d, 'confidence', 0.0) or 0.0),
                    'reasoning': (getattr(d, 'reasoning', '') or '')[:80],
                    'executed': bool(d.executed),
                })
        except Exception as e:
            logger.warning('Failed to get decisions: %s', e)

    # Positions
    positions = [
        {
            'symbol': p.ticker_symbol,
            'name': p.ticker_name,
            'qty': str(p.quantity),
            'avg_cost': str(p.average_cost),
            'current_price': str(p.current_price),
            'unrealized_pnl': str(p.unrealized_pnl),
        }
        for p in snap.positions
    ]

    # Order books (only two-sided)
    order_books = []
    for ob in snap.orderbooks:
        best_bid = float(ob.bids[0][0]) if ob.bids else 0.0
        best_ask = float(ob.asks[0][0]) if ob.asks else 0.0
        if best_bid <= 0 or best_ask <= 0:
            continue
        order_books.append({
            'symbol': ob.ticker_symbol,
            'bid': f'{best_bid:.4f}',
            'ask': f'{best_ask:.4f}',
            'spread': f'{best_ask - best_bid:.4f}',
        })

    results = {
        'source': 'DFlow Solana Backtest',
        'episode': EPISODE_DIR,
        'initial_capital': str(INITIAL_CAPITAL),
        'equity': str(snap.equity),
        'cash': str(snap.cash),
        'total_pnl': str(snap.total_pnl),
        'realized_pnl': str(snap.realized_pnl),
        'unrealized_pnl': str(snap.unrealized_pnl),
        'event_count': snap.event_count,
        'positions': positions,
        'order_books': order_books,
        'trades': trades,
        'decisions': decisions,
        'performance': performance,
        'equity_curve': equity_curve,
    }

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(results, f, indent=2, default=str)

    logger.info('Results saved to %s', OUTPUT_FILE)
    logger.info(
        'Final: equity=$%s  pnl=$%s  trades=%d  positions=%d',
        snap.equity, snap.total_pnl, len(trades), len(positions),
    )


if __name__ == '__main__':
    asyncio.run(main())
