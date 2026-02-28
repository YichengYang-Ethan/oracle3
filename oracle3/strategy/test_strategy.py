from decimal import Decimal

from oracle3.events.events import Event, PriceChangeEvent
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

from .quant_strategy import QuantStrategy


class TestStrategy(QuantStrategy):
    name = 'test'
    version = '0.1.0'
    author = 'oracle3'

    def __init__(self):
        super().__init__()
        self.last_prices = {}
        self.fixed_quantity = Decimal('100')

    async def process_event(self, event: Event, trader: Trader) -> None:
        if isinstance(event, PriceChangeEvent):
            ticker = event.ticker
            current_price = event.price

            if ticker in self.last_prices:
                last_price = self.last_prices[ticker]

                # Buy if price went up
                if current_price > last_price:
                    print(
                        f'{ticker} price increased from {last_price} to {current_price}. Buying {self.fixed_quantity}.'
                    )
                    print(
                        await trader.place_order(
                            side=TradeSide.BUY,
                            ticker=ticker,
                            limit_price=current_price + Decimal('0.01'),
                            quantity=self.fixed_quantity,
                        )
                    )

                # Sell if price went down
                elif current_price < last_price:
                    print(
                        f'{ticker} price decreased from {last_price} to {current_price}. Selling {self.fixed_quantity}.'
                    )
                    print(
                        await trader.place_order(
                            side=TradeSide.SELL,
                            ticker=ticker,
                            limit_price=current_price - Decimal('0.01'),
                            quantity=self.fixed_quantity,
                        )
                    )

            self.last_prices[ticker] = current_price

        print(event)

        cash_positions = trader.position_manager.get_cash_positions()
        print(f'Current cash: {cash_positions}')

        positions = trader.position_manager.get_non_cash_positions()
        print(f'Current positions: {positions}')

        print(trader.market_data.order_books)
