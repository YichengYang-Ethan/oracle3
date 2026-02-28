"""Composite data source that merges events from multiple DataSources."""

from __future__ import annotations

import asyncio
import logging

from oracle3.data.data_source import DataSource
from oracle3.events.events import Event

logger = logging.getLogger(__name__)


class CompositeDataSource(DataSource):
    """Merges events from multiple DataSource instances into a single stream.

    Each child data source runs its own polling loop. Events from all sources
    are funneled into a shared queue and delivered to the engine in arrival order.
    """

    def __init__(self, sources: list[DataSource]) -> None:
        self.sources = sources
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=2000)
        self._relay_tasks: list[asyncio.Task] = []

    async def _relay(self, source: DataSource) -> None:
        """Continuously relay events from one child source into the shared queue."""
        while True:
            try:
                event = await source.get_next_event()
                if event is not None:
                    await self._queue.put(event)
                else:
                    # Most child sources already sleep internally (wait_for 1s).
                    # This guard prevents CPU-spin if a source returns None instantly.
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    'Relay error from %s', type(source).__name__, exc_info=True
                )
                await asyncio.sleep(1.0)

    async def start(self) -> None:
        for source in self.sources:
            await source.start()
        for source in self.sources:
            task = asyncio.create_task(self._relay(source))
            self._relay_tasks.append(task)

    async def stop(self) -> None:
        for task in self._relay_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._relay_tasks.clear()
        for source in self.sources:
            try:
                await source.stop()
            except Exception:
                pass

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    def watch_token(self, token_id: str) -> None:
        for source in self.sources:
            watch = getattr(source, 'watch_token', None)
            if watch:
                watch(token_id)

    def unwatch_token(self, token_id: str) -> None:
        for source in self.sources:
            unwatch = getattr(source, 'unwatch_token', None)
            if unwatch:
                unwatch(token_id)
