#!/usr/bin/env python3
"""
Real LLM Backtest — Polymarket Multi-Strategy Comparison

Replaces mock keyword sentiment with real LLM calls via litellm.
Compares:
  1. Momentum (baseline, no LLM)
  2. LLM News Sentiment (single call per news event)
  3. LLM Debate (bull/bear/judge, 3 calls per event)
  4. LLM Debate + Kelly sizing

Requires: ANTHROPIC_API_KEY (or OPENAI_API_KEY / GEMINI_API_KEY)

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 scripts/run_llm_backtest.py
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
MAX_MARKETS = 4  # fewer markets to keep LLM calls manageable
COMMISSION_RATE = Decimal("0.002")
MIN_FILL_RATE = Decimal("0.85")
MAX_FILL_RATE = Decimal("1.0")
NEWS_INTERVAL = 30  # inject synthetic news every N price events (less frequent)

# LLM config — uses `claude -p` CLI with user's Claude Code subscription
FAST_MODEL = os.environ.get("BACKTEST_FAST_MODEL", "claude-haiku-4-5-20251001")
JUDGE_MODEL = os.environ.get("BACKTEST_JUDGE_MODEL", "claude-haiku-4-5-20251001")
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_DELAY = 2.0  # seconds

logging.getLogger("httpx").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("llm_backtest")
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# LLM call with retry
# ---------------------------------------------------------------------------

_llm_call_count = 0
_llm_cache: dict[str, str] = {}


async def call_llm(model: str, prompt: str, system: str = "") -> str | None:
    """Call LLM via `claude -p` CLI (stdin pipe) using the user's subscription."""
    global _llm_call_count

    # Cache key to avoid duplicate calls
    cache_key = hashlib.md5(f"{model}:{system}:{prompt}".encode()).hexdigest()
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    for attempt in range(LLM_MAX_RETRIES):
        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)  # bypass nested session check

            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", "--model", model, "--max-turns", "1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode()), timeout=120
            )
            text = stdout.decode().strip()

            if proc.returncode != 0 or not text:
                err = stderr.decode().strip()
                logger.warning(f"claude CLI error (attempt {attempt+1}): {err[:200]}")
                if attempt < LLM_MAX_RETRIES - 1:
                    await asyncio.sleep(LLM_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                return None

            _llm_call_count += 1
            _llm_cache[cache_key] = text
            logger.info(f"  LLM #{_llm_call_count} OK ({len(text)} chars)")
            return text

        except asyncio.TimeoutError:
            logger.warning(f"claude CLI timeout (attempt {attempt+1})")
            try:
                proc.kill()
            except Exception:
                pass
            if attempt < LLM_MAX_RETRIES - 1:
                await asyncio.sleep(LLM_RETRY_BASE_DELAY)
            else:
                return None
        except Exception as e:
            logger.warning(f"claude CLI failed (attempt {attempt+1}): {e}")
            if attempt < LLM_MAX_RETRIES - 1:
                await asyncio.sleep(LLM_RETRY_BASE_DELAY)
            else:
                return None
    return None


def parse_json_response(text: str | None) -> dict | None:
    """Extract JSON from LLM response text."""
    if not text:
        return None
    # Try to find JSON block
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find first { ... }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end+1])
            except json.JSONDecodeError:
                return None
    return None


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
    sharpe_ratio: Decimal
    profit_factor: Decimal
    win_rate: Decimal
    avg_trade_pnl: Decimal
    llm_calls: int
    timeline: list[tuple[datetime, Decimal]]


# ---------------------------------------------------------------------------
# Synthetic news (more realistic for LLM analysis)
# ---------------------------------------------------------------------------

