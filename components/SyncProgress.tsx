"use client";
import { useEffect, useState } from "react";
import { LoaderCircle, Square } from "lucide-react";
import type { UpdateStatus } from "@/lib/types";

const stages: Record<string, number> = { collect_etf: 0, collect_breadth: 0, collect_bonds: 0, collect_index: 0, calculate: 0, diagnostics: 0, save: 0, export: 1, upload: 2, finalize: 2, completed: 3 };
const names: Record<string, string> = { "000300.SH": "沪深300", "000905.SH": "中证500", "000016.SH": "上证50", "399673.SZ": "创业板50", "000688.SH": "科创50" };

export default function SyncProgress({ update, stopping, onStop }: { update: UpdateStatus; stopping: boolean; onStop: () => void }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { if (!update.busy) return; const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, [update.busy]);
  const job = update.job;
  if (!job) return update.busy ? <div className="sync-progress">本机有更新任务占用中，正在读取任务信息…</div> : null;
  const progress = job.details.progress;
  const isStopping = stopping || job.status === "stopping";
  const publication = job.details.publication?.status;
  const phase = job.status === "completed" && (publication === "published" || publication === "unchanged") ? 3
    : stages[progress?.stopped_stage || progress?.stage || ""] ?? (job.status === "publishing" || publication === "failed" || publication === "publishing" ? 2 : 0);
  const done = progress?.completed, total = progress?.total;
  const percent = total != null && total > 0 && done != null ? Math.min(100, Math.max(0, done / total * 100)) : null;
  const elapsed = Math.max(0, Math.floor(((update.busy ? now : Date.parse(job.finished_at || job.started_at)) - Date.parse(job.started_at)) / 1000));
  const sinceProgress = progress ? Math.max(0, Math.floor((now - Date.parse(progress.updated_at)) / 1000)) : 0;
  const results = job.details.results || [], errors = job.details.errors || [];
  const indices = job.details.indices || Object.keys(names);
  const cancelled = job.status === "cancelled";
  const checkedExisting = !update.busy && job.status === "completed" && (publication === "published" || publication === "unchanged");
  const failed = ["failed", "partial", "publish_failed", "publish_interrupted", "interrupted"].includes(job.status);
  return <section className={`sync-progress ${cancelled ? "sync-cancelled" : ""}`} aria-label="数据同步进度">
    <div className="sync-topline"><div><h3>{update.busy && <LoaderCircle size={16} className="spin" />}{isStopping ? "正在停止同步" : update.busy ? "数据同步进行中" : cancelled ? "同步已停止" : failed ? "上次同步未完成" : "上次同步已完成"}</h3>
      <p>{job.details.trigger === "scheduler" ? "定时任务" : job.details.trigger === "cli" ? "命令行任务" : "网页任务"} · {Math.floor(elapsed / 60)}分{elapsed % 60}秒{update.busy && progress ? ` · 进度更新于 ${sinceProgress}秒前` : ""}</p></div>
      {update.busy && <button className="sync-stop" onClick={onStop} disabled={isStopping || !update.can_stop} title={!update.can_stop ? "正在确认最终发布或无法验证任务进程，暂不能停止" : "停止当前同步，保留已完成数据"}>{isStopping ? <LoaderCircle size={13} className="spin" /> : <Square size={12} />}{isStopping ? "正在停止…" : "停止同步"}</button>}
    </div>
    <ol className="sync-steps">{[job.details.mode === "publish_only" ? "复用本机结果" : "本机采集与计算", "导出因子分析", "同步网页结果"].map((name, i) => <li key={name} className={i < phase ? "done" : i === phase ? "active" : ""}><span>{i < phase ? "✓" : i + 1}</span>{name}</li>)}</ol>
    <div className="sync-stage" role="status"><span>{isStopping ? "已请求停止，正在结束当前工作并释放任务锁…" : cancelled ? "已完成的本机快照保留，可点击刷新数据重试。" : failed && progress?.stage !== "failed" ? "任务已结束，请查看下方任务提示后重试。" : progress?.label || (job.status === "publishing" ? "核对并同步网页结果" : update.busy ? "正在采集与计算历史数据…" : "任务结束")}</span>
      {percent !== null && <strong>{done}/{total}{progress?.unit ? ` ${progress.unit}` : ""} · {percent.toFixed(0)}%</strong>}</div>
    {update.busy && <div className={`sync-bar ${percent === null ? "indeterminate" : ""}`} role="progressbar" aria-label="当前阶段进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} aria-valuetext={percent === null ? "当前阶段处理中，暂不可量化" : `${done}/${total}，${percent.toFixed(0)}%`}><span style={percent === null ? undefined : { width: `${percent}%` }} /></div>}
    {update.busy && <p className="sync-hint">{percent !== null ? "百分比仅表示当前阶段的完成量，不表示整个任务的剩余时间。" : "采集和诊断耗时受数据源影响，完成前不预估百分比。"}{sinceProgress >= 30 ? " 正在等待请求或计算完成，进度暂未变化。" : ""}{progress?.stage === "finalize" ? " 正在确认最终发布，暂不能停止。" : ""}</p>}
    <div className="sync-indices">{indices.map(code => { const result = results.find(r => r.index === code), error = errors.find(e => e.index === code); return <span key={code} className={result || checkedExisting ? "done" : error ? "failed" : ""}>{names[code] || code}<b>{result ? `✓ ${result.last_date || "已完成"}` : error ? "更新失败" : checkedExisting ? "✓ 已核对本机结果" : job.details.active_index === code && update.busy ? "处理中" : cancelled ? "已停止" : "等待"}</b></span>; })}</div>
    {progress?.completed_bytes != null && progress?.total_bytes != null && <p className="sync-hint">已核对 {(progress.completed_bytes / 1048576).toFixed(2)} / {(progress.total_bytes / 1048576).toFixed(2)} MB；包含复用的已上传对象。</p>}
    {progress?.uploaded != null && <p className="sync-hint">新上传 {progress.uploaded} 个对象（{((progress.uploaded_bytes || 0) / 1048576).toFixed(2)} MB），复用 {progress.reused || 0} 个历史对象。</p>}
    {job.details.timings && <p className="sync-hint">阶段耗时：{Object.entries(job.details.timings).map(([key, seconds]) => `${({ collect: "采集", calculate: "计算", diagnostics: "诊断", save: "入库", export: "导出", upload: "上传", finalize: "发布确认" } as Record<string, string>)[key] || key} ${(seconds / 60).toFixed(1)}分`).join(" · ")}</p>}
    {cancelled && <p className="sync-hint">已完整提交的发布版本可能已生效；未完成的上传不会切换为部分结果。停止不会暂停以后的定时任务。</p>}
  </section>;
}
