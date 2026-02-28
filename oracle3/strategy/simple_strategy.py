import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, TypedDict

import httpx

from oracle3.events.events import Event, NewsEvent, OrderBookEvent
from oracle3.ticker.ticker import KalshiTicker, PolyMarketTicker, Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

from .agent_strategy import AgentStrategy

# API endpoint: auto-select based on available keys
# DeepSeek: set DEEPSEEK_API_KEY; OpenAI: set OPENAI_API_KEY
DEEPSEEK_URL = 'https://api.deepseek.com/v1/chat/completions'
OPENAI_URL = 'https://api.openai.com/v1/chat/completions'

# --- Opening trade thresholds ---
DEFAULT_EDGE_THRESHOLD = Decimal('0.00')  # Neutral default: no fixed edge prior
DEFAULT_ESTIMATED_SPREAD = Decimal('0.00')  # Neutral default: no fixed spread prior

# --- Position management thresholds ---
# Edge consumed: market moved toward our estimate, take the win
EDGE_CONSUMED_THRESHOLD = Decimal('0.00')  # Neutral default: no fixed close prior
# Edge reversed: market moved past our LLM estimate, we're wrong
EDGE_REVERSED = True  # Close immediately if edge reverses
# LLM re-evaluation cooldown
REEVAL_COOLDOWN_SECONDS = 300  # Re-evaluate a position at most every 5 min
# Max holding time before forced close (capital efficiency)
MAX_HOLDING_SECONDS = 3600  # 1 hour


class LLMAnalysisResult(TypedDict):
    action: Literal['buy_yes', 'buy_no', 'hold']
    confidence: float
    reasoning: str | None
    llm_prob: float
    market_price: float


@dataclass
class PositionMeta:
    """Tracks metadata for an open position to drive exit decisions."""

    ticker_symbol: str
    side: str  # 'yes' or 'no' — which token we bought
    llm_prob: float  # LLM probability estimate at open
    entry_price: float  # market price when we entered
    entry_time: datetime = field(default_factory=datetime.now)
    last_reeval_time: datetime = field(default_factory=datetime.now)
    title: str = ''  # market title for logging


