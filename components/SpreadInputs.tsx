"use client";
import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import type { FactorDetail } from "@/lib/types";
import Chart from "./Chart";

export default function SpreadInputs({ detail, since, lastDate }: { detail: FactorDetail; since: string; lastDate: string }) {
  const latest = detail.components?.find(row => row.date === lastDate);
  const option = useMemo<EChartsOption>(() => {
    const inputs = new Map(detail.components?.map(row => [row.date, row]));
    const dates = detail.series.filter(row => row.date >= since).map(row => row.date);
    return { animation: false, color: ["#526d4e", "#668aa7"], tooltip: { trigger: "axis", confine: true },
      legend: { top: 0, data: ["盈利收益率", "10年国债到期收益率"], textStyle: { fontSize: 11, color: "#737d6b" } },
      grid: { left: 55, right: 24, top: 40, bottom: 45 },
      xAxis: { type: "category", data: dates, boundaryGap: false, axisLabel: { color: "#92988a", fontSize: 10 } },
      yAxis: { type: "value", name: "%", axisLabel: { color: "#92988a", fontSize: 10 }, splitLine: { lineStyle: { color: "#edf0e8" } } },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 15, bottom: 3 }],
      series: [{ name: "盈利收益率", type: "line", symbol: "none", connectNulls: false,
        data: dates.map(day => inputs.get(day)?.earnings_yield ?? null), lineStyle: { color: "#526d4e", width: 1.5 } },
      { name: "10年国债到期收益率", type: "line", symbol: "none", connectNulls: false,
        data: dates.map(day => inputs.get(day)?.bond_yield ?? null), lineStyle: { color: "#668aa7", width: 1.5 } }] };
  }, [detail, since]);
  const value = (number: number | null | undefined, suffix: string) => number == null ? "—" : `${number.toFixed(3)}${suffix}`;
  return <section className="spread-inputs" aria-label="股债收益差输入与口径">
    <div className="charts-topline"><h4>股债收益差 = 盈利收益率 − 国债收益率</h4><span className="light-tag">估值观察 · 不参与综合评分</span></div>
    <div className="research-metrics"><div><span>同日观测PE-TTM</span><strong>{value(latest?.pe_ttm, "倍")}</strong></div><div><span>盈利收益率</span><strong>{value(latest?.earnings_yield, "%")}</strong></div><div><span>10年国债到期收益率</span><strong>{value(latest?.bond_yield, "%")}</strong></div><div><span>原始观测日 / 信号日</span><strong>{latest?.observation_date || "—"} / {lastDate}</strong></div></div>
    <p className="chart-note">收益差单位为百分点，1个百分点=100基点。两端按同一观测日配对，最早从下一交易日生成研究信号；当前输入可用日期：{latest?.available_date || "缺失"}。缺少真实发布时间时采用次日可用假设，历史修订前版本不可还原。</p>
    <Chart option={option} height={230} label="指数盈利收益率与10年国债收益率历史对照" />
    <p className="chart-note">两条线使用相同的百分比坐标，横轴为可用信号日；两端原始观测日期一致，缺失不填补。PE来源：{latest?.valuation_source?.source || "未接入"}；债券来源：中债国债收益率曲线（到期），10年标准期限。</p>
  </section>;
}
