#!/usr/bin/env python
"""
LLM News Analysis Strategy Simulation Runner

Fetches real Polymarket data, generates synthetic news events, and runs a
mock LLM-driven trading simulation using the oracle3 framework.

Usage:
    python scripts/run_llm_news_simulation.py
"""

import asyncio
import hashlib
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import NewsEvent, PriceChangeEvent
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import StandardRiskManager
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.types import OrderStatus, TradeSide

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INITIAL_CAPITAL = Decimal("10000")
TRADE_SIZE = Decimal("50")  # contracts per trade
CONFIDENCE_THRESHOLD = 0.3
MIN_MID_PRICE = 0.15
MAX_MID_PRICE = 0.85
MAX_MARKETS = 8
NEWS_INTERVAL = 10  # inject a news event every N price events
COMMISSION_RATE = Decimal("0.002")
MIN_FILL_RATE = Decimal("0.8")
MAX_FILL_RATE = Decimal("1.0")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulation")


# ---------------------------------------------------------------------------
# Data classes for tracking
# ---------------------------------------------------------------------------

@dataclass
class MarketInfo:
    """Metadata about a Polymarket market."""
    question: str
    condition_id: str
    token_id: str
    outcome: str
    best_bid: float
    best_ask: float
    volume: float
    liquidity: float
    ticker: PolyMarketTicker = field(default=None, repr=False)
    price_history: list[dict] = field(default_factory=list, repr=False)


@dataclass
class TradeRecord:
    """Record of a trade executed during the simulation."""
    timestamp: datetime
    market_question: str
    ticker_symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    news_headline: str


@dataclass
class PortfolioSnapshot:
    """Point-in-time snapshot of portfolio value."""
    timestamp: datetime
    total_value: Decimal
    cash: Decimal
    positions_value: Decimal


# ---------------------------------------------------------------------------
# Mock LLM Provider
# ---------------------------------------------------------------------------

class MockLLMProvider:
    """
    Simulates LLM news analysis without requiring real API keys.

    Uses simple keyword heuristics to generate buy/sell/hold signals with
    mock confidence scores, emulating what an LLM-based strategy would do.
    """

    # Keywords that suggest positive sentiment
    POSITIVE_KEYWORDS = [
        "win", "wins", "ahead", "leads", "leading", "surge", "soar",
        "rally", "boost", "positive", "approve", "approved", "success",
        "breakthrough", "confirm", "confirmed", "strong", "increase",
        "gain", "up", "rise", "rising", "support", "pass", "passed",
        "victory", "agree", "agreement", "deal", "progress", "advance",
    ]

    # Keywords that suggest negative sentiment
    NEGATIVE_KEYWORDS = [
        "lose", "loses", "behind", "trails", "trailing", "drop", "fall",
        "decline", "negative", "reject", "rejected", "fail", "failure",
        "crash", "crisis", "concern", "weak", "decrease", "loss",
        "down", "falling", "oppose", "block", "blocked", "delay",
        "defeat", "disagree", "collapse", "risk", "threat", "warning",
    ]

    def analyze_news(self, news_text: str, market_question: str) -> dict[str, Any]:
        """Analyze news text and return a mock LLM analysis result."""
        text_lower = (news_text + " " + market_question).lower()

        positive_score = sum(1 for kw in self.POSITIVE_KEYWORDS if kw in text_lower)
        negative_score = sum(1 for kw in self.NEGATIVE_KEYWORDS if kw in text_lower)

        total = positive_score + negative_score
        if total == 0:
            return {"action": "hold", "confidence": 0.1, "reasoning": "No clear signal"}

        net_score = (positive_score - negative_score) / max(total, 1)

        # Add some randomness to simulate LLM variability
        noise = random.uniform(-0.15, 0.15)
        adjusted = max(-1.0, min(1.0, net_score + noise))

        confidence = min(0.95, abs(adjusted) * 0.8 + random.uniform(0.05, 0.25))

        if adjusted > 0.1:
            action = "buy"
            reasoning = f"Positive sentiment detected (score={adjusted:.2f})"
        elif adjusted < -0.1:
            action = "sell"
            reasoning = f"Negative sentiment detected (score={adjusted:.2f})"
        else:
            action = "hold"
            reasoning = f"Mixed/neutral sentiment (score={adjusted:.2f})"

        return {"action": action, "confidence": confidence, "reasoning": reasoning}


