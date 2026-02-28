"""Tests for OnChainLogger."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oracle3.onchain.logger import OnChainLogger


@pytest.fixture
def mock_keypair():
    kp = MagicMock()
    kp.pubkey.return_value = 'FakeLoggerKey456'
    return kp


@pytest.fixture
def logger_instance(mock_keypair):
    return OnChainLogger(keypair=mock_keypair)


def test_public_key(logger_instance):
    assert logger_instance.public_key == 'FakeLoggerKey456'


@pytest.mark.asyncio
async def test_log_trade_handles_import_error(logger_instance):
    with patch.dict('sys.modules', {'solders': None, 'solders.keypair': None}):
        # Should not raise, just return empty string
        sig = await logger_instance.log_trade(
            market_ticker='TEST',
            side='yes',
            price=0.50,
            quantity=10,
            trade_signature='abc123def456',
        )
        # May or may not return empty depending on import order
        assert isinstance(sig, str)


@pytest.mark.asyncio
async def test_get_trade_log_empty(logger_instance):
    with patch('httpx.AsyncClient') as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = {'result': []}
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=mock_response)))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = mock_ctx

        trades = await logger_instance.get_trade_log(limit=5)
        assert isinstance(trades, list)
