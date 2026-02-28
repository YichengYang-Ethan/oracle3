from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from oracle3.events.events import OrderBookEvent, PriceChangeEvent
from oracle3.order.order_book import Level, OrderBook
from oracle3.ticker.ticker import Ticker


@dataclass(frozen=True)
class MarketDataPoint:
    """Snapshot of visible market state after a single market-data event."""

    sequence: int
    ticker: Ticker
    event_type: str
    timestamp: Any = None
    event_price: Decimal | None = None
    event_side: str = ''
    event_size: Decimal | None = None
    event_size_delta: Decimal | None = None
    best_bid: Decimal | None = None
    best_bid_size: Decimal | None = None
    best_ask: Decimal | None = None
    best_ask_size: Decimal | None = None


class MarketDataManager:
    def __init__(
        self,
        spread: Decimal = Decimal('0.01'),
        synthetic_size: Decimal = Decimal('1000'),
        max_history_per_ticker: int | None = 5000,
        max_timeline_events: int | None = 20000,
    ) -> None:
        self.order_books: dict[Ticker, OrderBook] = {}
        self.spread = spread
        self.synthetic_size = synthetic_size
        self.max_history_per_ticker = max_history_per_ticker
        self.max_timeline_events = max_timeline_events
        self._market_history: dict[Ticker, deque[MarketDataPoint]] = {}
        self._market_timeline: deque[MarketDataPoint] = deque(
            maxlen=max_timeline_events
        )
        self._next_market_sequence = 1

    def update_order_book(self, ticker: Ticker, order_book: OrderBook) -> None:
        self.order_books[ticker] = order_book

    def process_orderbook_event(self, event: OrderBookEvent) -> None:
        """Update order book from an incremental OrderBookEvent.

        Each event carries a single price level with its current size and side.
        We merge it into the existing order book, keeping levels sorted.
        """
        if event.ticker not in self.order_books:
            self.order_books[event.ticker] = OrderBook()

        ob = self.order_books[event.ticker]
        level = Level(price=event.price, size=event.size)

        if event.side == 'bid':
            self._upsert_level(ob.bids, level, descending=True)
        elif event.side == 'ask':
            self._upsert_level(ob.asks, level, descending=False)

        self._record_market_point(
            ticker=event.ticker,
            event_type='order_book',
            event_price=event.price,
            event_side=event.side,
            event_size=event.size,
            event_size_delta=event.size_delta,
        )

    @staticmethod
    def _upsert_level(levels: list[Level], new: Level, descending: bool) -> None:
        """Insert or update a price level in a sorted list."""
        for i, existing in enumerate(levels):
            if existing.price == new.price:
                if new.size <= 0:
                    levels.pop(i)
                else:
                    levels[i] = new
                return
        if new.size > 0:
            levels.append(new)
            levels.sort(key=lambda lv: lv.price, reverse=descending)

    def process_price_change_event(self, event: PriceChangeEvent) -> None:
        """Update order book based on probability change event"""
        if event.ticker not in self.order_books:
            self.order_books[event.ticker] = OrderBook()

        half_spread = self.spread / Decimal('2')
        bid_price = max(Decimal('0'), event.price - half_spread)
        ask_price = min(Decimal('1'), event.price + half_spread)

        size = self.synthetic_size

        bids = [Level(price=bid_price, size=size)] if bid_price > Decimal('0') else []
        asks = [Level(price=ask_price, size=size)] if ask_price < Decimal('1') else []

        order_book = self.order_books[event.ticker]
        order_book.update(asks=asks, bids=bids)

        # Bootstrap No-side order book on first encounter so PaperTrader
        # can fill No orders before the first No PriceChangeEvent arrives.
        # Subsequent updates come from the No events emitted by the data source.
        no_ticker = getattr(event.ticker, 'get_no_ticker', lambda: None)()
        if no_ticker is not None and no_ticker not in self.order_books:
            self.order_books[no_ticker] = OrderBook()

            no_price = Decimal('1') - event.price
            no_bid = max(Decimal('0'), no_price - half_spread)
            no_ask = min(Decimal('1'), no_price + half_spread)

            no_bids = [Level(price=no_bid, size=size)] if no_bid > Decimal('0') else []
            no_asks = [Level(price=no_ask, size=size)] if no_ask < Decimal('1') else []

            no_ob = self.order_books[no_ticker]
            no_ob.update(asks=no_asks, bids=no_bids)

        self._record_market_point(
            ticker=event.ticker,
            event_type='price_change',
            timestamp=event.timestamp,
            event_price=event.price,
        )

    def remove_ticker(self, ticker: Ticker) -> None:
        """Remove all order book data for a ticker."""
        self.order_books.pop(ticker, None)

    def prune_stale_tickers(self, active_symbols: set[str]) -> int:
        """Remove order books for tickers whose symbol is not in *active_symbols*.

        Returns the number of tickers removed.
        """
        stale = [t for t in self.order_books if t.symbol not in active_symbols]
        for t in stale:
            del self.order_books[t]
        return len(stale)

    def get_bids(self, ticker: Ticker, depth: int | None = None) -> list[Level]:
        ob = self.order_books.get(ticker)
        return ob.get_bids(depth) if ob is not None else []

    def get_asks(self, ticker: Ticker, depth: int | None = None) -> list[Level]:
        ob = self.order_books.get(ticker)
        return ob.get_asks(depth) if ob is not None else []

    def get_best_bid(self, ticker: Ticker) -> Level | None:
        ob = self.order_books.get(ticker)
        return ob.best_bid if ob is not None else None

    def get_best_ask(self, ticker: Ticker) -> Level | None:
        ob = self.order_books.get(ticker)
        return ob.best_ask if ob is not None else None

    def get_market_history(
        self, ticker: Ticker | None = None, limit: int | None = None
    ) -> list[MarketDataPoint]:
        """Return visible market history up to the current timestep.

        When *ticker* is omitted, this returns the global cross-market timeline.
        """
        if ticker is None:
            history: list[MarketDataPoint] = list(self._market_timeline)
        else:
            buf = self._market_history.get(ticker)
            history = list(buf) if buf is not None else []
        if limit is not None:
            if limit <= 0:
                return []
            return history[-limit:]
        return history

    def get_price_history(
        self, ticker: Ticker, limit: int | None = None
    ) -> list[Decimal]:
        """Return a numeric price series strategies can use for indicators."""
        prices: list[Decimal] = []
        for point in self.get_market_history(ticker=ticker, limit=limit):
            if point.best_bid is not None and point.best_ask is not None:
                prices.append((point.best_bid + point.best_ask) / Decimal('2'))
                continue
            if point.event_price is not None:
                prices.append(point.event_price)
        return prices

    def _record_market_point(
        self,
        *,
        ticker: Ticker,
        event_type: str,
        timestamp: Any = None,
        event_price: Decimal | None = None,
        event_side: str = '',
        event_size: Decimal | None = None,
        event_size_delta: Decimal | None = None,
    ) -> None:
        ob = self.order_books.get(ticker)
        best_bid = ob.best_bid if ob is not None else None
        best_ask = ob.best_ask if ob is not None else None
        point = MarketDataPoint(
            sequence=self._next_market_sequence,
            ticker=ticker,
            event_type=event_type,
            timestamp=timestamp,
            event_price=event_price,
            event_side=event_side,
            event_size=event_size,
            event_size_delta=event_size_delta,
            best_bid=best_bid.price if best_bid is not None else None,
            best_bid_size=best_bid.size if best_bid is not None else None,
            best_ask=best_ask.price if best_ask is not None else None,
            best_ask_size=best_ask.size if best_ask is not None else None,
        )
        self._next_market_sequence += 1
        self._market_timeline.append(point)
        self._history_buffer(ticker).append(point)

    def _history_buffer(self, ticker: Ticker) -> deque[MarketDataPoint]:
        buf = self._market_history.get(ticker)
        if buf is not None:
            return buf
        fresh: deque[MarketDataPoint] = deque(maxlen=self.max_history_per_ticker)
        self._market_history[ticker] = fresh
        return fresh
