from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from oracle3.trader.types import Trade, TradeSide


@dataclass
class TradeStats:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    average_profit: Decimal
    average_loss: Decimal
    max_drawdown: Decimal
    sharpe_ratio: Decimal
    profit_factor: Decimal = Decimal('0')
    total_pnl: Decimal = Decimal('0')
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0


@dataclass
class EquityPoint:
    """Represents a point in the equity curve."""

    timestamp: int
    equity: Decimal
    trade_index: int


class PerformanceAnalyzer:
    def __init__(self, initial_capital: Decimal = Decimal('10000')) -> None:
        self.trades: list[Trade] = []
        self.initial_capital = initial_capital
        self.equity_curve: list[EquityPoint] = []
        self._stats: TradeStats | None = None
        self._pnl_per_trade: list[Decimal] = []

        # Initialize equity curve with starting capital
        self.equity_curve.append(
            EquityPoint(timestamp=0, equity=initial_capital, trade_index=-1)
        )

    def add_trade(self, trade: Trade) -> None:
        """Add a trade and update metrics."""
        self.trades.append(trade)
        self._update_metrics()

    def _calculate_trade_pnl(self, trade: Trade) -> Decimal:
        """
        Calculate PnL for a single trade.
        For simplicity, we track the cost basis and assume selling realizes PnL.
        """
        # For buy trades, we're spending money (negative cash flow)
        # For sell trades, we're receiving money (positive cash flow)
        if trade.side == TradeSide.BUY:
            return -(trade.price * trade.quantity + trade.commission)
        else:
            return trade.price * trade.quantity - trade.commission

    def _update_metrics(self) -> None:
        """Update all performance metrics based on current trades."""
        if not self.trades:
            self._stats = TradeStats(
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=Decimal('0'),
                average_profit=Decimal('0'),
                average_loss=Decimal('0'),
                max_drawdown=Decimal('0'),
                sharpe_ratio=Decimal('0'),
                profit_factor=Decimal('0'),
                total_pnl=Decimal('0'),
                max_consecutive_wins=0,
                max_consecutive_losses=0,
            )
            return

        # Calculate PnL for each trade
        self._pnl_per_trade = [self._calculate_trade_pnl(t) for t in self.trades]

        # Build equity curve
        current_equity = self.initial_capital
        self.equity_curve = [
            EquityPoint(timestamp=0, equity=self.initial_capital, trade_index=-1)
        ]

        for i, pnl in enumerate(self._pnl_per_trade):
            current_equity += pnl
            self.equity_curve.append(
                EquityPoint(timestamp=i + 1, equity=current_equity, trade_index=i)
            )

        # Separate winning and losing trades
        winning_pnls = [pnl for pnl in self._pnl_per_trade if pnl > 0]
        losing_pnls = [pnl for pnl in self._pnl_per_trade if pnl < 0]

        total_trades = len(self.trades)
        winning_trades = len(winning_pnls)
        losing_trades = len(losing_pnls)

        # Win rate
        win_rate = (
            Decimal(winning_trades) / Decimal(total_trades)
            if total_trades > 0
            else Decimal('0')
        )

        # Average profit and loss
        average_profit = (
            sum(winning_pnls) / Decimal(winning_trades)
            if winning_trades > 0
            else Decimal('0')
        )
        average_loss = (
            sum(losing_pnls) / Decimal(losing_trades)
            if losing_trades > 0
            else Decimal('0')
        )

        # Total PnL
        total_pnl = sum(self._pnl_per_trade)

        # Profit factor (gross profit / gross loss)
        gross_profit = sum(winning_pnls) if winning_pnls else Decimal('0')
        gross_loss = abs(sum(losing_pnls)) if losing_pnls else Decimal('0')
        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else Decimal('999.99')
            if gross_profit > 0
            else Decimal('0')
        )

        # Max drawdown
        max_drawdown = self._calculate_max_drawdown()

        # Sharpe ratio
        sharpe_ratio = self._calculate_sharpe_ratio()

        # Consecutive wins/losses
        max_consecutive_wins, max_consecutive_losses = (
            self._calculate_consecutive_streaks()
        )

        self._stats = TradeStats(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            average_profit=average_profit,
            average_loss=average_loss,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            profit_factor=profit_factor,
            total_pnl=total_pnl,
            max_consecutive_wins=max_consecutive_wins,
            max_consecutive_losses=max_consecutive_losses,
        )

    def _calculate_max_drawdown(self) -> Decimal:
        """Calculate the maximum drawdown from the equity curve."""
        if len(self.equity_curve) < 2:
            return Decimal('0')

        peak = self.equity_curve[0].equity
        max_drawdown = Decimal('0')

        for point in self.equity_curve:
            if point.equity > peak:
                peak = point.equity

            drawdown = (peak - point.equity) / peak if peak > 0 else Decimal('0')
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        return max_drawdown

    def _calculate_sharpe_ratio(
        self, risk_free_rate: Decimal = Decimal('0.02')
    ) -> Decimal:
        """
        Calculate the Sharpe ratio.

        Args:
            risk_free_rate: Annual risk-free rate (default 2%)

        Returns:
            Sharpe ratio (annualized)
        """
        if len(self._pnl_per_trade) < 2:
            return Decimal('0')

        # Calculate returns
        returns = []
        for i, point in enumerate(self.equity_curve[1:], 1):
            prev_equity = self.equity_curve[i - 1].equity
            if prev_equity > 0:
                ret = (point.equity - prev_equity) / prev_equity
                returns.append(float(ret))

        if len(returns) < 2:
            return Decimal('0')

        # Calculate mean and standard deviation
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 0

        if std_dev == 0:
            return Decimal('0')

        # Annualize (assuming daily returns, 252 trading days)
        annualized_return = mean_return * 252
        annualized_std = std_dev * math.sqrt(252)

        sharpe = (annualized_return - float(risk_free_rate)) / annualized_std

        return Decimal(str(round(sharpe, 4)))

    def _calculate_consecutive_streaks(self) -> tuple[int, int]:
        """Calculate max consecutive wins and losses."""
        if not self._pnl_per_trade:
            return 0, 0

        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0

        for pnl in self._pnl_per_trade:
            if pnl > 0:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            elif pnl < 0:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)
            else:
                # Break-even trade
                current_wins = 0
                current_losses = 0

        return max_wins, max_losses

    def get_stats(self) -> TradeStats:
        """Get current performance statistics."""
        if self._stats is None:
            self._update_metrics()
        return self._stats

    def get_equity_curve(self) -> list[EquityPoint]:
        """Get the equity curve."""
        return self.equity_curve.copy()

    def get_current_equity(self) -> Decimal:
        """Get the current equity value."""
        if self.equity_curve:
            return self.equity_curve[-1].equity
        return self.initial_capital

    def get_return_pct(self) -> Decimal:
        """Get the total return percentage."""
        current = self.get_current_equity()
        return ((current - self.initial_capital) / self.initial_capital) * Decimal(
            '100'
        )

    def print_summary(self) -> None:
        """Print a summary of performance metrics."""
        stats = self.get_stats()

        print('\n' + '=' * 50)
        print('PERFORMANCE SUMMARY')
        print('=' * 50)
        print(f'Initial Capital:      ${self.initial_capital:,.2f}')
        print(f'Current Equity:       ${self.get_current_equity():,.2f}')
        print(f'Total Return:         {self.get_return_pct():.2f}%')
        print('-' * 50)
        print(f'Total Trades:         {stats.total_trades}')
        print(f'Winning Trades:       {stats.winning_trades}')
        print(f'Losing Trades:        {stats.losing_trades}')
        print(f'Win Rate:             {stats.win_rate * 100:.2f}%')
        print('-' * 50)
        print(f'Total PnL:            ${stats.total_pnl:,.2f}')
        print(f'Average Profit:       ${stats.average_profit:,.2f}')
        print(f'Average Loss:         ${stats.average_loss:,.2f}')
        print(f'Profit Factor:        {stats.profit_factor:.2f}')
        print('-' * 50)
        print(f'Max Drawdown:         {stats.max_drawdown * 100:.2f}%')
        print(f'Sharpe Ratio:         {stats.sharpe_ratio:.4f}')
        print(f'Max Consecutive Wins: {stats.max_consecutive_wins}')
        print(f'Max Consecutive Losses: {stats.max_consecutive_losses}')
        print('=' * 50 + '\n')

    def reset(self) -> None:
        """Reset the analyzer to initial state."""
        self.trades = []
        self.equity_curve = [
            EquityPoint(timestamp=0, equity=self.initial_capital, trade_index=-1)
        ]
        self._stats = None
        self._pnl_per_trade = []

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize trades, initial_capital, and equity_curve to a JSON-safe dict."""
        from oracle3.storage.serializers import (
            serialize_equity_point,
            serialize_trade,
        )

        return {
            'initial_capital': str(self.initial_capital),
            'trades': [serialize_trade(t) for t in self.trades],
            'equity_curve': [serialize_equity_point(pt) for pt in self.equity_curve],
        }

    @classmethod
    def from_dict(cls, d: dict) -> PerformanceAnalyzer:
        """Reconstruct a PerformanceAnalyzer from a previously serialized dict."""
        from oracle3.storage.serializers import deserialize_trade

        analyzer = cls(initial_capital=Decimal(d['initial_capital']))
        for trade_data in d.get('trades', []):
            analyzer.add_trade(deserialize_trade(trade_data))
        return analyzer
