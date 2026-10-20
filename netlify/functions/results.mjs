import { getStore } from "@netlify/blobs";

export async function serveResults(request, store) {
  const headers = { "Content-Type": "application/json; charset=utf-8", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store" };
  const error = (status, detail) => Response.json({ detail }, { status, headers });
  if (request.method !== "GET" && request.method !== "HEAD") return new Response(null, { status: 405, headers: { ...headers, Allow: "GET, HEAD" } });
  const key = new URL(request.url).searchParams.get("key") || "manifest";
  if (key !== "manifest" && !/^objects\/[a-f0-9]{64}\.json\.gz$/.test(key)) return error(400, "无效的结果标识");
  try {
    if (key === "manifest") {
      const value = await store.get("manifest", { type: "json", consistency: "strong" });
      if (!value) return error(503, "研究结果尚未发布，请稍后重试");
      return new Response(request.method === "HEAD" ? null : JSON.stringify(value), { headers });
    }
    const data = await store.get(key, { type: "arrayBuffer" });
    if (!data) return error(404, "研究结果不存在，请重新读取最新快照");
    return new Response(request.method === "HEAD" ? null : data, { headers: {
      ...headers, "Content-Encoding": "gzip", "Cache-Control": "public, max-age=31536000, immutable",
      "Netlify-CDN-Cache-Control": "public, max-age=31536000, immutable", ETag: `"${key.slice(8, 72)}"`,
    } });
  } catch {
    return error(503, "研究结果暂时无法读取，请稍后重试");
  }
}

export default async function handler(request) {
  try { return await serveResults(request, getStore({ name: "research-results", consistency: "strong" })); }
  catch { return Response.json({ detail: "研究结果存储尚未就绪" }, { status: 503, headers: { "Cache-Control": "no-store" } }); }
}
