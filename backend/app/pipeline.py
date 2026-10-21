import hashlib
import fcntl
import json
import uuid

import numpy as np
from sqlalchemy import insert, select

from .config import DATA_DIR, ENGINE_VERSION, HORIZONS, INDICES, SPREAD_FACTOR, factor_horizons, now_iso
from .db import SessionLocal, init_db
from .evaluation import build_assessment, evaluate_factor, finite, forward_returns
from .factors import REGISTRY, calculate_all
from .models import Assessment, Bar, Evaluation, FactorDefinition, FactorValue, ForwardReturn, JobRun, MarketIndex, RollingEvaluation, Snapshot, MacroSeries, MacroObservation, ValuationObservation, FactorInput
from .providers import full_history
from .job_control import check_cancelled, job_started_at, report_progress


def _stored_research_inputs(session, snapshot_id):
    import pandas as pd
    bars = session.scalars(select(Bar).where(Bar.snapshot_id == snapshot_id).order_by(Bar.trade_date)).all()
    frame = pd.DataFrame([{"date": b.trade_date, "close": b.close} for b in bars])
    values = session.scalars(select(FactorValue).where(FactorValue.snapshot_id == snapshot_id)
        .order_by(FactorValue.factor_id, FactorValue.trade_date)).all()
    grouped = {}
    for row in values:
        grouped.setdefault(row.factor_id, [[], []])
        grouped[row.factor_id][0].append(row.value)
        grouped[row.factor_id][1].append(row.status)
    factors = {key: (pd.Series(parts[0], dtype=float), pd.Series(parts[1])) for key, parts in grouped.items()}
    return frame, factors


def refresh_diagnostics(specs=INDICES):
    from .diagnostics import diagnose_all
    for spec in specs:
        with SessionLocal.begin() as session:
            market = session.get(MarketIndex, spec.code)
            if not market or not market.active_snapshot:
                continue
            frame, factors = _stored_research_inputs(session, market.active_snapshot)
            results = diagnose_all(frame, factors, spec.launch_date)
            for evaluation in session.scalars(select(Evaluation).where(Evaluation.snapshot_id == market.active_snapshot)).all():
                evaluation.result = {**evaluation.result, "predictive_diagnostics": results[evaluation.factor_id]}
        print(f"[{spec.short_name}] 增量预测诊断与FDR校正完成", flush=True)


def refresh_assessments(specs=INDICES):
    """Refresh the versioned summary view without duplicating historical factor datasets."""
    for spec in specs:
        with SessionLocal.begin() as session:
            market = session.get(MarketIndex, spec.code)
            if not market or not market.active_snapshot:
                continue
            snapshot = session.get(Snapshot, market.active_snapshot)
            frame, factors = _stored_research_inputs(session, snapshot.id)
            for h in HORIZONS:
                evaluations = {e.factor_id: e.result for e in session.scalars(select(Evaluation).where(
                    Evaluation.snapshot_id == snapshot.id, Evaluation.horizon == h)).all()}
                result = build_assessment(frame, factors, evaluations, snapshot.coverage["factor_definitions"], h)
                session.get(Assessment, (snapshot.id, h)).result = result
        print(f"[{spec.short_name}] 综合观察已更新为规则v5", flush=True)


