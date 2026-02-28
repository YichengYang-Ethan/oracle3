import random
from datetime import datetime
from decimal import Decimal
from enum import Enum

from oracle3.data.data_source import DataSource
from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.strategy import Strategy
from oracle3.ticker.ticker import PolyMarketTicker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class MockMode(Enum):
    NONE = 'none'
    MOCK_STRATEGY = 'mock_strategy'
    MOCK_DATA = 'mock_data'
    MOCK_ALL = 'mock_all'


class MockStrategy(Strategy):
    """Strategy that generates random allocations without LLM calls."""

    def __init__(self, buy_probability: float = 0.3) -> None:
        self.buy_probability = buy_probability

    async def process_event(self, event: Event, trader: Trader) -> None:
        if not isinstance(event, PriceChangeEvent):
            return

        if random.random() < self.buy_probability:
            ask = trader.market_data.get_best_ask(event.ticker)
            if ask is None:
                return
            quantity = Decimal(str(random.randint(1, 10)))
            await trader.place_order(
                side=TradeSide.BUY,
                ticker=event.ticker,
                limit_price=ask.price,
                quantity=quantity,
            )


class MockDataSource(DataSource):
    """Data source that generates synthetic price and orderbook events."""

    def __init__(
        self,
        tickers: list[PolyMarketTicker],
        num_events: int = 100,
        seed: int | None = None,
    ) -> None:
        self.tickers = tickers
        self.num_events = num_events
        self._index = 0
        self._rng = random.Random(seed)
        self._prices: dict[str, Decimal] = {
            t.symbol: Decimal(str(round(self._rng.uniform(0.1, 0.9), 2)))
            for t in tickers
        }

    async def get_next_event(self) -> Event | None:
        if self._index >= self.num_events:
            return None

        self._index += 1
        ticker = self._rng.choice(self.tickers)
        current = self._prices[ticker.symbol]

        if self._rng.random() < 0.5:
            # Price change event
            delta = Decimal(str(round(self._rng.uniform(-0.05, 0.05), 3)))
            new_price = max(Decimal('0.01'), min(Decimal('0.99'), current + delta))
            self._prices[ticker.symbol] = new_price
            return PriceChangeEvent(
                ticker=ticker,
                price=new_price,
                timestamp=datetime.now(),
            )
        else:
            # Orderbook event — emit both a bid slightly below and ask slightly above
            # current price so the paper trader can find liquidity to fill orders.
            side = self._rng.choice(['bid', 'ask'])
            offset = Decimal(str(round(self._rng.uniform(0.01, 0.03), 3)))
            if side == 'ask':
                price = min(Decimal('0.99'), current + offset)
            else:
                price = max(Decimal('0.01'), current - offset)
            size = Decimal(str(self._rng.randint(100, 5000)))
            return OrderBookEvent(
                ticker=ticker,
                price=price,
                size=size,
                size_delta=size,
                side=side,
            )
