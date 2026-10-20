"""Detached web updates share the CLI/scheduler lock and persisted job history."""
import argparse
import fcntl
import os
import subprocess
import sys
import uuid

from sqlalchemy import select

from .config import DATA_DIR, INDICES, ROOT, now_iso
from .db import SessionLocal
from .models import JobRun
from .publication import publish_current_results


class UpdateBusy(RuntimeError):
    pass


def acquire_update_lock():
    lock = (DATA_DIR / "pipeline.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise UpdateBusy("已有数据更新任务正在运行") from None
    return lock


def serialize_job(job):
    return {"id": job.id, "status": job.status, "started_at": job.started_at,
        "finished_at": job.finished_at, "details": job.details}


def update_status():
    try:
        lock = acquire_update_lock()
    except UpdateBusy:
        busy, lock = True, None
    else:
        busy = False
    try:
        with SessionLocal() as session:
            job = session.scalar(select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1))
            result = serialize_job(job) if job else None
        if result and result["status"] == "publishing" and not busy:
            result = {**result, "status": "publish_interrupted", "details": {**result["details"],
                "publication": {**result["details"].get("publication", {}), "status": "interrupted"}}}
        elif result and result["status"] in ("queued", "running") and not busy:
            # A crashed worker cannot leave the button spinning indefinitely.
            result = {**result, "status": "interrupted"}
        return {"busy": busy, "job": result}
    finally:
        if lock:
            lock.close()


def start_update():
    lock = acquire_update_lock()
    run_id = uuid.uuid4().hex
    try:
        with SessionLocal.begin() as session:
            job = JobRun(id=run_id, started_at=now_iso(), status="queued",
                details={"indices": [spec.code for spec in INDICES], "trigger": "web"})
            session.add(job)
            result = serialize_job(job)
        logs = DATA_DIR / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / "web-update.log").open("a") as output:
            # Child inherits the SAME locked open-file description. Parent closing its
            # copy doesn't unlock the worker; CLI and other web workers remain excluded.
            subprocess.Popen([sys.executable, "-m", "backend.app.updates", "--worker", run_id,
                "--lock-fd", str(lock.fileno())], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                pass_fds=(lock.fileno(),), start_new_session=True)
        return {"busy": True, "job": result}
    except Exception:
        with SessionLocal.begin() as session:
            job = session.get(JobRun, run_id)
            if job:
                job.status, job.finished_at = "failed", now_iso()
                job.details = {"errors": [{"error": "无法启动更新进程，请检查服务日志"}]}
        raise
    finally:
        lock.close()


def run_web_update(run_id):
    from .pipeline import run_pipeline
    result = run_pipeline(run_id=run_id)
    enabled = os.getenv("CN_MONITOR_PUBLISH") == "1"
    with SessionLocal.begin() as session:
        job = session.get(JobRun, run_id)
        publication = {"status": "skipped" if result["errors"] else "publishing" if enabled else "disabled"}
        job.details = {**job.details, "publication": publication}
        if publication["status"] == "publishing":
            job.status, job.finished_at = "publishing", None
    if result["errors"] or not enabled:
        return result
    # Collection and calculation already succeeded. Publication failure must never
    # overwrite their result or misreport the local snapshots as failed.
    try:
        publication = publish_current_results()
        status = "completed"
    except Exception as exc:
        publication = {"status": "failed", "finished_at": now_iso(),
            "error": "网页同步失败，本机数据已保留；请检查网络和本机发布日志后重试"}
        print(f"[网页同步] {type(exc).__name__}；本机计算结果已保留", flush=True)
        status = "publish_failed"
    with SessionLocal.begin() as session:
        job = session.get(JobRun, run_id)
        job.status, job.finished_at = status, now_iso()
        job.details = {**job.details, "publication": publication}
    return {**result, "publication": publication}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args()
    try:
        result = run_web_update(args.worker)
        return 1 if result["errors"] or result.get("publication", {}).get("status") == "failed" else 0
    except Exception:
        with SessionLocal.begin() as session:
            job = session.get(JobRun, args.worker)
            job.status, job.finished_at = "failed", now_iso()
            job.details = {**job.details, "errors": [{"error": "更新进程异常退出，请检查服务日志"}]}
        raise
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    sys.exit(main())