# ---------------------------------------------------------------------------
# Synthetic News Generator
# ---------------------------------------------------------------------------

NEWS_TEMPLATES_POSITIVE = [
    "New poll shows strong support for the '{q}' outcome",
    "Analysts predict positive momentum: {q}",
    "Breaking: Key development boosts likelihood - {q}",
    "Sources confirm progress toward resolution: {q}",
    "Market surge as experts signal agreement on {q}",
    "Report indicates rising probability for {q}",
]

NEWS_TEMPLATES_NEGATIVE = [
    "Setback reported: concerns grow over {q}",
    "Opposition mounts against expected outcome: {q}",
    "Analysts warn of declining prospects - {q}",
    "Crisis threatens progress on {q}",
    "New data suggests risk of failure: {q}",
    "Sources report delay and disagreement on {q}",
]

NEWS_TEMPLATES_NEUTRAL = [
    "Ongoing debate continues: {q}",
    "Mixed signals from experts regarding {q}",
    "Uncertainty persists in markets around {q}",
    "No clear consensus yet on {q}",
]


def generate_synthetic_news(
    market: MarketInfo, timestamp: datetime
) -> NewsEvent:
    """Generate a synthetic news event for a given market."""
    # Randomly choose sentiment
    sentiment = random.choices(
        ["positive", "negative", "neutral"], weights=[0.4, 0.35, 0.25]
    )[0]

    if sentiment == "positive":
        template = random.choice(NEWS_TEMPLATES_POSITIVE)
    elif sentiment == "negative":
        template = random.choice(NEWS_TEMPLATES_NEGATIVE)
    else:
        template = random.choice(NEWS_TEMPLATES_NEUTRAL)

    headline = template.format(q=market.question[:80])
    body = (
        f"{headline}. Market currently trading with "
        f"${market.volume:,.0f} in volume. "
        f"Current probability implied by mid-price: "
        f"{(market.best_bid + market.best_ask) / 2:.1%}."
    )

    uid = hashlib.md5(
        f"{market.token_id}-{timestamp.isoformat()}".encode()
    ).hexdigest()[:12]

    return NewsEvent(
        news=body,
        title=headline,
        source="Synthetic News Generator",
        url="",
        published_at=timestamp,
        categories=["prediction-market"],
        description=body,
        image_url="",
        uuid=uid,
        event_id=market.condition_id,
        ticker=market.ticker,
    )


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

async def fetch_markets(client: httpx.AsyncClient) -> list[dict]:
    """Fetch active Polymarket markets from the Gamma API."""
    logger.info("Fetching active markets from Polymarket Gamma API...")
    url = "https://gamma-api.polymarket.com/markets"
    params = {"active": "true", "closed": "false", "limit": "50"}

    resp = await client.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    markets = resp.json()

    if not isinstance(markets, list):
        logger.warning("Unexpected response format from Gamma API")
        return []

    logger.info(f"Fetched {len(markets)} raw markets")
    return markets


