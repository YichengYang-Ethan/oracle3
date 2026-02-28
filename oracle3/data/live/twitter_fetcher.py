from __future__ import annotations

import logging
import os
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar

from ..base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)


class TwitterFetchBackend(Enum):
    OFFICIAL_API = 'official_api'  # X API v2 (needs BEARER_TOKEN)
    NITTER = 'nitter'  # Nitter instances (free, no auth)
    SOCIALDATA = 'socialdata'  # SocialData.tools API (cheaper alternative)


class TwitterFetcher(BaseFetcher):
    """Multi-backend Twitter/X fetcher for prediction market intelligence."""

    # Key accounts for Polymarket-relevant events (political + crypto + macro)
    DEFAULT_ACCOUNTS: ClassVar[dict[str, list[str]]] = {
        'politics': [
            'POTUS',
            'WhiteHouse',
            'SpeakerJohnson',
            'LeaderMcConnell',
            'SenSchumer',
            'VP',
            'SecYellen',
        ],
        'crypto': [
            'elonmusk',
            'VitalikButerin',
            'saborin_bitcoin',
            'GaryGensler',
            'SECGov',
        ],
        'macro': [
            'federalreserve',
            'WSJ',
            'business',
            'ReutersBiz',
            'AP',
            'BBCBreaking',
        ],
        'geopolitics': [
            'KremlinRussia_E',
            'ZelenskyyUa',
            'IsraeliPM',
            'ChinaDaily',
        ],
    }

    # Nitter instances (public, rotating)
    NITTER_INSTANCES: ClassVar[list[str]] = [
        'https://nitter.privacydev.net',
        'https://nitter.poast.org',
        'https://nitter.woodland.cafe',
    ]

    def __init__(
        self,
        backend: TwitterFetchBackend = TwitterFetchBackend.NITTER,
        bearer_token: str | None = None,
        socialdata_api_key: str | None = None,
        accounts: dict[str, list[str]] | None = None,
        max_tweets_per_account: int = 10,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
    ) -> None:
        super().__init__(min_delay=min_delay, max_delay=max_delay)

        self.bearer_token = bearer_token or os.environ.get('X_BEARER_TOKEN')
        self.socialdata_api_key = socialdata_api_key or os.environ.get(
            'SOCIALDATA_API_KEY'
        )
        self.accounts = accounts or self.DEFAULT_ACCOUNTS
        self.max_tweets_per_account = max_tweets_per_account

        # Auto-detect best available backend
        if self.bearer_token:
            self.backend = TwitterFetchBackend.OFFICIAL_API
        elif self.socialdata_api_key:
            self.backend = TwitterFetchBackend.SOCIALDATA
        else:
            self.backend = backend  # Falls back to caller's choice (default NITTER)

        # Cache for official API user-id lookups: username -> user_id
        self._user_id_cache: dict[str, str] = {}

        logger.info('TwitterFetcher initialized with backend=%s', self.backend.value)

    # ------------------------------------------------------------------
    # Backend implementations
    # ------------------------------------------------------------------

    async def _fetch_official(self, username: str, max_results: int = 10) -> list[dict]:
        """Fetch tweets via X API v2 (requires bearer token)."""
        if not self.bearer_token:
            raise RuntimeError('Official API backend requires a bearer token')

        auth_headers = {'Authorization': f'Bearer {self.bearer_token}'}

        # Resolve username -> user ID (cached)
        user_id = self._user_id_cache.get(username)
        if not user_id:
            lookup_url = f'https://api.twitter.com/2/users/by/username/{username}'
            resp = await self.make_request(lookup_url, headers=auth_headers)
            self.validate_response(resp, context=f'User lookup @{username}')
            data = self.safe_json_parse(resp, context=f'User lookup @{username}')
            user_id = data.get('data', {}).get('id')
            if not user_id:
                logger.warning('Could not resolve user ID for @%s', username)
                return []
            self._user_id_cache[username] = user_id

        # Fetch recent tweets
        tweets_url = (
            f'https://api.twitter.com/2/users/{user_id}/tweets'
            f'?max_results={min(max_results, 100)}'
            f'&tweet.fields=created_at,public_metrics,referenced_tweets'
        )
        resp = await self.make_request(tweets_url, headers=auth_headers)
        self.validate_response(resp, context=f'Tweets @{username}')
        payload = self.safe_json_parse(resp, context=f'Tweets @{username}')

        tweets: list[dict] = []
        for tw in payload.get('data', []):
            is_retweet = False
            for ref in tw.get('referenced_tweets', []):
                if ref.get('type') == 'retweeted':
                    is_retweet = True
                    break

            created_at = tw.get('created_at', '')
            try:
                ts = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                ts = datetime.now(tz=timezone.utc)

            metrics = tw.get('public_metrics', {})
            tweets.append(
                self._normalize_tweet(
                    text=tw.get('text', ''),
                    username=username,
                    timestamp=ts,
                    url=f'https://x.com/{username}/status/{tw["id"]}',
                    metrics={
                        'likes': metrics.get('like_count', 0),
                        'retweets': metrics.get('retweet_count', 0),
                        'replies': metrics.get('reply_count', 0),
                        'quotes': metrics.get('quote_count', 0),
                    },
                    is_retweet=is_retweet,
                )
            )
        return tweets

    async def _fetch_nitter(self, username: str, max_results: int = 10) -> list[dict]:
        """Fetch tweets via Nitter RSS (free, no auth)."""
        instances = list(self.NITTER_INSTANCES)
        random.shuffle(instances)

        last_exc: Exception | None = None
        for instance_url in instances:
            rss_url = f'{instance_url}/{username}/rss'
            try:
                resp = await self.make_request(
                    rss_url,
                    headers={
                        'Accept': 'application/rss+xml, application/xml, text/xml',
                    },
                )
                if not resp.is_success:
                    logger.debug(
                        'Nitter instance %s returned %d for @%s',
                        instance_url,
                        resp.status_code,
                        username,
                    )
                    continue

                return self._parse_nitter_rss(resp.text, username, max_results)

            except Exception as exc:
                last_exc = exc
                logger.debug(
                    'Nitter instance %s failed for @%s: %s',
                    instance_url,
                    username,
                    exc,
                )
                continue

        if last_exc:
            logger.warning(
                'All Nitter instances failed for @%s: %s', username, last_exc
            )
        return []

    def _parse_nitter_rss(
        self, xml_text: str, username: str, max_results: int
    ) -> list[dict]:
        """Parse Nitter RSS XML into normalized tweet dicts."""
        tweets: list[dict] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning('Failed to parse Nitter RSS XML for @%s: %s', username, exc)
            return []

        # RSS items live under <channel><item>
        channel = root.find('channel')
        if channel is None:
            return []

        for item in channel.findall('item')[:max_results]:
            title = item.findtext('title', '')
            description = item.findtext('description', '')
            link = item.findtext('link', '')
            pub_date = item.findtext('pubDate', '')

            # Use description (full text) if available, else title
            text = description or title
            # Strip HTML tags from Nitter descriptions
            text = self._strip_html(text)

            # Detect retweets
            is_retweet = text.startswith('RT @') or title.startswith('RT @')

            # Parse pubDate (RFC 2822 format)
            ts = self._parse_rfc2822(pub_date)

            tweets.append(
                self._normalize_tweet(
                    text=text,
                    username=username,
                    timestamp=ts,
                    url=link or f'https://x.com/{username}',
                    metrics={},
                    is_retweet=is_retweet,
                )
            )
        return tweets

    async def _fetch_socialdata(
        self, username: str, max_results: int = 10
    ) -> list[dict]:
        """Fetch tweets via SocialData.tools API."""
        if not self.socialdata_api_key:
            raise RuntimeError('SocialData backend requires an API key')

        url = (
            f'https://api.socialdata.tools/twitter/user/{username}/tweets'
            f'?limit={max_results}'
        )
        headers = {
            'Authorization': f'Bearer {self.socialdata_api_key}',
            'Accept': 'application/json',
        }
        resp = await self.make_request(url, headers=headers)
        self.validate_response(resp, context=f'SocialData @{username}')
        payload = self.safe_json_parse(resp, context=f'SocialData @{username}')

        tweets: list[dict] = []
        for tw in payload.get('tweets', []):
            created_at = tw.get('created_at', '')
            try:
                ts = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                ts = datetime.now(tz=timezone.utc)

            is_retweet = tw.get('text', '').startswith('RT @')

            tweets.append(
                self._normalize_tweet(
                    text=tw.get('text', ''),
                    username=username,
                    timestamp=ts,
                    url=tw.get('url', f'https://x.com/{username}'),
                    metrics={
                        'likes': tw.get('favorite_count', 0),
                        'retweets': tw.get('retweet_count', 0),
                        'replies': tw.get('reply_count', 0),
                    },
                    is_retweet=is_retweet,
                )
            )
        return tweets

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    async def fetch_account_tweets(self, username: str) -> list[dict]:
        """Fetch tweets for a single account, routing to the active backend.

        Falls back to the next backend on failure.
        """
        backend_order = self._backend_fallback_order()

        for backend in backend_order:
            try:
                fetcher = self._backend_dispatch(backend)
                tweets = await fetcher(username, self.max_tweets_per_account)
                if tweets:
                    logger.debug(
                        'Fetched %d tweets for @%s via %s',
                        len(tweets),
                        username,
                        backend.value,
                    )
                    return tweets
            except Exception as exc:
                logger.warning(
                    'Backend %s failed for @%s: %s', backend.value, username, exc
                )
                continue

        logger.warning('All backends failed for @%s', username)
        return []

    async def fetch_all_accounts(
        self, categories: list[str] | None = None
    ) -> dict[str, list[dict]]:
        """Fetch tweets from all accounts in the specified categories.

        Args:
            categories: List of category names to fetch. If None, fetch all.

        Returns:
            Mapping of username -> list of normalized tweet dicts.
        """
        target_categories = categories or list(self.accounts.keys())
        usernames: list[str] = []
        for cat in target_categories:
            if cat in self.accounts:
                usernames.extend(self.accounts[cat])
            else:
                logger.warning('Unknown category: %s', cat)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_usernames: list[str] = []
        for u in usernames:
            if u not in seen:
                seen.add(u)
                unique_usernames.append(u)

        results: dict[str, list[dict]] = {}
        for username in unique_usernames:
            try:
                tweets = await self.fetch_account_tweets(username)
                if tweets:
                    results[username] = tweets
            except Exception as exc:
                logger.error('Failed to fetch tweets for @%s: %s', username, exc)

            # Rate-limit delay between accounts
            await self._rate_limit_delay()

        logger.info(
            'Fetched tweets from %d/%d accounts',
            len(results),
            len(unique_usernames),
        )
        return results

    async def fetch(
        self, categories: list[str] | None = None, **kwargs: Any
    ) -> dict[str, list[dict]]:
        """BaseFetcher.fetch implementation -- fetches tweets from all monitored accounts."""
        return await self.fetch_all_accounts(categories=categories)

    def tweets_to_news_events(
        self,
        tweets: dict[str, list[dict]],
        ticker: Any | None = None,
    ) -> list[Any]:
        """Convert tweet dicts to NewsEvent objects.

        Args:
            tweets: Mapping of username -> list of normalized tweet dicts.
            ticker: Optional PolyMarketTicker to attach to each event.

        Returns:
            List of NewsEvent instances.
        """
        from oracle3.events.events import NewsEvent

        events: list[NewsEvent] = []
        for username, tweet_list in tweets.items():
            for tw in tweet_list:
                text = tw.get('text', '')
                title = text[:100] if len(text) > 100 else text
                events.append(
                    NewsEvent(
                        news=text,
                        title=title,
                        source=f'Twitter/@{username}',
                        url=tw.get('url', ''),
                        published_at=tw.get('timestamp'),
                        ticker=ticker,
                    )
                )
        return events

    def filter_relevant_tweets(
        self,
        tweets: list[dict],
        keywords: list[str],
        min_relevance: float = 0.3,
    ) -> list[dict]:
        """Score and filter tweets by keyword relevance.

        Uses simple keyword-overlap scoring: count of matching keywords divided
        by total keywords. Returns tweets above min_relevance, sorted by
        relevance descending.
        """
        if not keywords:
            return tweets

        # Normalize keywords to lowercase
        kw_lower = [kw.lower() for kw in keywords]
        total_keywords = len(kw_lower)

        scored: list[tuple[float, dict]] = []
        for tw in tweets:
            text_lower = tw.get('text', '').lower()
            matches = sum(1 for kw in kw_lower if kw in text_lower)
            relevance = matches / total_keywords
            if relevance >= min_relevance:
                tw_copy = dict(tw)
                tw_copy['relevance_score'] = relevance
                scored.append((relevance, tw_copy))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [tw for _, tw in scored]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tweet(
        text: str,
        username: str,
        timestamp: datetime,
        url: str,
        metrics: dict,
        is_retweet: bool,
    ) -> dict:
        """Return a normalized tweet dict."""
        return {
            'text': text,
            'username': username,
            'timestamp': timestamp,
            'url': url,
            'metrics': metrics,
            'is_retweet': is_retweet,
        }

    def _backend_fallback_order(self) -> list[TwitterFetchBackend]:
        """Return backends in priority order, starting with the configured one."""
        all_backends = [
            TwitterFetchBackend.OFFICIAL_API,
            TwitterFetchBackend.SOCIALDATA,
            TwitterFetchBackend.NITTER,
        ]
        order = [self.backend]
        for b in all_backends:
            if b != self.backend:
                # Only include backends that have credentials (or are NITTER)
                if b == TwitterFetchBackend.OFFICIAL_API and not self.bearer_token:
                    continue
                if b == TwitterFetchBackend.SOCIALDATA and not self.socialdata_api_key:
                    continue
                order.append(b)
        return order

    def _backend_dispatch(self, backend: TwitterFetchBackend) -> Any:
        """Return the fetch coroutine for a given backend."""
        dispatch = {
            TwitterFetchBackend.OFFICIAL_API: self._fetch_official,
            TwitterFetchBackend.NITTER: self._fetch_nitter,
            TwitterFetchBackend.SOCIALDATA: self._fetch_socialdata,
        }
        return dispatch[backend]

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from a string."""
        import re

        clean = re.sub(r'<[^>]+>', '', text)
        # Collapse whitespace
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean

    @staticmethod
    def _parse_rfc2822(date_str: str) -> datetime:
        """Parse an RFC 2822 date string into a datetime."""
        from email.utils import parsedate_to_datetime

        if not date_str:
            return datetime.now(tz=timezone.utc)
        try:
            return parsedate_to_datetime(date_str)
        except (ValueError, TypeError):
            return datetime.now(tz=timezone.utc)
