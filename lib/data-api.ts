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
  price_chunks?: string[]; research_chunks?: string[]; components_key?: string;
  analyses?: Record<string, FactorDetail["analysis"]>;
}
interface PriceChunk { prices: Dashboard["prices"]; returns: IndexResult["returns"]; }
interface ResearchChunk { dates: string[]; factors: Record<string, Pick<FactorResult, "values" | "rolling">>; }
interface FactorResult {
  snapshot_id: string; values: [number | null, string][];
  rolling: Record<string, FactorDetail["rolling"]>; components: FactorDetail["components"];
  analysis?: FactorDetail["analysis"];
}
let release: Manifest | undefined;
let pendingManifest: Promise<Manifest> | undefined;
const indicesBySnapshot = new Map<string, IndexResult>();
const releasesBySnapshot = new Map<string, Manifest>();
const pinned = new Map<string, { index: IndexResult; manifest: Manifest }>();
const objects = new Map<string, Promise<unknown>>();
const researchBySnapshot = new Map<string, Promise<ResearchChunk[]>>();

async function chunks<T>(m: Manifest, names: string[]): Promise<T[]> {
  const result: T[] = new Array(names.length);
  let cursor = 0;
  await Promise.all(Array.from({ length: Math.min(6, names.length) }, async () => {
    while (cursor < names.length) { const i = cursor++; result[i] = await object<T>(m, names[i]); }
  }));
  return result;
}

async function json<T>(url: string, signal?: AbortSignal, immutable = false): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    let response: Response, body;
    try {
      response = await fetch(url, { signal, cache: immutable ? "default" : "no-store" });
      if ([408, 429, 500, 502, 503, 504].includes(response.status)) throw new Error("研究结果暂时不可读取");
      body = await response.json();
    } catch (error) {
      if (!immutable || signal?.aborted || attempt >= 2) throw error;
      await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt));
      continue;
    }
    if (!response.ok) throw new Error(body.detail || "研究结果读取失败");
    return body;
  }
}
async function manifest(refresh = false): Promise<Manifest> {
  if (pendingManifest) return pendingManifest;
  if (!refresh && release) return release;
  const pending = json<Manifest>("/.netlify/functions/results?key=manifest").then(m => {
      if (![1, 2].includes(m.schema_version)) throw new Error("研究结果版本不兼容，请更新网页");
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
    if (objects.size > 160) objects.delete(objects.keys().next().value!);
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
  const m = await manifest(), summary = await object<IndexResult>(m, `index/${code}`);
  const pinKey = `${m.export_id}/${summary.snapshot_id}`;
  let index = pinned.get(pinKey)?.index;
  if (!index) {
    index = { ...summary };
    if (m.schema_version === 2) {
      if (!index.price_chunks?.length || !index.research_chunks?.length) throw new Error("历史分块缺失");
      const parts = await chunks<PriceChunk>(m, index.price_chunks);
      index.prices = parts.flatMap(p => p.prices);
      index.returns = Object.fromEntries(Object.keys(parts[0].returns).map(h => [h, parts.flatMap(p => p.returns[h])]));
      if (index.prices.length !== index.coverage.rows || index.prices.at(-1)?.date !== index.coverage.last_date
        || index.prices.some((p, i) => i > 0 && p.date <= index!.prices[i - 1].date)) throw new Error("历史分块覆盖不完整");
    }
  }
  indicesBySnapshot.set(index.snapshot_id, index);
  releasesBySnapshot.set(index.snapshot_id, m);
  pinned.set(pinKey, { index, manifest: m });
  const assessment = index.assessments[String(horizon)];
  if (!assessment) throw new Error("该预测期限尚未发布");
  return { code, publication_id: m.export_id, snapshot_id: index.snapshot_id, updated_at: index.updated_at, coverage: index.coverage, prices: index.prices,
    assessment, factors: Object.entries(index.definitions).map(([fid, definition]) => ({ ...definition,
      coverage: index.coverage.factor_coverage[fid], evaluation: index.evaluations[fid][String(horizon)] })) };
}
export async function readFactor(code: string, factorId: string, horizon: number, snapshotId: string, signal?: AbortSignal, publicationId?: string): Promise<FactorDetail> {
  if (!publishedMode) return json(`/api/v1/indices/${code}/factors/${factorId}?horizon=${horizon}&snapshot_id=${snapshotId}`, signal);
  const pin = publicationId ? pinned.get(`${publicationId}/${snapshotId}`) : undefined;
  const index = publicationId ? pin?.index : indicesBySnapshot.get(snapshotId), m = publicationId ? pin?.manifest : releasesBySnapshot.get(snapshotId);
  if (!index || index.code !== code || !m) throw new Error("请重新读取指数研究快照");
  let result: FactorResult;
  if (m.schema_version === 2) {
    const researchKey = `${m.export_id}/${snapshotId}`;
    if (!researchBySnapshot.has(researchKey)) {
      const pending = chunks<ResearchChunk>(m, index.research_chunks!);
      researchBySnapshot.set(researchKey, pending);
      pending.catch(() => researchBySnapshot.delete(researchKey));
      if (researchBySnapshot.size > 10) researchBySnapshot.delete(researchBySnapshot.keys().next().value!);
    }
    const parts = await researchBySnapshot.get(researchKey)!;
    const dates = parts.flatMap(p => p.dates);
    if (dates.length !== index.prices.length || dates.some((d, i) => d !== index.prices[i].date)
      || parts.some(p => p.factors[factorId]?.values.length !== p.dates.length)) throw new Error("因子历史分块不完整");
    const inputs = index.components_key && factorId === "equity_bond_spread_10y"
      ? await object<Record<string, FactorDetail["components"]>>(m, index.components_key) : {};
    const horizons = Object.keys(parts[0].factors[factorId].rolling);
    result = { snapshot_id: snapshotId, values: parts.flatMap(p => p.factors[factorId].values),
      rolling: Object.fromEntries(horizons.map(h => [h, parts.flatMap(p => p.factors[factorId].rolling[h])])),
      components: inputs[factorId] || [], analysis: index.analyses?.[factorId] };
  } else result = await object<FactorResult>(m, `factor/${snapshotId}/${factorId}`);
  const evaluation = index.evaluations[factorId]?.[String(horizon)], returns = index.returns[String(horizon)];
  if (result.snapshot_id !== snapshotId || result.values.length !== index.prices.length || !evaluation || !returns) throw new Error("研究结果不完整，请重新读取");
  return { snapshot_id: snapshotId, definition: index.definitions[factorId], coverage: index.coverage.factor_coverage[factorId],
    analysis: result.analysis ?? null,
    evaluation, components: result.components, rolling: result.rolling[String(horizon)],
    series: index.prices.map((p, i) => ({ date: p.date, value: result.values[i][0], status: result.values[i][1], forward_return: returns[i] })) };
}
