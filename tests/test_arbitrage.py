"""Tests for oracle3.arbitrage static no-arbitrage checks."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.arbitrage import Quote, check_constraint
from oracle3.fees import KalshiSchedule, PolymarketSchedule

FREE = KalshiSchedule(multiplier=Decimal('0'))


def q(
    market_id,
    yes_bid=None,
    yes_ask=None,
    no_bid=None,
    no_ask=None,
    venue='kalshi',
    schedule=FREE,
):
    return Quote(market_id, venue, yes_bid, yes_ask, no_bid, no_ask, schedule=schedule)


def test_missing_ask_is_derived_from_the_other_side():
    quote = q('A', yes_bid=0.60, no_bid=0.35)
    assert quote.ask('yes') == pytest.approx(0.65)
    assert quote.ask('no') == pytest.approx(0.40)
    assert q('B').ask('yes') is None


def test_implication_violation_without_fees():
    # A implies B but A is bid at 0.60 while B is offered at 0.55.
    result = check_constraint(
        'implication', [q('A', yes_bid=0.60), q('B', yes_ask=0.55)]
    )
    best = result.best
    assert result.violated and result.profitable_after_fees
    assert best.description == 'NO on A + YES on B'
    assert best.cost == pytest.approx(0.95)
    assert best.gross_edge == pytest.approx(0.05)
    assert best.net_edge == pytest.approx(0.05)


def test_implication_after_kalshi_taker_fees():
    kalshi = KalshiSchedule()
    result = check_constraint(
        'implication',
        [q('A', yes_bid=0.60, schedule=kalshi), q('B', yes_ask=0.55, schedule=kalshi)],
    )
    # NO on A at 0.40: 0.07*0.4*0.6 = 0.0168; YES on B at 0.55: 0.017325 -> 0.0174
    assert result.best.fees == pytest.approx(0.0342)
    assert result.best.net_edge == pytest.approx(0.0158)


def test_fees_can_erase_a_violation():
    kalshi = KalshiSchedule()
    result = check_constraint(
        'implication',
        [q('A', yes_bid=0.51, schedule=kalshi), q('B', yes_ask=0.50, schedule=kalshi)],
    )
    assert result.violated
    assert not result.profitable_after_fees
    assert result.best.net_edge < 0


def test_no_violation_when_prices_respect_the_bound():
    result = check_constraint(
        'implication', [q('A', yes_bid=0.40), q('B', yes_ask=0.55)]
    )
    assert not result.violated
    assert result.best.gross_edge == pytest.approx(-0.15)


def test_exclusivity_over_three_outcomes_pays_n_minus_one():
    quotes = [q('A', yes_bid=0.40), q('B', yes_bid=0.35), q('C', yes_bid=0.30)]
    result = check_constraint('exclusivity', quotes)
    best = result.best
    assert best.payoff_per_contract == 2
    # NO asks 0.60 + 0.65 + 0.70 = 1.95 < 2
    assert best.gross_edge == pytest.approx(0.05)


def test_complement_picks_the_cheaper_basket():
    result = check_constraint(
        'complement',
        [q('A', yes_bid=0.40, yes_ask=0.42), q('B', yes_bid=0.52, yes_ask=0.55)],
    )
    # YES+YES costs 0.97; NO+NO costs 0.60 + 0.48 = 1.08
    assert result.best.description == 'YES on A + YES on B'
    assert result.best.gross_edge == pytest.approx(0.03)
    assert len(result.baskets) == 2


def test_same_event_across_venues_uses_each_venues_fees():
    poly = PolymarketSchedule(rate=Decimal('0.04'))
    kalshi = KalshiSchedule()
    result = check_constraint(
        'same_event',
        [
            q('K', yes_bid=0.58, yes_ask=0.60, venue='kalshi', schedule=kalshi),
            q('P', yes_bid=0.64, yes_ask=0.65, venue='polymarket', schedule=poly),
        ],
    )
    best = result.best
    assert best.description == 'YES on A + NO on B'
    assert best.cost == pytest.approx(0.96)
    venues = {leg.venue for leg in best.legs}
    assert venues == {'kalshi', 'polymarket'}
    # Kalshi YES at 0.60: 0.0168; Polymarket NO at 0.36: 0.04*0.36*0.64 = 0.009216 -> 0.00922
    assert best.fees == pytest.approx(0.0168 + 0.00922)


def test_event_sum_both_directions():
    cheap = [q('A', yes_ask=0.30), q('B', yes_ask=0.30), q('C', yes_ask=0.35)]
    assert check_constraint('event_sum', cheap).best.gross_edge == pytest.approx(0.05)

    rich = [q('A', yes_bid=0.40), q('B', yes_bid=0.35), q('C', yes_bid=0.30)]
    best = check_constraint('event_sum', rich).best
    assert best.description == 'NO on every outcome'
    assert best.payoff_per_contract == 2
    assert best.gross_edge == pytest.approx(0.05)


def test_size_scales_edge_and_fees():
    kalshi = KalshiSchedule()
    one = check_constraint(
        'implication',
        [q('A', yes_bid=0.6, schedule=kalshi), q('B', yes_ask=0.55, schedule=kalshi)],
    )
    hundred = check_constraint(
        'implication',
        [q('A', yes_bid=0.6, schedule=kalshi), q('B', yes_ask=0.55, schedule=kalshi)],
        contracts=100,
    )
    assert hundred.best.gross_edge == pytest.approx(100 * one.best.gross_edge)
    assert hundred.best.fees_per_contract == pytest.approx(
        0.07 * 0.4 * 0.6 + 0.07 * 0.55 * 0.45, abs=1e-4
    )


def test_maker_pricing_uses_maker_schedule():
    no_maker_fee = KalshiSchedule()
    result = check_constraint(
        'implication',
        [
            q('A', yes_bid=0.6, schedule=no_maker_fee),
            q('B', yes_ask=0.55, schedule=no_maker_fee),
        ],
        maker=True,
    )
    assert result.best.fees == 0
    assert result.to_dict()['order_type'] == 'maker'


def test_missing_quotes_are_reported_and_basket_skipped():
    result = check_constraint(
        'complement', [q('A', yes_ask=0.40), q('B', yes_ask=0.55)]
    )
    assert [b.description for b in result.baskets] == ['YES on A + YES on B']
    assert 'A:no_ask' in result.missing_quotes


def test_input_validation():
    with pytest.raises(ValueError):
        check_constraint('implication', [q('A', yes_bid=0.5)])
    with pytest.raises(ValueError):
        check_constraint('exclusivity', [q('A', yes_bid=0.5)])
    with pytest.raises(ValueError):
        check_constraint('parity', [q('A'), q('B')])
    with pytest.raises(ValueError):
        check_constraint(
            'implication', [q('A', yes_bid=0.5), q('B', yes_ask=0.5)], contracts=0
        )


def test_to_dict_is_json_ready():
    import json

    result = check_constraint(
        'implication', [q('A', yes_bid=0.60), q('B', yes_ask=0.55)]
    )
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload['constraint'] == 'P(A) <= P(B)'
    assert payload['best']['net_edge_per_contract'] == pytest.approx(0.05)
    assert payload['assumptions']
