from __future__ import annotations

import csv
import json

from click.testing import CliRunner

from oracle3.cli.cli import cli


def _write_calibration_csv(
    path,
    rows: int = 50,
    *,
    include_covariates: bool = False,
    float_outcomes: bool = False,
) -> None:
    fieldnames = ['price', 'outcome']
    if include_covariates:
        fieldnames.extend(['volume', 'duration_hours', 'spread'])

    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index in range(rows):
            price = 0.2 + (index % 30) / 50
            outcome = 1 if index % 3 != 0 else 0
            row = {
                'price': f'{price:.3f}',
                'outcome': f'{float(outcome):.1f}' if float_outcomes else outcome,
            }
            if include_covariates:
                row.update({
                    'volume': f'{100 + index * 5:.2f}',
                    'duration_hours': f'{24 + index % 14:.2f}',
                    'spread': f'{0.01 + (index % 5) / 1000:.4f}',
                })
            writer.writerow(row)


def test_calibrate_cli_summary_contains_lambda(tmp_path):
    csv_path = tmp_path / 'contracts.csv'
    _write_calibration_csv(csv_path)

    result = CliRunner().invoke(cli, ['calibrate', '--csv', str(csv_path)])

    assert result.exit_code == 0, result.output
    assert 'Wang Transform MLE' in result.output
    assert 'lambda' in result.output


def test_calibrate_cli_json_outputs_mle_result(tmp_path):
    csv_path = tmp_path / 'contracts.csv'
    _write_calibration_csv(csv_path, float_outcomes=True)

    result = CliRunner().invoke(cli, ['calibrate', '--csv', str(csv_path), '--json'])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert 'beta' in payload
    assert isinstance(payload['beta'], list)
    assert payload['n_obs'] == 50


def test_calibrate_cli_hierarchical_outputs_covariate_result(tmp_path):
    csv_path = tmp_path / 'contracts.csv'
    _write_calibration_csv(csv_path, rows=120, include_covariates=True)

    result = CliRunner().invoke(
        cli, ['calibrate', '--csv', str(csv_path), '--hierarchical', '--json']
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['n_obs'] == 120
    assert payload['n_params'] == 5
    assert payload['covariate_names'] == [
        'constant',
        'ln(1+volume)',
        'ln(1+duration)',
        '|p-0.5|',
        'spread',
    ]


def test_calibrate_cli_hierarchical_requires_complete_covariates(tmp_path):
    csv_path = tmp_path / 'contracts.csv'
    _write_calibration_csv(csv_path)

    result = CliRunner().invoke(
        cli, ['calibrate', '--csv', str(csv_path), '--hierarchical']
    )

    assert result.exit_code != 0
    assert '--hierarchical requires complete CSV column(s)' in result.output
