"""Tests for storage.serializers and storage.StateStore."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from oracle3.position.position_manager import Position
from oracle3.storage.serializers import (
    deserialize_equity_point,
    deserialize_order,
    deserialize_position,
    deserialize_ticker,
    deserialize_trade,
    serialize_equity_point,
    serialize_order,
    serialize_position,
    serialize_ticker,
    serialize_trade,
)
from oracle3.storage.state_store import StateStore
from oracle3.ticker.ticker import CashTicker, KalshiTicker, PolyMarketTicker
from oracle3.trader.types import Order, OrderStatus, Trade, TradeSide

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def poly_ticker():
    return PolyMarketTicker(
        symbol='TOKEN-ABC',
        name='Will BTC hit 100k?',
        token_id='abc123',
        market_id='mkt1',
        event_id='evt1',
        no_token_id='xyz999',
    )


@pytest.fixture
def kalshi_ticker():
    return KalshiTicker(
        symbol='KALSHI-SYM',
        name='Kalshi market',
        market_ticker='MKT-T1',
        event_ticker='EVT-T1',
        series_ticker='SER-T1',
    )


@pytest.fixture
def sample_trade(poly_ticker):
    return Trade(
        side=TradeSide.BUY,
        ticker=poly_ticker,
        price=Decimal('0.45'),
        quantity=Decimal('10'),
        commission=Decimal('0.0'),
    )


@pytest.fixture
def sample_position(poly_ticker):
    return Position(
        ticker=poly_ticker,
        quantity=Decimal('10.5'),
        average_cost=Decimal('0.45'),
        realized_pnl=Decimal('1.25'),
    )


@pytest.fixture
def tmp_store(tmp_path):
    return StateStore(tmp_path / 'trading_data')


# ---------------------------------------------------------------------------
# serialize_ticker / deserialize_ticker round-trips
# ---------------------------------------------------------------------------


def test_roundtrip_polymarket_ticker(poly_ticker):
    d = serialize_ticker(poly_ticker)
    assert d['ticker_type'] == 'PolyMarketTicker'
    result = deserialize_ticker(d)
    assert result == poly_ticker


def test_roundtrip_kalshi_ticker(kalshi_ticker):
    d = serialize_ticker(kalshi_ticker)
    assert d['ticker_type'] == 'KalshiTicker'
    result = deserialize_ticker(d)
    assert result == kalshi_ticker


def test_roundtrip_kalshi_ticker_no_side():
    no_ticker = KalshiTicker(
        symbol='MKT_NO', name='Market', market_ticker='MKT-T1',
        event_ticker='EVT-T1', series_ticker='SER-T1', is_no_side=True,
    )
    d = serialize_ticker(no_ticker)
    assert d['is_no_side'] is True
    result = deserialize_ticker(d)
    assert result == no_ticker
    assert result.is_no_side is True


def test_roundtrip_cashticker_polymarket_usdc():
    d = serialize_ticker(CashTicker.POLYMARKET_USDC)
    assert d['ticker_type'] == 'CashTicker'
    result = deserialize_ticker(d)
    assert result is CashTicker.POLYMARKET_USDC


def test_roundtrip_cashticker_kalshi_usd():
    d = serialize_ticker(CashTicker.KALSHI_USD)
    result = deserialize_ticker(d)
    assert result is CashTicker.KALSHI_USD


# ---------------------------------------------------------------------------
# serialize_trade / deserialize_trade
# ---------------------------------------------------------------------------


def test_roundtrip_trade_preserves_decimal_precision(sample_trade):
    from datetime import datetime

    d = serialize_trade(sample_trade, datetime.now())
    assert d['side'] == 'buy'
    assert d['price'] == '0.45'
    assert d['quantity'] == '10'
    result = deserialize_trade(d)
    assert result.price == Decimal('0.45')
    assert result.quantity == Decimal('10')
    assert result.side == TradeSide.BUY
    assert result.ticker == sample_trade.ticker


def test_trade_without_timestamp(sample_trade):
    d = serialize_trade(sample_trade)
    assert 'timestamp' not in d
    result = deserialize_trade(d)
    assert result.price == sample_trade.price


# ---------------------------------------------------------------------------
# StateStore.save_positions / load_positions
# ---------------------------------------------------------------------------


def test_save_load_positions_roundtrip(tmp_store, sample_position):
    from oracle3.position.position_manager import PositionManager

    pm = PositionManager()
    pm.update_position(sample_position)
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('5000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    tmp_store.save_positions(pm)
    loaded = tmp_store.load_positions()

    symbols = {p.ticker.symbol for p in loaded}
    assert sample_position.ticker.symbol in symbols
    assert CashTicker.POLYMARKET_USDC.symbol in symbols

    loaded_pos = next(
        p for p in loaded if p.ticker.symbol == sample_position.ticker.symbol
    )
    assert loaded_pos.quantity == sample_position.quantity
    assert loaded_pos.average_cost == sample_position.average_cost
    assert loaded_pos.realized_pnl == sample_position.realized_pnl


def test_load_positions_missing_file_returns_empty(tmp_store):
    result = tmp_store.load_positions()
    assert result == []


# ---------------------------------------------------------------------------
# StateStore.append_trade / load_trades
# ---------------------------------------------------------------------------


def test_append_multiple_trades_accumulate(tmp_store, poly_ticker):
    t1 = Trade(TradeSide.BUY, poly_ticker, Decimal('0.4'), Decimal('5'), Decimal('0'))
    t2 = Trade(TradeSide.SELL, poly_ticker, Decimal('0.6'), Decimal('5'), Decimal('0'))

    tmp_store.append_trade(t1)
    tmp_store.append_trade(t2)

    loaded = tmp_store.load_trades()
    assert len(loaded) == 2
    assert loaded[0].side == TradeSide.BUY
    assert loaded[1].side == TradeSide.SELL


def test_load_trades_missing_file_returns_empty(tmp_store):
    assert tmp_store.load_trades() == []


# ---------------------------------------------------------------------------
# Atomic write (temp-file rename pattern)
# ---------------------------------------------------------------------------


def test_atomic_write_creates_no_tmp_file(tmp_store, sample_position):
    from oracle3.position.position_manager import PositionManager

    pm = PositionManager()
    pm.update_position(sample_position)
    tmp_store.save_positions(pm)

    tmp_files = list((tmp_store.data_dir).glob('*.tmp'))
    assert tmp_files == [], 'Temp files should be cleaned up after atomic write'

    positions_file = tmp_store.data_dir / 'positions.json'
    assert positions_file.exists()
    data = json.loads(positions_file.read_text())
    assert 'saved_at' in data
    assert 'positions' in data


# ---------------------------------------------------------------------------
# Equity point serialization
# ---------------------------------------------------------------------------


def test_roundtrip_equity_point():
    from oracle3.analytics.performance_analyzer import EquityPoint

    pt = EquityPoint(timestamp=5, equity=Decimal('10500.75'), trade_index=4)
    d = serialize_equity_point(pt)
    assert d['equity'] == '10500.75'

    result = deserialize_equity_point(d)
    assert result.equity == Decimal('10500.75')
    assert result.timestamp == 5
    assert result.trade_index == 4


# ---------------------------------------------------------------------------
# Order serialization
# ---------------------------------------------------------------------------


def test_roundtrip_order(sample_trade, poly_ticker):
    order = Order(
        status=OrderStatus.FILLED,
        side=TradeSide.BUY,
        ticker=poly_ticker,
        limit_price=Decimal('0.45'),
        filled_quantity=Decimal('10'),
        average_price=Decimal('0.45'),
        trades=[sample_trade],
        remaining=Decimal('0'),
        commission=Decimal('0'),
    )
    d = serialize_order(order)
    result = deserialize_order(d)
    assert result.status == OrderStatus.FILLED
    assert result.filled_quantity == Decimal('10')
    assert len(result.trades) == 1
    assert result.trades[0].price == Decimal('0.45')
