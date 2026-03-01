import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any

from ...events.events import Event, NewsEvent, OrderBookEvent
from ...ticker.ticker import KalshiTicker
from ..data_source import DataSource

logger = logging.getLogger(__name__)


async def _retry_with_backoff(
    func,
    *args,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    **kwargs,
):
    for attempt in range(max_attempts):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt >= max_attempts - 1:
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning(
                'Attempt %d/%d failed: %s. Retrying in %.1fs',
                attempt + 1,
                max_attempts,
                e,
                delay,
            )
            await asyncio.sleep(delay)


class LiveKalshiDataSource(DataSource):
    """Polls Kalshi REST API for markets, uses market-level bid/ask data."""

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        event_cache_file: str = 'kalshi_events_cache.jsonl',
        polling_interval: float = 60.0,
        reprocess_on_start: bool = True,
    ):
        self.polling_interval = polling_interval
        self.event_cache_file = event_cache_file
        self.processed_event_tickers: set[str] = set()
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.last_prices: dict[str, tuple[int, int]] = {}
        self._news_fetched_events: set[str] = set()
        self._poll_task: asyncio.Task | None = None

        # Setup Kalshi API client
        from kalshi_python import Configuration
        from kalshi_python.api.markets_api import MarketsApi
        from kalshi_python.api_client import ApiClient

        config = Configuration(host='https://api.elections.kalshi.com/trade-api/v2')

        key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
        pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')

        self._api_client = ApiClient(configuration=config)
        if key_id and pk_path:
            self._api_client.set_kalshi_auth(key_id, pk_path)
        else:
            logger.warning(
                'Kalshi API credentials not provided. '
                'Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH env vars.'
            )

        self._markets_api = MarketsApi(self._api_client)

        # Load cache
        if os.path.exists(self.event_cache_file):
            with open(self.event_cache_file) as f:
                for line in f:
                    try:
                        cached = json.loads(line.strip())
                        if 'event_ticker' in cached:
                            # Always track which events we've already sent news for,
                            # to avoid re-triggering LLM calls on restart.
                            self._news_fetched_events.add(cached['event_ticker'])
                            if not reprocess_on_start:
                                self.processed_event_tickers.add(cached['event_ticker'])
                    except json.JSONDecodeError:
                        pass

    async def _fetch_markets(self) -> list[dict[str, Any]]:
        """Fetch open markets from Kalshi API, filtering for liquid markets."""
        try:
            all_markets: list[dict[str, Any]] = []
            cursor = None

            for _ in range(5):
                kwargs: dict[str, Any] = {'status': 'open', 'limit': 200}
                if cursor:
                    kwargs['cursor'] = cursor
                response = await asyncio.to_thread(
                    self._markets_api.get_markets, **kwargs
                )
                raw_markets = response.markets if hasattr(response, 'markets') else []
                for m in raw_markets or []:
                    d: dict[str, Any] = (
                        m.to_dict() if hasattr(m, 'to_dict') else dict(m)
                    )
                    # Only include markets with at least an ask price
                    yes_ask = d.get('yes_ask', 0) or 0
                    if yes_ask == 0:
                        continue
                    all_markets.append(d)

                cursor = response.cursor if hasattr(response, 'cursor') else None
                if not cursor:
                    break
                await asyncio.sleep(0.3)

            return all_markets
        except Exception as e:
            logger.error('Error fetching Kalshi markets: %s', e)
            return []

    def _market_to_order_book_events(
        self,
        market: dict[str, Any],
    ) -> list[OrderBookEvent]:
        """Create OrderBookEvents from market-level yes_bid/yes_ask data.

        Kalshi's bulk market API includes top-of-book prices (in cents).
        We use these directly instead of fetching individual orderbooks.
        """
        events = []
        market_ticker = market.get('ticker', '')
        market_title = market.get('title', '')
        event_ticker = market.get('event_ticker', '')
        series_ticker = market.get('series_ticker', '')

        yes_bid = market.get('yes_bid', 0) or 0
        yes_ask = market.get('yes_ask', 0) or 0

        ticker = KalshiTicker(
            symbol=market_ticker,
            name=market_title,
            market_ticker=market_ticker,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
        )

        prev = self.last_prices.get(market_ticker, (0, 0))
        prev_bid, prev_ask = prev

        # Synthetic size for top-of-book
        size = Decimal('100')

        # Emit bid event if price changed
        if yes_bid > 0 and yes_bid != prev_bid:
            bid_price = Decimal(str(yes_bid)) / Decimal('100')
            events.append(
                OrderBookEvent(
                    ticker=ticker,
                    price=bid_price,
                    size=size,
                    size_delta=size,
                    side='bid',
                )
            )

        # Emit ask event if price changed
        if yes_ask > 0 and yes_ask != prev_ask:
            ask_price = Decimal(str(yes_ask)) / Decimal('100')
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
        ticker: KalshiTicker,
    ) -> None:
        """Emit market title as NewsEvent for strategy consumption."""
        try:
            news_event = NewsEvent(
                news=market_question,
                title=market_question,
                source='kalshi',
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
                markets = await _retry_with_backoff(self._fetch_markets)
                logger.info('Fetched %d liquid Kalshi markets', len(markets))

                new_event_tickers: set[str] = set()
                news_queue: list[tuple[str, str, KalshiTicker]] = []

                # Process up to 100 markets per poll
                for market in markets[:100]:
                    market_ticker = market.get('ticker', '')
                    event_ticker = market.get('event_ticker', '')
                    market_title = market.get('title', '')

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

                    # Use market-level bid/ask to create order book events
                    ob_events = self._market_to_order_book_events(market)

                    for ob_event in ob_events:
                        await self.event_queue.put(ob_event)

                    has_ask = (market.get('yes_ask', 0) or 0) > 0

                    # Queue news fetch for new events (once per event)
                    if (
                        event_ticker in new_event_tickers
                        and event_ticker not in self._news_fetched_events
                        and has_ask
                    ):
                        self._news_fetched_events.add(event_ticker)
                        tkr = KalshiTicker(
                            symbol=market_ticker,
                            name=market_title,
                            market_ticker=market_ticker,
                            event_ticker=event_ticker,
                            series_ticker=market.get('series_ticker', ''),
                        )
                        news_queue.append((market_title, event_ticker, tkr))

                # Emit news for up to 5 new markets per poll
                if news_queue:
                    batch = news_queue[:5]
                    logger.info(
                        'Emitting news for %d/%d new Kalshi markets...',
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
                logger.error('Error in Kalshi polling loop: %s', e)

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
