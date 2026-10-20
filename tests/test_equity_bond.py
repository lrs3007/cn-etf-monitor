import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app import api, pipeline
from backend.app.config import INDICES, SPREAD_FACTOR
from backend.app.db import Base
from backend.app.equity_bond import SERIES_ID, align_spread_inputs, identity, parse_yields
from backend.app.evaluation import build_assessment
from backend.app.factors import REGISTRY, calculate_all
from backend.app.models import FactorInput, FactorValue, MacroObservation, ValuationObservation
from backend.app.providers import ProviderError


def observation(day, value=2.):
    payload = {"series_id": SERIES_ID, "observation_date": day,
        "available_date": (date.fromisoformat(day) + timedelta(days=1)).isoformat(), "value": value,
        "provenance": {"source": "chinabond-test", "availability_assumed": True}}
    return {"id": identity(payload), **payload}


def valuation(day, pe=12.5, available=None):
    return {"date": day, "available_date": available or (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
        "pe_ttm": pe, "source": "test", "availability_assumed": True}


def context(*rows):
    return {"observations": list(rows), "source_records": [], "errors": []}


def test_spread_pairs_original_dates_and_waits_for_next_equity_session():
    frame = pd.DataFrame({"date": ["2026-10-16", "2026-10-19", "2026-10-20"]})
    result, lineage = align_spread_inputs(frame, [valuation("2026-10-16"), valuation("2026-10-19", pe=20)],
        context(observation("2026-10-16", 2), observation("2026-10-19", 1.5)), "000300.SH")
    values, status = calculate_all(result)[SPREAD_FACTOR]
    assert pd.isna(values.iloc[0]) and status.iloc[0] == "missing_source"
    assert values.iloc[1] == pytest.approx(6.)  # 8% - 2% = 6 percentage points, 600bp.
    assert values.iloc[2] == pytest.approx(3.5)
    assert lineage["inputs"][0]["trade_date"] == "2026-10-19"


def test_missing_bond_never_borrows_another_day_and_nonpositive_pe_is_undefined():
    days = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
    frame = pd.DataFrame({"date": days})
    result, _ = align_spread_inputs(frame, [valuation(day, pe) for day, pe in zip(days, [10, 0, -5, 10])],
        context(observation(days[1]), observation(days[2])), "000300.SH")
    values, status = calculate_all(result)[SPREAD_FACTOR]
    assert values.isna().all()
    assert status.tolist() == ["missing_source", "missing_source", "undefined", "undefined"]


def test_future_or_late_publications_cannot_change_past_inputs():
    days = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
    # Jan 2 PE released Jan 6 cannot be slipped into Jan 3, nor resurrected after a newer session.
    vals = [valuation(days[0], available="2020-01-06"), valuation(days[1])]
    bonds = context(*(observation(day) for day in days))
    full, _ = align_spread_inputs(pd.DataFrame({"date": days}), vals + [valuation(days[2], 20)], bonds, "000300.SH")
    prefix, _ = align_spread_inputs(pd.DataFrame({"date": days[:3]}), vals, bonds, "000300.SH")
    pd.testing.assert_frame_equal(full.iloc[:3], prefix)
    assert pd.isna(full.spread_pe_ttm.iloc[1])
    assert full.spread_pe_ttm.iloc[2] == 12.5


def test_parser_selects_government_ten_year_column_by_header():
    text = '''<table><tr><td>曲线名称</td><td>日期</td><td>30年</td><td>10<!--年-->年</td></tr>
    <tr><td>中债商业银行普通债收益率曲线(AAA)</td><td>2020-01-02</td><td>3.1</td><td>3</td></tr>
    <tr><td>中债国债收益率曲线</td><td>2020-01-02</td><td>3.1</td><td>2.5</td></tr></table>'''
    assert parse_yields(text, "2020-01-01", "2020-12-31") == [
        {"date": "2020-01-02", "available_date": "2020-01-03", "value": 2.5}]
    with pytest.raises(ProviderError, match="超出请求区间"):
        parse_yields(text, "2021-01-01", "2021-12-31")
    with pytest.raises(ProviderError, match="表头变化"):
        parse_yields("<html>Service unavailable</html>", "2020-01-01", "2020-12-31")


def test_spread_is_observation_only_even_with_strong_predictive_statistics():
    days = pd.bdate_range("2018-01-01", periods=1000).strftime("%Y-%m-%d")
    frame = pd.DataFrame({"date": days, "close": np.arange(1000.) + 1000,
        "spread_pe_ttm": np.linspace(10, 20, 1000), "cgb_10y_yield": 2.})
    factors = calculate_all(frame)
    evaluation = {key: {"evaluation_start": days[0], "oos_ic": .5, "oos_samples": 800,
        "direction_consistency": .9, "ci": {"lower": .3, "upper": .6}} for key in REGISTRY}
    metadata = {key: definition.metadata() for key, definition in REGISTRY.items()}
    result = build_assessment(frame, factors, evaluation, metadata, 20)
    spread = next(row for row in result["evidence"] if row["factor_id"] == SPREAD_FACTOR)
    assert not spread["eligible"] and spread["score"] is None
    assert spread["percentile"] is not None
    assert "不参与综合预测评分" in result["relative_value"]["text"]
    short = build_assessment(frame.iloc[:700], calculate_all(frame.iloc[:700]), evaluation, metadata, 20)
    assert short["relative_value"]["percentile"] is None


def test_spread_snapshot_persists_lineage_and_long_horizon_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}", connect_args={"check_same_thread": False})
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "init_db", lambda: Base.metadata.create_all(engine))
    days = pd.bdate_range("2020-01-02", periods=100).strftime("%Y-%m-%d")
    frame = pd.DataFrame({"date": days, "close": np.arange(100.) + 1000,
        "open": 1000., "high": 1100., "low": 990., "volume": 1e6, "amount": 1e8,
        "pe_ttm": 12.5, "pb": np.nan, "source": "fixture", "history_type": "live", "availability": days})
    frame, lineage = align_spread_inputs(frame, [valuation(day) for day in days],
        context(*(observation(day) for day in days)), "000300.SH")
    frame.attrs["spread_inputs"] = lineage
    coverage = {"first_date": days[0], "last_date": days[-1], "rows": 100, "provider_errors": [],
        "volume_rows": 100, "amount_rows": 100, "valuation_rows": 100}
    monkeypatch.setattr(pipeline, "full_history", lambda *a, **kw: (frame.copy(), dict(coverage)))
    try:
        pipeline.seed()
        first = pipeline.process_index(INDICES[0])
        assert pipeline.process_index(INDICES[0])["status"] == "unchanged"
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(FactorInput)) == 99
            assert session.scalar(select(func.count()).select_from(MacroObservation)) == 100
            assert session.scalar(select(func.count()).select_from(ValuationObservation)) == 100
            assert session.scalar(select(func.count()).select_from(FactorValue).where(FactorValue.factor_id == SPREAD_FACTOR)) == 100
        with TestClient(api.app) as client:
            response = client.get(f"/api/v1/indices/000300.SH/factors/{SPREAD_FACTOR}?horizon=252")
            assert response.status_code == 200
            body = response.json()
            assert body["snapshot_id"] == first["snapshot"]
            assert body["series"][1]["value"] == pytest.approx(6.)
            assert body["components"][0]["observation_date"] == days[0]
            assert body["components"][0]["date"] == days[1]
            assert body["evaluation"]["horizon"] == 252
            assert body["analysis"]["as_of"] == days[-1]
            assert body["analysis"]["current"] == pytest.approx(6.)
            assert body["analysis"]["percentile"] is None  # 100 days < 756; no fabricated position.
            assert [o["horizon"] for o in body["analysis"]["outcomes"]] == [1, 5, 10, 20, 60, 120, 252]
            missing = client.get("/api/v1/indices/000300.SH/factors/pb").json()
            assert missing["analysis"]["current"] is None
            assert "当天输入缺失" in missing["analysis"]["current_state"]
            # Local and published sites must render the exact same analysis.
            import gzip
            import json
            from backend.app.publication import export_results
            exported = export_results(tmp_path / "publication", sessions, (INDICES[0],))
            key = exported["objects"][f"factor/{first['snapshot']}/{SPREAD_FACTOR}"]["key"]
            payload = json.loads(gzip.decompress((tmp_path / "publication" / key).read_bytes()))
            assert payload["analysis"] == body["analysis"]
            assert client.get("/api/v1/indices/000300.SH/factors/pe_ttm?horizon=252").status_code == 422
    finally:
        engine.dispose()