class SimpleStrategy(AgentStrategy):
    name = 'simple_llm'
    version = '0.2.0'
    author = 'oracle3'

    def __init__(
        self,
        trade_size: Decimal = Decimal('1.0'),
        edge_threshold: Decimal = DEFAULT_EDGE_THRESHOLD,
        estimated_spread: Decimal = DEFAULT_ESTIMATED_SPREAD,
        edge_consumed_threshold: Decimal = EDGE_CONSUMED_THRESHOLD,
        reeval_cooldown: int = REEVAL_COOLDOWN_SECONDS,
        max_holding: int = MAX_HOLDING_SECONDS,
    ):
        self.trade_size = trade_size
        self.edge_threshold = Decimal(str(edge_threshold))
        self.estimated_spread = Decimal(str(estimated_spread))
        self.edge_consumed_threshold = Decimal(str(edge_consumed_threshold))
        self.reeval_cooldown = timedelta(seconds=reeval_cooldown)
        self.max_holding = timedelta(seconds=max_holding)
        self.logger = logging.getLogger(__name__)
        super().__init__()
        # Position metadata: ticker_symbol → PositionMeta
        self._position_meta: dict[str, PositionMeta] = {}
        # Guard against concurrent close attempts on the same ticker
        self._closing_in_progress: set[str] = set()
        # Track consecutive close failures per ticker to avoid infinite retry loops
        self._close_failures: dict[str, int] = {}
        # Guard against concurrent open attempts on the same market
        self._opening_in_progress: set[str] = set()
        # Buffer of recent Google News for context (title, snippet, source, timestamp)
        self._news_buffer: deque[tuple[str, str, str, float]] = deque(maxlen=200)
        # How old news can be before it's pruned (seconds)
        self._news_max_age: float = 1800.0  # 30 minutes

    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process incoming events and make trading decisions."""
        if self.is_paused():
            return
        if isinstance(event, OrderBookEvent):
            # On every price update: check if positions need closing
            await self._check_position_exits(event.ticker, trader)
        elif isinstance(event, NewsEvent):
            self.logger.info(f'NewsEvent: {event.title}')

            if event.ticker is None:
                # Google News — buffer for later matching
                self._news_buffer.append(
                    (
                        event.title or '',
                        event.news or '',
                        event.source or 'Google News',
                        time.time(),
                    )
                )
                self.logger.info(f'Buffered Google News: {(event.title or "")[:50]}')
                return

            ticker = event.ticker

            # Skip if we already hold a position in this market
            if ticker is not None:
                existing = trader.position_manager.get_position(ticker)
                if existing and existing.quantity > 0:
                    self.logger.debug(
                        f'Already holding position in {ticker.name[:30]}, skip'
                    )
                    return
                # Also check NO ticker (any ticker type with get_no_ticker)
                no_ticker = getattr(ticker, 'get_no_ticker', lambda: None)()
                if no_ticker:
                    no_pos = trader.position_manager.get_position(no_ticker)
                    if no_pos and no_pos.quantity > 0:
                        self.logger.debug(
                            f'Already holding NO position in {ticker.name[:30]}, skip'
                        )
                        return

                # Skip if another coroutine is already opening a position for this market
                market_key = getattr(ticker, 'market_id', '') or ticker.symbol
                if market_key in self._opening_in_progress:
                    self.logger.debug(
                        f'Opening already in progress for {ticker.name[:30]}, skip'
                    )
                    return

            market_price = self._get_market_price(ticker, trader)

            # Acquire opening lock to prevent concurrent opens for the same market
            market_key = getattr(ticker, 'market_id', '') or ticker.symbol
            self._opening_in_progress.add(market_key)
            try:
                analysis = await self._analyze_news_with_llm(event, market_price)
                self.logger.info(f'LLM Analysis: {analysis}')

                llm_prob = analysis.get('llm_prob', 0.0)
                market_price_val = analysis.get('market_price', 0.0)
                action_str = analysis['action'].upper()
                confidence = analysis['confidence']
                reasoning = analysis.get('reasoning', '') or ''
                ticker_name = (event.title or event.news or '')[:40]
                sig = {
                    'llm_prob': llm_prob,
                    'market_price': market_price_val,
                    'edge': round(llm_prob - market_price_val, 4),
                }

                if analysis['action'] == 'hold':
                    self.record_decision(
                        ticker_name=ticker_name,
                        action=action_str,
                        executed=False,
                        reasoning=reasoning,
                        confidence=confidence,
                        signal_values=sig,
                    )
                    return

                executed, fill_price = await self._execute_trade(
                    analysis, ticker, trader
                )
                self.record_decision(
                    ticker_name=ticker_name,
                    action=action_str,
                    executed=executed,
                    reasoning=reasoning,
                    confidence=confidence,
                    signal_values=sig,
                )

                # Record position metadata if trade was executed
                if executed:
                    side = 'yes' if analysis['action'] == 'buy_yes' else 'no'
                    sym = ticker.symbol
                    if side == 'no':
                        no_ticker = getattr(ticker, 'get_no_ticker', lambda: None)()
                        if no_ticker:
                            sym = no_ticker.symbol
                    # Use actual fill price for entry_price (critical for P&L tracking)
                    # For YES: fill_price is the YES token price paid
                    # For NO: fill_price is the NO token price paid
                    self._position_meta[sym] = PositionMeta(
                        ticker_symbol=sym,
                        side=side,
                        llm_prob=analysis['llm_prob'],
                        entry_price=fill_price,
                        title=(event.title or '')[:40],
                    )
            finally:
                self._opening_in_progress.discard(market_key)

    # ------------------------------------------------------------------
    # News buffer helpers
    # ------------------------------------------------------------------

    @property
    def news_buffer_count(self) -> int:
        return len(self._news_buffer)

    def _find_relevant_news(self, market_title: str, max_results: int = 5) -> list[str]:
        """Find Google News articles relevant to a market question."""
        now = time.time()
        # Prune old news
        while self._news_buffer and now - self._news_buffer[0][3] > self._news_max_age:
            self._news_buffer.popleft()

        if not self._news_buffer or not market_title:
            return []

        # Simple keyword matching: extract key words from market title
        stop_words = {
            'will',
            'the',
            'a',
            'an',
            'by',
            'in',
            'of',
            'to',
            'and',
            'or',
            'is',
            'be',
            'end',
            'before',
            'after',
            'on',
            'at',
            'for',
            'with',
            'from',
            'this',
            'that',
            'what',
            'which',
            'who',
            'how',
            'when',
            'where',
            'does',
            'do',
            'did',
            'has',
            'have',
            'had',
            'not',
            'no',
            'yes',
            'any',
            'all',
            'each',
            'every',
            'if',
        }
        keywords = set()
        for word in market_title.lower().split():
            word = word.strip('?.,!:;()[]{}"\'-')
            if len(word) > 2 and word not in stop_words:
                keywords.add(word)

        if not keywords:
            return []

        # Score each news article by keyword overlap
        scored: list[tuple[int, str]] = []
        for title, snippet, source, _ts in self._news_buffer:
            text = f'{title} {snippet}'.lower()
            score = sum(1 for kw in keywords if kw in text)
            if score >= 2:  # At least 2 keyword matches
                news_text = f'[{source}] {title}'
                if snippet and snippet != title:
                    news_text += f': {snippet[:200]}'
                scored.append((score, news_text))

        # Return top matches sorted by relevance, with a total character budget
        # to avoid overloading the LLM prompt
        scored.sort(key=lambda x: -x[0])
        results: list[str] = []
        total_chars = 0
        for _, text in scored[:max_results]:
            if total_chars + len(text) > 800:
                break
            results.append(text)
            total_chars += len(text)
        return results

    # ------------------------------------------------------------------
    # Position exit logic
    # ------------------------------------------------------------------

    async def _check_position_exits(self, ticker: Ticker, trader: Trader) -> None:
        """Check exits for this ticker's position.

        Also handles the case where a YES ticker OrderBookEvent arrives but we
        hold a NO position: the NO orderbook is often empty (no real bids/asks),
        so we derive the NO price from the YES bid (NO ≈ 1 - YES_bid) and run
        the same exit logic.
        """
        await self._check_one_position(ticker, trader)

        # If this is a YES ticker, also check the paired NO position.
        # This matters when the NO token has no real orderbook of its own.
        no_ticker = getattr(ticker, 'get_no_ticker', lambda: None)()
        if no_ticker and no_ticker.symbol in self._position_meta:
            yes_bid = trader.market_data.get_best_bid(ticker)
            if yes_bid is not None:
                no_price_derived = float(Decimal('1') - yes_bid.price)
                await self._check_one_position(
                    no_ticker,
                    trader,
                    price_override=no_price_derived,
                )

    async def _check_one_position(
        self,
        ticker: Ticker,
        trader: Trader,
        *,
        price_override: float | None = None,
    ) -> None:
        """Core exit logic for a single position.

        Exit rules (in priority order):
        1. Edge reversed: market price crossed past our LLM estimate
        2. Edge consumed: market moved toward us, remaining edge < 3%
        3. Timeout: held too long, close for capital efficiency
        4. LLM re-evaluation: periodically re-ask LLM if it still agrees
        """
        position = trader.position_manager.get_position(ticker)
        if position is None or position.quantity <= 0:
            return

        # Prevent concurrent close attempts on the same ticker
        if ticker.symbol in self._closing_in_progress:
            return

        meta = self._position_meta.get(ticker.symbol)
        if meta is None:
            return  # No metadata, position was opened before strategy started

        if price_override is not None:
            current_price = price_override
        else:
            bid = trader.market_data.get_best_bid(ticker)
            if bid is None:
                return
            current_price = float(bid.price)

        now = datetime.now()
        llm_prob = meta.llm_prob

        # --- Rule 1 & 2: Edge check (fast, no LLM call) ---
        close_reason = self._check_edge(meta, current_price)
        if close_reason:
            await self._close_position(
                ticker,
                trader,
                position.quantity,
                current_price,
                action=close_reason,
                reasoning=self._edge_reasoning(meta, current_price, close_reason),
                llm_prob=llm_prob,
            )
            return

        # --- Rule 3: Timeout ---
        if now - meta.entry_time > self.max_holding:
            # entry_price is always the actual token price paid (YES or NO)
            pnl = current_price - meta.entry_price
            await self._close_position(
                ticker,
                trader,
                position.quantity,
                current_price,
                action='CLOSE_TIMEOUT',
                reasoning=f'Held {self.max_holding.seconds // 60}min, pnl={pnl:+.2%}',
                llm_prob=llm_prob,
            )
            return

        # --- Rule 4: Periodic LLM re-evaluation ---
        if now - meta.last_reeval_time > self.reeval_cooldown:
            meta.last_reeval_time = now
            await self._reeval_position(ticker, trader, meta, current_price)

    def _check_edge(self, meta: PositionMeta, current_price: float) -> str | None:
        """Check if the edge has been consumed or reversed.

        For a YES position (we bought YES, bullish):
          - Original edge = llm_prob - entry_price (positive)
          - Current edge = llm_prob - current_price
          - Edge consumed: current_price rose toward llm_prob, edge < threshold
          - Edge reversed: current_price rose PAST llm_prob

        For a NO position (we bought NO, bearish):
          - Original edge = entry_price - llm_prob (positive)
          - Current edge = current_price - llm_prob
          - Edge consumed: current_price fell toward llm_prob, edge < threshold
          - Edge reversed: current_price fell PAST llm_prob
        """
        llm_prob = meta.llm_prob

        if meta.side == 'yes':
            # We're long YES: we profit when price goes UP
            remaining_edge = llm_prob - current_price
            if remaining_edge < 0:
                return 'CLOSE_EDGE_REV'  # Market went past our estimate
            if remaining_edge < float(self.edge_consumed_threshold):
                return 'CLOSE_EDGE_TP'  # Edge consumed, take profit
        else:
            # We're long NO: we profit when NO price goes UP (YES price goes DOWN)
            # NO fair value = 1 - llm_prob (since llm_prob is the YES probability)
            no_fair_value = 1.0 - llm_prob
            remaining_edge = no_fair_value - current_price
            if remaining_edge < 0:
                return 'CLOSE_EDGE_REV'  # Market went past our estimate
            if remaining_edge < float(self.edge_consumed_threshold):
                return 'CLOSE_EDGE_TP'  # Edge consumed, take profit

        return None

    def _edge_reasoning(
        self, meta: PositionMeta, current_price: float, action: str
    ) -> str:
        if meta.side == 'no':
            # entry_price is already the actual NO token price paid
            no_entry = meta.entry_price
            direction = 'up' if current_price > no_entry else 'down'
            pnl_pct = (current_price - no_entry) / no_entry if no_entry > 0 else 0
            no_fair = 1.0 - meta.llm_prob
            if action == 'CLOSE_EDGE_TP':
                return (
                    f'Edge consumed (NO): NO price moved {direction} to {current_price:.0%}, '
                    f'NO fair value={no_fair:.0%}, pnl={pnl_pct:+.1%}'
                )
            elif action == 'CLOSE_EDGE_REV':
                return (
                    f'Edge reversed (NO): NO mkt={current_price:.0%} > NO fair={no_fair:.0%}, '
                    f'position wrong'
                )
            return f'Close: {action}'
        else:
            direction = 'up' if current_price > meta.entry_price else 'down'
            pnl_pct = (
                (current_price - meta.entry_price) / meta.entry_price
                if meta.entry_price > 0
                else 0
            )
            if action == 'CLOSE_EDGE_TP':
                return (
                    f'Edge consumed: mkt moved {direction} to {current_price:.0%}, '
                    f'LLM was {meta.llm_prob:.0%}, pnl={pnl_pct:+.1%}'
                )
            elif action == 'CLOSE_EDGE_REV':
                return (
                    f'Edge reversed: mkt={current_price:.0%} > LLM={meta.llm_prob:.0%}, '
                    f'position wrong'
                )
            return f'Close: {action}'

    async def _reeval_position(
        self,
        ticker: Ticker,
        trader: Trader,
        meta: PositionMeta,
        current_price: float,
    ) -> None:
        """Re-evaluate a position by calling LLM again."""
        try:
            # For NO positions: current_price is the NO token price,
            # entry_price is the actual NO token price paid.
            # Convert to YES-equivalent for the LLM prompt.
            if meta.side == 'no':
                yes_implied_price = 1.0 - current_price
                display_entry = meta.entry_price  # actual NO purchase price
            else:
                yes_implied_price = current_price
                display_entry = meta.entry_price

            prompt = f"""You previously estimated the probability of this event.
