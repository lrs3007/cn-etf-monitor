import { getStore } from "@netlify/blobs";
import { readFile, mkdir, writeFile, access, rename } from "node:fs/promises";
import { createHash } from "node:crypto";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { getAPIToken } from "@netlify/dev-utils";
import fetch from "node-fetch";
import { HttpsProxyAgent } from "https-proxy-agent";
import { Readable } from "node:stream";
import { setTimeout } from "node:timers/promises";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// All immutable objects must be durable before the single public pointer changes.
export async function publishResults(store, manifest, readObject, progress = () => {}, checkCancelled = async () => {}, concurrency = 6, verified = new Set(), onVerified = async () => {}) {
  await checkCancelled();
  if (![1, 2].includes(manifest.schema_version) || !/^[a-f0-9]{64}$/.test(manifest.export_id)) throw new Error("Invalid export manifest");
  const previous = await store.getWithMetadata("manifest", { type: "json", consistency: "strong" });
  if (previous?.data?.export_id === manifest.export_id) {
    const items = [...new Map(Object.values(manifest.objects).map(item => [item.key, item])).values()];
    const bytes = items.reduce((sum, item) => sum + item.bytes, 0);
    progress(items.length, items.length, { phase: "finalize", completed_bytes: bytes, total_bytes: bytes,
      uploaded: 0, reused: items.length, uploaded_bytes: 0 });
    return { changed: false, uploaded: 0, reused: items.length, uploaded_bytes: 0, published_at: previous.data.published_at };
  }
  const unique = new Map();
  for (const item of Object.values(manifest.objects)) {
    if (!/^objects\/[a-f0-9]{64}\.json\.gz$/.test(item.key) || item.key !== `objects/${item.sha256}.json.gz`) throw new Error("Invalid object key");
    unique.set(item.key, item);
  }
  // Published objects are immutable and were strongly verified before that release.
  const durable = new Set(Object.values(previous?.data?.objects || {}).map(item => item.key));
  for (const key of verified) durable.add(key);
  let uploaded = 0, reused = 0, completed = 0, completedBytes = 0, uploadedBytes = 0;
  const items = [...unique.values()];
  const totalBytes = items.reduce((sum, item) => sum + item.bytes, 0);
  const notify = phase => progress(completed, items.length, { phase, completed_bytes: completedBytes, total_bytes: totalBytes, uploaded, reused, uploaded_bytes: uploadedBytes });
  notify("upload");
  async function upload(item) {
    await checkCancelled();
    const data = await readObject(item.key);
    if (data.byteLength !== item.bytes || createHash("sha256").update(data).digest("hex") !== item.sha256) throw new Error("Export checksum mismatch");
    if (durable.has(item.key)) { reused++; return; }
    const result = await store.set(item.key, new Uint8Array(data).buffer, { onlyIfNew: true });
    let durableObject = await store.getMetadata(item.key, { consistency: "strong" });
    for (let attempt = 0; !durableObject && attempt < 2; attempt++) {
      await checkCancelled();
      await setTimeout(500 * 2 ** attempt);
      durableObject = await store.getMetadata(item.key, { consistency: "strong" });
    }
    if (!durableObject) throw Object.assign(new Error("Uploaded object is not durable"), { code: "OBJECT_NOT_DURABLE" });
    verified.add(item.key);
    await onVerified();
    if (result.modified) { uploaded++; uploadedBytes += item.bytes; } else reused++;
  }
  let cursor = 0, failure;
  // Workers immediately take the next object; a slow request cannot stall a batch.
  const workers = Array.from({ length: Math.min(items.length, Math.max(1, Math.min(12, Math.trunc(concurrency) || 6))) }, async () => {
    while (!failure && cursor < items.length) {
      const item = items[cursor++];
      try {
        await upload(item);
        completed++; completedBytes += item.bytes; notify("upload");
      } catch (error) { failure ||= error; }
    }
  });
  await Promise.all(workers); // Drain in-flight writes before reporting failure/cancellation.
  if (failure) throw failure;
  await checkCancelled();
  const published = { ...manifest, published_at: new Date().toISOString() };
  // Keep old immutable objects so already-open browsers retain consistent snapshots.
  if (previous && !previous.etag) throw new Error("Missing manifest ETag; refusing an unsafe concurrent update");
  notify("finalize");
  await checkCancelled();
  const result = await store.setJSON("manifest", published, previous ? { onlyIfMatch: previous.etag } : { onlyIfNew: true });
  if (!result.modified) {
    // A retry after a lost PUT response may find our own successful publication.
    const current = await store.get("manifest", { type: "json", consistency: "strong" });
    if (current?.export_id !== manifest.export_id) throw new Error("Another publisher changed the release; retry publishing");
  }
  const confirmed = await store.get("manifest", { type: "json", consistency: "strong" });
  if (confirmed?.export_id !== manifest.export_id) throw new Error("Release verification failed");
  return { changed: true, uploaded, reused, uploaded_bytes: uploadedBytes, published_at: published.published_at };
}

