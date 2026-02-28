from __future__ import annotations

import json
from pathlib import Path

from oracle3.data.backtest.historical_data_source import HistoricalDataSource
from oracle3.ticker.ticker import PolyMarketTicker


def _ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='YES',
        name='Test Market',
        token_id='YES',
        no_token_id='NO',
        market_id='M1',
        event_id='E1',
    )


def test_historical_data_source_supports_json_array(tmp_path: Path) -> None:
    history_file = tmp_path / 'history.json'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {'Yes': [{'t': 2, 'p': 0.45}, {'t': 1, 'p': 0.40}]},
        },
        {
            'event_id': 'E2',
            'market_id': 'M2',
            'time_series': {'Yes': [{'t': 1, 'p': 0.90}]},
        },
    ]
    history_file.write_text(json.dumps(rows), encoding='utf-8')

    source = HistoricalDataSource(str(history_file), _ticker())
    assert len(source.events) == 4
    assert source.events[0].timestamp == 1
    assert source.events[-1].timestamp == 2


def test_historical_data_source_sorts_iso_timestamps(tmp_path: Path) -> None:
    history_file = tmp_path / 'history.jsonl'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {
                'Yes': [
                    {'t': '2025-01-02T00:00:00+00:00', 'p': 0.60},
                    {'t': '2025-01-01T00:00:00+00:00', 'p': 0.55},
                ]
            },
        }
    ]
    history_file.write_text(
        '\n'.join(json.dumps(row) for row in rows) + '\n',
        encoding='utf-8',
    )

    source = HistoricalDataSource(str(history_file), _ticker())
    assert len(source.events) == 4
    assert source.events[0].timestamp == '2025-01-01T00:00:00+00:00'
    assert source.events[-1].timestamp == '2025-01-02T00:00:00+00:00'


def test_historical_data_source_can_include_other_markets(tmp_path: Path) -> None:
    history_file = tmp_path / 'history.json'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'question': 'Primary market',
            'time_series': {'Yes': [{'t': 1, 'p': 0.40}]},
        },
        {
            'event_id': 'E1',
            'market_id': 'M2',
            'question': 'Linked market',
            'time_series': {'Yes': [{'t': 2, 'p': 0.75}]},
        },
    ]
    history_file.write_text(json.dumps(rows), encoding='utf-8')

    source = HistoricalDataSource(
        str(history_file),
        _ticker(),
        include_all_markets=True,
    )

    symbols = [event.ticker.symbol for event in source.events]

    assert len(source.events) == 4
    assert 'YES' in symbols
    assert 'NO' in symbols
    assert 'BT_M2' in symbols
    assert 'BT_M2_NO' in symbols


def test_historical_data_source_can_drain_same_timestamp_batch(tmp_path: Path) -> None:
    history_file = tmp_path / 'history.json'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {'Yes': [{'t': 1, 'p': 0.40}, {'t': 2, 'p': 0.45}]},
        },
        {
            'event_id': 'E1',
            'market_id': 'M2',
            'time_series': {'Yes': [{'t': 1, 'p': 0.75}]},
        },
    ]
    history_file.write_text(json.dumps(rows), encoding='utf-8')

    source = HistoricalDataSource(
        str(history_file),
        _ticker(),
        include_all_markets=True,
    )

    first_event = source.events[0]
    source.index = 1
    batch = source.drain_same_timestamp_events(first_event)

    assert [event.ticker.symbol for event in batch] == ['NO', 'BT_M2', 'BT_M2_NO']
    assert source.index == 4
