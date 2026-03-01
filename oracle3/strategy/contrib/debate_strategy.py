from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import litellm

from oracle3.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.contrib.allocation_strategy import parse_llm_response_to_json
from oracle3.strategy.strategy import Strategy
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Few-Shot Calibration Examples
# ---------------------------------------------------------------------------

CALIBRATION_EXAMPLES: list[dict] = [
    {
        'question': 'Will the Fed cut rates in September 2024?',
        'news': 'CPI comes in at 2.5%, below expectations. Multiple Fed governors signal openness to easing.',
        'market_price_before': 0.72,
        'correct_probability': 0.95,
        'outcome': 'YES',
        'reasoning': (
            'CPI data directly supports the rate cut thesis. Multiple governors '
            'signaling = near certainty.'
        ),
    },
    {
        'question': 'Will Biden drop out of the 2024 race?',
        'news': 'Senior Democrats privately urging Biden to reconsider. Polls show historic low approval.',
        'market_price_before': 0.25,
        'correct_probability': 0.55,
        'reasoning': (
            'Private pressure is significant but not decisive. Presidents rarely '
            'drop out. Market was underpricing the risk.'
        ),
    },
    {
        'question': 'Will Bitcoin hit $100K in 2024?',
        'news': 'Bitcoin ETF sees record inflows. Price currently at $67K.',
        'market_price_before': 0.45,
        'correct_probability': 0.50,
        'reasoning': (
            'ETF inflows are bullish but $67K to $100K is a 49% move. '
            'News is somewhat priced in.'
        ),
    },
    {
        'question': 'Will Russia-Ukraine ceasefire happen by end of 2024?',
        'news': 'Zelenskyy rules out territorial concessions. Russia escalates drone attacks.',
        'market_price_before': 0.12,
        'correct_probability': 0.05,
        'reasoning': (
            'Both sides escalating, no diplomatic track. Market price of 12% '
            'already low but still too high.'
        ),
    },
    {
        'question': 'Will the US government shut down in October 2024?',
        'news': 'House Speaker faces revolt from hardliners. Continuing resolution vote fails first attempt.',
        'market_price_before': 0.40,
        'correct_probability': 0.35,
        'reasoning': (
            'Procedural failures are common but shutdowns are usually averted at the '
            'last minute. The drama is partly theatre; leadership has strong incentive '
            'to reach a deal.'
        ),
    },
    {
        'question': 'Will OpenAI release GPT-5 before July 2024?',
        'news': "Sam Altman teases 'amazing new model coming soon' at Davos. Internal sources say training is complete.",
        'market_price_before': 0.55,
        'correct_probability': 0.30,
        'reasoning': (
            "CEO hype is cheap; 'soon' in AI often means months. Training complete "
            'does not mean safety review, red-teaming, and launch are done. Market is '
            'overweighting insider hype.'
        ),
    },
    {
        'question': 'Will Texas experience a major grid failure in summer 2024?',
        'news': 'ERCOT issues conservation appeal as temperatures hit 110F. Reserve margins drop below 3%.',
        'market_price_before': 0.20,
        'correct_probability': 0.25,
        'reasoning': (
            'Conservation appeals are routine during Texas summers. Low reserve '
            'margins increase risk but ERCOT has managed similar conditions before. '
            'Slight upward adjustment warranted.'
        ),
    },
]


# ---------------------------------------------------------------------------
# Debate Role Enum
# ---------------------------------------------------------------------------


class DebateRole(Enum):
    BULL = 'bull'
    BEAR = 'bear'
    JUDGE = 'judge'


# ---------------------------------------------------------------------------
# DebateStrategy
# ---------------------------------------------------------------------------


