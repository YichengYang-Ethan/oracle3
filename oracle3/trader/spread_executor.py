"""Atomic multi-leg spread execution layer.

Provides ``SpreadExecutor`` that wraps an underlying ``Trader`` and adds
multi-leg execution with partial-fill protection.  If leg *N* fills but
leg *N+1* fails, all previously filled legs are automatically unwound
(reversed) in LIFO order using current market prices.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from oracle3.trader.types import (
    OrderFailureReason,
    PlaceOrderResult,
    TradeSide,
)

if TYPE_CHECKING:
    from oracle3.ticker.ticker import Ticker
    from oracle3.trader.trader import Trader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SpreadLeg:
    """A single leg of a spread order."""

    side: TradeSide
    ticker: Ticker
    price: Decimal
    quantity: Decimal


@dataclass
class SpreadOrderResult:
    """Result of a multi-leg spread execution attempt.

    Attributes:
        success: True when every leg filled successfully.
        leg_results: Per-leg ``PlaceOrderResult`` objects in execution order.
        hedged: True if a partial fill was detected *and* unwinding was
            performed for the already-filled legs.
        unwind_details: Human-readable description of what was unwound
            (empty string when no unwind occurred).
        failure_reason: Short explanation when ``success`` is False.
    """

    success: bool
    leg_results: list[PlaceOrderResult] = field(default_factory=list)
    hedged: bool = False
    unwind_details: str = ""
    failure_reason: str = ""

    @property
    def all_filled(self) -> bool:
        """True when every leg resulted in a fill with quantity > 0."""
        return all(
            r.order is not None and r.order.filled_quantity > 0
            for r in self.leg_results
        )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class SpreadExecutor:
    """Execute multi-leg spread orders with automatic hedge on partial fill.

    The executor wraps a ``Trader`` instance and sequentially places each
    leg of the spread.  If any leg fails after earlier legs have already
    filled, the filled legs are unwound in reverse order to restore a
    hedged (flat) position.

    Usage::

        executor = SpreadExecutor(trader)
        result = await executor.execute_spread([
            SpreadLeg(TradeSide.BUY, ticker_a, ask_a, qty),
            SpreadLeg(TradeSide.SELL, ticker_b, bid_b, qty),
        ])

        if not result.success and result.hedged:
            logger.warning('Spread failed, positions unwound: %s', result.unwind_details)
    """

    def __init__(self, trader: Trader) -> None:
        self._trader = trader

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_spread(
        self,
        legs: list[SpreadLeg],
        *,
        unwind_on_partial: bool = True,
    ) -> SpreadOrderResult:
        """Execute a multi-leg spread order sequentially.

        Legs are executed in the order given.  If a leg fails and
        ``unwind_on_partial`` is True, all previously filled legs are
        unwound (reversed) using current market prices.

        Args:
            legs: Ordered list of spread legs to execute.
            unwind_on_partial: When True, automatically unwind filled
                legs if a subsequent leg fails.

        Returns:
            ``SpreadOrderResult`` with per-leg results and hedge status.
        """
        if not legs:
            return SpreadOrderResult(
                success=False,
                leg_results=[],
                failure_reason="no legs provided",
            )

        results: list[PlaceOrderResult] = []
        filled_legs: list[tuple[SpreadLeg, PlaceOrderResult]] = []

        for i, leg in enumerate(legs):
            leg_label = f"{i + 1}/{len(legs)}"
            logger.info(
                "Spread leg %s: %s %s qty=%s @ %s",
                leg_label,
                leg.side.value,
                leg.ticker.symbol,
                leg.quantity,
                leg.price,
            )

            result = await self._trader.place_order(
                side=leg.side,
                ticker=leg.ticker,
                limit_price=leg.price,
                quantity=leg.quantity,
            )
            results.append(result)

            if result.order is not None and result.order.filled_quantity > 0:
                filled_legs.append((leg, result))
                logger.info(
                    "Spread leg %s filled: qty=%s @ avg %s",
                    leg_label,
                    result.order.filled_quantity,
                    result.order.average_price,
                )
            else:
                # Leg failed
                failure = (
                    result.failure_reason.value
                    if result.failure_reason is not None
                    else "unknown"
                )
                logger.warning(
                    "Spread leg %s failed: %s  (filled so far: %d)",
                    leg_label,
                    failure,
                    len(filled_legs),
                )

                if unwind_on_partial and filled_legs:
                    logger.info(
                        "Unwinding %d previously filled leg(s)", len(filled_legs)
                    )
                    unwind_details = await self._unwind(filled_legs)
                    return SpreadOrderResult(
                        success=False,
                        leg_results=results,
                        hedged=True,
                        unwind_details=unwind_details,
                        failure_reason=f"leg {i + 1} failed: {failure}",
                    )

                return SpreadOrderResult(
                    success=False,
                    leg_results=results,
                    hedged=False,
                    failure_reason=f"leg {i + 1} failed: {failure}",
                )

        logger.info("Spread completed successfully (%d legs)", len(legs))
        return SpreadOrderResult(success=True, leg_results=results)

    async def execute_pair_spread(
        self,
        buy_ticker: Ticker,
        sell_ticker: Ticker,
        quantity: Decimal,
        *,
        unwind_on_partial: bool = True,
    ) -> SpreadOrderResult:
        """Convenience method for a simple 2-leg spread (buy A, sell B).

        Automatically reads the best ask/bid from the trader's
        ``market_data`` for pricing.
        """
        md = self._trader.market_data

        ask = md.get_best_ask(buy_ticker)
        bid = md.get_best_bid(sell_ticker)

        if ask is None:
            return SpreadOrderResult(
                success=False,
                leg_results=[],
                failure_reason=f"no ask available for {buy_ticker.symbol}",
            )
        if bid is None:
            return SpreadOrderResult(
                success=False,
                leg_results=[],
                failure_reason=f"no bid available for {sell_ticker.symbol}",
            )

        legs = [
            SpreadLeg(TradeSide.BUY, buy_ticker, ask.price, quantity),
            SpreadLeg(TradeSide.SELL, sell_ticker, bid.price, quantity),
        ]

        return await self.execute_spread(
            legs, unwind_on_partial=unwind_on_partial
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _unwind(
        self,
        filled_legs: list[tuple[SpreadLeg, PlaceOrderResult]],
    ) -> str:
        """Reverse all filled legs in LIFO order to hedge out the partial spread.

        Uses current market prices for urgency:
        - Selling an unwound BUY  -> use best bid
        - Buying  an unwound SELL -> use best ask

        Returns a human-readable summary of the unwind actions.
        """
        md = self._trader.market_data
        details_parts: list[str] = []

        for leg, result in reversed(filled_legs):
            if result.order is None:
                continue

            filled_qty = result.order.filled_quantity
            if filled_qty <= 0:
                continue

            # Reverse the side
            reverse_side = (
                TradeSide.SELL if leg.side == TradeSide.BUY else TradeSide.BUY
            )

            # Use market price for urgency
            if reverse_side == TradeSide.SELL:
                bid = md.get_best_bid(leg.ticker)
                price = bid.price if bid is not None else leg.price
            else:
                ask = md.get_best_ask(leg.ticker)
                price = ask.price if ask is not None else leg.price

            logger.info(
                "Unwinding: %s %s qty=%s @ %s",
                reverse_side.value,
                leg.ticker.symbol,
                filled_qty,
                price,
            )

            try:
                await self._trader.place_order(
                    side=reverse_side,
                    ticker=leg.ticker,
                    limit_price=price,
                    quantity=filled_qty,
                )
                details_parts.append(
                    f"{reverse_side.value} {leg.ticker.symbol} "
                    f"qty={filled_qty} @ {price}"
                )
            except Exception:
                logger.exception(
                    "CRITICAL: Failed to unwind leg %s "
                    "-- MANUAL INTERVENTION REQUIRED",
                    leg.ticker.symbol,
                )
                details_parts.append(
                    f"FAILED unwind {reverse_side.value} {leg.ticker.symbol} "
                    f"qty={filled_qty} -- MANUAL INTERVENTION REQUIRED"
                )

        return "; ".join(details_parts)
