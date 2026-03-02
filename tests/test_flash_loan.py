"""Tests for Feature 7: Flash Loan Arbitrage."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from oracle3.trader.flash_loan import FlashLoanArbitrage, FlashLoanResult


class MockKeypair:
    def pubkey(self):
        return MagicMock(__str__=lambda s: '11111111111111111111111111111111')


class TestFlashLoanResult:
    def test_frozen_dataclass(self):
        result = FlashLoanResult(
            success=True,
            signature='sig123',
            profit=0.05,
            borrow_amount=1000.0,
            protocol='marginfi',
        )
        assert result.success is True
        assert result.profit == 0.05
        assert result.protocol == 'marginfi'
        assert result.error == ''

    def test_with_error(self):
        result = FlashLoanResult(
            success=False, signature='', profit=0.0,
            borrow_amount=500.0, protocol='solend', error='insufficient liquidity',
        )
        assert result.success is False
        assert result.error == 'insufficient liquidity'


class TestFlashLoanArbitrage:
    def test_init_defaults(self):
        fla = FlashLoanArbitrage()
        assert fla.protocol == 'marginfi'
        assert fla.max_borrow == 10_000.0
        assert fla.min_profit_bps == 50

    @pytest.mark.asyncio
    async def test_exceeds_max_borrow(self):
        fla = FlashLoanArbitrage(max_borrow=100.0)
        result = await fla.execute_flash_arbitrage('A', 'B', 200.0)
        assert result['success'] is False
        assert 'exceeds' in result['error']
        assert fla._total_attempts == 1

    @pytest.mark.asyncio
    async def test_no_keypair_fails_gracefully(self):
        fla = FlashLoanArbitrage(keypair=None)
        result = await fla.execute_flash_arbitrage('A', 'B', 50.0)
        assert result['success'] is False

    @pytest.mark.asyncio
    async def test_with_mocked_submission(self):
        kp = MockKeypair()
        fla = FlashLoanArbitrage(keypair=kp)
        fla._build_flash_loan_tx = AsyncMock(return_value=None)
        result = await fla.execute_flash_arbitrage('MKT_A', 'MKT_B', 100.0)
        assert result['success'] is False
        assert 'Failed to build' in result['error']

    def test_stats_empty(self):
        fla = FlashLoanArbitrage()
        stats = fla.stats
        assert stats['total_attempts'] == 0
        assert stats['successes'] == 0
        assert stats['success_rate'] == 0.0

    @pytest.mark.asyncio
    async def test_stats_after_attempt(self):
        fla = FlashLoanArbitrage(max_borrow=10.0)
        await fla.execute_flash_arbitrage('A', 'B', 20.0)
        stats = fla.stats
        assert stats['total_attempts'] == 1
        assert stats['successes'] == 0
