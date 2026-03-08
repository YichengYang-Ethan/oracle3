"""Lead-lag strategy — trade the follower when the leader moves.

Detects temporal lead-lag relationships between two markets. When the
leader market makes a significant move, the follower is expected to
converge. We trade the follower in the leader's direction before it
catches up.

Entry: Leader moves > entry_threshold from its rolling mean.
  - Leader moved up   -> buy follower (expect follower to rise)
  - Leader moved down -> short follower / buy follower NO (expect drop)
Exit: Follower has caught up (follower's move tracks leader's), or
      max hold steps exceeded, or spread has collapsed below exit_threshold.

The strategy tracks rolling-window correlation between the two markets
and only trades when correlation exceeds a minimum (ensuring the
lead-lag relationship is currently active).

Usage:
    oracle3 engine run \
      --exchange polymarket --mode paper \
      --strategy-ref oracle3/strategy/contrib/lead_lag_strategy.py:LeadLagStrategy \
      --strategy-kwargs-json '{
          "leader_symbol": "BTC_ABOVE_100K",
          "follower_symbol": "ETH_ABOVE_4K"
      }'
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from decimal import Decimal
from typing import ClassVar

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.quant_strategy import QuantStrategy
from oracle3.ticker.ticker import Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)

# Conservative fee estimate per side (buy + sell round-trip = 2x)
_FEE_PER_SIDE = Decimal('0.005')


class LeadLagStrategy(QuantStrategy):
    """Trade the follower when the leader moves.

    Monitors two correlated markets. When the leader makes a
    statistically significant move from its recent mean, the strategy
    enters a position in the follower expecting convergence.

    Parameters
    ----------
    leader_symbol:
        Symbol (or substring) identifying the leader ticker.
    follower_symbol:
        Symbol (or substring) identifying the follower ticker.
    trade_size:
        Dollar amount per trade leg.
    entry_threshold:
        Minimum absolute leader move (as price fraction, e.g. 0.03 = 3 cents)
        from its rolling mean to trigger entry.
    exit_threshold:
        When the follower has caught up by this fraction of the leader's
        original move (0.0-1.0), trigger exit. Default 0.5 = 50% reversion.
    warmup:
        Number of leader price observations to accumulate before trading.
        Used to compute the leader's rolling mean baseline.
    max_hold:
        Maximum number of follower price updates to hold a position before
        forced exit (timeout protection).
    cooldown_seconds:
        Minimum seconds between consecutive entries (prevents over-trading).
    min_correlation:
        Minimum rolling correlation between leader and follower required
        before entering a trade. Set to 0.0 to disable correlation gating.
    correlation_window:
        Number of paired observations used to compute rolling correlation.
    lead_lag_steps:
        Expected number of follower observations that lag behind the leader.
        Affects correlation computation (leader[t] vs follower[t + lag]).
        Set to 0 for contemporaneous correlation.
    """

    name: ClassVar[str] = 'lead_lag'
    version: ClassVar[str] = '1.0.0'
    author: ClassVar[str] = 'oracle3'

    def __init__(
        self,
        leader_symbol: str = '',
        follower_symbol: str = '',
        trade_size: float = 10.0,
        entry_threshold: float = 0.03,
        exit_threshold: float = 0.5,
        warmup: int = 50,
        max_hold: int = 100,
        cooldown_seconds: float = 30.0,
        min_correlation: float = 0.3,
        correlation_window: int = 30,
        lead_lag_steps: int = 0,
    ) -> None:
        super().__init__()
        self._leader_symbol = leader_symbol
        self._follower_symbol = follower_symbol
        self.trade_size = Decimal(str(trade_size))
        self.entry_threshold = Decimal(str(entry_threshold))
        self._exit_threshold = exit_threshold
        self._warmup_size = warmup
        self._max_hold = max_hold
        self._cooldown_seconds = cooldown_seconds
        self._min_correlation = min_correlation
        self._correlation_window = correlation_window
        self._lead_lag_steps = max(0, lead_lag_steps)

        # ---- Price tracking ----
        # Rolling window of leader prices for mean/std calculation
        self._leader_prices: deque[float] = deque(maxlen=warmup)
        # Full history windows for correlation (need more than warmup)
        self._leader_corr_prices: deque[float] = deque(
            maxlen=correlation_window + lead_lag_steps + 10
        )
        self._follower_corr_prices: deque[float] = deque(
            maxlen=correlation_window + lead_lag_steps + 10
        )

        # Latest observed prices
        self._leader_price: Decimal | None = None
        self._follower_price: Decimal | None = None

        # Latest best bid/ask from order books
        self._leader_best_bid: Decimal | None = None
        self._leader_best_ask: Decimal | None = None
        self._follower_best_bid: Decimal | None = None
        self._follower_best_ask: Decimal | None = None

        # Ticker object references (resolved on first match)
        self._leader_ticker: Ticker | None = None
        self._follower_ticker: Ticker | None = None

        # ---- Position state machine ----
        # States: 'flat' | 'long_follower' | 'short_follower'
        self._position_state: str = 'flat'
        self._entry_leader_price: float = 0.0
        self._entry_follower_price: float = 0.0
        self._entry_leader_move: float = 0.0  # leader's deviation at entry
        self._hold_count: int = 0  # follower updates since entry

        # ---- Cooldown ----
        self._last_entry_time: float = 0.0

    # ------------------------------------------------------------------
    # Symbol matching
    # ------------------------------------------------------------------

    def _matches(self, ticker: Ticker, target_symbol: str) -> bool:
        """Check if a ticker matches the target symbol (substring match)."""
        if not target_symbol:
            return False
        symbol = ticker.symbol
        name = getattr(ticker, 'name', '') or ''
        market_id = getattr(ticker, 'market_id', '') or ''
        token_id = getattr(ticker, 'token_id', '') or ''
        # Exact match first, then substring
        for candidate in (symbol, name, market_id, token_id):
            if not candidate:
                continue
            if target_symbol == candidate or target_symbol in candidate or candidate in target_symbol:
                return True
        return False

    def _is_no_side(self, ticker: Ticker) -> bool:
        """Return True if this ticker represents a NO / complement side."""
        symbol = ticker.symbol
        name = getattr(ticker, 'name', '') or ''
        return symbol.endswith('_NO') or name.startswith('NO ')

    # ------------------------------------------------------------------
    # Correlation computation
    # ------------------------------------------------------------------

    def _compute_rolling_correlation(self) -> float | None:
        """Compute Pearson correlation between leader and follower prices.

        If lead_lag_steps > 0, we correlate leader[t] with
        follower[t + lead_lag_steps] to capture the temporal lag.

        Returns None if insufficient data.
        """
        lag = self._lead_lag_steps
        n_needed = self._correlation_window + lag

        leader_list = list(self._leader_corr_prices)
        follower_list = list(self._follower_corr_prices)

        if len(leader_list) < n_needed or len(follower_list) < self._correlation_window:
            return None

        # Align: leader from [-(n_needed) : -lag] if lag > 0, else last window
        if lag > 0:
            leader_window = leader_list[-(self._correlation_window + lag): -lag]
        else:
            leader_window = leader_list[-self._correlation_window:]
        follower_window = follower_list[-self._correlation_window:]

        if len(leader_window) != self._correlation_window or len(follower_window) != self._correlation_window:
            return None

        n = self._correlation_window
        mean_l = sum(leader_window) / n
        mean_f = sum(follower_window) / n

        cov = 0.0
        var_l = 0.0
        var_f = 0.0
        for i in range(n):
            dl = leader_window[i] - mean_l
            df = follower_window[i] - mean_f
            cov += dl * df
            var_l += dl * dl
            var_f += df * df

        if var_l < 1e-12 or var_f < 1e-12:
            return 0.0

        return cov / math.sqrt(var_l * var_f)

    # ------------------------------------------------------------------
    # Order book helpers
    # ------------------------------------------------------------------

    def _update_book_prices(self, ticker: Ticker, trader: Trader, is_leader: bool) -> None:
        """Refresh best bid/ask from the trader's market data manager."""
        bid_level = trader.market_data.get_best_bid(ticker)
        ask_level = trader.market_data.get_best_ask(ticker)
        if is_leader:
            self._leader_best_bid = bid_level.price if bid_level else None
            self._leader_best_ask = ask_level.price if ask_level else None
        else:
            self._follower_best_bid = bid_level.price if bid_level else None
            self._follower_best_ask = ask_level.price if ask_level else None

    def _get_mid_price(self, best_bid: Decimal | None, best_ask: Decimal | None) -> Decimal | None:
        """Compute mid-price from bid/ask. Returns None if either is missing."""
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / Decimal('2')
        return best_bid or best_ask

    # ------------------------------------------------------------------
    # Fee-aware edge calculation
    # ------------------------------------------------------------------

    def _net_edge(self, gross_move: float) -> float:
        """Compute net edge after round-trip fees (buy + sell)."""
        return abs(gross_move) - float(_FEE_PER_SIDE * 2)

    # ------------------------------------------------------------------
    # Main event handler
    # ------------------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:  # noqa: C901
        if self.is_paused():
            return

        # Only process price-bearing events
        if not isinstance(event, (PriceChangeEvent, OrderBookEvent)):
            return

        ticker: Ticker = event.ticker
        price: Decimal | None = None

        if isinstance(event, PriceChangeEvent):
            price = event.price
        elif isinstance(event, OrderBookEvent):
            price = event.price if event.price > 0 else None

        if price is None:
            return

        # Skip NO-side tickers (we only track YES prices)
        if self._is_no_side(ticker):
            return

        # Identify whether this event is for the leader or the follower
        is_leader = self._matches(ticker, self._leader_symbol)
        is_follower = self._matches(ticker, self._follower_symbol)

        if not is_leader and not is_follower:
            return

        # Update prices and order book references
        if is_leader:
            self._leader_price = price
            self._leader_prices.append(float(price))
            self._leader_corr_prices.append(float(price))
            if self._leader_ticker is None:
                self._leader_ticker = ticker
                logger.info(
                    'LeadLag: matched leader ticker %s', ticker.symbol[:30]
                )
            self._update_book_prices(ticker, trader, is_leader=True)

        if is_follower:
            self._follower_price = price
            self._follower_corr_prices.append(float(price))
            if self._follower_ticker is None:
                self._follower_ticker = ticker
                logger.info(
                    'LeadLag: matched follower ticker %s', ticker.symbol[:30]
                )
            self._update_book_prices(ticker, trader, is_leader=False)
            # Count follower updates while in a position
            if self._position_state != 'flat':
                self._hold_count += 1

        # Need warmup for leader baseline
        if len(self._leader_prices) < self._warmup_size:
            return
        if self._leader_price is None or self._follower_price is None:
            return

        # Compute leader deviation from rolling mean
        leader_mean = sum(self._leader_prices) / len(self._leader_prices)
        leader_move = float(self._leader_price) - leader_mean

        # ---- State machine ----
        if self._position_state == 'flat':
            # Only trigger entry on leader events
            if is_leader:
                await self._evaluate_entry(trader, leader_move, leader_mean)
        else:
            # Check exit on follower updates
            if is_follower:
                await self._check_exit(trader, leader_move)

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    async def _evaluate_entry(
        self, trader: Trader, leader_move: float, leader_mean: float
    ) -> None:
        """Decide whether to enter a position based on leader's move."""
        abs_move = abs(leader_move)

        # Check entry threshold
        if abs_move <= float(self.entry_threshold):
            self.record_decision(
                ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
                action='HOLD',
                executed=False,
                reasoning=(
                    f'leader_move={leader_move:.4f} '
                    f'< threshold={float(self.entry_threshold):.4f}'
                ),
                signal_values={
                    'leader': float(self._leader_price or 0),
                    'follower': float(self._follower_price or 0),
                    'leader_mean': leader_mean,
                    'leader_move': leader_move,
                },
            )
            return

        # Check fee-aware edge
        net = self._net_edge(leader_move)
        if net <= 0:
            self.record_decision(
                ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
                action='HOLD',
                executed=False,
                reasoning=(
                    f'net_edge={net:.4f} <= 0 after fees '
                    f'(gross={abs_move:.4f}, fees={float(_FEE_PER_SIDE * 2):.4f})'
                ),
                signal_values={
                    'leader_move': leader_move,
                    'net_edge': net,
                    'gross_edge': abs_move,
                },
            )
            return

        # Check correlation gate
        if self._min_correlation > 0:
            corr = self._compute_rolling_correlation()
            if corr is not None and corr < self._min_correlation:
                self.record_decision(
                    ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'correlation={corr:.3f} < min={self._min_correlation:.3f}'
                    ),
                    signal_values={
                        'leader_move': leader_move,
                        'correlation': corr,
                        'net_edge': net,
                    },
                )
                return

        # Check cooldown
        now = time.monotonic()
        if now - self._last_entry_time < self._cooldown_seconds:
            self.record_decision(
                ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
                action='HOLD',
                executed=False,
                reasoning=(
                    f'cooldown: {now - self._last_entry_time:.1f}s '
                    f'< {self._cooldown_seconds:.1f}s'
                ),
                signal_values={'leader_move': leader_move},
            )
            return

        # --- Enter position ---
        if leader_move > 0:
            await self._enter_long(trader, leader_move)
        else:
            await self._enter_short(trader, leader_move)

        self._last_entry_time = now

    async def _enter_long(self, trader: Trader, leader_move: float) -> None:
        """Leader moved up -> buy follower YES (expect follower to rise)."""
        follower_ticker = self._find_ticker(trader, self._follower_symbol, yes=True)
        executed = False

        if follower_ticker and self._follower_price is not None:
            # Use best ask (what we'd pay to buy) if available, else use last price
            buy_price = self._follower_best_ask or self._follower_price
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=follower_ticker,
                    limit_price=buy_price,
                    quantity=self.trade_size,
                )
                executed = result.failure_reason is None
                if result.failure_reason:
                    logger.warning(
                        'LeadLag: long entry failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('LeadLag: long entry error')

        if executed:
            self._position_state = 'long_follower'
        self._entry_leader_price = float(self._leader_price or 0)
        self._entry_follower_price = float(self._follower_price or 0)
        self._entry_leader_move = leader_move
        self._hold_count = 0

        corr = self._compute_rolling_correlation()
        corr_str = f', corr={corr:.3f}' if corr is not None else ''
        self.record_decision(
            ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
            action='BUY_YES',
            executed=executed,
            reasoning=(
                f'Leader UP {leader_move:.4f} > threshold, '
                f'net_edge={self._net_edge(leader_move):.4f}'
                f'{corr_str}'
            ),
            signal_values={
                'leader': self._entry_leader_price,
                'follower': self._entry_follower_price,
                'leader_move': leader_move,
                'net_edge': self._net_edge(leader_move),
                'correlation': corr if corr is not None else 0.0,
            },
        )
        logger.info(
            'ENTER long_follower: leader_move=%.4f net_edge=%.4f',
            leader_move, self._net_edge(leader_move),
        )

    async def _enter_short(self, trader: Trader, leader_move: float) -> None:
        """Leader moved down -> sell follower / buy follower NO."""
        follower_no_ticker = self._find_ticker(trader, self._follower_symbol, yes=False)
        executed = False

        if follower_no_ticker and self._follower_price is not None:
            # NO price = 1 - YES price; use best ask of the NO side if available
            no_price = Decimal('1') - self._follower_price
            try:
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=follower_no_ticker,
                    limit_price=no_price,
                    quantity=self.trade_size,
                )
                executed = result.failure_reason is None
                if result.failure_reason:
                    logger.warning(
                        'LeadLag: short entry failed: %s', result.failure_reason
                    )
            except Exception:
                logger.exception('LeadLag: short entry error')

        if executed:
            self._position_state = 'short_follower'
        self._entry_leader_price = float(self._leader_price or 0)
        self._entry_follower_price = float(self._follower_price or 0)
        self._entry_leader_move = leader_move
        self._hold_count = 0

        corr = self._compute_rolling_correlation()
        corr_str = f', corr={corr:.3f}' if corr is not None else ''
        self.record_decision(
            ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
            action='BUY_NO',
            executed=executed,
            reasoning=(
                f'Leader DOWN {leader_move:.4f} > threshold, '
                f'net_edge={self._net_edge(leader_move):.4f}'
                f'{corr_str}'
            ),
            signal_values={
                'leader': self._entry_leader_price,
                'follower': self._entry_follower_price,
                'leader_move': leader_move,
                'net_edge': self._net_edge(leader_move),
                'correlation': corr if corr is not None else 0.0,
            },
        )
        logger.info(
            'ENTER short_follower: leader_move=%.4f net_edge=%.4f',
            leader_move, self._net_edge(leader_move),
        )

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    async def _check_exit(self, trader: Trader, leader_move: float) -> None:
        """Check exit conditions on follower update.

        Exit triggers:
        1. Follower catch-up: follower moved in the expected direction by
           >= exit_threshold fraction of the leader's entry move.
        2. Timeout: hold_count >= max_hold.
        """
        current_follower = float(self._follower_price or 0)
        follower_move = current_follower - self._entry_follower_price

        # Compute catch-up ratio relative to the leader's move at entry time
        if abs(self._entry_leader_move) > 1e-8:
            catchup_ratio = follower_move / self._entry_leader_move
        else:
            catchup_ratio = 0.0

        # For short positions, we expect negative follower_move matching
        # negative leader_move, so catchup_ratio should be positive
        # when the follower has indeed followed the leader down.

        reason: str | None = None
        if catchup_ratio >= self._exit_threshold:
            reason = 'catchup'
        elif self._hold_count >= self._max_hold:
            reason = 'timeout'

        if reason is None:
            # Not yet time to exit — log a hold decision periodically
            if self._hold_count % 20 == 0:
                self.record_decision(
                    ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'holding: catchup={catchup_ratio:.3f} '
                        f'(need {self._exit_threshold:.2f}), '
                        f'hold={self._hold_count}/{self._max_hold}'
                    ),
                    signal_values={
                        'follower_move': follower_move,
                        'catchup_ratio': catchup_ratio,
                        'hold_count': float(self._hold_count),
                    },
                )
            return

        # --- Execute exit ---
        exit_executed = False
        for pos in trader.position_manager.positions.values():
            if pos.quantity > 0:
                # Check if this position is related to our follower
                pos_symbol = pos.ticker.symbol
                if not (
                    self._matches(pos.ticker, self._follower_symbol)
                    or pos_symbol.endswith('_NO')
                    and self._follower_symbol in pos_symbol
                ):
                    continue

                # Use best bid (what we can sell at)
                best_bid = trader.market_data.get_best_bid(pos.ticker)
                if best_bid:
                    try:
                        result = await trader.place_order(
                            side=TradeSide.SELL,
                            ticker=pos.ticker,
                            limit_price=best_bid.price,
                            quantity=pos.quantity,
                        )
                        if result.failure_reason is None:
                            exit_executed = True
                        else:
                            logger.warning(
                                'LeadLag exit failed: %s - %s',
                                pos.ticker.symbol, result.failure_reason,
                            )
                    except Exception:
                        logger.exception(
                            'LeadLag exit error: %s', pos.ticker.symbol
                        )

        # Compute PnL estimate
        if self._position_state == 'long_follower':
            pnl_estimate = follower_move - float(_FEE_PER_SIDE * 2)
        else:
            # Short: we bought NO at (1 - entry_yes), now selling NO.
            # Profit if YES went down (NO went up).
            pnl_estimate = -follower_move - float(_FEE_PER_SIDE * 2)

        self.record_decision(
            ticker_name=f'lag({self._leader_symbol[:15]}→{self._follower_symbol[:15]})',
            action='CLOSE_POSITION',
            executed=exit_executed,
            reasoning=(
                f'EXIT({reason}): follower_move={follower_move:.4f} '
                f'catchup={catchup_ratio:.3f} hold={self._hold_count} '
                f'pnl_est={pnl_estimate:.4f}'
            ),
            signal_values={
                'follower_move': follower_move,
                'catchup_ratio': catchup_ratio,
                'hold_count': float(self._hold_count),
                'pnl_estimate': pnl_estimate,
                'exit_reason': 1.0 if reason == 'catchup' else 2.0,
            },
        )
        logger.info(
            'EXIT %s (%s): catchup=%.3f hold=%d pnl_est=%.4f',
            self._position_state, reason, catchup_ratio,
            self._hold_count, pnl_estimate,
        )
        self._position_state = 'flat'

    # ------------------------------------------------------------------
    # Ticker resolution
    # ------------------------------------------------------------------

    def _find_ticker(
        self, trader: Trader, target_symbol: str, *, yes: bool = True
    ) -> Ticker | None:
        """Find a matching ticker in the trader's order book universe.

        Args:
            trader: The active trader instance.
            target_symbol: Symbol or substring to match.
            yes: If True, return the YES-side ticker. If False, return the
                 NO-side complement.
        """
        for ticker in trader.market_data.order_books:
            is_no = self._is_no_side(ticker)
            # Skip complement side if we want YES, and vice versa
            if yes and is_no:
                continue
            if not yes and not is_no:
                # Try to get the NO ticker from the YES ticker
                no_ticker = getattr(ticker, 'get_no_ticker', lambda: None)()
                if no_ticker is not None and self._matches(ticker, target_symbol):
                    return no_ticker
                continue

            if self._matches(ticker, target_symbol):
                if not yes:
                    # We found a NO-side ticker that matches
                    return ticker
                return ticker

        # Fallback for NO: if we didn't find a direct NO ticker,
        # find the YES ticker and derive NO from it
        if not yes:
            for ticker in trader.market_data.order_books:
                if self._is_no_side(ticker):
                    continue
                if self._matches(ticker, target_symbol):
                    return getattr(ticker, 'get_no_ticker', lambda: None)()

        return None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def position_state(self) -> str:
        """Current position state for external inspection."""
        return self._position_state

    @property
    def correlation(self) -> float | None:
        """Current rolling correlation for external inspection."""
        return self._compute_rolling_correlation()

    @property
    def leader_observations(self) -> int:
        """Number of leader price observations collected."""
        return len(self._leader_prices)

    @property
    def follower_observations(self) -> int:
        """Number of follower correlation observations collected."""
        return len(self._follower_corr_prices)
