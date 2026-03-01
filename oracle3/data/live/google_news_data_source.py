"""Google News scraping data source.

Ports the Google News scraping approach from
``live-trade-bench`` (``fetchers/news_fetcher.py``) into
the oracle3 ``DataSource`` interface.  Scrapes
``google.com/search?tbm=nws`` for news cards, parses them with
BeautifulSoup, and emits ``NewsEvent`` objects through an async queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from oracle3.data.data_source import DataSource
from oracle3.events.events import Event, NewsEvent

logger = logging.getLogger(__name__)

# Default queries oriented at prediction-market-relevant news.
DEFAULT_QUERIES: list[str] = [
    'polymarket',
    'prediction market',
    'cryptocurrency regulation',
]

# Google GDPR-bypass cookies (same as live-trade-bench).
_GOOGLE_COOKIES: dict[str, str] = {
    'CONSENT': 'YES+cb.20210720-07-p0.en+FX+410',
    'SOCS': 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg',
}

_HTML_HEADERS: dict[str, str] = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;'
        'q=0.9,image/avif,image/webp,*/*;q=0.8'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}


# ------------------------------------------------------------------
# Low-level HTTP helper (mirrors live-trade-bench BaseFetcher)
# ------------------------------------------------------------------


def _is_rate_limited(resp: Any) -> bool:
    try:
        return getattr(resp, 'status_code', None) == 429
    except Exception:
        return False


@retry(
    retry=retry_if_result(_is_rate_limited),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(5),
)
def _make_request(
    url: str,
    *,
    min_delay: float = 2.0,
    max_delay: float = 6.0,
    timeout: int = 15,
) -> requests.Response:
    """GET *url* with random delay and 429-retry (tenacity)."""
    time.sleep(random.uniform(min_delay, max_delay))
    cookies = {}
    if 'google.com' in url:
        cookies.update(_GOOGLE_COOKIES)
    return requests.get(
        url,
        headers=_HTML_HEADERS,
        cookies=cookies,
        timeout=timeout,
    )


# ------------------------------------------------------------------
# HTML-parsing helpers (ported from live-trade-bench NewsFetcher)
# ------------------------------------------------------------------


def _clean_google_href(href: str) -> str:
    """Unwrap ``/url?q=<real_url>`` redirects."""
    if href.startswith('/url?'):
        qs = parse_qs(urlparse(href).query)
        if 'q' in qs and qs['q']:
            return qs['q'][0]
    return href


def _parse_relative_or_absolute(text: str, ref: datetime) -> float:
    """Parse Google date strings like ``'5 hours ago'`` or ``'Feb 5, 2026'``."""
    t = text.strip().lower()
    m = re.match(r'^\s*(\d+)\s+(second|minute|hour|day)s?\s+ago\s*$', t)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        delta = {
            'second': timedelta(seconds=num),
            'minute': timedelta(minutes=num),
            'hour': timedelta(hours=num),
            'day': timedelta(days=num),
        }[unit]
        return (ref - delta).timestamp()
    for fmt in ('%b %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(text.strip(), fmt).timestamp()
        except ValueError:
            continue
    return ref.timestamp()


def _find_snippet(card: Any, title_text: str, source_text: str, date_text: str) -> str:
    """Extract longest snippet div that is not title/source/date."""
    candidates: list[str] = []
    for div in card.find_all(['div', 'span']):
        text = div.get_text(strip=True)
        if not text or len(text) < 20:
            continue
        if text in (title_text, source_text, date_text):
            continue
        if title_text in text and len(text) < len(title_text) + 50:
            continue
        candidates.append(text)
    return max(candidates, key=len) if candidates else ''


def _scrape_google_news(  # noqa: C901
    query: str,
    *,
    max_pages: int = 1,
    min_delay: float = 2.0,
    max_delay: float = 6.0,
) -> list[dict[str, Any]]:
    """Scrape Google News search results for *query*.

    Returns a list of dicts with keys:
    ``link``, ``title``, ``snippet``, ``date`` (unix ts), ``source``.
    """
    now = datetime.now()
    start_fmt = (now - timedelta(days=7)).strftime('%m/%d/%Y')
    end_fmt = now.strftime('%m/%d/%Y')

    results: list[dict[str, Any]] = []
    for page in range(max_pages):
        encoded = quote_plus(query)
        url = (
            f'https://www.google.com/search?q={encoded}'
            f'&tbs=cdr:1,cd_min:{start_fmt},cd_max:{end_fmt}'
            f'&tbm=nws&start={page * 10}'
        )
        try:
            resp = _make_request(
                url,
                min_delay=min_delay,
                max_delay=max_delay,
                timeout=15,
            )
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception:
            logger.exception('Google News request/parse failed for %r', query)
            break

        cards = soup.select('div.SoaBEf')
        if not cards:
            break

        for el in cards:
            try:
                a = el.find('a')
                if not a or 'href' not in a.attrs:
                    continue
                link = _clean_google_href(str(a['href']))

                title_el = el.select_one('div.MBeuO')
                date_el = el.select_one('.LfVVr')
                source_el = el.select_one('.NUnG9d span')

                if not (title_el and date_el and source_el):
                    continue

                title_text = title_el.get_text(strip=True)
                source_text = source_el.get_text(strip=True)
                date_text = date_el.get_text(strip=True)
                snippet = _find_snippet(el, title_text, source_text, date_text)
                ts = _parse_relative_or_absolute(date_text, now)

                results.append(
                    {
                        'link': link,
                        'title': title_text,
                        'snippet': snippet,
                        'date': ts,
                        'source': source_text,
                    }
                )
            except Exception:
                logger.exception('Error processing Google News card')
                continue

        # Stop if Google doesn't provide a "Next" link.
        if not soup.find('a', id='pnnext'):
            break

    return results


# ------------------------------------------------------------------
# RSS-based alternative (no scraping, no rate limits)
# ------------------------------------------------------------------


def _fetch_google_news_rss(
    query: str,
    *,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Fetch Google News results via RSS feed (no rate limiting).

    Google News RSS endpoint is public and does NOT enforce the same
    rate limits as the search HTML endpoint.
    """
    encoded = quote_plus(query)
    url = f'https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en'

    try:
        feed = feedparser.parse(url)
    except Exception:
        logger.exception('Google News RSS parse failed for %r', query)
        return []

    if not hasattr(feed, 'entries') or not feed.entries:
        return []

    results: list[dict[str, Any]] = []
    for entry in feed.entries[:max_results]:
        title = entry.get('title', '')
        link = entry.get('link', '')
        # Google News RSS titles often end with " - Source Name"
        source = ''
        if ' - ' in title:
            parts = title.rsplit(' - ', 1)
            title = parts[0]
            source = parts[1] if len(parts) > 1 else ''

        snippet = entry.get('summary', '')
        # Strip HTML from summary
        if snippet and '<' in snippet:
            try:
                snippet = BeautifulSoup(snippet, 'html.parser').get_text(strip=True)
            except Exception:
                snippet = ''

        ts = datetime.now().timestamp()
        if 'published_parsed' in entry and entry.published_parsed:
            try:
                from calendar import timegm

                ts = timegm(entry.published_parsed)
            except Exception:
                pass

        results.append(
            {
                'link': link,
                'title': title,
                'snippet': snippet[:300],
                'date': ts,
                'source': source or 'Google News',
            }
        )

    return results


