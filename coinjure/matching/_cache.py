"""Simple in-memory TTL cache."""

from __future__ import annotations

import time
from typing import TypeVar

T = TypeVar('T')


class TTLCache:
    """Thread-safe-ish TTL cache for API responses."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        """Return cached value if not expired, else None."""
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: object) -> None:
        """Store a value with the current timestamp."""
        self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        """Clear all entries."""
        self._store.clear()
