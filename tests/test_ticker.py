import pytest

from oracle3.ticker.ticker import CashTicker, KalshiTicker, PolyMarketTicker, Ticker


class TestPolyMarketTicker:
    def test_creation_minimal(self):
        """Test creating a PolyMarketTicker with minimal parameters."""
        ticker = PolyMarketTicker(symbol='TEST')

        assert ticker.symbol == 'TEST'
        assert ticker.name == ''
        assert ticker.token_id == ''
        assert ticker.market_id == ''
        assert ticker.event_id == ''

    def test_creation_full(self):
        """Test creating a PolyMarketTicker with all parameters."""
        ticker = PolyMarketTicker(
            symbol='TEST_FULL',
            name='Test Full Ticker',
            token_id='token123',
            market_id='market456',
            event_id='event789',
        )

        assert ticker.symbol == 'TEST_FULL'
        assert ticker.name == 'Test Full Ticker'
        assert ticker.token_id == 'token123'
        assert ticker.market_id == 'market456'
        assert ticker.event_id == 'event789'

    def test_collateral(self):
        """Test that collateral returns POLYMARKET_USDC."""
        ticker = PolyMarketTicker(symbol='TEST')

        assert ticker.collateral == CashTicker.POLYMARKET_USDC

    def test_from_token_id(self):
        """Test creating ticker from token ID."""
        ticker = PolyMarketTicker.from_token_id('abc123', name='Test Ticker')

        assert ticker.symbol == 'abc123'
        assert ticker.token_id == 'abc123'
        assert ticker.name == 'Test Ticker'

    def test_from_token_id_no_name(self):
        """Test creating ticker from token ID without name."""
        ticker = PolyMarketTicker.from_token_id('abc123')

        assert ticker.symbol == 'abc123'
        assert ticker.token_id == 'abc123'
        assert ticker.name == ''

    def test_equality(self):
        """Test ticker equality."""
        ticker1 = PolyMarketTicker(symbol='TEST', name='Test', token_id='123')
        ticker2 = PolyMarketTicker(symbol='TEST', name='Test', token_id='123')

        assert ticker1 == ticker2

    def test_inequality_different_symbol(self):
        """Test ticker inequality with different symbols."""
        ticker1 = PolyMarketTicker(symbol='TEST1')
        ticker2 = PolyMarketTicker(symbol='TEST2')

        assert ticker1 != ticker2

    def test_frozen_immutable(self):
        """Test that ticker is immutable (frozen dataclass)."""
        ticker = PolyMarketTicker(symbol='TEST')

        with pytest.raises(AttributeError):
            ticker.symbol = 'CHANGED'

    def test_hashable(self):
        """Test that ticker is hashable (can be used in sets/dicts)."""
        ticker = PolyMarketTicker(symbol='TEST', token_id='123')

        # Should be usable as dict key
        d = {ticker: 'value'}
        assert d[ticker] == 'value'

        # Should be usable in sets
        s = {ticker}
        assert ticker in s


class TestCashTicker:
    def test_creation(self):
        """Test creating a CashTicker."""
        ticker = CashTicker(symbol='USD', name='US Dollar')

        assert ticker.symbol == 'USD'
        assert ticker.name == 'US Dollar'

    def test_collateral_raises(self):
        """Test that collateral property raises NotImplementedError."""
        ticker = CashTicker(symbol='USD', name='US Dollar')

        with pytest.raises(NotImplementedError):
            _ = ticker.collateral

    def test_polymarket_usdc_constant(self):
        """Test the POLYMARKET_USDC constant."""
        usdc = CashTicker.POLYMARKET_USDC

        assert usdc.symbol == 'PolyMarket_USDC'
        assert usdc.name == 'PolyMarket USDC'

    def test_equality(self):
        """Test cash ticker equality."""
        ticker1 = CashTicker(symbol='USD', name='US Dollar')
        ticker2 = CashTicker(symbol='USD', name='US Dollar')

        assert ticker1 == ticker2

    def test_frozen_immutable(self):
        """Test that cash ticker is immutable."""
        ticker = CashTicker(symbol='USD', name='US Dollar')

        with pytest.raises(AttributeError):
            ticker.symbol = 'CHANGED'

    def test_hashable(self):
        """Test that cash ticker is hashable."""
        ticker = CashTicker(symbol='USD', name='US Dollar')

        d = {ticker: 'value'}
        assert d[ticker] == 'value'


class TestKalshiTicker:
    def test_creation_defaults(self):
        ticker = KalshiTicker(symbol='MKT')
        assert ticker.symbol == 'MKT'
        assert ticker.is_no_side is False

    def test_collateral(self):
        ticker = KalshiTicker(symbol='MKT')
        assert ticker.collateral == CashTicker.KALSHI_USD

    def test_get_no_ticker(self):
        yes = KalshiTicker(symbol='MKT', name='Market', market_ticker='MKT-T1')
        no = yes.get_no_ticker()
        assert no is not None
        assert no.symbol == 'MKT_NO'
        assert no.is_no_side is True
        assert no.market_ticker == 'MKT-T1'
        assert no.name == 'Market'

    def test_get_no_ticker_returns_none_for_no_side(self):
        no = KalshiTicker(symbol='MKT_NO', is_no_side=True)
        assert no.get_no_ticker() is None

    def test_yes_no_not_equal(self):
        yes = KalshiTicker(symbol='MKT', market_ticker='MKT-T1')
        no = yes.get_no_ticker()
        assert yes != no

    def test_yes_no_different_hash(self):
        yes = KalshiTicker(symbol='MKT', market_ticker='MKT-T1')
        no = yes.get_no_ticker()
        assert hash(yes) != hash(no)
        # Both usable as dict keys simultaneously
        d = {yes: 'yes_val', no: 'no_val'}
        assert d[yes] == 'yes_val'
        assert d[no] == 'no_val'

    def test_is_ticker(self):
        ticker = KalshiTicker(symbol='MKT')
        assert isinstance(ticker, Ticker)

    def test_frozen(self):
        ticker = KalshiTicker(symbol='MKT')
        with pytest.raises(AttributeError):
            ticker.symbol = 'CHANGED'


class TestTickerInheritance:
    def test_polymarket_ticker_is_ticker(self):
        """Test that PolyMarketTicker is a Ticker."""
        ticker = PolyMarketTicker(symbol='TEST')
        assert isinstance(ticker, Ticker)

    def test_cash_ticker_is_ticker(self):
        """Test that CashTicker is a Ticker."""
        ticker = CashTicker(symbol='USD', name='US Dollar')
        assert isinstance(ticker, Ticker)
