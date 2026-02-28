from __future__ import annotations

from click.testing import CliRunner

from oracle3.cli.cli import cli


def test_trade_pause_resume_stop_and_estop(monkeypatch):
    calls: list[str] = []

    def fake_run_command(cmd, socket_path=None, **kwargs):
        calls.append(cmd)
        return {'ok': True, 'status': 'paused' if cmd == 'pause' else 'stopping'}

    monkeypatch.setattr('oracle3.cli.trade_commands.run_command', fake_run_command)
    runner = CliRunner()

    pause = runner.invoke(cli, ['trade', 'pause'])
    resume = runner.invoke(cli, ['trade', 'resume'])
    stop = runner.invoke(cli, ['trade', 'stop'])
    estop = runner.invoke(cli, ['trade', 'estop'])

    assert pause.exit_code == 0
    assert resume.exit_code == 0
    assert stop.exit_code == 0
    assert estop.exit_code == 0
    assert calls == ['pause', 'resume', 'stop', 'stop']
    assert 'EMERGENCY STOP signal sent' in estop.output


def test_trade_status_human_and_json(monkeypatch):
    def fake_run_command(cmd, socket_path=None, **kwargs):
        assert cmd == 'status'
        return {
            'ok': True,
            'paused': False,
            'runtime': '0:00:05',
            'event_count': 12,
            'decisions': 4,
            'executed': 2,
            'orders': 3,
        }

    monkeypatch.setattr('oracle3.cli.trade_commands.run_command', fake_run_command)
    runner = CliRunner()

    human = runner.invoke(cli, ['trade', 'status'])
    js = runner.invoke(cli, ['trade', 'status', '--json'])

    assert human.exit_code == 0
    assert 'events=12' in human.output
    assert js.exit_code == 0
    assert '"event_count": 12' in js.output


def test_trade_error_returns_nonzero(monkeypatch):
    def fake_run_command(cmd, socket_path=None, **kwargs):
        return {'ok': False, 'error': 'no socket'}

    monkeypatch.setattr('oracle3.cli.trade_commands.run_command', fake_run_command)
    runner = CliRunner()
    result = runner.invoke(cli, ['trade', 'pause'])
    assert result.exit_code == 1
    assert 'error: no socket' in result.output


def test_trade_killswitch_toggle(tmp_path):
    runner = CliRunner()
    kill_file = tmp_path / 'kill.switch'

    enable = runner.invoke(
        cli, ['trade', 'killswitch', '--on', '--path', str(kill_file)]
    )
    assert enable.exit_code == 0
    assert kill_file.exists()

    status = runner.invoke(
        cli, ['trade', 'killswitch', '--path', str(kill_file), '--json']
    )
    assert status.exit_code == 0
    assert '"status": "enabled"' in status.output

    disable = runner.invoke(
        cli, ['trade', 'killswitch', '--off', '--path', str(kill_file)]
    )
    assert disable.exit_code == 0
    assert not kill_file.exists()
