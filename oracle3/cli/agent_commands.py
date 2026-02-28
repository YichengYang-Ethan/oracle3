"""Agent-first CLI commands for strategy, backtest, paper, and live modes."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

from oracle3.backtest.backtester import run_backtest
from oracle3.cli.utils import _emit
from oracle3.data.composite_data_source import CompositeDataSource
from oracle3.data.live.dflow_data_source import DFlowDataSource
from oracle3.data.live.google_news_data_source import GoogleNewsDataSource
from oracle3.data.live.kalshi_data_source import LiveKalshiDataSource
from oracle3.data.live.live_data_source import (
    LivePolyMarketDataSource,
    LiveRSSNewsDataSource,
)
from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.live.live_trader import (
    run_live_kalshi_paper_trading,
    run_live_kalshi_trading,
    run_live_paper_trading,
    run_live_polymarket_trading,
    run_live_solana_paper_trading,
    run_live_solana_trading,
)
from oracle3.strategy.loader import load_strategy_class as _shared_load_strategy_class
from oracle3.strategy.strategy import Strategy
from oracle3.ticker.ticker import PolyMarketTicker
from oracle3.trader.trader import Trader


class _IdleStrategy(Strategy):
    """No-op strategy: consume events without placing orders."""

    async def process_event(self, event: Event, trader: Trader) -> None:
        return


def _parse_strategy_kwargs_json(strategy_kwargs_json: str | None) -> dict[str, Any]:
    if not strategy_kwargs_json:
        return {}
    try:
        parsed = json.loads(strategy_kwargs_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f'Invalid --strategy-kwargs-json: {exc.msg}'
        ) from exc
    if not isinstance(parsed, dict):
        raise click.ClickException('--strategy-kwargs-json must be a JSON object.')
    return parsed


def _confirm_live_trading(*, as_json: bool) -> None:
    """Require explicit user confirmation before starting live trading."""
    disclaimer = (
        'DISCLAIMER: Live trading places real orders with real funds. '
        'You are fully responsible for all losses, fees, and operational risk.'
    )

    if as_json:
        raise click.ClickException(
            'Live trading confirmation required in interactive mode.'
        )

    click.echo(click.style(disclaimer, fg='yellow'))
    confirmed = click.confirm(
        'Proceed with live trading?',
        default=True,
        show_default=True,
    )
    if not confirmed:
        raise click.ClickException('Live trading cancelled by user.')


def _build_news_augmented_source(exchange: str):
    """Build market + Google/RSS news composite for paper trading."""
    if exchange == 'polymarket':
        market_source = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
    elif exchange == 'kalshi':
        market_source = LiveKalshiDataSource(
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
    elif exchange in ('solana', 'dflow'):
        market_source = DFlowDataSource(
            event_cache_file='dflow_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
    else:
        raise click.ClickException(f'Unsupported exchange for market feed: {exchange}')

    # Keep Google polling conservative to reduce block/rate-limit risk.
    google_source = GoogleNewsDataSource(
        queries=[
            'polymarket prediction market',
            'kalshi prediction market',
            'US politics elections 2026',
            'federal reserve inflation jobs report',
            'geopolitics world events',
            'crypto regulation SEC CFTC',
        ],
        cache_file='google_news_cache.jsonl',
        polling_interval=600.0,
        max_articles_per_poll=8,
        max_pages=1,
        min_delay=3.0,
        max_delay=8.0,
    )
    rss_source = LiveRSSNewsDataSource(
        cache_file='rss_news_cache.jsonl',
        polling_interval=600.0,
        max_articles_per_poll=8,
        categories=['world', 'business', 'finance', 'politics', 'economy', 'sports'],
    )
    return CompositeDataSource([market_source, google_source, rss_source])


def _load_strategy_class(strategy_ref: str) -> type[Strategy]:
    try:
        return _shared_load_strategy_class(strategy_ref)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _load_strategy(
    strategy_ref: str, strategy_kwargs: dict[str, Any] | None = None
) -> Strategy:
    kwargs = strategy_kwargs or {}
    strategy_cls = _load_strategy_class(strategy_ref)
    try:
        return strategy_cls(**kwargs)
    except TypeError as exc:
        raise click.ClickException(
            f'Could not instantiate strategy {strategy_ref!r} with kwargs={kwargs}: {exc}'
        ) from exc


def _build_mock_events(ticker: PolyMarketTicker, n_events: int) -> list[Event]:
    prices = [
        Decimal('0.47'),
        Decimal('0.49'),
        Decimal('0.46'),
        Decimal('0.51'),
        Decimal('0.48'),
    ]
    events: list[Event] = []
    for i in range(max(1, n_events)):
        base = prices[i % len(prices)]
        if i % 2 == 0:
            events.append(
                PriceChangeEvent(
                    ticker=ticker,
                    price=base,
                    timestamp=i + 1,
                )
            )
            continue

        side = 'bid' if i % 4 == 1 else 'ask'
        price = base - Decimal('0.01') if side == 'bid' else base + Decimal('0.01')
        size = Decimal('100') + Decimal(i * 10)
        events.append(
            OrderBookEvent(
                ticker=ticker,
                price=price,
                size=size,
                size_delta=Decimal('10'),
                side=side,
            )
        )
    return events


@click.group()
def strategy() -> None:
    """Strategy development commands."""


@strategy.command('create')
@click.option('--output', 'output_path', required=True, type=click.Path(path_type=Path))
@click.option(
    '--class-name',
    default=None,
    help='Class name (defaults: MyQuantStrategy / MyAgentStrategy)',
)
@click.option(
    '--force', is_flag=True, default=False, help='Overwrite output file if it exists.'
)
@click.option(
    '--type',
    'strategy_type',
    default='quant',
    show_default=True,
    type=click.Choice(['quant', 'agent']),
    help="'quant' → QuantStrategy (auto-tunable); 'agent' → AgentStrategy (LLM/tools).",
)
def strategy_create(
    output_path: Path, class_name: str | None, force: bool, strategy_type: str
) -> None:
    """Create a strategy template that agents can edit."""
    if output_path.exists() and not force:
        raise click.ClickException(
            f'File already exists: {output_path}. Pass --force to overwrite.'
        )

    if class_name is None:
        class_name = (
            'MyQuantStrategy' if strategy_type == 'quant' else 'MyAgentStrategy'
        )

    if strategy_type == 'quant':
        template = f"""from __future__ import annotations

