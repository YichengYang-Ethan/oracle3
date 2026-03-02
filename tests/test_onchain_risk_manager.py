"""Tests for Feature 2: Smart Contract Risk Manager."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.onchain_risk_manager import OnChainRiskManager
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.types import TradeSide


@pytest.fixture
def setup():
    pm = PositionManager()
    md = MarketDataManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return pm, md


class TestOnChainRiskManager:
    @pytest.mark.asyncio
    async def test_allows_valid_trade(self, setup):
        pm, md = setup
        rm = OnChainRiskManager(
            position_manager=pm,
            market_data=md,
            max_single_trade_size=Decimal('1000'),
            enable_simulation=False,
        )
        ticker = PolyMarketTicker(
            symbol='TEST', name='Test', token_id='a', market_id='b', event_id='c',
        )
        result = await rm.check_trade(
            ticker, TradeSide.BUY, Decimal('10'), Decimal('0.50')
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_rejects_oversized_trade(self, setup):
        pm, md = setup
        rm = OnChainRiskManager(
            position_manager=pm,
            market_data=md,
            max_single_trade_size=Decimal('5'),
            enable_simulation=False,
        )
        ticker = PolyMarketTicker(
            symbol='TEST', name='Test', token_id='a', market_id='b', event_id='c',
        )
        result = await rm.check_trade(
            ticker, TradeSide.BUY, Decimal('100'), Decimal('0.50')
        )
        assert result is False

    def test_get_risk_status(self, setup):
        pm, md = setup
        rm = OnChainRiskManager(
            position_manager=pm,
            market_data=md,
            enable_simulation=True,
        )
        status = rm.get_risk_status()
        assert 'max_single_trade' in status
        assert 'onchain_simulation_enabled' in status
        assert status['onchain_simulation_enabled'] is True

    def test_reset_daily_tracking(self, setup):
        pm, md = setup
        rm = OnChainRiskManager(position_manager=pm, market_data=md)
        rm._daily_volume = Decimal('5000')
        rm.reset_daily_tracking()
        assert rm._daily_volume == Decimal('0')

    def test_get_current_drawdown(self, setup):
        pm, md = setup
        rm = OnChainRiskManager(position_manager=pm, market_data=md)
        dd = rm.get_current_drawdown()
        assert isinstance(dd, Decimal)

    def test_check_portfolio_health(self, setup):
        pm, md = setup
        rm = OnChainRiskManager(position_manager=pm, market_data=md)
        ok, reason = rm.check_portfolio_health()
        assert ok is True
        assert reason == ''
