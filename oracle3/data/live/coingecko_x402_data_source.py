"""CoinGecko crypto price context data source with x402 payment support."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal

import httpx

from ...events.events import Event, NewsEvent, PriceChangeEvent
from ...ticker.ticker import SolanaTicker
from ..data_source import DataSource

logger = logging.getLogger(__name__)

_FREE_API_BASE = 'https://api.coingecko.com/api/v3'
_PRO_API_BASE = 'https://pro-api.coingecko.com/api/v3'
_COINS = {
    'bitcoin': ('BTC', 'Bitcoin'),
    'ethereum': ('ETH', 'Ethereum'),
    'solana': ('SOL', 'Solana'),
}
_COIN_IDS = ','.join(_COINS.keys())

_POLL_INTERVAL = 300.0  # 5 minutes
_SIGNIFICANT_CHANGE_24H = 2.0  # percent
_PRICE_MOVE_THRESHOLD = 0.5  # percent


class CoinGeckoX402DataSource(DataSource):
    """Polls CoinGecko for BTC/ETH/SOL prices as crypto market context.

    Emits NewsEvents when 24h change is significant and PriceChangeEvents
    when price moves meaningfully since last poll.  Attempts x402 payment
    via pro API, falls back to free tier.
    """

    def __init__(
        self,
        polling_interval: float = _POLL_INTERVAL,
        significant_change_pct: float = _SIGNIFICANT_CHANGE_24H,
        price_move_pct: float = _PRICE_MOVE_THRESHOLD,
    ) -> None:
        self.polling_interval = polling_interval
        self.significant_change_pct = significant_change_pct
        self.price_move_pct = price_move_pct
        self.event_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)
        self._poll_task: asyncio.Task | None = None
        self._last_prices: dict[str, float] = {}
        self._wallet_key: str | None = os.environ.get('COINGECKO_X402_WALLET_KEY')

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

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

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        backoff = self.polling_interval
        while True:
            try:
                data = await self._fetch_prices()
                if data:
                    self._process_prices(data)
                    backoff = self.polling_interval
                else:
                    logger.warning('CoinGeckoX402 empty response')
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    'CoinGeckoX402 poll error (next in %.0fs)',
                    backoff,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1800.0)
                continue

            await asyncio.sleep(self.polling_interval)

    async def _fetch_prices(self) -> dict | None:
        params = {
            'ids': _COIN_IDS,
            'vs_currencies': 'usd',
            'include_24hr_change': 'true',
        }

        # Try pro API with x402 payment header
        if self._wallet_key:
            try:
                return await self._fetch_with_x402(params)
            except Exception:
                logger.debug('CoinGeckoX402 pro API failed, falling back to free')

        # Free API fallback
        return await self._fetch_free(params)

    async def _fetch_with_x402(self, params: dict) -> dict:
        headers = {'X-402-Payment': self._wallet_key}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f'{_PRO_API_BASE}/simple/price',
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def _fetch_free(self, params: dict) -> dict | None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f'{_FREE_API_BASE}/simple/price',
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _process_prices(self, data: dict) -> None:
        for coin_id, (symbol, name) in _COINS.items():
            coin_data = data.get(coin_id)
            if not coin_data:
                continue

            price = coin_data.get('usd', 0)
            change_24h = coin_data.get('usd_24h_change', 0)
            if not price:
                continue

            ticker = SolanaTicker(
                symbol=symbol,
                name=name,
                market_ticker=f'{symbol}_USD',
            )

            # NewsEvent for significant 24h changes
            if abs(change_24h) >= self.significant_change_pct:
                direction = 'up' if change_24h > 0 else 'down'
                news_text = (
                    f'{name} ({symbol}) {direction} {abs(change_24h):.1f}% '
                    f'in 24h — now ${price:,.2f}'
                )
                try:
                    self.event_queue.put_nowait(NewsEvent(
                        news=news_text,
                        title=f'{symbol} 24h: {direction} {abs(change_24h):.1f}%',
                        source='coingecko',
                        description=news_text,
                        ticker=ticker,
                    ))
                except asyncio.QueueFull:
                    pass

            # PriceChangeEvent when price moves since last poll
            prev = self._last_prices.get(symbol, 0)
            if prev > 0:
                pct_move = abs(price - prev) / prev * 100
                if pct_move >= self.price_move_pct:
                    try:
                        self.event_queue.put_nowait(PriceChangeEvent(
                            ticker=ticker,
                            price=Decimal(str(price)),
                            timestamp=datetime.now(),
                        ))
                    except asyncio.QueueFull:
                        pass
            else:
                # First poll — always emit initial price
                try:
                    self.event_queue.put_nowait(PriceChangeEvent(
                        ticker=ticker,
                        price=Decimal(str(price)),
                        timestamp=datetime.now(),
                    ))
                except asyncio.QueueFull:
                    pass

            self._last_prices[symbol] = price

        logger.info(
            'CoinGeckoX402 polled: %s',
            ', '.join(
                f'{sym}=${self._last_prices.get(sym, 0):,.2f}'
                for _, (sym, _) in _COINS.items()
            ),
        )
