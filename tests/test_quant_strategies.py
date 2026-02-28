from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import OrderBookEvent, PriceChangeEvent
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.strategy.agent_strategy import AgentStrategy
from oracle3.strategy.market_making_strategy import MarketMakingStrategy
from oracle3.strategy.orderbook_imbalance_strategy import (
    OrderBookImbalanceStrategy,
)
from oracle3.strategy.quant_strategy import QuantStrategy
from oracle3.strategy.simple_strategy import SimpleStrategy
from oracle3.strategy.strategy import Strategy, StrategyDecision
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader


class DummyStrategy(Strategy):
    def __init__(self) -> None:
        super().__init__()

    async def process_event(self, event, trader) -> None:  # type: ignore[no-untyped-def]
        return


class DummyAgentStrategy(AgentStrategy):
    async def process_event(self, event, trader) -> None:  # type: ignore[no-untyped-def]
        return


class DummyQuantStrategy(QuantStrategy):
    async def process_event(self, event, trader) -> None:  # type: ignore[no-untyped-def]
        return


@pytest.fixture
def test_ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
        market_id='market123',
        event_id='event123',
    )


@pytest.fixture
def other_ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='OTHER_TOKEN',
        name='Other Market',
        token_id='token999',
        market_id='market999',
        event_id='event999',
    )


@pytest.fixture
def market_data(test_ticker: PolyMarketTicker) -> MarketDataManager:
    mdm = MarketDataManager()
    ob = OrderBook()
    ob.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
    )
    mdm.order_books[test_ticker] = ob
    return mdm


@pytest.fixture
def position_manager() -> PositionManager:
    pm = PositionManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return pm


@pytest.fixture
def paper_trader(
    market_data: MarketDataManager, position_manager: PositionManager
) -> PaperTrader:
    return PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0'),
    )


def _set_order_book(
    md: MarketDataManager,
    ticker: PolyMarketTicker,
    *,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
) -> None:
    ob = OrderBook()
    ob.update(
        bids=[Level(price=Decimal(p), size=Decimal(s)) for p, s in bids],
        asks=[Level(price=Decimal(p), size=Decimal(s)) for p, s in asks],
    )
    md.order_books[ticker] = ob


def _open_yes_position(
    pm: PositionManager, ticker: PolyMarketTicker, qty: str = '10'
) -> None:
    pm.update_position(
        Position(
            ticker=ticker,
            quantity=Decimal(qty),
            average_cost=Decimal('0.50'),
            realized_pnl=Decimal('0'),
        )
    )


def test_strategy_interface_defaults() -> None:
    decision = StrategyDecision(
        timestamp='12:00:00',
        ticker_name='Test',
        action='HOLD',
        executed=False,
    )
    assert decision.signal_values == {}
    assert decision.confidence == 0.0

    base = DummyStrategy()
    assert base.get_decisions() == []
    stats = base.get_decision_stats()
    assert stats['decisions'] == 0
    assert stats['executed'] == 0
    assert stats['holds'] == 0


def test_strategy_record_decision_default_buffer() -> None:
    base = DummyStrategy()
    base.record_decision(
        ticker_name='Test',
        action='BUY',
        executed=True,
        reasoning='compat path',
        signal_values={'edge': 0.1},
    )
    decisions = base.get_decisions()
    stats = base.get_decision_stats()
    assert len(decisions) == 1
    assert decisions[0].action == 'BUY'
    assert decisions[0].signal_values['edge'] == 0.1
    assert stats['decisions'] == 1
    assert stats['executed'] == 1
    assert stats['buy_yes'] == 1