def filter_and_rank_markets(raw_markets: list[dict]) -> list[MarketInfo]:
    """Filter markets by mid-price range and rank by volume."""
    candidates: list[MarketInfo] = []

    for mkt in raw_markets:
        question = mkt.get("question", "")
        condition_id = mkt.get("conditionId", "")
        clob_token_ids = mkt.get("clobTokenIds", "")
        best_bid_str = mkt.get("bestBid", "0")
        best_ask_str = mkt.get("bestAsk", "0")
        volume_str = mkt.get("volume", "0")
        liquidity_str = mkt.get("liquidityNum", "0")

        try:
            best_bid = float(best_bid_str)
            best_ask = float(best_ask_str)
            volume = float(volume_str)
            liquidity = float(liquidity_str)
        except (ValueError, TypeError):
            continue

        if best_bid <= 0 or best_ask <= 0:
            continue

        mid_price = (best_bid + best_ask) / 2.0
        if mid_price < MIN_MID_PRICE or mid_price > MAX_MID_PRICE:
            continue

        # clobTokenIds is a JSON string like '["token1", "token2"]'
        # First token is YES, second is NO
        if isinstance(clob_token_ids, str):
            try:
                import json as _json
                clob_token_ids = _json.loads(clob_token_ids)
            except (ValueError, TypeError):
                continue

        if not clob_token_ids or not isinstance(clob_token_ids, list):
            continue

        token_id = clob_token_ids[0]  # YES token
        if not token_id or not condition_id:
            continue

        candidates.append(
            MarketInfo(
                question=question,
                condition_id=condition_id,
                token_id=token_id,
                outcome="Yes",
                best_bid=best_bid,
                best_ask=best_ask,
                volume=volume,
                liquidity=liquidity,
            )
        )

    # Sort by volume descending, take top N
    candidates.sort(key=lambda m: m.volume, reverse=True)
    selected = candidates[:MAX_MARKETS]
    logger.info(
        f"Selected {len(selected)} markets after filtering "
        f"(mid-price {MIN_MID_PRICE}-{MAX_MID_PRICE}, top by volume)"
    )
    return selected


async def fetch_price_history(
    client: httpx.AsyncClient, token_id: str
) -> list[dict]:
    """Fetch hourly price history for a token from the CLOB API."""
    url = "https://clob.polymarket.com/prices-history"
    params = {"market": token_id, "interval": "max", "fidelity": "60"}

    try:
        resp = await client.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        history = data.get("history", [])
        return history
    except Exception as e:
        logger.warning(f"Failed to fetch price history for {token_id[:16]}...: {e}")
        return []


