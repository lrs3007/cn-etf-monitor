import { BookOpenText } from "lucide-react";
import type { FactorAnalysisData, Nullable } from "@/lib/types";

const pct = (value: Nullable, signed = false) => value === null ? "—" : `${signed && value > 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
const difference = (value: Nullable) => value === null ? "—" : `${value > 0 ? "+" : ""}${(value * 100).toFixed(2)}个百分点`;

export default function FactorAnalysis({ analysis, horizon }: { analysis: FactorAnalysisData | null | undefined; horizon: number }) {
  if (!analysis) return <p className="analysis-unavailable">此快照尚未包含逐因子解读；本机刷新并同步后即可查看。</p>;
  const outcome = analysis.outcomes.find(item => item.horizon === horizon);
  const bounds = analysis.match_bounds;
  return <section className="factor-analysis" aria-label="因子解读与历史相近位置">
    <div className="analysis-heading"><h4><BookOpenText size={15} />因子解读与历史相近位置</h4><span>数据日 {analysis.as_of} · 未来 {horizon} 个交易日</span></div>
    <div className="analysis-explanation"><span>关联逻辑</span><p>{analysis.mechanism}</p></div>
    <div className="analysis-explanation"><span>当天位置</span><div><p>{analysis.current_state}</p>
      <p className="analysis-note">近{analysis.method.window_sessions}个交易日内有{analysis.percentile_samples.toLocaleString()}个有效观测，至少需要{analysis.minimum_samples}个。
        {analysis.percentile !== null && <> 当前为{analysis.percentile.toFixed(1)}%分位，即{analysis.percentile.toFixed(1)}%的有效观测不高于当前值。</>}</p>
    </div></div>
    {analysis.constant_window && <p className="analysis-caution">当前窗口数值为常量；并列排名可达100%，不代表具有区分能力。</p>}
    {outcome && <>
      <div className="analysis-explanation"><span>收益关联</span><p>{outcome.relationship}</p></div>
      <div className="analysis-history-heading"><h5>相近位置后的历史表现</h5><span className={`analysis-sample ${outcome.sample_status !== "descriptive" ? "limited" : ""}`}>{outcome.sample_status === "no_matches" ? "暂无可比较样本" : outcome.sample_status === "limited" ? "样本偏少 · 参考有限" : "历史描述统计"}</span></div>
      {outcome.all_matches.count > 0 && <div className="analysis-metrics">
        <div><span>匹配日平均收益</span><strong>{pct(outcome.all_matches.mean, true)}</strong><small>基准 {pct(outcome.baseline.mean, true)}</small></div>
        <div><span>中位数 / 上涨比例</span><strong>{pct(outcome.all_matches.median, true)} / {pct(outcome.all_matches.win_rate)}</strong><small>历史频率，不是未来概率</small></div>
        <div><span>收益分布 P10 / P90</span><strong>{pct(outcome.all_matches.p10, true)} / {pct(outcome.all_matches.p90, true)}</strong><small>历史分位，不是最大亏损范围</small></div>
        <div><span>匹配日 / 不重叠观察期</span><strong>{outcome.all_matches.count.toLocaleString()}日 / {outcome.non_overlapping.count.toLocaleString()}段</strong><small>覆盖 {outcome.years.length} 个年份</small></div>
      </div>}
      <p className="analysis-interpretation">{outcome.interpretation}</p>
    </>}
    <details className="analysis-details"><summary>各期限对照、历史样本与计算口径</summary>
      <div className="analysis-table-scroll" tabIndex={0} role="region" aria-label="相近位置各期限历史表现"><table><thead><tr><th scope="col">未来期限</th><th scope="col">平均收益</th><th scope="col">中位数</th><th scope="col">上涨比例</th><th scope="col">相对基准均值差</th><th scope="col">匹配日 / 不重叠段</th></tr></thead><tbody>
        {analysis.outcomes.map(item => <tr key={item.horizon} className={item.horizon === horizon ? "analysis-selected" : ""}><th scope="row">{item.horizon}日{item.horizon === horizon ? " · 当前" : ""}</th><td>{pct(item.all_matches.mean, true)}</td><td>{pct(item.all_matches.median, true)}</td><td>{pct(item.all_matches.win_rate)}</td><td>{difference(item.mean_difference)}</td><td>{item.all_matches.count} / {item.non_overlapping.count}{item.sample_status === "limited" ? " · 偏少" : ""}</td></tr>)}
      </tbody></table></div>
      {outcome && outcome.non_overlapping.count > 0 && <>
        <p>不重叠取样：均值 {pct(outcome.non_overlapping.mean, true)}，中位数 {pct(outcome.non_overlapping.median, true)}，上涨比例 {pct(outcome.non_overlapping.win_rate)}；相同规则的基准均值 {pct(outcome.baseline_non_overlapping.mean, true)}。</p>
        <p className="analysis-note">匹配信号日 {outcome.first_signal} 至 {outcome.last_signal}；以下展示最近{outcome.examples.length}段不重叠观察期，统计使用全部{outcome.non_overlapping.count}段。</p>
        <div className="analysis-table-scroll" tabIndex={0} role="region" aria-label="相近位置历史观察期"><table><thead><tr><th scope="col">信号日</th><th scope="col">收益到期日</th><th scope="col">当时因子值</th><th scope="col">当时分位</th><th scope="col">未来{horizon}日收益</th></tr></thead><tbody>
          {outcome.examples.map(item => <tr key={item.signal_date}><td>{item.signal_date}</td><td>{item.end_date}</td><td>{item.value.toFixed(3)}</td><td>{item.percentile.toFixed(1)}%</td><td>{pct(item.return, true)}</td></tr>)}
        </tbody></table></div>
      </>}
      <p>匹配规则：历史各日仅用当时及之前的数据计算滚动分位，固定选择当前分位±{analysis.method.match_band_pp}个百分点{bounds ? `（${bounds[0].toFixed(1)}%—${bounds[1].toFixed(1)}%）` : "；当前分位不可用，暂不匹配"}；只统计正式发布后且收益标签已成熟的日期。图表展示区间不改变此统计。</p>
      <p>基准为同指数、同期限、历史分位可用且收益已成熟的全部日期。表格与上方指标使用全部匹配日，存在收益区间重叠；另按最早日期优先、至少间隔预测期限选取不重叠观察期，仍不保证统计独立。少于{analysis.method.minimum_reference_periods}段标为参考有限。</p>
      <p>这是回顾性描述，未进行前瞻策略验证。第三方历史版本可能包含修订；相近分位不代表相同市场环境，单项关联区间也未经多重比较校正。</p>
    </details>
    <p className="analysis-note analysis-boundary">以指数收盘到未来收盘的价格收益解释对应ETF的底层市场状态；未计ETF分红、费用、跟踪差异及成交条件。历史关联与上涨比例不构成因果证明或未来收益承诺。</p>
  </section>;
}
