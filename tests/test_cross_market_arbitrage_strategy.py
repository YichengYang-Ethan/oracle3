"""Tests for Feature 1: Cross-Market Arbitrage Strategy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.strategy.contrib.cross_market_arbitrage_strategy import (
    CrossMarketArbitrageStrategy,
    TickerGrouper,
    _normalize,
)
from oracle3.ticker.ticker import PolyMarketTicker, SolanaTicker


class TestNormalize:
    def test_lowercases_and_removes_punctuation(self):
        assert _normalize('Will BTC hit $100k?') == 'btc hit 100k'

    def test_removes_stopwords(self):
        assert _normalize('the price of ETH') == 'price eth'


class TestTickerGrouper:
    def test_groups_similar_tickers(self):
        grouper = TickerGrouper(min_similarity=0.5)
        t1 = PolyMarketTicker(
            symbol='BTC_100K_YES',
            name='Will BTC hit 100k by end of 2026',
            token_id='tok1',
            market_id='mkt1',
            event_id='evt1',
        )
        t2 = SolanaTicker(
            symbol='BTC_100K_DFLOW',
            name='Will Bitcoin hit 100k by 2026',
            market_ticker='mkt2',
            event_ticker='evt2',
        )

        groups = grouper.group({
            'polymarket': [t1],
            'dflow': [t2],
        })
        assert len(groups) >= 1
        assert 'polymarket' in groups[0].tickers
        assert 'dflow' in groups[0].tickers

    def test_does_not_group_dissimilar(self):
        grouper = TickerGrouper(min_similarity=0.9)
        t1 = PolyMarketTicker(
            symbol='BTC_YES', name='Bitcoin price',
            token_id='a', market_id='b', event_id='c',
        )
        t2 = SolanaTicker(
            symbol='ETH_YES', name='Ethereum merge date',
            market_ticker='d', event_ticker='e',
        )
        groups = grouper.group({'poly': [t1], 'dflow': [t2]})
        assert len(groups) == 0

    def test_needs_two_platforms(self):
        grouper = TickerGrouper()
        t1 = PolyMarketTicker(
            symbol='A', name='test', token_id='a', market_id='b', event_id='c',
        )
        assert grouper.group({'poly': [t1]}) == []


class TestCrossMarketArbitrageStrategy:
    def test_find_arbitrage_opportunities_no_prices(self):
        s = CrossMarketArbitrageStrategy(min_edge=0.03)
        assert s.find_arbitrage_opportunities() == []

    def test_find_opportunities_with_edge(self):
        s = CrossMarketArbitrageStrategy(min_edge=0.03, fee_rate=0.0)
        t1 = PolyMarketTicker(
            symbol='EVT_POLY', name='Same Event',
            token_id='a', market_id='b', event_id='c',
        )
        t2 = SolanaTicker(
            symbol='EVT_DFLOW', name='Same Event',
            market_ticker='d', event_ticker='e',
        )
        s.register_price('polymarket', t1, Decimal('0.40'))
        s.register_price('dflow', t2, Decimal('0.50'))

        opps = s.find_arbitrage_opportunities()
        assert len(opps) >= 1
        assert opps[0]['spread'] != 0

    def test_no_opportunity_when_edge_too_small(self):
        s = CrossMarketArbitrageStrategy(min_edge=0.20)
        t1 = PolyMarketTicker(
            symbol='EVT_P', name='Event X',
            token_id='a', market_id='b', event_id='c',
        )
        t2 = SolanaTicker(
            symbol='EVT_D', name='Event X',
            market_ticker='d', event_ticker='e',
        )
        s.register_price('polymarket', t1, Decimal('0.50'))
        s.register_price('dflow', t2, Decimal('0.51'))
        assert s.find_arbitrage_opportunities() == []

    def test_detect_platform(self):
        s = CrossMarketArbitrageStrategy()
        poly = PolyMarketTicker(
            symbol='A', name='', token_id='a', market_id='b', event_id='c',
        )
        sol = SolanaTicker(symbol='B', name='', market_ticker='d', event_ticker='e')
        assert s._detect_platform(poly) == 'polymarket'
        assert s._detect_platform(sol) == 'dflow'

    @pytest.mark.asyncio
    async def test_process_event_no_crash(self):
        from oracle3.data.market_data_manager import MarketDataManager
        from oracle3.events.events import PriceChangeEvent
        from oracle3.position.position_manager import PositionManager
        from oracle3.risk.risk_manager import NoRiskManager
        from oracle3.trader.paper_trader import PaperTrader

        s = CrossMarketArbitrageStrategy(min_edge=0.03)
        t = SolanaTicker(
            symbol='TEST', name='Test', market_ticker='m', event_ticker='e',
        )
        event = PriceChangeEvent(ticker=t, price=Decimal('0.50'))

        md = MarketDataManager()
        pm = PositionManager()
        trader = PaperTrader(
            market_data=md, risk_manager=NoRiskManager(),
            position_manager=pm, min_fill_rate=Decimal('1'), max_fill_rate=Decimal('1'),
            commission_rate=Decimal('0'),
        )
        s.bind_context(event, trader)
        await s.process_event(event, trader)
