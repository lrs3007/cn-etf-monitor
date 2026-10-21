import os
import json
import signal
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import api, pipeline, updates
from backend.app.db import Base
from backend.app.models import JobRun
from backend.app import publication
from backend.app.job_control import UpdateCancelled, check_cancelled, control_job, report_progress


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
    monkeypatch.setattr(updates, "worker_for", lambda job: None)  # The fake child has no real PID.
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


def test_stop_is_job_scoped_preserves_results_and_releases_lock(isolated_updates, monkeypatch):
    completed_pipeline(isolated_updates)
    with isolated_updates.begin() as session:
        session.get(JobRun, "web").status = "publishing"
    lock = updates.acquire_update_lock()
    signals = []
    monkeypatch.setattr(updates, "worker_for", lambda job: (4242, "verified cli worker"))
    monkeypatch.setattr(updates, "process_identity", lambda pid: "verified cli worker")
    def stop(pid, signum):
        signals.append((pid, signum))
        lock.close()
    monkeypatch.setattr(updates.os, "kill", stop)
    try:
        with pytest.raises(updates.UpdateBusy):
            updates.stop_update("older-job")
        assert signals == []
        status = updates.stop_update("web")
        assert signals == [(4242, signal.SIGTERM)]
        assert not status["busy"]
        assert status["job"]["status"] == "cancelled"
        assert status["job"]["details"]["results"][0]["last_date"] == "2026-10-19"
        assert not (updates.DATA_DIR / "run" / "cancel-web").exists()
    finally:
        lock.close()


def test_stale_pid_and_final_publication_are_never_signalled(isolated_updates, monkeypatch):
    completed_pipeline(isolated_updates)
    with isolated_updates.begin() as session:
        job = session.get(JobRun, "web")
        job.status = "publishing"
        job.details = {**job.details, "worker_pid": 4242, "worker_identity": "old process"}
    monkeypatch.setattr(updates, "process_identity", lambda pid: "different process after PID reuse")
    monkeypatch.setattr(updates.os, "kill", lambda *args: pytest.fail("must not signal stale PID"))
    with updates.acquire_update_lock():
        assert not updates.update_status()["can_stop"]
        with pytest.raises(updates.UpdateBusy):
            updates.stop_update("web")
        with isolated_updates.begin() as session:
            job = session.get(JobRun, "web")
            job.details = {**job.details, "progress": {"stage": "finalize"}}
        monkeypatch.setattr(updates, "worker_for", lambda job: (4242, "valid worker"))
        with pytest.raises(updates.UpdateBusy):
            updates.stop_update("web")


def test_stop_endpoint_rejects_cross_origin(isolated_updates, monkeypatch):
    monkeypatch.setattr(api, "stop_update", lambda *a: pytest.fail("untrusted caller stopped work"))
    with TestClient(api.app, base_url="http://localhost:3000", client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/v1/updates/web/stop", headers={"Origin": "https://evil.example"}).status_code == 403


def test_legacy_upload_progress_is_recovered_without_exposing_logs(isolated_updates):
    completed_pipeline(isolated_updates)
    with isolated_updates.begin() as session:
        session.get(JobRun, "web").status = "publishing"
    logs = updates.DATA_DIR / "logs"
    logs.mkdir()
    (logs / "web-update.log").write_text("[国债] 获取历史…\nprivate provider text\n结果对象：80/105\n")
    with updates.acquire_update_lock():
        status = updates.update_status()
    assert status["job"]["details"]["progress"]["completed"] == 80
    assert status["job"]["details"]["progress"]["total"] == 105
    assert "private provider text" not in str(status)


def test_cancel_marker_persists_progress_and_cancelled_state(isolated_updates):
    with pytest.raises(UpdateCancelled):
        with control_job("controlled", sessions=isolated_updates, directory=updates.DATA_DIR):
            report_progress("calculate", "因子检验", 4, 10, "项检验")
            path = updates.DATA_DIR / "run"
            path.mkdir()
            (path / "cancel-controlled").write_text("stop")
            check_cancelled()
    with isolated_updates() as session:
        job = session.get(JobRun, "controlled")
        assert job.status == "cancelled"
        assert job.details["progress"]["completed"] == 4
        assert job.details["progress"]["stage"] == "cancelled"
        assert job.details["progress"]["stopped_stage"] == "calculate"


@pytest.mark.parametrize("cancel", [False, True])
def test_streaming_uploader_reports_progress_and_terminates_child_on_stop(isolated_updates, monkeypatch, cancel):
    real_popen = publication.subprocess.Popen
    children = []
    script = 'import json,time; print("CN_MONITOR_PROGRESS " + json.dumps({"phase":"upload","completed":1,"total":1,"completed_bytes":42,"total_bytes":42}),flush=True); '
    script += 'time.sleep(60)' if cancel else 'print(json.dumps({"export_id":"release"}),flush=True)'
    def spawn(command, **kwargs):
        if command[0] == "node":
            child = real_popen([sys.executable, "-u", "-c", script], **kwargs)
            children.append(child)
            return child
        return real_popen(command, **kwargs)
    monkeypatch.setattr(publication.subprocess, "Popen", spawn)
    real_report = publication.report_progress
    def progress(stage, label, completed=None, total=None, unit=None, **extra):
        real_report(stage, label, completed, total, unit, **extra)
        if cancel and completed == 1:
            directory = updates.DATA_DIR / "run"
            directory.mkdir(exist_ok=True)
            (directory / "cancel-stream").write_text("stop")
    monkeypatch.setattr(publication, "report_progress", progress)
    def run():
        with control_job("stream", sessions=isolated_updates, directory=updates.DATA_DIR):
            return publication.run_publisher()
    if cancel:
        with pytest.raises(UpdateCancelled):
            run()
        with isolated_updates() as session:
            assert session.get(JobRun, "stream").status == "cancelled"
    else:
        receipt = run()
        assert receipt.returncode == 0
        assert json.loads(receipt.stdout)["export_id"] == "release"
        with isolated_updates() as session:
            assert session.get(JobRun, "stream").details["progress"]["completed_bytes"] == 42
    assert children[0].poll() is not None