async def fetch_all_price_histories(
    client: httpx.AsyncClient, markets: list[MarketInfo]
) -> None:
    """Fetch price history for all selected markets concurrently."""
    logger.info(f"Fetching price history for {len(markets)} markets...")

    tasks = [fetch_price_history(client, m.token_id) for m in markets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for market, result in zip(markets, results):
        if isinstance(result, Exception):
            logger.warning(
                f"Price history error for '{market.question[:40]}...': {result}"
            )
            market.price_history = []
        else:
            market.price_history = result
            logger.info(
                f"  {market.question[:50]}... -> {len(result)} price points"
            )


# ---------------------------------------------------------------------------
# Simulation Engine
# ---------------------------------------------------------------------------

def create_ticker(market: MarketInfo) -> PolyMarketTicker:
    """Create a PolyMarketTicker for a market."""
    # Use a short deterministic symbol to keep output readable
    short_id = hashlib.md5(market.token_id.encode()).hexdigest()[:8].upper()
    return PolyMarketTicker(
        symbol=short_id,
        name=market.question[:60],
        token_id=market.token_id,
        market_id=market.condition_id,
    )


def seed_order_book(
    market_data: MarketDataManager,
    ticker: PolyMarketTicker,
    bid_price: Decimal,
    ask_price: Decimal,
    size: Decimal = Decimal("1000"),
) -> None:
    """Create and seed an order book for a ticker."""
    ob = OrderBook()
    bids = [Level(price=bid_price, size=size)] if bid_price > Decimal("0") else []
    asks = [Level(price=ask_price, size=size)] if ask_price < Decimal("1") else []
    ob.update(asks=asks, bids=bids)
    market_data.update_order_book(ticker, ob)


def build_event_timeline(
    markets: list[MarketInfo],
) -> list[PriceChangeEvent | NewsEvent]:
    """
    Build a chronological timeline of PriceChangeEvents and NewsEvents.

    Interleaves synthetic news every NEWS_INTERVAL price events.
    """
    # Collect all price events with timestamps
    raw_events: list[tuple[datetime, PriceChangeEvent | NewsEvent]] = []

    for market in markets:
        ticker = market.ticker
        for point in market.price_history:
            try:
                ts = datetime.fromtimestamp(int(point["t"]), tz=timezone.utc)
                price = Decimal(str(point["p"]))
                # Clamp price to valid range
                price = max(Decimal("0.01"), min(Decimal("0.99"), price))
            except (KeyError, ValueError, TypeError):
                continue

            event = PriceChangeEvent(ticker=ticker, price=price, timestamp=ts)
            raw_events.append((ts, event))

    # Sort chronologically
    raw_events.sort(key=lambda x: x[0])

    # Interleave news events
    timeline: list[PriceChangeEvent | NewsEvent] = []
    price_count = 0

    for ts, event in raw_events:
        timeline.append(event)
        price_count += 1

        if price_count % NEWS_INTERVAL == 0:
            # Pick a random market for the news event
            market = random.choice(markets)
            news = generate_synthetic_news(market, ts)
            timeline.append(news)

    logger.info(
        f"Built timeline with {len(timeline)} events "
        f"({price_count} price changes, {len(timeline) - price_count} news)"
    )
    return timeline


async def execute_mock_strategy(
    event: NewsEvent,
    trader: PaperTrader,
    mock_llm: MockLLMProvider,
    market_map: dict[str, MarketInfo],
    trade_log: list[TradeRecord],
) -> None:
    """Process a news event through the mock LLM and execute trades."""
    ticker = event.ticker
    if ticker is None:
        return

    symbol = ticker.symbol
    market = market_map.get(symbol)
    if market is None:
        return

    # Analyze news with mock LLM
    analysis = mock_llm.analyze_news(event.news, market.question)
    action = analysis["action"]
    confidence = analysis["confidence"]

    if confidence < CONFIDENCE_THRESHOLD:
        return

    if action == "hold":
        return

    if action == "buy":
        level = trader.market_data.get_best_ask(ticker)
        if level is None:
            return
        price = level.price
        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=ticker,
            limit_price=price,
            quantity=TRADE_SIZE,
        )
        if result.order and result.order.status in (
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
        ):
            for trade in result.order.trades:
                trade_log.append(
                    TradeRecord(
                        timestamp=event.published_at,
                        market_question=market.question[:60],
                        ticker_symbol=symbol,
                        side="BUY",
                        quantity=trade.quantity,
                        price=trade.price,
                        commission=trade.commission,
                        news_headline=event.title[:80],
                    )
                )

    elif action == "sell":
        # Only sell if we have a position
        pos = trader.position_manager.get_position(ticker)
        if pos is None or pos.quantity <= Decimal("0"):
            return
        sell_qty = min(TRADE_SIZE, pos.quantity)
        level = trader.market_data.get_best_bid(ticker)
        if level is None:
            return
        price = level.price
        result = await trader.place_order(
            side=TradeSide.SELL,
            ticker=ticker,
            limit_price=price,
            quantity=sell_qty,
        )
        if result.order and result.order.status in (
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
        ):
            for trade in result.order.trades:
                trade_log.append(
                    TradeRecord(
                        timestamp=event.published_at,
                        market_question=market.question[:60],
                        ticker_symbol=symbol,
                        side="SELL",
                        quantity=trade.quantity,
                        price=trade.price,
                        commission=trade.commission,
                        news_headline=event.title[:80],
                    )
                )


