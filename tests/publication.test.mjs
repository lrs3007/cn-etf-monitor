import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import { publishResults } from "../scripts/publish-results.mjs";
import { serveResults } from "../netlify/functions/results.mjs";
import { readFile } from "node:fs/promises";
import ts from "typescript";

function fixture() {
  const data = gzipSync(Buffer.from('{"prices":[1,2],"values":[null,3]}'));
  const hash = createHash("sha256").update(data).digest("hex");
  return { data, manifest: { schema_version: 1, export_id: "a".repeat(64), objects: {
    "index/test": { key: `objects/${hash}.json.gz`, bytes: data.byteLength, sha256: hash },
  } } };
}
function store() {
  const values = new Map(), writes = [];
  return { values, writes,
    async getWithMetadata(k) { return values.has(k) ? { data: values.get(k), etag: "etag" } : null; },
    async get(k) { return values.get(k) ?? null; },
    async getMetadata(k) { return values.has(k) ? { etag: "etag", metadata: {} } : null; },
    async set(k, v) { writes.push(k); values.set(k, v); return { modified: true }; },
    async setJSON(k, v) { writes.push(k); values.set(k, v); return { modified: true }; },
  };
}
test("objects publish before manifest; unchanged export does not write", async () => {
  const s = store(), f = fixture();
  assert.equal((await publishResults(s, f.manifest, async () => f.data)).changed, true);
  assert.equal(s.writes.at(-1), "manifest");
  assert.equal(s.values.get("manifest").export_id, f.manifest.export_id);
  const count = s.writes.length;
  const unchanged = await publishResults(s, f.manifest, async () => { throw new Error(); });
  assert.equal(unchanged.changed, false);
  assert.equal(unchanged.published_at, s.values.get("manifest").published_at);
  assert.equal(s.writes.length, count);
});
test("interrupted or corrupt upload retains old release", async () => {
  const s = store(), f = fixture(), previous = { export_id: "b".repeat(64) };
  s.values.set("manifest", previous);
  await assert.rejects(publishResults(s, f.manifest, async () => Buffer.from("corrupt")), /checksum/);
  assert.equal(s.values.get("manifest"), previous);
  s.set = async () => { throw new Error("network"); };
  await assert.rejects(publishResults(s, f.manifest, async () => f.data), /network/);
  assert.equal(s.values.get("manifest"), previous);
});
test("concurrent publishers must compare manifest ETag", async () => {
  const s = store(), f = fixture();
  s.values.set("manifest", { export_id: "b".repeat(64) });
  s.setJSON = async (k, v, options) => { assert.equal(options.onlyIfMatch, "etag"); return { modified: false }; };
  await assert.rejects(publishResults(s, f.manifest, async () => f.data), /Another publisher/);
});
test("a reported write without a durable object must never switch manifest", async () => {
  const s = store(), f = fixture();
  s.set = async () => ({ modified: true });
  await assert.rejects(publishResults(s, f.manifest, async () => f.data), /not durable/);
  assert.equal(s.values.has("manifest"), false);
});
test("progress counts durable objects and bytes, including reused objects", async () => {
  const s = store(), f = fixture(), events = [];
  s.values.set(Object.values(f.manifest.objects)[0].key, f.data);
  await publishResults(s, f.manifest, async () => f.data, (done, total, extra) => events.push({ done, total, ...extra }));
  assert.deepEqual(events.map(e => e.done), [0, 1, 1]);
  assert.equal(events[1].completed_bytes, f.data.byteLength);
  assert.equal(events[1].total_bytes, f.data.byteLength);
  assert.equal(events.at(-1).phase, "finalize");
});
test("cancelling after uploads retains old manifest and allows retry", async () => {
  const s = store(), f = fixture(), previous = { export_id: "b".repeat(64) };
  s.values.set("manifest", previous);
  let cancelled = false;
  await assert.rejects(publishResults(s, f.manifest, async () => f.data,
    (done, total) => { if (done === total) cancelled = true; },
    async () => { if (cancelled) throw new Error("cancelled"); }), /cancelled/);
  assert.equal(s.values.get("manifest"), previous);
  assert.equal((await publishResults(s, f.manifest, async () => f.data)).changed, true);
});
test("public endpoint is read-only, validates keys and caches only immutable objects", async () => {
  const s = store(), f = fixture(), key = Object.values(f.manifest.objects)[0].key;
  s.values.set("manifest", f.manifest); s.values.set(key, new Uint8Array(f.data).buffer);
  const request = (key, method = "GET") => new Request(`https://test/.netlify/functions/results?key=${encodeURIComponent(key)}`, { method });
  assert.equal((await serveResults(request("manifest", "POST"), s)).status, 405);
  assert.equal((await serveResults(request("../../.env"), s)).status, 400);
  const m = await serveResults(request("manifest"), s);
  assert.equal(m.headers.get("cache-control"), "no-store");
  assert.deepEqual(await m.json(), f.manifest);
  const result = await serveResults(request(key), s);
  assert.equal(result.headers.get("content-encoding"), "gzip");
  assert.match(result.headers.get("cache-control"), /immutable/);
  assert.deepEqual(Buffer.from(await result.arrayBuffer()), f.data);
  assert.equal((await serveResults(request("manifest", "HEAD"), s)).body, null);
});