def test_unified_strategy_context_helpers(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    event = PriceChangeEvent(
        ticker=test_ticker,
        price=Decimal('0.52'),
        timestamp='t1',
    )
    paper_trader.market_data.process_price_change_event(event)
    paper_trader.record_news(
        timestamp='12:00:00',
        title='Linked market moved',
        source='test',
        url='https://example.com',
    )

    base = DummyStrategy()
    context = base.bind_context(event, paper_trader)

    assert base.get_context() is context
    assert context.event_type == 'PriceChangeEvent'
    assert context.ticker == test_ticker
    assert context.price_history() == [Decimal('0.52')]
    assert len(context.market_history()) == 1
    assert context.recent_news(limit=1)[0]['title'] == 'Linked market moved'
    assert any(book.symbol == test_ticker.symbol for book in context.order_books())
    assert any(pos.is_cash for pos in context.positions())
    assert context.resolve_ticker(test_ticker.symbol) == test_ticker
    assert context.resolve_trade_ticker(test_ticker.symbol, 'yes') == test_ticker


def test_agent_and_quant_helpers_share_same_context_contract(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    event = PriceChangeEvent(
        ticker=test_ticker,
        price=Decimal('0.54'),
        timestamp='t2',
    )
    paper_trader.market_data.process_price_change_event(event)
    paper_trader.record_news(
        timestamp='12:01:00',
        title='Related market repriced',
        source='test',
        url='https://example.com/2',
    )
    related_ticker = PolyMarketTicker(
        symbol='RELATED',
        name='Related Market',
        token_id='related',
        market_id='M2',
        event_id='E2',
    )
    related_event = PriceChangeEvent(
        ticker=related_ticker,
        price=Decimal('0.61'),
        timestamp='t2',
    )
    paper_trader.market_data.process_price_change_event(related_event)

    agent = DummyAgentStrategy()
    quant = DummyQuantStrategy()
    agent_context = agent.bind_context(event, paper_trader)
    quant_context = quant.bind_context(event, paper_trader)

    prompt = agent.build_prompt_context(agent_context)
    prepared = quant.prepare_data(quant_context)

    assert 'context.price_history' in prompt
    assert 'price_history' in prompt
    assert 'recent_news' in prompt
    assert prepared['ticker'] == test_ticker.symbol
    assert prepared['price_history'] == [Decimal('0.54')]
    assert len(prepared['market_history']) == 2
    assert prepared['recent_news'][0]['title'] == 'Related market repriced'
    assert quant_context.resolve_ticker('RELATED') == related_ticker
    assert quant_context.resolve_trade_ticker(test_ticker.symbol, 'no') is None
    assert len(quant_context.available_tickers()) >= 2
    assert len(quant_context.available_tickers(include_complements=False)) >= 2


def test_simple_strategy_decision_wrapping() -> None:
    strategy = SimpleStrategy()
    strategy.record_decision(
        ticker_name='My Market',
        action='BUY_YES',
        executed=True,
        reasoning='edge positive',
        confidence=0.8,
        signal_values={
            'llm_prob': 0.72,
            'market_price': 0.55,
            'edge': 0.17,
        },
        timestamp='12:34:56',
    )

    wrapped = strategy.get_decisions()
    assert len(wrapped) == 1
    assert wrapped[0].signal_values['llm_prob'] == 0.72
    assert wrapped[0].signal_values['market_price'] == 0.55
    assert wrapped[0].signal_values['edge'] == 0.17

    stats = strategy.get_decision_stats()
    assert set(stats.keys()) >= {
        'decisions',
        'executed',
        'buy_yes',
        'buy_no',
        'holds',
        'closes',
    }


@pytest.mark.asyncio
async def test_obi_balanced_no_trade(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = OrderBookImbalanceStrategy(tickers=[test_ticker], entry_threshold=0.3)
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '100'), ('0.49', '100')],
        asks=[('0.51', '100'), ('0.52', '100')],
    )
    event = OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0'))
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decision_stats()['decisions'] == 1
    assert strategy.get_decisions()[-1].action == 'HOLD'


