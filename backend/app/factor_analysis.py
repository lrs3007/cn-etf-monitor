"""Descriptive, point-in-history position matching, separate from IC evaluation.

These statistics are retrospective conditional returns, not an out-of-sample
strategy or a forecast probability. Thresholds never depend on observed returns.
"""
import numpy as np
import pandas as pd

from .evaluation import finite


POSITION_WINDOW = 1260
MATCH_BAND = 5.0
MIN_REFERENCE_PERIODS = 30
ANALYSIS_VERSION = "position-match-v1"


def rolling_percentiles(values, minimum=60, window=POSITION_WINDOW):
    """Percent at or below today's value, including today; ties use upper rank."""
    return pd.Series(values, dtype=float).rolling(window, min_periods=minimum).rank(method="max", pct=True).to_numpy() * 100


def non_overlapping(indices, horizon):
    """Earliest-first fixed rule; no forward-return interval is counted twice.

    Neighbouring intervals may share a boundary price; this is not a guarantee
    of statistical independence (regime persistence can remain).
    """
    chosen, last = [], -horizon
    for index in indices:
        if index >= last + horizon:
            chosen.append(int(index))
            last = int(index)
    return np.asarray(chosen, dtype=int)


def return_summary(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"count": 0, "mean": None, "median": None, "win_rate": None, "p10": None, "p90": None}
    return {"count": len(values), "mean": finite(np.mean(values)), "median": finite(np.median(values)),
            "win_rate": finite(np.mean(values > 0)), "p10": finite(np.quantile(values, .1)),
            "p90": finite(np.quantile(values, .9))}


def mechanism(factor_id):
    if factor_id.startswith("momentum_"):
        days = factor_id.rsplit("_", 1)[1]
        return f"过去{days}个交易日的累计涨跌幅，描述已经发生的行情。高值表示此前上涨较多；未来可能延续，也可能反转，须由该指数、该期限的检验分别判断。"
    if factor_id.startswith("ma_bias_"):
        days = factor_id.rsplit("_", 1)[1]
        return f"收盘价相对{days}日均线的偏离。正值表示在均线上方，负值表示在下方；高分位代表偏离较高，不等于必然超买或即将下跌。"
    return {
        "ma_slope_20": "20日均线在过去5个交易日的变化率。正值表示均线向上，负值表示向下；均线有滞后，不能把已发生的上升直接当成未来收益。",
        "rsi_14": "用平滑后的上涨与下跌幅度衡量近14日的相对动量，范围0—100。70/30等传统阈值只是状态参考；强趋势中高位或低位可能维持。历史分位与RSI本身的分数不同。",
        "macd_hist": "MACD快慢均线之差相对其信号线的距离，再按指数点位标准化。正柱表示DIF高于DEA，负柱表示低于；仅凭当天柱值不能断言发生金叉、死叉或背离。",
        "volatility_20": "过去20日对数收益的年化标准差，衡量近期波动强度。高值说明涨跌更剧烈，并不直接提供上涨或下跌方向。年化波动率不是未来20日可能亏损的百分比。",
        "volatility_60": "过去60日对数收益的年化标准差，比20日指标更平滑。高值说明中期收益分布更分散；方向和未来下行风险需分别核对。",
        "atr_14": "考虑日内高低价与跳空后的真实波幅，经14日Wilder平滑并除以收盘价。衡量日常振幅，不是收益预测，也不是已回测的止损距离。",
        "drawdown_252": "当前收盘价相对过去252日最高收盘价的回撤，数值通常不大于0。越接近0越靠近一年高点；更负表示跌离高点更多，不等于更便宜，也不是滚动最大回撤。",
        "range_position_252": "当前价格在过去252日最低、最高收盘价之间的位置。0靠近区间低点，100靠近高点；这是价格区间位置，不是估值。该读数的历史分位是第二层比较，二者不可混用。",
        "volume_ratio_20": "当日指数成交量相对此前20日平均成交量的倍数。大于1为放量，小于1为缩量；成交量是双边撮合结果，不能视为净流入。它也不是某只ETF的成交量或份额变化。",
        "amount_ratio_20": "当日指数成交额相对此前20日平均成交额的倍数。反映交易活跃度，受价格和数量共同影响；放量上涨与放量下跌含义不同，单一倍数不识别资金方向。",
        "pe_ttm": "数据源提供的指数滚动市盈率，按项目设定的可用日对齐。较低可能对应较低定价，也可能源于盈利周期、行业权重或盈利质量变化；不能直接推出未来收益。",
        "pb": "数据源提供的指数市净率，比较价格与账面净资产。解释受行业、ROE和资产质量影响；没有原始历史数据时，不用指数价格反推。",
        "equity_bond_spread_10y": "指数盈利收益率100/PE-TTM减去同一观测日10年国债到期收益率，单位为百分点，保守按下一交易日可用。较高表示按此口径股票相对国债更有吸引力；不是实际股债超额收益，也不是预计收益率。",
    }.get(factor_id, "按已登记公式计算的历史状态指标，方向取决于该指数和预测期限的检验。")


