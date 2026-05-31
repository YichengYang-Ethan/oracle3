from __future__ import annotations

import csv
import json

from click.testing import CliRunner

from oracle3.cli.cli import cli


def _write_calibration_csv(path, rows: int = 50) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['price', 'outcome'])
        writer.writeheader()
        for index in range(rows):
            price = 0.2 + (index % 30) / 50
            outcome = 1 if index % 3 != 0 else 0
            writer.writerow({'price': f'{price:.3f}', 'outcome': outcome})


def test_calibrate_cli_summary_contains_lambda(tmp_path):
    csv_path = tmp_path / 'contracts.csv'
    _write_calibration_csv(csv_path)

    result = CliRunner().invoke(cli, ['calibrate', '--csv', str(csv_path)])

    assert result.exit_code == 0, result.output
    assert 'Wang Transform MLE' in result.output
    assert 'lambda' in result.output


def test_calibrate_cli_json_outputs_mle_result(tmp_path):
    csv_path = tmp_path / 'contracts.csv'
    _write_calibration_csv(csv_path)

    result = CliRunner().invoke(cli, ['calibrate', '--csv', str(csv_path), '--json'])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert 'beta' in payload
    assert isinstance(payload['beta'], list)
    assert payload['n_obs'] == 50
