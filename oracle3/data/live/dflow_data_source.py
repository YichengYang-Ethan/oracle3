import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any

import httpx

from ...events.events import Event, NewsEvent, OrderBookEvent
from ...ticker.ticker import SolanaTicker
from ..data_source import DataSource

logger = logging.getLogger(__name__)


class DFlowDataSource(DataSource):
    """Polls DFlow prediction markets API for Solana-tokenized markets."""

    METADATA_BASE = 'https://dev-prediction-markets-api.dflow.net'

    def __init__(
        self,
        polling_interval: float = 60.0,
        event_cache_file: str = 'dflow_events_cache.jsonl',
        reprocess_on_start: bool = True,
    ):
        self.polling_interval = polling_interval
        self.event_cache_file = event_cache_file
        self.processed_event_tickers: set[str] = set()
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.last_prices: dict[str, tuple[float, float]] = {}
        self._news_fetched_events: set[str] = set()
        self._poll_task: asyncio.Task | None = None

        # Load cache
        if os.path.exists(self.event_cache_file):
            with open(self.event_cache_file) as f:
                for line in f:
                    try:
                        cached = json.loads(line.strip())
                        if 'event_ticker' in cached:
                            self._news_fetched_events.add(cached['event_ticker'])
                            if not reprocess_on_start:
                                self.processed_event_tickers.add(cached['event_ticker'])
                    except json.JSONDecodeError:
                        pass

    async def _fetch_categories(self) -> list[dict[str, Any]]:
        """Fetch available market categories from DFlow."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f'{self.METADATA_BASE}/api/v1/tags_by_categories')
            resp.raise_for_status()
            return resp.json()

    async def _fetch_events(self, series_tickers: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch active events with nested markets."""
        params: dict[str, Any] = {
            'status': 'active',
            'withNestedMarkets': 'true',
        }
        if series_tickers:
            params['seriesTickers'] = ','.join(series_tickers)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f'{self.METADATA_BASE}/api/v1/events', params=params)
            resp.raise_for_status()
            data = resp.json()
            # Response may be a list directly or wrapped in a key
            if isinstance(data, list):
                return data
            return data.get('events', data.get('data', []))

    async def _fetch_markets(self) -> list[dict[str, Any]]:
        """Fetch all active markets from DFlow."""
        try:
            events = await self._fetch_events()
            markets = []
            for event in events:
                event_ticker = event.get('eventTicker', event.get('event_ticker', ''))
                series_ticker = event.get('seriesTicker', event.get('series_ticker', ''))
                for mkt in event.get('markets', []):
                    mkt['_event_ticker'] = event_ticker
                    mkt['_series_ticker'] = series_ticker
                    mkt['_event_title'] = event.get('title', event.get('name', ''))
                    markets.append(mkt)
            return markets
        except Exception as e:
            logger.error('Error fetching DFlow markets: %s', e)
            return []

    def _market_to_order_book_events(self, market: dict[str, Any]) -> list[OrderBookEvent]:
        """Convert DFlow market pricing to OrderBookEvents with SolanaTicker."""
        events = []
        market_ticker = market.get('ticker', market.get('marketTicker', ''))
        market_title = market.get('title', market.get('question', ''))
        event_ticker = market.get('_event_ticker', '')
        series_ticker = market.get('_series_ticker', '')

        # DFlow prices may be in different formats
        yes_bid = market.get('yesBid', market.get('yes_bid', 0)) or 0
        yes_ask = market.get('yesAsk', market.get('yes_ask', 0)) or 0

        # Try alternate price fields
        if yes_bid == 0 and yes_ask == 0:
            last_price = market.get('lastPrice', market.get('last_price', 0)) or 0
            if last_price > 0:
                # Synthesize bid/ask from last price
                yes_bid = last_price - 0.01
                yes_ask = last_price + 0.01

        # Get token mints if available
        yes_mint = market.get('yesMint', market.get('yes_mint', ''))
        no_mint = market.get('noMint', market.get('no_mint', ''))

        ticker = SolanaTicker(
            symbol=market_ticker,
            name=market_title,
            yes_mint=yes_mint,
            no_mint=no_mint,
            market_ticker=market_ticker,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
        )

        prev = self.last_prices.get(market_ticker, (0, 0))
        prev_bid, prev_ask = prev

        size = Decimal('100')

        # Normalize prices - DFlow may use 0-1 or 0-100
        def _normalize_price(p: float) -> Decimal:
            if p > 1:
                return Decimal(str(p)) / Decimal('100')
            return Decimal(str(p))

        if yes_bid > 0 and yes_bid != prev_bid:
            bid_price = _normalize_price(yes_bid)
            if Decimal('0') < bid_price < Decimal('1'):
                events.append(
                    OrderBookEvent(
                        ticker=ticker,
                        price=bid_price,
                        size=size,
                        size_delta=size,
                        side='bid',
                    )
                )

        if yes_ask > 0 and yes_ask != prev_ask:
            ask_price = _normalize_price(yes_ask)
            if Decimal('0') < ask_price < Decimal('1'):
                events.append(
                    OrderBookEvent(
                        ticker=ticker,
                        price=ask_price,
                        size=size,
                        size_delta=size,
                        side='ask',
                    )
                )

        self.last_prices[market_ticker] = (yes_bid, yes_ask)
        return events

    async def _fetch_and_emit_news(
        self,
        market_question: str,
        event_ticker: str,
        ticker: SolanaTicker,
    ) -> None:
        """Emit market title as NewsEvent for strategy consumption."""
        try:
            news_event = NewsEvent(
                news=market_question,
                title=market_question,
                source='dflow',
                description=market_question,
                event_id=event_ticker,
                ticker=ticker,
            )
            await self.event_queue.put(news_event)
        except Exception as e:
            logger.warning('News emit error for "%s": %s', market_question[:50], e)

    async def _poll_data(self) -> None:
        while True:
            try:
                markets = await self._fetch_markets()
                logger.info('Fetched %d DFlow markets', len(markets))

                new_event_tickers: set[str] = set()
                news_queue: list[tuple[str, str, SolanaTicker]] = []

                for market in markets[:100]:
                    market_ticker = market.get('ticker', market.get('marketTicker', ''))
                    event_ticker = market.get('_event_ticker', '')
                    market_title = market.get('title', market.get('question', ''))

                    if not market_ticker:
                        continue

                    is_new = (
                        event_ticker
                        and event_ticker not in self.processed_event_tickers
                    )

                    if is_new:
                        new_event_tickers.add(event_ticker)
                        self.processed_event_tickers.add(event_ticker)
                        with open(self.event_cache_file, 'a') as f:
                            f.write(
                                json.dumps(
                                    {
                                        'event_ticker': event_ticker,
                                        'market_ticker': market_ticker,
                                        'title': market_title,
                                    }
                                )
                                + '\n'
                            )

                    ob_events = self._market_to_order_book_events(market)
                    for ob_event in ob_events:
                        await self.event_queue.put(ob_event)

                    if (
                        event_ticker in new_event_tickers
                        and event_ticker not in self._news_fetched_events
                    ):
                        self._news_fetched_events.add(event_ticker)
                        tkr = SolanaTicker(
                            symbol=market_ticker,
                            name=market_title,
                            market_ticker=market_ticker,
                            event_ticker=event_ticker,
                            series_ticker=market.get('_series_ticker', ''),
                        )
                        news_queue.append((market_title, event_ticker, tkr))

                if news_queue:
                    batch = news_queue[:5]
                    logger.info(
                        'Emitting news for %d/%d new DFlow markets...',
                        len(batch),
                        len(news_queue),
                    )
                    for question, evt_ticker, tkr in batch:
                        await self._fetch_and_emit_news(
                            market_question=question,
                            event_ticker=evt_ticker,
                            ticker=tkr,
                        )

            except Exception as e:
                logger.error('Error in DFlow polling loop: %s', e)

            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())

    async def stop(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
