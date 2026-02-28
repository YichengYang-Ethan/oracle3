from __future__ import annotations

import textwrap
from pathlib import Path

from click.testing import CliRunner

from oracle3.cli.cli import cli


def test_strategy_create_and_validate(tmp_path):
    runner = CliRunner()
    strategy_file = tmp_path / 'my_strategy.py'

    create = runner.invoke(
        cli,
        [
            'strategy',
            'create',
            '--output',
            str(strategy_file),
            '--class-name',
            'MyStrategy',
        ],
    )
    assert create.exit_code == 0
    assert strategy_file.exists()

    validate = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{strategy_file}:MyStrategy',
            '--json',
        ],
    )
    assert validate.exit_code == 0
    assert '"ok": true' in validate.output


def test_backtest_run_invokes_runner(monkeypatch, tmp_path):
    history_file = tmp_path / 'history.jsonl'
    history_file.write_text(
        '{"event_id":"E1","market_id":"M1","time_series":{"Yes":[{"t":1,"p":0.5}]}}\n'
    )

    captured = {}

    async def fake_run_backtest(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr('oracle3.cli.agent_commands.run_backtest', fake_run_backtest)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'backtest',
            'run',
            '--history-file',
            str(history_file),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--strategy-ref',
            'oracle3.strategy.test_strategy:TestStrategy',
        ],
    )
    assert result.exit_code == 0
    assert captured['history_file'] == str(history_file)
    assert captured['ticker_symbol'].market_id == 'M1'
    assert captured['include_all_markets_context'] is False
    assert captured['allow_cross_market_trading'] is False


def test_strategy_validate_with_kwargs_json(tmp_path):
    strategy_file = tmp_path / 'needs_kwargs.py'
    strategy_file.write_text(
        textwrap.dedent(
            """
            from oracle3.events.events import Event
            from oracle3.strategy.strategy import Strategy
            from oracle3.trader.trader import Trader

            class NeedsKwargs(Strategy):
                def __init__(self, threshold: float):
                    self.threshold = threshold

                async def process_event(self, event: Event, trader: Trader) -> None:
                    return
            """
        ).strip()
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{strategy_file}:NeedsKwargs',
            '--strategy-kwargs-json',
            '{"threshold": 0.7}',
            '--json',
        ],
    )

    assert result.exit_code == 0
    assert '"ok": true' in result.output
    assert '"threshold": 0.7' in result.output


def test_strategy_dry_run_with_kwargs_json(tmp_path):
    strategy_file = tmp_path / 'dry_run_strategy.py'
    strategy_file.write_text(
        textwrap.dedent(
            """
            from oracle3.events.events import Event
            from oracle3.strategy.strategy import Strategy
            from oracle3.trader.trader import Trader

            class DryRunStrategy(Strategy):
                def __init__(self, multiplier: int):
                    self.multiplier = multiplier

                async def process_event(self, event: Event, trader: Trader) -> None:
                    return
            """
        ).strip()
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{strategy_file}:DryRunStrategy',
            '--strategy-kwargs-json',
            '{"multiplier": 2}',
            '--dry-run',
            '--events',
            '6',
            '--json',
        ],
    )

    assert result.exit_code == 0
    assert '"ok": true' in result.output
    assert '"events_processed": 6' in result.output


def test_example_strategy_files_validate() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    momentum = repo_root / 'examples' / 'strategies' / 'threshold_momentum_strategy.py'
    pressure = repo_root / 'examples' / 'strategies' / 'orderbook_pressure_strategy.py'

    runner = CliRunner()
    result1 = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{momentum}:ThresholdMomentumStrategy',
            '--json',
        ],
    )
    result2 = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{pressure}:OrderBookPressureStrategy',
            '--json',
        ],
    )

    assert result1.exit_code == 0
    assert result2.exit_code == 0


