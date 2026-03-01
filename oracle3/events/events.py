from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from oracle3.ticker.ticker import Ticker

if TYPE_CHECKING:
    pass


class Event(ABC):
    @abstractmethod
    def trigger(self) -> None:
        pass

    @abstractmethod
    def __str__(self) -> str:
        pass

    @abstractmethod
    def __repr__(self) -> str:
        pass


class OrderBookEvent(Event):
    ticker: Ticker
    price: Decimal
    size: Decimal
    size_delta: Decimal
    side: str  # 'bid' or 'ask'
    timestamp: datetime | None

    def __init__(
        self,
        ticker: Ticker,
        price: Decimal,
        size: Decimal,
        size_delta: Decimal,
        side: str = '',
        timestamp: datetime | None = None,
    ):
        self.ticker = ticker
        self.price = price
        self.size = size
        self.size_delta = size_delta
        self.side = side
        self.timestamp = timestamp

    def trigger(self) -> None:
        pass

    def __str__(self) -> str:
        return f'OrderBookEvent: ticker={self.ticker.symbol}, price={self.price}, size={self.size}, size_delta={self.size_delta}'

    def __repr__(self) -> str:
        return self.__str__()


class NewsEvent(Event):
    news: str
    title: str
    source: str
    url: str
    published_at: datetime | None
    categories: list[str]
    description: str
    image_url: str
    uuid: str
    event_id: str
    ticker: Ticker

    def __init__(
        self,
        news: str,
        title: str = '',
        source: str = '',
        url: str = '',
        published_at: datetime | None = None,
        categories: list[str] = None,
        description: str = '',
        image_url: str = '',
        uuid: str = '',
        event_id: str = '',
        ticker: Ticker = None,
    ):
        self.news = news
        self.title = title
        self.source = source
        self.url = url
        self.published_at = published_at or datetime.now()
        self.categories = categories or []
        self.description = description
        self.image_url = image_url
        self.uuid = uuid
        self.event_id = event_id
        self.ticker = ticker

    def trigger(self) -> None:
        pass

    def __str__(self) -> str:
        ticker_str = f'{self.ticker.symbol}' if self.ticker else 'None'
        content = f'{self.news[:100]}{"..." if len(self.news) > 100 else ""}'
        return f'NewsEvent: ticker={ticker_str}, title={self.title}, source={self.source}\n  Content: {content}'

    def __repr__(self) -> str:
        return self.__str__()


class PriceChangeEvent(Event):
    def __init__(
        self,
        ticker: Ticker,
        price: Decimal,
        timestamp: datetime = None,
    ):
        self.ticker = ticker
        self.price = price
        self.timestamp = timestamp or datetime.now()

    def trigger(self) -> None:
        pass

    def __str__(self) -> str:
        return f'PriceChangeEvent: ticker={self.ticker.symbol}, price={self.price}, timestamp={self.timestamp}'

    def __repr__(self) -> str:
        return self.__str__()
