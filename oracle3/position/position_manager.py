from dataclasses import dataclass
from decimal import Decimal

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.ticker.ticker import CashTicker, Ticker
from oracle3.trader.types import Trade, TradeSide


@dataclass
class Position:
    ticker: Ticker
    quantity: Decimal
    average_cost: Decimal
    realized_pnl: Decimal


class PositionManager:
    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}

    def update_position(self, position: Position) -> None:
        """Update a position"""
        self.positions[position.ticker.symbol] = position

    def apply_trade(self, trade: Trade) -> Position:
        """Update positions based on a trade
        Args:
            trade: The trade to apply
        Returns:
            The updated position for the ticker being traded
        """
        ticker = trade.ticker
        quantity = trade.quantity if trade.side == TradeSide.BUY else -trade.quantity
        price = trade.price
        collateral = ticker.collateral
        commission = trade.commission

        if ticker.symbol not in self.positions:
            self.positions[ticker.symbol] = Position(
                ticker,
                quantity=Decimal('0'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )

        pos = self.positions[ticker.symbol]
        current_quantity = pos.quantity
        new_quantity = current_quantity + quantity

        if quantity < 0:
            # Calculate realized PnL for selling
            sell_quantity = abs(quantity)
            if sell_quantity > current_quantity:
                raise ValueError(
                    f'Cannot sell {sell_quantity} {ticker}, exceeding the current position {current_quantity}'
                )

            realized_pnl = (price - pos.average_cost) * sell_quantity - commission
            pos.realized_pnl += realized_pnl

        if new_quantity == 0:
            pos.average_cost = Decimal('0')
        elif new_quantity > 0 and quantity > 0:
            # Update cost basis for buying
            total_cost = (
                (current_quantity * pos.average_cost) + (quantity * price) + commission
            )
            pos.average_cost = total_cost / new_quantity

        pos.quantity = new_quantity

        # Update the corresponding collateral position
        self.positions[collateral.symbol].quantity -= price * quantity + commission

        return pos

    def get_position(self, ticker: Ticker) -> Position | None:
        """Get current position for a ticker"""
        return self.positions.get(ticker.symbol)

    def get_unrealized_pnl(
        self, ticker: Ticker, market_data: MarketDataManager
    ) -> Decimal:
        """Calculate unrealized PnL for a ticker"""
        if ticker.symbol not in self.positions:
            return Decimal('0')

        if isinstance(ticker, CashTicker):
            return Decimal('0')

        position = self.positions[ticker.symbol]
        if position.quantity <= 0:
            return Decimal('0')

        # Try bid first, fallback to ask
        current_price = Decimal('0')
        best_bid = market_data.get_best_bid(ticker)
        if best_bid is not None:
            current_price = best_bid.price
        else:
            best_ask = market_data.get_best_ask(ticker)
            if best_ask is not None:
                current_price = best_ask.price

        if current_price <= 0:
            return Decimal('0')

        unrealized_pnl = (current_price - position.average_cost) * position.quantity
        return unrealized_pnl

    def get_realized_pnl(self, ticker: Ticker) -> Decimal:
        """Get realized PnL for a ticker"""
        if ticker.symbol not in self.positions:
            return Decimal('0')
        return self.positions[ticker.symbol].realized_pnl

    def get_pnl(self, ticker: Ticker, market_data: MarketDataManager) -> Decimal:
        """Get PnL for a ticker"""
        return self.get_realized_pnl(ticker) + self.get_unrealized_pnl(
            ticker, market_data
        )

    def get_total_realized_pnl(self) -> Decimal:
        """Get total realized PnL across all positions"""
        return sum(
            (pos.realized_pnl for pos in self.positions.values()), Decimal('0.0')
        )

    def get_total_unrealized_pnl(self, market_data: MarketDataManager) -> Decimal:
        """Get total unrealized PnL across all positions"""
        return sum(
            (
                self.get_unrealized_pnl(pos.ticker, market_data)
                for pos in self.positions.values()
            ),
            Decimal('0.0'),
        )

    def get_total_pnl(self, market_data: MarketDataManager) -> Decimal:
        """Get total PnL across all positions"""
        return self.get_total_realized_pnl() + self.get_total_unrealized_pnl(
            market_data
        )

    def get_cash_positions(self) -> list[Position]:
        """Get all cash positions"""
        return [
            pos for pos in self.positions.values() if isinstance(pos.ticker, CashTicker)
        ]

    def get_non_cash_positions(self) -> list[Position]:
        """Get all non-cash positions"""
        return [
            pos
            for pos in self.positions.values()
            if not isinstance(pos.ticker, CashTicker)
        ]

    def get_portfolio_value(self, market_data: MarketDataManager) -> dict[str, Decimal]:
        """Get portfolio value by collateral currencies

        Returns:
            A dictionary where the keys are CashTicker symbols and
            the values are the total portfolio value in that collateral currency
        """
        portfolio_value: dict[str, Decimal] = {}

        for cash_pos in self.get_cash_positions():
            portfolio_value[cash_pos.ticker.symbol] = cash_pos.quantity

        for pos in self.get_non_cash_positions():
            if pos.quantity <= 0:
                continue
            collateral = pos.ticker.collateral

            best_bid = market_data.get_best_bid(pos.ticker)
            current_price = best_bid.price if best_bid else None
            if current_price is None:
                best_ask = market_data.get_best_ask(pos.ticker)
                current_price = best_ask.price if best_ask else None
            if current_price is None:
                continue
            position_value = pos.quantity * current_price

            portfolio_value[collateral.symbol] = (
                portfolio_value.get(collateral.symbol, Decimal('0')) + position_value
            )

        return portfolio_value
