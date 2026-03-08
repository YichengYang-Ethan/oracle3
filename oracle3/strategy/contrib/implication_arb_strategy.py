"""ImplicationArbStrategy -- trade constraint violations on implication pairs.

For implication relations (A implies B), the constraint is A <= B.
Example: "Trump wins nomination" implies "Trump wins election", so
P(nomination) <= P(election) must always hold.

When the market violates this (price_A > price_B), we:
  - Sell A (buy A's NO token)
  - Buy B (buy B's YES token)
and exit when the constraint is restored (price_A <= price_B).

Usage:
    oracle3 engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref oracle3/strategy/contrib/implication_arb_strategy.py:ImplicationArbStrategy \\
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


class ImplicationArbStrategy(QuantStrategy):
    """Arbitrage constraint violations on implication pairs (A <= B).

    For two events where A implies B, the probability constraint
    P(A) <= P(B) must hold.  When market A is priced higher than market B,
    we sell A (buy NO) and buy B (buy YES) to capture the convergence.

    Parameters
    ----------
    market_id_a:
        Market/condition identifier for outcome A (the implied event).
    market_id_b:
        Market/condition identifier for outcome B (the implying event).
    trade_size:
        Dollar amount per leg.
    min_edge:
        Minimum violation size (price_A - price_B) after fees to trigger.
    cooldown_seconds:
        Minimum seconds between successive entry attempts.
    fee_rate:
        Per-side fee rate (default 0.005 = 0.5%).
    """

    name = 'implication_arb'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        market_id_a: str = '',
        market_id_b: str = '',
        trade_size: float = 10.0,
        min_edge: float = 0.01,
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
        # Position state machine: flat -> short_a_long_b -> flat
        self._position_state = 'flat'  # flat | short_a_long_b
        self._last_entry_time: float = float('-inf')

    # -- Ticker matching -----------------------------------------------------

    @staticmethod
    def _matches(ticker_id: str, market_id: str) -> bool:
        if not market_id:
            return False
        return market_id in ticker_id or ticker_id in market_id

    def _extract_ticker_id(self, ticker: Ticker) -> str:
        return (
            getattr(ticker, 'market_id', '')
            or getattr(ticker, 'token_id', '')
            or ticker.symbol
        )

    def _find_ticker(
        self, trader: Trader, market_id: str, *, yes: bool = True
    ) -> Ticker | None:
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

    def _fee_cost_per_unit(self) -> Decimal:
        """Round-trip fee cost per unit of trade_size (2 legs x 2 sides)."""
        return self.fee_rate * Decimal('4')

    # -- Core logic -----------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
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

        if self._price_a is None or self._price_b is None:
            return

        # violation > 0 means A > B (constraint broken)
        violation = self._price_a - self._price_b

        if self._position_state == 'flat':
            net_edge = violation - self._fee_cost_per_unit()
            if violation > self.min_edge and net_edge > Decimal('0'):
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter(trader, violation)
            else:
                self.record_decision(
                    ticker_name=f'impl({self._id_a[:10]}|{self._id_b[:10]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={float(self._price_a):.4f} B={float(self._price_b):.4f} '
                        f'violation={float(violation):.4f} '
                        f'< min_edge={float(self.min_edge):.4f}'
                    ),
                    signal_values={
                        'price_a': float(self._price_a),
                        'price_b': float(self._price_b),
                        'violation': float(violation),
                    },
                )
        elif self._position_state == 'short_a_long_b':
            # Exit when constraint restored
            if violation <= Decimal('0'):
                await self._exit(trader, violation)

    async def _enter(self, trader: Trader, violation: Decimal) -> None:
        """Sell A (buy NO), buy B (buy YES)."""
        ticker_a_no = self._find_ticker(trader, self._id_a, yes=False)
        ticker_b = self._find_ticker(trader, self._id_b, yes=True)

        executed_legs = 0

        if ticker_a_no and self._price_a is not None:
            no_price = Decimal('1') - self._price_a
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_a_no,
                    limit_price=no_price, quantity=self.trade_size,
                )
                if result.failure_reason:
                    logger.warning(
                        'Implication arb leg A_NO failed: %s', result.failure_reason
                    )
                else:
                    executed_legs += 1
            except Exception:
                logger.exception('Error placing implication arb leg A_NO')

        if ticker_b and self._price_b is not None:
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_b,
                    limit_price=self._price_b, quantity=self.trade_size,
                )
                if result.failure_reason:
                    logger.warning(
                        'Implication arb leg B failed: %s', result.failure_reason
                    )
                else:
                    executed_legs += 1
            except Exception:
                logger.exception('Error placing implication arb leg B')

        if executed_legs > 0:
            self._position_state = 'short_a_long_b'
        self.record_decision(
            ticker_name=f'impl({self._id_a[:10]}|{self._id_b[:10]})',
            action='ENTER_ARB',
            executed=executed_legs > 0,
            reasoning=(
                f'Constraint violated: A={float(self._price_a or 0):.4f} > '
                f'B={float(self._price_b or 0):.4f}, '
                f'violation={float(violation):.4f}  legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
                'executed_legs': executed_legs,
            },
        )
        logger.info(
            'ENTER implication arb: sell A=%s buy B=%s violation=%.4f legs=%d/2',
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
                            'Error closing implication arb leg: %s', pos.ticker.symbol
                        )

        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'impl({self._id_a[:10]}|{self._id_b[:10]})',
            action='EXIT_ARB',
            executed=executed_legs > 0,
            reasoning=(
                f'Constraint restored: A={float(self._price_a or 0):.4f} <= '
                f'B={float(self._price_b or 0):.4f}'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
            },
        )
        logger.info('EXIT implication arb: constraint restored')
