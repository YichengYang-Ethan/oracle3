"""Cross-platform arbitrage strategy between Polymarket and Kalshi.

Detects price discrepancies on equivalent markets across the two platforms
and places offsetting trades to lock in risk-free profit.

Architecture
------------
- **MarketMatcher**: fuzzy-matches market titles between platforms.
- **CompositeTrader**: routes orders to the correct platform-specific trader.
- **CrossPlatformArbStrategy**: monitors price events and fires arb trades.

Usage
-----
See ``examples/cross_platform_arb_example.py`` for a full paper-trading demo.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coinjure.matching import MarketMatchingPipeline

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.strategy import Strategy
from oracle3.ticker.ticker import KalshiTicker, PolyMarketTicker, Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MarketMatcher – fuzzy-match markets across platforms
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    {'will', 'the', 'a', 'an', 'of', 'in', 'on', 'by', 'to', 'for', 'be', 'is', 'at'}
)


def _normalize(text: str) -> str:
    """Lower, strip punctuation, remove stopwords."""
    text = re.sub(r'[^a-z0-9\s]', ' ', text.lower())
    tokens = [t for t in text.split() if t not in _STOPWORDS]
    return ' '.join(tokens)


_CONFIDENCE_MULTIPLIER: dict[str, float] = {
    'HIGH': 1.0,
    'MEDIUM': 0.5,
    'LOW': 0.25,
}

_CONFIDENCE_ORDER = ['LOW', 'MEDIUM', 'HIGH']


@dataclass
class MatchedMarket:
    """A pair of tickers that refer to the same real-world event."""

    poly_ticker: PolyMarketTicker
    kalshi_ticker: KalshiTicker
    similarity: float  # 0-1 SequenceMatcher ratio
    label: str = ''  # human-readable name
    confidence: str = ''  # 'HIGH', 'MEDIUM', 'LOW' from pipeline


class MarketMatcher:
    """Match Polymarket tickers to Kalshi tickers by name similarity.

    Uses ``difflib.SequenceMatcher`` on normalised market names.
    """

    def __init__(self, min_similarity: float = 0.60) -> None:
        self.min_similarity = min_similarity
        self._matches: dict[str, MatchedMarket] = {}  # key: poly_symbol

    def match(
        self,
        poly_tickers: list[PolyMarketTicker],
        kalshi_tickers: list[KalshiTicker],
    ) -> list[MatchedMarket]:
        """Find the best match for each Polymarket ticker among Kalshi tickers."""
        results: list[MatchedMarket] = []
        kalshi_normed = [(kt, _normalize(kt.name)) for kt in kalshi_tickers if kt.name]

        for pt in poly_tickers:
            if not pt.name:
                continue
            pn = _normalize(pt.name)
            best_score = 0.0
            best_kt: KalshiTicker | None = None
            for kt, kn in kalshi_normed:
                score = SequenceMatcher(None, pn, kn).ratio()
                if score > best_score:
                    best_score = score
                    best_kt = kt

            if best_kt is not None and best_score >= self.min_similarity:
                m = MatchedMarket(
                    poly_ticker=pt,
                    kalshi_ticker=best_kt,
                    similarity=round(best_score, 3),
                    label=pt.name[:60],
                )
                results.append(m)
                self._matches[pt.symbol] = m
                logger.info(
                    'Matched: "%s" <-> "%s" (sim=%.3f)',
                    pt.name[:40],
                    best_kt.name[:40],
                    best_score,
                )

        return results

    def get_match(self, poly_symbol: str) -> MatchedMarket | None:
        return self._matches.get(poly_symbol)


# ---------------------------------------------------------------------------
# CompositeTrader – route orders to the correct platform
# ---------------------------------------------------------------------------


class CompositeTrader(Trader):
    """Wraps two platform-specific traders and routes orders by ticker type.

    ``TradingEngine`` only accepts a single ``Trader``.  ``CompositeTrader``
    satisfies that interface and internally delegates to the correct trader.
    """

    def __init__(
        self,
        poly_trader: Trader,
        kalshi_trader: Trader,
    ) -> None:
        # Use poly_trader's managers as the primary view
        self.poly_trader = poly_trader
        self.kalshi_trader = kalshi_trader
        self.market_data = poly_trader.market_data
        self.risk_manager = poly_trader.risk_manager
        self.position_manager = poly_trader.position_manager
        self.orders = poly_trader.orders

    async def place_order(
        self,
        side: TradeSide,
        ticker: Ticker,
        limit_price: Decimal,
        quantity: Decimal,
    ) -> Any:
        """Route order to the correct platform trader."""
        if isinstance(ticker, KalshiTicker):
            logger.info(
                'Routing %s order to Kalshi: %s qty=%s @ %s',
                side.value,
                ticker.symbol[:30],
                quantity,
                limit_price,
            )
            return await self.kalshi_trader.place_order(
                side,
                ticker,
                limit_price,
                quantity,
            )
        else:
            logger.info(
                'Routing %s order to Polymarket: %s qty=%s @ %s',
                side.value,
                ticker.symbol[:30],
                quantity,
                limit_price,
            )
            return await self.poly_trader.place_order(
                side,
                ticker,
                limit_price,
                quantity,
            )

    async def get_balance(self) -> Decimal:
        """Sum balances from both platforms."""
        poly_bal = await self.poly_trader.get_balance()
        kalshi_bal = await self.kalshi_trader.get_balance()
        return poly_bal + kalshi_bal

    async def get_positions(self) -> dict[str, Any]:
        poly_pos = await self.poly_trader.get_positions()
        kalshi_pos = await self.kalshi_trader.get_positions()
        return {**poly_pos, **kalshi_pos}


# ---------------------------------------------------------------------------
# CrossPlatformArbStrategy
# ---------------------------------------------------------------------------


class CrossPlatformArbStrategy(Strategy):
    """Detect and trade cross-platform arbitrage opportunities.

    On each PriceChangeEvent or OrderBookEvent, compare the YES price
    on Polymarket vs Kalshi for matched markets.  If one platform prices
    YES significantly cheaper than the other, we can lock in a risk-free
    profit by buying YES on the cheap side and buying NO on the expensive
    side.

    Example (Poly cheaper):
        Poly YES = 0.42,  Kalshi YES = 0.47
        Buy Poly YES  @ 0.42  (costs 0.42)
        Buy Kalshi NO @ 0.53  (costs 1 - 0.47 = 0.53)
        Total cost = 0.42 + 0.53 = 0.95
        Guaranteed payout = 1.00 (one of them wins)
        Profit = 0.05 per share

    Example (Kalshi cheaper):
        Poly YES = 0.55,  Kalshi YES = 0.48
        Buy Kalshi YES @ 0.48
        Buy Poly NO    @ 0.45  (costs 1 - 0.55 = 0.45)
        Total cost = 0.48 + 0.45 = 0.93
        Profit = 0.07 per share

    Note: this is a simplified example. Real arb requires accounting for
    fees, order book depth, settlement differences, and collateral.
    """

    name = 'cross_platform_arb'
    version = '0.2.0'
    author = 'oracle3'

    def __init__(
        self,
        matcher: MarketMatcher | None = None,
        pipeline: MarketMatchingPipeline | None = None,
        min_edge: float = 0.02,
        trade_size: Decimal = Decimal('10'),
        cooldown_seconds: int = 30,
        min_confidence: str = 'MEDIUM',
    ) -> None:
        super().__init__()
        self.matcher = matcher or MarketMatcher()
        self.pipeline = pipeline  # coinjure.matching.MarketMatchingPipeline
        self.min_edge = min_edge
        self.trade_size = trade_size
        self.cooldown_seconds = cooldown_seconds
        self.min_confidence = min_confidence

        # Pipeline refresh tracking
        self._pipeline_refresh_interval = 300.0
        self._last_pipeline_refresh = 0.0

        # symbol -> last YES price
        self._prices: dict[str, Decimal] = {}
        # symbol -> last arb attempt time
        self._last_arb_time: dict[str, float] = {}

    async def refresh_matches_from_pipeline(self) -> int:
        """Scan the pipeline and populate matcher._matches with results.

        Returns the number of matches populated.
        """
        if self.pipeline is None:
            return 0

        from coinjure.matching import match_result_to_matched_market

        results = await self.pipeline.scan()
        count = 0
        for result in results:
            matched = match_result_to_matched_market(result)
            if isinstance(matched, MatchedMarket):
                # Filter by min_confidence
                conf = matched.confidence
                if conf and conf in _CONFIDENCE_ORDER:
                    if _CONFIDENCE_ORDER.index(conf) < _CONFIDENCE_ORDER.index(self.min_confidence):
                        continue
                self.matcher._matches[matched.poly_ticker.symbol] = matched
                count += 1

        logger.info('Pipeline refresh: populated %d matches from %d results', count, len(results))
        return count

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        # Periodic pipeline refresh
        if self.pipeline is not None:
            import time as _time

            now = _time.time()
            if now - self._last_pipeline_refresh >= self._pipeline_refresh_interval:
                self._last_pipeline_refresh = now
                await self.refresh_matches_from_pipeline()

        ticker: Ticker | None = None
        price: Decimal | None = None

        if isinstance(event, PriceChangeEvent):
            ticker = event.ticker
            price = event.price
        elif isinstance(event, OrderBookEvent):
            ticker = event.ticker
            # Use mid price from order book
            if hasattr(event, 'bid_price') and hasattr(event, 'ask_price'):
                bid = getattr(event, 'bid_price', Decimal('0'))
                ask = getattr(event, 'ask_price', Decimal('0'))
                if bid > 0 and ask > 0:
                    price = (bid + ask) / Decimal('2')
                elif ask > 0:
                    price = ask
        else:
            return

        if ticker is None or price is None:
            return

        self._prices[ticker.symbol] = price

        # Only trigger arb check on Polymarket events (primary side)
        if not isinstance(ticker, PolyMarketTicker):
            return

        match = self.matcher.get_match(ticker.symbol)
        if match is None:
            return

        # Confidence-scaled trade sizing
        conf = match.confidence
        confidence_mult = _CONFIDENCE_MULTIPLIER.get(conf, 1.0) if conf else 1.0
        effective_size = Decimal(str(float(self.trade_size) * confidence_mult))

        # Get Kalshi YES price for the same event
        kalshi_price = self._prices.get(match.kalshi_ticker.symbol)
        if kalshi_price is None:
            return

        poly_yes = float(price)
        kalshi_yes = float(kalshi_price)

        # Cross-platform arb: same event priced differently.
        # edge = |poly_yes - kalshi_yes|
        # If poly_yes < kalshi_yes: buy Poly YES + buy Kalshi NO
        #   cost = poly_yes + (1 - kalshi_yes), profit = kalshi_yes - poly_yes
        # If kalshi_yes < poly_yes: buy Kalshi YES + buy Poly NO
        #   cost = kalshi_yes + (1 - poly_yes), profit = poly_yes - kalshi_yes
        spread = poly_yes - kalshi_yes
        edge = abs(spread)

        if edge >= self.min_edge:
            # Cooldown check
            import time

            now = time.time()
            key = ticker.symbol
            if now - self._last_arb_time.get(key, 0) < self.cooldown_seconds:
                return
            self._last_arb_time[key] = now

            if spread < 0:
                # Poly is cheaper: buy Poly YES + buy Kalshi NO
                logger.info(
                    'ARB (Poly cheap): %s | Poly YES=%.4f < Kalshi YES=%.4f | edge=%.4f',
                    match.label[:40],
                    poly_yes,
                    kalshi_yes,
                    edge,
                )

                # Buy YES on Polymarket (cheap side)
                await self._place_arb_leg(
                    trader,
                    match.poly_ticker,
                    TradeSide.BUY,
                    price,
                    f'Arb: buy Poly YES @ {poly_yes:.4f}',
                    size=effective_size,
                )

                # Buy NO on Kalshi (expensive side → sell YES equivalent)
                kalshi_no = match.kalshi_ticker.get_no_ticker()
                if kalshi_no is not None:
                    no_price = Decimal('1') - kalshi_price
                    await self._place_arb_leg(
                        trader,
                        kalshi_no,
                        TradeSide.BUY,
                        no_price,
                        f'Arb: buy Kalshi NO @ {float(no_price):.4f}',
                        size=effective_size,
                    )

                self.record_decision(
                    ticker_name=match.label[:40],
                    action='ARB_BUY_POLY',
                    executed=True,
                    reasoning=f'Poly cheap: edge={edge:.4f}, poly={poly_yes:.4f}, kalshi={kalshi_yes:.4f}',
                    signal_values={
                        'poly_yes': poly_yes,
                        'kalshi_yes': kalshi_yes,
                        'edge': edge,
                        'direction': -1.0,  # poly < kalshi
                    },
                )

            else:
                # Kalshi is cheaper: buy Kalshi YES + buy Poly NO
                logger.info(
                    'ARB (Kalshi cheap): %s | Kalshi YES=%.4f < Poly YES=%.4f | edge=%.4f',
                    match.label[:40],
                    kalshi_yes,
                    poly_yes,
                    edge,
                )

                # Buy YES on Kalshi (cheap side)
                await self._place_arb_leg(
                    trader,
                    match.kalshi_ticker,
                    TradeSide.BUY,
                    kalshi_price,
                    f'Arb: buy Kalshi YES @ {kalshi_yes:.4f}',
                    size=effective_size,
                )

                # Buy NO on Polymarket (expensive side → sell YES equivalent)
                poly_no = match.poly_ticker.get_no_ticker()
                if poly_no is not None:
                    no_price = Decimal('1') - price
                    await self._place_arb_leg(
                        trader,
                        poly_no,
                        TradeSide.BUY,
                        no_price,
                        f'Arb: buy Poly NO @ {float(no_price):.4f}',
                        size=effective_size,
                    )

                self.record_decision(
                    ticker_name=match.label[:40],
                    action='ARB_BUY_KALSHI',
                    executed=True,
                    reasoning=f'Kalshi cheap: edge={edge:.4f}, poly={poly_yes:.4f}, kalshi={kalshi_yes:.4f}',
                    signal_values={
                        'poly_yes': poly_yes,
                        'kalshi_yes': kalshi_yes,
                        'edge': edge,
                        'direction': 1.0,  # kalshi < poly
                    },
                )

        else:
            # No arb — spread too small
            self.record_decision(
                ticker_name=match.label[:40],
                action='HOLD',
                executed=False,
                reasoning=f'no arb: spread={spread:.4f}, edge={edge:.4f} < min_edge={self.min_edge}',
                signal_values={
                    'poly_yes': poly_yes,
                    'kalshi_yes': kalshi_yes,
                    'edge': edge,
                },
            )

    async def _place_arb_leg(
        self,
        trader: Trader,
        ticker: Ticker,
        side: TradeSide,
        price: Decimal,
        reason: str,
        size: Decimal | None = None,
    ) -> None:
        """Place one leg of the arbitrage trade."""
        qty = size if size is not None else self.trade_size
        try:
            result = await trader.place_order(
                side=side,
                ticker=ticker,
                limit_price=price,
                quantity=qty,
            )
            if result.failure_reason:
                logger.warning('Arb leg failed: %s - %s', reason, result.failure_reason)
            else:
                logger.info('Arb leg placed: %s', reason)
        except Exception:
            logger.exception('Error placing arb leg: %s', reason)