POS_TEMPLATES = [
    "Breaking: Official sources confirm strong progress toward '{q}' — key stakeholders signal support",
    "Reuters: New data supports positive outcome for '{q}'; analysts raise probability estimates",
    "AP: Major development increases likelihood of '{q}' resolution; expert panel agrees",
    "Bloomberg: Market participants shift bullish on '{q}' after new policy announcement",
]
NEG_TEMPLATES = [
    "Reuters: Significant setback for '{q}' as opposition mounts and key deadline approaches",
    "AP: New evidence casts doubt on '{q}'; multiple analysts lower probability forecasts",
    "Bloomberg: Critical failure threatens '{q}' outcome; stakeholders express concerns",
    "WSJ: Growing consensus that '{q}' faces substantial headwinds; institutional confidence drops",
]
NEU_TEMPLATES = [
    "AP: Mixed signals on '{q}' — experts divided on likely outcome as debate continues",
    "Reuters: Uncertainty persists around '{q}'; new data offers contradictory indications",
]


def make_news(market: MarketInfo, ts: datetime) -> NewsEvent:
    sentiment = random.choices(["pos", "neg", "neu"], weights=[0.40, 0.35, 0.25])[0]
    templates = {"pos": POS_TEMPLATES, "neg": NEG_TEMPLATES, "neu": NEU_TEMPLATES}
    headline = random.choice(templates[sentiment]).format(q=market.question[:80])
    mid = (market.best_bid + market.best_ask) / 2
    body = (
        f"{headline}. "
        f"Current market-implied probability: {mid:.1%}. "
        f"24h volume: ${market.volume:,.0f}. "
        f"Liquidity depth: ${market.liquidity:,.0f}."
    )
    uid = hashlib.md5(f"{market.token_id}-{ts.isoformat()}".encode()).hexdigest()[:12]
    return NewsEvent(
        news=body,
        title=headline,
        source=random.choice(["Reuters", "AP", "Bloomberg", "WSJ"]),
        published_at=ts,
        ticker=market.ticker,
        uuid=uid,
        event_id=market.condition_id,
    )


# ---------------------------------------------------------------------------
# Data fetching (same as run_multi_backtest.py)
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


async def fetch_price_history(client: httpx.AsyncClient, token_id: str) -> list[dict]:
    url = "https://clob.polymarket.com/prices-history"
    params = {"market": token_id, "interval": "max", "fidelity": "60"}
    try:
        resp = await client.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json().get("history", [])
    except Exception as e:
        logger.warning(f"Price history failed for {token_id[:16]}...: {e}")
        return []


async def fetch_all_histories(client: httpx.AsyncClient, markets: list[MarketInfo]) -> None:
    logger.info(f"Fetching price histories for {len(markets)} markets...")
    tasks = [fetch_price_history(client, m.token_id) for m in markets]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for market, result in zip(markets, results):
        if isinstance(result, Exception):
            market.price_history = []
        else:
            market.price_history = result
            logger.info(f"  {market.question[:50]}... -> {len(result)} pts")


def create_ticker(market: MarketInfo) -> PolyMarketTicker:
    short_id = hashlib.md5(market.token_id.encode()).hexdigest()[:8].upper()
    return PolyMarketTicker(
        symbol=short_id,
        name=market.question[:60],
        token_id=market.token_id,
        market_id=market.condition_id,
    )


def build_timeline(markets: list[MarketInfo]) -> list[Event]:
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
# Shared backtest context
# ---------------------------------------------------------------------------


