import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import feedparser
import httpx
from py_clob_client.client import ClobClient

from ...events.events import Event, NewsEvent, OrderBookEvent
from ...ticker.ticker import PolyMarketTicker
from ..data_source import DataSource

logger = logging.getLogger(__name__)


class LivePolyMarketDataSource(DataSource):
    event_cache_file: str
    polling_interval: float
    processed_event_ids: set[str]
    event_queue: asyncio.Queue
    clob_client: ClobClient
    last_order_book_state: dict[str, Decimal]

    def __init__(
        self,
        event_cache_file: str = 'events_cache.jsonl',
        polling_interval: float = 60.0,
        orderbook_refresh_interval: float = 10.0,
        reprocess_on_start: bool = True,
    ):
        self.event_cache_file = event_cache_file
        self.polling_interval = polling_interval
        self.orderbook_refresh_interval = orderbook_refresh_interval
        self.processed_event_ids: set[str] = set()
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clob_client = ClobClient('https://clob.polymarket.com')
        self.last_order_book_state: dict[str, Decimal] = {}
        self._poll_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._first_poll = True
        self._reprocess_on_start = reprocess_on_start
        # Track tickers with actual liquidity for periodic refresh
        self._known_tickers: dict[str, PolyMarketTicker] = {}  # token_id → ticker
        self._last_refresh_time: dict[str, float] = {}  # token_id → timestamp
        self._max_known_tickers: int = 500
        # Priority tokens (positions we hold) — always refreshed first
        self._priority_tokens: set[str] = set()
        # Rotation offset for non-priority token refresh batches
        self._refresh_offset: int = 0
        # Semaphore to limit concurrent order book fetches
        self._fetch_semaphore = asyncio.Semaphore(5)

        # Load and deduplicate cache file on startup (Fix 5)
        self._cached_event_ids: set[str] = set()
        if os.path.exists(self.event_cache_file):
            seen_ids: set[str] = set()
            unique_lines: list[str] = []
            with open(self.event_cache_file) as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())
                        eid = str(event.get('id', ''))
                        if eid and eid not in seen_ids:
                            seen_ids.add(eid)
                            unique_lines.append(line.strip())
                    except json.JSONDecodeError:
                        continue
            # Rewrite deduped cache file
            with open(self.event_cache_file, 'w') as f:
                for line in unique_lines:
                    f.write(line + '\n')
            self._cached_event_ids = seen_ids

        if not reprocess_on_start:
            self.processed_event_ids = set(self._cached_event_ids)

    async def _fetch_events(self) -> list[dict[str, Any]]:
        all_events: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                response = await client.get(
                    'https://gamma-api.polymarket.com/events',
                    params={
                        'active': 'true',
                        'closed': 'false',
                        'limit': limit,
                        'offset': offset,
                    },
                )
                if response.status_code != 200:
                    break
                events = response.json()
                if not events:
                    break
                all_events.extend(events)
                if len(events) < limit:
                    break  # Last page
                # On subsequent polls (not first), only fetch first page for new events
                if not self._first_poll:
                    break
                offset += limit
        return all_events

    def _fetch_order_book_sync(self, token_id: str) -> Any:
        """Synchronous order book fetch (runs in thread pool)."""
        try:
            return self.clob_client.get_order_book(token_id)
        except Exception:
            return None

    async def _fetch_order_book(self, token_id: str) -> Any:
        """Non-blocking order book fetch via thread pool."""
        return await asyncio.to_thread(self._fetch_order_book_sync, token_id)

    def _process_order_book_to_events(
        self,
        token_id: str,
        market_title: str,
        order_book: Any,
        market_id: str = '',
        event_id: str = '',
        no_token_id: str = '',
    ) -> list[OrderBookEvent]:
        events = []

        if not order_book:
            return events

        # Create PolyMarketTicker with all available IDs
        ticker = PolyMarketTicker(
            symbol=token_id,
            name=market_title,
            token_id=token_id,
            market_id=market_id,
            event_id=event_id,
            no_token_id=no_token_id,
        )

        # Only track tickers that have actual liquidity for refresh
        if order_book.bids or order_book.asks:
            self._known_tickers[token_id] = ticker
            self._last_refresh_time[token_id] = time.monotonic()

        for bid in order_book.bids:
            price = Decimal(bid.price)
            size = Decimal(bid.size)

            prev_size = Decimal('0')
            prev_key = f'{token_id}:{bid.price}:bid'
            if prev_key in self.last_order_book_state:
                prev_size = self.last_order_book_state[prev_key]

            size_delta = size - prev_size

            self.last_order_book_state[prev_key] = size

            if size_delta != 0:
                event = OrderBookEvent(
                    ticker=ticker,
                    price=price,
                    size=size,
                    size_delta=size_delta,
                    side='bid',
                )
                events.append(event)

        for ask in order_book.asks:
            price = Decimal(ask.price)
            size = Decimal(ask.size)

            prev_size = Decimal('0')
            prev_key = f'{token_id}:{ask.price}:ask'
            if prev_key in self.last_order_book_state:
                prev_size = self.last_order_book_state[prev_key]

            size_delta = size - prev_size

            self.last_order_book_state[prev_key] = size

            if size_delta != 0:
                event = OrderBookEvent(
                    ticker=ticker,
                    price=price,
                    size=size,
                    size_delta=size_delta,
                    side='ask',
                )
                events.append(event)

        return events

    async def _poll_data(self) -> None:
        while True:
            try:
                events = await self._fetch_events()
                for event in events:
                    event_id = str(event.get('id'))
                    if event_id not in self.processed_event_ids:
                        # For cached events on first poll, skip heavy order book fetching
                        is_cached = (
                            self._first_poll and event_id in self._cached_event_ids
                        )

                        # Skip _fetch_market_history entirely (Fix 2) — history
                        # data is always empty, so the HTTP requests are wasted.
                        enriched_event = event

                        for market in enriched_event.get('markets', []):
                            market_title = market.get(
                                'question', market.get('title', '')
                            )
                            market_id = market.get('id', '')
                            token_ids = json.loads(market.get('clobTokenIds', '[]'))

                            # Convention: clobTokenIds[0] = YES, [1] = NO
                            yes_token_id = token_ids[0] if len(token_ids) > 0 else ''
                            no_token_id = token_ids[1] if len(token_ids) > 1 else ''

                            if not is_cached:
                                # New event: fetch order books (full processing)
                                for idx, token_id in enumerate(token_ids):
                                    complement_id = ''
                                    if idx == 0 and len(token_ids) > 1:
                                        complement_id = token_ids[1]
                                    elif idx == 1 and len(token_ids) > 0:
                                        complement_id = token_ids[0]

                                    order_book = await self._fetch_order_book(token_id)
                                    order_book_events = (
                                        self._process_order_book_to_events(
                                            token_id,
                                            market_title,
                                            order_book,
                                            market_id=market_id,
                                            event_id=event_id,
                                            no_token_id=complement_id,
                                        )
                                    )
                                    for ob_event in order_book_events:
                                        await self.event_queue.put(ob_event)
                            else:
                                # Cached event: just register tickers for refresh loop
                                for idx, token_id in enumerate(token_ids):
                                    complement_id = ''
                                    if idx == 0 and len(token_ids) > 1:
                                        complement_id = token_ids[1]
                                    elif idx == 1 and len(token_ids) > 0:
                                        complement_id = token_ids[0]
                                    ticker = PolyMarketTicker(
                                        symbol=token_id,
                                        name=market_title,
                                        token_id=token_id,
                                        market_id=market_id,
                                        event_id=event_id,
                                        no_token_id=complement_id,
                                    )
                                    self._known_tickers[token_id] = ticker

                            # Emit NewsEvent only for genuinely new events (not cached)
                            # to avoid triggering LLM calls for hundreds of stale markets on restart
                            if yes_token_id and not is_cached:
                                yes_ticker = PolyMarketTicker(
                                    symbol=yes_token_id,
                                    name=market_title,
                                    token_id=yes_token_id,
                                    market_id=market_id,
                                    event_id=event_id,
                                    no_token_id=no_token_id,
                                )
                                news_content = f'{market_title}: {enriched_event.get("description", "")}'
                                news_event = NewsEvent(
                                    news=news_content,
                                    title=market_title,
                                    source='polymarket',
                                    event_id=event_id,
                                    ticker=yes_ticker,
                                )
                                await self.event_queue.put(news_event)

                        self.processed_event_ids.add(event_id)
                        if not is_cached:
                            with open(self.event_cache_file, 'a') as f:
                                f.write(json.dumps(enriched_event) + '\n')

                if self._first_poll:
                    self._first_poll = False
            except Exception as e:
                logger.error('Error in polling loop: %s', e, exc_info=True)

            await asyncio.sleep(self.polling_interval)

    def watch_token(self, token_id: str) -> None:
        """Mark a token as priority for order book refresh (e.g. when position opened)."""
        self._priority_tokens.add(token_id)

    def unwatch_token(self, token_id: str) -> None:
        """Remove a token from priority refresh (e.g. when position closed)."""
        self._priority_tokens.discard(token_id)

    def _evict_stale_tickers(self) -> None:
        """Evict non-priority tickers when _known_tickers exceeds max size.

        Removes the least-recently-refreshed non-priority tokens and cleans
        up their associated last_order_book_state entries.
        """
        if len(self._known_tickers) <= self._max_known_tickers:
            return

        # Collect non-priority tokens sorted by last refresh time (oldest first)
        evict_candidates = [
            (tid, self._last_refresh_time.get(tid, 0.0))
            for tid in self._known_tickers
            if tid not in self._priority_tokens
        ]
        evict_candidates.sort(key=lambda x: x[1])

        num_to_evict = len(self._known_tickers) - self._max_known_tickers
        for tid, _ in evict_candidates[:num_to_evict]:
            del self._known_tickers[tid]
            self._last_refresh_time.pop(tid, None)
            # Clean up last_order_book_state entries for evicted token
            stale_keys = [
                k for k in self.last_order_book_state if k.startswith(f'{tid}:')
            ]
            for k in stale_keys:
                del self.last_order_book_state[k]

    async def _process_refresh_result(
        self,
        token_id: str,
        ticker: PolyMarketTicker,
        order_book: Any,
    ) -> None:
        """Process a fetched order book: emit events for changed/stale levels."""
        # --- Full snapshot: clear stale levels for this token ---
        old_bid_keys = {
            k
            for k in self.last_order_book_state
            if k.startswith(f'{token_id}:') and k.endswith(':bid')
        }
        old_ask_keys = {
            k
            for k in self.last_order_book_state
            if k.startswith(f'{token_id}:') and k.endswith(':ask')
        }

        seen_bid_keys: set[str] = set()
        for bid in order_book.bids:
            price = Decimal(bid.price)
            size = Decimal(bid.size)
            key = f'{token_id}:{bid.price}:bid'
            seen_bid_keys.add(key)
            prev_size = self.last_order_book_state.get(key, Decimal('0'))
            self.last_order_book_state[key] = size
            if size != prev_size:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=price,
                        size=size,
                        size_delta=size - prev_size,
                        side='bid',
                    )
                )

        seen_ask_keys: set[str] = set()
        for ask in order_book.asks:
            price = Decimal(ask.price)
            size = Decimal(ask.size)
            key = f'{token_id}:{ask.price}:ask'
            seen_ask_keys.add(key)
            prev_size = self.last_order_book_state.get(key, Decimal('0'))
            self.last_order_book_state[key] = size
            if size != prev_size:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=price,
                        size=size,
                        size_delta=size - prev_size,
                        side='ask',
                    )
                )

        # Remove levels that disappeared from the real order book
        for stale_key in old_bid_keys - seen_bid_keys:
            price_str = stale_key.split(':')[1]
            prev_size = self.last_order_book_state.pop(stale_key, Decimal('0'))
            if prev_size > 0:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=Decimal(price_str),
                        size=Decimal('0'),
                        size_delta=-prev_size,
                        side='bid',
                    )
                )
        for stale_key in old_ask_keys - seen_ask_keys:
            price_str = stale_key.split(':')[1]
            prev_size = self.last_order_book_state.pop(stale_key, Decimal('0'))
            if prev_size > 0:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=Decimal(price_str),
                        size=Decimal('0'),
                        size_delta=-prev_size,
                        side='ask',
                    )
                )

        self._last_refresh_time[token_id] = time.monotonic()

    async def _refresh_loop(self) -> None:
        """Periodically re-fetch order books for position tokens.

        Only refreshes priority tokens (ones we hold positions in) plus
        a small rotating batch of other known tickers. Fetches are done
        concurrently with a semaphore to limit parallelism.
        """
        while True:
            await asyncio.sleep(self.orderbook_refresh_interval)

            # Evict stale tickers if we've exceeded the max
            self._evict_stale_tickers()

            # Build refresh list: priority tokens first, then a batch of others
            refresh_ids: list[tuple[str, PolyMarketTicker]] = []
            for tid in list(self._priority_tokens):
                ticker = self._known_tickers.get(tid)
                if ticker is None:
                    # Priority token was evicted or never fully registered;
                    # create a minimal ticker so we still refresh its order book.
                    ticker = PolyMarketTicker(symbol=tid, name='', token_id=tid)
                    self._known_tickers[tid] = ticker
                refresh_ids.append((tid, ticker))

            # Add a rotating batch of non-priority tokens
            non_priority = [
                (tid, t)
                for tid, t in self._known_tickers.items()
                if tid not in self._priority_tokens
            ]
            if non_priority:
                batch_size = 20
                start = self._refresh_offset % len(non_priority)
                batch = non_priority[start : start + batch_size]
                if len(batch) < batch_size:
                    batch.extend(non_priority[: batch_size - len(batch)])
                self._refresh_offset += batch_size
                refresh_ids.extend(batch)

            # Clean up last_order_book_state for tokens no longer tracked
            tracked_token_ids = set(self._known_tickers.keys())
            stale_state_keys = [
                k
                for k in self.last_order_book_state
                if k.split(':')[0] not in tracked_token_ids
            ]
            for k in stale_state_keys:
                del self.last_order_book_state[k]

            # Fetch order books concurrently with semaphore
            async def _fetch_one(token_id: str, ticker: PolyMarketTicker):
                async with self._fetch_semaphore:
                    order_book = await self._fetch_order_book(token_id)
                    return (token_id, ticker, order_book)

            results = await asyncio.gather(
                *[_fetch_one(tid, t) for tid, t in refresh_ids],
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, Exception):
                    continue
                token_id, ticker, order_book = result
                if not order_book:
                    continue
                try:
                    await self._process_refresh_result(token_id, ticker, order_book)
                except Exception:
                    logger.debug(
                        'Error processing refresh for %s', token_id, exc_info=True
                    )

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        for task in (self._poll_task, self._refresh_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._refresh_task = None

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None


class LiveNewsDataSource(DataSource):
    api_token: str
    cache_file: str
    polling_interval: float
    max_articles_per_poll: int
    languages: list[str]
    categories: list[str]
    base_url: str
    processed_article_ids: set[str]
    event_queue: asyncio.Queue

    def __init__(
        self,
        api_token: str,
        cache_file: str = 'news_cache.jsonl',
        polling_interval: float = 300.0,
        max_articles_per_poll: int = 10,
        languages: list[str] = None,
        categories: list[str] = None,
    ):
        self.api_token = api_token
        self.cache_file = cache_file
        self.polling_interval = polling_interval
        self.max_articles_per_poll = max_articles_per_poll
        self.languages = languages or ['en']
        self.categories = categories or []
        self.base_url = 'https://api.thenewsapi.com/v1/news/headlines'

        self.processed_article_ids = set()
        self.event_queue = asyncio.Queue()
        self._poll_task: asyncio.Task | None = None

        self._load_processed_articles()

    def _load_processed_articles(self) -> None:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file) as f:
                    for line in f:
                        article = json.loads(line.strip())
                        if 'uuid' in article:
                            self.processed_article_ids.add(article['uuid'])
            except Exception as e:
                logger.warning('Error loading article cache: %s', e)

    async def _fetch_articles(self) -> list[dict[str, Any]]:  # noqa: C901
        try:
            today = datetime.now()

            params = {
                'api_token': self.api_token,
                'language': ','.join(self.languages),
                'published_on': today.strftime('%Y-%m-%d'),
                'locale': 'us',
                'limit': self.max_articles_per_poll,
            }

            if self.categories:
                params['categories'] = ','.join(self.categories)

            async with httpx.AsyncClient() as client:
                response = await client.get(self.base_url, params=params)

                if response.status_code == 200:
                    try:
                        # Log raw response for debugging
                        logger.debug('Raw API response: %s...', response.text[:500])

                        # Try to parse the response as JSON
                        data = response.json()

                        # Verify that data is a dictionary
                        if not isinstance(data, dict):
                            logger.warning(
                                'API returned non-dictionary response: %s', type(data)
                            )
                            return []

                        # Check if 'data' key exists
                        if 'data' not in data:
                            logger.warning(
                                "API response missing 'data' key. Available keys: %s",
                                list(data.keys()),
                            )

                            # Try to extract articles from other possible structures
                            if 'articles' in data:
                                logger.debug("Found 'articles' key instead of 'data'")
                                articles = data.get('articles', [])
                                if isinstance(articles, list):
                                    return articles

                            # If this looks like it might be a single article
                            if 'title' in data or 'url' in data:
                                logger.debug('Response appears to be a single article')
                                return [data]

                            # Try other potential formats
                            for key, value in data.items():
                                if isinstance(value, list):
                                    logger.debug(
                                        "Found list under key '%s', trying this instead",
                                        key,
                                    )
                                    return value

                            return []

                        articles_data = data.get('data', [])

                        # Check if articles_data is a list
                        if isinstance(articles_data, list):
                            return articles_data

                        # If articles_data is a dict, it might be organized by categories
                        if isinstance(articles_data, dict):
                            logger.debug(
                                "'data' is a dictionary, attempting to flatten"
                            )
                            flattened_articles = []

                            for category, articles in articles_data.items():
                                if isinstance(articles, list):
                                    logger.debug(
                                        "Found %d articles in category '%s'",
                                        len(articles),
                                        category,
                                    )
                                    flattened_articles.extend(articles)
                                elif isinstance(articles, dict):
                                    logger.debug(
                                        "Category '%s' contains a dictionary, not a list",
                                        category,
                                    )
                                    flattened_articles.append(
                                        articles
                                    )  # Add as a single article

                            logger.debug(
                                'Flattened %d articles from categories',
                                len(flattened_articles),
                            )
                            return flattened_articles

                        logger.warning(
                            "Unexpected 'data' format: %s", type(articles_data)
                        )
                        return []

                    except json.JSONDecodeError as e:
                        logger.warning('Failed to parse API response as JSON: %s', e)
                        logger.debug('Raw response: %s...', response.text[:200])
                        return []
                else:
                    logger.warning('API error: HTTP %d', response.status_code)
                    logger.debug('Response: %s...', response.text[:200])
                    return []
        except Exception as e:
            logger.error('Error fetching articles: %s', e, exc_info=True)
            return []

    def _create_news_event(self, article: dict[str, Any]) -> NewsEvent:
        # Handle published_at date
        published_at = None
        if 'published_at' in article and article['published_at']:
            try:
                published_at_str = str(article['published_at'])
                if 'Z' in published_at_str:
                    published_at = datetime.fromisoformat(
                        published_at_str.replace('Z', '+00:00')
                    )
                else:
                    published_at = datetime.fromisoformat(published_at_str)
            except (ValueError, TypeError) as e:
                logger.warning(
                    "Error parsing date '%s': %s", article.get('published_at'), e
                )
                published_at = datetime.now()
        else:
            published_at = datetime.now()

        # Extract title safely
        title = ''
        if 'title' in article and article['title']:
            title = str(article['title'])

        # Extract description safely
        description = ''
        if 'description' in article and article['description']:
            description = str(article['description'])
        elif 'snippet' in article and article['snippet']:
            description = str(article['snippet'])

        # Create news content
        news_content = f'{title}: {description}' if description else title
        if not news_content:
            news_content = 'No content available'

        # Extract categories safely
        categories = []
        if 'categories' in article and isinstance(article['categories'], list):
            categories = article['categories']

        # Create the event
        return NewsEvent(
            news=news_content,
            title=title,
            source=str(article.get('source', '')),
            url=str(article.get('url', '')),
            published_at=published_at,
            categories=categories,
            description=description,
            image_url=str(article.get('image_url', '')),
            uuid=str(article.get('uuid', '')),
            event_id=str(article.get('uuid', '')),
        )

    async def _retry_on_error(
        self, func, *args, retries: int = 3, delay: int = 2, **kwargs
    ) -> Any:
        for attempt in range(retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.warning('Attempt %d failed: %s', attempt + 1, e)
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise

    def _save_article(self, article: dict[str, Any]) -> None:
        try:
            with open(self.cache_file, 'a', encoding='utf-8') as f:
                json.dump(article, f, ensure_ascii=False)
                f.write('\n')
        except Exception as e:
            logger.error('Error saving article: %s', e)

    async def _poll_data(self) -> None:
        while True:
            try:
                logger.info('Fetching news articles...')
                articles = await self._retry_on_error(self._fetch_articles)
                logger.info('Received %d articles', len(articles))

                for article in articles:
                    try:
                        # Verify article is a dictionary
                        if not isinstance(article, dict):
                            logger.debug(
                                'Skipping non-dictionary article: %s', type(article)
                            )
                            continue

                        # Check for UUID
                        article_id = article.get('uuid')
                        if not article_id:
                            logger.debug('Article missing UUID, generating random ID')
                            article_id = str(uuid.uuid4())
                            article['uuid'] = article_id

                        # Skip processed articles
                        if article_id in self.processed_article_ids:
                            logger.debug(
                                'Skipping already processed article: %s', article_id
                            )
                            continue

                        # Create news event
                        event = self._create_news_event(article)

                        # Add to queue
                        await self.event_queue.put(event)
                        logger.debug(
                            'Added article to queue: %s - %s',
                            article_id,
                            article.get('title', 'No title'),
                        )

                        # Mark as processed and save
                        self.processed_article_ids.add(article_id)
                        self._save_article(article)

                    except Exception as e:
                        logger.error('Error processing article: %s', e, exc_info=True)
                        continue

            except Exception as e:
                logger.error('Error in polling loop: %s', e, exc_info=True)

            logger.debug('Sleeping for %s seconds', self.polling_interval)
            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())

    async def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def get_next_event(self) -> NewsEvent | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None


