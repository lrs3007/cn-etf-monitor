import secrets
from contextlib import asynccontextmanager
import os
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import select

from .config import HORIZONS, INDEX_MAP, INDICES, SPREAD_FACTOR, factor_horizons, now_iso
from .db import SessionLocal
from .factor_analysis import web_analysis
from .models import Assessment, Bar, Evaluation, FactorValue, ForwardReturn, JobRun, MarketIndex, RollingEvaluation, Snapshot, FactorInput, MacroObservation, ValuationObservation
from .pipeline import seed
from .updates import UpdateBusy, start_update, stop_update, update_status


@asynccontextmanager
async def lifespan(app):
    seed()
    yield


app = FastAPI(title="宽基研究台 API", version="0.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


def snapshot_for(session, code, snapshot_id=None):
    if code not in INDEX_MAP:
        raise HTTPException(404, "未知指数")
    market = session.get(MarketIndex, code)
    target = snapshot_id or (market.active_snapshot if market else None)
    snapshot = session.get(Snapshot, target) if target else None
    if not snapshot or snapshot.index_code != code or snapshot.status != "published":
        raise HTTPException(404, "该指数尚未完成数据回填，请运行 backfill 命令")
    return snapshot


def validate_horizon(horizon):
    if horizon not in HORIZONS:
        raise HTTPException(422, "预测期限应为1、5、10、20、60交易日")


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "time": now_iso()}


@app.get("/api/v1/indices")
def indices():
    with SessionLocal() as session:
        result = []
        for spec in INDICES:
            market = session.get(MarketIndex, spec.code)
            snapshot = session.get(Snapshot, market.active_snapshot) if market and market.active_snapshot else None
            result.append({"code": spec.code, "name": spec.name, "short_name": spec.short_name,
                "description": spec.description, "ready": snapshot is not None,
                "coverage": snapshot.coverage if snapshot else None})
        return result


@app.get("/api/v1/dashboard/{code}")
def dashboard(code: str, horizon: int = 20):
    validate_horizon(horizon)
    with SessionLocal() as session:
        snapshot = snapshot_for(session, code)
        assessment = session.get(Assessment, (snapshot.id, horizon))
        evaluations = session.scalars(select(Evaluation).where(Evaluation.snapshot_id == snapshot.id, Evaluation.horizon == horizon)).all()
        bars = session.scalars(select(Bar).where(Bar.snapshot_id == snapshot.id).order_by(Bar.trade_date)).all()
        return {"code": code, "snapshot_id": snapshot.id, "updated_at": snapshot.created_at,
            "coverage": snapshot.coverage, "assessment": assessment.result,
            "factors": [{**snapshot.coverage["factor_definitions"][item.factor_id],
                "coverage": snapshot.coverage["factor_coverage"][item.factor_id], "evaluation": item.result}
                for item in evaluations],
            "prices": [{"date": b.trade_date, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume, "amount": b.amount, "history_type": b.history_type} for b in bars]}


