#!/usr/bin/env python3
"""
Custom Strategy Example

This example demonstrates how to create custom trading strategies with
the Oracle3 framework. It shows how to:
1. Implement the Strategy interface
2. Handle different event types
3. Make trading decisions based on events
4. Manage position sizing
"""

import asyncio
from decimal import Decimal

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import (
    Event,
    NewsEvent,
    OrderBookEvent,
    PriceChangeEvent,
)
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.strategy.strategy import Strategy
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class MomentumStrategy(Strategy):
    """
    A simple momentum strategy that:
    - Buys when price increases by more than a threshold
    - Sells when price decreases by more than a threshold
    - Uses fixed position sizing with a percentage of capital
    """

    def __init__(
        self,
        price_threshold: Decimal = Decimal('0.02'),  # 2% price change
        position_size_pct: Decimal = Decimal('0.10'),  # 10% of capital per trade
    ):
        self.price_threshold = price_threshold
        self.position_size_pct = position_size_pct
        self.last_prices: dict[str, Decimal] = {}

    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process incoming events and make trading decisions."""

        if isinstance(event, PriceChangeEvent):
            await self._handle_price_change(event, trader)

        elif isinstance(event, NewsEvent):
            # Could implement news sentiment analysis here
            print(
                f'News received: {event.title[:50] if event.title else "No title"}...'
            )

        elif isinstance(event, OrderBookEvent):
            # Could implement order flow analysis here
            pass

    async def _handle_price_change(
        self, event: PriceChangeEvent, trader: Trader
    ) -> None:
        """Handle price change events with momentum logic."""
        ticker = event.ticker
        current_price = event.price
        symbol = ticker.symbol

        # Get last price
        if symbol not in self.last_prices:
            self.last_prices[symbol] = current_price
            print(f'Initial price for {symbol}: {current_price}')
            return

        last_price = self.last_prices[symbol]
        price_change = (current_price - last_price) / last_price

        print(f'{symbol}: {last_price} -> {current_price} ({price_change * 100:.2f}%)')

        # Check for momentum signal
        if abs(price_change) >= self.price_threshold:
            cash_position = trader.position_manager.get_position(ticker.collateral)
            if cash_position is None:
                return

            # Calculate position size
            trade_value = cash_position.quantity * self.position_size_pct
            quantity = trade_value / current_price

            if price_change > 0:
                # Price increased -> buy
                print(f'BUY signal: Price up {price_change * 100:.2f}%')
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=current_price + Decimal('0.01'),
                    quantity=quantity,
                )
                if result.order:
                    print(
                        f'  Order filled: {result.order.filled_quantity} @ {result.order.average_price}'
                    )
                else:
                    print(f'  Order failed: {result.failure_reason}')

            else:
                # Price decreased -> sell if we have a position
                position = trader.position_manager.get_position(ticker)
                if position and position.quantity > 0:
                    print(f'SELL signal: Price down {price_change * 100:.2f}%')
                    sell_quantity = min(quantity, position.quantity)
                    result = await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=ticker,
                        limit_price=current_price - Decimal('0.01'),
                        quantity=sell_quantity,
                    )
                    if result.order:
                        print(
                            f'  Order filled: {result.order.filled_quantity} @ {result.order.average_price}'
                        )
                    else:
                        print(f'  Order failed: {result.failure_reason}')

        # Update last price
        self.last_prices[symbol] = current_price


class MeanReversionStrategy(Strategy):
    """
    A mean reversion strategy that:
    - Maintains a rolling average of prices
    - Buys when price drops below average by a threshold
    - Sells when price rises above average by a threshold
    """

    def __init__(
        self,
        window_size: int = 10,
        deviation_threshold: Decimal = Decimal('0.03'),
        position_size: Decimal = Decimal('100'),
    ):
        self.window_size = window_size
        self.deviation_threshold = deviation_threshold
        self.position_size = position_size
        self.price_history: dict[str, list] = {}

    def _calculate_moving_average(self, prices: list) -> Decimal:
        """Calculate simple moving average."""
        if not prices:
            return Decimal('0')
        return sum(prices) / len(prices)

    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process incoming events."""
        if isinstance(event, PriceChangeEvent):
            await self._handle_price_change(event, trader)

    async def _handle_price_change(
        self, event: PriceChangeEvent, trader: Trader
    ) -> None:
        """Handle price change with mean reversion logic."""
        ticker = event.ticker
        current_price = event.price
        symbol = ticker.symbol

        # Initialize price history
        if symbol not in self.price_history:
            self.price_history[symbol] = []

        # Add current price to history
        self.price_history[symbol].append(current_price)

        # Keep only the window size
        if len(self.price_history[symbol]) > self.window_size:
            self.price_history[symbol] = self.price_history[symbol][-self.window_size :]

        # Need at least window_size prices to trade
        if len(self.price_history[symbol]) < self.window_size:
            print(
                f'Building history for {symbol}: {len(self.price_history[symbol])}/{self.window_size}'
            )
            return

        # Calculate moving average
        ma = self._calculate_moving_average(self.price_history[symbol])
        deviation = (current_price - ma) / ma

        print(
            f'{symbol}: Price={current_price}, MA={ma:.4f}, Deviation={deviation * 100:.2f}%'
        )

        # Check for mean reversion signals
        if deviation < -self.deviation_threshold:
            # Price below average -> buy
            print(f'BUY signal: Price {abs(deviation) * 100:.2f}% below MA')
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=ticker,
                limit_price=current_price + Decimal('0.01'),
                quantity=self.position_size,
            )
            if result.order:
                print(f'  Bought {result.order.filled_quantity}')

        elif deviation > self.deviation_threshold:
            # Price above average -> sell if we have position
            position = trader.position_manager.get_position(ticker)
            if position and position.quantity > 0:
                print(f'SELL signal: Price {deviation * 100:.2f}% above MA')
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=ticker,
                    limit_price=current_price - Decimal('0.01'),
                    quantity=min(self.position_size, position.quantity),
                )
                if result.order:
                    print(f'  Sold {result.order.filled_quantity}')


