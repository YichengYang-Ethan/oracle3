"""ConditionalArbStrategy -- trade conditional probability constraint violations.

For conditional relations where p(A|B) is bounded, the joint pricing of
A and B is constrained:

    p(A) >= cond_lower x p(B)
    p(A) <= cond_upper x p(B) + (1 - p(B))

Example: "ceasefire by June" is conditional on "peace talks by March".
If p(ceasefire|talks) in [0.4, 0.9], and talks = 0.6, then:
    ceasefire should be in [0.24, 0.94]

When market prices violate these bounds, we trade:
  - p(A) too high: sell A, buy B
  - p(A) too low: buy A, sell B

Usage:
    oracle3 engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref oracle3/strategy/contrib/conditional_arb_strategy.py:ConditionalArbStrategy \\
      --strategy-kwargs-json '{"market_id_a": "...", "market_id_b": "...", "cond_lower": 0.4, "cond_upper": 0.9}'
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


class ConditionalArbStrategy(QuantStrategy):
    """Arbitrage conditional probability constraint violations.

    Given known bounds on p(A|B), derive the valid range for p(A) and
    trade when market prices fall outside that range.

    The valid band for p(A) given p(B):
        lower = cond_lower * p(B)
        upper = cond_upper * p(B) + (1 - p(B))

    Parameters
    ----------
    market_id_a:
        Market/condition identifier for outcome A (the conditional event).
    market_id_b:
        Market/condition identifier for outcome B (the conditioning event).
    trade_size:
        Dollar amount per leg.
    cond_lower:
        Lower bound on p(A|B). Default 0 (no lower bound).
    cond_upper:
        Upper bound on p(A|B). Default 1 (no upper bound).
    min_edge:
        Minimum distance outside the band to trigger entry.
    cooldown_seconds:
        Minimum seconds between successive entry attempts.
    fee_rate:
        Per-side fee rate (default 0.005 = 0.5%).
    """

    name = 'conditional_arb'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        market_id_a: str = '',
        market_id_b: str = '',
        trade_size: float = 10.0,
        cond_lower: float = 0.0,
        cond_upper: float = 1.0,
        min_edge: float = 0.02,
        cooldown_seconds: float = 120.0,
        fee_rate: float = 0.005,
    ) -> None:
        super().__init__()
        self._id_a = market_id_a
        self._id_b = market_id_b
        self.trade_size = Decimal(str(trade_size))
        self.cond_lower = cond_lower
        self.cond_upper = cond_upper
        self.min_edge = Decimal(str(min_edge))
        self.cooldown_seconds = cooldown_seconds
        self.fee_rate = Decimal(str(fee_rate))

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        # flat | long_a_short_b (A too low) | short_a_long_b (A too high)
        self._position_state = 'flat'
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

    # -- Bounds computation ---------------------------------------------------

    def _compute_bounds(self, price_b: float) -> tuple[float, float]:
        """Compute the valid range for p(A) given p(B) and conditional bounds.

        Derivation from total probability:
            p(A) = p(A|B)*p(B) + p(A|~B)*(1-p(B))

        Lower bound: p(A|B) = cond_lower, p(A|~B) = 0
        Upper bound: p(A|B) = cond_upper, p(A|~B) = 1
        """
        lower = self.cond_lower * price_b
        upper = self.cond_upper * price_b + (1 - price_b)
        return lower, upper

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

        pa = float(self._price_a)
        pb = float(self._price_b)
        lower, upper = self._compute_bounds(pb)
        fee_buffer = float(self.min_edge)

        if self._position_state == 'flat':
            if pa > upper + fee_buffer:
                # A too high -> sell A, buy B
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter_short_a(trader, pa, lower, upper)
            elif pa < lower - fee_buffer:
                # A too low -> buy A, sell B
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter_long_a(trader, pa, lower, upper)
            else:
                self.record_decision(
                    ticker_name=f'cond({self._id_a[:10]}|{self._id_b[:10]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={pa:.4f} in band [{lower:.4f}, {upper:.4f}] '
                        f'(B={pb:.4f})'
                    ),
                    signal_values={
                        'price_a': pa, 'price_b': pb,
                        'lower': lower, 'upper': upper,
                    },
                )
        else:
            # Exit when A is back inside the band
            if lower <= pa <= upper:
                await self._exit(trader, pa, lower, upper)

    async def _enter_short_a(
        self, trader: Trader, pa: float, lower: float, upper: float,
    ) -> None:
        """A too expensive -> sell A (buy NO), buy B (buy YES)."""
        ticker_a_no = self._find_ticker(trader, self._id_a, yes=False)
        ticker_b = self._find_ticker(trader, self._id_b, yes=True)

        executed_legs = 0

        if ticker_a_no and self._price_a is not None:
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_a_no,
                    limit_price=Decimal('1') - self._price_a,
                    quantity=self.trade_size,
                )
                if not result.failure_reason:
                    executed_legs += 1
                else:
                    logger.warning(
                        'Conditional arb leg A_NO failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing conditional arb leg A_NO')

        if ticker_b and self._price_b is not None:
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_b,
                    limit_price=self._price_b, quantity=self.trade_size,
                )
                if not result.failure_reason:
                    executed_legs += 1
                else:
                    logger.warning(
                        'Conditional arb leg B failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing conditional arb leg B')

        if executed_legs > 0:
            self._position_state = 'short_a_long_b'
        self.record_decision(
            ticker_name=f'cond({self._id_a[:10]}|{self._id_b[:10]})',
            action='ENTER_SHORT_A',
            executed=executed_legs > 0,
            reasoning=(
                f'A={pa:.4f} > upper={upper:.4f}: sell A, buy B  '
                f'legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'lower': lower, 'upper': upper,
                'executed_legs': executed_legs,
            },
        )
        logger.info('ENTER conditional arb: A too high, sell A buy B')

    async def _enter_long_a(
        self, trader: Trader, pa: float, lower: float, upper: float,
    ) -> None:
        """A too cheap -> buy A (buy YES), sell B (buy NO)."""
        ticker_a = self._find_ticker(trader, self._id_a, yes=True)
        ticker_b_no = self._find_ticker(trader, self._id_b, yes=False)

        executed_legs = 0

        if ticker_a and self._price_a is not None:
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_a,
                    limit_price=self._price_a, quantity=self.trade_size,
                )
                if not result.failure_reason:
                    executed_legs += 1
                else:
                    logger.warning(
                        'Conditional arb leg A failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing conditional arb leg A')

        if ticker_b_no and self._price_b is not None:
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY, ticker=ticker_b_no,
                    limit_price=Decimal('1') - self._price_b,
                    quantity=self.trade_size,
                )
                if not result.failure_reason:
                    executed_legs += 1
                else:
                    logger.warning(
                        'Conditional arb leg B_NO failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing conditional arb leg B_NO')

        if executed_legs > 0:
            self._position_state = 'long_a_short_b'
        self.record_decision(
            ticker_name=f'cond({self._id_a[:10]}|{self._id_b[:10]})',
            action='ENTER_LONG_A',
            executed=executed_legs > 0,
            reasoning=(
                f'A={pa:.4f} < lower={lower:.4f}: buy A, sell B  '
                f'legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'lower': lower, 'upper': upper,
                'executed_legs': executed_legs,
            },
        )
        logger.info('ENTER conditional arb: A too low, buy A sell B')

    async def _exit(
        self, trader: Trader, pa: float, lower: float, upper: float,
    ) -> None:
        """Close all positions -- A back inside the valid band."""
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
                            'Error closing conditional arb leg: %s', pos.ticker.symbol
                        )

        prev = self._position_state
        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'cond({self._id_a[:10]}|{self._id_b[:10]})',
            action='EXIT',
            executed=executed_legs > 0,
            reasoning=(
                f'A={pa:.4f} back in [{lower:.4f}, {upper:.4f}] (was {prev})'
            ),
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'lower': lower, 'upper': upper,
            },
        )
        logger.info('EXIT conditional arb: A back in band')
