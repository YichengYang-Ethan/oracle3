from __future__ import annotations

from abc import ABC, abstractmethod

from oracle3.events.events import Event


class DataSource(ABC):
    @abstractmethod
    async def get_next_event(self) -> Event | None:
        """Retrieve next event. Returns ``None`` if finished (or on timeout)."""
        pass

    async def start(self) -> None:  # noqa: B027
        """Lifecycle hook — called once before the engine begins polling.

        Override in subclasses that need to launch background polling tasks
        (e.g. ``LivePolyMarketDataSource``).  The default implementation is
        a no-op so that simple / historical data sources need not override.
        """

    async def stop(self) -> None:  # noqa: B027
        """Lifecycle hook — called when the engine shuts down.

        Override to cancel background tasks or close connections.
        """