from decimal import Decimal

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.quant_strategy import QuantStrategy
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class {class_name}(QuantStrategy):
    \"\"\"Deterministic, numerically-parameterised strategy.

    Constructor kwargs are auto-tunable via `research discover-alpha --param-grid-json`.
    Keep them JSON-serialisable numerics.
    \"\"\"

    name = '{class_name}'
    version = '0.1.0'
    author = ''

    def __init__(
        self,
        trade_size: float = 10.0,
        threshold: float = 0.05,
    ) -> None:
        self.trade_size = Decimal(str(trade_size))
        self.threshold = threshold

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        context = self.require_context()
        if isinstance(event, PriceChangeEvent):
            # Use prepare_data()/context helpers to load all visible state.
            data = self.prepare_data(context)
            self.record_decision(
                ticker_name=str(event.ticker),
                action='HOLD',
                executed=False,
                reasoning=f'skeleton — replace with your signal logic using {{len(data["price_history"])}} prices',
                signal_values={{}},
            )
            return
        if isinstance(event, OrderBookEvent):
            # Use bid/ask updates for microstructure-aware execution.
            return
"""
    else:
        template = f"""from __future__ import annotations

from decimal import Decimal

from oracle3.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.agent_strategy import AgentStrategy
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class {class_name}(AgentStrategy):
    \"\"\"LLM-driven or tool-using strategy.

    May call external APIs (LLMs, web search, MCP tools).
    NOT eligible for parameter grid search — evaluate via `paper run --monitor`.
    \"\"\"

    name = '{class_name}'
    version = '0.1.0'
    author = ''

    def __init__(self, trade_size: float = 10.0) -> None:
        super().__init__()
        self.trade_size = Decimal(str(trade_size))

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        context = self.require_context()
        if isinstance(event, NewsEvent):
            # build_prompt_context() explains the visible data/tools.
            prompt = self.build_prompt_context(context)
            self.record_decision(
                ticker_name=str(event.ticker) if hasattr(event, 'ticker') else 'unknown',
                action='HOLD',
                executed=False,
                reasoning=f'skeleton — replace with your LLM/tool call\\n{{prompt[:200]}}',
                signal_values={{}},
            )
            return
        if isinstance(event, PriceChangeEvent):
            return
        if isinstance(event, OrderBookEvent):
            return
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template)
    click.echo(f'Created {strategy_type} strategy template: {output_path}')


