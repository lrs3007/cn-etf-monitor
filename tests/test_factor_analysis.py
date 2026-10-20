import numpy as np
import pytest

from backend.app.factor_analysis import analyze_factor, non_overlapping, rolling_percentiles, web_analysis


def definition(minimum=3):
    return {"id": "momentum_20", "name": "20日动量", "category": "动量", "unit": "%", "min_percentile_samples": minimum}


def test_percentiles_match_trailing_ecdf_and_do_not_use_future():
    values = np.array([1, 2, np.nan, 2, 0, 9, 4, 3, 2, np.nan, 5], float)
    actual = rolling_percentiles(values, minimum=3, window=5)
    for i, value in enumerate(values):
        trailing = values[max(0, i - 4):i + 1]
        trailing = trailing[np.isfinite(trailing)]
        expected = np.mean(trailing <= value) * 100 if len(trailing) >= 3 and np.isfinite(value) else np.nan
        assert actual[i] == pytest.approx(expected, nan_ok=True)
    np.testing.assert_allclose(actual[:7], rolling_percentiles(values[:7], 3, 5), equal_nan=True)


def test_matching_uses_historical_positions_not_current_value_thresholds():
    dates = np.array([f"2026-01-{i:02}" for i in range(1, 13)])
    close = np.arange(100., 112.)
    values = np.arange(1., 13.)
    result = analyze_factor(dates, close, values, definition(), {2: {}}, dates[4])
    outcome = result["outcomes"][0]
    # Every rising value was at its then-highest position, though none equalled
    # today's raw value. Pre-launch dates and immature last two labels excluded.
    assert result["percentile"] == 100
    assert outcome["all_matches"]["count"] == 6
    assert outcome["non_overlapping"]["count"] == 3
    assert [e["signal_date"] for e in outcome["examples"]] == dates[[4, 6, 8]].tolist()
    assert outcome["all_matches"]["mean"] == pytest.approx(np.mean(close[6:12] / close[4:10] - 1))
    assert all(e["end_date"] <= dates[-1] for e in outcome["examples"])
    assert outcome["sample_status"] == "limited"


def test_non_overlapping_is_earliest_first_and_retains_calendar_spacing():
    assert non_overlapping([0, 1, 2, 3, 4, 10, 11, 12], 3).tolist() == [0, 3, 10]


@pytest.mark.parametrize("last", [None, float("nan")])
def test_missing_current_never_falls_back_to_old_signal(last):
    result = analyze_factor(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
        [100, 101, 102, 103], [1, 2, 3, last], definition(2), {1: {}}, "2026-01-01")
    assert result["current"] is None
    assert result["percentile"] is None
    assert result["outcomes"][0]["all_matches"]["count"] == 0
    assert result["outcomes"][0]["all_matches"]["mean"] is None


def test_long_horizon_and_insufficient_percentiles_have_no_fabricated_results():
    result = analyze_factor(["2026-01-01", "2026-01-02", "2026-01-03"], [100, 102, 101],
        [1, 1, 1], definition(3), {20: {}}, "2026-01-01")
    assert result["constant_window"]
    assert result["outcomes"][0]["sample_status"] == "no_matches"
    result = analyze_factor(["2026-01-01", "2026-01-02"], [100, 101], [1, 2], definition(3), {1: {}}, "2026-01-01")
    assert result["percentile"] is None


def test_unsorted_or_invalid_price_data_is_rejected():
    with pytest.raises(ValueError):
        analyze_factor(["2026-01-02", "2026-01-01"], [100, 101], [1, 2], definition(2), {1: {}}, "2026-01-01")
    with pytest.raises(ValueError):
        analyze_factor(["2026-01-01"], [0], [1], definition(2), {1: {}}, "2026-01-01")


def test_web_analysis_recomputes_with_new_data_and_limits_only_examples():
    dates = np.array([str(i).zfill(4) for i in range(100)])
    values = np.arange(100.)
    close = 100 + values
    first = web_analysis(dates[:-1], close[:-1], values[:-1], definition(), {1: {}, 20: {}}, dates[0])
    second = web_analysis(dates, close, values, definition(), {1: {}, 20: {}}, dates[0])
    raw = analyze_factor(dates, close, values, definition(), {1: {}, 20: {}}, dates[0])
    assert first["as_of"] != second["as_of"]
    assert second["current"] == 99
    assert second["outcomes"][0]["all_matches"]["count"] == first["outcomes"][0]["all_matches"]["count"] + 1
    assert second["outcomes"][0]["all_matches"] == raw["outcomes"][0]["all_matches"]
    assert len(second["outcomes"][0]["examples"]) == 12
    assert "未来20日" in second["outcomes"][1]["interpretation"]
    assert "少于30段" in second["outcomes"][1]["interpretation"]
    assert second["analysis_version"] == "position-match-v1"
