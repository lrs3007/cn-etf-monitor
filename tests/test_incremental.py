import numpy as np
import pandas as pd
import json
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.config import ENGINE_VERSION, INDICES
from backend.app.db import Base
from backend.app.incremental import reusable_rolling
from backend.app.models import Bar, FactorValue, MarketIndex, RollingEvaluation, Snapshot
from backend.app.provider_cache import cached_history, history_audit
from backend.app.providers import ProviderError
from backend.app.incremental import EvaluationMemo
from backend.app.evaluation import evaluate_factor


def test_closed_year_cache_matches_full_research_and_invalidates_revision(tmp_path):
    rng = np.random.default_rng(16)
    close = 100 * np.exp(np.cumsum(rng.normal(0, .01, 1800)))
    frame = pd.DataFrame({"date": pd.bdate_range("2018-01-01", periods=1800).strftime("%Y-%m-%d"), "close": close})
    values = frame.close.pct_change(20)
    memo = EvaluationMemo(tmp_path / "memo.json")
    expected, rolling = evaluate_factor(frame, values, 20, "2018-01-01", "test")
    actual, _ = evaluate_factor(frame, values, 20, "2018-01-01", "test", memo=memo)
    assert actual == expected and memo.misses > 0
    memo.save()
    reused = EvaluationMemo(tmp_path / "memo.json")
    actual, _ = evaluate_factor(frame, values, 20, "2018-01-01", "test", memo=reused)
    assert actual == expected and reused.hits > 0 and reused.misses == 0
    frame.loc[40, "close"] *= 1.01
    actual, _ = evaluate_factor(frame, values, 20, "2018-01-01", "test", memo=reused)
    revised, _ = evaluate_factor(frame, values, 20, "2018-01-01", "test")
    assert actual == revised and reused.misses > 0