@strategy.command('validate')
@click.option(
    '--strategy-ref',
    required=True,
    help='Strategy ref: module:Class or /path/file.py:Class',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option(
    '--dry-run',
    'do_dry_run',
    is_flag=True,
    default=False,
    help='Also feed mock events to confirm runtime behaviour.',
)
@click.option(
    '--events',
    default=8,
    show_default=True,
    type=click.IntRange(1, 50),
    help='Mock events to feed when --dry-run is set.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON result')
def strategy_validate(
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    do_dry_run: bool,
    events: int,
    as_json: bool,
) -> None:
    """Validate that a strategy is importable, constructible, and (optionally) runtime-safe."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)

    payload: dict[str, Any] = {
        'ok': True,
        'strategy_ref': strategy_ref,
        'strategy_kwargs': strategy_kwargs,
        'class': strategy_obj.__class__.__name__,
        'module': strategy_obj.__class__.__module__,
        'message': f'Valid strategy: {strategy_ref}',
    }

    if do_dry_run:
        from oracle3.data.market_data_manager import MarketDataManager
        from oracle3.position.position_manager import Position, PositionManager
        from oracle3.risk.risk_manager import NoRiskManager
        from oracle3.ticker.ticker import CashTicker
        from oracle3.trader.paper_trader import PaperTrader

        ticker = PolyMarketTicker(
            symbol='DRYRUN_YES',
            name='Dry Run Market',
            token_id='DRYRUN_YES',
            market_id='DRYRUN_MKT',
            event_id='DRYRUN_EVT',
            no_token_id='DRYRUN_NO',
        )
        market_data = MarketDataManager()
        position_manager = PositionManager()
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('10000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        trader = PaperTrader(
            market_data=market_data,
            risk_manager=NoRiskManager(),
            position_manager=position_manager,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.0'),
        )
        event_stream = _build_mock_events(ticker, events)

        async def _run_stream() -> tuple[int, str]:
            processed = 0
            error_message = ''
            for event in event_stream:
                if isinstance(event, OrderBookEvent):
                    market_data.process_orderbook_event(event)
                elif isinstance(event, PriceChangeEvent):
                    market_data.process_price_change_event(event)
                try:
                    await strategy_obj.process_event(event, trader)
                    processed += 1
                except Exception as exc:  # noqa: BLE001
                    error_message = str(exc)
                    break
            return processed, error_message

        processed, error_message = asyncio.run(_run_stream())
        decision_stats = strategy_obj.get_decision_stats()
        decisions = strategy_obj.get_decisions()
        payload.update(
            {
                'ok': error_message == '',
                'events_requested': events,
                'events_processed': processed,
                'orders_created': len(trader.orders),
                'decision_stats': decision_stats,
                'decisions_sample': [
                    {
                        'timestamp': d.timestamp,
                        'ticker_name': d.ticker_name,
                        'action': d.action,
                        'executed': d.executed,
                        'confidence': d.confidence,
                        'reasoning': d.reasoning,
                        'signal_values': d.signal_values,
                    }
                    for d in decisions[-5:]
                ],
                'error': error_message or None,
                'message': 'Dry-run completed'
                if error_message == ''
                else 'Dry-run failed',
            }
        )
        _emit(payload, as_json=as_json)
        if error_message:
            raise click.ClickException(f'Dry-run failed: {error_message}')
        return

    _emit(payload, as_json=as_json)


@click.group()
def backtest() -> None:
    """Backtest commands."""


@backtest.command('run')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--symbol', default='BACKTEST_TOKEN', show_default=True)
@click.option('--name', default='Backtest Market', show_default=True)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--spread',
    default='0.01',
    show_default=True,
    help='Synthetic bid-ask spread for simulated order book.',
)
@click.option(
    '--strategy-ref',
    default='oracle3.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--min-fill-rate', default='0.5', show_default=True)
@click.option('--max-fill-rate', default='1.0', show_default=True)
@click.option('--commission-rate', default='0.0', show_default=True)
@click.option(
    '--risk-profile',
    default='none',
    show_default=True,
    type=click.Choice(['none', 'standard']),
)
@click.option(
    '--all-markets-context/--primary-market-context',
    default=False,
    show_default=True,
    help='Expose all markets from the history file to the strategy context.',
)
@click.option(
    '--allow-cross-market-trading/--primary-market-only',
    default=False,
    show_default=True,
    help='Allow the strategy to place trades outside the requested market.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
def backtest_run(
    history_file: str,
    symbol: str,
    name: str,
    market_id: str,
    event_id: str,
    initial_capital: str,
    spread: str,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    min_fill_rate: str,
    max_fill_rate: str,
    commission_rate: str,
    risk_profile: str,
    all_markets_context: bool,
    allow_cross_market_trading: bool,
    as_json: bool,
) -> None:
    """Run backtest mode with historical data + paper execution."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    capital = Decimal(initial_capital)
    try:
        fill_min = Decimal(min_fill_rate)
        fill_max = Decimal(max_fill_rate)
        fee = Decimal(commission_rate)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            'Invalid fill/commission value. Use numeric decimals.'
        ) from exc
    if fill_min <= 0 or fill_max <= 0:
        raise click.ClickException('--min-fill-rate and --max-fill-rate must be > 0.')
    if fill_min > fill_max:
        raise click.ClickException('--min-fill-rate cannot exceed --max-fill-rate.')
    if fee < 0:
        raise click.ClickException('--commission-rate must be >= 0.')

    if as_json:
        from oracle3.cli.research_commands import _run_backtest_once

        try:
            metrics = _run_backtest_once(
                history_file=history_file,
                strategy_ref=strategy_ref,
                strategy_kwargs=strategy_kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
                min_fill_rate=fill_min,
                max_fill_rate=fill_max,
                commission_rate=fee,
                risk_profile=risk_profile,
                include_all_markets_context=all_markets_context,
                allow_cross_market_trading=allow_cross_market_trading,
            )
        except Exception as exc:  # noqa: BLE001
            _emit({'ok': False, 'error': str(exc)}, as_json=True)
            raise click.ClickException(str(exc)) from exc
        _emit({'ok': True, **metrics}, as_json=True)
        return

    if (
        fill_min != Decimal('0.5')
        or fill_max != Decimal('1.0')
        or fee != Decimal('0.0')
        or risk_profile != 'none'
    ):
        raise click.ClickException(
            'Custom fill/fee/risk options currently require --json mode.'
        )

    spread_val = Decimal(spread)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
    no_symbol = f'{symbol}_NO'
    ticker = PolyMarketTicker(
        symbol=symbol,
        name=name,
        market_id=market_id,
        event_id=event_id,
        token_id=symbol,
        no_token_id=no_symbol,
    )
    _emit(
        {
            'mode': 'backtest',
            'message': f'Starting backtest: {strategy_ref}',
            'history_file': history_file,
            'symbol': symbol,
            'strategy_kwargs': strategy_kwargs,
        },
        as_json=as_json,
    )
    asyncio.run(
        run_backtest(
            history_file=history_file,
            ticker_symbol=ticker,
            initial_capital=capital,
            strategy=strategy_obj,
            spread=spread_val,
            include_all_markets_context=all_markets_context,
            allow_cross_market_trading=allow_cross_market_trading,
        )
    )
    _emit({'mode': 'backtest', 'message': 'Backtest completed'}, as_json=as_json)


