import { getStore } from "@netlify/blobs";
import { readFile, mkdir, writeFile } from "node:fs/promises";
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
export async function publishResults(store, manifest, readObject, progress = () => {}) {
  if (manifest.schema_version !== 1 || !/^[a-f0-9]{64}$/.test(manifest.export_id)) throw new Error("Invalid export manifest");
  const previous = await store.getWithMetadata("manifest", { type: "json", consistency: "strong" });
  if (previous?.data?.export_id === manifest.export_id) return { changed: false, uploaded: 0, published_at: previous.data.published_at };
  const unique = new Map();
  for (const item of Object.values(manifest.objects)) {
    if (!/^objects\/[a-f0-9]{64}\.json\.gz$/.test(item.key) || item.key !== `objects/${item.sha256}.json.gz`) throw new Error("Invalid object key");
    unique.set(item.key, item);
  }
  let uploaded = 0;
  const items = [...unique.values()];
  async function upload(item) {
    const data = await readObject(item.key);
    if (data.byteLength !== item.bytes || createHash("sha256").update(data).digest("hex") !== item.sha256) throw new Error("Export checksum mismatch");
    if (await store.getMetadata(item.key, { consistency: "strong" })) return;
    const result = await store.set(item.key, new Uint8Array(data).buffer, { onlyIfNew: true });
    if (!await store.getMetadata(item.key, { consistency: "strong" })) throw new Error("Uploaded object is not durable");
    if (result.modified) uploaded++;
  }
  for (let offset = 0; offset < items.length; offset += 4) {
    const results = await Promise.allSettled(items.slice(offset, offset + 4).map(upload));
    const failed = results.find(r => r.status === "rejected");
    if (failed) throw failed.reason;
    progress(Math.min(offset + 4, items.length), items.length);
  }
  const published = { ...manifest, published_at: new Date().toISOString() };
  // Keep old immutable objects so already-open browsers retain consistent snapshots.
  if (previous && !previous.etag) throw new Error("Missing manifest ETag; refusing an unsafe concurrent update");
  const result = await store.setJSON("manifest", published, previous ? { onlyIfMatch: previous.etag } : { onlyIfNew: true });
  if (!result.modified) {
    // A retry after a lost PUT response may find our own successful publication.
    const current = await store.get("manifest", { type: "json", consistency: "strong" });
    if (current?.export_id !== manifest.export_id) throw new Error("Another publisher changed the release; retry publishing");
  }
  const confirmed = await store.get("manifest", { type: "json", consistency: "strong" });
  if (confirmed?.export_id !== manifest.export_id) throw new Error("Release verification failed");
  return { changed: true, uploaded, published_at: published.published_at };
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
  const directory = resolve(root, process.argv.slice(2).find(x => !x.startsWith("--")) || "data/publication");
  const manifest = JSON.parse(await readFile(resolve(directory, "manifest.json"), "utf8"));
  const proxy = process.env.https_proxy || process.env.HTTPS_PROXY || process.env.http_proxy || process.env.HTTP_PROXY;
  const agent = proxy ? new HttpsProxyAgent(proxy) : undefined;
  let result;
  try {
    const store = getStore({ name: "research-results", ...(await credentials()), consistency: "strong",
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
    result = await publishResults(store, manifest, key => readFile(resolve(directory, key)), (done, total) => {
      if (done % 20 === 0 || done === total) console.error(`结果对象：${done}/${total}`);
    });
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
