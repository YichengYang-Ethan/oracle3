"""Break-even violations for constraint arbitrage under published venue fees.

Everything here follows from the fee schedules in ``oracle3.fees``; no market
data is used. For a basket whose legs are bought at prices p_1..p_n, the fee
per contract is sum_i k_i * p_i * (1 - p_i), where k_i is the leg's fee
coefficient (Kalshi taker 0.07 * M, Polymarket taker rate, makers usually 0).
A violation is tradable only if it exceeds that amount.

Run from the repository root:

    python scripts/fee_frontier.py            # print the tables
    python scripts/fee_frontier.py --figure   # also write docs/assets/fee_frontier.png (needs matplotlib)
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oracle3.fees import KalshiSchedule, PolymarketSchedule  # noqa: E402

LARGE = Decimal('10000')  # per-contract fee at size, so rounding does not matter

SCHEDULES = {
    'Kalshi taker (M=1)': KalshiSchedule(),
    'Polymarket crypto (0.07)': PolymarketSchedule.for_category('crypto'),
    'Polymarket sports/economics (0.05)': PolymarketSchedule.for_category('sports'),
    'Polymarket politics/finance (0.04)': PolymarketSchedule.for_category('politics'),
    'Polymarket geopolitics (0)': PolymarketSchedule.for_category('geopolitics'),
}


def per_contract(schedule, price: float) -> float:
    return float(schedule.fee(price, LARGE)) / float(LARGE)


def two_leg_table(prices=(0.1, 0.3, 0.5)) -> list[str]:
    pairs = [
        ('Kalshi taker (M=1)', 'Kalshi taker (M=1)'),
        ('Kalshi taker (M=1)', 'Polymarket politics/finance (0.04)'),
        ('Kalshi taker (M=1)', 'Polymarket sports/economics (0.05)'),
        ('Polymarket politics/finance (0.04)', 'Polymarket politics/finance (0.04)'),
        ('Polymarket crypto (0.07)', 'Polymarket crypto (0.07)'),
        ('Polymarket geopolitics (0)', 'Polymarket geopolitics (0)'),
    ]
    head = (
        '| Leg 1 | Leg 2 | '
        + ' | '.join(f'both legs at {p:.1f}' for p in prices)
        + ' |'
    )
    rows = [head, '|---|---|' + '---:|' * len(prices)]
    for a, b in pairs:
        cells = [
            f'{100 * (per_contract(SCHEDULES[a], p) + per_contract(SCHEDULES[b], p)):.2f}¢'
            for p in prices
        ]
        rows.append(f'| {a} | {b} | ' + ' | '.join(cells) + ' |')
    return rows


def event_sum_table(ns=(2, 3, 5, 10, 20)) -> list[str]:
    head = (
        '| Outcomes (equal prices) | '
        + ' | '.join(
            name
            for name in (
                'Kalshi taker (M=1)',
                'Polymarket politics/finance (0.04)',
                'Polymarket sports/economics (0.05)',
            )
        )
        + ' |'
    )
    rows = [head, '|---:|---:|---:|---:|']
    for n in ns:
        p = 1.0 / n
        cells = []
        for name in (
            'Kalshi taker (M=1)',
            'Polymarket politics/finance (0.04)',
            'Polymarket sports/economics (0.05)',
        ):
            cells.append(f'{100 * n * per_contract(SCHEDULES[name], p):.2f}¢')
        rows.append(f'| {n} | ' + ' | '.join(cells) + ' |')
    return rows


def small_order_rows() -> list[str]:
    from oracle3.fees import CENT

    rows = [
        '| Contracts at $0.50 | Kalshi fee, centicent rounding | Kalshi fee, cent rounding (schedule table) |',
        '|---:|---:|---:|',
    ]
    for c in (1, 2, 5, 10, 100):
        a = KalshiSchedule().fee(0.5, c)
        b = KalshiSchedule(increment=CENT).fee(0.5, c)
        rows.append(f'| {c} | ${a} | ${b} |')
    return rows


def figure(path: Path) -> None:
    import matplotlib

    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    grid = [i / 200 for i in range(1, 200)]
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=150)
    styles = {
        'Kalshi taker (M=1)': {'linewidth': 3.2, 'alpha': 0.55},
        'Polymarket crypto (0.07)': {'linewidth': 1.4, 'linestyle': '--'},
    }
    for name, schedule in SCHEDULES.items():
        if name.endswith('(0)'):
            continue
        label = name + (' (same curve as Kalshi)' if 'crypto' in name else '')
        ax.plot(
            grid,
            [100 * per_contract(schedule, p) for p in grid],
            label=label,
            **styles.get(name, {'linewidth': 1.8}),
        )
    ax.set_xlabel('Price paid for the leg ($)')
    ax.set_ylabel('Taker fee per contract (cents)')
    ax.set_title('Taker fee per leg under published schedules (retrieved 2026-09-28)')
    ax.set_xlim(0, 1)
    ax.set_ylim(0, None)
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    print(f'wrote {path}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--figure', action='store_true', help='write docs/assets/fee_frontier.png'
    )
    args = parser.parse_args()

    print('Break-even violation per contract, two-leg taker baskets\n')
    print('\n'.join(two_leg_table()))
    print(
        '\nBreak-even violation per contract, buying every outcome of an n-way event\n'
    )
    print('\n'.join(event_sum_table()))
    print('\nRounding on small Kalshi orders\n')
    print('\n'.join(small_order_rows()))
    if args.figure:
        figure(
            Path(__file__).resolve().parents[1] / 'docs' / 'assets' / 'fee_frontier.png'
        )


if __name__ == '__main__':
    main()
