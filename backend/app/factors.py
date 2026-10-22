"""Versioned registry. A factor returns one value for every historical session."""
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .config import SPREAD_FACTOR, factor_horizons


@dataclass(frozen=True)
class Factor:
    id: str
    name: str
    category: str
    unit: str
    lookback: int
    inputs: tuple
    formula: str
    description: str
    compute: Callable = field(repr=False, compare=False)
    version: int = 1
    assessment_enabled: bool = True
    min_percentile_samples: int = 60
    source_group: str | None = None
    purpose: str | None = None
    explanation: str | None = None

    def metadata(self):
        result = {k: v for k, v in asdict(self).items() if k != "compute"}
        if self.assessment_enabled:
            result.pop("assessment_enabled")
        if self.min_percentile_samples == 60:
            result.pop("min_percentile_samples")
        result["horizons"] = factor_horizons(self.id)
        for key in ("source_group", "purpose", "explanation"):
            if result[key] is None:
                result.pop(key)
        return result


REGISTRY: dict[str, Factor] = {}


def register(factor):
    if factor.id in REGISTRY:
        raise ValueError(f"重复因子ID: {factor.id}")
    REGISTRY[factor.id] = factor
    return factor


def rsi(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    result = 100 - 100 / (1 + up / down.replace(0, np.nan))
    result = result.mask((down == 0) & (up > 0), 100)
    return result.mask((down == 0) & (up == 0), 50)


def macd_hist(close):
    dif = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
    dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
    return 2 * (dif - dea) / close * 100


for window in (5, 20, 60):
    register(Factor(f"momentum_{window}", f"{window}日动量", "动量", "%", window + 1, ("close",),
        f"(close / close.shift({window}) - 1) × 100", "衡量指数在指定交易日区间的累计涨跌，正负预测关系由历史检验确定。",
        lambda f, w=window: f.close.pct_change(w, fill_method=None) * 100))

for window in (20, 60, 120, 250):
    register(Factor(f"ma_bias_{window}", f"MA{window}乖离率", "趋势", "%", window, ("close",),
        f"(close / SMA(close, {window}) - 1) × 100", "衡量价格偏离均线的程度，可研究趋势延续与均值回归。",
        lambda f, w=window: (f.close / f.close.rolling(w, min_periods=w).mean() - 1) * 100))

register(Factor("ma_slope_20", "20日均线斜率", "趋势", "%", 25, ("close",),
    "(MA20 / MA20.shift(5) - 1) × 100", "20日均线在5个交易日内的变化率。",
    lambda f: f.close.rolling(20).mean().pct_change(5, fill_method=None) * 100))
register(Factor("rsi_14", "RSI · 14日", "动量", "分", 15, ("close",),
    "Wilder EWMA(up,14) / (EWMA(up,14)+EWMA(down,14)) × 100", "观察上涨与下跌动量的相对强弱，范围0到100。",
    lambda f: rsi(f.close)))
register(Factor("macd_hist", "MACD标准化柱", "趋势", "%", 34, ("close",),
    "2 × (EMA12 − EMA26 − DEA9) / close × 100", "对MACD柱按指数点位标准化，使不同时期可比较。",
    lambda f: macd_hist(f.close)))
for window in (20, 60):
    register(Factor(f"volatility_{window}", f"{window}日年化波动率", "风险", "%", window + 1, ("close",),
        f"std(log(close/prev_close), {window}, ddof=1) × √252 × 100", "刻画收益波动强度，不能单独解释为上涨或下跌预测。",
        lambda f, w=window: np.log(f.close / f.close.shift(1)).rolling(w).std(ddof=1) * np.sqrt(252) * 100))
register(Factor("atr_14", "ATR · 14日", "风险", "%", 15, ("close", "high", "low"),
    "Wilder EWMA(max(high-low,abs(high-prev_close),abs(low-prev_close)),14) / close × 100",
    "包含日内振幅与跳空的波动度，以指数点位标准化。",
    lambda f: pd.concat([f.high-f.low, (f.high-f.close.shift()).abs(), (f.low-f.close.shift()).abs()], axis=1)
        .max(axis=1).mask(f.close.shift().isna()).ewm(alpha=1/14, adjust=False, min_periods=14).mean() / f.close * 100))
register(Factor("drawdown_252", "距一年高点回撤", "风险", "%", 252, ("close",),
    "(close / rolling_max(close,252) - 1) × 100", "当前价格距离过去252日最高收盘价的跌幅，并非滚动最大回撤。",
    lambda f: (f.close / f.close.rolling(252).max() - 1) * 100))
register(Factor("range_position_252", "一年价格区间位置", "趋势", "%", 252, ("close",),
    "(close − min252) / (max252 − min252) × 100", "当前收盘价在过去252日收盘价区间的位置。",
    lambda f: (f.close - f.close.rolling(252).min()) / (f.close.rolling(252).max() - f.close.rolling(252).min()).replace(0, np.nan) * 100))
for column, label in (("volume", "成交量"), ("amount", "成交额")):
    register(Factor(f"{column}_ratio_20", f"{label}放大倍数", "量价", "倍", 21, (column,),
        f"{column} / mean({column}.shift(1),20)", "当日数值相对于此前20个交易日的均值；缺失交易量/额不补零。",
        lambda f, c=column: f[c] / f[c].shift(1).rolling(20, min_periods=20).mean()))
for column, label in (("pe_ttm", "PE-TTM"), ("pb", "PB")):
    register(Factor(column, label, "估值", "倍", 1, (column,),
        "数据源提供值，按可用日期对齐，不前向填充", "依赖额外估值数据；无数据时明确缺失，不用指数点位推算估值。",
        lambda f, c=column: f[c]))

register(Factor(SPREAD_FACTOR, "股债收益差 · 10年国债", "估值", "百分点", 1,
    ("spread_pe_ttm", "cgb_10y_yield"), "100 / PE-TTM − 10年期国债到期收益率(%)",
    "同一观测日的指数盈利收益率减去中债10年国债到期收益率。保守按下一交易日可用，不填补缺失；数值越高表示按此口径股票相对国债更有吸引力，不代表预期收益。首版不参与综合评分。",
    lambda f: 100 / f.spread_pe_ttm.where(f.spread_pe_ttm > 0) - f.cgb_10y_yield,
    assessment_enabled=False, min_percentile_samples=756))

# External series are calculated on their original observation dates, then made
# eligible on the next session (or later disclosed date) before reaching here.
for fid, name, category, unit, formula, explanation, group, purpose in (
    ("etf_shares_change_5", "ETF · 5日份额变化率", "资金申赎", "%", "(份额_t / 份额_t-5 − 1) × 100",
     "代表ETF连续6个指数交易日的实际总份额变化，剔除疑似份额折算区间。正值表示净份额增加，负值表示减少；不是二级市场成交额，也不等同现金净流入，不能直接解释为看涨或看跌。", "etf", "收益关联研究"),
    ("etf_shares_change_20", "ETF · 20日份额变化率", "资金申赎", "%", "(份额_t / 份额_t-20 − 1) × 100",
     "代表ETF连续21个指数交易日的总份额变化率，观察较长时间的申赎方向；不把零散披露的份额前向填充，不代表同指数全部ETF的资金流。", "etf", "收益关联研究"),
    ("etf_turnover", "ETF · 日份额换手率", "ETF流动性", "%", "当日成交份额 / 同日总份额 × 100",
     "代表ETF成交量占其总份额的比例，衡量交易活跃度；同一份额可以反复成交，数值可超过100%。高换手不等于净申购或流动性一定更好，也不代表价格上涨。", "etf", "交易观察"),
    ("etf_amount_ratio_20", "ETF · 成交额放大倍数", "ETF流动性", "倍", "ETF成交额_t / mean(ETF成交额_t-20:t-1)",
     "代表ETF当日成交额相对此前20个指数交易日均值的倍数。与现有指数成交额因子分开记录；反映场内活跃度，不是资金净流入，也不能替代买卖价差或冲击成本。", "etf", "收益关联研究"),
    ("etf_premium_discount", "ETF · 收盘折溢价率", "ETF特征", "%", "(ETF不复权收盘价 / 同日单位净值 − 1) × 100",
     "同一观测日的代表ETF不复权收盘价相对单位净值的偏离，正值为溢价、负值为折价。净值通常收盘后披露，本项目保守按下一交易日可用；不是盘中IOPV折溢价，折价也不等于底层指数估值便宜。", "etf", "交易观察"),
    ("etf_nav_index_gap_20", "ETF · 20日净值收益偏离", "ETF特征", "百分点", "[∏(1+ETF披露净值日增长率,20) − 指数close_t/close_t-20] × 100",
     "代表ETF披露的净值增长率复合收益与对应价格指数20日收益之差。可能包含分红口径、费用、跟踪及数据差异；比较对象是价格指数，因此不称为官方跟踪误差，也不是可直接套利的超额收益。", "etf", "跟踪观察"),
    ("breadth_advancing_ratio", "成分股 · 上涨家数占比", "市场宽度", "%", "当时成分股上涨家数 / 当时成分股总数 × 100",
     "只使用对应观测日可确认的指数成分股和同日有效收盘行情，比较上涨股票数量而非市值权重。高于50%表示多数成分股上涨，不保证指数未来上涨；不以今天的成分股重建过去，也不以全市场涨跌家数代替本指数宽度。", "breadth", "收益关联研究"),
    ("breadth_above_ma20_ratio", "成分股 · MA20上方占比", "市场宽度", "%", "当时成分股中收盘价高于各自20日均线的家数 / 当时成分股总数 × 100",
     "观察趋势是否由较多成分股参与。仅在当期成分完整、每只成分股均有足够历史收盘数据时计算；新增成分或历史行情不足时明确缺失，高比例不等于行情必然延续。", "breadth", "收益关联研究"),
):
    register(Factor(fid, name, category, unit, 1, (fid + "_input",), formula,
        explanation + " 所有新增因子首版仅作观察，不参与顶部综合评分。",
        lambda f, column=fid + "_input": f[column], assessment_enabled=False,
        source_group=group, purpose=purpose, explanation=explanation))


def calculate_all(frame):
    frame = frame.copy()
    for column in {column for definition in REGISTRY.values() for column in definition.inputs}:
        if column not in frame:
            frame[column] = np.nan
    results = {}
    for key, definition in REGISTRY.items():
        values = definition.compute(frame).replace([np.inf, -np.inf], np.nan)
        missing_input = frame[list(definition.inputs)].isna().any(axis=1)
        if definition.inputs in (("volume",), ("amount",)):
            missing_input |= missing_input.shift(1).rolling(20, min_periods=1).max().fillna(1).astype(bool)
        status = pd.Series("ok", index=frame.index)
        status.loc[values.isna()] = "undefined"
        status.iloc[:definition.lookback - 1] = "warmup"
        status.loc[missing_input & values.isna()] = "missing_source"
        results[key] = (values, status)
    return results
