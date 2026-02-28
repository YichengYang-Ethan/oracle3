from datetime import datetime
from decimal import Decimal

import pytest

from oracle3.events.events import NewsEvent, OrderBookEvent, PriceChangeEvent
from oracle3.ticker.ticker import PolyMarketTicker


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


class TestOrderBookEvent:
    def test_creation(self, test_ticker: PolyMarketTicker):
        """Test creating an OrderBookEvent."""
        event = OrderBookEvent(
            ticker=test_ticker,
            price=Decimal('0.50'),
            size=Decimal('1000'),
            size_delta=Decimal('500'),
        )

        assert event.ticker == test_ticker
        assert event.price == Decimal('0.50')
        assert event.size == Decimal('1000')
        assert event.size_delta == Decimal('500')

    def test_trigger(self, test_ticker: PolyMarketTicker):
        """Test that trigger method exists and runs."""
        event = OrderBookEvent(
            ticker=test_ticker,
            price=Decimal('0.50'),
            size=Decimal('1000'),
            size_delta=Decimal('500'),
        )
        # Should not raise
        event.trigger()

    def test_str(self, test_ticker: PolyMarketTicker):
        """Test string representation."""
        event = OrderBookEvent(
            ticker=test_ticker,
            price=Decimal('0.50'),
            size=Decimal('1000'),
            size_delta=Decimal('500'),
        )

        s = str(event)
        assert 'OrderBookEvent' in s
        assert 'TEST_TOKEN' in s
        assert '0.50' in s

    def test_repr(self, test_ticker: PolyMarketTicker):
        """Test repr."""
        event = OrderBookEvent(
            ticker=test_ticker,
            price=Decimal('0.50'),
            size=Decimal('1000'),
            size_delta=Decimal('500'),
        )

        r = repr(event)
        assert 'OrderBookEvent' in r


class TestNewsEvent:
    def test_creation_minimal(self):
        """Test creating a NewsEvent with minimal parameters."""
        event = NewsEvent(news='Test news content')

        assert event.news == 'Test news content'
        assert event.title == ''
        assert event.source == ''
        assert event.url == ''
        assert event.categories == []
        assert event.ticker is None

    def test_creation_full(self, test_ticker: PolyMarketTicker):
        """Test creating a NewsEvent with all parameters."""
        published = datetime(2024, 1, 15, 12, 0, 0)
        event = NewsEvent(
            news='Breaking news content',
            title='Breaking News',
            source='Reuters',
            url='https://example.com/news',
            published_at=published,
            categories=['finance', 'crypto'],
            description='Detailed description',
            image_url='https://example.com/image.jpg',
            uuid='abc123',
            event_id='event123',
            ticker=test_ticker,
        )

        assert event.news == 'Breaking news content'
        assert event.title == 'Breaking News'
        assert event.source == 'Reuters'
        assert event.url == 'https://example.com/news'
        assert event.published_at == published
        assert event.categories == ['finance', 'crypto']
        assert event.description == 'Detailed description'
        assert event.image_url == 'https://example.com/image.jpg'
        assert event.uuid == 'abc123'
        assert event.event_id == 'event123'
        assert event.ticker == test_ticker

    def test_trigger(self):
        """Test that trigger method exists and runs."""
        event = NewsEvent(news='Test news')
        # Should not raise
        event.trigger()

    def test_str(self, test_ticker: PolyMarketTicker):
        """Test string representation."""
        event = NewsEvent(
            news='This is a test news article about financial markets and cryptocurrency',
            title='Test Title',
            source='Test Source',
            ticker=test_ticker,
        )

        s = str(event)
        assert 'NewsEvent' in s
        assert 'Test Title' in s
        assert 'Test Source' in s
        assert 'TEST_TOKEN' in s

    def test_str_truncates_long_content(self):
        """Test that long content is truncated in string representation."""
        long_content = 'A' * 200
        event = NewsEvent(news=long_content, title='Long News')

        s = str(event)
        assert '...' in s

    def test_str_no_ticker(self):
        """Test string representation without ticker."""
        event = NewsEvent(news='Test news')

        s = str(event)
        assert 'None' in s

    def test_repr(self):
        """Test repr."""
        event = NewsEvent(news='Test news', title='Test')

        r = repr(event)
        assert 'NewsEvent' in r

    def test_default_published_at(self):
        """Test that published_at defaults to now."""
        before = datetime.now()
        event = NewsEvent(news='Test news')
        after = datetime.now()

        assert before <= event.published_at <= after


class TestPriceChangeEvent:
    def test_creation(self, test_ticker: PolyMarketTicker):
        """Test creating a PriceChangeEvent."""
        timestamp = datetime(2024, 1, 15, 12, 0, 0)
        event = PriceChangeEvent(
            ticker=test_ticker,
            price=Decimal('0.65'),
            timestamp=timestamp,
        )

        assert event.ticker == test_ticker
        assert event.price == Decimal('0.65')
        assert event.timestamp == timestamp

    def test_creation_default_timestamp(self, test_ticker: PolyMarketTicker):
        """Test that timestamp defaults to now."""
        before = datetime.now()
        event = PriceChangeEvent(
            ticker=test_ticker,
            price=Decimal('0.65'),
        )
        after = datetime.now()

        assert before <= event.timestamp <= after

    def test_trigger(self, test_ticker: PolyMarketTicker):
        """Test that trigger method exists and runs."""
        event = PriceChangeEvent(
            ticker=test_ticker,
            price=Decimal('0.65'),
        )
        # Should not raise
        event.trigger()

    def test_str(self, test_ticker: PolyMarketTicker):
        """Test string representation."""
        event = PriceChangeEvent(
            ticker=test_ticker,
            price=Decimal('0.65'),
        )

        s = str(event)
        assert 'PriceChangeEvent' in s
        assert 'TEST_TOKEN' in s
        assert '0.65' in s

    def test_repr(self, test_ticker: PolyMarketTicker):
        """Test repr."""
        event = PriceChangeEvent(
            ticker=test_ticker,
            price=Decimal('0.65'),
        )

        r = repr(event)
        assert 'PriceChangeEvent' in r
