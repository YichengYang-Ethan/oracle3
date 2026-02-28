from __future__ import annotations

from oracle3.strategy.strategy import Strategy, StrategyContext


class QuantStrategy(Strategy):
    """Base for deterministic, numerically-parameterised strategies.

    Subclasses are eligible for parameter grid search via `research auto-tune`.
    Constructor kwargs must be JSON-serialisable numerics or lists of them.
    """

    strategy_type = 'quant'

    @classmethod
    def supports_auto_tune(cls) -> bool:
        return True

    def prepare_data(self, context: StrategyContext | None = None) -> dict[str, object]:
        """Default data-loading helper for quant strategies."""
        ctx = context or self.require_context()
        ticker = ctx.ticker
        return {
            'event_type': ctx.event_type,
            'event_timestamp': ctx.event_timestamp,
            'ticker': getattr(ticker, 'symbol', None),
            'ticker_history': ctx.ticker_history(),
            'price_history': ctx.price_history(),
            'market_history': ctx.market_history(limit=500),
            'available_tickers': ctx.available_tickers(),
            'order_books': ctx.order_books(),
            'active_positions': ctx.active_positions(),
            'recent_news': ctx.recent_news(limit=20),
        }
