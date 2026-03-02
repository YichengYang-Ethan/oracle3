"""Adaptive On-Chain Strategy — auto-tunes parameters via feedback loop.

Two signal sources (no LLM):
1. Order book imbalance (OrderBookEvent) — range [-1, 1]
2. Price momentum via EMA crossover — normalized to [-1, 1]

Both OB and price events feed the EMA so momentum stays fresh.

Every ``adapt_window`` executed trades, recalculates per-signal win rates
and adjusts signal weights and entry thresholds.

Composite score:
    score = w_ob * ob_signal + w_momentum * mom_signal
    score >  composite_threshold → BUY
    score < -composite_threshold → SELL
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

from ..quant_strategy import QuantStrategy

logger = logging.getLogger(__name__)


class AdaptiveOnChainStrategy(QuantStrategy):
    """Two-signal adaptive strategy: OB imbalance + EMA momentum."""

    name = 'adaptive_onchain'
    version = '0.2.0'
    author = 'Yicheng Yang'

    def __init__(
        self,
        # --- signal thresholds ---
        composite_threshold: float = 0.10,
        # --- EMA windows ---
        ema_short_period: int = 5,
        ema_long_period: int = 20,
        # --- signal weights (sum to 1) ---
        w_ob: float = 0.6,
        w_momentum: float = 0.4,
        # --- momentum normalization ---
        momentum_scale: float = 20.0,
        # --- position / risk ---
        position_size: float = 10.0,
        max_position_pct: float = 0.10,
        stop_loss_pct: float = 0.08,
        max_hold_events: int = 25,
        # --- adaptation ---
        adapt_window: int = 5,
    ) -> None:
        super().__init__()
        self.composite_threshold = composite_threshold

        self.ema_short_period = ema_short_period
        self.ema_long_period = ema_long_period

        self.w_ob = w_ob
        self.w_momentum = w_momentum
        self.momentum_scale = momentum_scale

        self.position_size = Decimal(str(position_size))
        self.max_position_pct = max_position_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold_events = max_hold_events

        self.adapt_window = adapt_window

        # --- internal state ---
        self._emas: dict[str, dict[str, float]] = {}
        self._ob_signal: dict[str, float] = {}
        self._mom_signal: dict[str, float] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._closing_in_progress: set[str] = set()

        # Adaptation tracking
        self._trade_outcomes: list[dict[str, Any]] = []
        self._adapt_trade_count: int = 0

        # Cross-market reference prices for arb detection (Feature #1)
        self._reference_prices: dict[str, dict[str, float]] = {}
        self._trade_count: int = 0

        # On-chain feature hooks (set externally by CLI)
        self.flash_loan_handler: Any | None = None
        self.atomic_trader: Any | None = None
        self.coordinator: Any | None = None
        self.reputation_manager: Any | None = None

        # Persistence: local file + optional on-chain checkpoint
        self._params_file = Path.home() / '.oracle3' / 'learned_params.json'
        self._onchain_logger: Any | None = None  # set externally for chain writes
        self._adapt_epoch: int = 0  # counts how many times _adapt_parameters ran
        self._onchain_checkpoint_interval: int = 5  # write chain every N epochs
        self._load_learned_params()

    # ------------------------------------------------------------------
    # Main event router
    # ------------------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        if isinstance(event, OrderBookEvent):
            self._handle_orderbook(event, trader)
            ticker = event.ticker
        elif isinstance(event, PriceChangeEvent):
            self._handle_price(event)
            ticker = event.ticker
        else:
            return
        sym = ticker.symbol

        if sym in self._entries:
            self._entries[sym]['events'] += 1

        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity > 0

        if has_position:
            await self._maybe_exit(ticker, trader, position)
        else:
            await self._maybe_enter(ticker, trader)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _handle_orderbook(self, event: OrderBookEvent, trader: Trader) -> None:
        ticker = event.ticker
        ob = trader.market_data.order_books.get(ticker)
        if ob is None:
            return

        bids = ob.get_bids(5)
        asks = ob.get_asks(5)
        bid_vol = float(sum(lv.size for lv in bids))
        ask_vol = float(sum(lv.size for lv in asks))
        total_vol = bid_vol + ask_vol
        if total_vol == 0:
            return

        self._ob_signal[ticker.symbol] = (bid_vol - ask_vol) / total_vol

        # Also feed mid-price into EMA so momentum stays fresh
        bid_q = trader.market_data.get_best_bid(ticker)
        ask_q = trader.market_data.get_best_ask(ticker)
        if bid_q and ask_q:
            mid = (float(bid_q.price) + float(ask_q.price)) / 2.0
            self._reference_prices.setdefault(ticker.symbol, {})['dflow'] = mid
            self._update_ema(ticker.symbol, mid)

    def _handle_price(self, event: PriceChangeEvent) -> None:
        self._reference_prices.setdefault(event.ticker.symbol, {})['coingecko'] = float(event.price)
        self._update_ema(event.ticker.symbol, float(event.price))

    def _update_ema(self, sym: str, price: float) -> None:
        ema_state = self._emas.get(sym)
        if ema_state is None:
            self._emas[sym] = {'short': price, 'long': price}
            self._mom_signal[sym] = 0.0
            return

        alpha_s = 2.0 / (self.ema_short_period + 1)
        alpha_l = 2.0 / (self.ema_long_period + 1)
        ema_state['short'] = alpha_s * price + (1 - alpha_s) * ema_state['short']
        ema_state['long'] = alpha_l * price + (1 - alpha_l) * ema_state['long']

        ema_long = ema_state['long']
        if abs(ema_long) < 1e-10:
            self._mom_signal[sym] = 0.0
        else:
            # Raw momentum is tiny (~0.01), scale up to [-1, 1] range
            raw = (ema_state['short'] - ema_long) / abs(ema_long)
            self._mom_signal[sym] = max(-1.0, min(1.0, raw * self.momentum_scale))

    # ------------------------------------------------------------------
    # Composite scoring
    # ------------------------------------------------------------------

    def _compute_score(self, sym: str) -> float:
        ob = self._ob_signal.get(sym, 0.0)
        mom = self._mom_signal.get(sym, 0.0)
        return self.w_ob * ob + self.w_momentum * mom

    def _dominant_signal(self, sym: str) -> str:
        ob = abs(self.w_ob * self._ob_signal.get(sym, 0.0))
        mom = abs(self.w_momentum * self._mom_signal.get(sym, 0.0))
        return 'ob' if ob >= mom else 'momentum'

    # ------------------------------------------------------------------
    # Cross-market arbitrage detection (Feature #1)
    # ------------------------------------------------------------------

    def find_arbitrage_opportunities(self) -> list[dict[str, Any]]:
        """Compare DFlow order-book mid-prices vs CoinGecko spot prices.

        Returns opportunities where the absolute spread exceeds 0.5%.
        """
        opportunities: list[dict[str, Any]] = []
        for sym, prices in self._reference_prices.items():
            dflow_mid = prices.get('dflow')
            cg_price = prices.get('coingecko')
            if dflow_mid is None or cg_price is None or cg_price == 0:
                continue
            spread_pct = (dflow_mid - cg_price) / cg_price
            if abs(spread_pct) > 0.005:
                opportunities.append({
                    'symbol': sym,
                    'dflow_mid': round(dflow_mid, 6),
                    'coingecko_price': round(cg_price, 6),
                    'spread_pct': round(spread_pct * 100, 4),
                    'direction': 'buy_dflow' if spread_pct < 0 else 'sell_dflow',
                })
        return opportunities

    # ------------------------------------------------------------------
    # On-chain feature calls (Features #7, #8)
    # ------------------------------------------------------------------

    async def _attempt_onchain_features(
        self, sym: str, action: str, price: float,
    ) -> None:
        """Call flash-loan and atomic-trader modules every 5th trade."""
        self._trade_count += 1
        if self._trade_count % 5 != 0:
            return

        if self.flash_loan_handler is not None:
            try:
                await self.flash_loan_handler.execute_flash_arbitrage(
                    market_a=f'{sym}/dflow',
                    market_b=f'{sym}/jupiter',
                    amount=float(self.position_size),
                )
            except Exception:
                logger.debug('Flash loan attempt failed', exc_info=True)

        if self.atomic_trader is not None:
            try:
                hedge_side = 'sell' if action == 'BUY' else 'buy'
                await self.atomic_trader.place_hedged_order(
                    prediction_market_symbol=sym,
                    prediction_side=action.lower(),
                    prediction_qty=float(self.position_size),
                    prediction_price=price,
                    hedge_instrument='jupiter_swap',
                    hedge_ticker=sym,
                    hedge_side=hedge_side,
                    hedge_qty=float(self.position_size),
                    hedge_price=price,
                )
            except Exception:
                logger.debug('Atomic trader attempt failed', exc_info=True)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def _maybe_enter(self, ticker: Any, trader: Trader) -> None:
        sym = ticker.symbol
        score = self._compute_score(sym)

        signal_vals = {
            'composite': score,
            'ob_signal': self._ob_signal.get(sym, 0.0),
            'mom_signal': self._mom_signal.get(sym, 0.0),
            'w_ob': self.w_ob,
            'w_momentum': self.w_momentum,
        }

        if score > self.composite_threshold:
            side, action = TradeSide.BUY, 'BUY'
        elif score < -self.composite_threshold:
            side, action = TradeSide.SELL, 'SELL'
        else:
            self.record_decision(
                ticker_name=ticker.name or sym,
                action='HOLD',
                executed=False,
                reasoning=f'score={score:.4f}, threshold=±{self.composite_threshold}',
                signal_values=signal_vals,
            )
            return

        quote = (
            trader.market_data.get_best_ask(ticker)
            if side == TradeSide.BUY
            else trader.market_data.get_best_bid(ticker)
        )
        if quote is None:
            return

        quantity = self._cap_quantity(self.position_size, ticker, trader)
        if quantity <= Decimal('0'):
            self.record_decision(
                ticker_name=ticker.name or sym, action=action,
                executed=False, reasoning='capped qty=0', signal_values=signal_vals,
            )
            return

        result = await trader.place_order(
            side=side, ticker=ticker, limit_price=quote.price, quantity=quantity,
        )
        executed = result.order is not None
        if executed:
            self._entries[sym] = {
                'price': float(quote.price), 'events': 0,
                'signal': self._dominant_signal(sym), 'side': action,
            }
            await self._attempt_onchain_features(sym, action, float(quote.price))
        self.record_decision(
            ticker_name=ticker.name or sym, action=action, executed=executed,
            confidence=min(abs(score), 1.0),
            reasoning=f'score={score:.4f}', signal_values=signal_vals,
        )

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _get_mid_price(self, ticker: Any, trader: Trader) -> float | None:
        """Return mid-price from best bid/ask, or None if unavailable."""
        bid = trader.market_data.get_best_bid(ticker)
        ask = trader.market_data.get_best_ask(ticker)
        if bid is None and ask is None:
            return None
        if bid is not None and ask is not None:
            return (float(bid.price) + float(ask.price)) / 2.0
        level = bid if bid is not None else ask
        assert level is not None
        return float(level.price)

    def _determine_exit_action(self, sym: str, entry_info: dict[str, Any], pnl_pct: float) -> str | None:
        """Return exit action string or None if no exit condition met."""
        if pnl_pct < -self.stop_loss_pct:
            return 'CLOSE_SL'
        if entry_info['events'] >= self.max_hold_events:
            return 'CLOSE_TIMEOUT'
        if pnl_pct > self.stop_loss_pct:
            return 'CLOSE_TP'
        score = self._compute_score(sym)
        side = entry_info.get('side')
        if side == 'BUY' and score < -self.composite_threshold * 0.5:
            return 'CLOSE_REVERSAL'
        if side == 'SELL' and score > self.composite_threshold * 0.5:
            return 'CLOSE_REVERSAL'
        return None

    async def _maybe_exit(self, ticker: Any, trader: Trader, position: Any) -> None:
        sym = ticker.symbol
        if sym in self._closing_in_progress:
            return

        entry_info = self._entries.get(sym)
        if entry_info is None:
            return

        mid = self._get_mid_price(ticker, trader)
        if mid is None:
            return

        entry_price = entry_info['price']
        events_held = entry_info['events']

        if entry_info.get('side') == 'SELL':
            pnl_pct = (entry_price - mid) / entry_price if entry_price else 0.0
        else:
            pnl_pct = (mid - entry_price) / entry_price if entry_price else 0.0

        action = self._determine_exit_action(sym, entry_info, pnl_pct)
        if action is None:
            self.record_decision(
                ticker_name=ticker.name or sym, action='HOLD', executed=False,
                reasoning=f'pnl={pnl_pct:+.4f}, events={events_held}',
                signal_values={'pnl_pct': pnl_pct, 'events_held': float(events_held)},
            )
            return

        self._closing_in_progress.add(sym)
        try:
            sell_quote = trader.market_data.get_best_bid(ticker)
            if sell_quote is None:
                return
            result = await trader.place_order(
                side=TradeSide.SELL, ticker=ticker,
                limit_price=sell_quote.price, quantity=position.quantity,
            )
            executed = result.order is not None
            if executed:
                prev_epoch = self._adapt_epoch
                self._record_trade_outcome(entry_info.get('signal', 'ob'), pnl_pct > 0, pnl_pct)
                if self.reputation_manager is not None:
                    self.reputation_manager.record_trade_result(pnl_pct)
                # On-chain checkpoint when adaptation epoch advanced
                if (self._adapt_epoch > prev_epoch
                        and self._adapt_epoch % self._onchain_checkpoint_interval == 0):
                    await self._checkpoint_onchain()
                self._entries.pop(sym, None)
            self.record_decision(
                ticker_name=ticker.name or sym, action=action, executed=executed,
                reasoning=f'{action}: pnl={pnl_pct:+.4f}, events={events_held}',
                signal_values={'pnl_pct': pnl_pct, 'events_held': float(events_held)},
            )
        finally:
            self._closing_in_progress.discard(sym)

    # ------------------------------------------------------------------
    # Adaptation
    # ------------------------------------------------------------------

    def _record_trade_outcome(self, signal: str, profitable: bool, pnl_pct: float = 0.0) -> None:
        self._trade_outcomes.append({
            'signal': signal, 'profitable': profitable, 'pnl_pct': pnl_pct,
        })
        self._adapt_trade_count += 1
        if self._adapt_trade_count >= self.adapt_window:
            self._adapt_parameters()
            self._adapt_trade_count = 0

    def _adapt_parameters(self) -> None:
        recent = self._trade_outcomes[-self.adapt_window:]
        if not recent:
            return

        total_wins = sum(1 for o in recent if o['profitable'])
        overall_wr = total_wins / len(recent)
        logger.info('ADAPT [%d trades]: overall_wr=%.1f%%', len(recent), overall_wr * 100)

        self._adapt_signal_weights(recent)
        self._adapt_threshold(overall_wr)
        self._adapt_stop_loss(recent, overall_wr)
        self._adapt_hold_duration(overall_wr)
        self._adapt_epoch += 1

        logger.info(
            '  → w_ob=%.2f w_mom=%.2f threshold=%.3f sl=%.3f hold=%d epoch=%d',
            self.w_ob, self.w_momentum, self.composite_threshold,
            self.stop_loss_pct, self.max_hold_events, self._adapt_epoch,
        )

        # Persist locally every adaptation
        self._save_learned_params()

    def _adapt_signal_weights(self, recent: list[dict[str, Any]]) -> None:
        """Adjust per-signal weights based on win rates."""
        signal_stats: dict[str, dict[str, int]] = {}
        for outcome in recent:
            sig = outcome['signal']
            s = signal_stats.setdefault(sig, {'wins': 0, 'total': 0})
            s['total'] += 1
            if outcome['profitable']:
                s['wins'] += 1

        for sig, s in signal_stats.items():
            wr = s['wins'] / s['total'] if s['total'] else 0.5
            mult = 1.1 if wr > 0.55 else (0.85 if wr < 0.45 else 1.0)
            if sig == 'ob':
                self.w_ob = max(0.1, min(0.9, self.w_ob * mult))
            elif sig == 'momentum':
                self.w_momentum = max(0.1, min(0.9, self.w_momentum * mult))
            logger.info('  %s: wr=%.0f%% (%d/%d) ×%.2f', sig, wr * 100, s['wins'], s['total'], mult)

        w_sum = self.w_ob + self.w_momentum
        if w_sum > 0:
            self.w_ob /= w_sum
            self.w_momentum /= w_sum

    def _adapt_threshold(self, overall_wr: float) -> None:
        """Raise entry bar when losing, lower it when winning."""
        if overall_wr < 0.45:
            self.composite_threshold = min(0.20, self.composite_threshold * 1.03)
        elif overall_wr > 0.55:
            self.composite_threshold = max(0.03, self.composite_threshold * 0.95)

    def _adapt_stop_loss(self, recent: list[dict[str, Any]], overall_wr: float) -> None:
        """Tighten stop when losses are deep; loosen when stopped out too early."""
        pnl_vals = [o['pnl_pct'] for o in recent if o['pnl_pct'] != 0.0]
        if not pnl_vals:
            return
        losses = [v for v in pnl_vals if v < 0]
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        if avg_loss < -self.stop_loss_pct:
            self.stop_loss_pct = max(0.02, self.stop_loss_pct * 0.9)
        elif overall_wr < 0.40 and abs(avg_loss) < self.stop_loss_pct * 0.5:
            self.stop_loss_pct = min(0.15, self.stop_loss_pct * 1.1)

    def _adapt_hold_duration(self, overall_wr: float) -> None:
        """Shorten hold when losing; lengthen when winning."""
        if overall_wr < 0.40:
            self.max_hold_events = max(10, int(self.max_hold_events * 0.85))
        elif overall_wr > 0.60:
            self.max_hold_events = min(60, int(self.max_hold_events * 1.1))

    # ------------------------------------------------------------------
    # Persistence: local file + on-chain checkpoint
    # ------------------------------------------------------------------

    _LEARNED_KEYS = (
        'w_ob', 'w_momentum', 'composite_threshold',
        'stop_loss_pct', 'max_hold_events', '_adapt_epoch',
    )

    def _get_learned_snapshot(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self._LEARNED_KEYS}

    def _load_learned_params(self) -> None:
        """Load previously learned parameters from local JSON file."""
        if not self._params_file.exists():
            return
        try:
            data = json.loads(self._params_file.read_text())
            applied: list[str] = []
            for k in self._LEARNED_KEYS:
                if k in data:
                    setattr(self, k, type(getattr(self, k))(data[k]))
                    applied.append(k)
            if applied:
                logger.info('Loaded learned params (%s): %s', self._params_file, applied)
        except Exception:
            logger.debug('Failed to load learned params', exc_info=True)

    def _save_learned_params(self) -> None:
        """Persist current learned parameters to local JSON file."""
        try:
            self._params_file.parent.mkdir(parents=True, exist_ok=True)
            self._params_file.write_text(json.dumps(
                self._get_learned_snapshot(), indent=2,
            ))
        except Exception:
            logger.debug('Failed to save learned params', exc_info=True)

    async def _checkpoint_onchain(self) -> None:
        """Write a parameter snapshot to Solana as a Memo transaction."""
        if self._onchain_logger is None:
            return
        try:
            snap = self._get_learned_snapshot()
            snap['app'] = 'oracle3'
            snap['action'] = 'param_checkpoint'
            await self._onchain_logger.log_trade(
                market_ticker='__params__',
                side='checkpoint',
                price=snap.get('composite_threshold', 0),
                quantity=snap.get('_adapt_epoch', 0),
                trade_signature=json.dumps(snap, separators=(',', ':'))[:64],
            )
            logger.info('On-chain param checkpoint written (epoch %d)', self._adapt_epoch)
        except Exception:
            logger.debug('On-chain checkpoint failed', exc_info=True)

    # ------------------------------------------------------------------
    # Position cap
    # ------------------------------------------------------------------

    def _cap_quantity(self, desired: Decimal, ticker: Any, trader: Trader) -> Decimal:
        portfolio_values = trader.position_manager.get_portfolio_value(trader.market_data)
        total_value = sum(portfolio_values.values(), Decimal('0'))
        if total_value <= Decimal('0'):
            return desired

        max_value = total_value * Decimal(str(self.max_position_pct))
        position = trader.position_manager.get_position(ticker)
        current_qty = position.quantity if position else Decimal('0')

        best_ask = trader.market_data.get_best_ask(ticker)
        price_est = best_ask.price if best_ask else Decimal('1')
        if price_est <= Decimal('0'):
            price_est = Decimal('1')

        remaining = max_value - current_qty * price_est
        if remaining <= Decimal('0'):
            return Decimal('0')
        return min(desired, (remaining / price_est).quantize(Decimal('1')))
