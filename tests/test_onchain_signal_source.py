"""Tests for Feature 3: On-Chain Data Signal Source."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.data.live.onchain_signal_source import (
    OnChainSignal,
    OnChainSignalSource,
    WatchedWallet,
)
from oracle3.events.events import OnChainSignalEvent


class TestOnChainSignalEvent:
    def test_creation(self):
        event = OnChainSignalEvent(
            signal_type='whale_transfer',
            wallet='ABC123',
            amount=Decimal('50000'),
            direction='inflow',
        )
        assert event.signal_type == 'whale_transfer'
        assert event.wallet == 'ABC123'
        assert event.amount == Decimal('50000')
        assert event.direction == 'inflow'

    def test_str_repr(self):
        event = OnChainSignalEvent(
            signal_type='tvl_change',
            wallet='DEADBEEF1234',
            amount=Decimal('100'),
            direction='increase',
        )
        s = str(event)
        assert 'tvl_change' in s
        assert 'DEADBEEF' in s

    def test_trigger(self):
        event = OnChainSignalEvent(signal_type='test')
        event.trigger()  # should not raise


class TestOnChainSignal:
    def test_frozen_dataclass(self):
        signal = OnChainSignal(
            signal_type='whale_transfer',
            wallet='abc',
            amount=1000.0,
            direction='outflow',
            token='USDC',
            timestamp=1000.0,
            label='Whale 1',
        )
        assert signal.signal_type == 'whale_transfer'
        assert signal.label == 'Whale 1'


class TestWatchedWallet:
    def test_frozen_dataclass(self):
        w = WatchedWallet(address='abc123', label='Test Whale')
        assert w.address == 'abc123'
        assert w.label == 'Test Whale'


class TestOnChainSignalSource:
    def test_init_defaults(self):
        src = OnChainSignalSource()
        assert src.polling_interval == 30.0
        assert src.large_transfer_threshold == 10_000.0
        assert src.watched_wallets == []

    def test_init_with_wallets(self):
        wallets = [WatchedWallet('addr1', 'Whale A')]
        src = OnChainSignalSource(watched_wallets=wallets)
        assert len(src.watched_wallets) == 1

    def test_get_signals_empty(self):
        src = OnChainSignalSource()
        signals = src.get_onchain_signals(limit=5)
        assert signals == []

    def test_signals_property(self):
        src = OnChainSignalSource()
        assert src.signals == []

    @pytest.mark.asyncio
    async def test_get_next_event_timeout(self):
        src = OnChainSignalSource()
        result = await src.get_next_event()
        assert result is None

    @pytest.mark.asyncio
    async def test_start_stop(self):
        src = OnChainSignalSource(polling_interval=100.0)
        await src.start()
        assert src._running is True
        await src.stop()
        assert src._running is False
