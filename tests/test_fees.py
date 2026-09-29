"""Tests for oracle3.fees against the venues' published numbers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.fees import (
    CENT,
    KalshiSchedule,
    PolymarketSchedule,
    UnsupportedFeeSchedule,
    default_schedule,
)

# Kalshi fee schedule, "General Trading Fees Table" (effective July 7, 2026).
# The table rounds to the cent, so these use increment=CENT.
KALSHI_TABLE_100_CONTRACTS = [
    (0.01, '0.07'),
    (0.05, '0.34'),
    (0.10, '0.63'),
    (0.20, '1.12'),
    (0.30, '1.47'),
    (0.50, '1.75'),
    (0.70, '1.47'),
    (0.95, '0.34'),
    (0.99, '0.07'),
]


@pytest.mark.parametrize('price,expected', KALSHI_TABLE_100_CONTRACTS)
def test_kalshi_taker_matches_published_table_for_100_contracts(price, expected):
    assert KalshiSchedule(increment=CENT).fee(price, 100) == Decimal(expected)


@pytest.mark.parametrize(
    'price,expected', [(0.01, '0.01'), (0.20, '0.02'), (0.50, '0.02'), (0.99, '0.01')]
)
def test_kalshi_taker_matches_published_table_for_one_contract(price, expected):
    assert KalshiSchedule(increment=CENT).fee(price, 1) == Decimal(expected)


def test_kalshi_default_rounds_up_to_a_centicent():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 exactly
    assert KalshiSchedule().fee(0.5, 1) == Decimal('0.0175')
    # 0.07 * 1 * 0.55 * 0.45 = 0.017325 -> 0.0174
    assert KalshiSchedule().fee(0.55, 1) == Decimal('0.0174')


def test_kalshi_multiplier_scales_and_zero_is_free():
    assert KalshiSchedule(multiplier=Decimal('0')).fee(0.5, 100) == 0
    assert KalshiSchedule(multiplier=Decimal('2')).fee(0.5, 100) == Decimal('3.5')


def test_kalshi_maker_fee_only_on_series_with_maker_fees():
    assert KalshiSchedule(maker_fees=False).fee(0.5, 100, maker=True) == 0
    # 0.0175 * 100 * 0.25 = 0.4375
    assert KalshiSchedule(maker_fees=True).fee(0.5, 100, maker=True) == Decimal(
        '0.4375'
    )


def test_kalshi_from_series_reads_fee_type_and_multiplier():
    fed = KalshiSchedule.from_series(
        {'fee_type': 'quadratic_with_maker_fees', 'fee_multiplier': 1}
    )
    assert fed.maker_fees is True and fed.multiplier == 1
    btc = KalshiSchedule.from_series({'fee_type': 'quadratic', 'fee_multiplier': 0})
    assert btc.fee(0.5, 100) == 0
    with pytest.raises(UnsupportedFeeSchedule):
        KalshiSchedule.from_series(
            {'fee_type': 'quadratic_with_combo_maker_fees', 'fee_multiplier': 1}
        )
    with pytest.raises(UnsupportedFeeSchedule):
        KalshiSchedule.from_series({'fee_type': 'flat', 'fee_multiplier': 1})


def test_polymarket_documented_example():
    # docs.polymarket.com: 100 Crypto shares at $0.30 or $0.70 pay $1.47.
    crypto = PolymarketSchedule.for_category('crypto')
    assert crypto.fee(0.30, 100) == Decimal('1.47000')
    assert crypto.fee(0.70, 100) == Decimal('1.47000')


def test_polymarket_makers_and_geopolitics_are_free():
    assert PolymarketSchedule.for_category('sports').fee(0.5, 100, maker=True) == 0
    assert PolymarketSchedule.for_category('geopolitics').fee(0.5, 100) == 0


def test_polymarket_rounds_to_five_decimals_with_minimum():
    assert PolymarketSchedule(rate=Decimal('0.04')).fee(0.001, 0.01) == Decimal(
        '0.00001'
    )


def test_polymarket_from_gamma_market():
    market = {
        'feesEnabled': True,
        'feeType': 'sports_fees_v3',
        'feeSchedule': {
            'exponent': 1,
            'rate': 0.05,
            'takerOnly': True,
            'rebateRate': 0.15,
        },
    }
    schedule = PolymarketSchedule.from_gamma_market(market)
    assert schedule.rate == Decimal('0.05')
    assert (
        PolymarketSchedule.from_gamma_market({'feesEnabled': False}).fee(0.5, 100) == 0
    )
    with pytest.raises(UnsupportedFeeSchedule):
        PolymarketSchedule.from_gamma_market(
            {'feeSchedule': {'exponent': 2, 'rate': 0.05}}
        )
    with pytest.raises(UnsupportedFeeSchedule):
        PolymarketSchedule.from_gamma_market(
            {'feeSchedule': {'rate': 0.05, 'takerOnly': False}}
        )


def test_unknown_market_schedule_defaults_are_conservative():
    assert default_schedule('polymarket').rate == Decimal('0.07')
    assert default_schedule('kalshi').multiplier == 1
    with pytest.raises(ValueError):
        default_schedule('nyse')


@pytest.mark.parametrize('bad', [0, 1, -0.1, 1.2])
def test_price_must_be_inside_the_unit_interval(bad):
    with pytest.raises(ValueError):
        KalshiSchedule().fee(bad, 1)
    with pytest.raises(ValueError):
        PolymarketSchedule().fee(bad, 1)


def test_fee_is_symmetric_in_price():
    for schedule in (KalshiSchedule(), PolymarketSchedule()):
        for p in (0.1, 0.25, 0.4):
            assert schedule.fee(p, 37) == schedule.fee(1 - p, 37)