class DebateStrategy(Strategy):
    """Multi-agent debate strategy with Kelly criterion position sizing.

    Three sequential LLM calls simulate a structured debate:
        1. **Bull** argues the probability should be *higher* than the market price.
        2. **Bear** argues it should be *lower*.
        3. **Judge** weighs both arguments and outputs a fair probability with
           confidence. Positions are sized using fractional Kelly criterion.

    A *fast path* (single LLM call) can optionally bypass the full debate for
    low-impact events.
    """

    def __init__(
        self,
        bull_model: str = 'deepseek/deepseek-chat',
        bear_model: str = 'deepseek/deepseek-chat',
        judge_model: str = 'deepseek/deepseek-chat',
        temperature: float = 0.3,
        max_tokens: int = 1024,
        kelly_fraction: float = 0.25,
        max_position_pct: float = 0.15,
        confidence_threshold: float = 0.55,
        edge_threshold: float = 0.05,
        news_window_seconds: int = 1800,
        analysis_cooldown_seconds: int = 120,
        max_price_history: int = 100,
        use_fast_path: bool = True,
        fast_path_model: str = 'deepseek/deepseek-chat',
    ) -> None:
        self.bull_model = bull_model
        self.bear_model = bear_model
        self.judge_model = judge_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.confidence_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.news_window_seconds = news_window_seconds
        self.analysis_cooldown_seconds = analysis_cooldown_seconds
        self.max_price_history = max_price_history
        self.use_fast_path = use_fast_path
        self.fast_path_model = fast_path_model

        # Internal state --------------------------------------------------
        self._news_buffer: dict[str, list[tuple[NewsEvent, float]]] = defaultdict(list)
        self._price_history: dict[str, deque[tuple[Decimal, datetime]]] = defaultdict(
            lambda: deque(maxlen=max_price_history)
        )
        self._last_analysis_time: dict[str, float] = {}
        self._order_flow_signals: dict[str, list[dict]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:
        """Route incoming events to the appropriate handler."""
        if isinstance(event, PriceChangeEvent):
            self._handle_price_event(event)
        elif isinstance(event, NewsEvent):
            await self._handle_news_event(event, trader)
        elif isinstance(event, OrderBookEvent):
            # OrderBook events are consumed by MarketDataManager; nothing to do.
            pass

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_price_event(self, event: PriceChangeEvent) -> None:
        symbol = event.ticker.symbol
        self._price_history[symbol].append((event.price, event.timestamp))
        logger.debug(
            'Price update for %s: %s (history length: %d)',
            symbol,
            event.price,
            len(self._price_history[symbol]),
        )

    async def _handle_news_event(self, event: NewsEvent, trader: Trader) -> None:
        if event.ticker is None:
            logger.debug('Ignoring NewsEvent with no ticker')
            return

        symbol = event.ticker.symbol
        now = time.time()

        self._news_buffer[symbol].append((event, now))
        self._prune_news_buffer(symbol, now)

        # Need price data before we can analyse
        if symbol not in self._price_history or not self._price_history[symbol]:
            logger.debug('Skipping analysis for %s: no price history', symbol)
            return

        # Cooldown gate
        last = self._last_analysis_time.get(symbol, 0.0)
        if now - last < self.analysis_cooldown_seconds:
            logger.debug(
                'Skipping analysis for %s: cooldown (%.1fs remaining)',
                symbol,
                self.analysis_cooldown_seconds - (now - last),
            )
            return

        self._last_analysis_time[symbol] = now
        await self._run_debate_analysis(event.ticker, trader)

    # ------------------------------------------------------------------
    # News buffer management
    # ------------------------------------------------------------------

    def _prune_news_buffer(self, symbol: str, now: float) -> None:
        cutoff = now - self.news_window_seconds
        self._news_buffer[symbol] = [
            (evt, ts) for evt, ts in self._news_buffer[symbol] if ts >= cutoff
        ]

    # ------------------------------------------------------------------
    # Low-impact classification
    # ------------------------------------------------------------------

    def _is_low_impact(self, ticker: Any) -> bool:
        """Determine whether the event context is low-impact (use fast path).

        Low impact if:
        - Only 1 news article in buffer
        - Price change < 2% over the observation window
        - No order flow signals recorded
        """
        symbol = ticker.symbol

        # Few news articles
        if len(self._news_buffer.get(symbol, [])) > 1:
            return False

        # Significant price movement
        prices = self._price_history.get(symbol, deque())
        if len(prices) >= 2:
            oldest = prices[0][0]
            newest = prices[-1][0]
            if oldest > Decimal('0'):
                pct_change = abs((newest - oldest) / oldest)
                if pct_change >= Decimal('0.02'):
                    return False

        # Order flow signals present
        if self._order_flow_signals.get(symbol):
            return False

        return True

    # ------------------------------------------------------------------
    # Market context builder
    # ------------------------------------------------------------------

    def _build_market_context(self, ticker: Any, trader: Trader) -> dict[str, Any]:
        symbol = ticker.symbol
        market_name = ticker.name or symbol

        # News
        news_items = self._news_buffer.get(symbol, [])
        news_lines: list[str] = []
        for evt, _ts in news_items:
            snippet = evt.news[:300] if evt.news else ''
            title = evt.title or '(no title)'
            source = evt.source or 'unknown'
            news_lines.append(f'- [{source}] {title}: {snippet}')

        # Prices
        prices = self._price_history.get(symbol, deque())
        current_price = Decimal('0')
        price_pct_change = Decimal('0')
        trend = 'flat'
        if prices:
            current_price = prices[-1][0]
            oldest_price = prices[0][0]
            if oldest_price > Decimal('0'):
                price_pct_change = (
                    (current_price - oldest_price) / oldest_price * Decimal('100')
                )
            trend = (
                'up'
                if price_pct_change > 0
                else ('down' if price_pct_change < 0 else 'flat')
            )

        # Position
        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity != Decimal('0')

        # Cash
        cash_positions = trader.position_manager.get_cash_positions()
        cash_total = sum((p.quantity for p in cash_positions), Decimal('0'))

        # Order flow
        flow_signals = self._order_flow_signals.get(symbol, [])

        return {
            'symbol': symbol,
            'market_name': market_name,
            'news_lines': news_lines,
            'current_price': current_price,
            'price_pct_change': price_pct_change,
            'trend': trend,
            'num_price_points': len(prices),
            'position_quantity': position.quantity if position is not None and has_position else Decimal('0'),
            'position_avg_cost': position.average_cost
            if position is not None and has_position
            else Decimal('0'),
            'position_realized_pnl': position.realized_pnl
            if position is not None and has_position
            else Decimal('0'),
            'cash_total': cash_total,
            'order_flow_signals': flow_signals,
        }

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _format_context_block(self, ctx: dict[str, Any]) -> str:
        """Shared context section included in every prompt."""
        news_section = (
            '\n'.join(ctx['news_lines']) if ctx['news_lines'] else '(no recent news)'
        )

        price_section = (
            f'Current probability: {ctx["current_price"]}\n'
            f'Price change over window: {ctx["price_pct_change"]:+.2f}%\n'
            f'Trend: {ctx["trend"]}\n'
            f'Data points: {ctx["num_price_points"]}'
        )

        if ctx['position_quantity'] != Decimal('0'):
            position_section = (
                f'Current position: {ctx["position_quantity"]} shares '
                f'@ avg cost {ctx["position_avg_cost"]}\n'
                f'Realized PnL: {ctx["position_realized_pnl"]}'
            )
        else:
            position_section = 'No current position'

        flow_section = ''
        if ctx['order_flow_signals']:
            flow_lines = [json.dumps(s) for s in ctx['order_flow_signals'][-5:]]
            flow_section = '\n## Order Flow Signals\n' + '\n'.join(flow_lines) + '\n'

        return (
            f'## Market\nQuestion / Name: {ctx["market_name"]}\n\n'
            f'## Recent News\n{news_section}\n\n'
            f'## Price Data\n{price_section}\n\n'
            f'## Current Position\n{position_section}\n\n'
            f'## Account\nAvailable cash: {ctx["cash_total"]} USDC\n'
            f'{flow_section}'
        )

    def _build_bull_prompt(self, ctx: dict[str, Any]) -> str:
        context_block = self._format_context_block(ctx)
        return (
            'You are a prediction market analyst arguing the BULL case.\n\n'
            'Your job is to identify ALL evidence suggesting the probability should be '
            'HIGHER than the current market price. Consider the news, price data, and '
            'any order flow signals provided.\n\n'
            'Be specific and cite the evidence. Explain why the market is underpricing '
            'the YES outcome.\n\n'
            f'{context_block}\n'
            '## Instructions\n'
            'This is a Polymarket binary outcome market where prices represent '
            'probabilities (0 to 1).\n\n'
            'Make your strongest bull case. At the end, rate your conviction on a '
            'scale of 0.0 to 1.0 (where 1.0 means you are virtually certain the '
            'probability should be higher).\n\n'
            'Respond in this format:\n'
            'ARGUMENT: <your detailed bull case>\n'
            'CONVICTION: <float 0.0-1.0>\n'
            'FAIR_PROBABILITY: <your estimate, float 0.0-1.0>'
        )

    def _build_bear_prompt(self, ctx: dict[str, Any]) -> str:
        context_block = self._format_context_block(ctx)
        return (
            'You are a prediction market analyst arguing the BEAR case.\n\n'
            'Your job is to identify ALL evidence suggesting the probability should be '
            "LOWER than the current market price. Play devil's advocate. Consider what "
            'is already priced in, what could go wrong, and tail risks.\n\n'
            'Be specific and cite the evidence. Explain why the market is overpricing '
            'the YES outcome.\n\n'
            f'{context_block}\n'
            '## Instructions\n'
            'This is a Polymarket binary outcome market where prices represent '
            'probabilities (0 to 1).\n\n'
            'Make your strongest bear case. At the end, rate your conviction on a '
            'scale of 0.0 to 1.0 (where 1.0 means you are virtually certain the '
            'probability should be lower).\n\n'
            'Respond in this format:\n'
            'ARGUMENT: <your detailed bear case>\n'
            'CONVICTION: <float 0.0-1.0>\n'
            'FAIR_PROBABILITY: <your estimate, float 0.0-1.0>'
        )

    def _build_judge_prompt(
        self, ctx: dict[str, Any], bull_response: str, bear_response: str
    ) -> str:
        context_block = self._format_context_block(ctx)

        # Format calibration examples as few-shot
        cal_lines: list[str] = []
        for i, ex in enumerate(CALIBRATION_EXAMPLES, 1):
            cal_lines.append(
                f'Example {i}:\n'
                f'  Question: {ex["question"]}\n'
                f'  News: {ex["news"]}\n'
                f'  Market price: {ex["market_price_before"]}\n'
                f'  Correct fair probability: {ex["correct_probability"]}\n'
                f'  Reasoning: {ex["reasoning"]}'
            )
        calibration_block = '\n\n'.join(cal_lines)

        return (
            'You are a senior prediction market judge. You have heard two analysts '
            'debate and must render a final verdict.\n\n'
            f'{context_block}\n'
            '## Calibration Examples\n'
            'Study these resolved examples to calibrate your probability estimates:\n\n'
            f'{calibration_block}\n\n'
            '## Bull Case\n'
            f'{bull_response}\n\n'
            '## Bear Case\n'
            f'{bear_response}\n\n'
            '## Instructions\n'
            'Weigh both arguments carefully using Bayesian reasoning. Consider:\n'
            '1. Which side presented stronger, more specific evidence?\n'
            '2. What is already priced into the current market probability?\n'
            '3. How do the calibration examples inform your estimate?\n'
            '4. Are there biases in either argument?\n\n'
            'Respond ONLY with JSON in this exact format:\n'
            '```json\n'
            '{\n'
            '  "fair_probability": <float 0.0-1.0>,\n'
            '  "confidence": <float 0.0-1.0>,\n'
            '  "action": "buy" | "sell" | "hold",\n'
            '  "reasoning": "<brief explanation of your verdict>",\n'
            '  "edge": <float, fair_probability minus market_price>\n'
            '}\n'
            '```\n'
            'Do not include any text outside the JSON block.'
        )

    def _build_fast_path_prompt(self, ctx: dict[str, Any]) -> str:
        context_block = self._format_context_block(ctx)

        cal_lines: list[str] = []
        for i, ex in enumerate(CALIBRATION_EXAMPLES[:3], 1):
            cal_lines.append(
                f'Example {i}: Q: {ex["question"]} | Market: {ex["market_price_before"]} '
                f'| Fair: {ex["correct_probability"]} | Why: {ex["reasoning"]}'
            )
        calibration_block = '\n'.join(cal_lines)

        return (
            'You are an expert prediction market analyst. Quickly assess whether '
            'this market is mispriced.\n\n'
            f'{context_block}\n'
            '## Calibration Reference\n'
            f'{calibration_block}\n\n'
            'Respond ONLY with JSON in this exact format:\n'
            '```json\n'
            '{\n'
            '  "fair_probability": <float 0.0-1.0>,\n'
            '  "confidence": <float 0.0-1.0>,\n'
            '  "action": "buy" | "sell" | "hold",\n'
            '  "reasoning": "<brief explanation>",\n'
            '  "edge": <float, fair_probability minus market_price>\n'
            '}\n'
            '```\n'
            'Do not include any text outside the JSON block.'
        )

    # ------------------------------------------------------------------
    # LLM call helper
    # ------------------------------------------------------------------

    async def _call_llm(self, model: str, prompt: str) -> str:
        """Make a single LLM completion call and return the text content."""
        response = await litellm.acompletion(
            model=model,
            messages=[{'role': 'user', 'content': prompt}],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return response.choices[0].message.content

    # ------------------------------------------------------------------
    # Analysis flow
    # ------------------------------------------------------------------

    async def _run_debate_analysis(self, ticker: Any, trader: Trader) -> None:
        """Full debate flow: bull -> bear -> judge -> execute."""
        symbol = ticker.symbol
        logger.info('Running debate analysis for %s', symbol)

        ctx = self._build_market_context(ticker, trader)

        try:
            if self.use_fast_path and self._is_low_impact(ticker):
                logger.info('Using fast path for %s (low-impact event)', symbol)
                result = await self._call_llm(
                    self.fast_path_model, self._build_fast_path_prompt(ctx)
                )
            else:
                logger.info('Running full debate for %s', symbol)

                # 1. Bull makes the case
                bull_response = await self._call_llm(
                    self.bull_model, self._build_bull_prompt(ctx)
                )
                logger.debug('Bull response for %s: %s', symbol, bull_response)

                # 2. Bear makes the case
                bear_response = await self._call_llm(
                    self.bear_model, self._build_bear_prompt(ctx)
                )
                logger.debug('Bear response for %s: %s', symbol, bear_response)

                # 3. Judge decides
                result = await self._call_llm(
                    self.judge_model,
                    self._build_judge_prompt(ctx, bull_response, bear_response),
                )
                logger.debug('Judge response for %s: %s', symbol, result)
        except Exception:
            logger.exception('LLM call failed during debate for %s', symbol)
            return

        parsed = parse_llm_response_to_json(result)
        if parsed is None:
            logger.error('Failed to parse judge JSON for %s: %s', symbol, result)
            return

        await self._execute_with_kelly(parsed, ticker, trader)

    # ------------------------------------------------------------------
    # Kelly criterion position sizing
    # ------------------------------------------------------------------

    def _kelly_size(
        self,
        fair_prob: float,
        market_price: float,
        portfolio_value: Decimal,
    ) -> Decimal:
        """Calculate position size using fractional Kelly criterion.

        Kelly formula for binary bets:
            f* = (p * b - q) / b
        where:
            p = estimated probability of winning side
            q = 1 - p
            b = decimal odds for the winning side

        For buying YES (fair_prob > market_price):
            p = fair_prob
            b = (1 / market_price) - 1

        For selling / buying NO (fair_prob < market_price):
            p = 1 - fair_prob  (probability of NO)
            b = (1 / (1 - market_price)) - 1

        Returns Decimal quantity to trade (0 if no edge or invalid inputs).
        """
        if market_price <= 0.0 or market_price >= 1.0:
            return Decimal('0')

        if fair_prob > market_price:
            # Buying YES
            p = fair_prob
            b = (1.0 / market_price) - 1.0
        else:
            # Selling YES / buying NO
            p = 1.0 - fair_prob
            b = (1.0 / (1.0 - market_price)) - 1.0

        if b <= 0.0:
            return Decimal('0')

        q = 1.0 - p
        kelly_f = (p * b - q) / b

        if kelly_f <= 0.0:
            return Decimal('0')

        # Apply fractional Kelly
        fraction = kelly_f * self.kelly_fraction

        # Cap at max_position_pct of portfolio
        fraction = min(fraction, self.max_position_pct)

        # Convert to dollar amount, then to shares at market price
        dollar_size = Decimal(str(fraction)) * portfolio_value
        share_price = Decimal(str(market_price))

        if share_price <= Decimal('0'):
            return Decimal('0')

        quantity = (dollar_size / share_price).quantize(Decimal('1'))
        return max(quantity, Decimal('0'))

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    async def _execute_with_kelly(
        self, signal: dict[str, Any], ticker: Any, trader: Trader
    ) -> None:
        """Execute a trade using Kelly-sized position.

        Expected signal keys:
            fair_probability (float 0-1), confidence (float 0-1),
            reasoning (str), action (buy/sell/hold)
        """
        symbol = ticker.symbol
        action = signal.get('action', 'hold').lower()
        fair_prob = float(signal.get('fair_probability', 0.5))
        confidence = float(signal.get('confidence', 0.0))
        reasoning = signal.get('reasoning', '')

        logger.info(
            'Debate signal for %s: action=%s fair_prob=%.3f confidence=%.2f reasoning=%s',
            symbol,
            action,
            fair_prob,
            confidence,
            reasoning,
        )

        # Gate: confidence threshold
        if confidence < self.confidence_threshold:
            logger.info(
                'Holding %s: confidence %.2f below threshold %.2f',
                symbol,
                confidence,
                self.confidence_threshold,
            )
            return

        # Get current market price from best bid/ask midpoint
        best_bid = trader.market_data.get_best_bid(ticker)
        best_ask = trader.market_data.get_best_ask(ticker)

        if best_bid is None and best_ask is None:
            logger.warning('No bid or ask available for %s, cannot trade', symbol)
            return

        if best_bid is not None and best_ask is not None:
            market_price = (best_bid.price + best_ask.price) / Decimal('2')
        elif best_ask is not None:
            market_price = best_ask.price
        else:
            market_price = best_bid.price  # type: ignore[union-attr]

        market_price_float = float(market_price)

        # Gate: edge threshold
        edge = fair_prob - market_price_float
        if abs(edge) < self.edge_threshold:
            logger.info(
                'Holding %s: edge %.3f below threshold %.3f',
                symbol,
                edge,
                self.edge_threshold,
            )
            return

        # Gate: action == hold
        if action == 'hold':
            logger.info('Holding %s per judge recommendation', symbol)
            return

        # Calculate portfolio value for Kelly sizing
        cash_positions = trader.position_manager.get_cash_positions()
        cash_total = sum((p.quantity for p in cash_positions), Decimal('0'))
        # Use cash as proxy for portfolio value (conservative)
        portfolio_value = cash_total

        if portfolio_value <= Decimal('0'):
            logger.warning(
                'Portfolio value is zero for %s, cannot size position', symbol
            )
            return

        kelly_qty = self._kelly_size(fair_prob, market_price_float, portfolio_value)

        if kelly_qty <= Decimal('0'):
            logger.info('Kelly size is zero for %s (no edge per formula)', symbol)
            return

        # Adjust for existing position
        position = trader.position_manager.get_position(ticker)
        existing_qty = position.quantity if position is not None else Decimal('0')

        if edge > 0 and action == 'buy':
            # Want to be long; reduce target by existing long position
            target_qty = max(kelly_qty - existing_qty, Decimal('0'))
            if target_qty <= Decimal('0'):
                logger.info(
                    'Already at or above Kelly size for %s (existing=%s, kelly=%s)',
                    symbol,
                    existing_qty,
                    kelly_qty,
                )
                return
            await self._execute_buy(ticker, target_qty, trader, reasoning)

        elif edge < 0 and action == 'sell':
            # Want to reduce or sell position
            if existing_qty <= Decimal('0'):
                logger.info('No position to sell for %s, skipping', symbol)
                return
            sell_qty = min(kelly_qty, existing_qty)
            if sell_qty <= Decimal('0'):
                logger.info('Sell quantity is zero for %s, skipping', symbol)
                return
            await self._execute_sell(ticker, sell_qty, trader, reasoning)

        else:
            logger.info(
                'Edge/action mismatch for %s: edge=%.3f action=%s, skipping',
                symbol,
                edge,
                action,
            )

    # ------------------------------------------------------------------
    # Order placement helpers
    # ------------------------------------------------------------------

    async def _execute_buy(
        self,
        ticker: Any,
        quantity: Decimal,
        trader: Trader,
        reasoning: str,
    ) -> None:
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
        symbol = ticker.symbol

        position = trader.position_manager.get_position(ticker)
        if position is None or position.quantity <= Decimal('0'):
            logger.info('No position to sell for %s, skipping', symbol)
            return

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