@click.group()
def paper() -> None:
    """Paper trading commands (live data + simulated execution)."""


@paper.command('run')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'solana', 'rss']),
    default='polymarket',
)
@click.option(
    '--duration', type=float, default=None, help='Seconds to run (default: forever)'
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--strategy-ref',
    default=None,
    help='Strategy ref: module:Class or /path/file.py:Class. If omitted, run in idle mode (no orders).',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
@click.option(
    '--monitor', '-m', is_flag=True, default=False, help='Show live TUI dashboard'
)
def paper_run(
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
    monitor: bool,
) -> None:
    """Run paper trading against a live exchange feed."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    if strategy_kwargs and not strategy_ref:
        raise click.ClickException(
            '--strategy-kwargs-json requires --strategy-ref (idle mode has no strategy).'
        )
    strategy_obj = (
        _load_strategy(strategy_ref, strategy_kwargs)
        if strategy_ref
        else _IdleStrategy()
    )
    strategy_mode = 'active' if strategy_ref else 'idle'
    capital = Decimal(initial_capital)
    _emit(
        {
            'mode': 'paper',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'strategy_kwargs': strategy_kwargs,
            'strategy_mode': strategy_mode,
            'message': (
                f'Starting paper mode ({exchange})'
                if strategy_ref
                else f'Starting paper mode ({exchange}) in idle mode (no strategy orders)'
            ),
        },
        as_json=as_json,
    )

    if exchange == 'polymarket':
        data_source = _build_news_augmented_source('polymarket')
        asyncio.run(
            run_live_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Polymarket',
                emit_text=not as_json,
            )
        )
    elif exchange == 'kalshi':
        data_source = _build_news_augmented_source('kalshi')
        asyncio.run(
            run_live_kalshi_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Kalshi',
                emit_text=not as_json,
            )
        )
    elif exchange == 'solana':
        data_source = _build_news_augmented_source('solana')
        asyncio.run(
            run_live_solana_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Solana/DFlow',
                emit_text=not as_json,
            )
        )
    else:
        data_source = LiveRSSNewsDataSource(
            polling_interval=60.0,
            max_articles_per_poll=5,
        )
        asyncio.run(
            run_live_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='RSS',
                emit_text=not as_json,
            )
        )

    _emit({'mode': 'paper', 'message': 'Paper session ended'}, as_json=as_json)


@click.group()
def live() -> None:
    """Live trading commands (real exchange execution)."""


@live.command('run')
@click.option('--exchange', type=click.Choice(['polymarket', 'kalshi', 'solana']), required=True)
@click.option(
    '--duration', type=float, default=None, help='Seconds to run (default: forever)'
)
@click.option(
    '--strategy-ref',
    default=None,
    help='Strategy ref: module:Class or /path/file.py:Class. If omitted, run in idle mode (no orders).',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
@click.option(
    '--wallet-private-key',
    default=None,
    help='Polymarket wallet private key (or POLYMARKET_PRIVATE_KEY)',
)
@click.option('--signature-type', default=0, show_default=True, type=int)
@click.option('--funder', default=None, help='Polymarket funder wallet (optional)')
@click.option(
    '--kalshi-api-key-id', default=None, help='Kalshi API key id (or KALSHI_API_KEY_ID)'
)
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path (or KALSHI_PRIVATE_KEY_PATH)',
)
@click.option(
    '--solana-keypair-path',
    default=None,
    help='Solana keypair JSON file (or SOLANA_KEYPAIR_PATH)',
)
@click.option(
    '--solana-rpc-url',
    default='https://api.mainnet-beta.solana.com',
    show_default=True,
    help='Solana RPC URL',
)
@click.option(
    '--monitor', '-m', is_flag=True, default=False, help='Show live TUI dashboard'
)
def live_run(
    exchange: str,
    duration: float | None,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
    wallet_private_key: str | None,
    signature_type: int,
    funder: str | None,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    solana_keypair_path: str | None,
    solana_rpc_url: str,
    monitor: bool,
) -> None:
    """Run live mode with real order placement."""
    _confirm_live_trading(as_json=as_json)

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    if strategy_kwargs and not strategy_ref:
        raise click.ClickException(
            '--strategy-kwargs-json requires --strategy-ref (idle mode has no strategy).'
        )
    strategy_obj = (
        _load_strategy(strategy_ref, strategy_kwargs)
        if strategy_ref
        else _IdleStrategy()
    )
    strategy_mode = 'active' if strategy_ref else 'idle'
    _emit(
        {
            'mode': 'live',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'strategy_kwargs': strategy_kwargs,
            'strategy_mode': strategy_mode,
            'message': (
                f'Starting live mode ({exchange})'
                if strategy_ref
                else f'Starting live mode ({exchange}) in idle mode (no strategy orders)'
            ),
        },
        as_json=as_json,
    )

    if exchange == 'polymarket':
        private_key = wallet_private_key or os.environ.get('POLYMARKET_PRIVATE_KEY')
        if not private_key:
            raise click.ClickException(
                'Missing Polymarket key. Pass --wallet-private-key or set POLYMARKET_PRIVATE_KEY.'
            )
        data_source = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_polymarket_trading(
                data_source=data_source,
                strategy=strategy_obj,
                wallet_private_key=private_key,
                signature_type=signature_type,
                funder=funder,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Polymarket',
            )
        )
    elif exchange == 'kalshi':
        data_source = LiveKalshiDataSource(
            api_key_id=kalshi_api_key_id,
            private_key_path=kalshi_private_key_path,
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_kalshi_trading(
                data_source=data_source,
                strategy=strategy_obj,
                api_key_id=kalshi_api_key_id,
                private_key_path=kalshi_private_key_path,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Kalshi',
            )
        )
    else:
        # solana
        data_source = DFlowDataSource(
            event_cache_file='dflow_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_solana_trading(
                data_source=data_source,
                strategy=strategy_obj,
                keypair_path=solana_keypair_path,
                rpc_url=solana_rpc_url,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Solana/DFlow',
            )
        )

    _emit({'mode': 'live', 'message': 'Live session ended'}, as_json=as_json)
