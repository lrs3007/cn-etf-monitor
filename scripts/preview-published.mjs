// Preview the static website against locally exported results, without Python/API.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, extname, sep } from "node:path";
import { serveResults } from "../netlify/functions/results.mjs";

const root = resolve(".next-published"), data = resolve("data/publication");
const types = { ".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2" };
const store = { async get(key, options) {
  if (key === "manifest") {
    const m = JSON.parse(await readFile(resolve(data, "manifest.json"), "utf8"));
    return { ...m, published_at: m.generated_at };
  }
  try { const b = await readFile(resolve(data, key)); return new Uint8Array(b).buffer; } catch { return null; }
} };
createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://127.0.0.1:3001");
    if (url.pathname === "/.netlify/functions/results") {
      const result = await serveResults(new Request(url, { method: req.method }), store);
      res.writeHead(result.status, Object.fromEntries(result.headers));
      res.end(Buffer.from(await result.arrayBuffer())); return;
    }
    if (req.method !== "GET" && req.method !== "HEAD") { res.writeHead(405); res.end(); return; }
    const file = resolve(root, `.${decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname)}`);
    if (!file.startsWith(root + sep)) { res.writeHead(403); res.end(); return; }
    const body = await readFile(file);
    res.writeHead(200, { "Content-Type": types[extname(file)] || "application/octet-stream" });
    res.end(req.method === "HEAD" ? undefined : body);
  } catch { res.writeHead(404); res.end("Not found"); }
}).listen(3001, "127.0.0.1", () => console.log("Published preview: http://127.0.0.1:3001"));
