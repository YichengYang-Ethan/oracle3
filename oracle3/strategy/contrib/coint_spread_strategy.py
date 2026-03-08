"""CointSpreadStrategy -- cointegration-based mean reversion spread trading.

For semantic/conditional relations where two markets are cointegrated,
the spread (A - hedge_ratio * B) is stationary and mean-reverting.

Entry: spread deviates beyond entry_mult x std from its mean.
Exit: spread reverts within exit_mult x std of its mean.

The strategy self-calibrates during a warmup phase by computing the
spread mean and standard deviation from live data.

Usage:
    oracle3 engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref oracle3/strategy/contrib/coint_spread_strategy.py:CointSpreadStrategy \\
      --strategy-kwargs-json '{"market_id_a": "...", "market_id_b": "...", "hedge_ratio": 1.0}'
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from decimal import Decimal

from oracle3.events.events import Event, PriceChangeEvent
from oracle3.strategy.quant_strategy import QuantStrategy
from oracle3.ticker.ticker import Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)

# Conservative per-side fee estimate (0.5%)
_FEE_PER_SIDE = Decimal('0.005')


class CointSpreadStrategy(QuantStrategy):
    """Cointegration-based mean reversion on stationary spreads.

    Self-calibrates the spread distribution during a warmup window,
    then trades deviations from the calibrated mean.

    Spread = p(A) - hedge_ratio * p(B)

    Entry: |deviation from mean| > entry_mult * std
    Exit:  |deviation from mean| < exit_mult * std

    Parameters
    ----------
    market_id_a:
        Market/condition identifier for outcome A.
    market_id_b:
        Market/condition identifier for outcome B.
    trade_size:
        Dollar amount per leg.
    hedge_ratio:
        Hedge ratio for the spread (default 1.0).
    entry_mult:
        Entry at mean +/- entry_mult x std (default 2.0).
    exit_mult:
        Exit at mean +/- exit_mult x std (default 0.5).
    warmup:
        Number of spread samples before trading starts (default 200).
    max_position:
        Maximum position size per leg.
    cooldown_seconds:
        Minimum seconds between successive entry attempts.
    fee_rate:
        Per-side fee rate (default 0.005 = 0.5%).
    """

    name = 'coint_spread'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        market_id_a: str = '',
        market_id_b: str = '',
        trade_size: float = 10.0,
        hedge_ratio: float = 1.0,
        entry_mult: float = 2.0,
        exit_mult: float = 0.5,
        warmup: int = 200,
        max_position: float = 100.0,
        cooldown_seconds: float = 120.0,
        fee_rate: float = 0.005,
    ) -> None:
        super().__init__()
        self._id_a = market_id_a
        self._id_b = market_id_b
        self.trade_size = Decimal(str(trade_size))
        self.max_position = Decimal(str(max_position))
        self._hedge_ratio = Decimal(str(hedge_ratio))
        self._entry_mult = entry_mult
        self._exit_mult = exit_mult
        self._warmup_size = warmup
        self.cooldown_seconds = cooldown_seconds
        self.fee_rate = Decimal(str(fee_rate))

        # Calibration state
        self._spread_buffer: deque[float] = deque(maxlen=warmup)
        self._calibrated = False
        self._expected_spread = Decimal('0')
        self._entry_threshold = Decimal('0')
        self._exit_threshold = Decimal('0')

        # Prices
        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None

        # Position state machine: flat -> long_spread / short_spread -> flat
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

    # -- Self-calibration ----------------------------------------------------

    def _calibrate(self) -> None:
        """Compute spread mean and std from warmup buffer."""
        n = len(self._spread_buffer)
        if n < 2:
            return
        mean = sum(self._spread_buffer) / n
        variance = sum((x - mean) ** 2 for x in self._spread_buffer) / n
        std = math.sqrt(variance)

        if std < 1e-8:
            # Zero variance -- no meaningful spread to trade
            self._calibrated = True
            self._expected_spread = Decimal(str(mean))
            self._entry_threshold = Decimal('999')
            self._exit_threshold = Decimal('0')
            logger.info('Warmup: zero variance, no trades possible')
            return

        self._expected_spread = Decimal(str(mean))
        self._entry_threshold = Decimal(str(std * self._entry_mult))
        self._exit_threshold = Decimal(str(std * self._exit_mult))
        self._calibrated = True
        logger.info(
            'Warmup done (%d): mean=%.6f std=%.6f entry=%.6f exit=%.6f',
            n, mean, std,
            float(self._entry_threshold), float(self._exit_threshold),
        )

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

        spread = self._price_a - self._hedge_ratio * self._price_b
        spread_f = float(spread)

        # Warmup phase -- collect samples before trading
        if not self._calibrated:
            self._spread_buffer.append(spread_f)
            if len(self._spread_buffer) >= self._warmup_size:
                self._calibrate()
            return

        # Post-warmup: continue updating rolling buffer for recalibration
        self._spread_buffer.append(spread_f)
        deviation = spread - self._expected_spread

        if self._position_state == 'flat':
            if deviation > self._entry_threshold:
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter_short_spread(trader, deviation)
            elif deviation < -self._entry_threshold:
                now = time.monotonic()
                if now - self._last_entry_time < self.cooldown_seconds:
                    return
                self._last_entry_time = now
                await self._enter_long_spread(trader, deviation)
            else:
                self.record_decision(
                    ticker_name=f'coint({self._id_a[:10]}|{self._id_b[:10]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'spread={spread_f:.4f} dev={float(deviation):.4f} '
                        f'within [{-float(self._entry_threshold):.4f}, '
                        f'{float(self._entry_threshold):.4f}]'
                    ),
                    signal_values={
                        'price_a': float(self._price_a),
                        'price_b': float(self._price_b),
                        'spread': spread_f,
                        'deviation': float(deviation),
                    },
                )
        else:
            # Exit when deviation returns within exit threshold
            if abs(deviation) < self._exit_threshold:
                await self._exit_position(trader, deviation)

    async def _enter_long_spread(
        self, trader: Trader, deviation: Decimal
    ) -> None:
        """Buy A, sell B -- spread is below mean (B overpriced relative to A)."""
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
                        'Coint spread leg A failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing coint spread leg A')

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
                        'Coint spread leg B_NO failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing coint spread leg B_NO')

        if executed_legs > 0:
            self._position_state = 'long_spread'
        self.record_decision(
            ticker_name=f'coint({self._id_a[:10]}|{self._id_b[:10]})',
            action='BUY_SPREAD',
            executed=executed_legs > 0,
            reasoning=(
                f'Spread below mean: dev={float(deviation):.4f}  '
                f'legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
                'executed_legs': executed_legs,
            },
        )
        logger.info('ENTER long_spread: dev=%.4f', deviation)

    async def _enter_short_spread(
        self, trader: Trader, deviation: Decimal
    ) -> None:
        """Sell A, buy B -- spread is above mean (A overpriced relative to B)."""
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
                        'Coint spread leg A_NO failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing coint spread leg A_NO')

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
                        'Coint spread leg B failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('Error placing coint spread leg B')

        if executed_legs > 0:
            self._position_state = 'short_spread'
        self.record_decision(
            ticker_name=f'coint({self._id_a[:10]}|{self._id_b[:10]})',
            action='SELL_SPREAD',
            executed=executed_legs > 0,
            reasoning=(
                f'Spread above mean: dev={float(deviation):.4f}  '
                f'legs={executed_legs}/2'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
                'executed_legs': executed_legs,
            },
        )
        logger.info('ENTER short_spread: dev=%.4f', deviation)

    async def _exit_position(
        self, trader: Trader, deviation: Decimal
    ) -> None:
        """Close both legs -- spread has converged back to mean."""
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
                            'Error closing coint spread leg: %s',
                            pos.ticker.symbol,
                        )

        prev = self._position_state
        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'coint({self._id_a[:10]}|{self._id_b[:10]})',
            action='CLOSE_SPREAD',
            executed=executed_legs > 0,
            reasoning=(
                f'Spread converged: was {prev}, '
                f'dev={float(deviation):.4f}'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
            },
        )
        logger.info('EXIT %s: spread converged, dev=%.4f', prev, deviation)