class LiveRSSNewsDataSource(DataSource):
    cache_file: str
    polling_interval: float
    max_articles_per_poll: int
    languages: list[str]
    categories: list[str]
    processed_article_ids: set[str]
    event_queue: asyncio.Queue

    RSS_FEEDS = {
        'https://feeds.content.dowjones.io/public/rss/RSSOpinion': ['opinion'],
        'https://feeds.content.dowjones.io/public/rss/RSSWorldNews': ['world'],
        'https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness': ['business'],
        'https://feeds.content.dowjones.io/public/rss/RSSMarketsMain': ['finance'],
        'https://feeds.content.dowjones.io/public/rss/RSSWSJD': ['technology'],
        'https://feeds.content.dowjones.io/public/rss/RSSLifestyle': ['lifestyle'],
        'https://feeds.content.dowjones.io/public/rss/RSSUSnews': ['us'],
        'https://feeds.content.dowjones.io/public/rss/socialpoliticsfeed': ['politics'],
        'https://feeds.content.dowjones.io/public/rss/socialeconomyfeed': ['economy'],
        'https://feeds.content.dowjones.io/public/rss/RSSArtsCulture': ['arts'],
        'https://feeds.content.dowjones.io/public/rss/latestnewsrealestate': [
            'real estate'
        ],
        'https://feeds.content.dowjones.io/public/rss/RSSPersonalFinance': [
            'personal finance'
        ],
        'https://feeds.content.dowjones.io/public/rss/socialhealth': ['health'],
        'https://feeds.content.dowjones.io/public/rss/RSSStyle': ['style'],
        'https://feeds.content.dowjones.io/public/rss/rsssportsfeed': ['sports'],
    }

    def __init__(
        self,
        cache_file: str = 'rss_news_cache.jsonl',
        polling_interval: float = 300.0,
        max_articles_per_poll: int = 10,
        categories: list[str] = None,
    ):
        self.cache_file = cache_file
        self.polling_interval = polling_interval
        self.max_articles_per_poll = max_articles_per_poll
        self.languages = ['en-us']
        self.categories = categories or []
        feedparser.CACHE_DIRECTORY = None
        feedparser._check_cache = lambda *args, **kwargs: None
        self.processed_article_ids = set()
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._poll_task: asyncio.Task | None = None
        self._load_processed_articles()

    def _load_processed_articles(self) -> None:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, encoding='utf-8') as f:
                    for line in f:
                        article = json.loads(line.strip())
                        if 'uuid' in article:
                            self.processed_article_ids.add(article['uuid'])
            except Exception as e:
                logger.error('Error loading article cache: %s', e)

    def _save_article(self, article: dict[str, Any]) -> None:
        try:
            with open(self.cache_file, 'a', encoding='utf-8') as f:
                json.dump(article, f, ensure_ascii=False)
                f.write('\n')
        except Exception as e:
            logger.error('Error saving article: %s', e)

    def _extract_image_url(self, entry) -> str:
        if 'media_content' in entry:
            for media in entry.get('media_content', []):
                if isinstance(media, dict) and 'url' in media:
                    return media.get('url', '')
        if 'media_thumbnail' in entry:
            for media in entry.get('media_thumbnail', []):
                if isinstance(media, dict) and 'url' in media:
                    return media.get('url', '')
        return ''

    def _create_news_event(self, entry, feed_title, tags) -> NewsEvent:
        title = entry.get('title', 'No title')

        description = entry.get('description', '')

        if not description and 'summary' in entry:
            description = entry.get('summary', '')

        link = entry.get('link', '')

        guid = entry.get('guid', '')
        if isinstance(guid, dict):
            guid = guid.get('value', '')
        if not guid:
            guid = link
        if not guid:
            guid = str(uuid.uuid4())

        published_at = datetime.now(timezone.utc)
        if 'pubDate' in entry:
            try:
                pub_date_str = entry.get('pubDate', '')
                published_at = datetime.strptime(
                    pub_date_str, '%a, %d %b %Y %H:%M:%S %Z'
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError) as e:
                logger.debug("Error parsing date '%s': %s", entry.get('pubDate', ''), e)
        news_content = f'{title}: {description}' if description else title
        image_url = self._extract_image_url(entry)
        return NewsEvent(
            news=news_content,
            title=title,
            source=feed_title,
            url=link,
            published_at=published_at,
            categories=tags,
            description=description,
            image_url=image_url,
            uuid=guid,
            event_id=guid,
        )

    async def _retry_on_error(
        self, func, *args, retries: int = 3, delay: int = 2, **kwargs
    ) -> Any:
        for attempt in range(retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error('Attempt %d failed: %s', attempt + 1, e)
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise

    async def _fetch_rss_feeds(self) -> list[dict[str, Any]]:  # noqa: C901
        results = []
        processed_count = 0

        for feed_url, tags in self.RSS_FEEDS.items():
            if processed_count >= self.max_articles_per_poll:
                break

            try:
                logger.debug('Fetching %s', feed_url)
                feed = feedparser.parse(feed_url)

                if not feed or not hasattr(feed, 'entries'):
                    logger.info('Feed from %s is invalid', feed_url)
                    continue

                feed_title = 'Unknown Source'
                if hasattr(feed, 'feed') and hasattr(feed.feed, 'title'):
                    feed_title = feed.feed.title

                if self.categories and not any(cat in tags for cat in self.categories):
                    continue

                for entry in feed.entries:
                    if processed_count >= self.max_articles_per_poll:
                        break

                    guid = entry.get('guid', '')
                    if isinstance(guid, dict):
                        guid = guid.get('value', '')
                    if not guid:
                        guid = entry.get('link', '')
                    if not guid:
                        guid = str(uuid.uuid4())

                    if guid in self.processed_article_ids:
                        logger.debug('Skipping processed article: %s', guid)
                        continue

                    results.append((entry, feed_title, tags))
                    processed_count += 1

            except Exception as e:
                logger.error('Error fetching feed %s: %s', feed_url, e, exc_info=True)
        return results

    async def _poll_data(self) -> None:
        while True:
            try:
                news_items = await self._retry_on_error(self._fetch_rss_feeds)

                for entry, feed_title, tags in news_items:
                    try:
                        guid = entry.get('guid', '')
                        if isinstance(guid, dict):
                            guid = guid.get('value', '')
                        if not guid:
                            guid = entry.get('link', '')
                        if not guid:
                            guid = str(uuid.uuid4())

                        article = {
                            'uuid': guid,
                            'title': entry.get('title', 'No title'),
                            'description': entry.get('description', ''),
                            'link': entry.get('link', ''),
                            'pubDate': entry.get('pubDate', ''),
                            'source': feed_title,
                            'categories': tags,
                            'image_url': self._extract_image_url(entry),
                        }

                        event = self._create_news_event(entry, feed_title, tags)

                        await self.event_queue.put(event)

                        self.processed_article_ids.add(guid)
                        self._save_article(article)
                    except Exception as e:
                        logger.error('Error processing article: %s', e, exc_info=True)
                        continue

            except Exception as e:
                logger.error('Error in polling loop: %s', e, exc_info=True)

            logger.debug('Sleeping for %s seconds', self.polling_interval)
            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())

    async def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def get_next_event(self) -> NewsEvent | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
