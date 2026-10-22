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
test("previously published hashes reuse without remote HEAD/PUT but still validate local bytes", async () => {
  const s = store(), f = fixture(), events = [];
  s.values.set("manifest", { ...f.manifest, export_id: "b".repeat(64) });
  s.set = s.getMetadata = async () => { throw new Error("redundant network call"); };
  await publishResults(s, f.manifest, async () => f.data, (_, __, e) => events.push(e));
  assert.equal(events.at(-1).reused, 1);
  assert.equal(events.at(-1).uploaded_bytes, 0);
  s.values.set("manifest", { ...f.manifest, export_id: "b".repeat(64) });
  await assert.rejects(publishResults(s, f.manifest, async () => Buffer.from("bad")), /checksum/);
});
test("durable journal supports retry after an interrupted release without repeating remote requests", async () => {
  const s = store(), f = fixture(), verified = new Set();
  let saved = 0;
  s.setJSON = async () => { throw new Error("manifest interrupted"); };
  await assert.rejects(publishResults(s, f.manifest, async () => f.data, undefined, undefined, 2, verified, async () => saved++), /manifest interrupted/);
  assert.equal(saved, 1); assert.equal(verified.size, 1);
  s.set = s.getMetadata = async () => { throw new Error("must reuse verified journal"); };
  s.setJSON = async (key, value) => { s.values.set(key, value); return { modified: true }; };
  const result = await publishResults(s, f.manifest, async () => f.data, undefined, undefined, 2, verified);
  assert.equal(result.uploaded, 0); assert.equal(result.reused, 1);
});
test("worker queue continues while the first upload is slow and limits in-flight writes", async () => {
  const s = store(), manifest = { schema_version: 2, export_id: "a".repeat(64), objects: {} }, data = new Map();
  for (let i = 0; i < 6; i++) {
    const body = gzipSync(Buffer.from(String(i))), sha256 = createHash("sha256").update(body).digest("hex"), key = `objects/${sha256}.json.gz`;
    manifest.objects[i] = { key, sha256, bytes: body.byteLength }; data.set(key, body);
  }
  const first = Object.values(manifest.objects)[0].key;
  let release, active = 0, maximum = 0, count = 0;
  const gate = new Promise(resolve => { release = resolve; });
  const timer = setTimeout(() => release(), 1000);
  const write = s.set;
  s.set = async (key, ...args) => {
    active++; maximum = Math.max(maximum, active); count++;
    if (count === 4) release();
    if (key === first) { await gate; assert.ok(count >= 4, "slow first object stalled queue"); }
    const result = await write(key, ...args); active--; return result;
  };
  try { await publishResults(s, manifest, key => data.get(key), undefined, undefined, 2); }
  finally { clearTimeout(timer); }
  assert.equal(maximum, 2); assert.equal(s.writes.at(-1), "manifest");
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

for (const schema of [1, 2]) test(`browser adapter schema ${schema} preserves nulls, horizon and pinned release across refresh`, async () => {
  const originalFetch = globalThis.fetch, originalMode = process.env.NEXT_PUBLIC_DATA_MODE;
  process.env.NEXT_PUBLIC_DATA_MODE = "published";
  let version = 1, failManifest = false, manifestRequests = 0;
  const manifest = v => ({ schema_version: schema, export_id: `release${v}`, published_at: "2026-10-19", indices: [], objects: {
    "index/test": { key: `index${v}` }, [`factor/s${v}/spread`]: { key: `factor${v}` },
    "prices/test/2026": { key: `prices${v}` }, "research/test/2026": { key: `research${v}` },
  } });
  globalThis.fetch = async input => {
    const key = new URL(input, "https://test").searchParams.get("key");
    if (key === "manifest") {
      manifestRequests++;
      if (failManifest) throw new Error("offline");
      return Response.json(manifest(version));
    }
    const v = Number(key.slice(-1));
    const prices = [{ date: "2026-10-16", close: 100 }, { date: "2026-10-19", close: 101 }];
    const analysis = { analysis_version: "position-match-v1", as_of: `release${v}`, current: v, outcomes: [{ horizon: 252 }] };
    if (key.startsWith("prices")) return Response.json({ prices, returns: { "252": [.12, null] } });
    if (key.startsWith("research")) return Response.json({ dates: prices.map(p => p.date), factors: {
      spread: { values: [[null, "missing_source"], [v, "valid"]], rolling: { "252": [] } } } });
    if (key.startsWith("index")) return Response.json({
      ...(schema === 2 ? { price_chunks: ["prices/test/2026"], research_chunks: ["research/test/2026"], analyses: { spread: analysis } } : {}), code: "test", snapshot_id: `s${v}`, updated_at: "2026-10-19",
      prices: [{ date: "2026-10-16", close: 100 }, { date: "2026-10-19", close: 101 }],
      coverage: { rows: 2, last_date: "2026-10-19", factor_coverage: { spread: { valid: 1 } } }, definitions: { spread: { id: "spread" } },
      evaluations: { spread: { "20": { horizon: 20 }, "252": { horizon: 252 } } },
      assessments: { "20": { label: "test" } }, returns: { "252": [.12, null] } });
    return Response.json({ snapshot_id: `s${v}`, values: [[null, "missing_source"], [v, "valid"]], rolling: { "252": [] }, components: [],
      analysis: { analysis_version: "position-match-v1", as_of: `release${v}`, current: v, outcomes: [{ horizon: 252 }] } });
  };
  try {
    const source = await readFile(new URL("../lib/data-api.ts", import.meta.url), "utf8");
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
    const api = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}#schema${schema}`);
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

test("same-snapshot republication preserves each dashboard's release binding and retries transient chunk failures", async () => {
  const originalFetch = globalThis.fetch, originalMode = process.env.NEXT_PUBLIC_DATA_MODE;
  process.env.NEXT_PUBLIC_DATA_MODE = "published";
  let version = 1, failedOnce = false;
  globalThis.fetch = async input => {
    const key = new URL(input, "https://test").searchParams.get("key");
    if (key === "manifest") return Response.json({ schema_version: 2, export_id: `v${version}`, indices: [], published_at: String(version), objects: {
      "index/test": { key: `index${version}` }, price: { key: "price" }, research: { key: "research" },
    } });
    if (key === "price") {
      if (!failedOnce) { failedOnce = true; throw new TypeError("temporary connection reset"); }
      return Response.json({ prices: [{ date: "2026-10-22", close: 100 }], returns: { "20": [null] } });
    }
    if (key === "research") return Response.json({ dates: ["2026-10-22"], factors: { test: { values: [[1, "ok"]], rolling: { "20": [] } } } });
    const v = Number(key.at(-1));
    return Response.json({ code: "test", snapshot_id: "same", coverage: { rows: 1, last_date: "2026-10-22", factor_coverage: { test: {} } },
      definitions: { test: { id: "test" } }, evaluations: { test: { "20": {} } }, assessments: { "20": { label: `version${v}` } },
      price_chunks: ["price"], research_chunks: ["research"], analyses: { test: { current: v } } });
  };
  try {
    const source = await readFile(new URL("../lib/data-api.ts", import.meta.url), "utf8");
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
    const api = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}#same-snapshot`);
    const first = await api.readDashboard("test", 20);
    version = 2; await api.refreshPublication();
    const second = await api.readDashboard("test", 20);
    assert.equal(second.assessment.label, "version2");
    assert.equal((await api.readFactor("test", "test", 20, "same", undefined, first.publication_id)).analysis.current, 1);
    assert.equal((await api.readFactor("test", "test", 20, "same", undefined, second.publication_id)).analysis.current, 2);
  } finally { globalThis.fetch = originalFetch; if (originalMode === undefined) delete process.env.NEXT_PUBLIC_DATA_MODE; else process.env.NEXT_PUBLIC_DATA_MODE = originalMode; }
});
