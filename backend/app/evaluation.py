"""Time-series factor research, not a transaction-cost-aware trading backtest."""
import hashlib
import warnings

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from .config import HORIZONS, SPREAD_FACTOR


def finite(value):
    return float(value) if value is not None and np.isfinite(value) else None


def rank_ic(x, y, min_samples=30):
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < min_samples:
        return None
    a, b = rankdata(x[mask]), rankdata(y[mask])
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    return finite(np.corrcoef(a, b)[0, 1])


def forward_returns(close, horizon):
    return close.shift(-horizon) / close - 1


def bootstrap_ci(x, y, horizon, seed=42, repetitions=199):
    x, y = np.asarray(x, float), np.asarray(y, float)
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    n = len(x)
    block = max(horizon, int(np.ceil(n ** (1 / 3))))
    if n < max(100, 5 * block) or np.std(x) == 0 or np.std(y) == 0:
        return None
    rng = np.random.default_rng(seed)
    estimates = []
    # Same random draws/block definition as the scalar implementation, batched ranks.
    for offset in range(0, repetitions, 16):
        starts = rng.integers(0, n - block + 1, size=(min(16, repetitions - offset), int(np.ceil(n / block))))
        ids = (starts[:, :, None] + np.arange(block)).reshape(len(starts), -1)[:, :n]
        a, b = rankdata(x[ids], axis=1), rankdata(y[ids], axis=1)
        a -= a.mean(axis=1, keepdims=True)
        b -= b.mean(axis=1, keepdims=True)
        with np.errstate(invalid="ignore", divide="ignore"):
            correlations = (a * b).sum(axis=1) / np.sqrt((a * a).sum(axis=1) * (b * b).sum(axis=1))
        estimates.extend(correlations[np.isfinite(correlations)].tolist())
    if len(estimates) < repetitions * 0.9:
        return None
    lo, hi = np.quantile(estimates, [0.025, 0.975])
    return {"lower": float(lo), "upper": float(hi), "level": 0.95,
        "method": "moving_block_bootstrap", "block_length": block, "repetitions": repetitions,
        "note": "单项区间，未作多因子/多期限校正；用于不确定性展示"}


