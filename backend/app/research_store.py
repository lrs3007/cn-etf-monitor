"""Immutable rolling-series segments shared by complete logical snapshots."""
from sqlalchemy import select
from heapq import merge

from .models import RollingEvaluation, Snapshot


def rolling_sources(snapshot, factor_id, horizon):
    return snapshot.coverage.get("rolling_sources", {}).get(factor_id, {}).get(str(horizon), [snapshot.id])


def rolling_history(session, snapshot, factor_id=None, horizon=None):
    references = snapshot.coverage.get("rolling_sources", {})
    ids = {snapshot.id}
    for fid, horizons in references.items():
        if factor_id is None or fid == factor_id:
            for h, sources in horizons.items():
                if horizon is None or int(h) == horizon:
                    ids.update(sources)
    streams = []
    ids = sorted(ids)
    for start in range(0, len(ids), 400):
        query = select(RollingEvaluation.snapshot_id, RollingEvaluation.factor_id, RollingEvaluation.horizon,
            RollingEvaluation.as_of, RollingEvaluation.ic, RollingEvaluation.sample_count).join(
                Snapshot, RollingEvaluation.snapshot_id == Snapshot.id).where(
                    RollingEvaluation.snapshot_id.in_(ids[start:start + 400]), Snapshot.index_code == snapshot.index_code)
        if factor_id is not None:
            query = query.where(RollingEvaluation.factor_id == factor_id)
        if horizon is not None:
            query = query.where(RollingEvaluation.horizon == horizon)
        if last_date := snapshot.coverage.get("last_date"):
            query = query.where(RollingEvaluation.as_of <= last_date)
        query = query.order_by(RollingEvaluation.as_of, RollingEvaluation.factor_id, RollingEvaluation.horizon)
        streams.append(session.execute(query).yield_per(2048))
    last_key, allowed = None, {}
    for row in merge(*streams, key=lambda r: (r.as_of, r.factor_id, r.horizon)):
        group = row.factor_id, row.horizon
        if group not in allowed:
            allowed[group] = set(rolling_sources(snapshot, *group))
        if row.snapshot_id not in allowed[group]:
            continue
        key = row.as_of, row.factor_id, row.horizon
        if key == last_key:
            raise ValueError("滚动IC分段存在重叠，拒绝读取不一致快照")
        last_key = key
        yield row
