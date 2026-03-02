"""Cross-market arbitrage strategy across DFlow, Polymarket, and Kalshi.

Detects same-event price discrepancies across prediction market platforms
and trades when the spread exceeds a configurable threshold.

Agent tool: find_arbitrage_opportunities() -> list[dict]
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.quant_strategy import QuantStrategy
from oracle3.ticker.ticker import Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    {'will', 'the', 'a', 'an', 'of', 'in', 'on', 'by', 'to', 'for', 'be', 'is', 'at'}
)


def _normalize(text: str) -> str:
    """Lower, strip punctuation, remove stopwords."""
    text = re.sub(r'[^a-z0-9\s]', ' ', text.lower())
    tokens = [t for t in text.split() if t not in _STOPWORDS]
    return ' '.join(tokens)


# ---------------------------------------------------------------------------
# Ticker grouping across platforms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerGroup:
    """A group of tickers from different platforms referring to the same event."""

    label: str
    tickers: dict[str, Ticker]  # platform_name -> Ticker
    similarity: float


class TickerGrouper:
    """Group tickers across DFlow/Polymarket/Kalshi by event name similarity."""

    def __init__(self, min_similarity: float = 0.60) -> None:
        self.min_similarity = min_similarity
        self._groups: list[TickerGroup] = []

    def group(self, platform_tickers: dict[str, list[Ticker]]) -> list[TickerGroup]:
        """Group tickers from multiple platforms by name similarity.

        Args:
            platform_tickers: mapping of platform_name -> list of tickers
        """
        platforms = list(platform_tickers.keys())
        if len(platforms) < 2:
            return []

        # Use the first platform as the base for matching
        base_platform = platforms[0]
        other_platforms = platforms[1:]
        base_tickers = platform_tickers[base_platform]

        groups: list[TickerGroup] = []
        for bt in base_tickers:
            bt_name = getattr(bt, 'name', '') or bt.symbol
            if not bt_name:
                continue
            bt_norm = _normalize(bt_name)

            group_tickers: dict[str, Ticker] = {base_platform: bt}
            min_sim = 1.0

            for other in other_platforms:
                best_score = 0.0
                best_ticker: Ticker | None = None
                for ot in platform_tickers[other]:
                    ot_name = getattr(ot, 'name', '') or ot.symbol
                    if not ot_name:
                        continue
                    score = SequenceMatcher(None, bt_norm, _normalize(ot_name)).ratio()
                    if score > best_score:
                        best_score = score
                        best_ticker = ot
                if best_ticker is not None and best_score >= self.min_similarity:
                    group_tickers[other] = best_ticker
                    min_sim = min(min_sim, best_score)

            if len(group_tickers) >= 2:
                groups.append(
                    TickerGroup(
                        label=bt_name[:60],
                        tickers=group_tickers,
                        similarity=round(min_sim, 3),
                    )
                )

        self._groups = groups
        return groups

    @property
    def groups(self) -> list[TickerGroup]:
        return list(self._groups)


# ---------------------------------------------------------------------------
# ArbitrageOpportunity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArbitrageOpportunity:
    """A detected arbitrage opportunity between two markets."""

    market_a: str  # platform:symbol
    market_b: str  # platform:symbol
    price_a: float
    price_b: float
    spread: float
    expected_profit: float
    fees: float
    label: str = ''


# ---------------------------------------------------------------------------
# CrossMarketArbitrageStrategy
# ---------------------------------------------------------------------------


class CrossMarketArbitrageStrategy(QuantStrategy):
    """Detect and trade cross-market arbitrage across DFlow/Polymarket/Kalshi.

    Monitors price events across platforms, matches tickers by event name
    similarity, and places arb trades when the spread exceeds ``min_edge``.
    """

    name = 'cross_market_arbitrage'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        min_edge: float = 0.03,
        trade_size: float = 10.0,
        cooldown_seconds: float = 60.0,
        fee_rate: float = 0.02,
        min_similarity: float = 0.60,
    ) -> None:
        self.min_edge = min_edge
        self.trade_size = Decimal(str(trade_size))
        self.cooldown_seconds = cooldown_seconds
        self.fee_rate = fee_rate

        self._grouper = TickerGrouper(min_similarity=min_similarity)
        # symbol -> latest YES price
        self._prices: dict[str, Decimal] = {}
        # symbol -> platform name
        self._ticker_platform: dict[str, str] = {}
        # symbol -> ticker object
        self._ticker_map: dict[str, Ticker] = {}
        # label -> last arb time
        self._last_arb_time: dict[str, float] = {}
        # Detected opportunities (for agent tool)
        self._opportunities: list[ArbitrageOpportunity] = []
        # Flash loan integration hook (set by FlashLoanArbitrage)
        self.flash_loan_handler: Any = None

    def register_price(self, platform: str, ticker: Ticker, price: Decimal) -> None:
        """Register a price update from a platform."""
        self._prices[ticker.symbol] = price
        self._ticker_platform[ticker.symbol] = platform
        self._ticker_map[ticker.symbol] = ticker

    def find_arbitrage_opportunities(self) -> list[dict[str, Any]]:
        """Agent tool: scan current prices for arbitrage opportunities.

        Returns:
            List of opportunity dicts with keys: market_a, market_b,
            price_a, price_b, spread, expected_profit, fees, label.
        """
        opportunities: list[ArbitrageOpportunity] = []

        # Group symbols by normalized name
        platform_symbols: dict[str, list[str]] = {}
        for symbol, platform in self._ticker_platform.items():
            platform_symbols.setdefault(platform, []).append(symbol)

        # Compare prices across platforms for same-event tickers
        platform_tickers: dict[str, list[Ticker]] = {}
        for platform, symbols in platform_symbols.items():
            platform_tickers[platform] = [
                self._ticker_map[s] for s in symbols if s in self._ticker_map
            ]

        groups = self._grouper.group(platform_tickers)

        for group in groups:
            platforms = list(group.tickers.keys())
            for i in range(len(platforms)):
                for j in range(i + 1, len(platforms)):
                    pa, pb = platforms[i], platforms[j]
                    ta, tb = group.tickers[pa], group.tickers[pb]
                    price_a = self._prices.get(ta.symbol)
                    price_b = self._prices.get(tb.symbol)
                    if price_a is None or price_b is None:
                        continue

                    spread = float(price_a - price_b)
                    edge = abs(spread)
                    fee_cost = float(self.trade_size) * self.fee_rate * 2
                    profit = float(self.trade_size) * edge - fee_cost

                    if edge >= self.min_edge and profit > 0:
                        opp = ArbitrageOpportunity(
                            market_a=f'{pa}:{ta.symbol}',
                            market_b=f'{pb}:{tb.symbol}',
                            price_a=float(price_a),
                            price_b=float(price_b),
                            spread=spread,
                            expected_profit=round(profit, 4),
                            fees=round(fee_cost, 4),
                            label=group.label,
                        )
                        opportunities.append(opp)

        self._opportunities = opportunities
        return [asdict(o) for o in opportunities]

    async def process_event(self, event: Event, trader: Trader) -> None:  # noqa: C901
        if self.is_paused():
            return

        ticker: Ticker | None = None
        price: Decimal | None = None

        if isinstance(event, PriceChangeEvent):
            ticker = event.ticker
            price = event.price
        elif isinstance(event, OrderBookEvent):
            ticker = event.ticker
            price = event.price if event.price > 0 else None
        else:
            return

        if ticker is None or price is None:
            return

        # Detect platform from ticker type
        platform = self._detect_platform(ticker)
        self.register_price(platform, ticker, price)

        # Scan for arb opportunities
        opps = self.find_arbitrage_opportunities()
        if not opps:
            return

        now = time.time()
        for opp_dict in opps:
            label = opp_dict.get('label', '')
            if now - self._last_arb_time.get(label, 0) < self.cooldown_seconds:
                continue
            self._last_arb_time[label] = now

            spread = opp_dict['spread']
            market_a = opp_dict['market_a']
            market_b = opp_dict['market_b']

            # Determine which side to buy YES and which to buy NO
            if spread < 0:
                # market_a is cheaper
                cheap_symbol = market_a.split(':', 1)[-1]
                cheap_ticker = self._ticker_map.get(cheap_symbol)
                if cheap_ticker:
                    await self._place_arb_leg(
                        trader, cheap_ticker, TradeSide.BUY,
                        Decimal(str(opp_dict['price_a'])),
                    )
            else:
                # market_b is cheaper
                cheap_symbol = market_b.split(':', 1)[-1]
                cheap_ticker = self._ticker_map.get(cheap_symbol)
                if cheap_ticker:
                    await self._place_arb_leg(
                        trader, cheap_ticker, TradeSide.BUY,
                        Decimal(str(opp_dict['price_b'])),
                    )

            self.record_decision(
                ticker_name=label[:40],
                action='ARB_TRADE',
                executed=True,
                reasoning=(
                    f'Arb: {market_a} vs {market_b}, '
                    f'spread={spread:.4f}, profit={opp_dict["expected_profit"]:.4f}'
                ),
                signal_values={
                    'spread': spread,
                    'edge': abs(spread),
                    'expected_profit': opp_dict['expected_profit'],
                },
            )

    async def _place_arb_leg(
        self,
        trader: Trader,
        ticker: Ticker,
        side: TradeSide,
        price: Decimal,
    ) -> None:
        """Place one leg of an arbitrage trade."""
        try:
            result = await trader.place_order(
                side=side, ticker=ticker,
                limit_price=price, quantity=self.trade_size,
            )
            if result.failure_reason:
                logger.warning('Arb leg failed: %s - %s', ticker.symbol, result.failure_reason)
            else:
                logger.info('Arb leg placed: %s %s @ %s', side.value, ticker.symbol, price)
        except Exception:
            logger.exception('Error placing arb leg: %s', ticker.symbol)

    @staticmethod
    def _detect_platform(ticker: Ticker) -> str:
        """Detect platform from ticker type."""
        from oracle3.ticker.ticker import KalshiTicker, PolyMarketTicker, SolanaTicker

        if isinstance(ticker, SolanaTicker):
            return 'dflow'
        if isinstance(ticker, PolyMarketTicker):
            return 'polymarket'
        if isinstance(ticker, KalshiTicker):
            return 'kalshi'
        return 'unknown'

    @property
    def opportunities(self) -> list[ArbitrageOpportunity]:
        return list(self._opportunities)
