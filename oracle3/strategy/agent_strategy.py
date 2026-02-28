from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from oracle3.strategy.strategy import Strategy, StrategyContext


def _import_agents_sdk():
    try:
        from agents import Agent, Runner, function_tool
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            'OpenAI Agents SDK is not installed. Install it with `pip install openai-agents` '
            'or `poetry add openai-agents` to enable tool-using AgentStrategy runs.'
        ) from exc
    return Agent, Runner, function_tool


@dataclass
class TradeRequest:
    """A trade request queued by the agent's place_trade tool.

    Stored during the agent run and executed by the strategy after the run
    completes, since ``trader.place_order`` is async and cannot be called
    from inside a synchronous ``function_tool``.
    """

    symbol: str
    side: str  # 'buy' or 'sell'
    quantity: float
    limit_price: float


class AgentStrategy(Strategy):
    """Base for LLM-driven or tool-using strategies.

    These strategies may call external APIs (LLMs, web search, MCP tools) and
    are NOT eligible for parameter grid search — use paper trading for evaluation.
    """

    strategy_type = 'agent'

    @classmethod
    def supports_auto_tune(cls) -> bool:
        return False

    @classmethod
    def sdk_available(cls) -> bool:
        try:
            _import_agents_sdk()
        except RuntimeError:
            return False
        return True

    def get_agent_name(self) -> str:
        return getattr(self, 'agent_name', self.__class__.__name__)

    def get_agent_model(self) -> str:
        return str(getattr(self, 'agent_model', 'gpt-4.1-mini'))

    def get_agent_max_turns(self) -> int:
        return int(getattr(self, 'agent_max_turns', 8))

    def get_prompt_guide(self) -> str:
        """Default operator/LLM guide for agent strategies."""
        return (
            'You are an agent strategy running inside Oracle3, '
            'an AI-native prediction market agent supporting Solana/DFlow, Polymarket, and Kalshi. '
            'Use the bound StrategyContext as the source of truth. '
            'Inspect context.ticker_history(...) and context.price_history(...) '
            'for the current market, context.market_history(...) for cross-market '
            'state, context.available_tickers(include_complements=False) to choose '
            'base tradable markets, and context.resolve_trade_ticker(symbol, side) '
            'when you need the actual YES/NO contract, context.order_books() for current available books, '
            'context.positions() for exposure, and context.recent_news() for '
            'available news. Do not use future information. Only act on data '
            'visible in the current context and place trades through trader.place_order(...).'
        )

    def build_prompt_context(self, context: StrategyContext | None = None) -> str:
        """Render a prompt-friendly snapshot from the unified context."""
        ctx = context or self.require_context()
        ticker = ctx.ticker
        ticker_label = getattr(ticker, 'symbol', 'none')
        ticker_history = ctx.ticker_history(limit=10)
        price_history = ctx.price_history(limit=10)
        related_books = ctx.order_books(limit=10)
        available_tickers = [
            ticker.symbol
            for ticker in ctx.available_tickers(limit=10, include_complements=False)
        ]
        active_positions = ctx.active_positions()
        recent_news = ctx.recent_news(limit=5)

        return '\n'.join(
            [
                self.get_prompt_guide(),
                f'event_type={ctx.event_type}',
                f'event_ticker={ticker_label}',
                f'event_timestamp={ctx.event_timestamp}',
                f'ticker_history_points={len(ticker_history)}',
                f'global_market_points={len(ctx.market_history(limit=200))}',
                f'recent_prices={price_history}',
                f'available_tickers={available_tickers}',
                f'visible_order_books={[book.__dict__ for book in related_books]}',
                f'active_positions={[pos.__dict__ for pos in active_positions]}',
                f'recent_news={recent_news}',
            ]
        )

    def build_agent_instructions(self, context: StrategyContext | None = None) -> str:
        return self.build_prompt_context(context)

    def build_task_input(self, context: StrategyContext | None = None) -> str:
        ctx = context or self.require_context()
        ticker = ctx.ticker
        ticker_symbol = getattr(ticker, 'symbol', 'none')
        event = ctx.event
        fragments = [
            f'Analyze the current trading step for ticker={ticker_symbol}.',
            f'event_type={ctx.event_type}',
        ]
        news = getattr(event, 'title', '') or getattr(event, 'news', '')
        if news:
            fragments.append(f'event_text={news[:300]}')
        if hasattr(event, 'price'):
            fragments.append(f'event_price={event.price}')
        return '\n'.join(fragments)

    def build_openai_tools(self, context: StrategyContext | None = None) -> list[Any]:
        ctx = context or self.require_context()
        _Agent, _Runner, function_tool = _import_agents_sdk()

        # Mutable list shared with the place_trade tool; the strategy reads
        # it after the agent run completes and executes the queued trades.
        pending_trades: list[TradeRequest] = []
        self._pending_trades = pending_trades

        @function_tool
        def place_trade(
            symbol: str,
            side: str,
            quantity: float,
            limit_price: float,
        ) -> str:
            """Queue a trade to be executed after the agent run completes.

            Args:
                symbol: Ticker symbol to trade (e.g. 'BTC-YES').
                side: Trade direction - 'buy' or 'sell'.
                quantity: Number of shares/contracts to trade.
                limit_price: Limit price for the order.

            Returns:
                Confirmation that the trade has been queued.
            """
            side_lower = side.strip().lower()
            if side_lower not in ('buy', 'sell'):
                return f'Invalid side "{side}". Must be "buy" or "sell".'
            if quantity <= 0:
                return f'Invalid quantity {quantity}. Must be > 0.'
            if limit_price <= 0:
                return f'Invalid limit_price {limit_price}. Must be > 0.'
            request = TradeRequest(
                symbol=symbol,
                side=side_lower,
                quantity=quantity,
                limit_price=limit_price,
            )
            pending_trades.append(request)
            return (
                f'Trade queued: {side_lower} {quantity} of {symbol} '
                f'@ {limit_price}. Will execute after analysis completes.'
            )

        @function_tool
        def list_available_tickers() -> list[dict[str, object]]:
            """List currently visible tradable tickers."""
            rows: list[dict[str, object]] = []
            for ticker in ctx.available_tickers(include_complements=False):
                rows.append(
                    {
                        'symbol': ticker.symbol,
                        'name': getattr(ticker, 'name', '') or ticker.symbol,
                        'market_id': getattr(ticker, 'market_id', ''),
                        'event_id': getattr(ticker, 'event_id', ''),
                    }
                )
            return rows

        @function_tool
        def get_ticker_history(symbol: str, limit: int = 20) -> list[dict[str, object]]:
            """Return visible market history for a ticker symbol."""
            ticker = ctx.resolve_ticker(symbol)
            if ticker is None:
                return []
            history = ctx.market_history(ticker=ticker, limit=limit)
            return [self._market_point_to_dict(point) for point in history]

        @function_tool
        def get_price_history(symbol: str, limit: int = 20) -> list[float]:
            """Return recent price history for a ticker symbol."""
            ticker = ctx.resolve_ticker(symbol)
            if ticker is None:
                return []
            return [
                float(price) for price in ctx.price_history(ticker=ticker, limit=limit)
            ]

        @function_tool
        def resolve_trade_contract(symbol: str, side: str = 'yes') -> dict[str, object]:
            """Resolve a base market symbol plus side into the actual tradable contract."""
            ticker = ctx.resolve_trade_ticker(symbol, side)
            if ticker is None:
                return {}
            return {
                'symbol': ticker.symbol,
                'name': getattr(ticker, 'name', '') or ticker.symbol,
                'market_id': getattr(ticker, 'market_id', ''),
                'event_id': getattr(ticker, 'event_id', ''),
                'side': side.strip().lower() or 'yes',
            }

        @function_tool
        def get_order_books(limit: int = 20) -> list[dict[str, object]]:
            """Return current best bid/ask snapshots for visible tickers."""
            return [asdict(book) for book in ctx.order_books(limit=limit)]

        @function_tool
        def get_positions() -> list[dict[str, object]]:
            """Return current portfolio positions."""
            return [asdict(pos) for pos in ctx.positions()]

        @function_tool
        def get_recent_news(limit: int = 10) -> list[dict[str, str]]:
            """Return recent visible news items."""
            return ctx.recent_news(limit=limit)

        return [
            list_available_tickers,
            get_ticker_history,
            get_price_history,
            resolve_trade_contract,
            get_order_books,
            get_positions,
            get_recent_news,
            place_trade,
        ]

    def _resolve_model(self) -> Any:
        """Resolve model, using ChatCompletions adapter for non-OpenAI providers."""
        import os

        model_name = self.get_agent_model()
        base_url = os.environ.get('OPENAI_BASE_URL', '')

        # If using a non-OpenAI provider (DeepSeek, etc.), use chat completions
        if base_url and 'api.openai.com' not in base_url:
            try:
                from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
                from openai import AsyncOpenAI

                client = AsyncOpenAI(base_url=base_url)
                return OpenAIChatCompletionsModel(model=model_name, openai_client=client)
            except ImportError:
                pass

        return model_name

    def create_openai_agent(self, context: StrategyContext | None = None) -> Any:
        ctx = context or self.require_context()
        Agent, _Runner, _function_tool = _import_agents_sdk()
        return Agent(
            name=self.get_agent_name(),
            instructions=self.build_agent_instructions(ctx),
            model=self._resolve_model(),
            tools=self.build_openai_tools(ctx),
        )

    async def run_openai_agent(
        self,
        *,
        input_text: str | None = None,
        context: StrategyContext | None = None,
    ) -> Any:
        ctx = context or self.require_context()
        agent = self.create_openai_agent(ctx)
        _Agent, Runner, _function_tool = _import_agents_sdk()
        return await Runner.run(
            agent,
            input_text or self.build_task_input(ctx),
            max_turns=self.get_agent_max_turns(),
        )

    @staticmethod
    def get_run_final_output(run_result: Any) -> str:
        output = getattr(run_result, 'final_output', '')
        return output if isinstance(output, str) else str(output)

    @staticmethod
    def _market_point_to_dict(point) -> dict[str, object]:
        return {
            'sequence': point.sequence,
            'symbol': point.ticker.symbol,
            'name': getattr(point.ticker, 'name', '') or point.ticker.symbol,
            'event_type': point.event_type,
            'timestamp': point.timestamp,
            'event_price': float(point.event_price)
            if point.event_price is not None
            else None,
            'event_side': point.event_side,
            'event_size': float(point.event_size)
            if point.event_size is not None
            else None,
            'event_size_delta': (
                float(point.event_size_delta)
                if point.event_size_delta is not None
                else None
            ),
            'best_bid': float(point.best_bid) if point.best_bid is not None else None,
            'best_bid_size': (
                float(point.best_bid_size) if point.best_bid_size is not None else None
            ),
            'best_ask': float(point.best_ask) if point.best_ask is not None else None,
            'best_ask_size': (
                float(point.best_ask_size) if point.best_ask_size is not None else None
            ),
        }
