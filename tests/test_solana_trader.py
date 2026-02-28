"""Tests for SolanaTrader."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker, SolanaTicker
from oracle3.trader.types import OrderFailureReason, TradeSide


@pytest.fixture
def mock_keypair():
    kp = MagicMock()
    kp.pubkey.return_value = 'FakePublicKey123'
    return kp


@pytest.fixture
def trader(mock_keypair):
    from oracle3.trader.solana_trader import SolanaTrader

    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.DFLOW_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    risk_manager = NoRiskManager()

    with patch('oracle3.trader.solana_trader._load_keypair', return_value=mock_keypair):
        return SolanaTrader(
            market_data=market_data,
            risk_manager=risk_manager,
            position_manager=position_manager,
            keypair=mock_keypair,
        )


def test_public_key(trader):
    assert trader.public_key == 'FakePublicKey123'


@pytest.mark.asyncio
async def test_invalid_quantity(trader):
    ticker = SolanaTicker(symbol='X', market_ticker='X')
    result = await trader.place_order(
        side=TradeSide.BUY,
        ticker=ticker,
        limit_price=Decimal('0.50'),
        quantity=Decimal('0'),
    )
    assert result.order is None
    assert result.failure_reason == OrderFailureReason.INVALID_ORDER


@pytest.mark.asyncio
async def test_wrong_ticker_type(trader):
    ticker = PolyMarketTicker(symbol='X', token_id='X')
    result = await trader.place_order(
        side=TradeSide.BUY,
        ticker=ticker,
        limit_price=Decimal('0.50'),
        quantity=Decimal('10'),
    )
    assert result.order is None
    assert result.failure_reason == OrderFailureReason.INVALID_ORDER


@pytest.mark.asyncio
async def test_insufficient_cash(trader):
    ticker = SolanaTicker(symbol='X', market_ticker='X')
    result = await trader.place_order(
        side=TradeSide.BUY,
        ticker=ticker,
        limit_price=Decimal('0.50'),
        quantity=Decimal('50000'),
    )
    assert result.order is None
    assert result.failure_reason == OrderFailureReason.INSUFFICIENT_CASH


@pytest.mark.asyncio
async def test_sell_without_position(trader):
    ticker = SolanaTicker(symbol='NOPOS', market_ticker='NOPOS')
    result = await trader.place_order(
        side=TradeSide.SELL,
        ticker=ticker,
        limit_price=Decimal('0.50'),
        quantity=Decimal('10'),
    )
    assert result.order is None
    assert result.failure_reason == OrderFailureReason.INVALID_ORDER