The market has moved since then. Re-evaluate.

Market: {meta.title}
Your previous estimate: {meta.llm_prob:.0%}
Current YES market price: ${yes_implied_price:.2f} (implies {yes_implied_price * 100:.0f}% probability)
You bought {'YES' if meta.side == 'yes' else 'NO'} at ${display_entry:.2f}

Should you still hold this position? Re-estimate the true probability (of YES happening).

Respond in JSON only:
{{
    "probability": 0.0 to 1.0,
    "reasoning": "one sentence explanation"
}}
"""
            # Auto-select API: prefer DeepSeek, fallback to OpenAI
            deepseek_key = os.environ.get('DEEPSEEK_API_KEY', '')
            openai_key = os.environ.get('OPENAI_API_KEY', '')
            if deepseek_key:
                api_key, api_url, model = deepseek_key, DEEPSEEK_URL, 'deepseek-chat'
            else:
                api_key, api_url, model = openai_key, OPENAI_URL, 'gpt-4o-mini'

            payload = {
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'stream': False,
                'max_tokens': 256,
                'temperature': 0.3,
                'response_format': {'type': 'json_object'},
            }

            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(api_url, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()

            content = result['choices'][0]['message']['content']
            analysis = json.loads(content)

            new_prob = float(analysis.get('probability', meta.llm_prob))
            reasoning = analysis.get('reasoning', '')

            self.logger.info(
                f'Re-eval {meta.title[:25]}: '
                f'old={meta.llm_prob:.0%} new={new_prob:.0%} mkt={current_price:.0%}'
            )

            # Update the LLM estimate
            old_prob = meta.llm_prob
            meta.llm_prob = new_prob

            # Check if LLM changed its mind
            if meta.side == 'yes':
                # We hold YES. If LLM now thinks prob < market → edge gone
                new_edge = new_prob - current_price
                if new_edge < float(self.edge_consumed_threshold):
                    position = trader.position_manager.get_position(ticker)
                    if position and position.quantity > 0:
                        await self._close_position(
                            ticker,
                            trader,
                            position.quantity,
                            current_price,
                            action='CLOSE_REEVAL',
                            reasoning=(
                                f'LLM revised: {old_prob:.0%}→{new_prob:.0%}, '
                                f'mkt={current_price:.0%}, edge gone. {reasoning}'
                            ),
                            llm_prob=new_prob,
                        )
            elif meta.side == 'no':
                # We hold NO. NO fair value = 1 - YES prob.
                # Edge = NO_fair - NO_price (same logic as _check_edge)
                no_fair_value = 1.0 - new_prob
                new_edge = no_fair_value - current_price
                if new_edge < float(self.edge_consumed_threshold):
                    position = trader.position_manager.get_position(ticker)
                    if position and position.quantity > 0:
                        await self._close_position(
                            ticker,
                            trader,
                            position.quantity,
                            current_price,
                            action='CLOSE_REEVAL',
                            reasoning=(
                                f'LLM revised: {old_prob:.0%}→{new_prob:.0%}, '
                                f'mkt={current_price:.0%}, edge gone. {reasoning}'
                            ),
                            llm_prob=new_prob,
                        )

        except Exception as e:
            self.logger.error(f'Re-eval error for {meta.title[:25]}: {e}')

    async def _close_position(
        self,
        ticker: Ticker,
        trader: Trader,
        quantity: Decimal,
        current_price: float,
        action: str,
        reasoning: str,
        llm_prob: float,
    ) -> None:
        """Close a position and record the decision."""
        self._closing_in_progress.add(ticker.symbol)
        try:
            await self._do_close(
                ticker,
                trader,
                quantity,
                current_price,
                action,
                reasoning,
                llm_prob,
            )
        finally:
            self._closing_in_progress.discard(ticker.symbol)

    async def _do_close(
        self,
        ticker: Ticker,
        trader: Trader,
        quantity: Decimal,
        current_price: float,
        action: str,
        reasoning: str,
        llm_prob: float,
    ) -> None:
        """Internal close logic."""
        result = await trader.place_order(
            side=TradeSide.SELL,
            ticker=ticker,
            limit_price=Decimal(str(current_price)),
            quantity=quantity,
        )
        executed = result.order is not None
        if executed:
            self._position_meta.pop(ticker.symbol, None)
            self._close_failures.pop(ticker.symbol, None)
        else:
            # Count consecutive failures; drop meta after 3 to avoid infinite retry
            # (e.g. market already resolved, token delisted)
            failures = self._close_failures.get(ticker.symbol, 0) + 1
            self._close_failures[ticker.symbol] = failures
            if failures >= 3:
                self.logger.warning(
                    f'Close failed {failures}x for {ticker.symbol[:30]}, '
                    f'dropping position meta (market may be resolved/delisted)'
                )
                self._position_meta.pop(ticker.symbol, None)
                self._close_failures.pop(ticker.symbol, None)

        self.record_decision(
            ticker_name=f'{action[:12]}: {ticker.name[:25]}',
            action=action,
            executed=executed,
            reasoning=reasoning,
            signal_values={
                'llm_prob': llm_prob,
                'market_price': current_price,
                'edge': round(llm_prob - current_price, 4),
            },
        )

    # ------------------------------------------------------------------
    # Opening trade logic
    # ------------------------------------------------------------------

    def _get_market_price(self, ticker: Ticker, trader: Trader) -> float:
        """Get the current market implied probability (best ask price)."""
        ask = trader.market_data.get_best_ask(ticker)
        if ask is not None:
            return float(ask.price)
        bid = trader.market_data.get_best_bid(ticker)
        if bid is not None:
            return float(bid.price)
        return 0.0

    async def _analyze_news_with_llm(
        self, event: NewsEvent, market_price: float
    ) -> LLMAnalysisResult:
        """Call the LLM API to estimate the true probability of this event."""
        try:
            price_info = ''
            if market_price > 0:
                price_info = f'\nCurrent market price: ${market_price:.2f} (implies {market_price * 100:.0f}% probability)'

            # Find relevant Google News
            relevant_news = self._find_relevant_news(event.title or '')
            news_section = ''
            if relevant_news:
                news_section = '\n\nRecent relevant news:\n' + '\n'.join(
                    f'- {n}' for n in relevant_news
                )

            details_text = (event.news or '')[:500]
            prompt = f"""You are a prediction market analyst. Estimate the TRUE probability that this event will happen.