def seed():
    # API and scheduler may start together; both share data/ in the supported deployment.
    with (DATA_DIR / "schema.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        init_db()
        with SessionLocal.begin() as session:
            for spec in INDICES:
                if not session.get(MarketIndex, spec.code):
                    session.add(MarketIndex(code=spec.code, name=spec.name, base_date=spec.base_date, launch_date=spec.launch_date))
            for factor in REGISTRY.values():
                if not session.get(FactorDefinition, (factor.id, factor.version)):
                    session.add(FactorDefinition(id=factor.id, version=factor.version, metadata_json=factor.metadata()))


def bulk(session, model, rows, chunk=2000):
    for start in range(0, len(rows), chunk):
        session.execute(insert(model), rows[start:start + chunk])


def process_index(spec, offline=False, force=False, bond_context=None):
    report_progress("collect_index", f"{spec.short_name}：采集并核对历史数据", index=spec.code)
    print(f"[{spec.short_name}] 获取全部历史…", flush=True)
    frame, coverage = full_history(spec, offline=offline, bond_context=bond_context)
    lineage = frame.attrs.get("spread_inputs", {"inputs": [], "valuations": [], "bonds": []})
    if len(frame) < 60:
        raise ValueError("历史少于60个交易日，拒绝替换已有数据")
    metadata = {key: definition.metadata() for key, definition in REGISTRY.items()}
    dataset_hash = hashlib.sha256((frame.to_csv(index=False) + json.dumps(metadata, sort_keys=True)
        + json.dumps(lineage["inputs"], sort_keys=True) + ENGINE_VERSION).encode()).hexdigest()
    with SessionLocal() as session:
        market = session.get(MarketIndex, spec.code)
        previous = session.get(Snapshot, market.active_snapshot) if market.active_snapshot else None
        if previous and previous.dataset_hash == dataset_hash and not force:
            return {"index": spec.code, "snapshot": previous.id, "status": "unchanged", "rows": len(frame),
                "first_date": frame.date.iloc[0], "last_date": frame.date.iloc[-1], "warnings": coverage["provider_errors"]}
        if previous:
            if (len(frame) < previous.coverage["rows"] or frame.date.iloc[-1] < previous.coverage["last_date"]
                or frame.date.iloc[0] > previous.coverage["first_date"]):
                raise ValueError("新数据历史缩短或日期倒退，保留原快照；检查数据源")
            for field in ("volume_rows", "amount_rows", "valuation_rows"):
                if coverage[field] < previous.coverage.get(field, 0):
                    raise ValueError(f"{field}覆盖倒退，保留原快照；检查补充数据源")
            prior_spread = previous.coverage["factor_coverage"].get(SPREAD_FACTOR, {}).get("valid", 0)
            current_spread = ((frame.spread_pe_ttm > 0) & frame.cgb_10y_yield.notna()).sum()
            if current_spread < prior_spread:
                raise ValueError("股债收益差覆盖倒退，保留原快照；检查国债或PE数据源")
    print(f"[{spec.short_name}] {len(frame)}日，{frame.date.iloc[0]} → {frame.date.iloc[-1]}；计算{len(REGISTRY)}因子全历史…", flush=True)
    factors = calculate_all(frame)
    check_cancelled()
    snapshot_id = uuid.uuid4().hex
    coverage["factor_definitions"] = metadata
    coverage["factor_coverage"] = {}
    all_horizons = sorted({h for key in factors for h in factor_horizons(key)})
    evaluations = {h: {} for h in all_horizons}
    rolling_rows = []
    done, total = 0, sum(len(factor_horizons(key)) for key in factors)
    for key, (values, status) in factors.items():
        check_cancelled()
        valid = values.notna()
        coverage["factor_coverage"][key] = {"total": len(values), "valid": int(valid.sum()),
            "warmup": int((status == "warmup").sum()), "missing": int((status == "missing_source").sum()),
            "undefined": int((status == "undefined").sum()),
            "first_valid": frame.loc[valid, "date"].iloc[0] if valid.any() else None,
            "last_valid": frame.loc[valid, "date"].iloc[-1] if valid.any() else None}
        for h in factor_horizons(key):
            report_progress("calculate", f"{spec.short_name}：{metadata[key]['name']} · {h}日检验", done, total, "项检验", index=spec.code)
            evaluation, rolling = evaluate_factor(frame, values, h, spec.launch_date, key)
            done += 1
            evaluations[h][key] = evaluation
            for row in rolling:
                rolling_rows.append({"snapshot_id": snapshot_id, "factor_id": key, "horizon": h, **row})
        print(f"[{spec.short_name}] {key}: {int(valid.sum())}/{len(frame)}", flush=True)
    from .diagnostics import diagnose_all
    report_progress("diagnostics", f"{spec.short_name}：增量预测与方向稳定性检验", done, total, "项检验", index=spec.code)
    diagnostics = diagnose_all(frame, factors, spec.launch_date)
    check_cancelled()
    for h in all_horizons:
        for key in evaluations[h]:
            evaluations[h][key]["predictive_diagnostics"] = diagnostics[key]
    # Publication is one transaction. Readers keep seeing the previous complete snapshot until commit.
    report_progress("save", f"{spec.short_name}：保存完整历史快照", index=spec.code)
    with SessionLocal.begin() as session:
        session.add(Snapshot(id=snapshot_id, index_code=spec.code, created_at=now_iso(), engine_version=ENGINE_VERSION,
            dataset_hash=dataset_hash, status="published", coverage=coverage))
        session.flush()
        from .equity_bond import SERIES_ID, SERIES_METADATA
        if not session.get(MacroSeries, SERIES_ID):
            session.add(MacroSeries(id=SERIES_ID, metadata_json=SERIES_METADATA))
            session.flush()
        for model, rows in ((MacroObservation, lineage["bonds"]), (ValuationObservation, lineage["valuations"])):
            if rows:
                existing = set(session.scalars(select(model.id)))
                bulk(session, model, [row for row in rows if row["id"] not in existing])
        session.flush()
        bulk(session, FactorInput, [{"snapshot_id": snapshot_id, **row} for row in lineage["inputs"]])
        bars = []
        for row in frame.to_dict("records"):
            bars.append({"snapshot_id": snapshot_id, "trade_date": row["date"],
                **{col: finite(row[col]) for col in ("open", "high", "low", "close", "volume", "amount", "pe_ttm", "pb")},
                "source": row["source"], "history_type": row["history_type"], "availability": row["availability"]})
        bulk(session, Bar, bars)
        for key, (values, status) in factors.items():
            bulk(session, FactorValue, [{"snapshot_id": snapshot_id, "factor_id": key, "trade_date": str(dt),
                "value": finite(v), "status": str(s)} for dt, v, s in zip(frame.date, values, status)])
        for h in all_horizons:
            returns = forward_returns(frame.close, h)
            dates = frame.date.shift(-h)
            bulk(session, ForwardReturn, [{"snapshot_id": snapshot_id, "trade_date": str(dt), "horizon": h,
                "value": finite(v), "end_date": str(end) if isinstance(end, str) else None,
                "status": "ok" if np.isfinite(v) else "immature"} for dt, v, end in zip(frame.date, returns, dates)])
            bulk(session, Evaluation, [{"snapshot_id": snapshot_id, "factor_id": key, "horizon": h, "result": value}
                for key, value in evaluations[h].items()])
            if h in HORIZONS:
                session.add(Assessment(snapshot_id=snapshot_id, horizon=h,
                    result=build_assessment(frame, factors, evaluations[h], metadata, h)))
        bulk(session, RollingEvaluation, rolling_rows)
        session.get(MarketIndex, spec.code).active_snapshot = snapshot_id
    print(f"[{spec.short_name}] 已发布 {snapshot_id}", flush=True)
    check_cancelled()
    return {"index": spec.code, "snapshot": snapshot_id, "status": "published", "rows": len(frame),
        "first_date": frame.date.iloc[0], "last_date": frame.date.iloc[-1], "warnings": coverage["provider_errors"]}


def run_pipeline(specs=INDICES, offline=False, force=False, run_id=None):
    seed()
    run_id = run_id or uuid.uuid4().hex
    with SessionLocal.begin() as session:
        job = session.get(JobRun, run_id)
        if job:
            job.status = "running"
        else:
            session.add(JobRun(id=run_id, started_at=job_started_at(), status="running", details={"indices": [s.code for s in specs]}))
    results, errors = [], []
    from .equity_bond import load_bond_history
    print("[国债] 获取中债10年期国债收益率历史…", flush=True)
    report_progress("collect_bonds", "采集中债10年期国债收益率历史")
    bond_context = load_bond_history(offline=offline, force=force)
    check_cancelled()
    print(f"[国债] {len(bond_context['observations'])}条观测，{len(bond_context['errors'])}条源提示", flush=True)
    for spec in specs:
        with SessionLocal.begin() as session:
            job = session.get(JobRun, run_id)
            job.details = {**job.details, "indices": [s.code for s in specs],
                "active_index": spec.code, "results": list(results), "errors": list(errors)}
        try:
            results.append(process_index(spec, offline=offline, force=force, bond_context=bond_context))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            errors.append({"index": spec.code, "error": str(exc)})
        with SessionLocal.begin() as session:
            job = session.get(JobRun, run_id)
            job.details = {**job.details, "indices": [s.code for s in specs],
                "results": list(results), "errors": list(errors)}
    with SessionLocal.begin() as session:
        job = session.get(JobRun, run_id)
        job.finished_at = now_iso()
        job.status = "partial" if errors and results else "failed" if errors else "completed"
        job.details = {**job.details, "results": results, "errors": errors}
    return {"run_id": run_id, "results": results, "errors": errors}