# ------------------------------------------------------------------
# DataSource implementation
# ------------------------------------------------------------------


class GoogleNewsDataSource(DataSource):
    """Scrape Google News and emit ``NewsEvent`` objects.

    This is the oracle3 adaptation of the ``NewsFetcher`` from
    ``live-trade-bench``.  It polls Google News search
    results on a configurable interval, deduplicates by article URL,
    persists seen article IDs to a JSONL file, and feeds events into
    the standard ``asyncio.Queue`` consumed by ``TradingEngine``.

    Args:
        queries: Search queries for Google News.
        cache_file: JSONL file for dedup persistence.
        polling_interval: Seconds between poll cycles.
        max_articles_per_poll: Cap articles enqueued per cycle.
        max_pages: Google result pages to scrape per query.
        min_delay: Minimum random delay between HTTP requests (seconds).
        max_delay: Maximum random delay between HTTP requests (seconds).
    """

    def __init__(
        self,
        queries: list[str] | None = None,
        cache_file: str = 'google_news_cache.jsonl',
        polling_interval: float = 300.0,
        max_articles_per_poll: int = 10,
        max_pages: int = 1,
        min_delay: float = 2.0,
        max_delay: float = 6.0,
    ) -> None:
        self.queries = queries or list(DEFAULT_QUERIES)
        self.cache_file = cache_file
        self.polling_interval = polling_interval
        self.max_articles_per_poll = max_articles_per_poll
        self.max_pages = max_pages
        self.min_delay = min_delay
        self.max_delay = max_delay

        self.processed_article_ids: set[str] = set()
        self.event_queue: asyncio.Queue[NewsEvent] = asyncio.Queue()
        self._poll_task: asyncio.Task[None] | None = None

        self._load_processed_articles()

    # -- persistence (same pattern as LiveRSSNewsDataSource) ----------

    def _load_processed_articles(self) -> None:
        if not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file, encoding='utf-8') as f:
                for line in f:
                    article = json.loads(line.strip())
                    if 'uuid' in article:
                        self.processed_article_ids.add(article['uuid'])
        except Exception:
            logger.exception('Error loading article cache')

    def _save_article(self, article: dict[str, Any]) -> None:
        try:
            with open(self.cache_file, 'a', encoding='utf-8') as f:
                json.dump(article, f, ensure_ascii=False)
                f.write('\n')
        except Exception:
            logger.exception('Error saving article to cache')

    # -- event construction -------------------------------------------

    @staticmethod
    def _to_news_event(item: dict[str, Any], query: str) -> NewsEvent:
        """Convert a scraped result dict to a ``NewsEvent``."""
        title = item.get('title', '')
        snippet = item.get('snippet', '')
        news_content = f'{title}: {snippet}' if snippet else title
        published_at = datetime.now(timezone.utc)
        ts = item.get('date')
        if ts is not None:
            try:
                published_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OSError, ValueError):
                pass
        link = item.get('link', '')
        uid = link or str(uuid_mod.uuid4())
        return NewsEvent(
            news=news_content,
            title=title,
            source=item.get('source', 'Google News'),
            url=link,
            published_at=published_at,
            categories=[query],
            description=snippet,
            image_url='',
            uuid=uid,
            event_id=uid,
        )

    # -- polling loop -------------------------------------------------

    async def _fetch_all_queries(self) -> list[tuple[dict[str, Any], str]]:
        """Fetch news for all configured queries; return ``(item, query)`` pairs.

        Uses Google News RSS as primary source (reliable, no rate limits).
        Falls back to HTML scraping if RSS returns no results.
        """
        results: list[tuple[dict[str, Any], str]] = []
        count = 0
        for query in self.queries:
            if count >= self.max_articles_per_poll:
                break
            try:
                # Primary: RSS feed (no rate limits)
                items = await asyncio.to_thread(
                    _fetch_google_news_rss,
                    query,
                    max_results=self.max_articles_per_poll - count,
                )

                # Fallback: HTML scraping if RSS returns nothing
                if not items:
                    logger.debug('RSS returned 0 results for %r, trying scraper', query)
                    items = await asyncio.to_thread(
                        _scrape_google_news,
                        query,
                        max_pages=self.max_pages,
                        min_delay=self.min_delay,
                        max_delay=self.max_delay,
                    )

                for item in items:
                    if count >= self.max_articles_per_poll:
                        break
                    uid = item.get('link', '')
                    if uid in self.processed_article_ids:
                        continue
                    results.append((item, query))
                    count += 1
            except Exception:
                logger.exception(
                    'Error fetching Google News for query %r',
                    query,
                )
        return results

    async def _retry_on_error(
        self,
        func: Any,
        *args: Any,
        retries: int = 3,
        delay: int = 2,
        **kwargs: Any,
    ) -> Any:
        for attempt in range(retries):
            try:
                return await func(*args, **kwargs)
            except Exception:
                logger.exception('Attempt %d failed', attempt + 1)
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise

    async def _poll_data(self) -> None:
        """Main polling loop.  Runs until cancelled."""
        while True:
            try:
                items = await self._retry_on_error(self._fetch_all_queries)
                for item, query in items:
                    try:
                        uid = item.get('link', '') or str(uuid_mod.uuid4())
                        event = self._to_news_event(item, query)
                        await self.event_queue.put(event)
                        self.processed_article_ids.add(uid)
                        self._save_article(
                            {
                                'uuid': uid,
                                'title': item.get('title', ''),
                                'snippet': item.get('snippet', ''),
                                'link': item.get('link', ''),
                                'date': item.get('date'),
                                'source': item.get('source', ''),
                                'query': query,
                            }
                        )
                    except Exception:
                        logger.exception('Error processing scraped article')
            except Exception:
                logger.exception('Error in Google News polling loop')

            await asyncio.sleep(self.polling_interval)

    # -- DataSource lifecycle -----------------------------------------

    async def start(self) -> None:  # noqa: B027
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())

    async def stop(self) -> None:  # noqa: B027
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
