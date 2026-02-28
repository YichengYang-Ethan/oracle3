from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from typing import Any

import litellm

from oracle3.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.contrib.allocation_strategy import parse_llm_response_to_json
from oracle3.strategy.strategy import Strategy
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)


class LLMNewsStrategy(Strategy):
    """LLM-powered news analysis strategy for Polymarket prediction markets.

    Accumulates news and price data per ticker, then uses an LLM to generate
    trade signals when new news arrives. Position sizing scales with the LLM's
    reported confidence.
    """

    def __init__(
        self,
        model: str = 'deepseek/deepseek-chat',
        temperature: float = 0.2,
        max_tokens: int = 1024,
        base_trade_size: Decimal = Decimal('100'),
        max_trade_size: Decimal = Decimal('500'),
        confidence_threshold: float = 0.5,
        news_window_seconds: int = 1800,
        analysis_cooldown_seconds: int = 60,
        max_price_history: int = 100,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_trade_size = base_trade_size
        self.max_trade_size = max_trade_size
        self.confidence_threshold = confidence_threshold
        self.news_window_seconds = news_window_seconds
        self.analysis_cooldown_seconds = analysis_cooldown_seconds
        self.max_price_history = max_price_history

        # ticker symbol -> list of (NewsEvent, timestamp)
        self._news_buffer: dict[str, list[tuple[NewsEvent, float]]] = defaultdict(list)
        # ticker symbol -> deque of (price: Decimal, timestamp: datetime)
        self._price_history: dict[str, deque[tuple[Decimal, datetime]]] = defaultdict(
            lambda: deque(maxlen=max_price_history)
        )
        # ticker symbol -> last analysis epoch time
        self._last_analysis_time: dict[str, float] = {}

    async def process_event(self, event: Event, trader: Trader) -> None:
        """Route incoming events to the appropriate handler."""
        if isinstance(event, PriceChangeEvent):
            self._handle_price_event(event)
        elif isinstance(event, NewsEvent):
            await self._handle_news_event(event, trader)
        elif isinstance(event, OrderBookEvent):
            # OrderBook events are handled by the market data manager directly;
            # no additional processing needed here.
            pass

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_price_event(self, event: PriceChangeEvent) -> None:
        """Record a price observation for the ticker."""
        symbol = event.ticker.symbol
        self._price_history[symbol].append((event.price, event.timestamp))
        logger.debug(
            'Price update for %s: %s (history length: %d)',
            symbol,
            event.price,
            len(self._price_history[symbol]),
        )

    async def _handle_news_event(self, event: NewsEvent, trader: Trader) -> None:
        """Buffer the news event and potentially trigger LLM analysis."""
        if event.ticker is None:
            logger.debug('Ignoring NewsEvent with no ticker')
            return

        symbol = event.ticker.symbol
        now = time.time()

        # Add to buffer and prune stale entries
        self._news_buffer[symbol].append((event, now))
        self._prune_news_buffer(symbol, now)

        # Gate: need price data for this ticker
        if symbol not in self._price_history or not self._price_history[symbol]:
            logger.debug('Skipping analysis for %s: no price history available', symbol)
            return

        # Gate: cooldown
        last = self._last_analysis_time.get(symbol, 0.0)
        if now - last < self.analysis_cooldown_seconds:
            logger.debug(
                'Skipping analysis for %s: cooldown (%0.1fs remaining)',
                symbol,
                self.analysis_cooldown_seconds - (now - last),
            )
            return

        self._last_analysis_time[symbol] = now
        await self._run_analysis(event.ticker, trader)

    # ------------------------------------------------------------------
    # News buffer management
    # ------------------------------------------------------------------

    def _prune_news_buffer(self, symbol: str, now: float) -> None:
        """Remove news entries older than the configured window."""
        cutoff = now - self.news_window_seconds
        self._news_buffer[symbol] = [
            (evt, ts) for evt, ts in self._news_buffer[symbol] if ts >= cutoff
        ]

    # ------------------------------------------------------------------
    # LLM analysis
    # ------------------------------------------------------------------

    async def _run_analysis(self, ticker: Any, trader: Trader) -> None:
        """Build a prompt, call the LLM, and execute the resulting trade signal."""
        symbol = ticker.symbol
        logger.info('Running LLM analysis for %s', symbol)

        prompt = self._build_prompt(ticker, trader)

        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            content = response.choices[0].message.content
            logger.debug('LLM raw response for %s: %s', symbol, content)
        except Exception:
            logger.exception('LLM call failed for %s', symbol)
            return

        parsed = parse_llm_response_to_json(content)
        if parsed is None:
            logger.error('Failed to parse LLM JSON for %s: %s', symbol, content)
            return

        await self._execute_signal(parsed, ticker, trader)

    def _build_prompt(self, ticker: Any, trader: Trader) -> str:
        """Construct the LLM analysis prompt."""
        symbol = ticker.symbol
        market_name = ticker.name or symbol

        # -- News section --
        news_items = self._news_buffer.get(symbol, [])
        news_lines: list[str] = []
        for evt, _ts in news_items:
            snippet = evt.news[:300] if evt.news else ''
            title = evt.title or '(no title)'
            source = evt.source or 'unknown'
            news_lines.append(f'- [{source}] {title}: {snippet}')
        news_section = '\n'.join(news_lines) if news_lines else '(no recent news)'

        # -- Price section --
        prices = self._price_history.get(symbol, deque())
        if prices:
            current_price = prices[-1][0]
            oldest_price = prices[0][0]
            if oldest_price != Decimal('0'):
                pct_change = (
                    (current_price - oldest_price) / oldest_price * Decimal('100')
                )
            else:
                pct_change = Decimal('0')
            trend = 'up' if pct_change > 0 else ('down' if pct_change < 0 else 'flat')
            price_section = (
                f'Current probability: {current_price}\n'
                f'Price change over window: {pct_change:+.2f}%\n'
                f'Trend: {trend}\n'
                f'Data points: {len(prices)}'
            )
        else:
            price_section = '(no price data)'

        # -- Position section --
        position = trader.position_manager.get_position(ticker)
        if position and position.quantity != Decimal('0'):
            position_section = (
                f'Current position: {position.quantity} shares @ avg cost {position.average_cost}\n'
                f'Realized PnL: {position.realized_pnl}'
            )
        else:
            position_section = 'No current position'

        # -- Cash section --
        cash_positions = trader.position_manager.get_cash_positions()
        cash_total = sum((p.quantity for p in cash_positions), Decimal('0'))
        cash_section = f'Available cash: {cash_total} USDC'

        return (
            'You are an expert prediction-market analyst. Analyze the following data '
            'for a Polymarket binary outcome market and recommend a trading action.\n\n'
            f'## Market\n'
            f'Question / Name: {market_name}\n\n'
            f'## Recent News\n{news_section}\n\n'
            f'## Price Data\n{price_section}\n\n'
            f'## Current Position\n{position_section}\n\n'
            f'## Account\n{cash_section}\n\n'
            '## Instructions\n'
            'This is a prediction market where prices represent probabilities (0 to 1). '
            'A price of 0.70 means the market currently assigns a 70% probability to the outcome.\n\n'
            'Based on the news and price data, determine whether the current market probability '
            'is mispriced. If the news suggests a higher true probability than the current price, '
            'consider buying. If lower, consider selling (if we have a position).\n\n'
            'Respond ONLY with JSON in this exact format:\n'
            '```json\n'
            '{\n'
            '  "action": "buy" | "sell" | "hold",\n'
            '  "confidence": <float 0.0-1.0>,\n'
            '  "reasoning": "<brief explanation>",\n'
            '  "target_price": <float 0.0-1.0>\n'
            '}\n'
            '```\n'
            'Do not include any text outside the JSON block.'
        )

    # ------------------------------------------------------------------
    # Signal execution
    # ------------------------------------------------------------------

    async def _execute_signal(
        self, signal: dict[str, Any], ticker: Any, trader: Trader
    ) -> None:
        """Translate the LLM signal into an order, applying position sizing rules."""
        action = signal.get('action', 'hold').lower()
        confidence = float(signal.get('confidence', 0.0))
        reasoning = signal.get('reasoning', '')
        symbol = ticker.symbol

        logger.info(
            'LLM signal for %s: action=%s confidence=%.2f reasoning=%s',
            symbol,
            action,
            confidence,
            reasoning,
        )

        if action == 'hold' or confidence < self.confidence_threshold:
            logger.info(
                'Holding %s (action=%s, confidence=%.2f, threshold=%.2f)',
                symbol,
                action,
                confidence,
                self.confidence_threshold,
            )
            return

        # Position sizing: scale linearly from 0 at threshold to base_trade_size at 1.0
        size_fraction = Decimal(
            str(
                (confidence - self.confidence_threshold)
                / (1.0 - self.confidence_threshold)
            )
        )
        trade_size = min(self.base_trade_size * size_fraction, self.max_trade_size)
        trade_size = trade_size.quantize(Decimal('1'))  # whole shares

        if trade_size <= Decimal('0'):
            logger.info('Computed trade size is zero for %s, skipping', symbol)
            return

        if action == 'buy':
            await self._execute_buy(ticker, trade_size, trader, reasoning)
        elif action == 'sell':
            await self._execute_sell(ticker, trade_size, trader, reasoning)
        else:
            logger.warning("Unknown action '%s' from LLM for %s", action, symbol)

    async def _execute_buy(
        self,
        ticker: Any,
        quantity: Decimal,
        trader: Trader,
        reasoning: str,
    ) -> None:
        """Place a buy order at the best ask if we have sufficient cash."""
        symbol = ticker.symbol
        best_ask = trader.market_data.get_best_ask(ticker)
        if best_ask is None:
            logger.warning('No ask available for %s, cannot buy', symbol)
            return

        price = best_ask.price

        # Check available cash
        cash_positions = trader.position_manager.get_cash_positions()
        cash_total = sum((p.quantity for p in cash_positions), Decimal('0'))
        required = price * quantity
        if required > cash_total:
            # Reduce quantity to what we can afford
            if price > Decimal('0'):
                quantity = (cash_total / price).quantize(Decimal('1'))
            if quantity <= Decimal('0'):
                logger.warning(
                    'Insufficient cash for %s buy: need %s, have %s',
                    symbol,
                    required,
                    cash_total,
                )
                return

        logger.info(
            'Placing BUY order for %s: qty=%s @ %s | reason: %s',
            symbol,
            quantity,
            price,
            reasoning,
        )
        result = await trader.place_order(TradeSide.BUY, ticker, price, quantity)
        if result.failure_reason is not None:
            logger.warning('BUY order failed for %s: %s', symbol, result.failure_reason)
        elif result.order is not None:
            logger.info(
                'BUY order placed for %s: status=%s filled=%s',
                symbol,
                result.order.status,
                result.order.filled_quantity,
            )

    async def _execute_sell(
        self,
        ticker: Any,
        quantity: Decimal,
        trader: Trader,
        reasoning: str,
    ) -> None:
        """Place a sell order at the best bid if we have a position to sell."""
        symbol = ticker.symbol

        # Check existing position
        position = trader.position_manager.get_position(ticker)
        if position is None or position.quantity <= Decimal('0'):
            logger.info('No position to sell for %s, skipping', symbol)
            return

        # Cap sell quantity at current position
        quantity = min(quantity, position.quantity)

        best_bid = trader.market_data.get_best_bid(ticker)
        if best_bid is None:
            logger.warning('No bid available for %s, cannot sell', symbol)
            return

        price = best_bid.price

        logger.info(
            'Placing SELL order for %s: qty=%s @ %s | reason: %s',
            symbol,
            quantity,
            price,
            reasoning,
        )
        result = await trader.place_order(TradeSide.SELL, ticker, price, quantity)
        if result.failure_reason is not None:
            logger.warning(
                'SELL order failed for %s: %s', symbol, result.failure_reason
            )
        elif result.order is not None:
            logger.info(
                'SELL order placed for %s: status=%s filled=%s',
                symbol,
                result.order.status,
                result.order.filled_quantity,
            )
