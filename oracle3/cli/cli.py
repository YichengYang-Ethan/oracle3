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