class NewsKeywordStrategy(Strategy):
    """
    A strategy that trades based on keywords in news events.
    - Buys when bullish keywords are detected
    - Sells when bearish keywords are detected
    """

    BULLISH_KEYWORDS = {
        'surge',
        'rally',
        'gain',
        'profit',
        'growth',
        'bullish',
        'breakthrough',
        'success',
        'positive',
        'optimistic',
        'record high',
    }

    BEARISH_KEYWORDS = {
        'crash',
        'plunge',
        'drop',
        'loss',
        'decline',
        'bearish',
        'fail',
        'negative',
        'pessimistic',
        'record low',
        'crisis',
    }

    def __init__(self, position_size: Decimal = Decimal('100')):
        self.position_size = position_size
        self.last_signal: str = ''

    def _analyze_sentiment(self, text: str) -> str:
        """Analyze text for bullish/bearish sentiment."""
        text_lower = text.lower()

        bullish_count = sum(1 for kw in self.BULLISH_KEYWORDS if kw in text_lower)
        bearish_count = sum(1 for kw in self.BEARISH_KEYWORDS if kw in text_lower)

        if bullish_count > bearish_count:
            return 'bullish'
        elif bearish_count > bullish_count:
            return 'bearish'
        return 'neutral'

    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process incoming events."""
        if isinstance(event, NewsEvent):
            await self._handle_news(event, trader)

    async def _handle_news(self, event: NewsEvent, trader: Trader) -> None:
        """Handle news events with keyword analysis."""
        # Combine title and description for analysis
        text = f'{event.title} {event.description}'
        sentiment = self._analyze_sentiment(text)

        print(f'News: {event.title[:50]}... -> Sentiment: {sentiment}')

        # Only trade if we have a ticker and sentiment changed
        if event.ticker and sentiment != 'neutral' and sentiment != self.last_signal:
            ticker = event.ticker
            current_price = Decimal('0.50')  # Would get from market data

            if sentiment == 'bullish':
                print('  -> BUY signal')
                await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=current_price + Decimal('0.02'),
                    quantity=self.position_size,
                )

            elif sentiment == 'bearish':
                position = trader.position_manager.get_position(ticker)
                if position and position.quantity > 0:
                    print('  -> SELL signal')
                    await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=ticker,
                        limit_price=current_price - Decimal('0.02'),
                        quantity=min(self.position_size, position.quantity),
                    )

            self.last_signal = sentiment


def create_test_environment():
    """Create a test trading environment."""
    ticker = PolyMarketTicker(
        symbol='TEST_MARKET',
        name='Test Market',
        token_id='test123',
        market_id='market123',
        event_id='event123',
    )

    market_data = MarketDataManager()
    order_book = OrderBook()
    order_book.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('10000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('10000'))],
    )
    market_data.order_books[ticker] = order_book

    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0'),
    )

    return ticker, trader


async def test_momentum_strategy():
    """Test the momentum strategy."""
    print('=' * 60)
    print('Testing Momentum Strategy')
    print('=' * 60)

    ticker, trader = create_test_environment()
    strategy = MomentumStrategy(
        price_threshold=Decimal('0.02'),
        position_size_pct=Decimal('0.10'),
    )

    # Simulate price changes
    prices = [
        Decimal('0.50'),
        Decimal('0.51'),  # +2% -> buy
        Decimal('0.52'),
        Decimal('0.50'),  # -3.8% -> sell
        Decimal('0.48'),
        Decimal('0.51'),  # +6.25% -> buy
    ]

    for price in prices:
        event = PriceChangeEvent(ticker=ticker, price=price)
        await strategy.process_event(event, trader)

    print('\nFinal positions:')
    print(f'  Cash: {trader.position_manager.get_cash_positions()}')
    print(f'  Holdings: {trader.position_manager.get_non_cash_positions()}')


async def test_mean_reversion_strategy():
    """Test the mean reversion strategy."""
    print('\n' + '=' * 60)
    print('Testing Mean Reversion Strategy')
    print('=' * 60)

    ticker, trader = create_test_environment()
    strategy = MeanReversionStrategy(
        window_size=5,
        deviation_threshold=Decimal('0.02'),
        position_size=Decimal('100'),
    )

    # Simulate price changes
    prices = [
        Decimal('0.50'),
        Decimal('0.51'),
        Decimal('0.49'),
        Decimal('0.50'),
        Decimal('0.51'),  # MA now established
        Decimal('0.45'),  # Below MA -> buy
        Decimal('0.55'),  # Above MA -> sell
    ]

    for price in prices:
        event = PriceChangeEvent(ticker=ticker, price=price)
        await strategy.process_event(event, trader)

    print('\nFinal positions:')
    print(f'  Cash: {trader.position_manager.get_cash_positions()}')
    print(f'  Holdings: {trader.position_manager.get_non_cash_positions()}')


async def main():
    """Run all strategy examples."""
    print('Oracle3 - Custom Strategy Examples\n')
    print('This example demonstrates how to create custom trading strategies.\n')

    await test_momentum_strategy()
    await test_mean_reversion_strategy()

    print('\n' + '=' * 60)
    print('All examples completed!')
    print('=' * 60)


if __name__ == '__main__':
    asyncio.run(main())
