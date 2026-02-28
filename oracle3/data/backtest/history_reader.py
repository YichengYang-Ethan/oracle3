from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _iter_rows_from_payload(payload: Any) -> Iterator[dict[str, Any]]:
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                yield row
        return

    if not isinstance(payload, dict):
        return

    for key in ('rows', 'data', 'markets', 'items'):
        nested = payload.get(key)
        if isinstance(nested, list):
            for row in nested:
                if isinstance(row, dict):
                    yield row
            return

    if 'event_id' in payload and 'market_id' in payload:
        yield payload


def iter_history_rows(history_file: str) -> Iterator[dict[str, Any]]:
    """Iterate history rows from JSONL or JSON payloads.

    Supported formats:
    - JSONL: one row per line
    - JSON array: [{...}, {...}]
    - JSON object wrapping rows: {"rows": [...]}, {"data": [...]}, etc.
    - Single JSON row object: {"event_id": "...", "market_id": "...", ...}
    """
    path = Path(history_file).expanduser().resolve()

    jsonl_rows = 0
    with path.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                jsonl_rows += 1
                yield row

    if jsonl_rows > 0:
        return

    raw = path.read_text(encoding='utf-8').strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return
    yield from _iter_rows_from_payload(payload)
