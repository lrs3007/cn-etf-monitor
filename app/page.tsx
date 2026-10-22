"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDownRight, ArrowUpRight, ArrowRight, BookOpen, Check, ChevronDown, Database, ExternalLink, LoaderCircle, RefreshCw, SlidersHorizontal, X } from "lucide-react";
import FactorCard from "@/components/FactorCard";
import SyncProgress from "@/components/SyncProgress";
import type { Dashboard, IndexInfo, UpdateStatus } from "@/lib/types";
import { publishedMode, readDashboard, readIndices, refreshPublication, checkPublication } from "@/lib/data-api";

const defaults = [
  { code: "000300.SH", short_name: "沪深300", description: "沪深市场 · 大盘核心资产" },
  { code: "000905.SH", short_name: "中证500", description: "沪深市场 · 中盘成长" },
  { code: "000016.SH", short_name: "上证50", description: "上海市场 · 大盘蓝筹" },
  { code: "399673.SZ", short_name: "创业板50", description: "深圳市场 · 创业板高流动性" },
  { code: "000688.SH", short_name: "科创50", description: "上海市场 · 科技创新" },
];
const standardCategories = ["趋势", "动量", "风险", "量价", "估值", "资金申赎", "ETF流动性", "ETF特征", "市场宽度"];