test("browser adapter preserves nulls, horizon and pinned release across refresh", async () => {
  const originalFetch = globalThis.fetch, originalMode = process.env.NEXT_PUBLIC_DATA_MODE;
  process.env.NEXT_PUBLIC_DATA_MODE = "published";
  let version = 1, failManifest = false, manifestRequests = 0;
  const manifest = v => ({ schema_version: 1, export_id: `release${v}`, published_at: "2026-10-19", indices: [], objects: {
    "index/test": { key: `index${v}` }, [`factor/s${v}/spread`]: { key: `factor${v}` },
  } });
  globalThis.fetch = async input => {
    const key = new URL(input, "https://test").searchParams.get("key");
    if (key === "manifest") {
      manifestRequests++;
      if (failManifest) throw new Error("offline");
      return Response.json(manifest(version));
    }
    const v = Number(key.slice(-1));
    if (key.startsWith("index")) return Response.json({ code: "test", snapshot_id: `s${v}`, updated_at: "2026-10-19",
      prices: [{ date: "2026-10-16", close: 100 }, { date: "2026-10-19", close: 101 }],
      coverage: { factor_coverage: { spread: { valid: 1 } } }, definitions: { spread: { id: "spread" } },
      evaluations: { spread: { "20": { horizon: 20 }, "252": { horizon: 252 } } },
      assessments: { "20": { label: "test" } }, returns: { "252": [.12, null] } });
    return Response.json({ snapshot_id: `s${v}`, values: [[null, "missing_source"], [v, "valid"]], rolling: { "252": [] }, components: [],
      analysis: { analysis_version: "position-match-v1", as_of: `release${v}`, current: v, outcomes: [{ horizon: 252 }] } });
  };
  try {
    const source = await readFile(new URL("../lib/data-api.ts", import.meta.url), "utf8");
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
    const api = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
    const dashboard = await api.readDashboard("test", 20);
    assert.equal((await api.checkPublication()).changed, false);
    version = 2;
    const before = manifestRequests;
    const [automatic] = await Promise.all([api.checkPublication(), api.refreshPublication()]);
    assert.equal(automatic.changed, true);
    assert.equal(manifestRequests, before + 1); // Focus/manual/timer checks share one request.
    const detail = await api.readFactor("test", "spread", 252, dashboard.snapshot_id);
    assert.equal(detail.snapshot_id, "s1");
    assert.equal(detail.evaluation.horizon, 252);
    assert.equal(detail.analysis.as_of, "release1"); // Analysis pins to the same release as prices and factor values.
    assert.equal(detail.analysis.current, 1);
    assert.deepEqual(detail.series, [
      { date: "2026-10-16", value: null, status: "missing_source", forward_return: .12 },
      { date: "2026-10-19", value: 1, status: "valid", forward_return: null },
    ]);
    assert.equal((await api.readDashboard("test", 20)).snapshot_id, "s2");
    assert.equal((await api.readFactor("test", "spread", 252, "s2")).analysis.current, 2);
    assert.equal((await api.checkPublication()).changed, false);
    failManifest = true;
    await assert.rejects(api.checkPublication(), /offline/);
    assert.equal((await api.readDashboard("test", 20)).snapshot_id, "s2"); // Keep the last good release offline.
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.NEXT_PUBLIC_DATA_MODE; else process.env.NEXT_PUBLIC_DATA_MODE = originalMode;
  }
});
