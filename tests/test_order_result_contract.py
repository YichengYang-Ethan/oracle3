from __future__ import annotations

from decimal import Decimal

from oracle3.ticker.ticker import PolyMarketTicker
from oracle3.trader.types import (
    Order,
    OrderFailureReason,
    OrderStatus,
    PlaceOrderResult,
    Trade,
    TradeSide,
)


def _ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='T1',
        name='Test Market',
        token_id='tok_yes',
        market_id='m1',
        event_id='e1',
    )


def test_place_order_result_payload_for_failure() -> None:
    result = PlaceOrderResult(
        order=None,
        failure_reason=OrderFailureReason.INVALID_ORDER,
    )
    payload = result.to_payload()
    assert payload['ok'] is False
    assert payload['accepted'] is False
    assert payload['executed'] is False
    assert payload['status'] == 'failed'
    assert payload['failure_reason'] == 'invalid_order'
    assert payload['order'] is None


def test_place_order_result_payload_for_filled_order() -> None:
    ticker = _ticker()
    trade = Trade(
        side=TradeSide.BUY,
        ticker=ticker,
        price=Decimal('0.55'),
        quantity=Decimal('10'),
        commission=Decimal('0.01'),
    )
    order = Order(
        status=OrderStatus.FILLED,
        side=TradeSide.BUY,
        ticker=ticker,
        limit_price=Decimal('0.55'),
        filled_quantity=Decimal('10'),
        average_price=Decimal('0.55'),
        trades=[trade],
        remaining=Decimal('0'),
        commission=Decimal('0.01'),
    )
    payload = PlaceOrderResult(order=order).to_payload()
    assert payload['ok'] is True
    assert payload['accepted'] is True
    assert payload['executed'] is True
    assert payload['status'] == 'filled'
    assert payload['failure_reason'] is None
    assert payload['order'] is not None
