from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass(eq=True, frozen=True)
class Ticker(ABC):
    # The symbol of the ticker, must be unique across all markets
    symbol: str = field(metadata={'abstract': True})
    # The name of the ticker
    name: str = field(metadata={'abstract': True})

    @property
    @abstractmethod
    def collateral(self) -> Ticker:
        """The ticker of the collateral currency"""
        pass


@dataclass(eq=True, frozen=True)
class PolyMarketTicker(Ticker):
    symbol: str
    name: str = ''
    token_id: str = ''
    market_id: str = ''
    event_id: str = ''
    no_token_id: str = ''  # Complement token (NO side)

    @property
    def collateral(self) -> Ticker:
        """The ticker of the collateral currency"""
        return CashTicker.POLYMARKET_USDC

    @classmethod
    def from_token_id(cls, token_id: str, name: str = '') -> PolyMarketTicker:
        """Create a ticker from a token ID, using the token ID as the symbol"""
        return cls(symbol=token_id, name=name, token_id=token_id)

    def get_no_ticker(self) -> PolyMarketTicker | None:
        """Return a ticker for the NO side of this market, if available.

        The returned ticker must match the one stored in MarketDataManager
        by the data source, so we use the same ``name`` (not appending
        " (NO)") — ``PolyMarketTicker`` equality compares all fields.
        """
        if not self.no_token_id:
            return None
        return PolyMarketTicker(
            symbol=self.no_token_id,
            name=self.name,
            token_id=self.no_token_id,
            market_id=self.market_id,
            event_id=self.event_id,
            no_token_id=self.token_id,  # reverse: NO's complement is YES
        )


@dataclass(eq=True, frozen=True)
class SolanaTicker(Ticker):
    symbol: str
    name: str = ''
    yes_mint: str = ''
    no_mint: str = ''
    market_ticker: str = ''
    event_ticker: str = ''
    series_ticker: str = ''
    is_no_side: bool = False

    @property
    def collateral(self) -> Ticker:
        return CashTicker.DFLOW_USDC

    def get_no_ticker(self) -> SolanaTicker | None:
        if self.is_no_side:
            return None
        return SolanaTicker(
            symbol=f'{self.symbol}_NO',
            name=self.name,
            yes_mint=self.no_mint,
            no_mint=self.yes_mint,
            market_ticker=self.market_ticker,
            event_ticker=self.event_ticker,
            series_ticker=self.series_ticker,
            is_no_side=True,
        )


@dataclass(eq=True, frozen=True)
class CashTicker(Ticker):
    symbol: str
    name: str

    POLYMARKET_USDC: ClassVar[CashTicker]
    KALSHI_USD: ClassVar[CashTicker]
    DFLOW_USDC: ClassVar[CashTicker]

    @property
    def collateral(self) -> Ticker:
        """The ticker of the collateral currency"""
        raise NotImplementedError('Cash tickers do not have a collateral ticker')


CashTicker.POLYMARKET_USDC = CashTicker(
    symbol='PolyMarket_USDC', name='PolyMarket USDC'
)


@dataclass(eq=True, frozen=True)
class KalshiTicker(Ticker):
    symbol: str
    name: str = ''
    market_ticker: str = ''
    event_ticker: str = ''
    series_ticker: str = ''
    is_no_side: bool = False

    @property
    def collateral(self) -> Ticker:
        return CashTicker.KALSHI_USD

    def get_no_ticker(self) -> KalshiTicker | None:
        """Return a ticker for the NO side of this market."""
        if self.is_no_side:
            return None
        return KalshiTicker(
            symbol=f'{self.symbol}_NO',
            name=self.name,
            market_ticker=self.market_ticker,
            event_ticker=self.event_ticker,
            series_ticker=self.series_ticker,
            is_no_side=True,
        )


CashTicker.KALSHI_USD = CashTicker(symbol='Kalshi_USD', name='Kalshi USD')

CashTicker.DFLOW_USDC = CashTicker(symbol='DFlow_USDC', name='DFlow USDC')