This is a binary contract: pays $1 if YES, $0 if NO.{price_info}

Market: {event.title}
Details: {details_text}{news_section}

Your job: estimate the real probability (0.0 to 1.0) based on ALL available information.
If you have relevant news, use it to inform your estimate — the market may not have priced in recent developments.

Respond in JSON only:
{{
    "probability": 0.0 to 1.0,
    "reasoning": "one sentence explanation"
}}
"""

            # Auto-select API: prefer DeepSeek, fallback to OpenAI
            deepseek_key = os.environ.get('DEEPSEEK_API_KEY', '')
            openai_key = os.environ.get('OPENAI_API_KEY', '')
            if deepseek_key:
                api_key, api_url, model = deepseek_key, DEEPSEEK_URL, 'deepseek-chat'
            else:
                api_key, api_url, model = openai_key, OPENAI_URL, 'gpt-4o-mini'

            payload = {
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'stream': False,
                'max_tokens': 256,
                'temperature': 0.3,
                'response_format': {'type': 'json_object'},
            }

            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(api_url, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()

            content = result['choices'][0]['message']['content']
            analysis = json.loads(content)

            if 'probability' not in analysis:
                raise ValueError('LLM response missing probability field')

            llm_prob = float(analysis['probability'])
            reasoning = analysis.get('reasoning', '')

            return self._decide_action(llm_prob, market_price, reasoning)

        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
        ) as e:
            self.logger.error(f'Error analyzing news with LLM: {e}')
            return {
                'action': 'hold',
                'confidence': 0.0,
                'reasoning': f'Analysis error: {str(e)}',
                'llm_prob': 0.0,
                'market_price': market_price,
            }

    def _decide_action(
        self, llm_prob: float, market_price: float, reasoning: str
    ) -> LLMAnalysisResult:
        """Compare LLM probability estimate to market price and decide action.

        - If LLM prob > market price + edge → BUY YES (underpriced)
        - If LLM prob < market price - edge → BUY NO  (overpriced)
        - Otherwise → HOLD (no edge)
        """
        edge = float(self.edge_threshold)
        diff = llm_prob - market_price

        if market_price <= 0:
            return {
                'action': 'hold',
                'confidence': 0.0,
                'reasoning': 'No market price available',
                'llm_prob': llm_prob,
                'market_price': market_price,
            }

        estimated_spread = float(self.estimated_spread)
        effective_edge = abs(diff) - estimated_spread

        if effective_edge <= 0:
            return {
                'action': 'hold',
                'confidence': abs(diff),
                'reasoning': f'Edge {abs(diff):.0%} too small after spread ~{estimated_spread:.0%}',
                'llm_prob': llm_prob,
                'market_price': market_price,
            }

        if diff > edge:
            confidence = min(abs(diff), 1.0)
            return {
                'action': 'buy_yes',
                'confidence': confidence,
                'reasoning': f'LLM {llm_prob:.0%} > mkt {market_price:.0%}: {reasoning}',
                'llm_prob': llm_prob,
                'market_price': market_price,
            }
        elif diff < -edge:
            confidence = min(abs(diff), 1.0)
            return {
                'action': 'buy_no',
                'confidence': confidence,
                'reasoning': f'LLM {llm_prob:.0%} < mkt {market_price:.0%}: {reasoning}',
                'llm_prob': llm_prob,
                'market_price': market_price,
            }
        else:
            return {
                'action': 'hold',
                'confidence': abs(diff),
                'reasoning': f'No edge (LLM {llm_prob:.0%} ≈ mkt {market_price:.0%})',
                'llm_prob': llm_prob,
                'market_price': market_price,
            }

    async def _execute_trade(
        self, analysis: LLMAnalysisResult, ticker: Ticker, trader: Trader
    ) -> tuple[bool, float]:
        """Execute a trade based on the LLM analysis.

        Returns:
            (executed, fill_price) — fill_price is the actual execution price
            for the token bought (YES or NO).
        """
        try:
            action = analysis['action']

            if action == 'buy_yes':
                level = trader.market_data.get_best_ask(ticker)
                if level is None:
                    self.logger.info(f'No ask for {ticker.name[:30]}, skip')
                    return False, 0.0

                self.logger.info(f'BUY YES {ticker.name[:30]} @ ${level.price}')
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=level.price,
                    quantity=self.trade_size,
                )
                self.logger.info(f'BUY YES result: {result}')
                if result.order is not None:
                    fill = (
                        float(result.order.average_price)
                        if result.order.average_price > 0
                        else float(level.price)
                    )
                    return True, fill
                return False, 0.0

            elif action == 'buy_no':
                return await self._execute_buy_no(ticker, trader, analysis)

            else:
                self.logger.info(f'HOLD {ticker.name[:30]}')
                return False, 0.0

        except Exception as e:
            self.logger.error(f'Error executing trade: {e}')
            return False, 0.0

    async def _execute_buy_no(
        self, ticker: Ticker, trader: Trader, analysis: LLMAnalysisResult
    ) -> tuple[bool, float]:
        """Execute a BUY NO trade.

        Polymarket: Buy the NO token directly (using no_token_id).
        Kalshi: Sell existing YES position, or skip if none.

        Returns:
            (executed, fill_price) — fill_price is the actual NO token execution price.
        """
        if isinstance(ticker, PolyMarketTicker) and ticker.no_token_id:
            no_ticker = ticker.get_no_ticker()
            if no_ticker is None:
                self.logger.info('No NO ticker available, skip')
                return False, 0.0

            level = trader.market_data.get_best_ask(no_ticker)
            if level is None:
                yes_bid = trader.market_data.get_best_bid(ticker)
                if yes_bid is not None:
                    no_price = Decimal('1') - yes_bid.price
                    self.logger.info(
                        f'BUY NO {ticker.name[:30]} @ ${no_price} (estimated from YES bid)'
                    )
                    result = await trader.place_order(
                        side=TradeSide.BUY,
                        ticker=no_ticker,
                        limit_price=no_price,
                        quantity=self.trade_size,
                    )
                    if result.order is not None:
                        fill = (
                            float(result.order.average_price)
                            if result.order.average_price > 0
                            else float(no_price)
                        )
                        return True, fill
                    return False, 0.0
                self.logger.info('No price for NO token, skip')
                return False, 0.0

            self.logger.info(f'BUY NO {ticker.name[:30]} @ ${level.price}')
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=no_ticker,
                limit_price=level.price,
                quantity=self.trade_size,
            )
            self.logger.info(f'BUY NO result: {result}')
            if result.order is not None:
                fill = (
                    float(result.order.average_price)
                    if result.order.average_price > 0
                    else float(level.price)
                )
                return True, fill
            return False, 0.0

        # Kalshi: buy No side directly via get_no_ticker()
        if isinstance(ticker, KalshiTicker):
            no_ticker = ticker.get_no_ticker()
            if no_ticker is None:
                self.logger.info('No NO ticker available for Kalshi, skip')
                return False, 0.0

            level = trader.market_data.get_best_ask(no_ticker)
            if level is None:
                # Estimate No price from Yes bid
                yes_bid = trader.market_data.get_best_bid(ticker)
                if yes_bid is not None:
                    no_price = Decimal('1') - yes_bid.price
                    self.logger.info(
                        f'BUY NO {ticker.name[:30]} @ ${no_price} (Kalshi, estimated)'
                    )
                    result = await trader.place_order(
                        side=TradeSide.BUY,
                        ticker=no_ticker,
                        limit_price=no_price,
                        quantity=self.trade_size,
                    )
                    if result.order is not None:
                        fill = (
                            float(result.order.average_price)
                            if result.order.average_price > 0
                            else float(no_price)
                        )
                        return True, fill
                    return False, 0.0
                self.logger.info('No price for Kalshi NO token, skip')
                return False, 0.0

            self.logger.info(f'BUY NO {ticker.name[:30]} @ ${level.price} (Kalshi)')
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=no_ticker,
                limit_price=level.price,
                quantity=self.trade_size,
            )
            if result.order is not None:
                fill = (
                    float(result.order.average_price)
                    if result.order.average_price > 0
                    else float(level.price)
                )
                return True, fill
            return False, 0.0

        # Fallback: no NO token available — sell YES position if we have one
        position = trader.position_manager.get_position(ticker)
        if position is not None and position.quantity >= self.trade_size:
            level = trader.market_data.get_best_bid(ticker)
            if level is None:
                return False, 0.0
            self.logger.info(f'SELL YES {ticker.name[:30]} @ ${level.price} (bearish)')
            result = await trader.place_order(
                side=TradeSide.SELL,
                ticker=ticker,
                limit_price=level.price,
                quantity=self.trade_size,
            )
            if result.order is not None:
                fill = (
                    float(result.order.average_price)
                    if result.order.average_price > 0
                    else float(level.price)
                )
                return True, fill
            return False, 0.0

        self.logger.info(
            f'BUY_NO skipped — no NO token and no YES position for {ticker.name[:30]}'
        )
        return False, 0.0

    # ------------------------------------------------------------------
    # Generic strategy interface
    # ------------------------------------------------------------------