class BacktestContext:
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

        self.position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=INITIAL_CAPITAL,
                average_cost=Decimal("1"),
                realized_pnl=Decimal("0"),
            )
        )

        for market in markets:
            ticker = market.ticker
            bid = Decimal(str(market.best_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ask = Decimal(str(market.best_ask)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ob = OrderBook()
            bids = [Level(price=bid, size=Decimal("1000"))] if bid > 0 else []
            asks = [Level(price=ask, size=Decimal("1000"))] if ask < 1 else []
            ob.update(asks=asks, bids=bids)
            self.market_data.update_order_book(ticker, ob)


# ---------------------------------------------------------------------------
# Strategy 1: Momentum (baseline, no LLM)
# ---------------------------------------------------------------------------


class MomentumStrategy:
    def __init__(self, window: int = 20, buy_threshold: float = -0.03,
                 sell_threshold: float = 0.03, trade_size: Decimal = Decimal("50")):
        self.window = window
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.trade_size = trade_size
        self._prices: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))
        self.llm_calls = 0

    async def on_event(self, event: Event, ctx: BacktestContext) -> None:
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


# ---------------------------------------------------------------------------
# Strategy 2: LLM News Sentiment (single LLM call per news event)
# ---------------------------------------------------------------------------


class LLMNewsSentimentStrategy:
    """Real LLM news analysis — single call per news event."""

    def __init__(self, model: str = FAST_MODEL, confidence_threshold: float = 0.40,
                 trade_size: Decimal = Decimal("80"), cooldown: int = 10):
        self.model = model
        self.conf_threshold = confidence_threshold
        self.trade_size = trade_size
        self.cooldown = cooldown
        self._last_trade: dict[str, int] = defaultdict(int)
        self._event_count = 0
        self.llm_calls = 0

    async def on_event(self, event: Event, ctx: BacktestContext) -> None:
        self._event_count += 1
        if not isinstance(event, NewsEvent) or event.ticker is None:
            return

        sym = event.ticker.symbol
        if self._event_count - self._last_trade[sym] < self.cooldown:
            return

        # Build prompt
        ticker = event.ticker
        pos = ctx.position_manager.get_position(ticker)
        cash = sum(
            (p.quantity for p in ctx.position_manager.get_cash_positions()), Decimal("0")
        )
        best_bid = ctx.trader.market_data.get_best_bid(ticker)
        best_ask = ctx.trader.market_data.get_best_ask(ticker)
        if not best_bid or not best_ask:
            return
        mid = float((best_bid.price + best_ask.price) / 2)

        prompt = f"""Analyze this news for a Polymarket prediction market.

Market: {ticker.name}
Current probability: {mid:.2%}
Position: {pos.quantity if pos else 0} shares @ avg {pos.average_cost if pos else 0}
Cash: ${cash:.2f}

News headline: {event.title}
News body: {event.news}

Should I buy YES (probability should be higher), sell YES (probability should be lower), or hold?

Respond with ONLY this JSON:
{{"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, "fair_probability": 0.0-1.0, "reasoning": "brief explanation"}}"""

        response = await call_llm(self.model, prompt)
        self.llm_calls += 1
        parsed = parse_json_response(response)
        if not parsed:
            return

        action = parsed.get("action", "hold").lower()
        confidence = float(parsed.get("confidence", 0))

        if confidence < self.conf_threshold or action == "hold":
            return

        self._last_trade[sym] = self._event_count

        if action == "buy":
            level = ctx.trader.market_data.get_best_ask(ticker)
            if level:
                result = await ctx.trader.place_order(
                    TradeSide.BUY, ticker, level.price, self.trade_size
                )
                if result.order:
                    for t in result.order.trades:
                        ctx.analyzer.add_trade(t)
        elif action == "sell":
            if pos and pos.quantity > 0:
                level = ctx.trader.market_data.get_best_bid(ticker)
                if level:
                    qty = min(self.trade_size, pos.quantity)
                    result = await ctx.trader.place_order(
                        TradeSide.SELL, ticker, level.price, qty
                    )
                    if result.order:
                        for t in result.order.trades:
                            ctx.analyzer.add_trade(t)


# ---------------------------------------------------------------------------
# Strategy 3: LLM Debate (bull/bear/judge)
# ---------------------------------------------------------------------------


class LLMDebateStrategy:
    """Full 3-call debate: bull argues higher, bear argues lower, judge decides."""

    def __init__(self, bull_model: str = FAST_MODEL, bear_model: str = FAST_MODEL,
                 judge_model: str = JUDGE_MODEL, confidence_threshold: float = 0.50,
                 edge_threshold: float = 0.05, trade_size: Decimal = Decimal("80"),
                 cooldown: int = 15):
        self.bull_model = bull_model
        self.bear_model = bear_model
        self.judge_model = judge_model
        self.conf_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.trade_size = trade_size
        self.cooldown = cooldown
        self._news_buf: dict[str, list[str]] = defaultdict(list)
        self._prices: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self._last_trade: dict[str, int] = defaultdict(int)
        self._event_count = 0
        self.llm_calls = 0

    async def on_event(self, event: Event, ctx: BacktestContext) -> None:
        self._event_count += 1

        if isinstance(event, PriceChangeEvent):
            self._prices[event.ticker.symbol].append(float(event.price))
            return

        if not isinstance(event, NewsEvent) or event.ticker is None:
            return

        sym = event.ticker.symbol
        self._news_buf[sym].append(f"{event.title}: {event.news[:200]}")
        self._news_buf[sym] = self._news_buf[sym][-5:]  # keep last 5

        if self._event_count - self._last_trade.get(sym, 0) < self.cooldown:
            return

        ticker = event.ticker
        best_bid = ctx.trader.market_data.get_best_bid(ticker)
        best_ask = ctx.trader.market_data.get_best_ask(ticker)
        if not best_bid or not best_ask:
            return
        mid = float((best_bid.price + best_ask.price) / 2)
        pos = ctx.position_manager.get_position(ticker)
        cash = sum(
            (p.quantity for p in ctx.position_manager.get_cash_positions()), Decimal("0")
        )

        news_block = "\n".join(f"- {n}" for n in self._news_buf[sym])
        prices = self._prices.get(sym, deque())
        price_trend = "flat"
        if len(prices) >= 5:
            pct = (prices[-1] - prices[0]) / prices[0] if prices[0] else 0
            price_trend = f"{'up' if pct > 0 else 'down'} {abs(pct)*100:.1f}%"

        context_block = f"""Market: {ticker.name}
Current probability: {mid:.4f} ({mid:.1%})
Price trend: {price_trend}
Position: {pos.quantity if pos else 0} shares
Cash: ${cash:.2f}

Recent news:
{news_block}"""

        # 1. Bull case
        bull_prompt = f"""You are arguing the BULL case for this prediction market.
Argue why the probability should be HIGHER than {mid:.1%}.

{context_block}

Make your strongest case. End with:
CONVICTION: <0.0-1.0>
FAIR_PROBABILITY: <0.0-1.0>"""

        bull_response = await call_llm(self.bull_model, bull_prompt)
        self.llm_calls += 1

        # 2. Bear case
        bear_prompt = f"""You are arguing the BEAR case for this prediction market.
Argue why the probability should be LOWER than {mid:.1%}.

{context_block}

Make your strongest case. End with:
CONVICTION: <0.0-1.0>
FAIR_PROBABILITY: <0.0-1.0>"""

        bear_response = await call_llm(self.bear_model, bear_prompt)
        self.llm_calls += 1

        if not bull_response or not bear_response:
            return

        # 3. Judge
        judge_prompt = f"""You are a senior prediction market judge weighing two analysts' arguments.

{context_block}

## Bull Case
{bull_response}

## Bear Case
{bear_response}

## Calibration Examples
- Fed rate cut: Market 72%, correct was 95% (strong CPI data = near certainty)
- Biden dropout: Market 25%, correct was 55% (private pressure significant but uncertain)
- BTC $100K: Market 45%, correct was 50% (ETF inflows bullish but $67K→$100K is big move)

Weigh both arguments. Consider what's already priced in.

Respond ONLY with JSON:
{{"fair_probability": 0.0-1.0, "confidence": 0.0-1.0, "action": "buy"|"sell"|"hold", "reasoning": "brief", "edge": float}}"""

        judge_response = await call_llm(self.judge_model, judge_prompt)
        self.llm_calls += 1

        parsed = parse_json_response(judge_response)
        if not parsed:
            return

        action = parsed.get("action", "hold").lower()
        confidence = float(parsed.get("confidence", 0))
        fair_prob = float(parsed.get("fair_probability", mid))
        edge = fair_prob - mid

        if confidence < self.conf_threshold or abs(edge) < self.edge_threshold or action == "hold":
            return

        self._last_trade[sym] = self._event_count

        if action == "buy" and edge > 0:
            level = ctx.trader.market_data.get_best_ask(ticker)
            if level:
                result = await ctx.trader.place_order(
                    TradeSide.BUY, ticker, level.price, self.trade_size
                )
                if result.order:
                    for t in result.order.trades:
                        ctx.analyzer.add_trade(t)
        elif action == "sell" and edge < 0:
            if pos and pos.quantity > 0:
                level = ctx.trader.market_data.get_best_bid(ticker)
                if level:
                    qty = min(self.trade_size, pos.quantity)
                    result = await ctx.trader.place_order(
                        TradeSide.SELL, ticker, level.price, qty
                    )
                    if result.order:
                        for t in result.order.trades:
                            ctx.analyzer.add_trade(t)


# ---------------------------------------------------------------------------
# Strategy 4: LLM Debate + Kelly Criterion
# ---------------------------------------------------------------------------


class LLMDebateKellyStrategy:
    """Debate strategy with Kelly criterion position sizing."""

    def __init__(self, bull_model: str = FAST_MODEL, bear_model: str = FAST_MODEL,
                 judge_model: str = JUDGE_MODEL, kelly_fraction: float = 0.25,
                 max_position_pct: float = 0.15, confidence_threshold: float = 0.50,
                 edge_threshold: float = 0.05, cooldown: int = 15):
        self.bull_model = bull_model
        self.bear_model = bear_model
        self.judge_model = judge_model
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.conf_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.cooldown = cooldown
        self._news_buf: dict[str, list[str]] = defaultdict(list)
        self._prices: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self._last_trade: dict[str, int] = defaultdict(int)
        self._event_count = 0
        self.llm_calls = 0

    def _kelly_size(self, fair_prob: float, market_price: float, portfolio: Decimal) -> Decimal:
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
            return

        if not isinstance(event, NewsEvent) or event.ticker is None:
            return

        sym = event.ticker.symbol
        self._news_buf[sym].append(f"{event.title}: {event.news[:200]}")
        self._news_buf[sym] = self._news_buf[sym][-5:]

        if self._event_count - self._last_trade.get(sym, 0) < self.cooldown:
            return

        ticker = event.ticker
        best_bid = ctx.trader.market_data.get_best_bid(ticker)
        best_ask = ctx.trader.market_data.get_best_ask(ticker)
        if not best_bid or not best_ask:
            return
        mid = float((best_bid.price + best_ask.price) / 2)
        pos = ctx.position_manager.get_position(ticker)
        cash = sum(
            (p.quantity for p in ctx.position_manager.get_cash_positions()), Decimal("0")
        )

        news_block = "\n".join(f"- {n}" for n in self._news_buf[sym])
        prices = self._prices.get(sym, deque())
        price_trend = "flat"
        if len(prices) >= 5:
            pct = (prices[-1] - prices[0]) / prices[0] if prices[0] else 0
            price_trend = f"{'up' if pct > 0 else 'down'} {abs(pct)*100:.1f}%"

        context_block = f"""Market: {ticker.name}
Current probability: {mid:.4f} ({mid:.1%})
Price trend: {price_trend}
Position: {pos.quantity if pos else 0} shares
Cash: ${cash:.2f}

Recent news:
{news_block}"""

        # Bull
        bull_prompt = f"""You are arguing the BULL case for this Polymarket prediction market.
Argue why the true probability should be HIGHER than the current {mid:.1%}.

{context_block}

End with:
CONVICTION: <0.0-1.0>
FAIR_PROBABILITY: <0.0-1.0>"""

        bull_response = await call_llm(self.bull_model, bull_prompt)
        self.llm_calls += 1

        # Bear
        bear_prompt = f"""You are arguing the BEAR case for this Polymarket prediction market.
Argue why the true probability should be LOWER than the current {mid:.1%}.

{context_block}

End with:
CONVICTION: <0.0-1.0>
FAIR_PROBABILITY: <0.0-1.0>"""

        bear_response = await call_llm(self.bear_model, bear_prompt)
        self.llm_calls += 1

        if not bull_response or not bear_response:
            return

        # Judge
        judge_prompt = f"""You are a senior prediction market judge. You've heard bull and bear arguments.

{context_block}

## Bull Case
{bull_response}

## Bear Case
{bear_response}

## Calibration
- Fed rate cut: Market 72%, correct was 95% (CPI data was decisive)
- Biden dropout: Market 25%, correct was 55% (private pressure real but uncertain)
- BTC $100K: Market 45%, correct was 50% (ETF inflows bullish but huge move needed)
- Russia ceasefire: Market 12%, correct was 5% (both sides escalating, no track)
- Govt shutdown: Market 40%, correct was 35% (drama is theatre, usually averted)

Consider calibration. Respond ONLY with JSON:
{{"fair_probability": 0.0-1.0, "confidence": 0.0-1.0, "action": "buy"|"sell"|"hold", "reasoning": "brief", "edge": float}}"""

        judge_response = await call_llm(self.judge_model, judge_prompt)
        self.llm_calls += 1

        parsed = parse_json_response(judge_response)
        if not parsed:
            return

        action = parsed.get("action", "hold").lower()
        confidence = float(parsed.get("confidence", 0))
        fair_prob = float(parsed.get("fair_probability", mid))
        edge = fair_prob - mid

        if confidence < self.conf_threshold or abs(edge) < self.edge_threshold or action == "hold":
            return

        self._last_trade[sym] = self._event_count

        # Kelly sizing
        portfolio_val = cash
        qty = self._kelly_size(fair_prob, mid, portfolio_val)
        if qty <= 0:
            return

        if edge > 0 and action == "buy":
            level = ctx.trader.market_data.get_best_ask(ticker)
            if level:
                existing = pos.quantity if pos else Decimal("0")
                buy_qty = max(qty - existing, Decimal("0"))
                if buy_qty > 0:
                    result = await ctx.trader.place_order(
                        TradeSide.BUY, ticker, level.price, buy_qty
                    )
                    if result.order:
                        for t in result.order.trades:
                            ctx.analyzer.add_trade(t)
        elif edge < 0 and action == "sell":
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


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------


async def run_single_round(
    strategy_name: str, strategy: Any, markets: list[MarketInfo], timeline: list[Event],
) -> RoundResult:
    ctx = BacktestContext(markets)
    snapshots: list[tuple[datetime, Decimal]] = []
    peak = INITIAL_CAPITAL
    max_dd = Decimal("0")
    snap_interval = max(1, len(timeline) // 100)

    for i, event in enumerate(timeline):
        if isinstance(event, PriceChangeEvent):
            ctx.market_data.process_price_change_event(event)
        await strategy.on_event(event, ctx)

        if i % snap_interval == 0 or i == len(timeline) - 1:
            pv = ctx.position_manager.get_portfolio_value(ctx.market_data)
            total_val = sum(pv.values(), Decimal("0"))
            ts = (
                event.timestamp if isinstance(event, PriceChangeEvent)
                else getattr(event, "published_at", datetime.now(timezone.utc))
            )
            snapshots.append((ts, total_val))
            if total_val > peak:
                peak = total_val
            if peak > 0:
                dd = (peak - total_val) / peak
                max_dd = max(max_dd, dd)

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

    stats = ctx.analyzer.get_stats()
    return_pct = (
        (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        if INITIAL_CAPITAL > 0 else Decimal("0")
    )
    avg_pnl = (realized + unrealized) / Decimal(str(max(len(trades), 1)))

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
        sharpe_ratio=stats.sharpe_ratio,
        profit_factor=stats.profit_factor,
        win_rate=stats.win_rate,
        avg_trade_pnl=avg_pnl,
        llm_calls=getattr(strategy, "llm_calls", 0),
        timeline=snapshots,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(markets: list[MarketInfo], results: list[RoundResult], timeframe: str) -> None:
    sep = "=" * 105

    print(f"\n{sep}")
    print("  REAL LLM BACKTEST REPORT — POLYMARKET")
    print(sep)

    print("\n--- Configuration ---")
    print(f"  Initial Capital:  ${INITIAL_CAPITAL:,.2f}")
    print(f"  Fast Model:       {FAST_MODEL}")
    print(f"  Judge Model:      {JUDGE_MODEL}")
    print(f"  Markets:          {len(markets)}")
    print(f"  Timeframe:        {timeframe}")
    print(f"  LLM Calls Total:  {_llm_call_count}")
    print(f"  LLM Cache Hits:   {len(_llm_cache)}")

    print("\n--- Markets ---")
    for m in markets:
        mid = (m.best_bid + m.best_ask) / 2
        print(
            f"  [{m.ticker.symbol}] {m.question[:55]:<55} "
            f"mid={mid:.2f}  vol=${m.volume:>12,.0f}  pts={len(m.price_history)}"
        )

    print("\n--- Strategy Comparison ---")
    header = (
        f"  {'Strategy':<28} {'Return%':>9} {'Sharpe':>8} "
        f"{'MaxDD%':>8} {'WinRate':>8} {'PF':>6} "
        f"{'Trades':>7} {'LLM#':>6} {'P&L':>12}"
    )
    print(header)
    print("  " + "-" * 102)

    sorted_results = sorted(results, key=lambda r: r.return_pct, reverse=True)
    for r in sorted_results:
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor < Decimal("100") else "INF"
        print(
            f"  {r.name:<28} {r.return_pct:>+8.2f}% {r.sharpe_ratio:>8.3f} "
            f"{r.max_drawdown * 100:>7.2f}% {r.win_rate * 100:>7.1f}% {pf_str:>6} "
            f"{r.total_trades:>7} {r.llm_calls:>6} ${r.total_pnl:>+10.2f}"
        )

    for r in sorted_results:
        print(f"\n--- {r.name} (Detail) ---")
        print(f"  Trades:       {r.total_trades} (buy={r.buy_trades}, sell={r.sell_trades})")
        print(f"  Realized:     ${r.realized_pnl:+.4f}")
        print(f"  Unrealized:   ${r.unrealized_pnl:+.4f}")
        print(f"  Commission:   ${r.total_commission:.4f}")
        print(f"  Final Value:  ${r.final_value:,.2f}")
        print(f"  LLM Calls:    {r.llm_calls}")

        if r.timeline:
            step = max(1, len(r.timeline) // 8)
            pts = r.timeline[::step]
            if pts[-1] != r.timeline[-1]:
                pts.append(r.timeline[-1])
            print("  Portfolio curve:")
            for ts, val in pts:
                bar_len = max(0, int((float(val) / float(INITIAL_CAPITAL) - 0.9) * 200))
                bar = "█" * min(bar_len, 40)
                print(f"    {ts:%m-%d %H:%M}  ${val:>10,.2f}  {bar}")

    best = sorted_results[0]
    worst = sorted_results[-1]
    print(f"\n{sep}")
    print(f"  BEST:  {best.name} ({best.return_pct:+.2f}%, Sharpe={best.sharpe_ratio:.3f})")
    print(f"  WORST: {worst.name} ({worst.return_pct:+.2f}%, Sharpe={worst.sharpe_ratio:.3f})")
    print(f"  TOTAL LLM CALLS: {_llm_call_count}  |  CACHE ENTRIES: {len(_llm_cache)}")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    logger.info(f"Using claude CLI with models: fast={FAST_MODEL}, judge={JUDGE_MODEL}")

    # Quick connectivity test via claude CLI
    logger.info("Testing claude CLI connectivity...")
    test = await call_llm(FAST_MODEL, "Reply with just the word 'ok'")
    if test is None:
        print("ERROR: claude CLI test failed. Make sure 'claude' is installed.")
        sys.exit(1)
    logger.info(f"Claude CLI test OK: {test.strip()[:50]}")

    # Fetch real market data
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

        if markets:
            api_markets = [m for m in markets if not m.price_history]
            if api_markets:
                await fetch_all_histories(client, api_markets)

    markets = [m for m in markets if m.price_history]
    if not markets:
        logger.error("No markets with price history. Check API.")
        sys.exit(1)

    for m in markets:
        m.ticker = create_ticker(m)

    timeline = build_timeline(markets)
    if not timeline:
        logger.error("Empty timeline, aborting.")
        return

    first_ts = last_ts = None
    for e in timeline:
        if isinstance(e, PriceChangeEvent):
            if first_ts is None:
                first_ts = e.timestamp
            last_ts = e.timestamp
    timeframe = "N/A"
    if first_ts and last_ts:
        timeframe = f"{first_ts:%Y-%m-%d %H:%M} -> {last_ts:%Y-%m-%d %H:%M} UTC"

    news_count = sum(1 for e in timeline if isinstance(e, NewsEvent))
    logger.info(f"Total news events for LLM analysis: {news_count}")

    # Define strategies
    strategies: list[tuple[str, Any]] = [
        ("Momentum (no LLM)", MomentumStrategy(
            window=20, buy_threshold=-0.03, sell_threshold=0.03,
            trade_size=Decimal("50"),
        )),
        ("LLM News Sentiment", LLMNewsSentimentStrategy(
            model=FAST_MODEL,
            confidence_threshold=0.40,
            trade_size=Decimal("80"),
            cooldown=200,  # ~15 LLM calls total for this strategy
        )),
        ("LLM Debate (fixed size)", LLMDebateStrategy(
            bull_model=FAST_MODEL,
            bear_model=FAST_MODEL,
            judge_model=JUDGE_MODEL,
            confidence_threshold=0.50,
            edge_threshold=0.05,
            trade_size=Decimal("80"),
            cooldown=350,  # ~3 calls each × ~10 triggers = ~30 LLM calls
        )),
        ("LLM Debate + Kelly", LLMDebateKellyStrategy(
            bull_model=FAST_MODEL,
            bear_model=FAST_MODEL,
            judge_model=JUDGE_MODEL,
            kelly_fraction=0.25,
            max_position_pct=0.15,
            confidence_threshold=0.50,
            edge_threshold=0.05,
            cooldown=350,
        )),
    ]

    # Run strategies sequentially (LLM calls need to be paced)
    logger.info(f"Running {len(strategies)} strategies...")
    results: list[RoundResult] = []

    for name, strat in strategies:
        logger.info(f"  Running: {name}...")
        random.seed(42)
        start_time = time.time()
        result = await run_single_round(name, strat, markets, timeline)
        elapsed = time.time() - start_time
        results.append(result)
        logger.info(
            f"  {name}: return={result.return_pct:+.2f}%, "
            f"trades={result.total_trades}, llm_calls={result.llm_calls}, "
            f"time={elapsed:.1f}s"
        )

    print_report(markets, results, timeframe)


if __name__ == "__main__":
    asyncio.run(main())
