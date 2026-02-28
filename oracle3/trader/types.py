from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from oracle3.ticker.ticker import Ticker


class TradeSide(Enum):
    BUY = 'buy'
    SELL = 'sell'


@dataclass
class Trade:
    side: TradeSide
    ticker: Ticker
    price: Decimal
    quantity: Decimal
    commission: Decimal

    def to_payload(self) -> dict[str, str | dict[str, str]]:
        return {
            'side': self.side.value,
            'ticker': _ticker_to_payload(self.ticker),
            'price': str(self.price),
            'quantity': str(self.quantity),
            'commission': str(self.commission),
        }


class OrderStatus(Enum):
    PLACED = 'placed'
    REJECTED = 'rejected'
    PARTIALLY_FILLED = 'partially_filled'
    FILLED = 'filled'
    CANCELLED = 'cancelled'
    UNKNOWN = 'unknown'


class OrderFailureReason(Enum):
    RISK_CHECK_FAILED = 'risk_check_failed'
    INVALID_ORDER = 'invalid_order'
    INSUFFICIENT_CASH = 'insufficient_cash'
    DUPLICATE_ORDER = 'duplicate_order'
    MARKET_NOT_ALLOWED = 'market_not_allowed'
    TRADING_DISABLED = 'trading_disabled'
    UNKNOWN = 'unknown'


@dataclass
class Order:
    status: OrderStatus
    side: TradeSide
    ticker: Ticker
    limit_price: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    trades: list[Trade]
    remaining: Decimal
    commission: Decimal

    def to_payload(self) -> dict[str, object]:
        return {
            'status': self.status.value,
            'side': self.side.value,
            'ticker': _ticker_to_payload(self.ticker),
            'limit_price': str(self.limit_price),
            'filled_quantity': str(self.filled_quantity),
            'average_price': str(self.average_price),
            'remaining': str(self.remaining),
            'commission': str(self.commission),
            'trades': [trade.to_payload() for trade in self.trades],
        }


@dataclass
class PlaceOrderResult:
    order: Order | None = None
    failure_reason: OrderFailureReason | None = None

    @property
    def status(self) -> str:
        if self.order is None:
            return 'failed'
        return self.order.status.value

    @property
    def accepted(self) -> bool:
        return self.order is not None and self.order.status != OrderStatus.REJECTED

    @property
    def executed(self) -> bool:
        return self.order is not None and self.order.filled_quantity > 0

    def to_payload(self) -> dict[str, object]:
        return {
            'ok': self.accepted and self.failure_reason is None,
            'accepted': self.accepted,
            'executed': self.executed,
            'status': self.status,
            'failure_reason': (
                self.failure_reason.value if self.failure_reason is not None else None
            ),
            'order': self.order.to_payload() if self.order is not None else None,
        }


def _ticker_to_payload(ticker: Ticker) -> dict[str, str]:
    payload = {
        'symbol': ticker.symbol,
        'name': getattr(ticker, 'name', '') or '',
    }
    for key in ('token_id', 'market_id', 'event_id', 'market_ticker', 'event_ticker'):
        value = getattr(ticker, key, '')
        if value:
            payload[key] = str(value)
    return payload
