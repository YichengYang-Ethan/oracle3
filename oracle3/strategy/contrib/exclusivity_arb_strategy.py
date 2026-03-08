"""ExclusivityArbStrategy -- trade constraint violations on exclusive pairs.

For exclusivity relations (A and B mutually exclusive), the constraint is
A + B <= 1. Example: "AOC wins Dem nomination" and "Ossoff wins Dem nomination"
can't both happen, so P(A) + P(B) <= 1.

When the market violates this (price_A + price_B > 1), we:
  - Sell A (buy A's NO token)
  - Sell B (buy B's NO token)
  Cost = (1 - price_A) + (1 - price_B) = 2 - (A + B) < 1
  Payout = at least 1 (at most one resolves YES, so at least one NO pays 1)
  Profit = payout - cost > 0

Exit when the constraint is restored (A + B <= 1).

Usage:
    oracle3 engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref oracle3/strategy/contrib/exclusivity_arb_strategy.py:ExclusivityArbStrategy \\
      --strategy-kwargs-json '{"market_id_a": "...", "market_id_b": "..."}'
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from oracle3.events.events import Event, PriceChangeEvent
from oracle3.strategy.quant_strategy import QuantStrategy
from oracle3.ticker.ticker import Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)

# Conservative per-side fee estimate (0.5%)
_FEE_PER_SIDE = Decimal('0.005')


class ExclusivityArbStrategy(QuantStrategy):
    """Arbitrage constraint violations on exclusive pairs (A + B <= 1).

    For two mutually exclusive events A and B, the probability constraint
    P(A) + P(B) <= 1 must hold.  When markets misprice such that the sum
    exceeds 1, buying both NO tokens creates a risk-free profit.

    Parameters
    ----------
    market_id_a:
        Market/condition identifier for outcome A.
    market_id_b:
        Market/condition identifier for outcome B.
    trade_size:
        Dollar amount per leg.
    min_edge:
        Minimum violation size (A + B - 1) after fees to trigger entry.
    cooldown_seconds:
        Minimum seconds between successive entry attempts.
    fee_rate:
        Per-side fee rate (default 0.005 = 0.5%).
    """

    name = 'exclusivity_arb'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        market_id_a: str = '',
        market_id_b: str = '',
        trade_size: float = 10.0,
        min_edge: float = 0.02,
        cooldown_seconds: float = 120.0,
        fee_rate: float = 0.005,
    ) -> None:
        super().__init__()
        self._id_a = market_id_a
        self._id_b = market_id_b
        self.trade_size = Decimal(str(trade_size))
        self.min_edge = Decimal(str(min_edge))
        self.cooldown_seconds = cooldown_seconds
        self.fee_rate = Decimal(str(fee_rate))

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        # Position state machine: flat -> short_both -> flat
        self._position_state = 'flat'  # flat | short_both
        self._last_entry_time: float = float('-inf')

    # -- Ticker matching -----------------------------------------------------

    @staticmethod
    def _matches(ticker_id: str, market_id: str) -> bool:
        """Flexible substring match for market/condition IDs."""
        if not market_id:
            return False
        return market_id in ticker_id or ticker_id in market_id

    def _extract_ticker_id(self, ticker: Ticker) -> str:
        """Extract the best identifier from a ticker for matching."""
        return (
            getattr(ticker, 'market_id', '')
            or getattr(ticker, 'token_id', '')
            or ticker.symbol
        )

    def _find_ticker(
        self, trader: Trader, market_id: str, *, yes: bool = True
    ) -> Ticker | None:
        """Find a YES or NO ticker in the order books matching a market ID."""
        for ticker in trader.market_data.order_books:
            is_no = (
                ticker.symbol.endswith('_NO')
                or (getattr(ticker, 'name', '') or '').startswith('NO ')
            )
            if yes and is_no:
                continue
            if not yes and not is_no:
                continue
            tid = self._extract_ticker_id(ticker)
            if self._matches(tid, market_id):
                return ticker
        return None

    # -- Fee helpers ----------------------------------------------------------

    def _fee_cost(self, n_legs: int = 2) -> Decimal:
        """Total fee cost for entry (buy-side) plus eventual exit (sell-side)."""
        return self.fee_rate * self.trade_size * Decimal(str(n_legs)) * Decimal('2')

    # -- Core logic -----------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
        # Skip NO-side events — we only track YES prices
        if ticker.symbol.endswith('_NO') or (
            getattr(ticker, 'name', '') or ''
        ).startswith('NO '):
            return

        tid = self._extract_ticker_id(ticker)

        if self._matches(tid, self._id_a):
            self._price_a = event.price
        elif self._matches(tid, self._id_b):
            self._price_b = event.price
        else:
            return

        # Need both prices
        if self._price_a is None or self._price_b is None:
            return

        total = self._price_a + self._price_b
        violation = total - Decimal('1')  # > 0 means constraint broken

        if self._position_state == 'flat':
            # Check if violation exceeds min_edge after fees
            net_edge = violation - self._fee_cost()  / self.trade_size
            if violation > self.min_edge and net_edge > Decimal('0'):
                # Cooldown guard
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter(trader, violation)
            else:
                self.record_decision(
                    ticker_name=f'excl({self._id_a[:10]}|{self._id_b[:10]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={float(self._price_a):.4f} B={float(self._price_b):.4f} '
                        f'sum={float(total):.4f} violation={float(violation):.4f}'
                    ),
                    signal_values={
                        'price_a': float(self._price_a),
                        'price_b': float(self._price_b),
                        'sum': float(total),
                        'violation': float(violation),
                    },
                )
        elif self._position_state == 'short_both':
            # Exit when constraint is restored
            if violation <= Decimal('0'):
                await self._exit(trader, violation)

    async def _enter(self, trader: Trader, violation: Decimal) -> None:
        """Sell both A and B (buy both NO tokens)."""
        ticker_a_no = self._find_ticker(trader, self._id_a, yes=False)
        ticker_b_no = self._find_ticker(trader, self._id_b, yes=False)

        executed_legs = 0

        if ticker_a_no and self._price_a is not None:
            no_price_a = Decimal('1') - self._price_a
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_a_no,
                    limit_price=no_price_a, quantity=self.trade_size,
                )
                if result.failure_reason:
                    logger.warning(
                        'Exclusivity arb leg A failed: %s', result.failure_reason
                    )
                else:
                    executed_legs += 1
            except Exception:
                logger.exception('Error placing exclusivity arb leg A')

        if ticker_b_no and self._price_b is not None:
            no_price_b = Decimal('1') - self._price_b
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_b_no,
                    limit_price=no_price_b, quantity=self.trade_size,
                )
                if result.failure_reason:
                    logger.warning(
                        'Exclusivity arb leg B failed: %s', result.failure_reason
                    )
                else:
                    executed_legs += 1
            except Exception:
                logger.exception('Error placing exclusivity arb leg B')

        if executed_legs > 0:
            self._position_state = 'short_both'
        self.record_decision(
            ticker_name=f'excl({self._id_a[:10]}|{self._id_b[:10]})',
            action='ENTER_ARB',
            executed=executed_legs > 0,
            reasoning=(
                f'Constraint violated: A={float(self._price_a or 0):.4f} + '
                f'B={float(self._price_b or 0):.4f} = '
                f'{float((self._price_a or 0) + (self._price_b or 0)):.4f} > 1  '
                f'legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
                'executed_legs': executed_legs,
            },
        )
        logger.info(
            'ENTER exclusivity arb: sell A=%s sell B=%s violation=%.4f legs=%d/2',
            self._price_a, self._price_b, violation, executed_legs,
        )

    async def _exit(self, trader: Trader, violation: Decimal) -> None:
        """Close all positions -- constraint restored."""
        executed_legs = 0
        for pos in trader.position_manager.positions.values():
            if pos.quantity > 0:
                best_bid = trader.market_data.get_best_bid(pos.ticker)
                if best_bid:
                    try:
                        result = await trader.place_order(
                            side=TradeSide.SELL, ticker=pos.ticker,
                            limit_price=best_bid.price, quantity=pos.quantity,
                        )
                        if not result.failure_reason:
                            executed_legs += 1
                    except Exception:
                        logger.exception(
                            'Error closing exclusivity arb leg: %s', pos.ticker.symbol
                        )

        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'excl({self._id_a[:10]}|{self._id_b[:10]})',
            action='EXIT_ARB',
            executed=executed_legs > 0,
            reasoning=(
                f'Constraint restored: A={float(self._price_a or 0):.4f} + '
                f'B={float(self._price_b or 0):.4f} <= 1'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
            },
        )
        logger.info('EXIT exclusivity arb: constraint restored')
