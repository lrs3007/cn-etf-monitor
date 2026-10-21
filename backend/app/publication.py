"""Export immutable, full-history research results; never export the database itself.

Call under pipeline.lock so the five active snapshots form a consistent release.
The manifest is written last. Remote publishing follows the same protocol.
"""
import gzip
import hashlib
import json
import os
import selectors
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

from sqlalchemy import select

from .config import DATA_DIR, HORIZONS, INDICES, ROOT, SPREAD_FACTOR, factor_horizons, now_iso
from .db import SessionLocal
from .factor_analysis import web_analysis
from .job_control import cancellation_file, check_cancelled, report_progress
from .models import (Assessment, Bar, Evaluation, FactorInput, FactorValue, ForwardReturn,
                     MacroObservation, MarketIndex, RollingEvaluation, Snapshot, ValuationObservation)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def public_coverage(coverage):
    # Explicit public fields: raw provider errors and local provenance paths stay local.
    fields = ("first_date", "last_date", "rows", "base_date", "launch_date", "backcast_rows",
              "volume_rows", "amount_rows", "valuation_rows", "cross_source_checked",
              "cross_source_through", "calendar_verified", "notes", "scope", "bond_series", "factor_coverage")
    result = {key: coverage[key] for key in fields if key in coverage}
    for key in ("provider_errors", "bond_provider_errors"):
        result[key] = [f"部分数据源存在采集提示（{len(coverage[key])}项），详细日志保留本机。"] if coverage.get(key) else []
    return result


def public_source(source):
    return {k: source[k] for k in ("source", "sha256", "fetched_at", "availability_assumed") if k in source} if source else None


