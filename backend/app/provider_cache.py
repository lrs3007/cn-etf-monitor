"""Merge recent provider observations into audited full history, never forward fill."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import json

import pandas as pd

from . import config

_force = ContextVar("full_history_audit", default=False)


def cache_rows(frame):
    # pandas.to_json's decimal rounding can invalidate an otherwise exact prefix.
    return frame.astype(object).where(frame.notna(), None).to_dict("records")


@contextmanager
def history_audit(force=False):
    token = _force.set(force)
    try:
        yield
    finally:
        _force.reset(token)


def cached_history(source):
    def decorate(provider):
        @wraps(provider)
        def history(spec, offline=False):
            from .providers import ProviderError
            path = config.DATA_DIR / "provider-cache" / f"{spec.code}-{source}.json"
            cache = None
            if path.exists():
                try:
                    cache = json.loads(path.read_text())
                    if cache.get("version") != 1 or not cache.get("rows"):
                        cache = None
                except (ValueError, OSError):
                    cache = None
            if offline and cache:
                return pd.DataFrame(cache["rows"]), {**cache["record"], "replayed_cache": True}
            now = datetime.now(timezone.utc)
            # Bootstrap from the existing full raw archive once, then audit online.
            if not cache and not offline:
                try:
                    prior, record = provider(spec, offline=True)
                    if source == "csindex":
                        prior["pe_record"] = [record] * len(prior)
                    cache = {"version": 1, "rows": cache_rows(prior),
                             "record": record, "audited_at": "1970-01-01T00:00:00+00:00"}
                except (ProviderError, ValueError):
                    pass
            audit = not cache or _force.get() or (now - datetime.fromisoformat(cache["audited_at"])).total_seconds() >= 7 * 86400
            start = None
            if cache and not audit and not offline:
                last = max(r["date"] for r in cache["rows"])
                start = min(date.today() - timedelta(days=30), date.fromisoformat(last) - timedelta(days=7)).isoformat()
            fresh, record = provider(spec, offline=offline, start_date=start)
            fresh = fresh.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            if fresh.empty:
                raise ProviderError("增量数据为空，不能确认数据源状态")
            if source == "csindex":
                fresh["pe_record"] = [record] * len(fresh)
            if cache and not offline:
                old = pd.DataFrame(cache["rows"])
                if not start and (fresh.date.min() > old.date.min() or fresh.date.max() < old.date.max()):
                    raise ProviderError("全历史核查返回区间缩短，保留历史缓存")
                if source == "csindex" and "pe_record" in old:
                    lookup = old.set_index("date")
                    for i, row in fresh.iterrows():
                        if row.date in lookup.index:
                            prior = lookup.loc[row.date]
                            a, b = row.get("pe_observed"), prior.get("pe_observed")
                            if (pd.isna(a) and pd.isna(b)) or a == b:
                                fresh.at[i, "pe_record"] = prior.pe_record
                if start:
                    fresh = pd.concat([old[old.date < start], fresh[fresh.date >= start]], ignore_index=True)
                    if fresh.date.max() < old.date.max():
                        raise ProviderError("增量数据日期倒退，保留历史缓存")
            if not offline:
                cache = {"version": 1, "rows": cache_rows(fresh),
                    "record": {**record, "history_mode": "full_audit" if audit else "recent_overlap", "request_start": start},
                    "audited_at": now.isoformat() if audit else cache["audited_at"]}
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(cache, ensure_ascii=False, allow_nan=False))
                temp.replace(path)
                record = cache["record"]
            return fresh, record
        return history
    return decorate
