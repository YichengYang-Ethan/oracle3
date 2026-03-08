"""Quantitative validation tools for market relations.

Provides statistical tests to confirm that a discovered relation
produces a tradable, mean-reverting spread:

- Cointegration (Engle-Granger two-step via OLS residual + ADF)
- Correlation analysis (Pearson)
- Hedge ratio estimation (OLS regression)
- Half-life calculation for mean reversion (Ornstein-Uhlenbeck)
- Lead-lag detection via cross-correlation on returns
- Structural constraint checking (implication, exclusivity, sum-to-one)

Adapted from prediction-market-cli for oracle3's market relation system.

Requires ``numpy`` and ``statsmodels`` (oracle3 already depends on both).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from oracle3.market.relations import ValidationResult

logger = logging.getLogger(__name__)


# ── Dependency helpers ────────────────────────────────────────────────────


def _require_numpy():  # type: ignore[return]
    """Lazy-import numpy and return the module."""
    try:
        import numpy as np
        return np
    except ImportError:
        raise ImportError(
            "numpy is required for quantitative validation. "
            "Install with: pip install numpy"
        ) from None


def _require_statsmodels() -> bool:
    try:
        import statsmodels  # noqa: F401
        return True
    except ImportError:
        raise ImportError(
            "statsmodels is required for quantitative validation. "
            "Install with: pip install statsmodels"
        ) from None


def _require_scipy() -> bool:
    try:
        import scipy  # noqa: F401
        return True
    except ImportError:
        raise ImportError(
            "scipy is required for correlation analysis. "
            "Install with: pip install scipy"
        ) from None


# ── Hedge Ratio ──────────────────────────────────────────────────────────


def estimate_hedge_ratio(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
) -> float:
    """Estimate the hedge ratio (beta) via OLS: prices_a = alpha + beta * prices_b.

    Returns beta.  For a simple spread, beta ~ 1.0 for same-event pairs.
    """
    np = _require_numpy()
    _require_statsmodels()
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant

    y = np.array(prices_a, dtype=float)
    x = add_constant(np.array(prices_b, dtype=float))
    result = OLS(y, x).fit()
    return float(result.params[1])


# ── Spread Construction ──────────────────────────────────────────────────


def compute_spread(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    hedge_ratio: float = 1.0,
) -> list[float]:
    """Compute the spread: s_t = prices_a_t - hedge_ratio * prices_b_t."""
    return [a - hedge_ratio * b for a, b in zip(prices_a, prices_b)]


# ── Stationarity Tests ───────────────────────────────────────────────────


def adf_test(
    series: Sequence[float],
    significance: float = 0.05,
) -> tuple[float, float, bool]:
    """Augmented Dickey-Fuller test for stationarity.

    Returns (test_statistic, p_value, is_stationary).
    """
    np = _require_numpy()
    _require_statsmodels()
    from statsmodels.tsa.stattools import adfuller

    arr = np.array(series, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 20:
        logger.warning("ADF test: insufficient data points (%d < 20)", len(arr))
        return 0.0, 1.0, False

    result = adfuller(arr, autolag="AIC")
    stat, pvalue = float(result[0]), float(result[1])
    return stat, pvalue, pvalue < significance


# ── Cointegration (Engle-Granger) ────────────────────────────────────────


def engle_granger_test(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    significance: float = 0.05,
) -> tuple[float, float, bool]:
    """Engle-Granger two-step cointegration test.

    Step 1: OLS regression prices_a ~ prices_b
    Step 2: ADF test on the residuals

    Returns (test_statistic, p_value, is_cointegrated).
    """
    np = _require_numpy()
    _require_statsmodels()
    from statsmodels.tsa.stattools import coint

    a = np.array(prices_a, dtype=float)
    b = np.array(prices_b, dtype=float)

    mask = ~(np.isnan(a) | np.isnan(b))
    a, b = a[mask], b[mask]
    if len(a) < 30:
        logger.warning("Cointegration test: insufficient data (%d < 30)", len(a))
        return 0.0, 1.0, False

    stat, pvalue, _crit = coint(a, b)
    return float(stat), float(pvalue), float(pvalue) < significance


# ── Half-Life (Ornstein-Uhlenbeck) ───────────────────────────────────────


def estimate_half_life(spread: Sequence[float]) -> float:
    """Estimate the half-life of mean reversion via OU process.

    Fits: delta_s_t = phi * (s_{t-1} - mean) + epsilon
    Half-life = -ln(2) / ln(1 + phi)

    Returns half-life in bars (NaN if not mean-reverting).
    """
    np = _require_numpy()
    _require_statsmodels()
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant

    s = np.array(spread, dtype=float)
    s = s[~np.isnan(s)]
    if len(s) < 10:
        return float("nan")

    delta = np.diff(s)
    lagged = s[:-1]
    x = add_constant(lagged)
    result = OLS(delta, x).fit()
    phi = float(result.params[1])

    if phi >= 0 or phi <= -1:
        return float("nan")  # not mean-reverting or unstable

    half_life = -math.log(2) / math.log(1 + phi)
    return half_life


# ── Correlation ──────────────────────────────────────────────────────────


def pearson_correlation(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
) -> float:
    """Compute Pearson correlation between two price series."""
    np = _require_numpy()
    a = np.array(prices_a, dtype=float)
    b = np.array(prices_b, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(b))
    a, b = a[mask], b[mask]
    if len(a) < 5:
        return 0.0
    # Use numpy corrcoef to avoid hard scipy dependency for this one
    corr = float(np.corrcoef(a, b)[0, 1])
    if np.isnan(corr):
        return 0.0
    return corr


# ── Lead-Lag Detection ───────────────────────────────────────────────────


def detect_lead_lag(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    max_lag: int = 10,
) -> tuple[int, float]:
    """Detect lead-lag via cross-correlation on returns.

    Computes corr(returns_a[t], returns_b[t+k]) for k in [-max_lag, max_lag].
    Positive result means A leads B by that many steps.

    Returns (optimal_lag, correlation_at_optimal_lag).
    """
    np = _require_numpy()
    a = np.array(prices_a, dtype=float)
    b = np.array(prices_b, dtype=float)
    n = min(len(a), len(b))
    if n < max_lag + 5:
        return 0, 0.0

    a, b = a[:n], b[:n]
    ra = np.diff(a)
    rb = np.diff(b)

    best_lag = 0
    best_corr = 0.0

    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x = ra[: len(ra) - lag] if lag > 0 else ra
            y = rb[lag:] if lag > 0 else rb
        else:
            x = ra[-lag:]
            y = rb[: len(rb) + lag]

        overlap = min(len(x), len(y))
        if overlap < 5:
            continue
        x, y = x[:overlap], y[:overlap]

        std_x = float(np.std(x))
        std_y = float(np.std(y))
        if std_x < 1e-12 or std_y < 1e-12:
            continue

        corr = float(np.corrcoef(x, y)[0, 1])
        if np.isnan(corr):
            continue
        if abs(corr) > abs(best_corr):
            best_corr = corr
            best_lag = lag

    return best_lag, best_corr


# ── Structural Constraint Validation ─────────────────────────────────────


def _check_constraint(
    a: list[float], b: list[float], constraint: str
) -> tuple[int, list[float], float]:
    """Check a single constraint.

    Returns (violation_count, violation_sizes, current_arb).
    """
    violations: list[float] = []
    for i in range(len(a)):
        if constraint == "A <= B":
            if a[i] > b[i]:
                violations.append(a[i] - b[i])
        elif constraint == "A >= B":
            if a[i] < b[i]:
                violations.append(b[i] - a[i])
        elif constraint == "A + B <= 1":
            total = a[i] + b[i]
            if total > 1.0:
                violations.append(total - 1.0)

    if constraint == "A <= B":
        current_arb = max(a[-1] - b[-1], 0.0)
    elif constraint == "A >= B":
        current_arb = max(b[-1] - a[-1], 0.0)
    elif constraint == "A + B <= 1":
        current_arb = max(a[-1] + b[-1] - 1.0, 0.0)
    else:
        current_arb = 0.0

    return len(violations), violations, current_arb


def _validate_structural(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    constraint: str = "A <= B",
) -> ValidationResult:
    """Validate a structural pricing constraint between two markets.

    Constraint types:
      'A <= B'     -- implication / nested timeframe (A implies B, so A <= B)
      'A >= B'     -- reverse implication
      'A + B <= 1' -- exclusivity (A and B cannot both be true)
      'auto'       -- tries A<=B and A>=B, picks the one with fewer violations
    """
    a = list(prices_a)
    b = list(prices_b)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]

    if n < 1:
        return ValidationResult(analysis_type="structural", constraint=constraint)

    # Auto-detect constraint direction for implication
    if constraint == "auto":
        if n >= 2:
            v_le, _, _ = _check_constraint(a, b, "A <= B")
            v_ge, _, _ = _check_constraint(a, b, "A >= B")
            constraint = "A <= B" if v_le <= v_ge else "A >= B"
        else:
            constraint = "A <= B"

    violation_count, violations, current_arb = _check_constraint(a, b, constraint)
    violation_rate = violation_count / n
    mean_arb = sum(violations) / len(violations) if violations else 0.0

    corr = pearson_correlation(a, b) if n >= 5 else None
    mean_spread = sum(ai - bi for ai, bi in zip(a, b)) / n
    std_spread = (
        sum((ai - bi - mean_spread) ** 2 for ai, bi in zip(a, b)) / n
    ) ** 0.5

    return ValidationResult(
        analysis_type="structural",
        constraint=constraint,
        constraint_holds=violation_count == 0,
        violation_count=violation_count,
        violation_rate=violation_rate,
        current_arb=current_arb,
        mean_arb=mean_arb,
        correlation=corr,
        mean_spread=mean_spread,
        std_spread=std_spread,
    )


# ── Lead-Lag Validation ──────────────────────────────────────────────────


def _validate_lead_lag(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    min_corr: float = 0.3,
) -> ValidationResult:
    """Validate whether A significantly leads or lags B.

    A lead-lag relationship is significant if |cross-correlation| > min_corr
    at a non-zero lag.
    """
    a = list(prices_a)
    b = list(prices_b)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]

    if n < 15:
        return ValidationResult(analysis_type="lead_lag")

    lag, lag_corr = detect_lead_lag(a, b)
    corr = pearson_correlation(a, b) if n >= 5 else None
    significant = lag != 0 and abs(lag_corr) > min_corr

    return ValidationResult(
        analysis_type="lead_lag",
        lead_lag=lag if lag != 0 else None,
        lead_lag_corr=lag_corr if lag != 0 else None,
        lead_lag_significant=significant,
        correlation=corr,
    )


# ── Cointegration Validation ─────────────────────────────────────────────


def _validate_cointegration(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    significance: float = 0.05,
) -> ValidationResult:
    """Validate spread mean-reversion via cointegration and stationarity tests.

    For relation types with no structural constraint (cointegration,
    conditional, etc.) the spread must be statistically mean-reverting.
    """
    a = list(prices_a)
    b = list(prices_b)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]

    if n < 30:
        logger.warning("Validation: insufficient data (%d < 30 points)", n)
        return ValidationResult(analysis_type="cointegration")

    hedge = estimate_hedge_ratio(a, b)
    spread = compute_spread(a, b, hedge_ratio=hedge)
    adf_stat, adf_p, is_stationary = adf_test(spread, significance)
    coint_stat, coint_p, is_coint = engle_granger_test(a, b, significance)
    hl = estimate_half_life(spread)
    corr = pearson_correlation(a, b)
    lag, lag_corr = detect_lead_lag(a, b)

    np = _require_numpy()
    spread_arr = np.array(spread, dtype=float)
    mean_s = float(np.nanmean(spread_arr))
    std_s = float(np.nanstd(spread_arr))

    return ValidationResult(
        analysis_type="cointegration",
        adf_statistic=adf_stat,
        adf_pvalue=adf_p,
        is_stationary=is_stationary,
        coint_statistic=coint_stat,
        coint_pvalue=coint_p,
        is_cointegrated=is_coint,
        half_life=hl if not math.isnan(hl) else None,
        hedge_ratio=hedge,
        correlation=corr,
        mean_spread=mean_s,
        std_spread=std_s,
        lead_lag=lag if lag != 0 else None,
        lead_lag_corr=lag_corr if lag != 0 else None,
    )


# ── Type-Dispatched Validation ───────────────────────────────────────────

# Maps spread_type -> (structural constraint string)
_STRUCTURAL_TYPES: dict[str, str] = {
    "same_event": "A <= B",
    "cross_platform": "A <= B",
    "implication": "auto",  # auto-detect A<=B or A>=B
    "complement": "A + B <= 1",
    "exclusivity": "A + B <= 1",
}

# Types that use cointegration analysis
_COINTEGRATION_TYPES = {"cointegration", "conditional", "structural"}


def validate_relation(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    spread_type: str,
    significance: float = 0.05,
) -> ValidationResult:
    """Run the appropriate validation tests based on relation type.

    Dispatches to the correct validation method:
    - same_event / cross_platform / implication: structural constraint
    - complement / exclusivity: structural constraint (sum-to-one)
    - cointegration / conditional / structural: Engle-Granger + ADF
    - Unknown types: default to cointegration analysis

    Args:
        prices_a: Price series for market A.
        prices_b: Price series for market B.
        spread_type: The relation's spread_type (see SPREAD_TYPES).
        significance: P-value threshold for statistical tests (default 0.05).

    Returns:
        ValidationResult with populated fields for the relevant tests.
    """
    if spread_type in _STRUCTURAL_TYPES:
        return _validate_structural(
            prices_a, prices_b, _STRUCTURAL_TYPES[spread_type]
        )

    if spread_type in _COINTEGRATION_TYPES:
        return _validate_cointegration(prices_a, prices_b, significance)

    # Unknown type -- default to cointegration
    logger.info(
        "Unknown spread_type %r, defaulting to cointegration validation",
        spread_type,
    )
    return _validate_cointegration(prices_a, prices_b, significance)