export default function Page() {
  const [code, setCode] = useState("000300.SH");
  const [horizon, setHorizon] = useState(20);
  const [range, setRange] = useState("all");
  const [category, setCategory] = useState("全部因子");
  const [data, setData] = useState<Dashboard | null>(null);
  const [indices, setIndices] = useState<IndexInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["momentum_20"]));
  const [modal, setModal] = useState<"coverage" | "method" | null>(null);
  const [sort, setSort] = useState("ic");
  const [update, setUpdate] = useState<UpdateStatus | null>(null);
  const [startingUpdate, setStartingUpdate] = useState(false);
  const [stoppingUpdate, setStoppingUpdate] = useState(false);
  const [updateError, setUpdateError] = useState("");
  const [publishedAt, setPublishedAt] = useState("");
  const lastJob = useRef<string | null>(null);
  const current = defaults.find(i => i.code === code)!;
  const categories = useMemo(() => ["全部因子", ...new Set([...standardCategories,
    ...(data?.factors.map(f => f.category) || [])])], [data]);

  useEffect(() => {
    if (publishedMode) {
      let stopped = false, checking = false;
      async function check() {
        if (checking || document.visibilityState !== "visible") return;
        checking = true;
        try {
          const result = await checkPublication();
          if (stopped) return;
          setPublishedAt(result.published_at); setUpdateError("");
          if (result.changed) setRefresh(x => x + 1);
        } catch {
          if (!stopped) setUpdateError("自动读取最新结果失败，将稍后重试；当前已加载结果继续保留。");
        } finally { checking = false; }
      }
      const timer = setInterval(check, 60000);
      window.addEventListener("focus", check);
      document.addEventListener("visibilitychange", check);
      check();
      return () => { stopped = true; clearInterval(timer); window.removeEventListener("focus", check); document.removeEventListener("visibilitychange", check); };
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch("/api/v1/updates/latest", { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error("无法读取同步状态");
        const status: UpdateStatus = await response.json();
        if (controller.signal.aborted) return;
        setUpdate(status); setUpdateError("");
        const key = status.job ? `${status.job.id}:${status.job.status}:${status.busy}` : `none:${status.busy}`;
        if (lastJob.current !== null && key !== lastJob.current && !status.busy) setRefresh(x => x + 1);
        lastJob.current = key;
        timer = setTimeout(poll, status.busy ? 2000 : 15000);
      } catch (e) {
        if (!controller.signal.aborted) { setUpdateError(e instanceof Error ? e.message : "无法读取同步状态"); timer = setTimeout(poll, 5000); }
      }
    }
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    readIndices(controller.signal).then(d => { if (!controller.signal.aborted) setIndices(d); }).catch(() => {});
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    readDashboard(code, horizon, controller.signal)
      .then((d: Dashboard) => { if (!controller.signal.aborted) { setData(d); setLoading(false); } })
      .catch(e => { if (!controller.signal.aborted) { setError(e.message); setData(null); setLoading(false); } });
    return () => controller.abort();
  }, [code, horizon, refresh]);
  useEffect(() => {
    if (!modal) return;
    const close = (e: KeyboardEvent) => { if (e.key === "Escape") setModal(null); };
    document.addEventListener("keydown", close);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", close); document.body.style.overflow = previous; };
  }, [modal]);

  const since = useMemo(() => {
    if (range === "all" || !data) return "1900-01-01";
    const d = new Date(`${data.coverage.last_date}T00:00:00`);
    d.setFullYear(d.getFullYear() - Number(range));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }, [range, data]);
  const factors = useMemo(() => {
    const filtered = (data?.factors || []).filter(f => category === "全部因子" || f.category === category);
    const order = ["momentum_20", "ma_bias_20", "rsi_14", "volatility_20", "amount_ratio_20", "equity_bond_spread_10y", "pe_ttm"];
    return filtered.sort((a, b) => {
      const researchOrder = (order.includes(a.id) ? order.indexOf(a.id) : 99) - (order.includes(b.id) ? order.indexOf(b.id) : 99);
      if (sort === "default") return researchOrder;
      const av = sort.startsWith("full_") ? a.evaluation.full_ic : a.evaluation.oos_ic;
      const bv = sort.startsWith("full_") ? b.evaluation.full_ic : b.evaluation.oos_ic;
      const aMissing = av == null || !Number.isFinite(av), bMissing = bv == null || !Number.isFinite(bv);
      if (aMissing || bMissing) return aMissing === bMissing ? researchOrder : aMissing ? 1 : -1;
      const difference = sort === "ic" ? Math.abs(bv) - Math.abs(av)
        : sort.endsWith("_asc") ? av - bv : bv - av;
      return difference || researchOrder;
    });
  }, [data, category, sort]);
  const activeCount = data?.factors.filter(f => f.coverage.valid > 0).length || 0;

  function focusFactor(id: string) {
    setCategory("全部因子"); setExpanded(previous => new Set([...previous, id]));
    setTimeout(() => document.getElementById(`factor-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  }

  async function refreshData(mode: "update" | "publish_only" = "update") {
    setStartingUpdate(true); setUpdateError("");
    try {
      if (publishedMode) { setPublishedAt(await refreshPublication()); setRefresh(x => x + 1); return; }
      const response = await fetch(`/api/v1/updates?mode=${mode}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "无法启动同步");
      setUpdate(body); lastJob.current = `${body.job.id}:${body.job.status}:true`;
    } catch (e) { setUpdateError(e instanceof Error ? e.message : "无法启动同步"); }
    finally { setStartingUpdate(false); }
  }
  const syncing = startingUpdate || update?.busy;
  async function stopSync() {
    const runId = update?.job?.id;
    if (!runId || stoppingUpdate) return;
    setStoppingUpdate(true); setUpdateError("");
    try {
      const response = await fetch(`/api/v1/updates/${encodeURIComponent(runId)}/stop`, { method: "POST" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "无法停止同步");
      setUpdate(body);
      if (!body.busy) setRefresh(value => value + 1);
    } catch (error) { setUpdateError(error instanceof Error ? error.message : "无法停止同步"); }
    finally { setStoppingUpdate(false); }
  }
  const publication = update?.job?.details.publication;
  const updateMessage = publishedMode ? `本机计算 · 网页展示已发布结果${publishedAt ? ` · 发布于 ${new Date(publishedAt).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })}` : ""}。每分钟自动检查新结果；数据日期以各指数显示为准。` : update?.busy && update.job?.status === "publishing" ? "本机采集与计算已完成，正在同步网页结果，请稍候。" : update?.busy ? `正在同步五个指数${update.job?.details.active_index ? ` · ${defaults.find(i => i.code === update.job?.details.active_index)?.short_name || update.job.details.active_index}` : ""}，行情采集与全历史因子计算可能需要数分钟。`
    : update?.job?.status === "cancelled" ? "同步已停止，已完成的本机数据保留。可点击刷新数据重新同步。"
    : update?.job?.status === "publish_failed" ? "本机数据已更新，但网页同步失败。线上保留此前结果，可点击“仅同步网页”重试。"
    : update?.job?.status === "publish_interrupted" ? "本机数据已更新，网页同步任务中断。可点击“仅同步网页”重试。"
    : publication?.status === "published" ? `本机数据已更新，网页同步成功 · 发布于 ${new Date(publication.published_at!).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })}。`
    : publication?.status === "unchanged" ? "本机数据已更新并核对，网页结果已是最新版本，无需重复上传。"
    : update?.job?.status === "completed" ? `本机更新完成${publication?.status === "disabled" ? "（未启用网页自动同步）" : ""}：${update.job.finished_at?.replace("T", " ").slice(0, 19)}。数据日期以各指数显示为准。`
    : update?.job?.status === "partial" ? "部分指数更新失败，已保留原快照。"
    : update?.job?.status === "failed" ? "数据同步失败，已保留原快照。"
    : update?.job?.status === "interrupted" ? "上次同步中断，可点击刷新数据重试。" : "";

  return <>
    <header className="site-header"><div className="header-inner"><a className="brand" href="/" aria-label="宽基研究台首页"><span className="brand-icon"><Activity size={19} strokeWidth={1.8} /></span><strong>宽基研究台<span>INDEX OBSERVATORY</span></strong></a>
      <nav className="main-nav" aria-label="主导航"><span className="nav-active">指数研究</span><button onClick={() => setModal("coverage")}>数据覆盖</button><button onClick={() => setModal("method")}>研究方法</button></nav>
      <div className="header-end"><span className="live-dot" />日频研究<span className="header-separator" />A 股市场</div>
    </div></header>
    <main className="page-shell">
      <div className="breadcrumb">研究工作台 <span>/</span> 宽基指数 <span>/</span> <b>{current.short_name}</b></div>
      <div className="page-heading"><div><div className="eyebrow">A LONGER VIEW OF THE MARKET</div><h1>用因子，理解市场的脉络<span>。</span></h1><p>从完整历史到当下信号，让每一个判断都有据可循。</p></div><div className="refresh-actions">{!publishedMode && <button className="outline-button" onClick={() => refreshData("publish_only")} disabled={loading || !!syncing} title="使用现有本机快照，仅导出并上传网页结果，不重新采集计算">仅同步网页</button>}<button className="outline-button" onClick={() => refreshData()} disabled={loading || !!syncing}><RefreshCw size={14} className={syncing ? "spin" : ""} />{syncing ? (publishedMode ? "读取中…" : "同步中…") : publishedMode ? "刷新结果" : "刷新数据"}</button></div></div>
      {(updateMessage || updateError) && <div className="update-status" role="status" aria-live="polite"><p>{updateError || updateMessage}</p>{!syncing && update?.job?.details.errors?.map((item, i) => <p className="negative" key={i}>{defaults.find(d => d.code === item.index)?.short_name || item.index}：{item.error}</p>)}</div>}
      {!publishedMode && update && <SyncProgress update={update} stopping={stoppingUpdate} onStop={stopSync} />}
      <section className="workspace" aria-label="指数与研究参数">
        <div className="index-tabs" role="tablist" aria-label="选择指数">{defaults.map(i => <button role="tab" aria-selected={code === i.code} key={i.code} className={code === i.code ? "active" : ""} onClick={() => setCode(i.code)}><span>{i.short_name}</span><small>{i.code}</small>{code === i.code && <span className="tab-indicator" />}</button>)}</div>
      <div className="research-controls"><div><span className="control-label">预测期限</span><div className="horizon-selector">{[1, 5, 10, 20, 60].map(h => <button key={h} aria-pressed={horizon === h} className={horizon === h ? "active" : ""} onClick={() => setHorizon(h)}>{h}日</button>)}</div></div><label className="range-control"><span className="control-label">图表区间</span><select value={range} onChange={e => setRange(e.target.value)}><option value="all">全部历史</option><option value="10">近10年</option><option value="5">近5年</option><option value="3">近3年</option><option value="1">近1年</option></select></label><span className="research-mode"><span className="small-dot" />时间序列 Rank IC</span></div>
      </section>

      {loading ? <div className="loading-state"><LoaderCircle className="spin" size={28} /><h2>正在读取 {current.short_name} 的研究快照</h2><p>加载指数历史、因子证据与样本外检验。</p></div>
      : error || !data ? <div className="error-state"><Database size={30} /><h2>该指数的数据尚未就绪</h2><p>{error}</p>{!publishedMode && <code>.venv/bin/python -m backend.app.cli backfill</code>}<button className="outline-button" onClick={() => setRefresh(x => x + 1)}>重新读取</button></div>
      : <>
        <div className="index-context"><h2>{current.short_name}<span>{current.code}</span></h2><span>{current.description}</span><span className="as-of"><span className="live-dot" />数据截至 {data.coverage.last_date}</span></div>
        <section className="assessment-panel" aria-label="指数整体分析结论">
          <div className="assessment-main"><div className="section-overline"><span className="small-dot" />指数综合观察<span className="light-tag">未来 {horizon} 个交易日</span></div><h2>{data.assessment.label}</h2><p className="assessment-description">{data.assessment.text}</p>{data.assessment.relative_value && <p className="relative-value-note">{data.assessment.relative_value.text}</p>}
            <div className="category-assessments">{Object.entries(data.assessment.categories).map(([name, value]) => <div key={name}><span>{name}</span><strong className={value.stance === "偏弱" ? "negative" : value.stance === "证据不足" ? "subtle" : "positive"}>{value.stance}</strong><small>{value.count} 项因子支持</small></div>)}</div>
            <div className="evidence-line"><span>主要证据</span>{data.assessment.highlights.length ? data.assessment.highlights.map(e => <button key={e.factor_id} onClick={() => focusFactor(e.factor_id)}>{e.name}<span className={e.stance === "偏弱" ? "negative" : "positive"}>{e.stance}</span><ArrowRight size={12} /></button>) : <span className="subtle">当前历史检验尚不足以形成稳定方向。</span>}</div>
          </div>
          <aside className="index-snapshot"><div className="snapshot-top">最新收盘点位<span>PRICE INDEX</span></div><strong className="price-number">{data.assessment.last_price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong><span className={`price-change ${(data.assessment.day_change ?? 0) >= 0 ? "market-up" : "market-down"}`}>{(data.assessment.day_change ?? 0) >= 0 ? <ArrowUpRight size={15} /> : <ArrowDownRight size={15} />}{((data.assessment.day_change ?? 0) * 100).toFixed(2)}%<small>较上一交易日</small></span><Sparkline values={data.prices.slice(-120).map(p => p.close)} /><div className="snapshot-footer"><span>近120个交易日</span><span>价格走势</span></div><p>综合状态为研究性汇总；组合尚未独立验证，不代表收益预测概率。</p></aside>
        </section>
        <div className="data-strip"><span><Database size={14} />{data.coverage.first_date} — {data.coverage.last_date}</span><span><b>{data.coverage.rows.toLocaleString()}</b> 个历史交易日</span><span><b>{activeCount}/{data.factors.length}</b> 个因子有数据</span><span>{data.coverage.cross_source_checked ? <Check size={14} /> : <InfoIcon />}{data.coverage.cross_source_checked ? "价格双源核对" : "单数据源"}</span><button onClick={() => setModal("coverage")}>查看覆盖详情 <ArrowRight size={13} /></button></div>
        <section className="factor-section"><div className="section-heading"><div><span className="eyebrow">FACTOR EVIDENCE</span><h2>逐个因子，检验每一条线索<span>{data.factors.length}</span></h2><p>默认按样本外 IC 绝对值从高到低排列，保留正负方向。排序使用顶部所选预测期限，缺失值排在末尾。</p></div><label className="sort-control"><SlidersHorizontal size={14} /><select aria-label="因子排序" value={sort} onChange={e => setSort(e.target.value)}><option value="ic">样本外 |IC| · 从高到低</option><option value="oos_desc">样本外 IC · 从高到低</option><option value="oos_asc">样本外 IC · 从低到高</option><option value="full_desc">全样本 IC · 从高到低</option><option value="full_asc">全样本 IC · 从低到高</option><option value="default">研究顺序</option></select></label></div>
          <div className="category-tabs">{categories.map(c => <button key={c} className={category === c ? "active" : ""} onClick={() => setCategory(c)}>{c}<span>{c === "全部因子" ? data.factors.length : data.factors.filter(f => f.category === c).length}</span></button>)}<button className="collapse-button" onClick={() => setExpanded(new Set())}>收起全部</button></div>
          <div className="factor-list">{factors.map(f => <FactorCard key={`${data.publication_id || data.snapshot_id}-${horizon}-${f.id}`} factor={f} dashboard={data} horizon={horizon} since={since} expanded={expanded.has(f.id)} onToggle={() => setExpanded(s => { const next = new Set(s); next.has(f.id) ? next.delete(f.id) : next.add(f.id); return next; })} />)}</div>
        </section>
        <section className="research-footnote"><BookOpen size={18} /><div><h4>把历史作为证据，保留对结论的审视。</h4><p>IC 衡量因子与未来收益的历史关联。负 IC 也可能有预测价值；相关性与领先关系不构成经济因果证明。图表筛选仅改变展示区间，统计使用注明的完整研究样本。</p><button onClick={() => setModal("method")}>阅读完整研究口径 <ExternalLink size={12} /></button></div></section>
        <footer className="page-footer"><span>宽基研究台<span className="footer-dot">·</span>持续观察，独立判断</span><span>快照 {data.snapshot_id.slice(0, 8)}<span className="footer-dot">·</span>更新于 {data.updated_at.replace("T", " ").slice(0, 19)}</span></footer>
      </>}
    </main>
    {modal && <div className="modal-backdrop" onClick={() => setModal(null)}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onClick={e => e.stopPropagation()}><button className="modal-close" autoFocus onClick={() => setModal(null)} aria-label="关闭"><X size={20} /></button><span className="eyebrow">RESEARCH TRANSPARENCY</span><h2 id="modal-title">{modal === "coverage" ? "数据覆盖与历史边界" : "研究方法与统计口径"}</h2>
      {modal === "coverage" ? <><p>保存数据源可获得的全部历史。预热期、缺失区间与发布前回溯分别记录，不作补零或回填伪造。</p><div className="table-scroll"><table><thead><tr><th>指数</th><th>实际覆盖</th><th>交易日</th><th>发布前回溯</th></tr></thead><tbody>{defaults.map(i => { const item = indices.find(x => x.code === i.code); return <tr key={i.code}><td>{i.short_name}</td><td>{item?.coverage ? `${item.coverage.first_date} → ${item.coverage.last_date}` : "尚未回填"}</td><td>{item?.coverage?.rows.toLocaleString() ?? "—"}</td><td>{item?.coverage?.backcast_rows.toLocaleString() ?? "—"}</td></tr>; })}</tbody></table></div>{data && <><h3>{current.short_name} · 因子覆盖</h3><div className="table-scroll"><table><thead><tr><th>因子</th><th>首次有效</th><th>有效</th><th>预热</th><th>缺源/未定义</th></tr></thead><tbody>{data.factors.map(f => <tr key={f.id}><td>{f.name}</td><td>{f.coverage.first_valid || "未接入"}</td><td>{f.coverage.valid}</td><td>{f.coverage.warmup}</td><td>{f.coverage.missing + f.coverage.undefined}</td></tr>)}</tbody></table></div><p>价格来源：新浪；成交额及部分PE由中证官方、东方财富、国证指数补充。重叠收盘价差超过0.2%时暂停发布新快照。交易日完整性尚未通过独立交易所日历逐日认证。</p>{[...data.coverage.provider_errors, ...(data.coverage.bond_provider_errors || [])].map(n => <p key={n}>{n}</p>)}{data.coverage.notes.map(n => <p key={n}>{n}</p>)}</>}</>
      : <><p>本项目研究单个指数的时间序列预测关系。因子值与未来1、5、10、20、60个交易日的价格指数收益计算 Spearman 秩相关。</p><h3>全历史保存，发布后检验</h3><p>指数发布前的回溯历史用于图表与因子预热，默认不参与 IC。收益标签为收盘到未来收盘，尚未成熟的标签不参与统计。第三方历史数据可能修订，现有数据不能还原修订前的真实点时版本。</p><h3>按年度推进样本外验证</h3><p>每年只用此前历史训练，至少504个有效训练样本。剔除未来收益区间跨越训练/测试边界的记录。五分组阈值仅根据训练集确定。因子定义预先固定，但本页并非策略收益回测。</p><h3>重叠收益与不确定性</h3><p>252日滚动IC至少200个有效样本，结果横轴标记为最后收益标签成熟日。样本外 IC 使用分块Bootstrap估计95%区间，保留局部时间依赖；该区间未校正多因子、多期限比较，不据此宣称显著性。</p><h3>顶部结论如何生成</h3><p>综合历史方向、样本外方向与年度稳定性；因子在同类别内平均，再按类别等权汇总。因子需至少252个样本外样本、年度方向一致率不低于60%、|样本外IC|至少0.03，且95%单项分块区间不跨零；至少3因子覆盖2类才给综合方向。当前分位使用最近1260个有效交易日范围；组合权重未经独立样本外验证。</p><h3>观察关联，审慎解释因果</h3><p>价格衍生因子来自价格本身。散点与滞后分析展示统计关联，不足以证明因果。负滞后包含较晚因子，含前视，不能作为交易信号。已加入固定5阶滞后、HAC稳健Wald双向检验及按年度推进的增量样本外预测比较。因子预先固定一阶差分，通过ADF检查后检验；对本指数全部因子的双向p值作BH-FDR校正。它们仍不能识别经济因果。</p></>}
    </section></div>}
  </>;
}

function InfoIcon() { return <span className="small-dot" />; }
function Sparkline({ values }: { values: number[] }) {
  const min = Math.min(...values), max = Math.max(...values), width = 290, height = 65;
  const points = values.map((v, i) => `${i / Math.max(1, values.length - 1) * width},${height - 4 - (v - min) / (max - min || 1) * (height - 10)}`).join(" ");
  return <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="近120个交易日指数走势"><defs><linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#73896b" stopOpacity=".14" /><stop offset="100%" stopColor="#73896b" stopOpacity="0" /></linearGradient></defs><polygon points={`0,${height} ${points} ${width},${height}`} fill="url(#spark-fill)" /><polyline points={points} fill="none" stroke="#74896b" strokeWidth="1.5" strokeLinejoin="round" /></svg>;
}
