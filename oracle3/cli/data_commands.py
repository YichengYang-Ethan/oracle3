"""CLI commands for recording live market data to JSONL files."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from oracle3.data.data_source import DataSource
from oracle3.events.events import (
    Event,
    NewsEvent,
    OrderBookEvent,
    PriceChangeEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_to_record(event: Event) -> dict | None:
    """Serialize a live event to a JSONL-compatible dict.

    OrderBookEvent and PriceChangeEvent are written in a format compatible
    with HistoricalDataSource so they can be replayed via ``backtest run``:

        {"event_id": "...", "market_id": "...", "time_series": {"Yes": [{"t": ts, "p": price}]}}

    NewsEvent is written as a raw JSON record with ``type: "news"``.
    Lines that HistoricalDataSource cannot interpret (news, unknown types)
    are simply ignored during backtest replay.
    """
    ts = int(time.time())

    if isinstance(event, OrderBookEvent):
        ticker = event.ticker
        # HistoricalDataSource keys
        event_id = getattr(ticker, 'event_id', '') or getattr(
            ticker, 'event_ticker', ''
        )
        market_id = getattr(ticker, 'market_id', '') or getattr(
            ticker, 'market_ticker', ''
        )
        return {
            'event_id': event_id,
            'market_id': market_id,
            'side': event.side,
            'time_series': {
                'Yes': [{'t': ts, 'p': float(event.price)}],
            },
        }

    if isinstance(event, PriceChangeEvent):
        ticker = event.ticker
        event_id = getattr(ticker, 'event_id', '') or getattr(
            ticker, 'event_ticker', ''
        )
        market_id = getattr(ticker, 'market_id', '') or getattr(
            ticker, 'market_ticker', ''
        )
        return {
            'event_id': event_id,
            'market_id': market_id,
            'time_series': {
                'Yes': [{'t': ts, 'p': float(event.price)}],
            },
        }

    if isinstance(event, NewsEvent):
        return {
            'type': 'news',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'title': event.title,
            'source': event.source,
            'url': event.url,
            'description': event.description,
            'published_at': event.published_at.isoformat()
            if event.published_at
            else None,
        }

    return None


async def _record_loop(
    data_source,
    output_path: Path,
    duration: float | None,
    verbose: bool,
) -> int:
    """Run a data source and write events to *output_path*. Returns event count."""
    await data_source.start()
    count = 0
    deadline = time.monotonic() + duration if duration else None

    try:
        with open(output_path, 'a', encoding='utf-8') as f:
            while True:
                if deadline and time.monotonic() >= deadline:
                    break
                event = await data_source.get_next_event()
                if event is None:
                    continue
                record = _event_to_record(event)
                if record is None:
                    continue
                f.write(json.dumps(record) + '\n')
                f.flush()
                count += 1
                if verbose:
                    label = type(event).__name__
                    sym = getattr(getattr(event, 'ticker', None), 'symbol', '')
                    click.echo(f'  [{count}] {label} {sym}')
    except asyncio.CancelledError:
        pass
    finally:
        await data_source.stop()

    return count


# ---------------------------------------------------------------------------
# Click group + command
# ---------------------------------------------------------------------------


@click.group()
def data() -> None:
    """Live data recording and management commands."""


@data.command('record')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'rss']),
    default='rss',
    show_default=True,
    help='Exchange / data source to record from.',
)
@click.option(
    '--output',
    default='events.jsonl',
    show_default=True,
    type=click.Path(dir_okay=False),
    help='Output JSONL file path (appended if it already exists).',
)
@click.option(
    '--duration',
    default=None,
    type=float,
    help='How many seconds to record (default: run until Ctrl-C).',
)
@click.option(
    '--polling-interval',
    default=60.0,
    show_default=True,
    type=float,
    help='Polling interval in seconds for market data sources.',
)
@click.option(
    '--kalshi-api-key-id',
    default=None,
    help='Kalshi API key id (or KALSHI_API_KEY_ID).',
)
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path (or KALSHI_PRIVATE_KEY_PATH).',
)
@click.option(
    '--verbose', '-v', is_flag=True, default=False, help='Print each recorded event.'
)
@click.option(
    '--json',
    'as_json',
    is_flag=True,
    default=False,
    help='Emit JSON status on completion.',
)
def data_record(
    exchange: str,
    output: str,
    duration: float | None,
    polling_interval: float,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Record live market events to a JSONL file for later backtesting.

    Events are written in a format compatible with ``backtest run``.
    Press Ctrl-C to stop recording early.

    \b
    Example workflow:
      oracle3 data record --exchange rss --output events.jsonl --duration 60
      oracle3 backtest run --history-file events.jsonl --market-id X --event-id Y ...
    """
    from oracle3.data.live.kalshi_data_source import LiveKalshiDataSource
    from oracle3.data.live.live_data_source import (
        LivePolyMarketDataSource,
        LiveRSSNewsDataSource,
    )

    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data_source: DataSource
    if exchange == 'polymarket':
        data_source = LivePolyMarketDataSource(
            event_cache_file='record_events_cache.jsonl',
            polling_interval=polling_interval,
            orderbook_refresh_interval=min(polling_interval, 10.0),
            reprocess_on_start=True,
        )
    elif exchange == 'kalshi':
        data_source = LiveKalshiDataSource(
            api_key_id=kalshi_api_key_id,
            private_key_path=kalshi_private_key_path,
            event_cache_file='record_kalshi_cache.jsonl',
            polling_interval=polling_interval,
            reprocess_on_start=True,
        )
    else:
        data_source = LiveRSSNewsDataSource(
            polling_interval=polling_interval,
            max_articles_per_poll=20,
        )

    dur_msg = f'{duration}s' if duration else 'until Ctrl-C'
    if not as_json:
        click.echo(f'Recording {exchange} events → {output_path}  ({dur_msg})')
        click.echo('Press Ctrl-C to stop.\n')

    count = 0
    try:
        count = asyncio.run(_record_loop(data_source, output_path, duration, verbose))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        raise click.ClickException(f'Recording failed: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    'exchange': exchange,
                    'output': str(output_path),
                    'events_recorded': count,
                }
            )
        )
    else:
        click.echo(f'\nRecording complete: {count} event(s) written to {output_path}')
