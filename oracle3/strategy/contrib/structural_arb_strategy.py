"""StructuralArbStrategy -- trade deterministic structural constraint violations.

For structural relations with a known linear constraint between two markets:

    p(A) = slope x p(B) + intercept    (+/- tolerance)

Example: Two markets on the same underlying with different payout
structures, or markets with a known mathematical relationship.

When market prices deviate beyond tolerance from the expected relationship,
we trade the deviation back to the structural equilibrium.

Usage:
    oracle3 engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref oracle3/strategy/contrib/structural_arb_strategy.py:StructuralArbStrategy \\
      --strategy-kwargs-json '{"market_id_a": "...", "market_id_b": "...", "slope": 1.0, "intercept": 0.0}'
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


class StructuralArbStrategy(QuantStrategy):
    """Arbitrage deviations from a deterministic structural constraint.

    The expected relationship is: p(A) = slope x p(B) + intercept.
    Trade when the residual (actual - expected) exceeds min_edge.

    Entry: |residual| > min_edge  (after fee buffer)
    Exit:  |residual| < min_edge * exit_fraction

    Parameters
    ----------
    market_id_a:
        Market/condition identifier for outcome A.
    market_id_b:
        Market/condition identifier for outcome B.
    trade_size:
        Dollar amount per leg.
    slope:
        Expected linear relationship slope (default 1.0).
    intercept:
        Expected linear relationship intercept (default 0.0).
    min_edge:
        Minimum residual to trigger entry.
    exit_fraction:
        Exit when |residual| falls below min_edge * exit_fraction (default 0.5).
    cooldown_seconds:
        Minimum seconds between successive entry attempts.
    fee_rate:
        Per-side fee rate (default 0.005 = 0.5%).
    """

    name = 'structural_arb'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        market_id_a: str = '',
        market_id_b: str = '',
        trade_size: float = 10.0,
        slope: float = 1.0,
        intercept: float = 0.0,
        min_edge: float = 0.02,
        exit_fraction: float = 0.5,
        cooldown_seconds: float = 120.0,
        fee_rate: float = 0.005,
    ) -> None:
        super().__init__()
        self._id_a = market_id_a
        self._id_b = market_id_b
        self.trade_size = Decimal(str(trade_size))
        self.slope = slope
        self.intercept = intercept
        self.min_edge = Decimal(str(min_edge))
        self.exit_fraction = exit_fraction
        self.cooldown_seconds = cooldown_seconds
        self.fee_rate = Decimal(str(fee_rate))

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        # flat | long_a_short_b (A underpriced) | short_a_long_b (A overpriced)
        self._position_state = 'flat'
        self._last_entry_time: float = 0.0

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

    # -- Structural relationship ---------------------------------------------

    def _expected_a(self, price_b: float) -> float:
        """Compute expected p(A) from the structural relationship."""
        return self.slope * price_b + self.intercept

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
        expected = self._expected_a(pb)
        residual = pa - expected  # positive = A overpriced

        if self._position_state == 'flat':
            if residual > float(self.min_edge):
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter_short_a(trader, pa, expected, residual)
            elif residual < -float(self.min_edge):
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter_long_a(trader, pa, expected, residual)
            else:
                self.record_decision(
                    ticker_name=f'struc({self._id_a[:10]}|{self._id_b[:10]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={pa:.4f} expected={expected:.4f} '
                        f'residual={residual:.4f} '
                        f'within +/-{float(self.min_edge):.4f}'
                    ),
                    signal_values={
                        'price_a': pa, 'price_b': pb,
                        'expected': expected, 'residual': residual,
                    },
                )
        else:
            # Exit when residual converges toward zero
            exit_threshold = float(self.min_edge) * self.exit_fraction
            if abs(residual) < exit_threshold:
                await self._exit(trader, residual)

    async def _enter_short_a(
        self, trader: Trader, pa: float, expected: float, residual: float,
    ) -> None:
        """A overpriced -> sell A (buy NO), buy B (buy YES)."""
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
                        'Structural arb leg A_NO failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing structural arb leg A_NO')

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
                        'Structural arb leg B failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing structural arb leg B')

        if executed_legs > 0:
            self._position_state = 'short_a_long_b'
        self.record_decision(
            ticker_name=f'struc({self._id_a[:10]}|{self._id_b[:10]})',
            action='ENTER_SHORT_A',
            executed=executed_legs > 0,
            reasoning=(
                f'A={pa:.4f} > expected={expected:.4f}, '
                f'residual={residual:.4f}  legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'expected': expected, 'residual': residual,
                'executed_legs': executed_legs,
            },
        )
        logger.info(
            'ENTER structural arb: A overpriced, residual=%.4f', residual
        )

    async def _enter_long_a(
        self, trader: Trader, pa: float, expected: float, residual: float,
    ) -> None:
        """A underpriced -> buy A (buy YES), sell B (buy NO)."""
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
                        'Structural arb leg A failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing structural arb leg A')

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
                        'Structural arb leg B_NO failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing structural arb leg B_NO')

        if executed_legs > 0:
            self._position_state = 'long_a_short_b'
        self.record_decision(
            ticker_name=f'struc({self._id_a[:10]}|{self._id_b[:10]})',
            action='ENTER_LONG_A',
            executed=executed_legs > 0,
            reasoning=(
                f'A={pa:.4f} < expected={expected:.4f}, '
                f'residual={residual:.4f}  legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'expected': expected, 'residual': residual,
                'executed_legs': executed_legs,
            },
        )
        logger.info(
            'ENTER structural arb: A underpriced, residual=%.4f', residual
        )

    async def _exit(self, trader: Trader, residual: float) -> None:
        """Close all positions -- residual has converged."""
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
                            'Error closing structural arb leg: %s',
                            pos.ticker.symbol,
                        )

        prev = self._position_state
        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'struc({self._id_a[:10]}|{self._id_b[:10]})',
            action='EXIT',
            executed=executed_legs > 0,
            reasoning=f'Residual converged: {residual:.4f} (was {prev})',
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'residual': residual,
            },
        )
        logger.info('EXIT structural arb: residual converged')
