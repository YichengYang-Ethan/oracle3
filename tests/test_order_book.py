from decimal import Decimal

from oracle3.order.order_book import Level, OrderBook


class TestLevel:
    def test_level_creation(self):
        """Test creating a Level."""
        level = Level(price=Decimal('0.50'), size=Decimal('1000'))
        assert level.price == Decimal('0.50')
        assert level.size == Decimal('1000')

    def test_level_str(self):
        """Test Level string representation."""
        level = Level(price=Decimal('0.50'), size=Decimal('1000'))
        assert str(level) == '0.50@1000'

    def test_level_repr(self):
        """Test Level repr."""
        level = Level(price=Decimal('0.50'), size=Decimal('1000'))
        assert "Level(price=Decimal('0.50')" in repr(level)


class TestOrderBook:
    def test_empty_order_book(self):
        """Test empty order book."""
        ob = OrderBook()
        assert ob.asks == []
        assert ob.bids == []
        assert ob.best_ask is None
        assert ob.best_bid is None

    def test_update_order_book(self):
        """Test updating order book."""
        ob = OrderBook()

        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
        ]
        bids = [
            Level(price=Decimal('0.50'), size=Decimal('1000')),
            Level(price=Decimal('0.49'), size=Decimal('800')),
        ]

        ob.update(asks=asks, bids=bids)

        assert len(ob.asks) == 2
        assert len(ob.bids) == 2

    def test_best_ask(self):
        """Test getting best ask."""
        ob = OrderBook()
        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
        ]
        ob.update(asks=asks, bids=[])

        best = ob.best_ask
        assert best is not None
        assert best.price == Decimal('0.55')
        assert best.size == Decimal('500')

    def test_best_bid(self):
        """Test getting best bid."""
        ob = OrderBook()
        bids = [
            Level(price=Decimal('0.50'), size=Decimal('1000')),
            Level(price=Decimal('0.49'), size=Decimal('800')),
        ]
        ob.update(asks=[], bids=bids)

        best = ob.best_bid
        assert best is not None
        assert best.price == Decimal('0.50')
        assert best.size == Decimal('1000')

    def test_get_asks_with_depth(self):
        """Test getting asks with depth limit."""
        ob = OrderBook()
        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
            Level(price=Decimal('0.57'), size=Decimal('200')),
        ]
        ob.update(asks=asks, bids=[])

        top_2 = ob.get_asks(depth=2)
        assert len(top_2) == 2
        assert top_2[0].price == Decimal('0.55')
        assert top_2[1].price == Decimal('0.56')

    def test_get_bids_with_depth(self):
        """Test getting bids with depth limit."""
        ob = OrderBook()
        bids = [
            Level(price=Decimal('0.50'), size=Decimal('1000')),
            Level(price=Decimal('0.49'), size=Decimal('800')),
            Level(price=Decimal('0.48'), size=Decimal('600')),
        ]
        ob.update(asks=[], bids=bids)

        top_2 = ob.get_bids(depth=2)
        assert len(top_2) == 2
        assert top_2[0].price == Decimal('0.50')
        assert top_2[1].price == Decimal('0.49')

    def test_get_asks_no_depth(self):
        """Test getting all asks without depth limit."""
        ob = OrderBook()
        asks = [
            Level(price=Decimal('0.55'), size=Decimal('500')),
            Level(price=Decimal('0.56'), size=Decimal('300')),
        ]
        ob.update(asks=asks, bids=[])

        all_asks = ob.get_asks()
        assert len(all_asks) == 2

    def test_order_book_str(self):
        """Test order book string representation."""
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000'))]
        ob.update(asks=asks, bids=bids)

        s = str(ob)
        assert 'best_bid' in s
        assert 'best_ask' in s

    def test_order_book_repr(self):
        """Test order book repr."""
        ob = OrderBook()
        asks = [Level(price=Decimal('0.55'), size=Decimal('500'))]
        bids = [Level(price=Decimal('0.50'), size=Decimal('1000'))]
        ob.update(asks=asks, bids=bids)

        r = repr(ob)
        assert 'OrderBook' in r
        assert 'asks=' in r
        assert 'bids=' in r
