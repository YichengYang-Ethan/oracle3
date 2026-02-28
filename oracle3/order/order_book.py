from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Level:
    price: Decimal
    size: Decimal

    def __str__(self) -> str:
        return f'{self.price}@{self.size}'

    def __repr__(self) -> str:
        return f"Level(price=Decimal('{self.price}'), size=Decimal('{self.size}'))"


class OrderBook:
    def __init__(self) -> None:
        self.asks: list[Level] = []
        self.bids: list[Level] = []

    def update(self, asks: list[Level], bids: list[Level]) -> None:
        self.asks = asks
        self.bids = bids

    def __str__(self) -> str:
        best_bid = self.best_bid
        best_ask = self.best_ask
        return f'OrderBook(best_bid={best_bid}, best_ask={best_ask})'

    def __repr__(self) -> str:
        return f'OrderBook(asks={self.asks}, bids={self.bids})'

    @property
    def best_ask(self) -> Level | None:
        """Get the best ask"""
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> Level | None:
        """Get the best bid"""
        return self.bids[0] if self.bids else None

    def get_asks(self, depth: int | None = None) -> list[Level]:
        """Get asks"""
        return self.asks[:depth] if depth else self.asks

    def get_bids(self, depth: int | None = None) -> list[Level]:
        """Get bids"""
        return self.bids[:depth] if depth else self.bids