async def run_simulation(markets: list[MarketInfo]) -> None:
    """Run the full trading simulation."""

    # -- Set up framework components --
    market_data = MarketDataManager()
    position_manager = PositionManager()
    risk_manager = StandardRiskManager(
        position_manager=position_manager,
        market_data=market_data,
        max_single_trade_size=Decimal("500"),
        max_position_size=Decimal("2000"),
        max_total_exposure=Decimal("8000"),
        max_drawdown_pct=Decimal("0.25"),
        max_positions=MAX_MARKETS,
        initial_capital=INITIAL_CAPITAL,
    )
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=MIN_FILL_RATE,
        max_fill_rate=MAX_FILL_RATE,
        commission_rate=COMMISSION_RATE,
    )

    # -- Initial cash position --
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=INITIAL_CAPITAL,
            average_cost=Decimal("1"),
            realized_pnl=Decimal("0"),
        )
    )

    # -- Create tickers and seed order books --
    market_map: dict[str, MarketInfo] = {}
    for market in markets:
        ticker = create_ticker(market)
        market.ticker = ticker
        market_map[ticker.symbol] = market

        bid = Decimal(str(market.best_bid)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        ask = Decimal(str(market.best_ask)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        seed_order_book(market_data, ticker, bid, ask)

    # -- Build event timeline --
    timeline = build_event_timeline(markets)
    if not timeline:
        logger.error("No events in timeline - cannot run simulation")
        return

    # -- Run simulation loop --
    mock_llm = MockLLMProvider()
    trade_log: list[TradeRecord] = []
    snapshots: list[PortfolioSnapshot] = []
    peak_value = INITIAL_CAPITAL
    max_drawdown = Decimal("0")
    events_processed = 0
    snapshot_interval = max(1, len(timeline) // 50)  # ~50 snapshots

    logger.info(f"Starting simulation with {len(timeline)} events...")

    for i, event in enumerate(timeline):
        if isinstance(event, PriceChangeEvent):
            # Update market data
            market_data.process_price_change_event(event)

        elif isinstance(event, NewsEvent):
            # Process through mock strategy
            await execute_mock_strategy(
                event, trader, mock_llm, market_map, trade_log
            )

        events_processed += 1

        # Take periodic snapshots
        if i % snapshot_interval == 0 or i == len(timeline) - 1:
            portfolio_values = position_manager.get_portfolio_value(market_data)
            total_value = sum(portfolio_values.values(), Decimal("0"))
            cash_pos = position_manager.get_position(CashTicker.POLYMARKET_USDC)
            cash = cash_pos.quantity if cash_pos else Decimal("0")

            ts = (
                event.timestamp
                if isinstance(event, PriceChangeEvent)
                else getattr(event, "published_at", datetime.now(timezone.utc))
            )
            snapshots.append(
                PortfolioSnapshot(
                    timestamp=ts,
                    total_value=total_value,
                    cash=cash,
                    positions_value=total_value - cash,
                )
            )

            # Track drawdown
            if total_value > peak_value:
                peak_value = total_value
            if peak_value > 0:
                dd = (peak_value - total_value) / peak_value
                if dd > max_drawdown:
                    max_drawdown = dd

    # -- Generate report --
    print_report(markets, trade_log, snapshots, max_drawdown, market_data, position_manager)


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def print_report(
    markets: list[MarketInfo],
    trade_log: list[TradeRecord],
    snapshots: list[PortfolioSnapshot],
    max_drawdown: Decimal,
    market_data: MarketDataManager,
    position_manager: PositionManager,
) -> None:
    """Print a comprehensive simulation report."""
    sep = "=" * 78

    print(f"\n{sep}")
    print("  LLM NEWS ANALYSIS STRATEGY - SIMULATION REPORT")
    print(sep)

    # -- Simulation Parameters --
    print("\n--- Simulation Parameters ---")
    print(f"  Initial Capital:      ${INITIAL_CAPITAL:,.2f}")
    print(f"  Trade Size:           {TRADE_SIZE} contracts")
    print(f"  Confidence Threshold: {CONFIDENCE_THRESHOLD}")
    print(f"  Commission Rate:      {COMMISSION_RATE}")
    print(f"  Markets Tracked:      {len(markets)}")
    if snapshots:
        start = snapshots[0].timestamp
        end = snapshots[-1].timestamp
        print(f"  Timeframe:            {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")

    # -- Markets Summary --
    print("\n--- Markets ---")
    for m in markets:
        mid = (m.best_bid + m.best_ask) / 2
        hist_len = len(m.price_history)
        print(
            f"  [{m.ticker.symbol}] {m.question[:55]:<55} "
            f"mid={mid:.2f}  vol=${m.volume:>12,.0f}  pts={hist_len}"
        )

    # -- Per-Market Trade Summary --
    print("\n--- Per-Market Trade Summary ---")
    market_trades: dict[str, list[TradeRecord]] = {}
    for t in trade_log:
        market_trades.setdefault(t.ticker_symbol, []).append(t)

    total_pnl = Decimal("0")
    total_commission = Decimal("0")
    winning_markets = 0
    losing_markets = 0

    if not market_trades:
        print("  No trades executed during simulation.")
    else:
        print(
            f"  {'Symbol':<10} {'Question':<40} {'Trades':>6} "
            f"{'Buys':>5} {'Sells':>5} {'Realized P&L':>14} {'Commission':>12}"
        )
        print("  " + "-" * 100)

        for symbol, trades in sorted(market_trades.items()):
            mkt = None
            for m in markets:
                if m.ticker and m.ticker.symbol == symbol:
                    mkt = m
                    break

            question = mkt.question[:38] if mkt else "?"
            buys = sum(1 for t in trades if t.side == "BUY")
            sells = sum(1 for t in trades if t.side == "SELL")
            comm = sum(t.commission for t in trades)
            total_commission += comm

            # Get realized PnL from position manager
            ticker = mkt.ticker if mkt else None
            realized = Decimal("0")
            if ticker:
                pos = position_manager.get_position(ticker)
                if pos:
                    realized = pos.realized_pnl

            total_pnl += realized
            if realized > 0:
                winning_markets += 1
            elif realized < 0:
                losing_markets += 1

            pnl_str = f"${realized:>+10.2f}"
            comm_str = f"${comm:>8.2f}"
            print(
                f"  {symbol:<10} {question:<40} {len(trades):>6} "
                f"{buys:>5} {sells:>5} {pnl_str:>14} {comm_str:>12}"
            )

    # -- Overall Summary --
    print("\n--- Overall Performance ---")
    final_value = snapshots[-1].total_value if snapshots else INITIAL_CAPITAL
    total_return = (
        ((final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100)
        if INITIAL_CAPITAL > 0
        else Decimal("0")
    )

    total_trades = len(trade_log)
    winning_trades = sum(1 for t in trade_log if t.side == "SELL" and t.price > Decimal("0"))

    unrealized = position_manager.get_total_unrealized_pnl(market_data)
    total_pnl_combined = total_pnl + unrealized

    print(f"  Total Trades:         {total_trades}")
    print(f"  Buy Trades:           {sum(1 for t in trade_log if t.side == 'BUY')}")
    print(f"  Sell Trades:          {sum(1 for t in trade_log if t.side == 'SELL')}")
    print(f"  Winning Markets:      {winning_markets}")
    print(f"  Losing Markets:       {losing_markets}")
    print(f"  Total Commission:     ${total_commission:.4f}")
    print(f"  Realized P&L:         ${total_pnl:+.4f}")
    print(f"  Unrealized P&L:       ${unrealized:+.4f}")
    print(f"  Total P&L:            ${total_pnl_combined:+.4f}")
    print(f"  Final Portfolio Value: ${final_value:.2f}")
    print(f"  Total Return:         {total_return:+.2f}%")
    print(f"  Max Drawdown:         {max_drawdown * 100:.2f}%")

    # -- Open Positions --
    non_cash = position_manager.get_non_cash_positions()
    open_positions = [p for p in non_cash if p.quantity > Decimal("0")]
    if open_positions:
        print("\n--- Open Positions at End ---")
        print(f"  {'Symbol':<10} {'Qty':>8} {'Avg Cost':>10} {'Realized':>12}")
        print("  " + "-" * 44)
        for pos in open_positions:
            print(
                f"  {pos.ticker.symbol:<10} {pos.quantity:>8.1f} "
                f"${pos.average_cost:>8.4f} ${pos.realized_pnl:>+10.4f}"
            )

    # -- Portfolio Value Timeline --
    if snapshots:
        print(f"\n--- Portfolio Value Timeline ({len(snapshots)} snapshots) ---")
        print(f"  {'Timestamp':<22} {'Total Value':>14} {'Cash':>14} {'Positions':>14}")
        print("  " + "-" * 66)

        # Show at most 25 evenly-spaced snapshots
        step = max(1, len(snapshots) // 25)
        display = snapshots[::step]
        # Always include the last snapshot
        if display[-1] is not snapshots[-1]:
            display.append(snapshots[-1])

        for s in display:
            print(
                f"  {s.timestamp:%Y-%m-%d %H:%M}    "
                f"${s.total_value:>12,.2f} "
                f"${s.cash:>12,.2f} "
                f"${s.positions_value:>12,.2f}"
            )

    print(f"\n{sep}")
    print("  Simulation complete.")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    """Fetch data and run simulation."""
    logger.info("LLM News Strategy Simulation starting...")

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "oracle3-simulation/1.0"},
    ) as client:
        # 1. Fetch and filter markets
        try:
            raw_markets = await fetch_markets(client)
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch markets: {e}")
            logger.info("Generating fallback synthetic markets for demo purposes...")
            raw_markets = []

        if raw_markets:
            markets = filter_and_rank_markets(raw_markets)
        else:
            markets = []

        # Fallback: generate synthetic markets if API fails or returns nothing usable
        if not markets:
            logger.info("No suitable markets from API; using synthetic fallback data")
            markets = _generate_fallback_markets()

        # 2. Fetch price histories
        api_markets = [m for m in markets if not m.price_history]
        if api_markets:
            await fetch_all_price_histories(client, api_markets)

        # Drop markets with no price history
        markets = [m for m in markets if m.price_history]
        if not markets:
            logger.error(
                "No markets with price history available. "
                "Using generated price data as fallback."
            )
            markets = _generate_fallback_markets()

    # 3. Run simulation
    await run_simulation(markets)


def _generate_fallback_markets() -> list[MarketInfo]:
    """Generate synthetic fallback markets when the API is unavailable."""
    import time

    synthetic_questions = [
        "Will Bitcoin exceed $100,000 by end of Q1 2026?",
        "Will the Federal Reserve cut interest rates in March 2026?",
        "Will SpaceX successfully launch Starship to orbit?",
        "Will the US GDP growth exceed 3% in 2026?",
        "Will AI regulation legislation pass in the US Senate?",
        "Will global temperatures set a new record in 2026?",
    ]

    markets: list[MarketInfo] = []
    base_time = int(time.time()) - 72 * 3600  # 72 hours ago

    for i, question in enumerate(synthetic_questions):
        bid = round(random.uniform(0.20, 0.75), 2)
        ask = round(bid + random.uniform(0.01, 0.05), 2)
        ask = min(ask, 0.85)
        token_id = hashlib.md5(question.encode()).hexdigest()

        # Generate synthetic price history (72 hours of hourly data)
        history = []
        price = (bid + ask) / 2
        for h in range(72):
            ts = base_time + h * 3600
            # Random walk
            price += random.gauss(0, 0.015)
            price = max(0.05, min(0.95, price))
            history.append({"t": ts, "p": f"{price:.4f}"})

        markets.append(
            MarketInfo(
                question=question,
                condition_id=f"synthetic-{i}",
                token_id=token_id,
                outcome="Yes",
                best_bid=bid,
                best_ask=ask,
                volume=random.uniform(50000, 500000),
                liquidity=random.uniform(10000, 100000),
                price_history=history,
            )
        )

    logger.info(f"Generated {len(markets)} synthetic fallback markets")
    return markets


if __name__ == "__main__":
    asyncio.run(main())
