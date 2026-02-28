from __future__ import annotations

from decimal import Decimal

from oracle3.events.events import Event, PriceChangeEvent
from oracle3.strategy.agent_strategy import AgentStrategy
from oracle3.ticker.ticker import Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class RelatedMarketAgentStrategy(AgentStrategy):
    """Cross-market agent strategy that trades lagging markets.

    The strategy scans all visible markets in the bound StrategyContext,
    finds the most related market by recent return correlation, and buys the
    current market when that related market has already moved more than the
    current market by ``divergence_threshold``.
    """

    def __init__(
        self,
        trade_size: Decimal = Decimal('10'),
        lookback: int = 6,
        min_correlation: float = 0.60,
        divergence_threshold: float = 0.04,
    ) -> None:
        self.trade_size = Decimal(str(trade_size))
        self.lookback = max(3, int(lookback))
        self.min_correlation = float(min_correlation)
        self.divergence_threshold = float(divergence_threshold)

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, PriceChangeEvent):
            return

        context = self.require_context()
        current_ticker = event.ticker
        current_prices = context.price_history(limit=self.lookback + 1)
        if len(current_prices) < self.lookback:
            return

        best_relation = self._find_best_related_market(context, current_ticker)
        if best_relation is None:
            self.record_decision(
                ticker_name=current_ticker.name or current_ticker.symbol,
                action='HOLD',
                executed=False,
                reasoning='no related market with sufficient overlap',
                signal_values={},
            )
            return

        related_ticker, correlation, divergence, lead_move = best_relation
        reasoning = (
            f'related={related_ticker.symbol} corr={correlation:+.3f} '
            f'divergence={divergence:+.3f} lead_move={lead_move:+.3f}'
        )

        position = trader.position_manager.get_position(current_ticker)
        has_position = position is not None and position.quantity > 0

        if (
            not has_position
            and correlation >= self.min_correlation
            and divergence >= self.divergence_threshold
        ):
            best_ask = trader.market_data.get_best_ask(current_ticker)
            if best_ask is None:
                return
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=current_ticker,
                limit_price=best_ask.price,
                quantity=self.trade_size,
            )
            executed = result.order is not None and result.order.filled_quantity > 0
            self.record_decision(
                ticker_name=current_ticker.name or current_ticker.symbol,
                action='BUY_YES',
                executed=executed,
                reasoning=reasoning,
                signal_values={
                    'correlation': correlation,
                    'divergence': divergence,
                    'lead_move': lead_move,
                },
            )
            return

        if has_position and divergence <= -self.divergence_threshold:
            best_bid = trader.market_data.get_best_bid(current_ticker)
            if best_bid is None:
                return
            result = await trader.place_order(
                side=TradeSide.SELL,
                ticker=current_ticker,
                limit_price=best_bid.price,
                quantity=position.quantity,
            )
            executed = result.order is not None and result.order.filled_quantity > 0
            self.record_decision(
                ticker_name=current_ticker.name or current_ticker.symbol,
                action='CLOSE_RELATION',
                executed=executed,
                reasoning=reasoning,
                signal_values={
                    'correlation': correlation,
                    'divergence': divergence,
                    'lead_move': lead_move,
                },
            )
            return

        self.record_decision(
            ticker_name=current_ticker.name or current_ticker.symbol,
            action='HOLD',
            executed=False,
            reasoning=reasoning,
            signal_values={
                'correlation': correlation,
                'divergence': divergence,
                'lead_move': lead_move,
            },
        )

    def _find_best_related_market(
        self,
        context,
        current_ticker: Ticker,
    ) -> tuple[Ticker, float, float, float] | None:
        current_prices = context.price_history(current_ticker, limit=self.lookback + 1)
        current_returns = self._returns(current_prices)
        if len(current_returns) < 2:
            return None

        best: tuple[Ticker, float, float, float] | None = None
        best_score = float('-inf')
        for ticker in context.available_tickers():
            if ticker.symbol == current_ticker.symbol:
                continue
            if ticker.symbol.endswith('_NO'):
                continue
            other_prices = context.price_history(ticker, limit=self.lookback + 1)
            other_returns = self._returns(other_prices)
            overlap = min(len(current_returns), len(other_returns))
            if overlap < 2:
                continue
            corr = self._correlation(
                current_returns[-overlap:], other_returns[-overlap:]
            )
            if corr is None:
                continue
            lead_move = other_returns[-1]
            divergence = lead_move - current_returns[-1]
            score = corr * abs(divergence)
            if score > best_score:
                best_score = score
                best = (ticker, corr, divergence, lead_move)
        return best

    @staticmethod
    def _returns(prices: list[Decimal]) -> list[float]:
        returns: list[float] = []
        for prev, curr in zip(prices, prices[1:]):
            if prev <= 0:
                continue
            returns.append(float((curr - prev) / prev))
        return returns

    @staticmethod
    def _correlation(xs: list[float], ys: list[float]) -> float | None:
        if len(xs) != len(ys) or len(xs) < 2:
            return None
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        var_x = sum((x - mean_x) ** 2 for x in xs)
        var_y = sum((y - mean_y) ** 2 for y in ys)
        if var_x <= 0 or var_y <= 0:
            return None
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        return cov / (var_x**0.5 * var_y**0.5)
