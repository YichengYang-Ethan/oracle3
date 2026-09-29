"""Published trading-fee schedules for binary event contracts.

Every constant below is taken from the venue's own documentation, retrieved on
2026-09-28. Fee schedules change, so prefer the per-market schedule each venue
exposes through its API (:meth:`KalshiSchedule.from_series`,
:meth:`PolymarketSchedule.from_gamma_market`) over the defaults.

Kalshi (Fee Schedule, "Last updated and effective: July 7, 2026")::

    taker fee = round_up(M * 0.07   * C * P * (1 - P))
    maker fee = round_up(M * 0.0175 * C * P * (1 - P))

``P`` is the contract price in dollars, ``C`` the number of contracts and ``M``
the series multiplier (API field ``fee_multiplier``; some series are set to 0).
Maker fees apply only to series whose ``fee_type`` includes maker fees. The
schedule defines "round up" as rounding so that fee plus position cost lands on
a centicent ($0.0001); its illustrative table rounds to the cent instead, which
:data:`CENT` reproduces. Kalshi charges no settlement fee.

Polymarket (docs.polymarket.com, "Trading fees")::

    taker fee = C * rate * p * (1 - p)

Makers are never charged. Fees are rounded to five decimal places with a
minimum of 0.00001 USDC. The rate is set per market (Gamma API field
``feeSchedule.rate``); the documented category defaults are in
:data:`POLYMARKET_CATEGORY_TAKER_RATES`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any, ClassVar, TypeAlias

__all__ = [
    'CENT',
    'CENTICENT',
    'KALSHI_MAKER_COEFFICIENT',
    'KALSHI_TAKER_COEFFICIENT',
    'POLYMARKET_CATEGORY_TAKER_RATES',
    'POLYMARKET_CONSERVATIVE_RATE',
    'FeeSchedule',
    'KalshiSchedule',
    'PolymarketSchedule',
    'UnsupportedFeeSchedule',
    'default_schedule',
]

CENT = Decimal('0.01')
CENTICENT = Decimal('0.0001')

KALSHI_TAKER_COEFFICIENT = Decimal('0.07')
KALSHI_MAKER_COEFFICIENT = Decimal('0.0175')

#: Documented taker-fee rates by market category (makers pay nothing).
POLYMARKET_CATEGORY_TAKER_RATES: dict[str, float] = {
    'crypto': 0.07,
    'sports': 0.05,
    'economics': 0.05,
    'culture': 0.05,
    'weather': 0.05,
    'other': 0.05,
    'finance': 0.04,
    'politics': 0.04,
    'mentions': 0.04,
    'tech': 0.04,
    'geopolitics': 0.0,
}

#: Highest documented Polymarket taker rate; used when a market's own schedule
#: is unknown so that arbitrage checks err on the side of higher costs.
POLYMARKET_CONSERVATIVE_RATE = 0.07

_POLYMARKET_INCREMENT = Decimal('0.00001')


class UnsupportedFeeSchedule(ValueError):
    """Raised when a venue reports a fee schedule this module cannot price."""


def _price(value: float | Decimal) -> Decimal:
    price = Decimal(str(value))
    if not Decimal('0') < price < Decimal('1'):
        raise ValueError(f'price must be strictly between 0 and 1, got {value}')
    return price


def _quantity(value: float | Decimal) -> Decimal:
    qty = Decimal(str(value))
    if qty <= 0:
        raise ValueError(f'quantity must be positive, got {value}')
    return qty


def _round_up(amount: Decimal, increment: Decimal) -> Decimal:
    if amount <= 0:
        return Decimal('0')
    steps = (amount / increment).to_integral_value(rounding=ROUND_CEILING)
    return steps * increment


@dataclass(frozen=True)
class KalshiSchedule:
    """Kalshi fee schedule for one series."""

    multiplier: Decimal = Decimal('1')
    maker_fees: bool = False
    increment: Decimal = CENTICENT

    venue: ClassVar[str] = 'kalshi'

    @classmethod
    def from_series(
        cls, series: Mapping[str, Any], *, increment: Decimal = CENTICENT
    ) -> KalshiSchedule:
        """Build the schedule from a ``GET /series/{ticker}`` payload.

        Combo series (``fee_type`` mentioning ``combo``) use a separate maker
        schedule that is not documented in machine-readable form, so they are
        rejected rather than priced with a guess.
        """
        fee_type = str(series.get('fee_type') or 'quadratic')
        if 'combo' in fee_type:
            raise UnsupportedFeeSchedule(
                f'Kalshi fee_type {fee_type!r} (combo markets) is not supported'
            )
        if not fee_type.startswith('quadratic'):
            raise UnsupportedFeeSchedule(f'unknown Kalshi fee_type {fee_type!r}')
        multiplier = series.get('fee_multiplier')
        return cls(
            multiplier=Decimal(str(1 if multiplier is None else multiplier)),
            maker_fees='maker' in fee_type,
            increment=increment,
        )

    def fee(
        self, price: float | Decimal, contracts: float | Decimal, *, maker: bool = False
    ) -> Decimal:
        """Fee in dollars for filling ``contracts`` at ``price``."""
        p = _price(price)
        c = _quantity(contracts)
        if maker and not self.maker_fees:
            return Decimal('0')
        coefficient = KALSHI_MAKER_COEFFICIENT if maker else KALSHI_TAKER_COEFFICIENT
        raw = self.multiplier * coefficient * c * p * (1 - p)
        return _round_up(raw, self.increment)

    def describe(self) -> dict[str, Any]:
        return {
            'venue': self.venue,
            'formula': 'round_up(M * k * C * P * (1 - P))',
            'taker_k': float(KALSHI_TAKER_COEFFICIENT),
            'maker_k': float(KALSHI_MAKER_COEFFICIENT) if self.maker_fees else 0.0,
            'multiplier_M': float(self.multiplier),
            'rounding_increment': float(self.increment),
        }


@dataclass(frozen=True)
class PolymarketSchedule:
    """Polymarket fee schedule for one market."""

    rate: Decimal = Decimal(str(POLYMARKET_CONSERVATIVE_RATE))
    source: str = 'conservative default'

    venue: ClassVar[str] = 'polymarket'

    @classmethod
    def from_gamma_market(cls, market: Mapping[str, Any]) -> PolymarketSchedule:
        """Build the schedule from a Gamma API market object."""
        if market.get('feesEnabled') is False:
            return cls(rate=Decimal('0'), source='feesEnabled=false')
        schedule = market.get('feeSchedule')
        if isinstance(schedule, Mapping):
            exponent = schedule.get('exponent', 1)
            if exponent not in (1, 1.0, None):
                raise UnsupportedFeeSchedule(
                    f'Polymarket feeSchedule exponent {exponent!r} is not documented'
                )
            if schedule.get('takerOnly') is False:
                raise UnsupportedFeeSchedule(
                    'Polymarket feeSchedule with maker fees is not documented'
                )
            return cls(
                rate=Decimal(str(schedule.get('rate', 0) or 0)),
                source=f"feeSchedule ({market.get('feeType') or 'unnamed'})",
            )
        return cls()

    @classmethod
    def for_category(cls, category: str) -> PolymarketSchedule:
        key = category.strip().lower()
        if key not in POLYMARKET_CATEGORY_TAKER_RATES:
            raise KeyError(
                f'unknown category {category!r}; expected one of '
                f'{sorted(POLYMARKET_CATEGORY_TAKER_RATES)}'
            )
        return cls(
            rate=Decimal(str(POLYMARKET_CATEGORY_TAKER_RATES[key])),
            source=f'documented category rate ({key})',
        )

    def fee(
        self, price: float | Decimal, shares: float | Decimal, *, maker: bool = False
    ) -> Decimal:
        """Fee in USDC for filling ``shares`` at ``price``."""
        p = _price(price)
        c = _quantity(shares)
        if maker or self.rate == 0:
            return Decimal('0')
        raw = c * self.rate * p * (1 - p)
        rounded = raw.quantize(_POLYMARKET_INCREMENT, rounding=ROUND_HALF_UP)
        return max(rounded, _POLYMARKET_INCREMENT)

    def describe(self) -> dict[str, Any]:
        return {
            'venue': self.venue,
            'formula': 'C * rate * p * (1 - p), takers only',
            'taker_rate': float(self.rate),
            'maker_rate': 0.0,
            'source': self.source,
        }


FeeSchedule: TypeAlias = KalshiSchedule | PolymarketSchedule


def default_schedule(venue: str) -> FeeSchedule:
    """Default schedule for a venue when its per-market schedule is unknown.

    Kalshi defaults to the standard multiplier of 1 with no maker fees.
    Polymarket defaults to the highest documented taker rate.
    """
    key = venue.strip().lower()
    if key == 'kalshi':
        return KalshiSchedule()
    if key == 'polymarket':
        return PolymarketSchedule()
    raise ValueError(f'no fee schedule for venue {venue!r}')
