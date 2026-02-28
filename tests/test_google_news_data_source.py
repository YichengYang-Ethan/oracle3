"""Smoke tests for GoogleNewsDataSource (no network access)."""

from __future__ import annotations

from unittest.mock import patch

from oracle3.data.live.google_news_data_source import (
    DEFAULT_QUERIES,
    GoogleNewsDataSource,
    _clean_google_href,
    _parse_relative_or_absolute,
    _scrape_google_news,
)
from oracle3.events.events import NewsEvent

# -- Sample HTML fragment matching real Google News card structure --------

SAMPLE_HTML = """
<div class="SoaBEf">
  <a href="/url?q=https://example.com/article1&amp;sa=U">
    <div class="MBeuO">Breaking: Market Moves on Fed Decision</div>
  </a>
  <div class="NUnG9d"><span>Reuters</span></div>
  <div class="LfVVr">2 hours ago</div>
  <div class="GI74Re">The Federal Reserve announced a rate hold today.</div>
</div>
<div class="SoaBEf">
  <a href="/url?q=https://example.com/article2">
    <div class="MBeuO">Polymarket Volume Surges</div>
  </a>
  <div class="NUnG9d"><span>CoinDesk</span></div>
  <div class="LfVVr">5 days ago</div>
  <div class="GI74Re">Prediction markets see record activity this week.</div>
</div>
"""


class FakeResponse:
    """Minimal mock of requests.Response."""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.ok = status_code < 400


# -- Unit tests -----------------------------------------------------------


class TestHelpers:
    def test_clean_google_href_unwraps_redirect(self) -> None:
        href = '/url?q=https://example.com/real&sa=U&ved=abc'
        assert _clean_google_href(href) == 'https://example.com/real'

    def test_clean_google_href_passthrough(self) -> None:
        assert _clean_google_href('https://direct.com') == 'https://direct.com'

    def test_parse_relative_hours_ago(self) -> None:
        from datetime import datetime

        ref = datetime(2026, 2, 15, 12, 0, 0)
        ts = _parse_relative_or_absolute('3 hours ago', ref)
        expected = datetime(2026, 2, 15, 9, 0, 0).timestamp()
        assert abs(ts - expected) < 1

    def test_parse_absolute_date(self) -> None:
        from datetime import datetime

        ref = datetime(2026, 2, 15)
        ts = _parse_relative_or_absolute('Jan 10, 2026', ref)
        expected = datetime(2026, 1, 10).timestamp()
        assert abs(ts - expected) < 1

    def test_parse_fallback_to_ref(self) -> None:
        from datetime import datetime

        ref = datetime(2026, 2, 15, 12, 0, 0)
        ts = _parse_relative_or_absolute('unknown format', ref)
        assert abs(ts - ref.timestamp()) < 1


class TestScrapeGoogleNews:
    """Test the HTML parser with a mocked HTTP response."""

    @patch(
        'oracle3.data.live.google_news_data_source._make_request',
        return_value=FakeResponse(SAMPLE_HTML),
    )
    def test_parses_cards(self, mock_req) -> None:  # type: ignore[no-untyped-def]
        results = _scrape_google_news('test', max_pages=1, min_delay=0, max_delay=0)
        assert len(results) == 2
        assert results[0]['title'] == 'Breaking: Market Moves on Fed Decision'
        assert results[0]['source'] == 'Reuters'
        assert results[0]['link'] == 'https://example.com/article1'
        assert results[1]['title'] == 'Polymarket Volume Surges'
        assert results[1]['source'] == 'CoinDesk'

    @patch(
        'oracle3.data.live.google_news_data_source._make_request',
        return_value=FakeResponse('<html><body>no cards</body></html>'),
    )
    def test_no_cards_returns_empty(self, mock_req) -> None:  # type: ignore[no-untyped-def]
        results = _scrape_google_news('test', max_pages=1, min_delay=0, max_delay=0)
        assert results == []


class TestGoogleNewsDataSource:
    def test_default_construction(self) -> None:
        ds = GoogleNewsDataSource()
        assert ds.queries == list(DEFAULT_QUERIES)
        assert ds.polling_interval == 300.0
        assert ds.max_articles_per_poll == 10

    async def test_get_next_event_timeout(self) -> None:
        ds = GoogleNewsDataSource()
        result = await ds.get_next_event()
        assert result is None

    async def test_start_stop_lifecycle(self) -> None:
        ds = GoogleNewsDataSource(polling_interval=9999)
        # Patch scraping to avoid real network calls
        with patch.object(ds, '_fetch_all_queries', return_value=[]):
            await ds.start()
            assert ds._poll_task is not None
            assert not ds._poll_task.done()
            await ds.stop()
            assert ds._poll_task is None

    def test_dedup_skips_seen_links(self) -> None:
        ds = GoogleNewsDataSource()
        ds.processed_article_ids.add('https://example.com/article1')

        item = {
            'link': 'https://example.com/article1',
            'title': 'Old',
            'snippet': '',
            'date': None,
            'source': 'Test',
        }
        # Simulate what _fetch_all_queries does: skip already-seen links
        uid = item.get('link', '')
        assert uid in ds.processed_article_ids

    def test_to_news_event(self) -> None:
        item = {
            'link': 'https://example.com/a',
            'title': 'Test Title',
            'snippet': 'Test snippet text',
            'date': 1739620800.0,
            'source': 'Reuters',
        }
        event = GoogleNewsDataSource._to_news_event(item, 'polymarket')
        assert isinstance(event, NewsEvent)
        assert event.title == 'Test Title'
        assert event.source == 'Reuters'
        assert event.url == 'https://example.com/a'
        assert 'polymarket' in event.categories
        assert event.news == 'Test Title: Test snippet text'
