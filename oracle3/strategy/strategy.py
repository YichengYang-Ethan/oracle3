from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from oracle3.data.market_data_manager import MarketDataPoint
from oracle3.events.events import Event
from oracle3.ticker.ticker import CashTicker, Ticker
from oracle3.trader.trader import Trader


@dataclass
class StrategyDecision:
    """Generic decision record emitted by any strategy."""

    timestamp: str  # HH:MM:SS
    ticker_name: str
    action: str  # BUY_YES / BUY_NO / HOLD / CLOSE_* etc.
    executed: bool
    reasoning: str = ''
    confidence: float = 0.0  # 0.0 if not applicable
    signal_values: dict[str, float] = field(default_factory=dict)
    # Examples:
    # LLM:           {'llm_prob': 0.72, 'market_price': 0.55, 'edge': 0.17}
    # OBI:           {'imbalance': 0.42, 'bid_vol': 1200.0, 'ask_vol': 450.0}
    # Momentum:      {'momentum': 0.031}
    # MeanReversion: {'z_score': -1.8, 'mean': 0.52, 'std': 0.04}
    # MarketMaking:  {'spread': 0.08, 'mid': 0.50}


@dataclass(frozen=True)
class StrategyOrderBookView:
    symbol: str
    name: str
    best_bid: float | None
    best_ask: float | None
    bid_size: float | None
    ask_size: float | None


@dataclass(frozen=True)
class StrategyPositionView:
    symbol: str
    name: str
    quantity: float
    average_cost: float
    realized_pnl: float
    is_cash: bool


@dataclass(frozen=True)
class StrategyContext:
    """Unified runtime context bound before every strategy invocation."""

    event: Event
    trader: Trader

    @property
    def event_type(self) -> str:
        return type(self.event).__name__

    @property
    def ticker(self) -> Ticker | None:
        return getattr(self.event, 'ticker', None)

    @property
    def event_timestamp(self) -> object:
        return getattr(self.event, 'timestamp', None) or getattr(
            self.event, 'published_at', None
        )

    def market_history(
        self, ticker: Ticker | None = None, limit: int | None = None
    ) -> list[MarketDataPoint]:
        return self.trader.market_data.get_market_history(ticker=ticker, limit=limit)

    def ticker_history(self, limit: int | None = None) -> list[MarketDataPoint]:
        if self.ticker is None:
            return []
        return self.market_history(ticker=self.ticker, limit=limit)

    def price_history(self, ticker: Ticker | None = None, limit: int | None = None):
        target = ticker or self.ticker
        if target is None:
            return []
        return self.trader.market_data.get_price_history(target, limit=limit)

    def order_books(self, limit: int | None = None) -> list[StrategyOrderBookView]:
        rows: list[StrategyOrderBookView] = []
        for ticker, _order_book in self.trader.market_data.order_books.items():
            bid = self.trader.market_data.get_best_bid(ticker)
            ask = self.trader.market_data.get_best_ask(ticker)
            rows.append(
                StrategyOrderBookView(
                    symbol=ticker.symbol,
                    name=getattr(ticker, 'name', '') or ticker.symbol,
                    best_bid=float(bid.price) if bid is not None else None,
                    best_ask=float(ask.price) if ask is not None else None,
                    bid_size=float(bid.size) if bid is not None else None,
                    ask_size=float(ask.size) if ask is not None else None,
                )
            )
        if limit is not None:
            if limit <= 0:
                return []
            return rows[:limit]
        return rows

    def available_tickers(
        self,
        limit: int | None = None,
        *,
        include_complements: bool = True,
    ) -> list[Ticker]:
        tickers = list(self.trader.market_data.order_books.keys())
        if not include_complements:
            tickers = [
                ticker for ticker in tickers if not ticker.symbol.endswith('_NO')
            ]
        if limit is not None:
            if limit <= 0:
                return []
            return tickers[:limit]
        return tickers

    def resolve_ticker(self, symbol: str) -> Ticker | None:
        for ticker in self.trader.market_data.order_books:
            if ticker.symbol == symbol:
                return ticker
        return None

    def resolve_trade_ticker(self, symbol: str, side: str = 'yes') -> Ticker | None:
        ticker = self.resolve_ticker(symbol)
        if ticker is None:
            return None
        side_normalized = side.strip().lower()
        if side_normalized == 'no':
            return getattr(ticker, 'get_no_ticker', lambda: None)()
        return ticker

    def positions(self) -> list[StrategyPositionView]:
        views: list[StrategyPositionView] = []
        for pos in self.trader.position_manager.positions.values():
            ticker = pos.ticker
            views.append(
                StrategyPositionView(
                    symbol=ticker.symbol,
                    name=getattr(ticker, 'name', '') or ticker.symbol,
                    quantity=float(pos.quantity),
                    average_cost=float(pos.average_cost),
                    realized_pnl=float(pos.realized_pnl),
                    is_cash=isinstance(ticker, CashTicker),
                )
            )
        return views

    def cash_positions(self) -> list[StrategyPositionView]:
        return [pos for pos in self.positions() if pos.is_cash]

    def active_positions(self) -> list[StrategyPositionView]:
        return [pos for pos in self.positions() if not pos.is_cash and pos.quantity > 0]

    def recent_news(self, limit: int | None = None) -> list[dict[str, str]]:
        return self.trader.get_recent_news(limit=limit)


