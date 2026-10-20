import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import api, pipeline, updates
from backend.app.db import Base
from backend.app.models import JobRun


@pytest.fixture
def isolated_updates(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(updates, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updates, "SessionLocal", sessions)
    monkeypatch.setattr(api, "seed", lambda: None)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    yield sessions
    engine.dispose()


def test_refresh_starts_detached_job_and_excludes_cli_until_child_exits(isolated_updates, monkeypatch):
    inherited = []

    def launch(command, **kwargs):
        assert command[1:3] == ["-m", "backend.app.updates"]
        inherited.append(os.dup(kwargs["pass_fds"][0]))

    monkeypatch.setattr(updates.subprocess, "Popen", launch)
    try:
        with TestClient(api.app, base_url="http://localhost:3000", client=("127.0.0.1", 50000)) as client:
            response = client.post("/api/v1/updates", headers={"Origin": "http://localhost:3000"})
            assert response.status_code == 202
            run_id = response.json()["job"]["id"]
            assert client.get("/api/v1/updates/latest").json()["busy"]
            assert client.post("/api/v1/updates", headers={"Origin": "http://localhost:3000"}).status_code == 409
            # This is the same flock used by unattended and CLI updates.
            with pytest.raises(updates.UpdateBusy):
                updates.acquire_update_lock()
            with isolated_updates.begin() as session:
                job = session.get(JobRun, run_id)
                job.status, job.finished_at = "completed", "2026-10-19T22:00:00+08:00"
                job.details = {"results": [{"index": "000300.SH", "last_date": "2026-10-19"}], "errors": []}
            os.close(inherited.pop())
            status = client.get("/api/v1/updates/latest").json()
            assert not status["busy"] and status["job"]["status"] == "completed"
            assert status["job"]["details"]["results"][0]["last_date"] == "2026-10-19"
    finally:
        for fd in inherited:
            os.close(fd)


@pytest.mark.parametrize("origin,headers", [
    ("https://evil.example", {}),
    ("http://localhost:9999", {}),
    ("http://localhost:3000", {"X-Forwarded-For": "198.51.100.8"}),
    ("http://localhost:3000", {"Sec-Fetch-Site": "cross-site"}),
    ("", {}),
    ("http://[", {}),
])
def test_untrusted_refresh_requests_never_start_work(isolated_updates, monkeypatch, origin, headers):
    monkeypatch.setattr(api, "start_update", lambda: pytest.fail("untrusted caller started an update"))
    with TestClient(api.app, base_url="http://localhost:3000", client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/v1/updates", headers={"Origin": origin, **headers}).status_code == 403


def test_remote_refresh_requires_correct_admin_token(isolated_updates, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    monkeypatch.setattr(api, "start_update", lambda: {"busy": True, "job": None})
    with TestClient(api.app, base_url="https://monitor.example", client=("198.51.100.8", 50000)) as client:
        assert client.post("/api/v1/updates", headers={"Origin": "https://monitor.example"}).status_code == 403
        assert client.post("/api/v1/updates", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/v1/updates", headers={"Authorization": "Bearer test-token"}).status_code == 202


def test_dead_worker_is_reported_as_interrupted_and_spawn_failures_release_lock(isolated_updates, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("spawn failed")
    monkeypatch.setattr(updates.subprocess, "Popen", fail)
    with pytest.raises(OSError):
        updates.start_update()
    status = updates.update_status()
    assert not status["busy"] and status["job"]["status"] == "failed"
    with isolated_updates.begin() as session:
        session.get(JobRun, status["job"]["id"]).status = "running"
    assert updates.update_status()["job"]["status"] == "interrupted"


def completed_pipeline(sessions, run_id="web", errors=None):
    details = {"results": [{"index": "000300.SH", "status": "unchanged", "last_date": "2026-10-19", "rows": 10}], "errors": errors or []}
    with sessions.begin() as session:
        session.add(JobRun(id=run_id, started_at="2026-10-20", finished_at="2026-10-20",
            status="partial" if errors else "completed", details=details))
    return {"run_id": run_id, **details}


@pytest.mark.parametrize("outcome", ["published", "unchanged", "failed"])
def test_web_calculation_publishes_under_same_lock_and_preserves_local_results(isolated_updates, monkeypatch, outcome):
    monkeypatch.setenv("CN_MONITOR_PUBLISH", "1")
    monkeypatch.setattr(pipeline, "run_pipeline", lambda run_id: completed_pipeline(isolated_updates, run_id))

    def publish():
        status = updates.update_status()
        assert status["busy"] and status["job"]["status"] == "publishing"
        assert status["job"]["finished_at"] is None
        with pytest.raises(updates.UpdateBusy):
            updates.acquire_update_lock()
        if outcome == "failed":
            raise RuntimeError("credentials or provider details must never appear in job JSON")
        return {"status": outcome, "published_at": "2026-10-20T00:00:00+08:00", "export_id": "release"}

    monkeypatch.setattr(updates, "publish_current_results", publish)
    with updates.acquire_update_lock():
        result = updates.run_web_update("web")
    status = updates.update_status()
    assert not status["busy"]
    assert status["job"]["status"] == ("publish_failed" if outcome == "failed" else "completed")
    assert status["job"]["details"]["results"] == result["results"]
    assert status["job"]["details"]["errors"] == []
    assert status["job"]["details"]["publication"]["status"] == outcome
    assert "credentials" not in str(status)


@pytest.mark.parametrize("enabled,errors,publication_status", [
    ("0", [], "disabled"), ("1", [{"index": "000905.SH", "error": "source failed"}], "skipped"),
])
def test_web_publication_respects_switch_and_rejects_partial_updates(isolated_updates, monkeypatch, enabled, errors, publication_status):
    monkeypatch.setenv("CN_MONITOR_PUBLISH", enabled)
    monkeypatch.setattr(pipeline, "run_pipeline", lambda run_id: completed_pipeline(isolated_updates, run_id, errors))
    monkeypatch.setattr(updates, "publish_current_results", lambda: pytest.fail("must not publish incomplete or disabled update"))
    with updates.acquire_update_lock():
        updates.run_web_update("web")
    job = updates.update_status()["job"]
    assert job["status"] == ("partial" if errors else "completed")
    assert job["details"]["publication"]["status"] == publication_status


def test_lost_publication_worker_reports_interruption_without_losing_calculation(isolated_updates):
    result = completed_pipeline(isolated_updates)
    with isolated_updates.begin() as session:
        job = session.get(JobRun, "web")
        job.status = "publishing"
        job.details = {**job.details, "publication": {"status": "publishing"}}
    status = updates.update_status()
    assert not status["busy"] and status["job"]["status"] == "publish_interrupted"
    assert status["job"]["details"]["publication"]["status"] == "interrupted"
    assert status["job"]["details"]["results"] == result["results"]
