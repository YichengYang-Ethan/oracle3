import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)


class BaseFetcher(ABC):
    """Async base class for HTTP fetchers with rate limiting, retries, and connection pooling."""

    def __init__(self, min_delay: float = 1.0, max_delay: float = 3.0) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._client: httpx.AsyncClient | None = None
        self.default_headers: dict[str, str] = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self.default_headers,
                follow_redirects=True,
                timeout=httpx.Timeout(15.0),
            )
        return self._client

    async def __aenter__(self) -> 'BaseFetcher':
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _rate_limit_delay(self) -> None:
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

    async def make_request(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an HTTP GET request with rate limiting and retries."""
        await self._rate_limit_delay()

        request_headers = headers if headers is not None else None

        cookies = kwargs.pop('cookies', {})
        if 'google.com' in url:
            cookies.update(
                {
                    'CONSENT': 'YES+cb.20210720-07-p0.en+FX+410',
                    'SOCS': 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg',
                }
            )

        kwargs.setdefault('timeout', 15.0)

        return await self._request_with_retry(
            url, headers=request_headers, cookies=cookies, **kwargs
        )

    async def _request_with_retry(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        max_attempts: int = 5,
        **kwargs: Any,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                response = await self.client.get(
                    url, headers=headers, cookies=cookies or {}, **kwargs
                )
                if response.status_code == 429:
                    wait = min(4 * (2**attempt), 60)
                    logger.warning(
                        'Rate limited (429) on attempt %d, waiting %.1fs',
                        attempt + 1,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                return response
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                wait = min(2 * (2**attempt), 30)
                logger.warning(
                    'Request error on attempt %d: %s, waiting %.1fs',
                    attempt + 1,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f'All {max_attempts} attempts exhausted for {url}')

    def validate_response(self, response: httpx.Response, context: str = '') -> None:
        if not response.is_success:
            raise RuntimeError(
                f'{context} failed with status {response.status_code}: {response.text[:200]}'
            )

    def safe_json_parse(self, response: httpx.Response, context: str = '') -> Any:
        try:
            return response.json()
        except Exception as e:
            raise RuntimeError(f'{context} JSON parsing failed: {e}') from e

    @staticmethod
    def clean_google_href(href: str) -> str:
        if href.startswith('/url?'):
            qs = parse_qs(urlparse(href).query)
            if 'q' in qs and qs['q']:
                return qs['q'][0]
        return href

    @abstractmethod
    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        pass
