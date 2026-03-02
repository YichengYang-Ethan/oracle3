"""Tests for Feature 6: Multi-Agent Coordination."""

from __future__ import annotations

import pytest

from oracle3.agent.coordinator import (
    AgentCoordinator,
    ExecutionAgent,
    ExecutionResult,
    RiskAgent,
    Signal,
    SignalAgent,
    TradeProposal,
)
from oracle3.strategy.contrib.multi_agent_strategy import MultiAgentStrategy


class TestSignal:
    def test_creation(self):
        s = Signal(ticker='BTC', direction='buy', confidence=0.8, reasoning='bullish')
        assert s.ticker == 'BTC'
        assert s.direction == 'buy'
        assert s.confidence == 0.8


class TestTradeProposal:
    def test_creation(self):
        p = TradeProposal(
            ticker='BTC', side='buy', quantity=10.0, price=0.5,
            approved=True, risk_notes='ok',
        )
        assert p.approved is True
        assert p.quantity == 10.0


class TestExecutionResult:
    def test_creation(self):
        r = ExecutionResult(
            ticker='BTC', side='buy', quantity=10.0, price=0.5,
            success=True, signature='abc',
        )
        assert r.success is True


class TestSignalAgent:
    @pytest.mark.asyncio
    async def test_process_returns_signal(self):
        agent = SignalAgent()
        signal = await agent.process({'ticker': 'BTC', 'price': 50000, 'context': ''})
        assert isinstance(signal, Signal)
        assert signal.ticker == 'BTC'
        assert signal.direction == 'hold'


class TestRiskAgent:
    @pytest.mark.asyncio
    async def test_rejects_hold_signal(self):
        agent = RiskAgent()
        signal = Signal(ticker='BTC', direction='hold', confidence=0.5)
        proposal = await agent.process(signal)
        assert proposal.approved is False

    @pytest.mark.asyncio
    async def test_rejects_low_confidence(self):
        agent = RiskAgent()
        signal = Signal(ticker='BTC', direction='buy', confidence=0.1)
        proposal = await agent.process(signal)
        assert proposal.approved is False

    @pytest.mark.asyncio
    async def test_approves_strong_signal(self):
        agent = RiskAgent()
        signal = Signal(ticker='BTC', direction='buy', confidence=0.8)
        proposal = await agent.process(signal)
        assert proposal.approved is True


class TestExecutionAgent:
    @pytest.mark.asyncio
    async def test_rejects_unapproved(self):
        agent = ExecutionAgent()
        proposal = TradeProposal(
            ticker='BTC', side='buy', quantity=10, price=0.5, approved=False,
        )
        result = await agent.process(proposal)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_executes_approved(self):
        agent = ExecutionAgent()
        proposal = TradeProposal(
            ticker='BTC', side='buy', quantity=10, price=0.5, approved=True,
        )
        result = await agent.process(proposal)
        assert result.success is True


class TestAgentCoordinator:
    @pytest.mark.asyncio
    async def test_run_pipeline(self):
        coordinator = AgentCoordinator()
        result = await coordinator.run_pipeline({
            'ticker': 'BTC', 'price': 0.5, 'context': 'test',
        })
        assert isinstance(result, ExecutionResult)

    @pytest.mark.asyncio
    async def test_delegate_to_signal(self):
        coordinator = AgentCoordinator()
        result = await coordinator.delegate_to_specialist('signal', 'analyze BTC')
        assert 'Signal:' in result

    @pytest.mark.asyncio
    async def test_delegate_to_risk(self):
        coordinator = AgentCoordinator()
        result = await coordinator.delegate_to_specialist('risk', 'check risk')
        assert 'Risk:' in result

    @pytest.mark.asyncio
    async def test_delegate_unknown(self):
        coordinator = AgentCoordinator()
        result = await coordinator.delegate_to_specialist('unknown', 'test')
        assert 'Unknown' in result

    def test_get_pipeline_status(self):
        coordinator = AgentCoordinator()
        status = coordinator.get_pipeline_status()
        assert 'running' in status
        assert status['running'] is False
        assert status['total_results'] == 0

    @pytest.mark.asyncio
    async def test_start_stop(self):
        coordinator = AgentCoordinator()
        await coordinator.start()
        assert coordinator._running is True
        await coordinator.stop()
        assert coordinator._running is False

    @pytest.mark.asyncio
    async def test_results_accumulate(self):
        coordinator = AgentCoordinator()
        await coordinator.run_pipeline({'ticker': 'A', 'price': 0.5, 'context': ''})
        await coordinator.run_pipeline({'ticker': 'B', 'price': 0.6, 'context': ''})
        assert len(coordinator.results) == 2


class TestMultiAgentStrategy:
    def test_init(self):
        s = MultiAgentStrategy()
        assert s.name == 'multi_agent'
        assert s.coordinator is not None

    def test_event_to_task_price_change(self):
        from decimal import Decimal
        from oracle3.events.events import PriceChangeEvent
        from oracle3.ticker.ticker import PolyMarketTicker

        ticker = PolyMarketTicker(
            symbol='TEST', name='Test', token_id='a', market_id='b', event_id='c',
        )
        event = PriceChangeEvent(ticker=ticker, price=Decimal('0.55'))
        task = MultiAgentStrategy._event_to_task(event, None)
        assert task is not None
        assert task['ticker'] == 'TEST'
        assert task['price'] == 0.55

    def test_event_to_task_news(self):
        from oracle3.events.events import NewsEvent

        event = NewsEvent(news='BTC surges', title='Bitcoin Surges')
        task = MultiAgentStrategy._event_to_task(event, None)
        assert task is not None
        assert 'Bitcoin Surges' in task['context']