def current_state(factor_id, value, percentile):
    if value is None:
        return "当天输入缺失或尚不可定义，不用较早读数代替当前值。"
    if factor_id.startswith("momentum_"):
        state = "观察区间已经上涨" if value > 0 else "观察区间已经下跌" if value < 0 else "观察区间涨跌持平"
    elif factor_id.startswith("ma_bias_"):
        state = "价格在对应均线上方" if value > 0 else "价格在对应均线下方" if value < 0 else "价格与对应均线持平"
    elif factor_id == "ma_slope_20":
        state = "20日均线向上" if value > 0 else "20日均线向下" if value < 0 else "20日均线持平"
    elif factor_id == "macd_hist":
        state = "DIF高于DEA" if value > 0 else "DIF低于DEA" if value < 0 else "DIF与DEA持平"
    elif factor_id == "rsi_14":
        state = "RSI处于70以上的较强动量区" if value >= 70 else "RSI处于30以下的较弱动量区" if value <= 30 else "RSI在30—70之间"
    elif factor_id in ("volume_ratio_20", "amount_ratio_20", "etf_amount_ratio_20"):
        state = "相对前20日均值放大" if value > 1 else "相对前20日均值收缩" if value < 1 else "等于前20日均值"
    elif factor_id.startswith("etf_shares_change_"):
        state = "代表ETF份额净增加" if value > 0 else "代表ETF份额净减少" if value < 0 else "代表ETF份额持平"
    elif factor_id == "etf_premium_discount":
        state = "收盘价相对同日净值溢价" if value > 0 else "收盘价相对同日净值折价" if value < 0 else "收盘价与净值相等"
    elif factor_id == "etf_turnover":
        state = f"代表ETF当日成交份额相当于总份额的{value:.2f}%"
    elif factor_id == "etf_nav_index_gap_20":
        state = "净值20日收益高于价格指数" if value > 0 else "净值20日收益低于价格指数" if value < 0 else "净值20日收益与价格指数相等"
    elif factor_id.startswith("breadth_"):
        state = ("多数成分股当日上涨" if value > 50 else "不超过半数成分股当日上涨") if factor_id == "breadth_advancing_ratio" else f"{value:.1f}%成分股在各自MA20上方"
    elif factor_id == "drawdown_252":
        state = "处在过去252日最高收盘价" if value == 0 else f"比过去252日最高收盘价低{abs(value):.2f}%"
    elif factor_id == "range_position_252":
        state = f"处在一年收盘价格区间的{value:.1f}%位置"
    elif factor_id == "equity_bond_spread_10y":
        state = "盈利收益率高于国债收益率" if value > 0 else "盈利收益率低于国债收益率" if value < 0 else "盈利收益率与国债收益率相等"
    else:
        state = "当前有效读数已取得"
    if percentile is None:
        return state + "；近1260个交易日内有效观测不足，暂不判断历史高低。"
    level = "较高区间" if percentile >= 80 else "较低区间" if percentile <= 20 else "中间区间"
    return state + f"；按近1260个交易日的有效观测比较，处于{level}。"


def relationship(evaluation):
    ic = evaluation.get("oos_ic")
    ci = evaluation.get("ci")
    consistency = evaluation.get("direction_consistency")
    if ic is None:
        return "有效样本外检验不足，不能判断稳定的未来收益关联方向。"
    direction = "较高因子值对应较高未来收益" if ic > 0 else "较高因子值对应较低未来收益" if ic < 0 else "未见单调关联"
    supported = bool(ci and (ci["lower"] > 0 or ci["upper"] < 0))
    uncertainty = "95%单项分块区间未跨0，但尚未校正多因子、多期限比较" if supported else "95%单项分块区间跨0或无法估计，方向证据不确定"
    stability = f"；年度训练/测试方向一致率为{consistency * 100:.0f}%" if consistency is not None else ""
    return f"年度滚动样本外Rank IC为{ic:+.3f}，历史上{direction}；{uncertainty}{stability}。这衡量排序关联，不是因果关系或收益幅度。"


