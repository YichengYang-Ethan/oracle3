"""Trading monitor CLI command."""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

import click
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from oracle3.position.position_manager import PositionManager
from oracle3.ticker.ticker import CashTicker
from oracle3.trader.trader import Trader
from oracle3.trader.types import OrderStatus


class TradingMonitor:
    """Monitor for displaying trading engine state."""

    def __init__(
        self,
        trader: Trader,
        position_manager: PositionManager,
        exchange_name: str = '',
    ) -> None:
        self.trader = trader
        self.position_manager = position_manager
        self.exchange_name = exchange_name
        self.console = Console()
        self.start_time = datetime.now()

        # Attributes synced from engine by MonitoredTradingEngine._sync_data()
        self.decisions: list = []
        self.decision_stats: dict[str, int | float] = {}
        self.activity_log: list[tuple[str, str]] = []
        self.news_headlines: list[dict[str, str] | tuple[str, str]] = []
        self.event_count: int = 0
        self.news_buffer_count: int = 0
        self.perf_stats = None
        self.ob_count: int = 0

        # Keyboard interaction state
        self.PANELS: list[str] = ['decisions', 'orderbooks', 'news', 'activity_log']
        self.focused_panel: str = 'decisions'
        self.scroll_offsets: dict[str, int] = dict.fromkeys(self.PANELS, 0)
        self.paused: bool = False

    def _next_panel(self) -> None:
        """Cycle focus to the next panel."""
        idx = self.PANELS.index(self.focused_panel)
        self.focused_panel = self.PANELS[(idx + 1) % len(self.PANELS)]
        self.scroll_offsets[self.focused_panel] = 0  # Reset scroll on switch

    def _prev_panel(self) -> None:
        """Cycle focus to the previous panel."""
        idx = self.PANELS.index(self.focused_panel)
        self.focused_panel = self.PANELS[(idx - 1) % len(self.PANELS)]
        self.scroll_offsets[self.focused_panel] = 0

    def _scroll(self, delta: int) -> None:
        """Scroll the currently focused panel."""
        key = self.focused_panel
        self.scroll_offsets[key] = max(0, self.scroll_offsets.get(key, 0) + delta)

    def _format_decimal(self, value: Decimal, decimals: int = 4) -> str:
        """Format decimal with specified precision."""
        return f'{value:.{decimals}f}'

    def _format_pnl(self, value: Decimal) -> Text:
        """Format P&L with color coding."""
        formatted = self._format_decimal(value, 2)
        if value > 0:
            return Text(f'+${formatted}', style='green')
        elif value < 0:
            return Text(f'-${abs(value):.2f}', style='red')
        return Text(f'${formatted}', style='white')

    def _create_portfolio_summary(self) -> Panel:
        """Create portfolio summary panel."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column('Metric', style='cyan')
        table.add_column('Value', justify='right')

        try:
            # Snapshot positions dict to avoid RuntimeError during iteration
            md = self.trader.market_data
            portfolio_value = self.position_manager.get_portfolio_value(md)
            total_value = sum(portfolio_value.values(), Decimal('0'))
            table.add_row(
                'Total Portfolio Value', f'${self._format_decimal(total_value, 2)}'
            )

            cash_positions = self.position_manager.get_cash_positions()
            for cash_pos in cash_positions:
                table.add_row(
                    f'  {cash_pos.ticker.symbol}',
                    f'${self._format_decimal(cash_pos.quantity, 2)}',
                )

            realized_pnl = self.position_manager.get_total_realized_pnl()
            unrealized_pnl = self.position_manager.get_total_unrealized_pnl(md)
            total_pnl = realized_pnl + unrealized_pnl

            table.add_row('Realized P&L', self._format_pnl(realized_pnl))
            table.add_row('Unrealized P&L', self._format_pnl(unrealized_pnl))
            table.add_row('Total P&L', self._format_pnl(total_pnl))

            # Exposure metrics
            non_cash = self.position_manager.get_non_cash_positions()
            market_exposure = Decimal('0')
            for p in non_cash:
                if p.quantity > 0:
                    bid = md.get_best_bid(p.ticker)
                    if bid:
                        market_exposure += bid.price * p.quantity

            if total_value > 0:
                exposure_pct = float(market_exposure / total_value * 100)
                table.add_row(
                    'Exposure',
                    f'${self._format_decimal(market_exposure, 2)} ({exposure_pct:.1f}%)',
                )
            else:
                table.add_row('Exposure', '$0.00 (0.0%)')
        except (RuntimeError, KeyError, AttributeError):
            table.add_row('', Text('updating...', style='dim'))

        return Panel(table, title='[bold]Portfolio Summary[/bold]', border_style='blue')

    def _create_decisions_panel(
        self, limit: int = 12, scroll_offset: int = 0, is_focused: bool = False
    ) -> Panel:
        """Create strategy decisions panel with dynamic signal columns."""
        all_decisions: list = getattr(self, 'decisions', [])
        # Newest-first, scroll_offset=0 → latest, positive → scroll back
        end_idx = max(0, len(all_decisions) - scroll_offset)
        decisions = all_decisions[max(0, end_idx - limit) : end_idx]

        border_style = 'bold bright_white' if is_focused else 'bright_magenta'
        focus_tag = ' [●]' if is_focused else ''
        scroll_tag = f' ↑{scroll_offset}' if scroll_offset > 0 else ''

        if not decisions:
            return Panel(
                '[dim]Waiting for strategy decisions...[/dim]',
                title=f'[bold]Strategy Decisions{focus_tag}[/bold]',
                border_style=border_style,
            )

        table = Table(show_header=True, header_style='bold', expand=True)
        table.add_column('Time', style='dim', width=8)
        table.add_column('Action', width=8)
        signal_keys: list[str] = []
        for d in decisions:
            sig = getattr(d, 'signal_values', {}) or {}
            for k in sig:
                if k not in signal_keys:
                    signal_keys.append(k)
                if len(signal_keys) >= 3:
                    break
            if len(signal_keys) >= 3:
                break
        for key in signal_keys:
            table.add_column(key[:6], justify='right', width=7)
        table.add_column('Market', width=22)
        table.add_column('Reasoning', ratio=1)
        table.add_column('', width=3)

        for d in reversed(decisions):
            action_style = {
                'BUY_YES': 'bold green',
                'BUY_NO': 'bold red',
                'HOLD': 'dim',
                'CLOSE_EDGE_TP': 'bold yellow',
                'CLOSE_EDGE_REV': 'bold red',
                'CLOSE_REEVAL': 'bold magenta',
                'CLOSE_TIMEOUT': 'yellow',
            }.get(d.action, 'white')

            exec_text = (
                Text('✓', style='bold green') if d.executed else Text('—', style='dim')
            )
            reasoning = getattr(d, 'reasoning', '') or ''
            sig = getattr(d, 'signal_values', {}) or {}
            signal_cells: list[str | Text] = []
            for key in signal_keys:
                val = sig.get(key)
                if val is None:
                    signal_cells.append(Text('—', style='dim'))
                else:
                    signal_cells.append(f'{float(val):.3f}')

            table.add_row(
                d.timestamp,
                Text(d.action, style=action_style),
                *signal_cells,
                d.ticker_name[:22],
                Text(reasoning[:45], style='dim'),
                exec_text,
            )

        return Panel(
            table,
            title=f'[bold]Strategy Decisions{focus_tag}{scroll_tag}[/bold]',
            border_style=border_style,
        )

    def _create_activity_log_panel(
        self, limit: int = 12, scroll_offset: int = 0, is_focused: bool = False
    ) -> Panel:
        """Create scrolling activity log panel."""
        all_entries: list[tuple[str, str]] = getattr(self, 'activity_log', [])
        # Newest-first with scroll
        entries_rev = list(reversed(all_entries))
        log_entries = entries_rev[scroll_offset : scroll_offset + limit]

        border_style = 'bold bright_white' if is_focused else 'bright_cyan'
        focus_tag = ' [●]' if is_focused else ''
        scroll_tag = f' ↑{scroll_offset}' if scroll_offset > 0 else ''

        if not log_entries:
            return Panel(
                '[dim]Waiting for activity...[/dim]',
                title=f'[bold]Activity Log{focus_tag}[/bold]',
                border_style=border_style,
            )

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column('Time', style='dim', width=8)
        table.add_column('Event')

        for ts, msg in log_entries:
            # Color code based on content
            if 'BUY' in msg:
                style = 'green'
            elif 'SELL' in msg:
                style = 'red'
            elif 'Error' in msg:
                style = 'bold red'
            elif 'LLM' in msg:
                style = 'bright_magenta'
            elif 'News' in msg:
                style = 'cyan'
            else:
                style = 'white'

            table.add_row(ts, Text(msg[:80], style=style))

        return Panel(
            table,
            title=f'[bold]Activity Log{focus_tag}{scroll_tag}[/bold]',
            border_style=border_style,
        )

    def _create_stats_panel(self) -> Panel:
        """Create statistics panel."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column('Metric', style='cyan')
        table.add_column('Value', justify='right')

        # Runtime
        runtime = datetime.now() - self.start_time
        runtime_str = str(runtime).split('.')[0]
        table.add_row('Runtime', runtime_str)

        # Order statistics — snapshot the list to avoid RuntimeError
        try:
            orders = list(getattr(self.trader, 'orders', []))
        except RuntimeError:
            orders = []
        total_orders = len(orders)
        filled_orders = sum(1 for o in orders if o.status == OrderStatus.FILLED)
        rejected_orders = sum(1 for o in orders if o.status == OrderStatus.REJECTED)

        table.add_row('Total Orders', str(total_orders))
        table.add_row('Filled Orders', str(filled_orders))
        table.add_row('Rejected Orders', str(rejected_orders))

        if total_orders > 0:
            fill_rate = (filled_orders / total_orders) * 100
            table.add_row('Fill Rate', f'{fill_rate:.1f}%')

        # Position count (only positions with qty > 0)
        try:
            active_positions = sum(
                1
                for p in self.position_manager.get_non_cash_positions()
                if p.quantity > 0
            )
        except RuntimeError:
            active_positions = 0
        table.add_row('Active Positions', str(active_positions))

        decision_stats: dict[str, int | float] = getattr(self, 'decision_stats', {})
        for key, value in decision_stats.items():
            label = key.replace('_', ' ').title()
            table.add_row(f'Strategy {label}', str(value))

        # News buffer stats
        news_count = max(
            int(getattr(self, 'news_buffer_count', 0) or 0),
            len(getattr(self, 'news_headlines', [])),
        )
        table.add_row('  News Buffered', str(news_count))

        # Performance analyzer stats
        perf_stats = getattr(self, 'perf_stats', None)
        if perf_stats is not None and perf_stats.total_trades > 0:
            table.add_row('', '')  # separator
            table.add_row('[bold]Performance[/bold]', '')
            win_pct = f'{perf_stats.win_rate * 100:.1f}%'
            table.add_row(
                '  Win Rate',
                Text(
                    win_pct,
                    style='green' if perf_stats.win_rate > Decimal('0.5') else 'red',
                ),
            )
            table.add_row('  Profit Factor', f'{perf_stats.profit_factor:.2f}')
            table.add_row('  Sharpe Ratio', f'{perf_stats.sharpe_ratio:.2f}')
            dd_pct = f'{perf_stats.max_drawdown * 100:.1f}%'
            table.add_row(
                '  Max Drawdown',
                Text(
                    dd_pct,
                    style='red'
                    if perf_stats.max_drawdown > Decimal('0.05')
                    else 'white',
                ),
            )
            table.add_row('  Total PnL', self._format_pnl(perf_stats.total_pnl))
            table.add_row(
                '  W/L Streak',
                f'{perf_stats.max_consecutive_wins}W / {perf_stats.max_consecutive_losses}L',
            )

        return Panel(table, title='[bold]Statistics[/bold]', border_style='cyan')

    def _create_trading_panel(self, limit: int = 10) -> Panel:
        """Create a position-first panel with clear per-market exposure."""
        # Snapshot shared collections to avoid RuntimeError
        try:
            positions = [
                p
                for p in self.position_manager.get_non_cash_positions()
                if p.quantity > 0
            ]
        except RuntimeError:
            positions = []
        try:
            orders = list(getattr(self.trader, 'orders', []))[-limit:]
        except RuntimeError:
            orders = []

        pos_table = Table(show_header=True, header_style='bold', expand=True)
        pos_table.add_column('Market', ratio=2)
        pos_table.add_column('Qty', justify='right', width=8)
        pos_table.add_column('Avg', justify='right', width=8)
        pos_table.add_column('Mark', justify='right', width=8)
        pos_table.add_column('uPnL', justify='right', width=10)
        if positions:
            for pos in positions:
                md = self.trader.market_data
                best_bid = md.get_best_bid(pos.ticker)
                cur = best_bid.price if best_bid else Decimal('0')
                if cur <= 0:
                    best_ask = md.get_best_ask(pos.ticker)
                    cur = best_ask.price if best_ask else Decimal('0')
                pnl = (
                    (cur - pos.average_cost) * pos.quantity if cur > 0 else Decimal('0')
                )
                display_name = getattr(pos.ticker, 'name', '') or pos.ticker.symbol
                pos_table.add_row(
                    display_name[:34],
                    str(pos.quantity),
                    f'${pos.average_cost:.4f}',
                    f'${cur:.4f}' if cur > 0 else '—',
                    Text(f'${pnl:+.2f}', style='green' if pnl >= 0 else 'red')
                    if cur > 0
                    else Text('—', style='dim'),
                )
        else:
            pos_table.add_row('No active positions', '—', '—', '—', '—')

        orders_table = Table(show_header=True, header_style='bold', expand=True)
        orders_table.add_column('Recent Orders', ratio=2)
        orders_table.add_column('Px', justify='right', width=8)
        orders_table.add_column('Fill', justify='right', width=8)
        orders_table.add_column('St', justify='right', width=10)
        if orders:
            for order in reversed(orders[-5:]):
                side_style = 'green' if order.side.value == 'buy' else 'red'
                status_style = (
                    'green' if order.status == OrderStatus.FILLED else 'yellow'
                )
                order_name = getattr(order.ticker, 'name', '') or order.ticker.symbol
                orders_table.add_row(
                    Text(
                        f'{order.side.value.upper()} {order_name[:28]}',
                        style=side_style,
                    ),
                    f'${order.limit_price:.4f}',
                    str(order.filled_quantity),
                    Text(order.status.value.upper(), style=status_style),
                )
        else:
            orders_table.add_row('No recent orders', '—', '—', '—')

        return Panel(
            Group(pos_table, Text(''), orders_table),
            title='[bold]Positions By Market[/bold]',
            border_style='yellow',
        )

    def _create_orderbook_panel(
        self, limit: int = 8, scroll_offset: int = 0, is_focused: bool = False
    ) -> Panel:
        """Create order book panel showing top markets with bid/ask/spread."""
        try:
            # Snapshot to avoid RuntimeError
            ob_items = list(self.trader.market_data.order_books.items())
        except (RuntimeError, AttributeError):
            ob_items = []

        # Filter: skip cash tickers, empty books, and extreme probability markets
        active_books: list[tuple] = []
        for ticker, ob in ob_items:
            if isinstance(ticker, CashTicker):
                continue
            best_bid = ob.best_bid
            best_ask = ob.best_ask
            if not (best_bid and best_ask and best_bid.price > 0):
                continue
            mid = (best_bid.price + best_ask.price) / 2
            # Skip extreme markets (< 5% or > 95%) — uninteresting
            if mid < Decimal('0.05') or mid > Decimal('0.95'):
                continue
            spread = best_ask.price - best_bid.price
            active_books.append((ticker, best_bid, best_ask, spread, mid))

        border_style = 'bold bright_white' if is_focused else 'green'
        focus_tag = ' [●]' if is_focused else ''
        scroll_tag = f' ↑{scroll_offset}' if scroll_offset > 0 else ''

        if not active_books:
            return Panel(
                '[dim]Waiting for order book data...[/dim]',
                title=f'[bold]Order Books{focus_tag}[/bold]',
                border_style=border_style,
            )

        # Sort by closeness to 50% (most interesting markets first)
        active_books.sort(key=lambda x: abs(x[4] - Decimal('0.5')))
        visible = active_books[scroll_offset : scroll_offset + limit]

        table = Table(show_header=True, header_style='bold', expand=True)
        table.add_column('Market', ratio=2)
        table.add_column('Bid', justify='right', width=8)
        table.add_column('Ask', justify='right', width=8)
        table.add_column('Sprd', justify='right', width=7)
        table.add_column('Mid', justify='right', width=6)

        for ticker, bid, ask, spread, mid in visible:
            name = getattr(ticker, 'name', '') or ticker.symbol
            spread_style = (
                'green'
                if spread <= Decimal('0.02')
                else 'yellow'
                if spread <= Decimal('0.05')
                else 'red'
            )
            mid_pct = f'{mid * 100:.0f}%'
            table.add_row(
                name[:30],
                f'${bid.price:.4f}',
                f'${ask.price:.4f}',
                Text(f'{spread:.4f}', style=spread_style),
                mid_pct,
            )

        total_ob = getattr(self, 'ob_count', len(ob_items))
        return Panel(
            table,
            title=f'[bold]Order Books ({len(active_books)}/{total_ob} active){focus_tag}{scroll_tag}[/bold]',
            border_style=border_style,
        )

    def _create_news_panel(
        self, limit: int = 10, scroll_offset: int = 0, is_focused: bool = False
    ) -> Panel:
        """Create news headlines panel."""
        news: list = getattr(self, 'news_headlines', [])

        border_style = 'bold bright_white' if is_focused else 'bright_yellow'
        focus_tag = ' [●]' if is_focused else ''
        scroll_tag = f' ↑{scroll_offset}' if scroll_offset > 0 else ''

        if not news:
            return Panel(
                '[dim]Waiting for news...[/dim]',
                title=f'[bold]News Headlines{focus_tag}[/bold]',
                border_style=border_style,
            )

        # Newest-first with scroll
        news_rev = list(reversed(news))
        visible = news_rev[scroll_offset : scroll_offset + limit]

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column('Time', style='dim', width=8)
        table.add_column('News')

        for item in visible:
            if isinstance(item, dict):
                ts = str(item.get('timestamp', ''))
                title = str(item.get('title', ''))
                source = str(item.get('source', ''))
                url = str(item.get('url', ''))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                ts = str(item[0])
                title = str(item[1])
                source = ''
                url = ''
            else:
                ts = ''
                title = str(item)
                source = ''
                url = ''
            meta = source if source else ''
            if url:
                meta = f'{meta} | {url}' if meta else url
            body = title[:80]
            if meta:
                body = f'{body}\n{meta[:100]}'
            table.add_row(ts, Text(body, style='white'))

        return Panel(
            table,
            title=f'[bold]News Headlines ({len(news)} total){focus_tag}{scroll_tag}[/bold]',
            border_style=border_style,
        )

    def _create_system_status_panel(self) -> Panel:
        """Create compact system status bar."""
        event_count = getattr(self, 'event_count', 0)
        ob_count = getattr(self, 'ob_count', 0)
        news_count = len(getattr(self, 'news_headlines', []))
        news_buf = getattr(self, 'news_buffer_count', 0)

        # Runtime
        runtime = datetime.now() - self.start_time
        runtime_str = str(runtime).split('.')[0]

        status_parts = [
            f'Runtime: {runtime_str}',
            f'Events: {event_count}',
            f'Order Books: {ob_count}',
            f'News: {news_count}',
            f'News Buffer: {news_buf}',
        ]
        status_line = Text('  |  '.join(status_parts), style='white')

        return Panel(
            status_line,
            title='[bold]System Status[/bold]',
            border_style='bright_green',
        )

    def create_layout(self) -> Layout:
        """Create the full monitoring layout."""
        layout = Layout()

        # Header + body + status bar + footer
        layout.split_column(
            Layout(name='header', size=3),
            Layout(name='body'),
            Layout(name='status_bar', size=3),
            Layout(name='footer', size=1),
        )

        # Header with exchange name
        title = 'Oracle3 - Trading Monitor'
        if self.exchange_name:
            title = f'Oracle3 - {self.exchange_name} Trading Monitor'
        header_text = Text(title, justify='center', style='bold blue')
        layout['header'].update(Panel(header_text, border_style='blue'))

        # Body: 3 rows
        layout['body'].split_column(
            Layout(name='top_row', ratio=3),
            Layout(name='mid_row', ratio=3),
            Layout(name='bot_row', ratio=3),
        )

        # Top row: Portfolio + Stats | Strategy Decisions (main focus)
        layout['top_row'].split_row(
            Layout(name='left_top', ratio=1),
            Layout(name='decisions', ratio=2),
        )

        layout['left_top'].split_column(
            Layout(name='portfolio'),
            Layout(name='stats'),
        )

        # Mid row: Trading Activity (positions + orders) | Activity Log
        layout['mid_row'].split_row(
            Layout(name='trading', ratio=1),
            Layout(name='activity_log', ratio=1),
        )

        # Bottom row: Order Books | News Headlines
        layout['bot_row'].split_row(
            Layout(name='orderbooks', ratio=1),
            Layout(name='news', ratio=1),
        )

        # Populate panels (pass focus + scroll state)
        focus = self.focused_panel
        offsets = self.scroll_offsets
        layout['portfolio'].update(self._create_portfolio_summary())
        layout['stats'].update(self._create_stats_panel())
        layout['decisions'].update(
            self._create_decisions_panel(
                scroll_offset=offsets.get('decisions', 0),
                is_focused=(focus == 'decisions'),
            )
        )
        layout['trading'].update(self._create_trading_panel())
        layout['activity_log'].update(
            self._create_activity_log_panel(
                scroll_offset=offsets.get('activity_log', 0),
                is_focused=(focus == 'activity_log'),
            )
        )
        layout['orderbooks'].update(
            self._create_orderbook_panel(
                scroll_offset=offsets.get('orderbooks', 0),
                is_focused=(focus == 'orderbooks'),
            )
        )
        layout['news'].update(
            self._create_news_panel(
                scroll_offset=offsets.get('news', 0),
                is_focused=(focus == 'news'),
            )
        )

        # System status bar
        layout['status_bar'].update(self._create_system_status_panel())

        # Footer with keyboard shortcuts
        panel_labels = {
            'decisions': 'Decisions',
            'orderbooks': 'OrderBooks',
            'news': 'News',
            'activity_log': 'Activity',
        }
        pause_tag = '  [PAUSED]' if self.paused else ''
        focus_label = panel_labels.get(focus, focus)
        footer_text = Text(
            f'[Tab] Next Panel  [j/k ↑↓] Scroll  [p] Pause  [q] Quit'
            f'  │  Focus: {focus_label}{pause_tag}'
            f'  │  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            justify='center',
            style='dim',
        )
        layout['footer'].update(footer_text)

        return layout

    def display_snapshot(self) -> None:
        """Display a single snapshot of current state."""
        layout = self.create_layout()
        self.console.print(layout)

    def display_live(self, refresh_rate: float = 1.0) -> None:
        """Display live updating monitor."""
        with Live(
            self.create_layout(),
            console=self.console,
            refresh_per_second=1 / refresh_rate,
            screen=True,
        ) as live:
            try:
                while True:
                    time.sleep(refresh_rate)
                    live.update(self.create_layout())
            except KeyboardInterrupt:
                pass


@click.command()
@click.option(
    '--socket',
    '-s',
    default=None,
    type=click.Path(),
    help='Path to engine control socket (default: ~/.oracle3/engine.sock)',
)
def monitor(socket: str | None) -> None:
    """Attach a live Textual monitor to a running trading engine.

    Connects via Unix socket — the engine continues running when you close
    the monitor.  Use ``oracle3 trade`` to control the engine.

    Examples:
        oracle3 monitor                       # attach to default socket
        oracle3 monitor -s /tmp/eng.sock      # custom socket path
        oracle3 trade status                  # check engine health
        oracle3 trade pause                   # pause LLM decisions
    """
    from pathlib import Path

    from oracle3.cli.control import SOCKET_PATH
    from oracle3.cli.textual_monitor import SocketTradingMonitorApp

    sock = Path(socket) if socket else SOCKET_PATH

    try:
        app = SocketTradingMonitorApp(socket_path=sock)
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        # Textual crash or unsupported terminal — exit cleanly
        click.echo(f'Monitor closed: {exc}', err=True)