class Strategy(ABC):
    """Base class for all trading strategies.

    Provides:
    - Metadata ClassVars: ``name``, ``version``, ``author``
    - ``strategy_type`` and ``supports_auto_tune()`` for engine integration
    - Pause/resume via ``set_paused`` / ``is_paused``
    - Decision recording via ``record_decision`` (shared buffer + counters)
    - Lifecycle hooks: ``on_start`` / ``on_stop``
    - ``param_schema()`` classmethod for auto-tune integration
    - ``StrategyContext`` binding via ``bind_context`` / ``require_context``

    Subclasses MUST implement ``process_event``.
    """

    # -- Metadata (override in subclasses) -----------------------------------
    name: ClassVar[str] = ''
    version: ClassVar[str] = '0.1.0'
    author: ClassVar[str] = ''
    strategy_type: ClassVar[str] = 'generic'

    @classmethod
    def supports_auto_tune(cls) -> bool:
        """Return True if this strategy can participate in parameter grid search.

        QuantStrategy overrides to True; AgentStrategy stays False.
        Checked by `research discover-alpha` before running param combos.
        """
        return False

    def __init__(self) -> None:
        self._paused: bool = False
        self._decisions: deque[StrategyDecision] = deque(maxlen=200)
        self._decision_stats_cache: dict[str, int] = {
            'decisions': 0,
            'executed': 0,
            'buy_yes': 0,
            'buy_no': 0,
            'sells': 0,
            'closes': 0,
            'holds': 0,
        }

    # -- Pause / resume ------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        """Set control-plane pause state for this strategy."""
        self._paused = paused

    def is_paused(self) -> bool:
        """Return whether control-plane has paused decision-making."""
        return getattr(self, '_paused', False)

    # -- Abstract event handler ----------------------------------------------

    @abstractmethod
    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process an event.

        Strategies can inspect cumulative replay state through
        ``self.require_context()`` or ``trader.market_data``.
        """
        pass

    # -- Lifecycle hooks (optional overrides) --------------------------------

    async def on_start(self) -> None:
        """Called once when the engine starts, before the first event."""

    async def on_stop(self) -> None:
        """Called once when the engine shuts down, after the last event."""

    # -- Context binding -----------------------------------------------------

    def bind_context(self, event: Event, trader: Trader) -> StrategyContext:
        """Bind the shared strategy context for this timestep."""
        context = StrategyContext(event=event, trader=trader)
        self._runtime_context = context
        return context

    def get_context(self) -> StrategyContext | None:
        context = getattr(self, '_runtime_context', None)
        return context if isinstance(context, StrategyContext) else None

    def require_context(self) -> StrategyContext:
        context = self.get_context()
        if context is None:
            raise RuntimeError(
                'Strategy context is not bound. '
                'Use the trading engine or call bind_context(...) before processing.'
            )
        return context

    # -- Decision recording --------------------------------------------------

    def record_decision(
        self,
        *,
        ticker_name: str,
        action: str,
        executed: bool,
        reasoning: str = '',
        confidence: float = 0.0,
        signal_values: dict[str, float] | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Record a strategy decision in the shared buffer.

        Updates both the decision deque and the running counters.
        All built-in strategies should call this instead of maintaining
        their own deque + counters.
        """
        signal_payload = signal_values or {}
        self._ensure_init()
        self._decisions.append(
            StrategyDecision(
                timestamp=timestamp or datetime.now().strftime('%H:%M:%S'),
                ticker_name=ticker_name[:40],
                action=action,
                executed=executed,
                reasoning=reasoning,
                confidence=confidence,
                signal_values=signal_payload,
            )
        )

        stats = self._decision_stats_cache
        stats['decisions'] += 1
        action_key = action.upper()
        if action_key == 'HOLD':
            stats['holds'] += 1
            return

        if not executed:
            return

        stats['executed'] += 1
        if action_key in {'BUY', 'BUY_YES'}:
            stats['buy_yes'] += 1
        elif action_key == 'BUY_NO':
            stats['buy_no'] += 1
        elif action_key.startswith('SELL'):
            stats['sells'] += 1
        elif action_key.startswith('CLOSE'):
            stats['closes'] += 1

    def get_decisions(self) -> list[StrategyDecision]:
        """Return recent strategy decisions."""
        self._ensure_init()
        return list(self._decisions)

    def get_decision_stats(self) -> dict[str, int | float]:
        """Return running decision counters."""
        self._ensure_init()
        return dict(self._decision_stats_cache)

    def _ensure_init(self) -> None:
        """Lazily initialise buffers if __init__ was not called (backward compat)."""
        if not hasattr(self, '_decisions'):
            self._decisions = deque(maxlen=200)
        if not hasattr(self, '_decision_stats_cache'):
            self._decision_stats_cache = {
                'decisions': 0,
                'executed': 0,
                'buy_yes': 0,
                'buy_no': 0,
                'sells': 0,
                'closes': 0,
                'holds': 0,
            }
        if not hasattr(self, '_paused'):
            self._paused = False

    # -- Param schema (for auto-tune) ----------------------------------------

    @classmethod
    def param_schema(cls) -> dict[str, dict[str, Any]]:
        """Return the tunable parameters of this strategy.

        Inspects ``__init__`` signature and returns a dict mapping parameter
        names to metadata (type, default). Used by auto-tune to generate
        parameter grids automatically.

        Example return value::

            {
                'entry_threshold': {'type': 'float', 'default': 0.3},
                'position_size': {'type': 'Decimal', 'default': Decimal('10')},
            }
        """
        schema: dict[str, dict[str, Any]] = {}
        sig = inspect.signature(cls.__init__)
        for name, param in sig.parameters.items():
            if name == 'self':
                continue
            info: dict[str, Any] = {}
            if param.annotation is not inspect.Parameter.empty:
                ann = param.annotation
                info['type'] = ann.__name__ if hasattr(ann, '__name__') else str(ann)
            if param.default is not inspect.Parameter.empty:
                info['default'] = param.default
            schema[name] = info
        return schema