def rolling_rank_ic(x, y, dates, horizon, window=252, min_samples=200, start_after=None):
    """Window ends at signal t; the result is available only when label t+h matures."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < window + horizon:
        return []
    points = []
    xs = np.lib.stride_tricks.sliding_window_view(x, window)
    ys = np.lib.stride_tricks.sliding_window_view(y, window)
    start = max(0, int(np.searchsorted(np.asarray(dates), start_after, side="right")) - (window - 1 + horizon)) if start_after else 0
    for offset in range(start, len(xs) - horizon, 256):
        a = xs[offset:min(offset + 256, len(xs) - horizon)].copy()
        b = ys[offset:min(offset + 256, len(xs) - horizon)].copy()
        mask = np.isfinite(a) & np.isfinite(b)
        a[~mask], b[~mask] = np.nan, np.nan
        count = mask.sum(axis=1)
        with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            ra, rb = rankdata(a, axis=1, nan_policy="omit"), rankdata(b, axis=1, nan_policy="omit")
            ra -= np.nanmean(ra, axis=1, keepdims=True)
            rb -= np.nanmean(rb, axis=1, keepdims=True)
            correlation = np.nansum(ra * rb, axis=1) / np.sqrt(np.nansum(ra ** 2, axis=1) * np.nansum(rb ** 2, axis=1))
        for j, (ic, n) in enumerate(zip(correlation, count)):
            end = offset + j + window - 1
            points.append({"as_of": str(dates[end + horizon]),
                "ic": finite(ic) if n >= min_samples else None, "sample_count": int(n)})
    return points


def walk_forward(x, y, dates, horizon, min_train=504, memo=None):
    """Annual expanding training; remove labels crossing each test boundary."""
    x, y, dates = np.asarray(x, float), np.asarray(y, float), np.asarray(dates)
    years = np.array([str(d)[:4] for d in dates])
    folds, oos_indices = [], []
    for year in np.unique(years):
        test = np.where((years == year) & np.isfinite(x) & np.isfinite(y))[0]
        year_start = int(np.where(years == year)[0][0])
        train = np.where((np.arange(len(x)) + horizon < year_start) & np.isfinite(x) & np.isfinite(y))[0]
        if len(train) < min_train or len(test) < 30:
            continue
        def calculate():
            train_ic, test_ic = rank_ic(x[train], y[train]), rank_ic(x[test], y[test])
            if train_ic is None:
                return None
            thresholds = np.quantile(x[train], [0.2, 0.4, 0.6, 0.8])
            buckets = np.searchsorted(thresholds, x[test], side="right")
            return {"year": str(year), "train_ic": train_ic, "test_ic": test_ic,
                "train_samples": len(train), "test_samples": len(test),
                "train_end": str(dates[train[-1]]), "test_start": str(dates[test[0]]),
                "direction": 1 if train_ic >= 0 else -1,
                "buckets": buckets.tolist(), "test_indices": test.tolist()}
        closed = int(np.where(years == year)[0][-1]) + horizon < len(x)
        fold = memo.compute(f"walk_forward/{horizon}/{min_train}/{year}",
            [train, test, x[train], y[train], x[test], y[test], dates[train].astype("U10"), dates[test].astype("U10")], calculate) if memo and closed else calculate()
        if fold is None:
            continue
        folds.append(fold)
        oos_indices.extend(test.tolist())
    return folds, np.array(oos_indices, dtype=int)


def evaluate_factor(frame, values, horizon, launch_date, factor_id, previous_rolling=None, memo=None):
    # All factors retain backcast history; research defaults to published-index history.
    keep = np.asarray(frame.date >= launch_date)
    dates = frame.date.to_numpy()[keep]
    x = values.to_numpy(dtype=float)[keep]
    y = forward_returns(frame.close, horizon).to_numpy()[keep]
    valid = np.isfinite(x) & np.isfinite(y)
    full_ic = rank_ic(x, y)
    folds, oos = walk_forward(x, y, dates, horizon, memo=memo)
    seed = int(hashlib.sha256(factor_id.encode()).hexdigest()[:8], 16) + horizon
    oos_ic = rank_ic(x[oos], y[oos]) if len(oos) else None
    ci = bootstrap_ci(x[oos], y[oos], horizon, seed=seed) if len(oos) else None
    years = np.array([str(d)[:4] for d in dates])
    yearly = []
    for year in np.unique(years):
        ids = np.where(years == year)[0]
        def statistic():
            return {"year": str(year), "ic": rank_ic(x[ids], y[ids]), "samples": int(valid[ids].sum())}
        closed = ids[-1] + horizon < len(x)
        yearly.append(memo.compute(f"yearly/{horizon}/{year}", [x[ids], y[ids]], statistic) if memo and closed else statistic())
    grouped = [[] for _ in range(5)]
    for fold in folds:
        for bucket, idx in zip(fold["buckets"], fold["test_indices"]):
            grouped[bucket].append(y[idx])
    buckets = [{"bucket": i + 1, "count": len(group), "mean_return": finite(np.mean(group)) if group else None,
        "median_return": finite(np.median(group)) if group else None,
        "win_rate": float(np.mean(np.array(group) > 0)) if group else None} for i, group in enumerate(grouped)]
    matching = [np.sign(f["train_ic"]) == np.sign(f["test_ic"]) for f in folds if f["test_ic"] is not None]
    for fold in folds:
        fold.pop("buckets")
        fold.pop("test_indices")
    lag = [{"lag": k, "ic": rank_ic(pd.Series(x).shift(k).to_numpy(), y)} for k in (-20, -10, -5, 0, 5, 10, 20)]
    mature = np.where(np.isfinite(y))[0]
    result = {
        "factor_id": factor_id, "horizon": horizon, "full_ic": full_ic, "oos_ic": oos_ic,
        "samples": int(valid.sum()), "oos_samples": int(len(oos)), "ci": ci,
        "first_signal_date": str(dates[np.where(valid)[0][0]]) if valid.any() else None,
        "last_signal_date": str(dates[np.where(valid)[0][-1]]) if valid.any() else None,
        "labels_complete_through": str(dates[mature[-1]]) if len(mature) else None,
        "yearly": yearly, "folds": folds, "buckets": buckets, "lag": lag,
        "direction_consistency": finite(np.mean(matching)) if matching else None,
        "method": "time_series_spearman", "return_basis": "price_index_close_to_close",
        "evaluation_start": launch_date, "include_backcast": False, "window": 252,
        "multiple_testing": "未进行显著性筛选；全部预设因子与期限均展示，置信区间未校正多重检验",
        "walk_forward": "年度扩展训练，至少504个有效训练样本，边界剔除跨期标签；分组阈值仅由训练集确定",
    }
    cached = previous_rolling or []
    rolling = cached + rolling_rank_ic(x, y, dates, horizon, start_after=cached[-1]["as_of"] if cached else None) if valid.any() else []
    result["recent_ic"] = next((point["ic"] for point in reversed(rolling) if point["ic"] is not None), None)
    return result, rolling


def build_assessment(frame, results, evaluations, metadata, horizon):
    evidence = []
    for key, (values, _) in results.items():
        evaluation = evaluations[key]
        current = finite(values.iloc[-1])
        historical = values.iloc[-1260:].dropna()
        minimum = metadata[key].get("min_percentile_samples", 60)
        percentile = float((historical <= current).mean() * 100) if current is not None and len(historical) >= minimum else None
        # Current direction learned only from matured historical labels, never future observations.
        train = values.iloc[:-horizon] if horizon else values
        target = forward_returns(frame.close, horizon).iloc[:-horizon]
        published = frame.date.iloc[:-horizon] >= evaluation["evaluation_start"]
        direction_ic = rank_ic(train[published], target[published])
        oos = evaluation["oos_ic"]
        consistency = evaluation["direction_consistency"]
        ci = evaluation.get("ci")
        interval_supports_direction = bool(ci and (ci["lower"] > 0 or ci["upper"] < 0))
        eligible = (metadata[key].get("assessment_enabled", True) and current is not None and percentile is not None and direction_ic is not None
            and oos is not None and evaluation["oos_samples"] >= 252 and consistency is not None
            and consistency >= 0.6 and abs(oos) >= .03 and interval_supports_direction
            and np.sign(direction_ic) == np.sign(oos))
        score = ((percentile - 50) / 50) * np.sign(direction_ic) if eligible else None
        evidence.append({"factor_id": key, "name": metadata[key]["name"], "category": metadata[key]["category"],
            "current": current, "percentile": percentile, "direction_ic": direction_ic, "oos_ic": oos,
            "eligible": bool(eligible), "score": finite(score),
            "stance": "偏强" if eligible and score > 0.2 else "偏弱" if eligible and score < -0.2 else "中性" if eligible else ("估值观察" if metadata[key]["category"] == "估值" else metadata[key].get("purpose", "研究观察")) if not metadata[key].get("assessment_enabled", True) else "证据不足"})
    categories = {}
    for category in dict.fromkeys(item["category"] for item in evidence):
        eligible = [e["score"] for e in evidence if e["category"] == category and e["eligible"]]
        categories[category] = {"score": finite(np.mean(eligible)) if eligible else None,
            "count": len(eligible), "stance": "证据不足" if not eligible else "偏强" if np.mean(eligible) > .2 else "偏弱" if np.mean(eligible) < -.2 else "中性"}
    scores = [v["score"] for v in categories.values() if v["score"] is not None]
    eligible_count = sum(e["eligible"] for e in evidence)
    overall = float(np.mean(scores)) if len(scores) >= 2 and eligible_count >= 3 else None
    label = "证据不足" if overall is None else "温和偏强" if overall > .2 else "温和偏弱" if overall < -.2 else "多空分歧 · 中性观察"
    supporting = sorted([e for e in evidence if e["eligible"] and abs(e["score"]) > .2], key=lambda e: abs(e["score"]), reverse=True)
    last = frame.iloc[-1]
    bias20 = finite(results["ma_bias_20"][0].iloc[-1])
    bias60 = finite(results["ma_bias_60"][0].iloc[-1])
    trend = ("短中期趋势偏弱" if bias20 is not None and bias60 is not None and bias20 < 0 and bias60 < 0
        else "短中期趋势偏强" if bias20 is not None and bias60 is not None and bias20 > 0 and bias60 > 0 else "短中期趋势分化")
    if overall is None:
        label = f"{trend} · 预测证据不足"
    spread = next((e for e in evidence if e["factor_id"] == SPREAD_FACTOR), None)
    relative_value = None
    if spread:
        level = spread["percentile"]
        relative_value = {"current": spread["current"], "percentile": level, "unit": "百分点",
            "text": "股债收益差输入缺失，暂不能判断相对估值。" if spread["current"] is None else
                "股债收益差已有读数，5年窗口有效样本不足756日，暂不判断历史位置。" if level is None else
                f"股债收益差处于近5年{level:.1f}%分位，按此口径股票相对国债的吸引力"
                + ("较高" if level >= 80 else "较低" if level <= 20 else "处于历史中间区间")
                + "；这是估值观察，不参与综合预测评分。"}
    return {"label": label, "score": overall, "horizon": horizon, "as_of": str(last.date), "assessment_version": "5",
        "relative_value": relative_value,
        "headline": f"{horizon}日观察：{label}",
        "text": f"当前{trend}。历史样本外检验中，{eligible_count}个因子、{len(scores)}个类别符合方向稳定性条件。"
            + ("至少需要3个因子且覆盖2类证据，当前尚不足以形成综合预测方向。" if overall is None else f"按当前因子分位汇总，未来{horizon}日观察为{label}；同类先平均，再按类别等权。"),
        "categories": categories, "evidence": evidence, "highlights": supporting[:4],
        "limitations": ["综合状态为研究性启发式汇总，组合本身尚未进行独立样本外验证。",
            "IC衡量历史关联；价格衍生因子及领先滞后关系不构成经济因果证明。",
            "使用第三方当前历史版本，不具有历史数据修订前的点时快照。",
            "研究收益为收盘到未来收盘；实盘策略需另以次日可成交价格及费用验证。"],
        "last_price": float(last.close), "day_change": float(frame.close.iloc[-1] / frame.close.iloc[-2] - 1) if len(frame) > 1 else None}
