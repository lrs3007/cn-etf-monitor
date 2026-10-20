import gzip
import hashlib
import json

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from backend.app.config import INDICES, HORIZONS, SPREAD_FACTOR, factor_horizons
from backend.app.db import Base
from backend.app.factors import REGISTRY
from backend.app.models import Assessment, Bar, Evaluation, FactorValue, ForwardReturn, MarketIndex, Snapshot
from backend.app.publication import export_results
from backend.app import cli, publication
from types import SimpleNamespace


def test_full_history_export_checks_coverage_and_preserves_last_manifest(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fixture.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    spec = INDICES[0]
    definitions = {fid: REGISTRY[fid].metadata() for fid in ("momentum_20", SPREAD_FACTOR)}
    dates = ["2026-10-16", "2026-10-19"]
    with sessions.begin() as s:
        s.add(MarketIndex(code=spec.code, name=spec.name, base_date=spec.base_date, launch_date=spec.launch_date, active_snapshot="s1"))
        s.add(Snapshot(id="s1", index_code=spec.code, created_at="2026-10-19", engine_version="test", dataset_hash="test", status="published",
            coverage={"rows": 2, "first_date": dates[0], "last_date": dates[-1], "factor_definitions": definitions,
                "factor_coverage": {fid: {"valid": 1} for fid in definitions},
                "source_records": [{"path": "/private/secret", "token": "SECRET"}], "provider_errors": ["TOKEN=SECRET /private/path"]}))
        for i, day in enumerate(dates):
            s.add(Bar(snapshot_id="s1", trade_date=day, close=100 + i, source="fixture", history_type="live", availability=day))
            for fid in definitions:
                s.add(FactorValue(snapshot_id="s1", factor_id=fid, trade_date=day, value=3 if i else None, status="valid" if i else "missing_source"))
            for h in factor_horizons(SPREAD_FACTOR):
                s.add(ForwardReturn(snapshot_id="s1", trade_date=day, horizon=h, value=.1 if not i else None, status="complete" if not i else "immature"))
        for h in HORIZONS:
            s.add(Assessment(snapshot_id="s1", horizon=h, result={"label": "test"}))
        for fid in definitions:
            for h in factor_horizons(fid):
                s.add(Evaluation(snapshot_id="s1", factor_id=fid, horizon=h, result={"horizon": h, "full_ic": .1}))
    directory = tmp_path / "public"
    first = export_results(directory, sessions, (spec,))
    def read(name):
        item = first["objects"][name]
        raw = (directory / item["key"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        return json.loads(gzip.decompress(raw))
    index = read(f"index/{spec.code}")
    assert [p["date"] for p in index["prices"]] == dates
    assert index["returns"]["252"] == [.1, None]
    assert index["evaluations"][SPREAD_FACTOR]["252"]["full_ic"] == .1
    assert read("factor/s1/momentum_20")["values"] == [[None, "missing_source"], [3, "valid"]]
    analysis = read("factor/s1/momentum_20")["analysis"]
    assert analysis["as_of"] == dates[-1]
    assert analysis["current"] == 3
    assert analysis["percentile"] is None
    assert [o["horizon"] for o in analysis["outcomes"]] == list(HORIZONS)
    assert "没有符合" in analysis["outcomes"][0]["interpretation"]
    assert [o["horizon"] for o in read(f"factor/s1/{SPREAD_FACTOR}")["analysis"]["outcomes"]] == list(factor_horizons(SPREAD_FACTOR))
    assert "SECRET" not in json.dumps(first) + json.dumps(index)
    assert "/private" not in json.dumps(index)
    assert export_results(directory, sessions, (spec,))["export_id"] == first["export_id"]
    previous = (directory / "manifest.json").read_bytes()
    with sessions.begin() as s:
        s.execute(delete(FactorValue).where(FactorValue.factor_id == "momentum_20", FactorValue.trade_date == dates[0]))
    with pytest.raises(ValueError, match="未覆盖全部交易日"):
        export_results(directory, sessions, (spec,))
    assert (directory / "manifest.json").read_bytes() == previous
    engine.dispose()


@pytest.mark.parametrize("command,errors,expected_publish", [
    ("update", [], True), ("update", [{"index": "test", "error": "failed"}], False),
    ("publish", [], True),
])
def test_scheduled_publish_only_after_success(tmp_path, monkeypatch, command, errors, expected_publish):
    monkeypatch.setenv("CN_MONITOR_PUBLISH", "1")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "seed", lambda: None)
    monkeypatch.setattr(cli, "run_pipeline", lambda *a, **kw: {"errors": errors})
    monkeypatch.setattr(publication, "export_results", lambda: {"export_id": "test", "objects": {}})
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: (calls.append(a) or SimpleNamespace(returncode=0)))
    monkeypatch.setattr("sys.argv", ["cli", command])
    if errors:
        with pytest.raises(SystemExit): cli.main()
    else:
        cli.main()
    assert bool(calls) == expected_publish


@pytest.mark.parametrize("changed,expected", [(True, "published"), (False, "unchanged")])
def test_web_publish_validates_receipt_for_current_export(monkeypatch, changed, expected):
    monkeypatch.setattr(publication, "export_results", lambda: {"export_id": "release"})
    monkeypatch.setattr(publication.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0,
        stdout=json.dumps({"export_id": "release", "changed": changed, "published_at": "2026-10-20T00:00:00Z"})))
    assert publication.publish_current_results()["status"] == expected


@pytest.mark.parametrize("returncode,stdout", [(1, ""), (0, "not JSON"), (0, '{}'),
    (0, '{"export_id":"other-release","changed":true,"published_at":"2026-10-20"}')])
def test_web_publish_does_not_claim_success_on_failed_or_mismatched_receipt(monkeypatch, returncode, stdout):
    monkeypatch.setattr(publication, "export_results", lambda: {"export_id": "release"})
    monkeypatch.setattr(publication.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=returncode, stdout=stdout))
    with pytest.raises(RuntimeError):
        publication.publish_current_results()


def test_web_publish_timeout_never_claims_sync_success(monkeypatch):
    monkeypatch.setattr(publication, "export_results", lambda: {"export_id": "release"})
    def timeout(*a, **kw):
        raise publication.subprocess.TimeoutExpired(a[0], kw["timeout"])
    monkeypatch.setattr(publication.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="同步超时"):
        publication.publish_current_results()
