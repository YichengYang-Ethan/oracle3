"""EventSumArbStrategy -- intra-event sum arbitrage.

Within a single prediction market event (e.g. "NBA Champion 2025"), there are N
binary markets -- one per outcome.  Because exactly one outcome settles YES,
the fair-value constraint is:

    sum(YES_price_i  for i in 1..N) = 1.0

In practice ask prices deviate due to bid-ask spreads and mispricing:

    Case A -- underpriced  (sum < 1.0):
        Buy YES on every outcome.
        Cost   = sum(ask_YES_i)            < 1.0
        Payout = 1.0  (exactly one wins)
        Profit = 1.0 - sum(ask_YES_i)      > 0

    Case B -- overpriced  (sum > 1.0):
        Buy NO on every outcome.
        Cost   = sum(1 - ask_YES_i)  =  N - sum(ask_YES_i)
        Payout = N - 1               (all N-1 losing YES settle NO)
        Profit = sum(ask_YES_i) - 1.0     > 0

Usage:
    oracle3 engine run \\
        --exchange polymarket \\
        --strategy-ref oracle3/strategy/contrib/event_sum_arb_strategy.py:EventSumArbStrategy \\
        --strategy-kwargs-json '{"event_id": "...", "min_edge": 0.02}'

The strategy self-discovers markets by watching for any ticker whose
``event_id`` matches the configured value.  No pre-wiring required.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.quant_strategy import QuantStrategy
from oracle3.ticker.ticker import PolyMarketTicker, Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)

# Conservative per-side fee estimate (0.5%)
_FEE_PER_SIDE = Decimal('0.005')


class EventSumArbStrategy(QuantStrategy):
    """Intra-event sum arbitrage for a specific event_id.

    Monitors all markets within an event and trades when the sum of
    YES prices deviates from 1.0 by more than ``min_edge`` after fees.

    Parameters
    ----------
    event_id:
        Event ID -- all markets in this event are monitored.
    min_edge:
        Minimum net profit per share after fees to trigger (default 0.02).
    trade_size:
        Dollar amount per leg.  Total outlay = trade_size x N_markets.
    cooldown_seconds:
        Minimum seconds between arb executions (default 120).
    min_markets:
        Require at least this many markets before attempting arb (default 2).
    fee_rate:
        Per-side fee rate (default 0.005 = 0.5%).
    """

    name = 'event_sum_arb'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        event_id: str = '',
        min_edge: float = 0.02,
        trade_size: float = 10.0,
        cooldown_seconds: float = 120.0,
        min_markets: int = 2,
        fee_rate: float = 0.005,
    ) -> None:
        super().__init__()
        self.event_id = event_id
        self.min_edge = Decimal(str(min_edge))
        self.trade_size = Decimal(str(trade_size))
        self.cooldown_seconds = cooldown_seconds
        self.min_markets = min_markets
        self.fee_rate = Decimal(str(fee_rate))

        # market_id -> latest ask/price
        self._asks: dict[str, Decimal] = {}
        # market_id -> ticker
        self._tickers: dict[str, PolyMarketTicker] = {}
        self._last_arb_time: float = 0.0

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        ticker = getattr(event, 'ticker', None)
        if not isinstance(ticker, PolyMarketTicker):
            return
        if ticker.event_id != self.event_id:
            return

        mid = ticker.market_id
        if not mid:
            return

        # Register ticker on first sight
        if mid not in self._tickers:
            self._tickers[mid] = ticker
            logger.debug(
                'EventSumArb: registered market %s (%s)',
                mid[:16],
                ticker.name[:40] if ticker.name else '?',
            )

        # Update price estimate
        if isinstance(event, OrderBookEvent):
            if event.side == 'ask' and event.price > 0:
                self._asks[mid] = event.price
            elif event.side == 'bid' and mid not in self._asks:
                # Use bid as fallback until we see an ask
                self._asks[mid] = event.price
        elif isinstance(event, PriceChangeEvent):
            if mid not in self._asks:
                self._asks[mid] = event.price

        await self._check_arb(trader)

    async def _check_arb(self, trader: Trader) -> None:
        if len(self._asks) < self.min_markets:
            return

        # Only include markets with both a ticker and a price
        market_ids = [mid for mid in self._asks if mid in self._tickers]
        if len(market_ids) < self.min_markets:
            return

        prices = {mid: self._asks[mid] for mid in market_ids}
        sum_yes = sum(prices.values())
        n = len(prices)

        fee_per_leg = self.fee_rate
        total_fees = fee_per_leg * n

        # Case A: underpriced -- buy all YES
        edge_buy_yes = Decimal('1') - sum_yes - total_fees
        # Case B: overpriced -- buy all NO
        edge_buy_no = sum_yes - Decimal('1') - total_fees

        best_edge = max(edge_buy_yes, edge_buy_no)
        action = 'BUY_YES' if edge_buy_yes >= edge_buy_no else 'BUY_NO'

        signal = {
            'sum_yes': float(sum_yes),
            'n_markets': n,
            'edge_buy_yes': float(edge_buy_yes),
            'edge_buy_no': float(edge_buy_no),
            'best_edge': float(best_edge),
        }

        if best_edge < self.min_edge:
            self.record_decision(
                ticker_name=f'event:{self.event_id[:16]}',
                action='HOLD',
                executed=False,
                reasoning=(
                    f'sum_yes={float(sum_yes):.4f} n={n} '
                    f'best_edge={float(best_edge):.4f} '
                    f'< min={float(self.min_edge):.4f}'
                ),
                signal_values=signal,
            )
            return

        # Cooldown guard
        now = time.monotonic()
        if now - self._last_arb_time < self.cooldown_seconds:
            return
        self._last_arb_time = now

        logger.info(
            'EventSumArb: %s event=%s sum_yes=%.4f n=%d edge=%.4f',
            action, self.event_id[:16], float(sum_yes), n, float(best_edge),
        )

        executed_legs = 0
        failed_legs = 0

        for mid, ask_price in prices.items():
            ticker = self._tickers[mid]

            if action == 'BUY_YES':
                trade_ticker: Ticker = ticker
                leg_price = ask_price
            else:
                # BUY_NO: use the NO token if available
                no_ticker = ticker.get_no_ticker()
                if no_ticker is None:
                    logger.warning(
                        'EventSumArb: no NO ticker for market %s, skipping leg',
                        mid[:16],
                    )
                    failed_legs += 1
                    continue
                trade_ticker = no_ticker
                leg_price = Decimal('1') - ask_price  # NO price = 1 - YES price

            try:
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=trade_ticker,
                    limit_price=leg_price,
                    quantity=self.trade_size,
                )
                if result.failure_reason:
                    logger.warning(
                        'EventSumArb leg failed: %s - %s',
                        mid[:16], result.failure_reason,
                    )
                    failed_legs += 1
                else:
                    executed_legs += 1
            except Exception:
                logger.exception(
                    'EventSumArb: exception placing leg for market %s', mid[:16]
                )
                failed_legs += 1

        self.record_decision(
            ticker_name=f'event:{self.event_id[:16]}',
            action=action,
            executed=executed_legs > 0,
            reasoning=(
                f'sum_yes={float(sum_yes):.4f} n={n} '
                f'edge={float(best_edge):.4f} '
                f'legs={executed_legs}/{n} failed={failed_legs}'
            ),
            signal_values={
                **signal,
                'executed_legs': executed_legs,
                'failed_legs': failed_legs,
            },
        )
