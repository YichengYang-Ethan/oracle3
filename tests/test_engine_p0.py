from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.core.trading_engine import TradingEngine
from oracle3.data.data_source import DataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import Event, NewsEvent
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager, StandardRiskManager
from oracle3.strategy.strategy import Strategy
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.trader import Trader


class MockDataSource(DataSource):
    def __init__(self, events: list[Event]):
        self.events = events
        self.idx = 0

    async def get_next_event(self) -> Event | None:
        if self.idx < len(self.events):
            e = self.events[self.idx]
            self.idx += 1
            return e
        return None


class FailingStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        raise RuntimeError('boom')


@pytest.fixture
def paper_trader() -> PaperTrader:
    ticker = PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
        market_id='market123',
        event_id='event123',
    )
    mdm = MarketDataManager()
    ob = OrderBook()
    ob.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
    )
    mdm.order_books[ticker] = ob

    pm = PositionManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return PaperTrader(
        market_data=mdm,
        risk_manager=NoRiskManager(),
        position_manager=pm,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0'),
    )


@pytest.mark.asyncio
async def test_error_storm_auto_degrades_to_read_only(paper_trader: PaperTrader):
    events = [NewsEvent(news=f'n{i}') for i in range(6)]
    engine = TradingEngine(
        data_source=MockDataSource(events),
        strategy=FailingStrategy(),
        trader=paper_trader,
    )
    await engine.start()
    assert paper_trader.read_only is True
    assert engine.strategy.is_paused() is True


@pytest.mark.asyncio
async def test_portfolio_health_breach_degrades_to_read_only(paper_trader: PaperTrader):
    # Use a StandardRiskManager so engine health check is active.
    rm = StandardRiskManager(
        position_manager=paper_trader.position_manager,
        market_data=paper_trader.market_data,
    )
    paper_trader.risk_manager = rm
    engine = TradingEngine(
        data_source=MockDataSource([]),
        strategy=FailingStrategy(),
        trader=paper_trader,
    )

    # Force breach without depending on market math details.
    rm.check_portfolio_health = lambda: (False, 'forced_test_breach')  # type: ignore[method-assign]
    await engine._check_portfolio_health()
    assert paper_trader.read_only is True
    assert engine.strategy.is_paused() is True