def analyze_factor(dates, close, values, definition, evaluations, launch_date):
    dates, close, values = np.asarray(dates), np.asarray(close, dtype=float), np.asarray(values, dtype=float)
    if not len(dates) or not (len(dates) == len(close) == len(values)):
        raise ValueError("因子和价格必须覆盖相同、非空的交易日序列")
    if np.any(dates[1:] <= dates[:-1]) or np.any(~np.isfinite(close)) or np.any(close <= 0):
        raise ValueError("交易日须严格递增，价格须为正且有效")
    minimum = definition.get("min_percentile_samples", 60)
    percentiles = rolling_percentiles(values, minimum)
    current, position = finite(values[-1]), finite(percentiles[-1])
    bounds = [max(0., position - MATCH_BAND), min(100., position + MATCH_BAND)] if position is not None else None
    last_window = values[-POSITION_WINDOW:]
    window_valid = last_window[np.isfinite(last_window)]
    history = np.isfinite(percentiles) & (dates >= launch_date)
    matched = history & (percentiles >= bounds[0] - 1e-10) & (percentiles <= bounds[1] + 1e-10) if bounds else np.zeros(len(dates), bool)
    outcomes = []
    for horizon, evaluation in sorted(evaluations.items(), key=lambda item: int(item[0])):
        horizon = int(horizon)
        if horizon <= 0:
            raise ValueError("收益期限必须为正交易日数")
        target = np.full(len(close), np.nan)
        if horizon < len(close):
            target[:-horizon] = close[horizon:] / close[:-horizon] - 1
        eligible = history & np.isfinite(target)
        matches = np.flatnonzero(matched & eligible)
        selected = non_overlapping(matches, horizon)
        all_stats, sampled = return_summary(target[matches]), return_summary(target[selected])
        baseline = return_summary(target[eligible])
        # A calendar-phase comparison is useful as a robustness check; keep all
        # baseline dates too, rather than imply the two samples are paired trials.
        baseline_ids = non_overlapping(np.flatnonzero(eligible), horizon)
        baseline_sampled = return_summary(target[baseline_ids])
        years = sorted(set(str(dates[i])[:4] for i in selected))
        outcomes.append({"horizon": horizon, "all_matches": all_stats, "non_overlapping": sampled,
            "baseline": baseline, "baseline_non_overlapping": baseline_sampled,
            "mean_difference": finite(all_stats["mean"] - baseline["mean"]) if len(matches) else None,
            "win_rate_difference": finite(all_stats["win_rate"] - baseline["win_rate"]) if len(matches) else None,
            "non_overlapping_mean_difference": finite(sampled["mean"] - baseline_sampled["mean"]) if len(selected) else None,
            "sample_status": "no_matches" if not len(selected) else "limited" if len(selected) < MIN_REFERENCE_PERIODS else "descriptive",
            "first_signal": str(dates[matches[0]]) if len(matches) else None,
            "last_signal": str(dates[matches[-1]]) if len(matches) else None,
            "years": years, "relationship": relationship(evaluation),
            "full_ic": evaluation.get("full_ic"), "oos_ic": evaluation.get("oos_ic"),
            "recent_ic": evaluation.get("recent_ic"), "ci": evaluation.get("ci"),
            "examples": [{"signal_date": str(dates[i]), "end_date": str(dates[i + horizon]),
                "value": float(values[i]), "percentile": float(percentiles[i]), "return": float(target[i])} for i in selected],
        })
    return {"factor_id": definition["id"], "name": definition["name"], "category": definition["category"],
        "unit": definition["unit"], "as_of": str(dates[-1]), "current": current, "percentile": position,
        "percentile_samples": len(window_valid), "minimum_samples": minimum,
        "constant_window": bool(len(window_valid) and np.ptp(window_valid) == 0), "match_bounds": bounds,
        "mechanism": definition.get("explanation") or mechanism(definition["id"]), "current_state": current_state(definition["id"], current, position),
        "outcomes": outcomes}


def outcome_text(outcome):
    """Shared explanation for the standalone report and the live factor cards."""
    all_stats, sampled, base = outcome["all_matches"], outcome["non_overlapping"], outcome["baseline"]
    if not all_stats["count"]:
        return "没有符合当前分位区间且未来收益已完整成熟的历史样本，不作表现判断。"
    def pct(value):
        return f"{value * 100:+.2f}%"
    result = (f"历史相近位置后，未来{outcome['horizon']}日平均收益{pct(all_stats['mean'])}、中位数{pct(all_stats['median'])}，"
        f"上涨比例{all_stats['win_rate'] * 100:.2f}%；同期所有可比较日平均收益{pct(base['mean'])}，"
        f"两者均值相差{outcome['mean_difference'] * 100:+.2f}个百分点。"
        f"按固定规则选取不重叠观察期后剩{sampled['count']}段，平均收益{pct(sampled['mean'])}、上涨比例{sampled['win_rate'] * 100:.2f}%。")
    if sampled["count"] < MIN_REFERENCE_PERIODS:
        result += "不重叠观察期少于30段，只描述已发生的历史，参考有限。"
    else:
        result += "观察期仍可能集中在相同市场阶段，数量较多不代表结论已显著或稳定。"
    if (all_stats["mean"] > base["mean"]) != (sampled["mean"] > outcome["baseline_non_overlapping"]["mean"]):
        result += "全部匹配日与不重叠比较的相对方向不同，结果对取样方式敏感。"
    return result


def web_analysis(dates, close, values, definition, evaluations, launch_date):
    """A compact snapshot-bound payload; publishing never ships the full report.

    Compute from the same snapshot as the factor history, on local API reads and
    on every export. No independently refreshed prose cache can become stale.
    """
    result = analyze_factor(dates, close, values, definition, evaluations, launch_date)
    result["analysis_version"] = ANALYSIS_VERSION
    result["method"] = {"window_sessions": POSITION_WINDOW, "match_band_pp": MATCH_BAND,
                        "minimum_reference_periods": MIN_REFERENCE_PERIODS}
    for outcome in result["outcomes"]:
        outcome["interpretation"] = outcome_text(outcome)
        outcome["examples"] = outcome["examples"][-12:]
    return result
