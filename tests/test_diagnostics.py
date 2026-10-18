import numpy as np
import pandas as pd
import pytest

from backend.app.diagnostics import adjust_fdr, predictive_diagnostics


def test_known_incremental_predictor_is_detected_out_of_sample():
    rng = np.random.default_rng(83)
    n = 1600
    signal = rng.normal(size=n)
    returns = .006 * np.roll(signal, 1) + rng.normal(0, .004, n)
    frame = pd.DataFrame({"date": pd.bdate_range("2017-01-01", periods=n).strftime("%Y-%m-%d"),
        "close": 1000 * np.exp(np.cumsum(returns))})
    result = predictive_diagnostics(frame, pd.Series(signal), "2017-01-01")
    assert result["status"] == "ok"
    assert result["forward"]["p_value"] < 1e-6
    assert result["oos"]["incremental_r2"] > .3
    for fold in result["oos"]["folds"]:
        assert fold["train_end"] < fold["test_start"]
        assert fold["train_samples"] >= 504


def test_fdr_adjustment_is_monotone_and_accounts_for_both_directions():
    def result(a, b):
        return {"forward": {"p_value": a}, "reverse": {"p_value": b}}
    results = adjust_fdr({"a": result(.01, .04), "b": {"forward": {"p_value": .03}, "reverse": None}})
    assert results["a"]["forward"]["q_value"] == pytest.approx(.03)
    assert results["a"]["reverse"]["q_value"] == pytest.approx(.04)
    assert results["b"]["forward"]["q_value"] == pytest.approx(.04)
    assert results["a"]["fdr_family_tests"] == 3


def test_constant_factor_produces_no_predictive_claim():
    rng = np.random.default_rng(9)
    frame = pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=700).strftime("%Y-%m-%d"),
        "close": 100 * np.exp(np.cumsum(rng.normal(0, .01, 700)))})
    result = predictive_diagnostics(frame, pd.Series(np.ones(700)), "2020-01-01")
    assert result["status"] == "constant_series"
    assert result["forward"] is None
