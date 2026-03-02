"""Tests for DFlowDataSource."""

import json

import pytest

from oracle3.data.live.dflow_data_source import DFlowDataSource
from oracle3.events.events import OrderBookEvent
from oracle3.ticker.ticker import SolanaTicker


@pytest.fixture
def data_source(tmp_path):
    cache = str(tmp_path / 'test_cache.jsonl')
    return DFlowDataSource(
        polling_interval=1.0,
        event_cache_file=cache,
        reprocess_on_start=True,
    )


def test_init(data_source):
    assert data_source.polling_interval == 1.0
    assert len(data_source.processed_event_tickers) == 0


def test_market_to_order_book_events(data_source):
    market = {
        'ticker': 'TEST_MKT',
        'title': 'Will X happen?',
        'yesBid': 0.45,
        'yesAsk': 0.55,
        '_event_ticker': 'EVT1',
        '_series_ticker': 'SER1',
    }
    events = data_source._market_to_order_book_events(market)
    assert len(events) == 2
    assert all(isinstance(e, OrderBookEvent) for e in events)
    assert isinstance(events[0].ticker, SolanaTicker)
    assert events[0].ticker.symbol == 'TEST_MKT'


def test_market_to_order_book_no_price_change(data_source):
    market = {
        'ticker': 'TEST_MKT',
        'yesBid': 0.50,
        'yesAsk': 0.60,
        '_event_ticker': 'EVT1',
        '_series_ticker': 'SER1',
    }
    # First call populates
    events1 = data_source._market_to_order_book_events(market)
    assert len(events1) == 2
    # Same prices → still emits events but with size_delta=0
    events2 = data_source._market_to_order_book_events(market)
    assert len(events2) == 2
    assert all(e.size_delta == 0 for e in events2)


def test_cache_loading(tmp_path):
    cache = tmp_path / 'cache.jsonl'
    cache.write_text(json.dumps({'event_ticker': 'EVT_CACHED'}) + '\n')
    ds = DFlowDataSource(
        event_cache_file=str(cache),
        reprocess_on_start=False,
    )
    assert 'EVT_CACHED' in ds.processed_event_tickers
    assert 'EVT_CACHED' in ds._news_fetched_events


@pytest.mark.asyncio
async def test_start_stop(data_source):
    await data_source.start()
    assert data_source._poll_task is not None
    await data_source.stop()
    assert data_source._poll_task.done()


@pytest.mark.asyncio
async def test_get_next_event_timeout(data_source):
    result = await data_source.get_next_event()
    assert result is None