def test_source_cache_merges_overlap_preserves_precision_and_audits_revisions(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.config.DATA_DIR", tmp_path)
    calls, version = [], [0]
    @cached_history("csindex")
    def provider(spec, offline=False, start_date=None):
        if offline:
            raise ProviderError("no raw cache")
        calls.append(start_date)
        rows = [{"date": "2005-01-04", "close": 1234.1234567890123, "pe_observed": 12.345678901234567},
                {"date": "2026-10-20", "close": 2345., "pe_observed": 13.}]
        if version[0]:
            rows[0]["close"] = 1200.
            rows += [{"date": "2026-10-21", "close": 2346., "pe_observed": 14.}]
        if start_date:
            rows = [r for r in rows if r["date"] >= start_date]
        return pd.DataFrame(rows), {"source": "csindex", "sha256": str(version[0]), "fetched_at": "original"}
    first, _ = provider(INDICES[0])
    version[0] = 1
    second, record = provider(INDICES[0])
    assert calls[0] is None and calls[1] is not None
    assert second.date.tolist() == ["2005-01-04", "2026-10-20", "2026-10-21"]
    assert second.close.iloc[0] == first.close.iloc[0]  # no decimal rounding or truncation
    assert second.pe_record.iloc[0]["sha256"] == "0"
    assert second.pe_record.iloc[1]["sha256"] == "0"  # same PE keeps original lineage
    assert record["history_mode"] == "recent_overlap"
    with history_audit(True):
        third, record = provider(INDICES[0])
    assert calls[-1] is None and third.close.iloc[0] == 1200.
    assert record["history_mode"] == "full_audit"
    replay, record = provider(INDICES[0], offline=True)
    assert replay.close.tolist() == third.close.tolist() and record["replayed_cache"]
    path = tmp_path / "provider-cache" / "000300.SH-csindex.json"
    cached = json.loads(path.read_text()); cached["audited_at"] = "1970-01-01T00:00:00+00:00"
    path.write_text(json.dumps(cached))
    provider(INDICES[0])
    assert calls[-1] is None  # Scheduled audit expiry, even without --force.


@pytest.mark.parametrize("change", ["none", "price", "factor", "date", "engine", "definition", "truncated", "new_factor"])
def test_rolling_cache_requires_entire_identical_prefix_and_complete_maturity_dates(tmp_path, change):
    engine = create_engine(f"sqlite:///{tmp_path / 'cache.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    dates = pd.bdate_range("2020-01-01", periods=300).strftime("%Y-%m-%d").tolist()
    frame = pd.DataFrame({"date": dates + ["2021-03-01"], "close": np.arange(301.) + 100})
    values = pd.Series(np.arange(301.), dtype=float)
    definitions = {"test": {"version": 1, "inputs": ("close",)}}  # SQL JSON stores tuples as lists.
    with sessions.begin() as s:
        s.add(MarketIndex(code="test", name="test", base_date=dates[0], launch_date=dates[0]))
        s.add(Snapshot(id="old", index_code="test", created_at=dates[-1], engine_version=ENGINE_VERSION,
            dataset_hash="hash", status="published", coverage={"factor_definitions": definitions}))
        s.flush()
        for i, day in enumerate(dates):
            s.add(Bar(snapshot_id="old", trade_date=day, close=100. + i, source="test", availability=day, history_type="live"))
            s.add(FactorValue(snapshot_id="old", factor_id="test", trade_date=day, value=float(i), status="valid"))
        for day in dates[271:]:
            if change != "truncated" or day != dates[-1]:
                s.add(RollingEvaluation(snapshot_id="old", factor_id="test", horizon=20, as_of=day, ic=.1, sample_count=252))
    if change == "price": frame.loc[0, "close"] += 1
    if change == "factor": values.iloc[0] += 1
    if change == "date": frame.loc[0, "date"] = "1999-01-01"
    if change == "definition": definitions = {"test": {"version": 2}}
    if change == "new_factor": definitions = {**definitions, "new_factor": {"version": 1}}
    with sessions() as s:
        previous = s.get(Snapshot, "old")
        if change == "engine": previous.engine_version = "old"
        result, _ = reusable_rolling(s, previous, frame, {"test": (values, None)}, definitions, dates[0])
    assert bool(result) == (change in ("none", "new_factor"))
    engine.dispose()


def test_shared_rolling_segments_reconstruct_full_history_and_pin_old_snapshot(tmp_path):
    from backend.app.research_store import rolling_history
    engine = create_engine(f"sqlite:///{tmp_path / 'segments.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as s:
        s.add(MarketIndex(code="test", name="test", base_date="2020-01-01", launch_date="2020-01-01"))
        for sid, coverage in [("old", {"last_date": "2026-10-21"}), ("new", {"last_date": "2026-10-22",
            "rolling_sources": {"test": {"20": ["old", "new"], "60": ["new"]}}})]:
            s.add(Snapshot(id=sid, index_code="test", created_at="2026-10-22", engine_version=ENGINE_VERSION,
                dataset_hash=sid, status="published", coverage=coverage))
        s.flush()
        for h in [20, 60]:
            s.add(RollingEvaluation(snapshot_id="old", factor_id="test", horizon=h, as_of="2026-10-21", ic=.1, sample_count=252))
        # h=20 is append-only; h=60 was revised and is fully recomputed in new.
        for h, day, ic in [(20, "2026-10-22", .2), (60, "2026-10-21", -.1), (60, "2026-10-22", -.2)]:
            s.add(RollingEvaluation(snapshot_id="new", factor_id="test", horizon=h, as_of=day, ic=ic, sample_count=252))
    with sessions() as s:
        new = s.get(Snapshot, "new")
        assert [r.ic for r in rolling_history(s, new, "test", 20)] == [.1, .2]
        assert [r.ic for r in rolling_history(s, new, "test", 60)] == [-.1, -.2]
        assert [r.ic for r in rolling_history(s, s.get(Snapshot, "old"), "test", 20)] == [.1]
        assert len(list(rolling_history(s, new))) == 4
    engine.dispose()


def test_pipeline_appends_rolling_storage_and_exports_complete_history(tmp_path, monkeypatch):
    from sqlalchemy import func
    from backend.app import pipeline
    from backend.app.evaluation import rolling_rank_ic, forward_returns
    from backend.app.factors import calculate_all
    from backend.app.research_store import rolling_history
    engine = create_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    rng = np.random.default_rng(57)
    dates = pd.bdate_range("2018-01-01", periods=1201).strftime("%Y-%m-%d")
    close = 1000 * np.exp(np.cumsum(rng.normal(0, .01, len(dates))))
    frame = pd.DataFrame({"date": dates, "close": close, "open": close, "high": close * 1.01,
        "low": close * .99, "volume": rng.uniform(1e6, 1e8, len(dates)), "amount": 1e8,
        "pe_ttm": np.nan, "pb": np.nan, "spread_pe_ttm": np.nan, "cgb_10y_yield": np.nan,
        "source": "fixture", "history_type": "live", "availability": dates})
    size = [1200]
    def source(*a, **kw):
        data = frame.iloc[:size[0]].copy()
        return data, {"rows": len(data), "first_date": data.date.iloc[0], "last_date": data.date.iloc[-1],
            "volume_rows": len(data), "amount_rows": len(data), "valuation_rows": 0, "provider_errors": []}
    monkeypatch.setattr(pipeline, "full_history", source)
    pipeline.seed()
    old = pipeline.process_index(INDICES[0])
    size[0] += 1
    new = pipeline.process_index(INDICES[0])
    frame["pe_record"] = [{"sha256": "new_response_same_observations"}] * len(frame)
    assert pipeline.process_index(INDICES[0])["status"] == "unchanged"
    with sessions() as s:
        snap = s.get(Snapshot, new["snapshot"])
        assert snap.coverage["computation"]["rolling_cache_groups"] >= 80
        physical = s.scalar(select(func.count()).select_from(RollingEvaluation).where(RollingEvaluation.snapshot_id == snap.id))
        assert 0 < physical < 150
        rows = rolling_history(s, snap, "momentum_20", 20)
        values = calculate_all(frame)["momentum_20"][0]
        expected = rolling_rank_ic(values.to_numpy(), forward_returns(frame.close, 20).to_numpy(), dates.to_numpy(), 20)
        assert [{"as_of": r.as_of, "ic": r.ic, "sample_count": r.sample_count} for r in rows] == expected
        assert len(list(rolling_history(s, s.get(Snapshot, old["snapshot"]), "momentum_20", 20))) == len(expected) - 1
    import gzip
    from backend.app.publication import export_results
    directory = tmp_path / "published"
    manifest = export_results(directory, sessions, (INDICES[0],))
    read = lambda name: json.loads(gzip.decompress((directory / manifest["objects"][name]["key"]).read_bytes()))
    summary = read(f"index/{INDICES[0].code}")
    published = [r for key in summary["research_chunks"] for r in read(key)["factors"]["momentum_20"]["rolling"]["20"]]
    assert published == [{"date": r["as_of"], "ic": r["ic"], "samples": r["sample_count"]} for r in expected]
    engine.dispose()
