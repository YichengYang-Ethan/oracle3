import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from oracle3.events.events import Event, PriceChangeEvent
from oracle3.ticker.ticker import PolyMarketTicker

from ..data_source import DataSource
from .history_reader import iter_history_rows

logger = logging.getLogger(__name__)


class HistoricalDataSource(DataSource):
    def __init__(
        self,
        history_file: str,
        ticker: PolyMarketTicker,
        *,
        include_all_markets: bool = False,
    ):
        self.history_file = history_file
        self.ticker = ticker
        self.include_all_markets = include_all_markets
        self._tickers_by_market: dict[tuple[str, str], PolyMarketTicker] = {
            (str(ticker.event_id), str(ticker.market_id)): ticker
        }
        self.events = self._load_events()
        self.index = 0

    def _load_events(self) -> list[Event]:
        events: list[Event] = []

        try:
            for data in iter_history_rows(self.history_file):
                market_id = str(data.get('market_id', ''))
                event_id = str(data.get('event_id', ''))
                is_primary_market = event_id == str(
                    self.ticker.event_id
                ) and market_id == str(self.ticker.market_id)
                if not is_primary_market and not self.include_all_markets:
                    continue

                row_ticker = self._ticker_for_row(data)
                no_ticker = row_ticker.get_no_ticker()
                ts = data.get('time_series')
                ts_yes = ts.get('Yes') if isinstance(ts, dict) else None
                if ts_yes:
                    for entry in ts_yes:
                        if not isinstance(entry, dict):
                            continue
                        timestamp = entry.get('t')
                        price = entry.get('p')
                        if price is None:
                            continue
                        yes_price = Decimal(str(price))
                        event = PriceChangeEvent(
                            ticker=row_ticker,
                            price=yes_price,
                            timestamp=timestamp,
                        )
                        events.append(event)

                        # Also emit No-side price event
                        if no_ticker is not None:
                            no_price = Decimal('1') - yes_price
                            no_event = PriceChangeEvent(
                                ticker=no_ticker,
                                price=no_price,
                                timestamp=timestamp,
                            )
                            events.append(no_event)
        except Exception as e:
            logger.error('Error loading events from %s: %s', self.history_file, e)

        # Sort events by timestamp
        events.sort(key=lambda e: self._timestamp_sort_key(getattr(e, 'timestamp', None)))
        logger.info('Historical data loaded: %d events', len(events))
        return events

    def _ticker_for_row(self, data: dict[str, Any]) -> PolyMarketTicker:
        market_id = str(data.get('market_id', ''))
        event_id = str(data.get('event_id', ''))
        key = (event_id, market_id)
        existing = self._tickers_by_market.get(key)
        if existing is not None:
            return existing

        market_label = (
            str(
                data.get('question')
                or data.get('title')
                or data.get('market_title')
                or data.get('name')
                or market_id
            )
            or market_id
        )
        symbol = f'BT_{market_id}'
        ticker = PolyMarketTicker(
            symbol=symbol,
            name=market_label,
            token_id=symbol,
            market_id=market_id,
            event_id=event_id,
            no_token_id=f'{symbol}_NO',
        )
        self._tickers_by_market[key] = ticker
        return ticker

    @staticmethod
    def _timestamp_sort_key(value: Any) -> float:
        if isinstance(value, bool):
            return float('inf')
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return float('inf')
            try:
                return float(raw)
            except ValueError:
                pass
            try:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                return float('inf')
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return float('inf')

    async def get_next_event(self) -> Event | None:
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            return event
        return None

    def drain_same_timestamp_events(self, first_event: Event) -> list[Event]:
        """Consume later events that belong to the same replay timestamp."""
        target_key = self._timestamp_sort_key(getattr(first_event, 'timestamp', None))
        if target_key == float('inf'):
            return []

        drained: list[Event] = []
        while self.index < len(self.events):
            candidate = self.events[self.index]
            candidate_key = self._timestamp_sort_key(
                getattr(candidate, 'timestamp', None)
            )
            if candidate_key != target_key:
                break
            drained.append(candidate)
            self.index += 1
        return drained
