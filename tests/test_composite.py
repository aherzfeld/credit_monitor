import pytest
from credit_monitor import IncompleteIndicatorsError, IndicatorResult, composite_score

# Weights as assigned in build_indicators; they sum to 1.0.
WEIGHTS = {
    "High-Yield OAS": 0.25,
    "Investment-Grade OAS": 0.10,
    "2s10s Yield Curve": 0.15,
    "SLOOS C&I Survey": 0.15,
    "Credit Card Delinquency": 0.10,
    "KRE/SPY 3-Month": 0.10,
    "EM Debt (EMB) 6-Month": 0.10,
    "HYG/LQD 3-Month": 0.05,
}


def _indicator(name: str, score: float) -> IndicatorResult:
    return IndicatorResult(name=name, value=0.0, score=score, weight=WEIGHTS[name], source="test")


def test_full_set_is_weighted_average():
    inds = [_indicator(n, 40.0 if n == "High-Yield OAS" else 20.0) for n in WEIGHTS]
    assert composite_score(inds) == pytest.approx(0.25 * 40 + 0.75 * 20)


def test_missing_indicators_raise_instead_of_renormalizing():
    # 2026-10-22: all five FRED series failed and the three Yahoo ones were
    # renormalized into a composite of 39.5 against ~23 the day before.
    yahoo_only = [
        _indicator("KRE/SPY 3-Month", 58),
        _indicator("EM Debt (EMB) 6-Month", 32),
        _indicator("HYG/LQD 3-Month", 17),
    ]
    with pytest.raises(IncompleteIndicatorsError, match="25%"):
        composite_score(yahoo_only)


def test_no_indicators_raise():
    with pytest.raises(IncompleteIndicatorsError):
        composite_score([])