async function credentials() {
  let siteID = process.env.NETLIFY_SITE_ID;
  if (!siteID) {
    try { siteID = JSON.parse(await readFile(resolve(root, ".netlify/state.json"), "utf8")).siteId; } catch {}
  }
  // A dedicated PAT can be supplied by the local environment, never NEXT_PUBLIC_*.
  const token = process.env.NETLIFY_AUTH_TOKEN || await getAPIToken();
  if (!siteID || !token) throw new Error("请在本机 .env 配置 NETLIFY_SITE_ID 和 NETLIFY_AUTH_TOKEN（仅用于本机上传）");
  return { siteID, token };
}

async function main() {
  const verify = process.argv.includes("--verify");
  const structuredProgress = process.argv.includes("--progress");
  async function checkCancelled() {
    const file = process.env.CN_MONITOR_CANCEL_FILE;
    if (!file) return;
    try { await access(file); } catch (error) { if (error.code === "ENOENT") return; throw error; }
    throw Object.assign(new Error("Publication cancelled"), { name: "AbortError" });
  }
  const directory = resolve(root, process.argv.slice(2).find(x => !x.startsWith("--")) || "data/publication");
  const manifest = JSON.parse(await readFile(resolve(directory, "manifest.json"), "utf8"));
  const proxy = process.env.https_proxy || process.env.HTTPS_PROXY || process.env.http_proxy || process.env.HTTP_PROXY;
  const agent = proxy ? new HttpsProxyAgent(proxy) : undefined;
  let result;
  try {
    const auth = await credentials();
    const journalFile = resolve(root, "data/run", `verified-objects-${auth.siteID}.json`);
    let journal = [];
    try { journal = JSON.parse(await readFile(journalFile, "utf8")); } catch {}
    const verified = new Set(Array.isArray(journal) ? journal.filter(key => typeof key === "string" && /^objects\/[a-f0-9]{64}\.json\.gz$/.test(key)) : []);
    let journalWrite = Promise.resolve();
    const saveJournal = () => {
      journalWrite = journalWrite.then(async () => {
        await mkdir(dirname(journalFile), { recursive: true });
        await writeFile(journalFile + ".tmp", JSON.stringify([...verified]));
        await rename(journalFile + ".tmp", journalFile);
      });
      return journalWrite;
    };
    const store = getStore({ name: "research-results", ...auth, consistency: "strong",
      fetch: async (url, options) => {
        for (let attempt = 0; ; attempt++) {
          try {
            const response = await fetch(url, { ...options,
              body: options?.body instanceof ArrayBuffer ? Buffer.from(options.body) : options?.body,
              agent, signal: AbortSignal.timeout(90000) });
            if (attempt >= 3 || ![408, 429, 500, 502, 503, 504].includes(response.status)) {
              // Blobs expects a web Response/ReadableStream, not node-fetch's Node stream.
              const body = response.body && ![204, 205, 304].includes(response.status) && options?.method?.toLowerCase() !== "head"
                ? Readable.toWeb(response.body) : null;
              return new Response(body, { status: response.status, headers: Object.fromEntries(response.headers) });
            }
            response.body?.destroy();
          } catch (error) { if (attempt >= 3) throw error; }
          await setTimeout(1000 * 2 ** attempt);
        }
      } });
    if (verify) {
      const current = await store.get("manifest", { type: "json", consistency: "strong" });
      if (current?.export_id !== manifest.export_id) throw new Error("Remote release differs from local export");
      const items = [...new Set(Object.values(manifest.objects).map(x => x.key))];
      for (let offset = 0; offset < items.length; offset += 8) {
        const checked = await Promise.all(items.slice(offset, offset + 8).map(key => store.getMetadata(key, { consistency: "strong" })));
        if (checked.some(x => !x)) throw new Error("Published object is missing");
      }
      console.log(JSON.stringify({ verified: true, objects: items.length, export_id: current.export_id }));
      return;
    }
    result = await publishResults(store, manifest, key => readFile(resolve(directory, key)), (done, total, extra) => {
      if (structuredProgress) console.log("CN_MONITOR_PROGRESS " + JSON.stringify({ completed: done, total, ...extra }));
      if (done % 20 === 0 || done === total) console.error(`结果对象：${done}/${total}`);
    }, checkCancelled, 6, verified, saveJournal);
  } finally { agent?.destroy(); }
  await mkdir(resolve(root, "data/run"), { recursive: true });
  await writeFile(resolve(root, "data/run/last-publication.json"), JSON.stringify({ ...result, export_id: manifest.export_id, checked_at: new Date().toISOString() }));
  console.log(JSON.stringify({ ...result, export_id: manifest.export_id }));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`结果发布失败（${error.name}; HTTP=${error.status || error.statusCode || "—"}; network=${error.cause?.code || error.code || "—"}）。请检查本机凭证、网络和导出文件；线上保留此前结果。`);
    process.exitCode = 1;
  });
}
