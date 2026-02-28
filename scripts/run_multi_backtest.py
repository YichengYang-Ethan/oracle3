#!/usr/bin/env python3
"""
Multi-Round Backtest with Real Polymarket Data

Fetches real market data from Polymarket APIs, runs multiple strategy
configurations in parallel, and produces a comparative report.

Strategies tested:
  1. Momentum (baseline) — pure price momentum signals
  2. News Sentiment — keyword-based mock LLM news analysis
  3. Debate (conservative) — bull/bear/judge with 1/4 Kelly, tight thresholds
  4. Debate (aggressive) — bull/bear/judge with 1/2 Kelly, looser thresholds
  5. Combined — news + order flow signals + debate sizing

Usage:
    python scripts/run_multi_backtest.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracle3.analytics.performance_analyzer import PerformanceAnalyzer
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import Event, NewsEvent, PriceChangeEvent
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import StandardRiskManager
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.types import TradeSide

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INITIAL_CAPITAL = Decimal("10000")
MIN_MID_PRICE = 0.15
MAX_MID_PRICE = 0.85
MAX_MARKETS = 10
COMMISSION_RATE = Decimal("0.002")
MIN_FILL_RATE = Decimal("0.85")
MAX_FILL_RATE = Decimal("1.0")
NEWS_INTERVAL = 8  # inject synthetic news every N price events

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MarketInfo:
    question: str
    condition_id: str
    token_id: str
    outcome: str
    best_bid: float
    best_ask: float
    volume: float
    liquidity: float
    ticker: PolyMarketTicker | None = field(default=None, repr=False)
    price_history: list[dict] = field(default_factory=list, repr=False)


@dataclass
class RoundResult:
    """Results from a single backtest round."""

    name: str
    total_trades: int
    buy_trades: int
    sell_trades: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    return_pct: Decimal
    max_drawdown: Decimal
    final_value: Decimal
    total_commission: Decimal
    markets_won: int
    markets_lost: int
    sharpe_ratio: Decimal
    profit_factor: Decimal
    win_rate: Decimal
    avg_trade_pnl: Decimal
    timeline: list[tuple[datetime, Decimal]]  # (timestamp, portfolio_value)


# ---------------------------------------------------------------------------
# Sentiment-based mock LLM
# ---------------------------------------------------------------------------

POSITIVE_KW = {
    "win", "wins", "ahead", "leads", "surge", "rally", "boost", "approve",
    "approved", "success", "breakthrough", "confirm", "confirmed", "strong",
    "increase", "gain", "rise", "rising", "support", "pass", "passed",
    "victory", "agree", "deal", "progress", "advance",
}

NEGATIVE_KW = {
    "lose", "loses", "behind", "drop", "fall", "decline", "reject",
    "rejected", "fail", "failure", "crash", "crisis", "weak", "decrease",
    "loss", "down", "falling", "oppose", "block", "blocked", "delay",
    "defeat", "collapse", "risk", "threat", "warning",
}


def mock_sentiment(text: str) -> tuple[str, float]:
    """Return (action, confidence) from keyword sentiment."""
    words = set(text.lower().split())
    pos = len(words & POSITIVE_KW)
    neg = len(words & NEGATIVE_KW)
    total = pos + neg
    if total == 0:
        return "hold", 0.1
    net = (pos - neg) / total
    noise = random.uniform(-0.12, 0.12)
    adj = max(-1.0, min(1.0, net + noise))
    conf = min(0.95, abs(adj) * 0.7 + random.uniform(0.05, 0.2))
    if adj > 0.15:
        return "buy", conf
    elif adj < -0.15:
        return "sell", conf
    return "hold", conf


# ---------------------------------------------------------------------------
# Synthetic news templates
# ---------------------------------------------------------------------------

POS_TEMPLATES = [
    "New poll shows strong support for the '{q}' outcome",
    "Analysts predict positive momentum: {q}",
    "Breaking: Key development boosts likelihood — {q}",
    "Sources confirm progress toward resolution: {q}",
    "Market surge as experts signal agreement on {q}",
]
NEG_TEMPLATES = [
    "Setback reported: concerns grow over {q}",
    "Opposition mounts against expected outcome: {q}",
    "Analysts warn of declining prospects — {q}",
    "Crisis threatens progress on {q}",
    "New data suggests risk of failure: {q}",
]
NEU_TEMPLATES = [
    "Ongoing debate continues: {q}",
    "Mixed signals from experts regarding {q}",
    "Uncertainty persists around {q}",
]


def make_news(market: MarketInfo, ts: datetime) -> NewsEvent:
    sentiment = random.choices(
        ["pos", "neg", "neu"], weights=[0.40, 0.35, 0.25]
    )[0]
    templates = {"pos": POS_TEMPLATES, "neg": NEG_TEMPLATES, "neu": NEU_TEMPLATES}
    headline = random.choice(templates[sentiment]).format(q=market.question[:80])
    body = (
        f"{headline}. Volume ${market.volume:,.0f}. "
        f"Mid-price implied probability: "
        f"{(market.best_bid + market.best_ask) / 2:.1%}."
    )
    uid = hashlib.md5(f"{market.token_id}-{ts.isoformat()}".encode()).hexdigest()[:12]
    return NewsEvent(
        news=body,
        title=headline,
        source="Synthetic",
        published_at=ts,
        ticker=market.ticker,
        uuid=uid,
        event_id=market.condition_id,
    )


# ---------------------------------------------------------------------------
# Data fetching  (real Polymarket data)
# ---------------------------------------------------------------------------


async def fetch_markets(client: httpx.AsyncClient) -> list[dict]:
    logger.info("Fetching markets from Polymarket Gamma API...")
    resp = await client.get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": "true", "closed": "false", "limit": "100"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json() if isinstance(resp.json(), list) else []


def select_markets(raw: list[dict]) -> list[MarketInfo]:
    candidates: list[MarketInfo] = []
    for mkt in raw:
        try:
            bb = float(mkt.get("bestBid", 0))
            ba = float(mkt.get("bestAsk", 0))
            vol = float(mkt.get("volume", 0))
            liq = float(mkt.get("liquidityNum", 0))
        except (ValueError, TypeError):
            continue
        if bb <= 0 or ba <= 0:
            continue
        mid = (bb + ba) / 2
        if mid < MIN_MID_PRICE or mid > MAX_MID_PRICE:
            continue
        clob_ids = mkt.get("clobTokenIds", "")
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except (ValueError, TypeError):
                continue
        if not clob_ids or not isinstance(clob_ids, list):
            continue
        cid = mkt.get("conditionId", "")
        if not cid:
            continue
        candidates.append(
            MarketInfo(
                question=mkt.get("question", ""),
                condition_id=cid,
                token_id=clob_ids[0],
                outcome="Yes",
                best_bid=bb,
                best_ask=ba,
                volume=vol,
                liquidity=liq,
            )
        )
    candidates.sort(key=lambda m: m.volume, reverse=True)
    sel = candidates[:MAX_MARKETS]
    logger.info(f"Selected {len(sel)} markets (mid {MIN_MID_PRICE}-{MAX_MID_PRICE})")
    return sel


async def fetch_price_history(
    client: httpx.AsyncClient, token_id: str
) -> list[dict]:
    url = "https://clob.polymarket.com/prices-history"
    params = {"market": token_id, "interval": "max", "fidelity": "60"}
    try:
        resp = await client.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json().get("history", [])
    except Exception as e:
        logger.warning(f"Price history failed for {token_id[:16]}...: {e}")
        return []


async def fetch_all_histories(
    client: httpx.AsyncClient, markets: list[MarketInfo]
) -> None:
    logger.info(f"Fetching price histories for {len(markets)} markets...")
    tasks = [fetch_price_history(client, m.token_id) for m in markets]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for market, result in zip(markets, results):
        if isinstance(result, Exception):
            market.price_history = []
        else:
            market.price_history = result
            logger.info(
                f"  {market.question[:50]}... -> {len(result)} pts"
            )


def generate_fallback_markets() -> list[MarketInfo]:
    """Synthetic markets when API is unreachable."""
    questions = [
        "Will Bitcoin exceed $100,000 by end of Q1 2026?",
        "Will the Federal Reserve cut interest rates in March 2026?",
        "Will SpaceX successfully launch Starship to orbit?",
        "Will the US GDP growth exceed 3% in 2026?",
        "Will AI regulation legislation pass in the US Senate?",
        "Will global temperatures set a new record in 2026?",
        "Will Ethereum flip Bitcoin market cap in 2026?",
        "Will there be a US government shutdown in Q1 2026?",
    ]
    base_time = int(time.time()) - 30 * 24 * 3600  # 30 days ago
    markets: list[MarketInfo] = []
    for i, q in enumerate(questions):
        bid = round(random.uniform(0.20, 0.70), 2)
        ask = round(bid + random.uniform(0.02, 0.06), 2)
        ask = min(ask, 0.85)
        tid = hashlib.md5(q.encode()).hexdigest()
        price = (bid + ask) / 2
        history = []
        for h in range(30 * 24):  # hourly for 30 days
            ts_int = base_time + h * 3600
            price += random.gauss(0, 0.012)
            price = max(0.05, min(0.95, price))
            history.append({"t": ts_int, "p": f"{price:.4f}"})
        markets.append(
            MarketInfo(
                question=q,
                condition_id=f"syn-{i}",
                token_id=tid,
                outcome="Yes",
                best_bid=bid,
                best_ask=ask,
                volume=random.uniform(50000, 500000),
                liquidity=random.uniform(10000, 100000),
                price_history=history,
            )
        )
    logger.info(f"Generated {len(markets)} fallback markets")
    return markets


# ---------------------------------------------------------------------------
# Create tickers and timeline
# ---------------------------------------------------------------------------


def create_ticker(market: MarketInfo) -> PolyMarketTicker:
    short_id = hashlib.md5(market.token_id.encode()).hexdigest()[:8].upper()
    return PolyMarketTicker(
        symbol=short_id,
        name=market.question[:60],
        token_id=market.token_id,
        market_id=market.condition_id,
    )


def build_timeline(
    markets: list[MarketInfo],
) -> list[Event]:
    raw: list[tuple[float, Event]] = []
    for market in markets:
        ticker = market.ticker
        for pt in market.price_history:
            try:
                ts = datetime.fromtimestamp(int(pt["t"]), tz=timezone.utc)
                price = Decimal(str(pt["p"]))
                price = max(Decimal("0.01"), min(Decimal("0.99"), price))
            except (KeyError, ValueError, TypeError):
                continue
            raw.append((float(pt["t"]), PriceChangeEvent(ticker=ticker, price=price, timestamp=ts)))
    raw.sort(key=lambda x: x[0])

    timeline: list[Event] = []
    price_count = 0
    for _ts, event in raw:
        timeline.append(event)
        price_count += 1
        if price_count % NEWS_INTERVAL == 0:
            m = random.choice(markets)
            ts_dt = event.timestamp if hasattr(event, "timestamp") else datetime.now(timezone.utc)
            timeline.append(make_news(m, ts_dt))

    news_count = len(timeline) - price_count
    logger.info(f"Timeline: {len(timeline)} events ({price_count} price, {news_count} news)")
    return timeline


# ---------------------------------------------------------------------------
# Strategy implementations (self-contained for backtesting)
# ---------------------------------------------------------------------------


class BacktestContext:
    """Shared framework components for one backtest round."""

    def __init__(self, markets: list[MarketInfo]) -> None:
        self.market_data = MarketDataManager()
        self.position_manager = PositionManager()
        self.risk_manager = StandardRiskManager(
            position_manager=self.position_manager,
            market_data=self.market_data,
            max_single_trade_size=Decimal("500"),
            max_position_size=Decimal("2000"),
            max_total_exposure=Decimal("8000"),
            max_drawdown_pct=Decimal("0.25"),
            max_positions=len(markets),
            initial_capital=INITIAL_CAPITAL,
        )
        self.trader = PaperTrader(
            market_data=self.market_data,
            risk_manager=self.risk_manager,
            position_manager=self.position_manager,
            min_fill_rate=MIN_FILL_RATE,
            max_fill_rate=MAX_FILL_RATE,
            commission_rate=COMMISSION_RATE,
        )
        self.analyzer = PerformanceAnalyzer(initial_capital=INITIAL_CAPITAL)

        # Initial cash
        self.position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=INITIAL_CAPITAL,
                average_cost=Decimal("1"),
                realized_pnl=Decimal("0"),
            )
        )

        # Seed order books
        for market in markets:
            ticker = market.ticker
            bid = Decimal(str(market.best_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ask = Decimal(str(market.best_ask)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ob = OrderBook()
            bids = [Level(price=bid, size=Decimal("1000"))] if bid > 0 else []
            asks = [Level(price=ask, size=Decimal("1000"))] if ask < 1 else []
            ob.update(asks=asks, bids=bids)
            self.market_data.update_order_book(ticker, ob)


# ---- Strategy 1: Momentum (baseline) ----

class MomentumStrategy:
    """Simple price momentum — buy on dips, sell on spikes."""

    def __init__(
        self,
        window: int = 20,
        buy_threshold: float = -0.03,
        sell_threshold: float = 0.03,
        trade_size: Decimal = Decimal("50"),
    ):
        self.window = window
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.trade_size = trade_size
        self._prices: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    async def on_event(
        self, event: Event, ctx: BacktestContext
    ) -> None:
        if isinstance(event, PriceChangeEvent):
            sym = event.ticker.symbol
            self._prices[sym].append(float(event.price))
            prices = self._prices[sym]
            if len(prices) < self.window:
                return
            pct = (prices[-1] - prices[0]) / prices[0] if prices[0] != 0 else 0

            if pct <= self.buy_threshold:
                level = ctx.trader.market_data.get_best_ask(event.ticker)
                if level:
                    result = await ctx.trader.place_order(
                        TradeSide.BUY, event.ticker, level.price, self.trade_size
                    )
                    if result.order:
                        for t in result.order.trades:
                            ctx.analyzer.add_trade(t)

            elif pct >= self.sell_threshold:
                pos = ctx.position_manager.get_position(event.ticker)
                if pos and pos.quantity > 0:
                    level = ctx.trader.market_data.get_best_bid(event.ticker)
                    if level:
                        qty = min(self.trade_size, pos.quantity)
                        result = await ctx.trader.place_order(
                            TradeSide.SELL, event.ticker, level.price, qty
                        )
                        if result.order:
                            for t in result.order.trades:
                                ctx.analyzer.add_trade(t)


# ---- Strategy 2: News Sentiment ----

class NewsSentimentStrategy:
    """Mock LLM news analysis using keyword sentiment."""

    def __init__(
        self,
        confidence_threshold: float = 0.35,
        trade_size: Decimal = Decimal("50"),
        cooldown: int = 5,
    ):
        self.conf_threshold = confidence_threshold
        self.trade_size = trade_size
        self.cooldown = cooldown
        self._last_trade: dict[str, int] = defaultdict(int)
        self._event_count = 0

    async def on_event(self, event: Event, ctx: BacktestContext) -> None:
        self._event_count += 1
        if not isinstance(event, NewsEvent) or event.ticker is None:
            return
        sym = event.ticker.symbol
        if self._event_count - self._last_trade[sym] < self.cooldown:
            return

        action, conf = mock_sentiment(event.news)
        if conf < self.conf_threshold or action == "hold":
            return

        self._last_trade[sym] = self._event_count
        if action == "buy":
            level = ctx.trader.market_data.get_best_ask(event.ticker)
            if level:
                result = await ctx.trader.place_order(
                    TradeSide.BUY, event.ticker, level.price, self.trade_size
                )
                if result.order:
                    for t in result.order.trades:
                        ctx.analyzer.add_trade(t)
        elif action == "sell":
            pos = ctx.position_manager.get_position(event.ticker)
            if pos and pos.quantity > 0:
                level = ctx.trader.market_data.get_best_bid(event.ticker)
                if level:
                    qty = min(self.trade_size, pos.quantity)
                    result = await ctx.trader.place_order(
                        TradeSide.SELL, event.ticker, level.price, qty
                    )
                    if result.order:
                        for t in result.order.trades:
                            ctx.analyzer.add_trade(t)


# ---- Strategy 3 & 4: Debate (mock) with Kelly ----

class DebateKellyStrategy:
    """Mock debate strategy with Kelly sizing."""

    def __init__(
        self,
        name: str = "Debate",
        kelly_fraction: float = 0.25,
        max_position_pct: float = 0.15,
        confidence_threshold: float = 0.50,
        edge_threshold: float = 0.05,
        news_weight: float = 0.6,
        momentum_weight: float = 0.4,
        price_window: int = 20,
    ):
        self.name = name
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.conf_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.news_weight = news_weight
        self.momentum_weight = momentum_weight
        self._prices: dict[str, deque] = defaultdict(lambda: deque(maxlen=price_window))
        self._news_buf: dict[str, list[str]] = defaultdict(list)
        self._cooldown: dict[str, int] = defaultdict(int)
        self._event_count = 0

    def _kelly_size(
        self, fair_prob: float, market_price: float, portfolio: Decimal
    ) -> Decimal:
        if market_price <= 0 or market_price >= 1:
            return Decimal("0")
        if fair_prob > market_price:
            p, b = fair_prob, (1.0 / market_price) - 1.0
        else:
            p, b = 1.0 - fair_prob, (1.0 / (1.0 - market_price)) - 1.0
        if b <= 0:
            return Decimal("0")
        q = 1.0 - p
        f_star = (p * b - q) / b
        if f_star <= 0:
            return Decimal("0")
        fraction = min(f_star * self.kelly_fraction, self.max_position_pct)
        dollar = Decimal(str(fraction)) * portfolio
        share_price = Decimal(str(market_price))
        if share_price <= 0:
            return Decimal("0")
        return max((dollar / share_price).quantize(Decimal("1")), Decimal("0"))

    async def on_event(self, event: Event, ctx: BacktestContext) -> None:
        self._event_count += 1

        if isinstance(event, PriceChangeEvent):
            self._prices[event.ticker.symbol].append(float(event.price))

        if isinstance(event, NewsEvent) and event.ticker:
            sym = event.ticker.symbol
            self._news_buf[sym].append(event.news)
            # Keep last 10 news items
            self._news_buf[sym] = self._news_buf[sym][-10:]

            if self._event_count - self._cooldown.get(sym, 0) < 8:
                return

            # --- Mock debate: bull/bear scores from news + momentum ---
            news_text = " ".join(self._news_buf[sym])
            _, news_conf = mock_sentiment(news_text)

            prices = self._prices.get(sym, deque())
            momentum_signal = 0.0
            if len(prices) >= 5:
                pct = (prices[-1] - prices[0]) / prices[0] if prices[0] != 0 else 0
                momentum_signal = pct  # positive = price rising

            # Bull argues: combine positive news + positive momentum
            bull_score = (
                self.news_weight * news_conf
                + self.momentum_weight * max(0, momentum_signal * 10)
            )
            # Bear argues: combine negative news + negative momentum
            bear_score = (
                self.news_weight * (1 - news_conf)
                + self.momentum_weight * max(0, -momentum_signal * 10)
            )

            # Judge: weighted average → fair probability estimate
            total = bull_score + bear_score
            if total <= 0:
                return
            bull_pct = bull_score / total

            current_price = prices[-1] if prices else 0.5
            # Fair prob shifts from market price based on debate outcome
            fair_prob = current_price + (bull_pct - 0.5) * 0.2
            fair_prob = max(0.01, min(0.99, fair_prob))

            edge = fair_prob - current_price
            confidence = max(bull_score, bear_score) / total
            confidence = min(0.95, confidence + random.uniform(-0.05, 0.05))

            if confidence < self.conf_threshold or abs(edge) < self.edge_threshold:
                return

            self._cooldown[sym] = self._event_count

            # Kelly position sizing
            cash_positions = ctx.position_manager.get_cash_positions()
            portfolio_val = sum((p.quantity for p in cash_positions), Decimal("0"))
            qty = self._kelly_size(fair_prob, current_price, portfolio_val)
            if qty <= 0:
                return

            ticker = event.ticker
            if edge > 0:
                level = ctx.trader.market_data.get_best_ask(ticker)
                if level:
                    # Adjust for existing position
                    pos = ctx.position_manager.get_position(ticker)
                    existing = pos.quantity if pos else Decimal("0")
                    buy_qty = max(qty - existing, Decimal("0"))
                    if buy_qty > 0:
                        result = await ctx.trader.place_order(
                            TradeSide.BUY, ticker, level.price, buy_qty
                        )
                        if result.order:
                            for t in result.order.trades:
                                ctx.analyzer.add_trade(t)
            else:
                pos = ctx.position_manager.get_position(ticker)
                if pos and pos.quantity > 0:
                    level = ctx.trader.market_data.get_best_bid(ticker)
                    if level:
                        sell_qty = min(qty, pos.quantity)
                        if sell_qty > 0:
                            result = await ctx.trader.place_order(
                                TradeSide.SELL, ticker, level.price, sell_qty
                            )
                            if result.order:
                                for t in result.order.trades:
                                    ctx.analyzer.add_trade(t)


# ---- Strategy 5: Combined (all signals) ----

class CombinedStrategy:
    """Uses news + momentum + order flow heuristics with dynamic sizing."""

    def __init__(self):
        self.momentum = MomentumStrategy(
            window=30, buy_threshold=-0.02, sell_threshold=0.02, trade_size=Decimal("30")
        )
        self.news = NewsSentimentStrategy(
            confidence_threshold=0.40, trade_size=Decimal("40"), cooldown=8
        )
        self.debate = DebateKellyStrategy(
            name="CombinedDebate",
            kelly_fraction=0.20,
            max_position_pct=0.10,
            confidence_threshold=0.45,
            edge_threshold=0.04,
        )

    async def on_event(self, event: Event, ctx: BacktestContext) -> None:
        # Feed all sub-strategies; they share the same ctx
        await self.momentum.on_event(event, ctx)
        await self.news.on_event(event, ctx)
        await self.debate.on_event(event, ctx)


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------


async def run_single_round(
    strategy_name: str,
    strategy: Any,
    markets: list[MarketInfo],
    timeline: list[Event],
) -> RoundResult:
    """Run one strategy over the full timeline."""
    ctx = BacktestContext(markets)

    snapshots: list[tuple[datetime, Decimal]] = []
    peak = INITIAL_CAPITAL
    max_dd = Decimal("0")
    snap_interval = max(1, len(timeline) // 100)

    for i, event in enumerate(timeline):
        # Update market data
        if isinstance(event, PriceChangeEvent):
            ctx.market_data.process_price_change_event(event)

        # Strategy processes event
        await strategy.on_event(event, ctx)

        # Periodic snapshots
        if i % snap_interval == 0 or i == len(timeline) - 1:
            pv = ctx.position_manager.get_portfolio_value(ctx.market_data)
            total_val = sum(pv.values(), Decimal("0"))
            ts = (
                event.timestamp
                if isinstance(event, PriceChangeEvent)
                else getattr(event, "published_at", datetime.now(timezone.utc))
            )
            snapshots.append((ts, total_val))
            if total_val > peak:
                peak = total_val
            if peak > 0:
                dd = (peak - total_val) / peak
                max_dd = max(max_dd, dd)

    # Collect results
    pv = ctx.position_manager.get_portfolio_value(ctx.market_data)
    final_value = sum(pv.values(), Decimal("0"))
    realized = ctx.position_manager.get_total_realized_pnl()
    unrealized = ctx.position_manager.get_total_unrealized_pnl(ctx.market_data)

    trades = []
    for order in ctx.trader.orders:
        trades.extend(order.trades)

    buy_count = sum(1 for t in trades if t.side == TradeSide.BUY)
    sell_count = sum(1 for t in trades if t.side == TradeSide.SELL)
    total_comm = sum(t.commission for t in trades)

    # Per-market win/loss
    won = lost = 0
    for m in markets:
        if m.ticker:
            pos = ctx.position_manager.get_position(m.ticker)
            if pos and pos.realized_pnl > 0:
                won += 1
            elif pos and pos.realized_pnl < 0:
                lost += 1

    stats = ctx.analyzer.get_stats()
    return_pct = (
        (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        if INITIAL_CAPITAL > 0
        else Decimal("0")
    )

    avg_pnl = (
        (realized + unrealized) / Decimal(str(max(len(trades), 1)))
    )

    return RoundResult(
        name=strategy_name,
        total_trades=len(trades),
        buy_trades=buy_count,
        sell_trades=sell_count,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=realized + unrealized,
        return_pct=return_pct,
        max_drawdown=max_dd,
        final_value=final_value,
        total_commission=total_comm,
        markets_won=won,
        markets_lost=lost,
        sharpe_ratio=stats.sharpe_ratio,
        profit_factor=stats.profit_factor,
        win_rate=stats.win_rate,
        avg_trade_pnl=avg_pnl,
        timeline=snapshots,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(
    markets: list[MarketInfo],
    results: list[RoundResult],
    timeframe: str,
) -> None:
    sep = "=" * 100

    print(f"\n{sep}")
    print("  MULTI-STRATEGY BACKTEST REPORT — REAL POLYMARKET DATA")
    print(sep)

    print("\n--- Parameters ---")
    print(f"  Initial Capital:  ${INITIAL_CAPITAL:,.2f}")
    print(f"  Commission:       {COMMISSION_RATE}")
    print(f"  Markets:          {len(markets)}")
    print(f"  Timeframe:        {timeframe}")

    print("\n--- Markets ---")
    for m in markets:
        mid = (m.best_bid + m.best_ask) / 2
        print(
            f"  [{m.ticker.symbol}] {m.question[:55]:<55} "
            f"mid={mid:.2f}  vol=${m.volume:>12,.0f}  pts={len(m.price_history)}"
        )

    # --- Comparison table ---
    print("\n--- Strategy Comparison ---")
    header = (
        f"  {'Strategy':<22} {'Return%':>9} {'Sharpe':>8} "
        f"{'MaxDD%':>8} {'WinRate':>8} {'PF':>6} "
        f"{'Trades':>7} {'P&L':>12} {'AvgTrade':>10}"
    )
    print(header)
    print("  " + "-" * 96)

    # Sort by return descending
    sorted_results = sorted(results, key=lambda r: r.return_pct, reverse=True)
    for r in sorted_results:
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor < Decimal("100") else "INF"
        print(
            f"  {r.name:<22} {r.return_pct:>+8.2f}% {r.sharpe_ratio:>8.3f} "
            f"{r.max_drawdown * 100:>7.2f}% {r.win_rate * 100:>7.1f}% {pf_str:>6} "
            f"{r.total_trades:>7} ${r.total_pnl:>+10.2f} ${r.avg_trade_pnl:>+8.4f}"
        )

    # --- Detailed per-strategy ---
    for r in sorted_results:
        print(f"\n--- {r.name} (Detail) ---")
        print(f"  Trades:       {r.total_trades} (buy={r.buy_trades}, sell={r.sell_trades})")
        print(f"  Realized:     ${r.realized_pnl:+.4f}")
        print(f"  Unrealized:   ${r.unrealized_pnl:+.4f}")
        print(f"  Commission:   ${r.total_commission:.4f}")
        print(f"  Final Value:  ${r.final_value:,.2f}")
        print(f"  Mkt Won/Lost: {r.markets_won}/{r.markets_lost}")

        # Mini timeline (10 points)
        if r.timeline:
            step = max(1, len(r.timeline) // 10)
            pts = r.timeline[::step]
            if pts[-1] != r.timeline[-1]:
                pts.append(r.timeline[-1])
            print(f"  Portfolio curve ({len(pts)} points):")
            for ts, val in pts:
                bar_len = max(0, int((float(val) / float(INITIAL_CAPITAL) - 0.9) * 200))
                bar = "█" * min(bar_len, 40)
                print(f"    {ts:%m-%d %H:%M}  ${val:>10,.2f}  {bar}")

    # --- Winner ---
    best = sorted_results[0]
    worst = sorted_results[-1]
    print(f"\n{sep}")
    print(f"  BEST:  {best.name} ({best.return_pct:+.2f}% return, "
          f"Sharpe={best.sharpe_ratio:.3f})")
    print(f"  WORST: {worst.name} ({worst.return_pct:+.2f}% return, "
          f"Sharpe={worst.sharpe_ratio:.3f})")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    logger.info("Multi-Strategy Backtest starting...")

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "oracle3-backtest/1.0"},
    ) as client:
        try:
            raw = await fetch_markets(client)
        except Exception as e:
            logger.error(f"Market fetch failed: {e}")
            raw = []

        markets = select_markets(raw) if raw else []

        if not markets:
            logger.info("Using synthetic fallback markets")
            markets = generate_fallback_markets()

        api_markets = [m for m in markets if not m.price_history]
        if api_markets:
            await fetch_all_histories(client, api_markets)

    # Drop markets with no history
    markets = [m for m in markets if m.price_history]
    if not markets:
        markets = generate_fallback_markets()

    # Create tickers
    for m in markets:
        m.ticker = create_ticker(m)

    # Build timeline
    timeline = build_timeline(markets)
    if not timeline:
        logger.error("Empty timeline, aborting.")
        return

    # Determine timeframe string
    first_ts = None
    last_ts = None
    for e in timeline:
        if isinstance(e, PriceChangeEvent):
            if first_ts is None:
                first_ts = e.timestamp
            last_ts = e.timestamp
    timeframe = "N/A"
    if first_ts and last_ts:
        timeframe = f"{first_ts:%Y-%m-%d %H:%M} -> {last_ts:%Y-%m-%d %H:%M} UTC"

    # --- Define strategy configurations ---
    strategies = [
        ("Momentum (baseline)", MomentumStrategy(
            window=20, buy_threshold=-0.03, sell_threshold=0.03,
            trade_size=Decimal("50"),
        )),
        ("News Sentiment", NewsSentimentStrategy(
            confidence_threshold=0.35, trade_size=Decimal("50"), cooldown=5,
        )),
        ("Debate Conservative", DebateKellyStrategy(
            name="Debate-Cons",
            kelly_fraction=0.25,
            max_position_pct=0.15,
            confidence_threshold=0.50,
            edge_threshold=0.05,
        )),
        ("Debate Aggressive", DebateKellyStrategy(
            name="Debate-Aggr",
            kelly_fraction=0.50,
            max_position_pct=0.25,
            confidence_threshold=0.35,
            edge_threshold=0.03,
        )),
        ("Combined (All)", CombinedStrategy()),
    ]

    # --- Run all strategies ---
    logger.info(f"Running {len(strategies)} strategy rounds...")
    results: list[RoundResult] = []

    for name, strat in strategies:
        logger.info(f"  Running: {name}...")
        # Each round gets fresh random seed for reproducible noise
        random.seed(42)
        result = await run_single_round(name, strat, markets, timeline)
        results.append(result)
        logger.info(f"  {name}: return={result.return_pct:+.2f}%, trades={result.total_trades}")

    # --- Report ---
    print_report(markets, results, timeframe)


if __name__ == "__main__":
    asyncio.run(main())
