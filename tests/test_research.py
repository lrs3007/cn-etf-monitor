import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr

from backend.app.evaluation import build_assessment, evaluate_factor, forward_returns, rank_ic, rolling_rank_ic, walk_forward
from backend.app.factors import REGISTRY, calculate_all, rsi


@pytest.fixture
def frame():
    rng = np.random.default_rng(14)
    close = 1000 * np.exp(np.cumsum(rng.normal(.0002, .015, 1300)))
    return pd.DataFrame({"date": pd.bdate_range("2018-01-01", periods=len(close)).strftime("%Y-%m-%d"),
        "close": close, "open": close * .999, "high": close * 1.01, "low": close * .99,
        "volume": rng.uniform(1e6, 1e8, len(close)), "amount": rng.uniform(1e8, 1e10, len(close)),
        "pe_ttm": np.nan, "pb": np.nan})


def test_all_factors_are_prefix_invariant_and_cover_every_date(frame):
    full, prefix = calculate_all(frame), calculate_all(frame.iloc[:830])
    assert len(REGISTRY) == 19
    for key in REGISTRY:
        assert len(full[key][0]) == len(frame)
        pd.testing.assert_series_equal(full[key][0].iloc[:830], prefix[key][0])
        pd.testing.assert_series_equal(full[key][1].iloc[:830], prefix[key][1])


def test_warmup_and_missing_are_explicit(frame):
    calculated = calculate_all(frame)
    values, status = calculated["ma_bias_250"]
    assert values.iloc[:249].isna().all()
    assert (status.iloc[:249] == "warmup").all()
    assert values.iloc[249:].notna().all()
    assert (calculated["pe_ttm"][1] == "missing_source").all()


def test_missing_volume_is_not_filled(frame):
    frame.loc[100, "volume"] = np.nan
    values, status = calculate_all(frame)["volume_ratio_20"]
    assert values.iloc[100:121].isna().all()
    assert (status.iloc[100:121] == "missing_source").all()
    assert pd.notna(values.iloc[121])


def test_forward_labels_are_correct_and_never_filled(frame):
    values = forward_returns(frame.close, 20)
    assert values.iloc[0] == pytest.approx(frame.close.iloc[20] / frame.close.iloc[0] - 1)
    assert values.iloc[-20:].isna().all()


def test_rank_ic_matches_scipy(frame):
    x, y = frame.close, forward_returns(frame.close, 5)
    assert rank_ic(x, y) == pytest.approx(spearmanr(x[:-5], y[:-5]).statistic)
    assert rank_ic(np.ones(100), np.arange(100)) is None


def test_rolling_ic_is_exact_and_dates_are_label_maturity(frame):
    x = frame.close.to_numpy()
    y = forward_returns(frame.close, 20).to_numpy()
    rows = rolling_rank_ic(x, y, frame.date.to_numpy(), 20)
    assert rows[0]["as_of"] == frame.date.iloc[251 + 20]
    assert rows[-1]["as_of"] == frame.date.iloc[-1]
    assert rows[0]["ic"] == pytest.approx(rank_ic(x[:252], y[:252]))
    assert rows[-1]["ic"] == pytest.approx(rank_ic(x[-272:-20], y[-272:-20]))


def test_walk_forward_purges_boundary_and_uses_training_thresholds(frame):
    y = forward_returns(frame.close, 60).to_numpy()
    folds, ids = walk_forward(frame.close.to_numpy(), y, frame.date.to_numpy(), 60)
    assert len(folds) >= 2
    for fold in folds:
        train_end = frame.index[frame.date == fold["train_end"]][0]
        test_year_start = frame.index[frame.date.str.startswith(fold["year"])][0]
        assert train_end + 60 < test_year_start
        assert fold["train_samples"] >= 504
    assert np.all(np.isfinite(y[ids]))


def test_prelaunch_backcast_is_excluded_from_ic(frame):
    values = frame.close.pct_change(20) * 100
    launch = frame.date.iloc[400]
    result, _ = evaluate_factor(frame, values, 20, launch, "test")
    assert result["first_signal_date"] >= launch
    assert result["samples"] == len(frame) - 400 - 20
    assert result["include_backcast"] is False


def test_rsi_boundaries_and_flat_market():
    assert rsi(pd.Series(np.arange(1., 100.))).iloc[-1] == 100
    assert rsi(pd.Series(np.ones(100))).iloc[-1] == 50


def test_weak_or_uncertain_ic_cannot_drive_summary(frame):
    factors = calculate_all(frame)
    metadata = {key: f.metadata() for key, f in REGISTRY.items()}
    evaluations = {key: {"evaluation_start": frame.date.iloc[0], "oos_ic": .006,
        "oos_samples": 800, "direction_consistency": .9, "ci": {"lower": -.17, "upper": .18}}
        for key in REGISTRY}
    assessment = build_assessment(frame, factors, evaluations, metadata, 20)
    assert not any(e["eligible"] for e in assessment["evidence"])
    assert assessment["score"] is None
    assert "预测证据不足" in assessment["label"]
