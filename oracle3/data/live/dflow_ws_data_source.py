"""DFlow WebSocket streaming data source for real-time Solana market data."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal

import websockets

from ...events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from ...ticker.ticker import SolanaTicker
from ..data_source import DataSource

logger = logging.getLogger(__name__)

_WS_URL = 'wss://dev-prediction-markets-api.dflow.net/api/v1/ws'
_CHANNELS = ['prices', 'orderbook', 'trades']
_MAX_BACKOFF = 300.0
_INITIAL_BACKOFF = 1.0
_SIGNIFICANT_TRADE_USDC = 500.0


def _safe_float(v) -> float:
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalize_price(p: float) -> Decimal:
    if p > 1:
        return Decimal(str(p)) / Decimal('100')
    return Decimal(str(p))


class DFlowWebSocketDataSource(DataSource):
    """Streams real-time market data from DFlow via WebSocket.

    Subscribes to prices, orderbook, and trades channels.  Significant
    trades (>$500 USDC) are emitted as NewsEvents to give the LLM
    market-flow context.
    """

    def __init__(
        self,
        ws_url: str = _WS_URL,
        significant_trade_threshold: float = _SIGNIFICANT_TRADE_USDC,
    ) -> None:
        self.ws_url = ws_url
        self.significant_trade_threshold = significant_trade_threshold
        self.event_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=2000)
        self._ws_task: asyncio.Task | None = None
        self._last_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------------
    # WebSocket loop with auto-reconnect
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        backoff = _INITIAL_BACKOFF
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    logger.info('DFlowWS connected to %s', self.ws_url)
                    backoff = _INITIAL_BACKOFF

                    # Subscribe to channels
                    for channel in _CHANNELS:
                        subscribe_msg = json.dumps({
                            'type': 'subscribe',
                            'channel': channel,
                        })
                        await ws.send(subscribe_msg)
                        logger.debug('DFlowWS subscribed to %s', channel)

                    # Read messages
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.debug('DFlowWS non-JSON message: %s', raw[:200])
                            continue
                        self._handle_message(msg)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    'DFlowWS connection lost (reconnect in %.0fs)',
                    backoff,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
        channel = msg.get('channel', msg.get('type', ''))
        if channel == 'prices':
            self._handle_price(msg)
        elif channel == 'orderbook':
            self._handle_orderbook(msg)
        elif channel == 'trades':
            self._handle_trade(msg)
        else:
            logger.debug('DFlowWS unknown message type: %s', channel)

    def _make_ticker(self, data: dict) -> SolanaTicker:
        market_ticker = data.get('marketTicker', data.get('market_ticker', ''))
        event_ticker = data.get('eventTicker', data.get('event_ticker', ''))
        series_ticker = data.get('seriesTicker', data.get('series_ticker', ''))
        name = data.get('title', data.get('name', market_ticker))
        return SolanaTicker(
            symbol=market_ticker,
            name=name,
            market_ticker=market_ticker,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
        )

    def _enqueue(self, event: Event) -> None:
        try:
            self.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug('DFlowWS queue full, dropping event: %s', type(event).__name__)

    # ------------------------------------------------------------------
    # Channel handlers
    # ------------------------------------------------------------------

    def _handle_price(self, msg: dict) -> None:
        data = msg.get('data', msg)
        market_ticker = data.get('marketTicker', data.get('market_ticker', ''))
        if not market_ticker:
            return

        price = _safe_float(data.get('price', data.get('lastPrice', 0)))
        if price <= 0:
            return

        ticker = self._make_ticker(data)
        normalized = _normalize_price(price)
        if Decimal('0') < normalized < Decimal('1'):
            self._enqueue(PriceChangeEvent(
                ticker=ticker,
                price=normalized,
                timestamp=datetime.now(),
            ))
        self._last_prices[market_ticker] = price

    def _handle_orderbook(self, msg: dict) -> None:
        data = msg.get('data', msg)
        market_ticker = data.get('marketTicker', data.get('market_ticker', ''))
        if not market_ticker:
            return

        ticker = self._make_ticker(data)

        # Nested bids/asks arrays (top-of-book only)
        for side_key, side_label in [('bids', 'bid'), ('asks', 'ask')]:
            levels = data.get(side_key, [])
            if isinstance(levels, list):
                for level in levels[:1]:
                    self._emit_ob_level(ticker, level, side_label)

        # Flat bid/ask fields (alternative format)
        self._emit_flat_ob(ticker, data)

    def _emit_ob_level(self, ticker: SolanaTicker, level, side: str) -> None:
        if isinstance(level, dict):
            p = _safe_float(level.get('price', 0))
            s = _safe_float(level.get('size', level.get('quantity', 100)))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            p, s = _safe_float(level[0]), _safe_float(level[1])
        else:
            return
        if p <= 0:
            return
        norm_p = _normalize_price(p)
        if Decimal('0') < norm_p < Decimal('1'):
            self._enqueue(OrderBookEvent(
                ticker=ticker,
                price=norm_p,
                size=Decimal(str(s)),
                size_delta=Decimal(str(s)),
                side=side,
            ))

    def _emit_flat_ob(self, ticker: SolanaTicker, data: dict) -> None:
        for field, side_label in [
            ('yesBid', 'bid'), ('yes_bid', 'bid'),
            ('yesAsk', 'ask'), ('yes_ask', 'ask'),
        ]:
            val = _safe_float(data.get(field, 0))
            if val > 0:
                norm_p = _normalize_price(val)
                if Decimal('0') < norm_p < Decimal('1'):
                    self._enqueue(OrderBookEvent(
                        ticker=ticker,
                        price=norm_p,
                        size=Decimal('100'),
                        size_delta=Decimal('100'),
                        side=side_label,
                    ))

    def _handle_trade(self, msg: dict) -> None:
        data = msg.get('data', msg)
        market_ticker = data.get('marketTicker', data.get('market_ticker', ''))
        if not market_ticker:
            return

        price = _safe_float(data.get('price', 0))
        size = _safe_float(data.get('size', data.get('quantity', 0)))
        notional = price * size

        if price > 0:
            ticker = self._make_ticker(data)
            normalized = _normalize_price(price)
            if Decimal('0') < normalized < Decimal('1'):
                self._enqueue(PriceChangeEvent(
                    ticker=ticker,
                    price=normalized,
                    timestamp=datetime.now(),
                ))

        # Emit significant trades as NewsEvents for LLM context
        if notional >= self.significant_trade_threshold:
            ticker = self._make_ticker(data)
            side = data.get('side', 'unknown')
            title = data.get('title', data.get('name', market_ticker))
            news_text = (
                f'Large trade on {title}: {side} ${notional:,.0f} USDC '
                f'at {price:.4f}'
            )
            self._enqueue(NewsEvent(
                news=news_text,
                title=f'DFlow trade: {title}',
                source='dflow_ws',
                description=news_text,
                ticker=ticker,
            ))
