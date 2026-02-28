"""Order Book Imbalance (OBI) strategy.

Fires on OrderBookEvent. Computes volume imbalance across ``depth`` price levels.

    imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)

- Enter BUY_YES when imbalance > entry_threshold and no position
- Exit SELL when imbalance < exit_threshold OR elapsed > max_hold_seconds
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from oracle3.events.events import Event, OrderBookEvent
from oracle3.ticker.ticker import Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

from .quant_strategy import QuantStrategy


class OrderBookImbalanceStrategy(QuantStrategy):
    name = 'orderbook_imbalance'
    version = '0.2.0'
    author = 'oracle3'

    def __init__(
        self,
        tickers: list[Ticker] | None = None,
        depth: int = 3,
        entry_threshold: float = 0.3,
        exit_threshold: float = -0.1,
        position_size: Decimal = Decimal('10'),
        max_hold_seconds: int = 300,
    ) -> None:
        super().__init__()
        ticker_list = tickers or []
        self.tickers: set[str] = {t.symbol for t in ticker_list}
        self._ticker_map: dict[str, Ticker] = {t.symbol: t for t in ticker_list}
        self.depth = depth
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.position_size = position_size
        self.max_hold_seconds = max_hold_seconds
        self.logger = logging.getLogger(__name__)

        # symbol → entry_time
        self._entries: dict[str, datetime] = {}
        self._closing_in_progress: set[str] = set()

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, OrderBookEvent):
            return
        ticker = event.ticker
        if self.tickers and ticker.symbol not in self.tickers:
            return

        ob = trader.market_data.order_books.get(ticker)
        if ob is None:
            return

        bids = ob.get_bids(self.depth)
        asks = ob.get_asks(self.depth)
        bid_vol = float(sum(lv.size for lv in bids))
        ask_vol = float(sum(lv.size for lv in asks))
        total_vol = bid_vol + ask_vol
        if total_vol == 0:
            return

        imbalance = (bid_vol - ask_vol) / total_vol

        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity > 0

        now = datetime.now()

        if has_position:
            # Check exit conditions
            if ticker.symbol in self._closing_in_progress:
                return
            entry_time = self._entries.get(ticker.symbol)
            elapsed = (now - entry_time).total_seconds() if entry_time else 0

            should_exit = (
                imbalance < self.exit_threshold or elapsed > self.max_hold_seconds
            )
            if should_exit:
                action = (
                    'CLOSE_TIMEOUT' if elapsed > self.max_hold_seconds else 'CLOSE_OBI'
                )
                self._closing_in_progress.add(ticker.symbol)
                try:
                    bid = trader.market_data.get_best_bid(ticker)
                    if bid is None:
                        return
                    result = await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=ticker,
                        limit_price=bid.price,
                        quantity=position.quantity,
                    )
                    executed = result.order is not None
                    if executed:
                        self._entries.pop(ticker.symbol, None)
                    self.record_decision(
                        ticker_name=ticker.name or ticker.symbol,
                        action=action,
                        executed=executed,
                        reasoning=f'imbalance={imbalance:.3f}, elapsed={elapsed:.0f}s',
                        signal_values={
                            'imbalance': imbalance,
                            'bid_vol': bid_vol,
                            'ask_vol': ask_vol,
                        },
                    )
                finally:
                    self._closing_in_progress.discard(ticker.symbol)
        else:
            # Check entry condition
            if imbalance > self.entry_threshold:
                ask = trader.market_data.get_best_ask(ticker)
                if ask is None:
                    return
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=ask.price,
                    quantity=self.position_size,
                )
                executed = result.order is not None
                if executed:
                    self._entries[ticker.symbol] = now
                self.record_decision(
                    ticker_name=ticker.name or ticker.symbol,
                    action='BUY_YES',
                    executed=executed,
                    reasoning=f'imbalance={imbalance:.3f} > threshold={self.entry_threshold}',
                    signal_values={
                        'imbalance': imbalance,
                        'bid_vol': bid_vol,
                        'ask_vol': ask_vol,
                    },
                )
            else:
                self.record_decision(
                    ticker_name=ticker.name or ticker.symbol,
                    action='HOLD',
                    executed=False,
                    reasoning=f'imbalance={imbalance:.3f}',
                    signal_values={
                        'imbalance': imbalance,
                        'bid_vol': bid_vol,
                        'ask_vol': ask_vol,
                    },
                )
