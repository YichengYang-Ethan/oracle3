"""Tests for Feature 8: Atomic Multi-Leg Trades."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from oracle3.trader.atomic_trader import AtomicTrader, AtomicTradeResult, HedgeLeg


class MockKeypair:
    def pubkey(self):
        return MagicMock(__str__=lambda s: '11111111111111111111111111111111')


class TestHedgeLeg:
    def test_frozen_dataclass(self):
        leg = HedgeLeg(
            instrument_type='prediction_market',
            ticker='BTC_YES',
            side='buy',
            qty=10.0,
            price=0.55,
        )
        assert leg.instrument_type == 'prediction_market'
        assert leg.ticker == 'BTC_YES'
        assert leg.qty == 10.0


class TestAtomicTradeResult:
    def test_frozen_dataclass(self):
        result = AtomicTradeResult(
            success=True,
            signature='sig_abc',
            legs=[{'type': 'pm', 'ticker': 'A'}],
            total_cost=15.5,
        )
        assert result.success is True
        assert result.total_cost == 15.5
        assert result.error == ''

    def test_with_error(self):
        result = AtomicTradeResult(
            success=False, signature='', legs=[], total_cost=0.0,
            error='simulation failed',
        )
        assert result.error == 'simulation failed'


class TestAtomicTrader:
    def test_init(self):
        trader = AtomicTrader()
        assert trader._total_attempts == 0
        assert trader._successes == 0

    @pytest.mark.asyncio
    async def test_no_keypair_fails_gracefully(self):
        trader = AtomicTrader(keypair=None)
        result = await trader.place_hedged_order(
            prediction_market_symbol='BTC_YES',
            prediction_side='buy',
            prediction_qty=10.0,
            prediction_price=0.55,
            hedge_instrument='jupiter_swap',
            hedge_ticker='SOL',
            hedge_side='sell',
            hedge_qty=5.0,
            hedge_price=100.0,
        )
        assert result['success'] is False

    @pytest.mark.asyncio
    async def test_with_mocked_build(self):
        kp = MockKeypair()
        trader = AtomicTrader(keypair=kp)
        trader._build_atomic_tx = AsyncMock(return_value=None)
        result = await trader.place_hedged_order(
            prediction_market_symbol='ETH_YES',
            prediction_side='buy',
            prediction_qty=5.0,
            prediction_price=0.60,
            hedge_instrument='drift_perp',
            hedge_ticker='ETH-PERP',
            hedge_side='sell',
            hedge_qty=0.1,
            hedge_price=3000.0,
        )
        assert result['success'] is False
        assert 'Failed to build' in result['error']
        assert len(result['legs']) == 2

    def test_stats_empty(self):
        trader = AtomicTrader()
        stats = trader.stats
        assert stats['total_attempts'] == 0
        assert stats['success_rate'] == 0.0

    @pytest.mark.asyncio
    async def test_stats_after_attempt(self):
        trader = AtomicTrader(keypair=None)
        await trader.place_hedged_order(
            prediction_market_symbol='A', prediction_side='buy',
            prediction_qty=1.0, prediction_price=0.5,
            hedge_instrument='jupiter_swap', hedge_ticker='B',
            hedge_side='sell', hedge_qty=1.0, hedge_price=1.0,
        )
        assert trader.stats['total_attempts'] == 1

    @pytest.mark.asyncio
    async def test_legs_in_result(self):
        trader = AtomicTrader(keypair=None)
        result = await trader.place_hedged_order(
            prediction_market_symbol='MKT_A',
            prediction_side='buy',
            prediction_qty=10.0,
            prediction_price=0.55,
            hedge_instrument='jupiter_swap',
            hedge_ticker='SOL',
            hedge_side='sell',
            hedge_qty=1.0,
            hedge_price=150.0,
        )
        assert len(result['legs']) == 2
        assert result['legs'][0]['instrument_type'] == 'prediction_market'
        assert result['legs'][1]['instrument_type'] == 'jupiter_swap'