@pytest.mark.asyncio
async def test_obi_entry_exit_and_guards(
    test_ticker: PolyMarketTicker,
    other_ticker: PolyMarketTicker,
    paper_trader: PaperTrader,
) -> None:
    strategy = OrderBookImbalanceStrategy(
        tickers=[test_ticker],
        depth=3,
        entry_threshold=0.3,
        exit_threshold=-0.1,
        max_hold_seconds=1,
    )

    _set_order_book(
        paper_trader.market_data,
        other_ticker,
        bids=[('0.50', '500')],
        asks=[('0.51', '500')],
    )
    await strategy.process_event(
        OrderBookEvent(other_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decision_stats()['decisions'] == 0

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '1200'), ('0.49', '200')],
        asks=[('0.51', '200'), ('0.52', '100')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'BUY_YES'
    assert strategy.get_decisions()[-1].executed is True

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.49', '100')],
        asks=[('0.50', '1000'), ('0.51', '700')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.49'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'CLOSE_OBI'

    _open_yes_position(paper_trader.position_manager, test_ticker, '5')
    strategy._entries[test_ticker.symbol] = datetime.now() - timedelta(seconds=10)
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '500')],
        asks=[('0.51', '500')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'CLOSE_TIMEOUT'

    _open_yes_position(paper_trader.position_manager, test_ticker, '5')
    strategy._entries[test_ticker.symbol] = datetime.now()
    strategy._closing_in_progress.add(test_ticker.symbol)
    prev_orders = len(paper_trader.orders)
    prev_decisions = strategy.get_decision_stats()['decisions']
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert len(paper_trader.orders) == prev_orders
    assert strategy.get_decision_stats()['decisions'] == prev_decisions


@pytest.mark.asyncio
async def test_market_making_entry_and_exits(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = MarketMakingStrategy(
        tickers=[test_ticker],
        min_spread=Decimal('0.05'),
        take_profit_pct=0.5,
        stop_loss_pct=0.02,
        max_hold_seconds=1,
        tick=Decimal('0.01'),
    )
    event = OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0'))

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '1000')],
        asks=[('0.53', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'HOLD'

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.45', '1000')],
        asks=[('0.55', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'BUY_YES'
    assert test_ticker.symbol in strategy._entries

    _open_yes_position(paper_trader.position_manager, test_ticker, '10')
    strategy._entries[test_ticker.symbol] = (
        datetime.now(),
        Decimal('0.46'),
        Decimal('0.50'),
        Decimal('0.40'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.51', '1000')],
        asks=[('0.56', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_TP'

    _open_yes_position(paper_trader.position_manager, test_ticker, '10')
    strategy._entries[test_ticker.symbol] = (
        datetime.now(),
        Decimal('0.46'),
        Decimal('0.50'),
        Decimal('0.44'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.43', '1000')],
        asks=[('0.48', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_SL'

    _open_yes_position(paper_trader.position_manager, test_ticker, '10')
    strategy._entries[test_ticker.symbol] = (
        datetime.now() - timedelta(seconds=10),
        Decimal('0.46'),
        Decimal('0.60'),
        Decimal('0.40'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.47', '1000')],
        asks=[('0.56', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_TIMEOUT'


@pytest.mark.asyncio
async def test_market_making_no_cross_spread_entry(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = MarketMakingStrategy(
        tickers=[test_ticker],
        min_spread=Decimal('0.005'),
        tick=Decimal('0.01'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.49', '1000')],
        asks=[('0.50', '1000')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.49'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'HOLD'
    assert strategy.get_decisions()[-1].executed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('factory', 'event_factory'),
    [
        (
            lambda t: OrderBookImbalanceStrategy(tickers=[t]),
            lambda t: OrderBookEvent(t, Decimal('0.50'), Decimal('100'), Decimal('0')),
        ),
        (
            lambda t: MarketMakingStrategy(tickers=[t]),
            lambda t: OrderBookEvent(t, Decimal('0.50'), Decimal('100'), Decimal('0')),
        ),
    ],
)
async def test_quant_strategies_respect_pause(
    test_ticker: PolyMarketTicker,
    paper_trader: PaperTrader,
    factory,
    event_factory,
) -> None:
    strategy = factory(test_ticker)
    strategy.set_paused(True)
    before_orders = len(paper_trader.orders)
    await strategy.process_event(event_factory(test_ticker), paper_trader)
    assert strategy.get_decisions() == []
    assert strategy.get_decision_stats().get('decisions', 0) == 0
    assert len(paper_trader.orders) == before_orders
