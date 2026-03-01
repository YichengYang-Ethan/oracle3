"""Main CLI entry point for Oracle3."""

import click

from oracle3.cli.agent_commands import backtest, live, paper, strategy
from oracle3.cli.data_commands import data
from oracle3.cli.market_commands import market
from oracle3.cli.monitor import monitor
from oracle3.cli.news_commands import news
from oracle3.cli.research_commands import research
from oracle3.cli.trade_commands import trade


@click.group()
@click.version_option(version='1.0.0')
def cli() -> None:
    """Oracle3 - AI-native prediction market trading agent on Solana, Polymarket, and Kalshi."""
    pass


@cli.command()
@click.option('--host', default='0.0.0.0', show_default=True, help='Bind address')
@click.option('--port', default=8080, show_default=True, type=int, help='Server port')
def blinks(host: str, port: int) -> None:
    """Start the Solana Blinks (Actions) server."""
    from oracle3.blinks.server import run_server

    click.echo(f'Starting Oracle3 Blinks server on {host}:{port}...')
    run_server(host=host, port=port)


@cli.command()
@click.option('--port', default=3000, show_default=True, type=int, help='Dashboard server port')
@click.option(
    '--exchange',
    type=click.Choice(['solana', 'polymarket', 'kalshi']),
    default='solana',
    show_default=True,
)
@click.option('--duration', type=float, default=None, help='Seconds to run (default: forever)')
@click.option('--initial-capital', default='1000', show_default=True)
@click.option(
    '--strategy-ref',
    default=None,
    help='Strategy ref: module:Class or /path/file.py:Class. If omitted, run in idle mode.',
)
@click.option(
    '--strategy-kwargs-json', default=None, help='JSON object for strategy constructor kwargs.'
)
def dashboard(
    port: int,
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str | None,
    strategy_kwargs_json: str | None,
) -> None:
    """Launch the web dashboard with paper trading (open browser to http://localhost:PORT)."""
    import asyncio
    import json as json_lib
    from decimal import Decimal

    from oracle3.cli.agent_commands import _build_news_augmented_source, _IdleStrategy
    from oracle3.dashboard.server import DashboardServer
    from oracle3.strategy.loader import load_strategy_class

    # Build strategy
    if strategy_ref:
        kwargs = json_lib.loads(strategy_kwargs_json) if strategy_kwargs_json else {}
        strategy_cls = load_strategy_class(strategy_ref)
        strategy_obj = strategy_cls(**kwargs)
    else:
        strategy_obj = _IdleStrategy()

    capital = Decimal(initial_capital)
    data_source = _build_news_augmented_source(exchange)

    async def _run() -> None:
        from oracle3.cli.control import ControlServer
        from oracle3.core.trading_engine import TradingEngine
        from oracle3.data.market_data_manager import MarketDataManager
        from oracle3.position.position_manager import Position, PositionManager
        from oracle3.risk.risk_manager import NoRiskManager
        from oracle3.ticker.ticker import CashTicker
        from oracle3.trader.paper_trader import PaperTrader

        market_data = MarketDataManager()
        position_manager = PositionManager()

        # Set up initial cash based on exchange
        if exchange in ('solana', 'dflow'):
            cash_ticker = CashTicker.DFLOW_USDC
        elif exchange == 'kalshi':
            cash_ticker = CashTicker.KALSHI_USD
        else:
            cash_ticker = CashTicker.POLYMARKET_USDC

        position_manager.update_position(
            Position(
                ticker=cash_ticker,
                quantity=capital,
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        trader = PaperTrader(
            market_data=market_data,
            risk_manager=NoRiskManager(),
            position_manager=position_manager,
            min_fill_rate=Decimal('0.5'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.0'),
        )

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy_obj,
            trader=trader,
            continuous=True,
        )

        # Start dashboard server (background thread)
        dash = DashboardServer(engine, port=port)
        dash.start()

        # Start control server
        ctrl = ControlServer(engine)
        await ctrl.start()

        click.echo(f'Dashboard running at http://localhost:{port}')
        click.echo('Press Ctrl+C to stop.\n')

        try:
            if duration:
                await asyncio.wait_for(engine.start(), timeout=duration)
            else:
                await engine.start()
        except asyncio.TimeoutError:
            click.echo(f'\nDuration reached ({duration}s). Stopping...')
        except asyncio.CancelledError:
            pass
        finally:
            await engine.stop()
            await ctrl.stop()
            dash.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo('\nDashboard stopped.')


@cli.command('trade-log')
@click.option('--limit', default=20, show_default=True, type=int, help='Number of entries')
@click.option(
    '--keypair-path', default=None, help='Solana keypair JSON file (or SOLANA_KEYPAIR_PATH)'
)
@click.option(
    '--rpc-url',
    default='https://api.mainnet-beta.solana.com',
    show_default=True,
    help='Solana RPC URL',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Output as JSON')
def trade_log(limit: int, keypair_path: str | None, rpc_url: str, as_json: bool) -> None:
    """Show on-chain trade log from Solana Memo transactions."""
    import asyncio
    import json as json_lib

    from oracle3.onchain.logger import OnChainLogger
    from oracle3.trader.solana_trader import _load_keypair

    try:
        kp = _load_keypair(keypair_path=keypair_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    log = OnChainLogger(keypair=kp, rpc_url=rpc_url)
    trades = asyncio.run(log.get_trade_log(limit=limit))

    if as_json:
        click.echo(json_lib.dumps({'trades': trades, 'count': len(trades)}))
        return

    if not trades:
        click.echo('No Oracle3 trade log entries found.')
        return

    click.echo(f'On-chain trade log ({len(trades)} entries):\n')
    for i, t in enumerate(trades, 1):
        market = t.get('market', '?')
        side = t.get('side', '?')
        price = t.get('price', '?')
        qty = t.get('qty', '?')
        ts = t.get('ts', '')
        sig = t.get('signature', '')[:16]
        click.echo(f'  [{i}] {market} {side.upper()} x{qty} @ {price}  {ts}  tx:{sig}...')
    click.echo()


cli.add_command(monitor)
cli.add_command(trade)
cli.add_command(strategy)
cli.add_command(backtest)
cli.add_command(paper)
cli.add_command(live)
cli.add_command(news)
cli.add_command(market)
cli.add_command(data)
cli.add_command(research)


if __name__ == '__main__':
    cli()
