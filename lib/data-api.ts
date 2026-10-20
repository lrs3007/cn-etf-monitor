import type { Dashboard, Factor, FactorDetail, IndexInfo, Evaluation } from "./types";

export const publishedMode = process.env.NEXT_PUBLIC_DATA_MODE === "published";
interface Manifest {
  schema_version: number; export_id: string; generated_at: string; published_at: string;
  indices: (IndexInfo & { snapshot_id: string })[];
  objects: Record<string, { key: string }>;
}
interface IndexResult {
  code: string; snapshot_id: string; updated_at: string; coverage: Dashboard["coverage"]; prices: Dashboard["prices"];
  definitions: Record<string, Factor>; evaluations: Record<string, Record<string, Evaluation>>;
  assessments: Record<string, Dashboard["assessment"]>; returns: Record<string, (number | null)[]>;
}
interface FactorResult {
  snapshot_id: string; values: [number | null, string][];
  rolling: Record<string, FactorDetail["rolling"]>; components: FactorDetail["components"];
  analysis?: FactorDetail["analysis"];
}
let release: Manifest | undefined;
let pendingManifest: Promise<Manifest> | undefined;
const indicesBySnapshot = new Map<string, IndexResult>();
const releasesBySnapshot = new Map<string, Manifest>();
const objects = new Map<string, Promise<unknown>>();

async function json<T>(url: string, signal?: AbortSignal, immutable = false): Promise<T> {
  const response = await fetch(url, { signal, cache: immutable ? "default" : "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "研究结果读取失败");
  return body;
}
async function manifest(refresh = false): Promise<Manifest> {
  if (pendingManifest) return pendingManifest;
  if (!refresh && release) return release;
  const pending = json<Manifest>("/.netlify/functions/results?key=manifest").then(m => {
      if (m.schema_version !== 1) throw new Error("研究结果版本不兼容，请更新网页");
      release = m;
      return m;
    });
  pendingManifest = pending;
  try { return await pending; }
  finally { if (pendingManifest === pending) pendingManifest = undefined; }
}
async function object<T>(m: Manifest, name: string): Promise<T> {
  const key = m.objects[name]?.key;
  if (!key) throw new Error("当前发布结果缺少该研究快照，请刷新结果");
  if (!objects.has(key)) {
    const pending = json<T>(`/.netlify/functions/results?key=${encodeURIComponent(key)}`, undefined, true);
    objects.set(key, pending);
    pending.catch(() => objects.delete(key));
    if (objects.size > 24) objects.delete(objects.keys().next().value!);
  }
  return objects.get(key) as Promise<T>;
}

export async function readIndices(signal?: AbortSignal): Promise<IndexInfo[]> {
  return publishedMode ? (await manifest()).indices : json("/api/v1/indices", signal);
}
export async function refreshPublication(): Promise<string> {
  return (await manifest(true)).published_at;
}
export async function publicationTime(): Promise<string> {
  return (await manifest()).published_at;
}
export async function checkPublication(): Promise<{ changed: boolean; published_at: string }> {
  const previous = release?.export_id;
  const current = await manifest(true);
  return { changed: previous !== current.export_id, published_at: current.published_at };
}
export async function readDashboard(code: string, horizon: number, signal?: AbortSignal): Promise<Dashboard> {
  if (!publishedMode) return json(`/api/v1/dashboard/${code}?horizon=${horizon}`, signal);
  const m = await manifest(), index = await object<IndexResult>(m, `index/${code}`);
  indicesBySnapshot.set(index.snapshot_id, index);
  releasesBySnapshot.set(index.snapshot_id, m);
  const assessment = index.assessments[String(horizon)];
  if (!assessment) throw new Error("该预测期限尚未发布");
  return { code, publication_id: m.export_id, snapshot_id: index.snapshot_id, updated_at: index.updated_at, coverage: index.coverage, prices: index.prices,
    assessment, factors: Object.entries(index.definitions).map(([fid, definition]) => ({ ...definition,
      coverage: index.coverage.factor_coverage[fid], evaluation: index.evaluations[fid][String(horizon)] })) };
}
export async function readFactor(code: string, factorId: string, horizon: number, snapshotId: string, signal?: AbortSignal): Promise<FactorDetail> {
  if (!publishedMode) return json(`/api/v1/indices/${code}/factors/${factorId}?horizon=${horizon}&snapshot_id=${snapshotId}`, signal);
  const index = indicesBySnapshot.get(snapshotId), m = releasesBySnapshot.get(snapshotId);
  if (!index || index.code !== code || !m) throw new Error("请重新读取指数研究快照");
  const result = await object<FactorResult>(m, `factor/${snapshotId}/${factorId}`);
  const evaluation = index.evaluations[factorId]?.[String(horizon)], returns = index.returns[String(horizon)];
  if (result.snapshot_id !== snapshotId || result.values.length !== index.prices.length || !evaluation || !returns) throw new Error("研究结果不完整，请重新读取");
  return { snapshot_id: snapshotId, definition: index.definitions[factorId], coverage: index.coverage.factor_coverage[factorId],
    analysis: result.analysis ?? null,
    evaluation, components: result.components, rolling: result.rolling[String(horizon)],
    series: index.prices.map((p, i) => ({ date: p.date, value: result.values[i][0], status: result.values[i][1], forward_return: returns[i] })) };
}