@app.get("/api/v1/indices/{code}/factors/{factor_id}")
def factor_detail(code: str, factor_id: str, horizon: int = 20, snapshot_id: str | None = None):
    if horizon not in factor_horizons(factor_id):
        raise HTTPException(422, "该因子不支持此预测期限")
    with SessionLocal() as session:
        snapshot = snapshot_for(session, code, snapshot_id)
        if factor_id not in snapshot.coverage["factor_definitions"]:
            raise HTTPException(404, "未知因子")
        evaluation = session.get(Evaluation, (snapshot.id, factor_id, horizon))
        if not evaluation:
            raise HTTPException(404, "当前快照尚未计算该期限，请更新数据")
        values = session.scalars(select(FactorValue).where(FactorValue.snapshot_id == snapshot.id,
            FactorValue.factor_id == factor_id).order_by(FactorValue.trade_date)).all()
        bars = session.execute(select(Bar.trade_date, Bar.close).where(Bar.snapshot_id == snapshot.id).order_by(Bar.trade_date)).all()
        if [row.trade_date for row in bars] != [v.trade_date for v in values]:
            raise HTTPException(503, "当前因子与行情日期未对齐，请重新计算")
        evaluations = {item.horizon: item.result for item in session.scalars(select(Evaluation).where(
            Evaluation.snapshot_id == snapshot.id, Evaluation.factor_id == factor_id))}
        returns = {r.trade_date: r.value for r in session.scalars(select(ForwardReturn).where(
            ForwardReturn.snapshot_id == snapshot.id, ForwardReturn.horizon == horizon)).all()}
        from .research_store import rolling_history
        rolling = rolling_history(session, snapshot, factor_id, horizon)
        components = []
        if factor_id == SPREAD_FACTOR:
            inputs = session.execute(select(FactorInput, ValuationObservation, MacroObservation)
                .outerjoin(ValuationObservation, FactorInput.valuation_id == ValuationObservation.id)
                .outerjoin(MacroObservation, FactorInput.bond_id == MacroObservation.id)
                .where(FactorInput.snapshot_id == snapshot.id, FactorInput.factor_id == factor_id)
                .order_by(FactorInput.trade_date)).all()
            components = [{"date": row.trade_date, "observation_date": pe.observation_date if pe else None,
                "available_date": max(pe.available_date, bond.available_date) if pe and bond else None,
                "pe_ttm": pe.pe_ttm if pe else None,
                "earnings_yield": 100 / pe.pe_ttm if pe and pe.pe_ttm > 0 else None,
                "bond_yield": bond.value if bond else None,
                "valuation_source": pe.provenance if pe else None, "bond_source": bond.provenance if bond else None}
                for row, pe, bond in inputs]
        return {"snapshot_id": snapshot.id, "definition": snapshot.coverage["factor_definitions"][factor_id],
            "analysis": web_analysis([b.trade_date for b in bars], [b.close for b in bars], [v.value for v in values],
                snapshot.coverage["factor_definitions"][factor_id], evaluations, INDEX_MAP[code].launch_date),
            "components": components,
            "coverage": snapshot.coverage["factor_coverage"][factor_id], "evaluation": evaluation.result,
            "series": [{"date": v.trade_date, "value": v.value, "status": v.status,
                "forward_return": returns.get(v.trade_date)} for v in values],
            "rolling": [{"date": r.as_of, "ic": r.ic, "samples": r.sample_count} for r in rolling]}


@app.get("/api/v1/data-status")
def coverage_report():
    with SessionLocal() as session:
        jobs = session.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(10)).all()
    return {"indices": indices(), "jobs": [{"id": j.id, "status": j.status, "started_at": j.started_at,
        "finished_at": j.finished_at, "details": j.details} for j in jobs]}


def require_admin(authorization: str | None = Header(default=None)):
    token = os.getenv("ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(503, "未启用远程管理；请在本机使用CLI")
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "管理认证失败")


def require_update_access(request: Request):
    """Local same-origin browser writes, or explicitly authenticated API clients."""
    authorization = request.headers.get("authorization")
    if authorization:
        require_admin(authorization)
        return

    def loopback(host):
        if host == "localhost":
            return True
        try:
            return ip_address(host).is_loopback
        except ValueError:
            return False

    try:
        origin = urlsplit(request.headers.get("origin", ""))
    except ValueError:
        raise HTTPException(403, "无效的请求来源") from None
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    forwarded = request.headers.get("x-forwarded-for", "")
    if (not request.client or not loopback(request.client.host)
        or origin.scheme not in ("http", "https") or not loopback(origin.hostname or "")
        or origin.netloc != host or request.headers.get("forwarded")
        or any(not loopback(ip.strip()) for ip in forwarded.split(",") if ip.strip())
        or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"):
        raise HTTPException(403, "仅允许本机同源网页更新；远程调用请使用管理认证")


@app.get("/api/v1/updates/latest")
def latest_update():
    from fastapi.responses import JSONResponse
    return JSONResponse(update_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/v1/updates", status_code=202, dependencies=[Depends(require_update_access)])
def create_update(mode: str = "update"):
    if mode not in ("update", "publish_only"):
        raise HTTPException(422, "未知更新模式")
    try:
        return start_update(mode=mode) if mode != "update" else start_update()
    except UpdateBusy as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/v1/admin/config", dependencies=[Depends(require_admin)])
def admin_config():
    # Never return credentials, even to authenticated callers.
    return {"tushare_configured": bool(os.getenv("TUSHARE_TOKEN")), "remote_updates": bool(os.getenv("ADMIN_TOKEN")),
        "update_command": "python -m backend.app.cli update"}


@app.post("/api/v1/updates/{run_id}/stop", status_code=202, dependencies=[Depends(require_update_access)])
def cancel_update(run_id: str):
    try:
        return stop_update(run_id)
    except UpdateBusy as exc:
        raise HTTPException(409, str(exc)) from None
