from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import Event, PriceChangeEvent
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.strategy.agent_strategy import AgentStrategy
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.trader import Trader


class DummyOpenAIAgentStrategy(AgentStrategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        return


@pytest.fixture
def test_ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
        market_id='market123',
        event_id='event123',
        no_token_id='TEST_TOKEN_NO',
    )


@pytest.fixture
def paper_trader(test_ticker: PolyMarketTicker) -> PaperTrader:
    market_data = MarketDataManager()
    market_data.process_price_change_event(
        PriceChangeEvent(ticker=test_ticker, price=Decimal('0.52'), timestamp='t1')
    )
    related = PolyMarketTicker(
        symbol='RELATED',
        name='Related Market',
        token_id='related123',
        market_id='market456',
        event_id='event456',
    )
    market_data.process_price_change_event(
        PriceChangeEvent(ticker=related, price=Decimal('0.61'), timestamp='t2')
    )

    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0'),
    )
    trader.record_news(
        timestamp='12:00:00',
        title='Cross-market linkage observed',
        source='test',
        url='https://example.com',
    )
    return trader


def test_sdk_available_handles_missing_package(monkeypatch) -> None:
    monkeypatch.setattr(
        'oracle3.strategy.agent_strategy._import_agents_sdk',
        lambda: (_ for _ in ()).throw(RuntimeError('missing sdk')),
    )
    assert DummyOpenAIAgentStrategy.sdk_available() is False


@pytest.mark.asyncio
async def test_agent_strategy_runs_through_openai_sdk_adapter(
    monkeypatch,
    test_ticker: PolyMarketTicker,
    paper_trader: PaperTrader,
) -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.name = kwargs['name']
            self.instructions = kwargs['instructions']
            self.model = kwargs['model']
            self.tools = kwargs['tools']

    class FakeRunResult:
        final_output = '{"action":"hold","target_symbol":"TEST_TOKEN","reasoning":"analysis complete","confidence":0.7}'

    class FakeRunner:
        @staticmethod
        async def run(agent, input_text, max_turns):  # type: ignore[no-untyped-def]
            captured['agent'] = agent
            captured['input_text'] = input_text
            captured['max_turns'] = max_turns
            return FakeRunResult()

    def fake_function_tool(fn):  # type: ignore[no-untyped-def]
        return fn

    monkeypatch.setattr(
        'oracle3.strategy.agent_strategy._import_agents_sdk',
        lambda: (FakeAgent, FakeRunner, fake_function_tool),
    )

    event = PriceChangeEvent(ticker=test_ticker, price=Decimal('0.52'), timestamp='t3')
    strategy = DummyOpenAIAgentStrategy()
    context = strategy.bind_context(event, paper_trader)

    result = await strategy.run_openai_agent(
        input_text='analyze relation', context=context
    )
    agent = captured['agent']
    tools = {tool.__name__: tool for tool in agent.tools}

    assert DummyOpenAIAgentStrategy.sdk_available() is True
    assert strategy.get_run_final_output(result).startswith('{')
    assert captured['input_text'] == 'analyze relation'
    assert captured['max_turns'] == 8
    assert 'resolve_trade_ticker(symbol, side)' in agent.instructions
    listed = tools['list_available_tickers']()
    assert [row['symbol'] for row in listed] == ['TEST_TOKEN', 'RELATED']
    assert tools['get_price_history']('RELATED', 5) == [0.61]
    assert (
        tools['resolve_trade_contract']('TEST_TOKEN', 'no')['symbol'] == 'TEST_TOKEN_NO'
    )
    assert tools['get_recent_news'](5)[0]['title'] == 'Cross-market linkage observed'
