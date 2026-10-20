"use client";
import { useEffect, useMemo, useState } from "react";
import { ArrowDownToLine, ChevronDown, ChevronUp, FlaskConical, Info, LoaderCircle } from "lucide-react";
import type { EChartsOption } from "echarts";
import Chart from "./Chart";
import SpreadInputs from "./SpreadInputs";
import FactorAnalysis from "./FactorAnalysis";
import type { Dashboard, Evidence, Factor, FactorDetail, Nullable } from "@/lib/types";
import { readFactor } from "@/lib/data-api";

const green = "#526d4e", blue = "#668aa7", red = "#ac6c5a", grid = "#edf0e8";
const fmt = (n: Nullable | undefined, digits = 3) => n == null ? "—" : n.toFixed(digits);
const axis = { axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: "#92988a", fontSize: 10 }, splitLine: { lineStyle: { color: grid } } };
const tooltip = { trigger: "axis" as const, backgroundColor: "#fff", borderColor: "#e4e8df", textStyle: { color: "#34422e", fontSize: 12 }, confine: true };

export default function FactorCard({ factor, dashboard, horizon, since, expanded, onToggle }: {
  factor: Factor; dashboard: Dashboard; horizon: number; since: string; expanded: boolean; onToggle: () => void;
}) {
  const [researchHorizon, setResearchHorizon] = useState(horizon);
  const [detail, setDetail] = useState<FactorDetail | null>(null);
  const [error, setError] = useState("");
  const [diagnostics, setDiagnostics] = useState(false);
  const [chartMode, setChartMode] = useState("separate");
  const evaluation = detail?.evaluation ?? factor.evaluation;
  const evidence = dashboard.assessment.evidence.find(e => e.factor_id === factor.id) as Evidence;
  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setDetail(null); setError("");
    readFactor(dashboard.code, factor.id, researchHorizon, dashboard.snapshot_id, controller.signal)
      .then(d => { if (!controller.signal.aborted) setDetail(d); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [expanded, dashboard.code, dashboard.snapshot_id, factor.id, researchHorizon]);

  const historyOption = useMemo<EChartsOption>(() => {
    if (!detail) return {};
    const selected = detail.series.filter(p => p.date >= since);
    const priceMap = new Map(dashboard.prices.map(p => [p.date, p.close]));
    const dates = selected.map(p => p.date), prices = selected.map(p => priceMap.get(p.date) ?? null), values = selected.map(p => p.value);
    if (chartMode === "normalized") {
      const normalize = (items: Nullable[]) => {
        const valid = items.filter((v): v is number => v !== null);
        const mean = valid.reduce((a, b) => a + b, 0) / valid.length;
        const std = Math.sqrt(valid.reduce((s, v) => s + (v - mean) ** 2, 0) / valid.length);
        return items.map(v => v === null || !std ? null : (v - mean) / std);
      };
      return { animation: false, tooltip, legend: { data: ["指数（Z分数）", "因子（Z分数）"], top: 0, textStyle: { fontSize: 10, color: "#737d6b" } },
        grid: { left: 55, right: 20, top: 35, bottom: 45 },
        xAxis: { type: "category", data: dates, boundaryGap: false, ...axis }, yAxis: { type: "value", ...axis },
        dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 3, borderColor: "transparent" }],
        series: [{ name: "指数（Z分数）", type: "line", data: normalize(prices), symbol: "none", lineStyle: { color: green, width: 1.6 } },
          { name: "因子（Z分数）", type: "line", data: normalize(values), symbol: "none", lineStyle: { color: blue, width: 1.3 } }] };
    }
    return { animation: false, tooltip, axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [{ left: 58, right: 22, top: 18, height: "39%" }, { left: 58, right: 22, top: "57%", height: "25%" }],
      xAxis: [0, 1].map(i => ({ type: "category", gridIndex: i, data: dates, boundaryGap: false, ...axis, axisLabel: { ...axis.axisLabel, show: i === 1 } })),
      yAxis: [{ type: "value", gridIndex: 0, scale: true, ...axis, name: "指数点位", nameTextStyle: { color: "#92988a", fontSize: 10 } },
        { type: "value", gridIndex: 1, scale: true, ...axis, name: factor.unit, nameTextStyle: { color: "#92988a", fontSize: 10 } }],
      dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }, { type: "slider", xAxisIndex: [0, 1], height: 15, bottom: 2, borderColor: "transparent" }],
      series: [{ name: "指数点位", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: prices, showSymbol: false,
        lineStyle: { color: green, width: 1.5 }, areaStyle: { color: "rgba(82,109,78,.045)" } },
        { name: factor.name, type: "line", xAxisIndex: 1, yAxisIndex: 1, data: values, showSymbol: false, connectNulls: false,
          lineStyle: { color: blue, width: 1.2 } }] };
  }, [detail, dashboard.prices, since, chartMode, factor.name, factor.unit]);

  const rollingOption = useMemo<EChartsOption>(() => ({ animation: false, tooltip,
    grid: { left: 50, right: 20, top: 15, bottom: 30 },
    xAxis: { type: "category", data: detail?.rolling.filter(p => p.date >= since).map(p => p.date) || [], boundaryGap: false, ...axis },
    yAxis: { type: "value", min: -1, max: 1, ...axis },
    series: [{ name: "252日滚动IC", type: "line", showSymbol: false, data: detail?.rolling.filter(p => p.date >= since).map(p => p.ic) || [],
      lineStyle: { color: green, width: 1.5 }, areaStyle: { color: "rgba(82,109,78,.05)" },
      markLine: { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: "#acb3a6", type: "dashed" }, data: [{ yAxis: 0 }] } }] }), [detail, since]);

  const scatterOption = useMemo<EChartsOption>(() => {
    const pairs = detail?.series.filter(p => p.value !== null && p.forward_return !== null && p.date >= dashboard.coverage.launch_date && p.date >= since) || [];
    const stride = Math.max(1, Math.ceil(pairs.length / 2000));
    return { animation: false, tooltip: { ...tooltip, trigger: "item" }, grid: { left: 50, right: 20, top: 15, bottom: 40 },
      xAxis: { type: "value", name: factor.unit, scale: true, ...axis }, yAxis: { type: "value", name: "未来收益 %", ...axis },
      series: [{ type: "scatter", symbolSize: 4, itemStyle: { color: blue, opacity: .38 },
        data: pairs.filter((_, i) => i % stride === 0).map(p => [p.value, (p.forward_return as number) * 100]) }] };
  }, [detail, since, factor.unit, dashboard.coverage.launch_date]);

  const bucketOption = useMemo<EChartsOption>(() => ({ animation: false, tooltip,
    grid: { left: 45, right: 20, top: 20, bottom: 35 },
    xAxis: { type: "category", data: ["Q1 低", "Q2", "Q3", "Q4", "Q5 高"], ...axis },
    yAxis: { type: "value", name: "平均收益 %", ...axis },
    series: [{ name: "样本外平均收益", type: "bar", barWidth: "40%", data: evaluation.buckets.map(b => ({
      value: b.mean_return === null ? null : b.mean_return * 100, itemStyle: { color: (b.mean_return ?? 0) >= 0 ? green : red, borderRadius: [3, 3, 0, 0] } })) }] }), [evaluation.buckets]);

  const yearOption: EChartsOption = { animation: false, tooltip, grid: { left: 45, right: 20, top: 15, bottom: 35 },
    xAxis: { type: "category", data: evaluation.yearly.map(y => y.year), ...axis }, yAxis: { type: "value", min: -1, max: 1, ...axis },
    series: [{ name: "年度IC", type: "bar", data: evaluation.yearly.map(y => ({ value: y.ic, itemStyle: { color: (y.ic ?? 0) >= 0 ? green : red } })) }] };
  const lagOption: EChartsOption = { animation: false, tooltip, grid: { left: 45, right: 20, top: 15, bottom: 35 },
    xAxis: { type: "category", data: evaluation.lag.map(l => String(l.lag)), ...axis }, yAxis: { type: "value", min: -1, max: 1, ...axis },
    series: [{ name: "滞后关联IC", type: "bar", data: evaluation.lag.map(l => ({ value: l.ic, itemStyle: { color: l.lag < 0 ? "#d8b9aa" : green } })) }] };

  function download() {
    if (!detail) return;
    const components = new Map(detail.components?.map(row => [row.date, row]));
    const spread = factor.id === "equity_bond_spread_10y";
    const csv = "\ufeffdate,factor_value,status,forward_return" + (spread ? ",observation_date,available_date,pe_ttm,earnings_yield_pct,cgb_10y_yield_pct" : "") + "\n" + detail.series.map(s => {
      const input = components.get(s.date);
      return `${s.date},${s.value ?? ""},${s.status},${s.forward_return ?? ""}` + (spread ? `,${input?.observation_date ?? ""},${input?.available_date ?? ""},${input?.pe_ttm ?? ""},${input?.earnings_yield ?? ""},${input?.bond_yield ?? ""}` : "");
    }).join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a"); a.href = url; a.download = `${dashboard.code}-${factor.id}-${researchHorizon}d.csv`; a.click(); URL.revokeObjectURL(url);
  }

  const ci = evaluation.ci;
  const predictive = evaluation.predictive_diagnostics;
  const diagnosticStatus: Record<string,string> = { insufficient_data: "有效样本不足504日", constant_series: "序列为常量", stationarity_not_confirmed: "平稳性尚未确认", collinear_design: "变量共线，无法识别增量关系", model_not_estimable: "模型无法稳定估计" };
  const probability = (p: Nullable | undefined) => p == null ? "—" : p < .0001 ? "< 0.0001" : p.toFixed(4);
  return <article className={`factor-card ${expanded ? "is-expanded" : ""}`} id={`factor-${factor.id}`}>
    <button className="factor-heading" onClick={onToggle} aria-expanded={expanded}>
      <div className="factor-name"><span className={`category-dot cat-${factor.category}`} /><div><h3>{factor.name}</h3><span>{factor.category} / v{factor.version} <span className="coverage-small">· {factor.coverage.valid.toLocaleString()} 个有效交易日</span></span></div></div>
      <div className="factor-stat"><span>当前值</span><strong>{fmt(evidence.current, 2)}<small>{evidence.current !== null ? factor.unit : ""}</small></strong></div>
      <div className="factor-stat"><span>{factor.min_percentile_samples === 756 ? "5年分位" : "历史分位"}</span><strong>{fmt(evidence.percentile, 1)}<small>{evidence.percentile !== null ? "%" : ""}</small></strong></div>
      <div className="factor-stat"><span>全样本 IC{factor.id === "equity_bond_spread_10y" ? ` · ${evaluation.horizon}日` : ""}</span><strong className="mono">{fmt(evaluation.full_ic)}</strong></div>
      <div className="factor-stat"><span>样本外 IC{factor.id === "equity_bond_spread_10y" ? ` · ${evaluation.horizon}日` : ""}</span><strong className={`mono ${(evaluation.oos_ic ?? 0) < 0 ? "negative" : "positive"}`}>{fmt(evaluation.oos_ic)}</strong></div>
      <span className={`status-tag ${evidence.eligible ? "" : "muted"}`}>{evidence.stance}</span>
      {expanded ? <ChevronUp size={17} /> : <ChevronDown size={17} />}
    </button>
    {expanded && <div className="factor-body">
      <div className="factor-description"><Info size={15} /><p>{factor.description}</p><button onClick={download} disabled={!detail} title="导出该因子全部历史"><ArrowDownToLine size={15} /> 导出</button></div>
      {factor.id === "equity_bond_spread_10y" && <div className="spread-controls"><label>本因子预测期限 <select aria-label="股债收益差预测期限" value={researchHorizon} onChange={e => setResearchHorizon(Number(e.target.value))}>{(factor.horizons || [1, 5, 10, 20, 60]).map(h => <option key={h} value={h}>{h}个交易日</option>)}</select></label><span>5年分位至少756个有效观测；IC检验股票价格指数未来收益。</span></div>}
      {error ? <div className="error-panel">{error}</div> : !detail ? <div className="chart-loading"><LoaderCircle className="spin" size={22} /> 正在读取因子与历史分析…</div> : <>
        <FactorAnalysis analysis={detail.analysis} horizon={researchHorizon} />
        {!factor.coverage.valid ? <div className="empty-factor"><FlaskConical size={28} /><h4>历史数据尚未接入</h4><p>该因子已注册，全部日期保留缺失状态。可配置 Tushare 或导入含披露日期的历史估值 CSV。</p></div> : <>
        {factor.id === "equity_bond_spread_10y" && <SpreadInputs detail={detail} since={since} lastDate={dashboard.coverage.last_date} />}
        <div className="charts-topline"><h4><span className="line-key green" />指数与因子历史走势</h4><div className="mini-segment"><button className={chartMode === "separate" ? "selected" : ""} onClick={() => setChartMode("separate")}>共享时间轴</button><button className={chartMode === "normalized" ? "selected" : ""} onClick={() => setChartMode("normalized")}>标准化叠加</button></div></div>
        <Chart option={historyOption} height={330} label={`${factor.name}与指数的全历史走势图`} />
        <p className="chart-note">{chartMode === "normalized" ? "按所选展示区间计算Z分数，仅用于可视化，不参与历史信号计算。" : "上图为指数，下图为因子；共享缩放与光标。空白区间代表预热或缺失，未作填补。"}</p>
        <div className="charts-topline"><h4><span className="line-key green" />252日滚动 Rank IC</h4><span className="subtle">未来 {researchHorizon} 交易日 · 至少200个有效样本</span></div>
        <Chart option={rollingOption} height={195} label="252日滚动IC曲线" />
        <div className="research-metrics"><div><span>样本外样本数</span><strong>{evaluation.oos_samples.toLocaleString()}</strong></div><div><span>95% 分块Bootstrap区间</span><strong>{ci ? `[${fmt(ci.lower)}, ${fmt(ci.upper)}]` : "样本不足"}</strong></div><div><span>年度方向一致率</span><strong>{evaluation.direction_consistency === null ? "—" : `${(evaluation.direction_consistency * 100).toFixed(0)}%`}</strong></div><div><span>完整标签截至信号日</span><strong>{evaluation.last_signal_date || "—"}</strong></div></div>
        <button className="diagnostic-toggle" onClick={() => setDiagnostics(!diagnostics)}><FlaskConical size={15} />{diagnostics ? "收起" : "展开"}散点、分组、年度与领先滞后分析 {diagnostics ? <ChevronUp size={14} /> : <ChevronDown size={14} />}</button>
        {diagnostics && <div className="diagnostic-grid"><div><h4>因子值 × 未来{researchHorizon}日收益</h4><Chart option={scatterOption} height={240} label="因子与未来收益散点图" /><p className="chart-note">展示区间内发布后样本，超过2000点时等距抽样；统计值使用全部有效样本。</p></div><div><h4>样本外分组收益</h4><Chart option={bucketOption} height={240} label="因子分位组未来平均收益" /><p className="chart-note">分组边界由各年度训练集确定，展示全部样本外历史，未扣交易费用。</p></div><div><h4>分年度 IC</h4><Chart option={yearOption} height={200} label="逐年历史IC" /></div><div><h4>领先滞后关联</h4><Chart option={lagOption} height={200} label="因子滞后与未来收益关联" /><p className="chart-note">正数为因子领先收益；负数使用较晚因子，含前视，仅作诊断。关联不等于因果。</p></div></div>}
        {diagnostics && predictive && <section className="predictive-panel"><div className="charts-topline"><h4><FlaskConical size={14} />增量预测检验 · 因果线索</h4><span className="subtle">固定预测次日收益 · 5阶滞后</span></div><p>在历史收益与波动率之外，因子变化是否提供额外信息？采用预先固定的一阶差分、ADF平稳性检查和HAC稳健Wald检验。</p>{predictive.status !== "ok" ? <div className="diagnostic-status">{diagnosticStatus[predictive.status] || "该诊断暂不可用"}，不据此作方向判断。</div> : <><div className="research-metrics"><div><span>因子变化 → 收益 · FDR q</span><strong>{probability(predictive.forward?.q_value)}</strong></div><div><span>收益 → 因子变化 · FDR q</span><strong>{probability(predictive.reverse?.q_value)}</strong></div><div><span>增量样本外 R²</span><strong>{predictive.oos?.incremental_r2 == null ? "样本不足" : `${(predictive.oos.incremental_r2 * 100).toFixed(2)}%`}</strong></div><div><span>检验样本 / 样本外</span><strong>{predictive.samples.toLocaleString()} / {predictive.oos?.samples.toLocaleString() || "0"}</strong></div></div><p>原始 p：正向 {probability(predictive.forward?.p_value)}，反向 {probability(predictive.reverse?.p_value)}；对本指数 {predictive.fdr_family_tests} 项双向检验作 BH-FDR 校正。增量样本外 R² 为相对基线预测误差的改善，负值表示加入因子后变差。</p></>}<p className="chart-note">这里检验的是增量预测关系。未观测混杂、价格派生关系和历史修订仍可能影响结果，不能证明经济因果；该模块不参与顶部综合方向。</p></section>}
        <details className="method-details"><summary>计算口径与研究边界</summary><p>公式：<code>{factor.formula}</code></p><p>单指数时间序列 Spearman IC；发布前回溯数据保留在走势中，但不纳入本页IC。年度扩展训练至少504个样本，剔除跨边界标签。滚动IC横轴为标签成熟日。图表区间筛选不改变全样本及样本外统计。</p><p>置信区间使用{ci?.repetitions ?? 199}次分块Bootstrap，区间未经多重检验校正。卡片的历史分位仅用于当前状态；历史训练分组不使用未来数据。IC不等同于策略收益或经济因果。</p></details>
        </>}
      </>}
    </div>}
  </article>;
}
