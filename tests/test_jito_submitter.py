"""Tests for Feature 4: MEV Protection (Jito)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oracle3.trader.jito_submitter import (
    DEFAULT_TIP_LAMPORTS,
    JITO_MAINNET_URL,
    JITO_TIP_ACCOUNT,
    JitoSubmitResult,
    JitoSubmitter,
)


class MockKeypair:
    def pubkey(self):
        return MagicMock(__str__=lambda s: '11111111111111111111111111111111')


class TestJitoSubmitResult:
    def test_frozen_dataclass(self):
        result = JitoSubmitResult(
            success=True,
            bundle_id='bundle123',
            signature='sig456',
            tip_lamports=10000,
        )
        assert result.success is True
        assert result.bundle_id == 'bundle123'
        assert result.tip_lamports == 10000
        assert result.fallback_used is False

    def test_fallback_flag(self):
        result = JitoSubmitResult(
            success=True,
            bundle_id='',
            signature='sig',
            tip_lamports=0,
            fallback_used=True,
        )
        assert result.fallback_used is True


class TestJitoSubmitter:
    def test_init_defaults(self):
        kp = MockKeypair()
        submitter = JitoSubmitter(keypair=kp)
        assert submitter.jito_url == JITO_MAINNET_URL
        assert submitter.tip_lamports == DEFAULT_TIP_LAMPORTS
        assert submitter.tip_account == JITO_TIP_ACCOUNT

    def test_public_key(self):
        kp = MockKeypair()
        submitter = JitoSubmitter(keypair=kp)
        pk = submitter.public_key
        assert isinstance(pk, str)

    def test_get_mev_protection_status(self):
        kp = MockKeypair()
        submitter = JitoSubmitter(keypair=kp)
        status = submitter.get_mev_protection_status()
        assert status['enabled'] is True
        assert status['tip_lamports'] == DEFAULT_TIP_LAMPORTS
        assert status['total_submitted'] == 0
        assert status['jito_success_rate'] == 0.0

    def test_disabled_submitter(self):
        kp = MockKeypair()
        submitter = JitoSubmitter(keypair=kp)
        submitter._enabled = False
        status = submitter.get_mev_protection_status()
        assert status['enabled'] is False

    @pytest.mark.asyncio
    async def test_submit_falls_back_on_error(self):
        kp = MockKeypair()
        submitter = JitoSubmitter(keypair=kp)
        submitter._enabled = False  # force fallback

        # Mock the standard RPC submission
        submitter._submit_standard_rpc = AsyncMock(return_value='fallback_sig')

        result = await submitter.submit_with_jito(b'\x00' * 10)
        assert result.fallback_used is True
        assert result.signature == 'fallback_sig'

    def test_extract_signature_invalid(self):
        sig = JitoSubmitter._extract_signature(b'\x00\x01\x02')
        assert sig == ''
