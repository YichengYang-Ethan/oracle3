from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import OrderBookEvent, PriceChangeEvent
from oracle3.order.order_book import Level, OrderBook
from oracle3.ticker.ticker import PolyMarketTicker


@pytest.fixture
def market_data() -> MarketDataManager:
    """Create a fresh market data manager."""
    return MarketDataManager()


@pytest.fixture
def test_ticker() -> PolyMarketTicker:
    """Create a test ticker."""
    return PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
        market_id='market123',
        event_id='event123',
    )


class TestMarketDataManager:
    def test_empty_market_data(self, market_data: MarketDataManager):
        """Test empty market data manager."""
        assert market_data.order_books == {}

    def test_custom_spread(self, test_ticker: PolyMarketTicker):
        """Test that custom spread is applied to synthetic order book."""
        mdm = MarketDataManager(spread=Decimal('0.02'))
        event = PriceChangeEvent(ticker=test_ticker, price=Decimal('0.50'))
        mdm.process_price_change_event(event)
        ob = mdm.order_books[test_ticker]
        assert ob.best_bid.price == Decimal('0.49')  # 0.50 - 0.01
        assert ob.best_ask.price == Decimal('0.51')  # 0.50 + 0.01

    def test_custom_synthetic_size(self, test_ticker: PolyMarketTicker):
        """Test that custom synthetic size is applied."""
        mdm = MarketDataManager(synthetic_size=Decimal('500'))
        event = PriceChangeEvent(ticker=test_ticker, price=Decimal('0.50'))
        mdm.process_price_change_event(event)
        ob = mdm.order_books[test_ticker]
        assert ob.best_bid.size == Decimal('500')
        assert ob.best_ask.size == Decimal('500')

    def test_update_order_book(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test updating order book directly."""
        order_book = OrderBook()
        order_book.update(
            asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
            bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
        )

        market_data.update_order_book(test_ticker, order_book)

        assert test_ticker in market_data.order_books
        assert market_data.order_books[test_ticker].best_bid.price == Decimal('0.50')

    def test_process_orderbook_event(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test processing order book event."""
        event = OrderBookEvent(
            ticker=test_ticker,
            price=Decimal('0.50'),
            size=Decimal('1000'),
            size_delta=Decimal('1000'),
        )

        market_data.process_orderbook_event(event)

        assert test_ticker in market_data.order_books

    def test_process_price_change_event(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test processing price change event creates synthetic order book."""
        event = PriceChangeEvent(
            ticker=test_ticker,
            price=Decimal('0.50'),
        )

        market_data.process_price_change_event(event)

        assert test_ticker in market_data.order_books
        ob = market_data.order_books[test_ticker]

        # Synthetic spread of 0.01 around price
        assert ob.best_bid is not None
        assert ob.best_ask is not None
        assert ob.best_bid.price == Decimal('0.495')  # 0.50 - 0.005
        assert ob.best_ask.price == Decimal('0.505')  # 0.50 + 0.005

    def test_process_price_change_at_boundaries(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test price change at market boundaries."""
        # Price near 0
        event = PriceChangeEvent(ticker=test_ticker, price=Decimal('0.01'))
        market_data.process_price_change_event(event)

        ob = market_data.order_books[test_ticker]
        # Best bid should exist and be >= 0
        if ob.best_bid:
            assert ob.best_bid.price >= Decimal('0')

        # Price near 1
        event2 = PriceChangeEvent(ticker=test_ticker, price=Decimal('0.99'))
        market_data.process_price_change_event(event2)

        ob2 = market_data.order_books[test_ticker]
        # Ask should be capped at less than 1
        if ob2.best_ask:
            assert ob2.best_ask.price <= Decimal('1')

    def test_get_bids(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test getting bids."""
        order_book = OrderBook()
        order_book.update(
            asks=[],
            bids=[
                Level(price=Decimal('0.50'), size=Decimal('1000')),
                Level(price=Decimal('0.49'), size=Decimal('500')),
            ],
        )
        market_data.update_order_book(test_ticker, order_book)

        bids = market_data.get_bids(test_ticker)
        assert len(bids) == 2
        assert bids[0].price == Decimal('0.50')

    def test_get_bids_with_depth(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test getting bids with depth limit."""
        order_book = OrderBook()
        order_book.update(
            asks=[],
            bids=[
                Level(price=Decimal('0.50'), size=Decimal('1000')),
                Level(price=Decimal('0.49'), size=Decimal('500')),
                Level(price=Decimal('0.48'), size=Decimal('300')),
            ],
        )
        market_data.update_order_book(test_ticker, order_book)

        bids = market_data.get_bids(test_ticker, depth=2)
        assert len(bids) == 2

    def test_get_bids_no_order_book(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test getting bids when no order book exists."""
        bids = market_data.get_bids(test_ticker)
        assert bids == []

    def test_get_asks(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test getting asks."""
        order_book = OrderBook()
        order_book.update(
            asks=[
                Level(price=Decimal('0.55'), size=Decimal('1000')),
                Level(price=Decimal('0.56'), size=Decimal('500')),
            ],
            bids=[],
        )
        market_data.update_order_book(test_ticker, order_book)

        asks = market_data.get_asks(test_ticker)
        assert len(asks) == 2
        assert asks[0].price == Decimal('0.55')

    def test_get_asks_no_order_book(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test getting asks when no order book exists."""
        asks = market_data.get_asks(test_ticker)
        assert asks == []

    def test_get_best_bid(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test getting best bid."""
        order_book = OrderBook()
        order_book.update(
            asks=[],
            bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
        )
        market_data.update_order_book(test_ticker, order_book)

        best = market_data.get_best_bid(test_ticker)
        assert best.price == Decimal('0.50')

    def test_get_best_ask(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Test getting best ask."""
        order_book = OrderBook()
        order_book.update(
            asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
            bids=[],
        )
        market_data.update_order_book(test_ticker, order_book)

        best = market_data.get_best_ask(test_ticker)
        assert best.price == Decimal('0.55')

    def test_market_history_tracks_visible_snapshots(
        self, market_data: MarketDataManager, test_ticker: PolyMarketTicker
    ):
        """Strategies should be able to inspect the cumulative replay timeline."""
        market_data.process_price_change_event(
            PriceChangeEvent(
                ticker=test_ticker,
                price=Decimal('0.50'),
                timestamp='t0',
            )
        )
        market_data.process_orderbook_event(
            OrderBookEvent(
                ticker=test_ticker,
                price=Decimal('0.503'),
                size=Decimal('250'),
                size_delta=Decimal('50'),
                side='bid',
            )
        )

        history = market_data.get_market_history(test_ticker)

        assert [point.event_type for point in history] == [
            'price_change',
            'order_book',
        ]
        assert history[0].timestamp == 't0'
        assert history[0].event_price == Decimal('0.50')
        assert history[0].best_bid == Decimal('0.495')
        assert history[0].best_ask == Decimal('0.505')
        assert history[1].event_side == 'bid'
        assert history[1].event_size == Decimal('250')
        assert history[1].best_bid == Decimal('0.503')
        assert (
            market_data.get_market_history(limit=1)[0].sequence == history[1].sequence
        )
        assert market_data.get_price_history(test_ticker) == [
            Decimal('0.50'),
            Decimal('0.504'),
        ]


class TestNoSideBootstrap:
    """Tests for No-side orderbook bootstrap from PriceChangeEvent."""

    def test_no_orderbook_bootstrapped_on_first_yes_event(
        self, market_data: MarketDataManager
    ):
        yes = PolyMarketTicker(
            symbol='YES',
            name='T',
            token_id='YES',
            no_token_id='NO',
            market_id='M',
            event_id='E',
        )
        no = yes.get_no_ticker()
        event = PriceChangeEvent(ticker=yes, price=Decimal('0.60'), timestamp='t0')
        market_data.process_price_change_event(event)

        # No orderbook should exist with complement prices
        no_bid = market_data.get_best_bid(no)
        no_ask = market_data.get_best_ask(no)
        assert no_bid is not None
        assert no_ask is not None
        assert no_bid.price == Decimal('0.395')  # 1 - 0.60 - 0.005
        assert no_ask.price == Decimal('0.405')  # 1 - 0.60 + 0.005

    def test_no_orderbook_not_overwritten_by_second_yes_event(
        self, market_data: MarketDataManager
    ):
        yes = PolyMarketTicker(
            symbol='YES',
            name='T',
            token_id='YES',
            no_token_id='NO',
            market_id='M',
            event_id='E',
        )
        no = yes.get_no_ticker()

        # First Yes event bootstraps No OB
        market_data.process_price_change_event(
            PriceChangeEvent(ticker=yes, price=Decimal('0.60'), timestamp='t0')
        )
        # Directly update No OB (simulating a No PriceChangeEvent)
        market_data.process_price_change_event(
            PriceChangeEvent(ticker=no, price=Decimal('0.30'), timestamp='t1')
        )
        # Second Yes event should NOT overwrite No OB
        market_data.process_price_change_event(
            PriceChangeEvent(ticker=yes, price=Decimal('0.80'), timestamp='t2')
        )

        no_bid = market_data.get_best_bid(no)
        # Should still reflect the direct No event (0.30), not Yes-derived (0.20)
        assert no_bid.price == Decimal('0.295')

    def test_no_bootstrap_skipped_without_no_token(
        self, market_data: MarketDataManager
    ):
        ticker = PolyMarketTicker(symbol='SOLO', name='Solo')
        event = PriceChangeEvent(ticker=ticker, price=Decimal('0.50'), timestamp='t0')
        market_data.process_price_change_event(event)

        # Only the Yes orderbook should exist
        assert len(market_data.order_books) == 1
