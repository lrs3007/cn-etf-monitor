"""Exact reuse of mature rolling statistics, with conservative prefix validation."""
import numpy as np
import hashlib
import json
from copy import deepcopy
from sqlalchemy import select

from .config import ENGINE_VERSION
from .models import Bar, FactorValue
from .research_store import rolling_history, rolling_sources


class EvaluationMemo:
    """Content-addressed closed-year calculations. Current/revised samples miss cache."""
    def __init__(self, path, force=False):
        self.path, self.items, self.hits, self.misses = path, {}, 0, 0
        if path.exists() and not force:
            try:
                value = json.loads(path.read_text())
                if value.get("version") == ENGINE_VERSION + "/evaluation-memo-v1":
                    self.items = value["items"]
            except (ValueError, OSError, KeyError):
                pass

    def compute(self, label, arrays, calculate):
        digest = hashlib.sha256(label.encode())
        for array in arrays:
            a = np.ascontiguousarray(array).copy()
            if a.dtype.kind == "f":
                a[np.isnan(a)] = np.nan
            digest.update(str((a.dtype.str, a.shape)).encode())
            digest.update(a.tobytes())
        key = digest.hexdigest()
        if key in self.items:
            self.hits += 1
            return deepcopy(self.items[key])
        self.misses += 1
        value = calculate()
        self.items[key] = deepcopy(value)
        return value

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": ENGINE_VERSION + "/evaluation-memo-v1",
            "items": dict(list(self.items.items())[-16000:])}, allow_nan=False, separators=(",", ":")))
        temporary.replace(self.path)


def reusable_rolling(session, previous, frame, factors, metadata, launch_date):
    if not previous or previous.engine_version != ENGINE_VERSION:
        return {}, "engine_or_definition_changed"
    bars = session.execute(select(Bar.trade_date, Bar.close).where(Bar.snapshot_id == previous.id).order_by(Bar.trade_date)).all()
    n = len(bars)
    if not n or n > len(frame) or [r.trade_date for r in bars] != frame.date.iloc[:n].tolist():
        return {}, "history_dates_changed"
    if not np.array_equal(np.asarray([r.close for r in bars], float), frame.close.iloc[:n].to_numpy(float), equal_nan=True):
        return {}, "history_prices_revised"
    reusable = set()
    for fid, (values, _) in factors.items():
        if json.dumps(previous.coverage.get("factor_definitions", {}).get(fid), sort_keys=True) != json.dumps(metadata.get(fid), sort_keys=True):
            continue
        old = session.execute(select(FactorValue.trade_date, FactorValue.value).where(
            FactorValue.snapshot_id == previous.id, FactorValue.factor_id == fid).order_by(FactorValue.trade_date)).all()
        if len(old) == n and [r.trade_date for r in old] == frame.date.iloc[:n].tolist() and np.array_equal(
                np.asarray([r.value for r in old], float), values.iloc[:n].to_numpy(float), equal_nan=True):
            reusable.add(fid)
    result = {}
    for row in rolling_history(session, previous):
        if row.factor_id in reusable:
            result.setdefault((row.factor_id, row.horizon), []).append({"as_of": row.as_of, "ic": row.ic, "sample_count": row.sample_count})
    # Missing/truncated caches are not trusted: require every expected maturity date.
    dates = [r.trade_date for r in bars if r.trade_date >= launch_date]
    for key, rows in list(result.items()):
        if [r["as_of"] for r in rows] != dates[251 + key[1]:] or len(rolling_sources(previous, *key)) >= 60:
            del result[key]
    return result, "validated_prefix"