def test_paper_and_live_commands_invokable(monkeypatch):
    captured = {}

    class DummySource:
        def __init__(self, *args, **kwargs):
            captured['source_kwargs'] = kwargs

    async def fake_paper(**kwargs):
        captured['paper_kwargs'] = kwargs

    async def fake_live(**kwargs):
        captured['live_kwargs'] = kwargs

    monkeypatch.setattr(
        'oracle3.cli.agent_commands.LivePolyMarketDataSource', DummySource
    )
    monkeypatch.setattr(
        'oracle3.cli.agent_commands.run_live_paper_trading', fake_paper
    )
    monkeypatch.setattr(
        'oracle3.cli.agent_commands.run_live_polymarket_trading', fake_live
    )

    runner = CliRunner()
    paper_res = runner.invoke(
        cli,
        [
            'paper',
            'run',
            '--exchange',
            'polymarket',
            '--duration',
            '1',
            '--strategy-ref',
            'oracle3.strategy.test_strategy:TestStrategy',
        ],
    )
    assert paper_res.exit_code == 0
    assert captured['paper_kwargs']['duration'] == 1.0

    live_res = runner.invoke(
        cli,
        [
            'live',
            'run',
            '--exchange',
            'polymarket',
            '--wallet-private-key',
            'dummy',
            '--duration',
            '1',
            '--strategy-ref',
            'oracle3.strategy.test_strategy:TestStrategy',
        ],
        input='y\n',
    )
    assert live_res.exit_code == 0
    assert captured['live_kwargs']['duration'] == 1.0


def test_paper_run_historical_replay_invokes_runner(monkeypatch, tmp_path):
    history_file = tmp_path / 'history.jsonl'
    history_file.write_text(
        '{"event_id":"E1","market_id":"M1","time_series":{"Yes":[{"t":1,"p":0.5}]}}\n'
    )

    captured = {}

    class DummyHistoricalSource:
        def __init__(self, history_file, ticker):
            captured['history_file'] = history_file
            captured['ticker'] = ticker

    async def fake_paper(**kwargs):
        captured['paper_kwargs'] = kwargs

    monkeypatch.setattr(
        'oracle3.cli.agent_commands.HistoricalDataSource', DummyHistoricalSource
    )
    monkeypatch.setattr(
        'oracle3.cli.agent_commands.run_live_paper_trading', fake_paper
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'paper',
            'run',
            '--exchange',
            'polymarket',
            '--history-file',
            str(history_file),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--duration',
            '1',
            '--strategy-ref',
            'oracle3.strategy.test_strategy:TestStrategy',
        ],
    )
    assert result.exit_code == 0
    assert captured['history_file'] == str(history_file)
    assert captured['ticker'].market_id == 'M1'
    assert captured['ticker'].event_id == 'E1'
    assert captured['paper_kwargs']['duration'] == 1.0
    assert captured['paper_kwargs']['continuous'] is False
    assert captured['paper_kwargs']['emit_text'] is True


def test_paper_run_historical_json_disables_text_output(monkeypatch, tmp_path):
    history_file = tmp_path / 'history.jsonl'
    history_file.write_text(
        '{"event_id":"E1","market_id":"M1","time_series":{"Yes":[{"t":1,"p":0.5}]}}\n'
    )

    captured = {}

    class DummyHistoricalSource:
        def __init__(self, history_file, ticker):
            captured['history_file'] = history_file
            captured['ticker'] = ticker

    async def fake_paper(**kwargs):
        captured['paper_kwargs'] = kwargs

    monkeypatch.setattr(
        'oracle3.cli.agent_commands.HistoricalDataSource', DummyHistoricalSource
    )
    monkeypatch.setattr(
        'oracle3.cli.agent_commands.run_live_paper_trading', fake_paper
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'paper',
            'run',
            '--history-file',
            str(history_file),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--duration',
            '1',
            '--strategy-ref',
            'oracle3.strategy.test_strategy:TestStrategy',
            '--json',
        ],
    )

    assert result.exit_code == 0
    assert captured['paper_kwargs']['emit_text'] is False


def test_paper_run_historical_requires_market_and_event(tmp_path):
    history_file = tmp_path / 'history.jsonl'
    history_file.write_text(
        '{"event_id":"E1","market_id":"M1","time_series":{"Yes":[{"t":1,"p":0.5}]}}\n'
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'paper',
            'run',
            '--history-file',
            str(history_file),
            '--strategy-ref',
            'oracle3.strategy.test_strategy:TestStrategy',
        ],
    )
    assert result.exit_code != 0
    assert '--history-file requires both --market-id and --event-id' in result.output
