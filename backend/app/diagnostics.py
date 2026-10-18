"""Incremental predictive evidence. These tests do not identify economic causality."""
import warnings

import numpy as np
import pandas as pd

from .evaluation import finite

LAGS = 5  # Prespecified; never choose the lag yielding the smallest p-value.


def _standardize(train, test=None):
    mean, std = train.mean(axis=0), train.std(axis=0)
    std = std.mask(std < 1e-12, 1)
    a = (train - mean) / std
    return a if test is None else (a, (test - mean) / std)


def _wald(y, own, candidates, volatility):
    import statsmodels.api as sm
    matrix = pd.concat([own, volatility, candidates], axis=1)
    matrix = sm.add_constant(_standardize(matrix), has_constant="add")
    if np.linalg.matrix_rank(matrix.to_numpy()) < matrix.shape[1]:
        return None
    model = sm.OLS(y.to_numpy(), matrix.to_numpy()).fit(cov_type="HAC", cov_kwds={"maxlags": LAGS})
    restriction = np.zeros((len(candidates.columns), matrix.shape[1]))
    restriction[:, -len(candidates.columns):] = np.eye(len(candidates.columns))
    test = model.wald_test(restriction, use_f=False, scalar=True)
    return {"p_value": finite(test.pvalue), "statistic": finite(test.statistic),
        "distribution": "chi2", "df": len(candidates.columns)}


def _incremental_oos(data, return_cols, factor_cols):
    """Annual expanding OLS, both models use identical observable rows and training sets."""
    import statsmodels.api as sm
    baseline_cols = return_cols + ["volatility_lag1"]
    augmented_cols = baseline_cols + factor_cols
    folds, base_errors, extra_errors = [], [], []
    years = data.date.str[:4]
    for year in sorted(years.unique()):
        train, test = data.loc[years < year], data.loc[years == year]
        if len(train) < 504 or len(test) < 30:
            continue
        bx, bt = _standardize(train[baseline_cols], test[baseline_cols])
        ax, at = _standardize(train[augmented_cols], test[augmented_cols])
        baseline = sm.OLS(train["r"].to_numpy(), sm.add_constant(bx.to_numpy(), has_constant="add")).fit()
        augmented = sm.OLS(train["r"].to_numpy(), sm.add_constant(ax.to_numpy(), has_constant="add")).fit()
        be = test["r"].to_numpy() - baseline.predict(sm.add_constant(bt.to_numpy(), has_constant="add"))
        ae = test["r"].to_numpy() - augmented.predict(sm.add_constant(at.to_numpy(), has_constant="add"))
        base_errors.extend((be ** 2).tolist())
        extra_errors.extend((ae ** 2).tolist())
        folds.append({"year": year, "train_samples": len(train), "test_samples": len(test),
            "train_end": train.date.iloc[-1], "test_start": test.date.iloc[0],
            "incremental_r2": finite(1 - np.sum(ae ** 2) / np.sum(be ** 2)) if np.sum(be ** 2) else None})
    r2 = finite(1 - np.sum(extra_errors) / np.sum(base_errors)) if base_errors and np.sum(base_errors) else None
    return {"incremental_r2": r2, "samples": len(base_errors), "folds": folds,
        "baseline": "收益滞后1–5日 + 前一日20日波动率", "augmented": "基线 + 因子变化滞后1–5日"}


def predictive_diagnostics(frame, values, launch_date):
    from statsmodels.tsa.stattools import adfuller
    data = pd.DataFrame({"date": frame.date.to_numpy(), "r": np.log(frame.close).diff().to_numpy(),
        "factor_change": values.diff().to_numpy()})
    data["volatility_lag1"] = data.r.rolling(20).std().shift(1)
    rcols, fcols = [], []
    for lag in range(1, LAGS + 1):
        rcol, fcol = f"return_lag{lag}", f"factor_lag{lag}"
        data[rcol], data[fcol] = data.r.shift(lag), data.factor_change.shift(lag)
        rcols.append(rcol)
        fcols.append(fcol)
    data = data.loc[data.date >= launch_date].replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    result = {"version": 1, "status": "insufficient_data", "samples": len(data), "lags": LAGS, "target_horizon": 1,
        "factor_transform": "预先固定一阶差分；不是原始因子水平", "forward": None, "reverse": None,
        "oos": None, "stationarity": None,
        "interpretation": "控制历史收益和波动率后的增量预测检验；不能证明经济因果。"}
    if len(data) < 504:
        return result
    if data.factor_change.std() < 1e-12 or data.r.std() < 1e-12:
        result["status"] = "constant_series"
        return result
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        fp = float(adfuller(data.factor_change, maxlag=LAGS, autolag=None, result_object=False)[1])
        rp = float(adfuller(data.r, maxlag=LAGS, autolag=None, result_object=False)[1])
    result["stationarity"] = {"factor_change_adf_p": fp, "return_adf_p": rp, "fixed_lags": LAGS}
    if fp >= .05 or rp >= .05:
        result["status"] = "stationarity_not_confirmed"
        return result
    try:
        result["forward"] = _wald(data.r, data[rcols], data[fcols], data[["volatility_lag1"]])
        result["reverse"] = _wald(data.factor_change, data[fcols], data[rcols], data[["volatility_lag1"]])
        result["oos"] = _incremental_oos(data, rcols, fcols)
        result["status"] = "ok" if result["forward"] is not None else "collinear_design"
        result["first_date"], result["last_date"] = data.date.iloc[0], data.date.iloc[-1]
    except (ValueError, np.linalg.LinAlgError):
        result["status"] = "model_not_estimable"
    return result


def adjust_fdr(results):
    """BH adjustment across all estimable forward AND reverse tests for one index."""
    tests = [(key, direction, result[direction]["p_value"]) for key, result in results.items()
        for direction in ("forward", "reverse") if result.get(direction) and result[direction]["p_value"] is not None]
    if tests:
        p = np.array([row[2] for row in tests])
        order = np.argsort(p)
        q = np.minimum.accumulate((p[order] * len(p) / np.arange(1, len(p) + 1))[::-1])[::-1].clip(0, 1)
        for position, adjusted in zip(order, q):
            key, direction, _ = tests[position]
            results[key][direction]["q_value"] = float(adjusted)
    for result in results.values():
        result["fdr_family_tests"] = len(tests)
        result["fdr_family"] = "本指数全部因子的双向预测检验；不跨指数合并"
    return results


def diagnose_all(frame, factors, launch_date):
    results = {}
    for key, (values, _) in factors.items():
        results[key] = predictive_diagnostics(frame, values, launch_date)
    return adjust_fdr(results)
