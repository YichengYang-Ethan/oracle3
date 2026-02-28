"""Tests for SolanaTicker."""

from oracle3.ticker.ticker import CashTicker, SolanaTicker


def test_solana_ticker_creation():
    t = SolanaTicker(
        symbol='TEST_MKT',
        name='Test Market',
        yes_mint='mint_yes_123',
        no_mint='mint_no_456',
        market_ticker='TEST_MKT',
        event_ticker='EVT_TEST',
        series_ticker='SER_TEST',
    )
    assert t.symbol == 'TEST_MKT'
    assert t.name == 'Test Market'
    assert t.yes_mint == 'mint_yes_123'
    assert t.is_no_side is False


def test_solana_ticker_collateral():
    t = SolanaTicker(symbol='X', market_ticker='X')
    assert t.collateral == CashTicker.DFLOW_USDC


def test_solana_ticker_get_no_ticker():
    t = SolanaTicker(
        symbol='MKT',
        name='My Market',
        yes_mint='yes_mint',
        no_mint='no_mint',
        market_ticker='MKT',
        event_ticker='EVT',
        series_ticker='SER',
    )
    no = t.get_no_ticker()
    assert no is not None
    assert no.symbol == 'MKT_NO'
    assert no.is_no_side is True
    assert no.yes_mint == 'no_mint'
    assert no.no_mint == 'yes_mint'
    assert no.get_no_ticker() is None


def test_solana_ticker_frozen():
    t = SolanaTicker(symbol='X', market_ticker='X')
    try:
        t.symbol = 'Y'
        raise AssertionError('Should be frozen')
    except AttributeError:
        pass


def test_dflow_usdc_exists():
    assert CashTicker.DFLOW_USDC.symbol == 'DFlow_USDC'
    assert CashTicker.DFLOW_USDC.name == 'DFlow USDC'