def publish_current_results():
    """Called with pipeline.lock held, including throughout the network upload."""
    manifest = export_results()
    # stderr inherits the local worker log; stdout contains only the final JSON receipt.
    try:
        if cancellation_file():
            process = run_publisher()
        else:
            process = subprocess.run(["node", "scripts/publish-results.mjs"], cwd=ROOT,
                                     stdout=subprocess.PIPE, text=True, check=False, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("网页同步超时，本机计算结果已保留") from exc
    if process.returncode:
        raise RuntimeError("本机数据已更新，但网页同步失败，请检查本机发布日志")
    try:
        receipt = json.loads(process.stdout)
        if (receipt.get("export_id") != manifest["export_id"] or type(receipt.get("changed")) is not bool
                or not isinstance(receipt.get("published_at"), str)):
            raise ValueError("invalid publication receipt")
    except (ValueError, AttributeError, TypeError) as exc:
        raise RuntimeError("网页同步回执不完整，尚无法确认发布结果") from exc
    return {"status": "published" if receipt["changed"] else "unchanged",
            "export_id": manifest["export_id"], "published_at": receipt["published_at"],
            "finished_at": now_iso()}


def run_publisher():
    """Stream structured progress without exposing uploader logs or credentials."""
    report_progress("upload", "连接网页结果存储，核对待同步对象")
    env = {**os.environ, "CN_MONITOR_CANCEL_FILE": cancellation_file()}
    deadline, buffer, receipt = time.monotonic() + 1800, b"", []
    with subprocess.Popen(["node", "scripts/publish-results.mjs", "--progress"], cwd=ROOT,
                          stdout=subprocess.PIPE, env=env) as process:
        try:
            os.set_blocking(process.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    check_cancelled()
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(process.args, 1800)
                    if not selector.select(.25):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.startswith(b"CN_MONITOR_PROGRESS "):
                            item = json.loads(line[len(b"CN_MONITOR_PROGRESS "):])
                            phase = item.get("phase", "upload")
                            report_progress(phase, "确认并切换完整网页版本" if phase == "finalize" else "核对并同步网页结果对象",
                                item.get("completed"), item.get("total"), "个对象",
                                completed_bytes=item.get("completed_bytes"), total_bytes=item.get("total_bytes"))
                        elif line.strip():
                            receipt.append(line.decode())
            if buffer.strip():
                receipt.append(buffer.decode())
            process.wait(timeout=max(1, deadline - time.monotonic()))
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    return SimpleNamespace(returncode=process.returncode, stdout="\n".join(receipt))


def export_results(directory=None, session_factory=SessionLocal, specs=INDICES):
    root = Path(directory or DATA_DIR / "publication")
    objects = root / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "indices": [], "objects": {}}

    def write(name, payload):
        data = gzip.compress(encoded(payload), compresslevel=6, mtime=0)
        digest = hashlib.sha256(data).hexdigest()
        key = f"objects/{digest}.json.gz"
        target = root / key
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
        manifest["objects"][name] = {"key": key, "bytes": len(data), "sha256": digest}

    with session_factory() as session:
        total_objects = 0
        for spec in specs:
            market = session.get(MarketIndex, spec.code)
            snapshot = session.get(Snapshot, market.active_snapshot) if market and market.active_snapshot else None
            if snapshot:
                total_objects += 1 + len(snapshot.coverage["factor_definitions"])
        for spec in specs:
            check_cancelled()
            report_progress("export", f"{spec.short_name}：导出全历史与逐因子分析", len(manifest["objects"]), total_objects, "份结果")
            market = session.get(MarketIndex, spec.code)
            snap = session.get(Snapshot, market.active_snapshot) if market and market.active_snapshot else None
            if not snap or snap.status != "published" or snap.index_code != spec.code:
                raise ValueError(f"{spec.code} 缺少可发布快照；请先完成回填")
            coverage = public_coverage(snap.coverage)
            definitions = snap.coverage["factor_definitions"]
            bars = session.scalars(select(Bar).where(Bar.snapshot_id == snap.id).order_by(Bar.trade_date)).all()
            dates = [b.trade_date for b in bars]
            if not dates or len(dates) != coverage["rows"] or dates[-1] != coverage["last_date"]:
                raise ValueError(f"{spec.code} 行情覆盖不完整")
            assessments = {str(h): session.get(Assessment, (snap.id, h)) for h in HORIZONS}
            if any(item is None for item in assessments.values()):
                raise ValueError(f"{spec.code} 缺少综合评估期限")
            evaluations = {}
            for item in session.scalars(select(Evaluation).where(Evaluation.snapshot_id == snap.id)):
                evaluations.setdefault(item.factor_id, {})[str(item.horizon)] = item.result
            returns = {}
            for h in sorted({h for fid in definitions for h in factor_horizons(fid)}):
                rows = session.scalars(select(ForwardReturn).where(ForwardReturn.snapshot_id == snap.id,
                    ForwardReturn.horizon == h).order_by(ForwardReturn.trade_date)).all()
                if [r.trade_date for r in rows] != dates:
                    raise ValueError(f"{spec.code} {h}日收益序列缺失")
                returns[str(h)] = [r.value for r in rows]
            write(f"index/{spec.code}", {"code": spec.code, "snapshot_id": snap.id, "updated_at": snap.created_at,
                "coverage": coverage, "definitions": definitions, "evaluations": evaluations,
                "assessments": {h: item.result for h, item in assessments.items()}, "returns": returns,
                "prices": [{"date": b.trade_date, **{k: getattr(b, k) for k in
                    ("open", "high", "low", "close", "volume", "amount", "history_type")}} for b in bars]})
            for fid, definition in definitions.items():
                report_progress("export", f"{spec.short_name}：导出{definition['name']}分析", len(manifest["objects"]), total_objects, "份结果")
                if any(str(h) not in evaluations.get(fid, {}) for h in factor_horizons(fid)):
                    raise ValueError(f"{spec.code} {fid} 缺少IC期限")
                values = session.scalars(select(FactorValue).where(FactorValue.snapshot_id == snap.id,
                    FactorValue.factor_id == fid).order_by(FactorValue.trade_date)).all()
                if [v.trade_date for v in values] != dates:
                    raise ValueError(f"{spec.code} {fid} 未覆盖全部交易日")
                rolling = {str(h): [] for h in factor_horizons(fid)}
                for r in session.scalars(select(RollingEvaluation).where(RollingEvaluation.snapshot_id == snap.id,
                        RollingEvaluation.factor_id == fid).order_by(RollingEvaluation.as_of)):
                    rolling[str(r.horizon)].append({"date": r.as_of, "ic": r.ic, "samples": r.sample_count})
                components = []
                if fid == SPREAD_FACTOR:
                    rows = session.execute(select(FactorInput, ValuationObservation, MacroObservation)
                        .outerjoin(ValuationObservation, FactorInput.valuation_id == ValuationObservation.id)
                        .outerjoin(MacroObservation, FactorInput.bond_id == MacroObservation.id)
                        .where(FactorInput.snapshot_id == snap.id, FactorInput.factor_id == fid)
                        .order_by(FactorInput.trade_date)).all()
                    components = [{"date": row.trade_date, "observation_date": pe.observation_date if pe else None,
                        "available_date": max(pe.available_date, bond.available_date) if pe and bond else None,
                        "pe_ttm": pe.pe_ttm if pe else None, "earnings_yield": 100 / pe.pe_ttm if pe and pe.pe_ttm > 0 else None,
                        "bond_yield": bond.value if bond else None,
                        "valuation_source": public_source(pe.provenance) if pe else None,
                        "bond_source": public_source(bond.provenance) if bond else None} for row, pe, bond in rows]
                write(f"factor/{snap.id}/{fid}", {"snapshot_id": snap.id,
                    "values": [[v.value, v.status] for v in values], "rolling": rolling, "components": components,
                    "analysis": web_analysis(dates, [b.close for b in bars], [v.value for v in values],
                        definition, evaluations[fid], spec.launch_date)})
            manifest["indices"].append({"code": spec.code, "name": spec.name, "short_name": spec.short_name,
                "description": spec.description, "ready": True, "coverage": coverage, "snapshot_id": snap.id})
        report_progress("export", "全历史与逐因子分析导出完成", len(manifest["objects"]), total_objects, "份结果")
    manifest["export_id"] = hashlib.sha256(encoded(manifest)).hexdigest()
    check_cancelled()
    manifest["generated_at"] = now_iso()
    temporary = root / "manifest.tmp"
    temporary.write_bytes(encoded(manifest))
    temporary.replace(root / "manifest.json")
    return manifest
