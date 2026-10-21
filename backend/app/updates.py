"""Detached web updates share the CLI/scheduler lock and persisted job history."""
import argparse
import fcntl
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime

from sqlalchemy import select

from .config import DATA_DIR, INDICES, ROOT, now_iso
from .db import SessionLocal
from .models import JobRun
from .publication import publish_current_results
from .job_control import UpdateCancelled, check_cancelled, control_job, job_started_at, mark_cancelled, process_identity


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
        "finished_at": job.finished_at, "details": {key: value for key, value in job.details.items()
            if key not in ("worker_pid", "worker_identity")}}


def worker_for(job):
    """Validate both PID birth/command and job identity; never signal an arbitrary PID."""
    pid, expected = job.details.get("worker_pid"), job.details.get("worker_identity")
    if pid and expected:
        actual = process_identity(pid)
        if actual == expected and ("backend.app.updates --worker " + job.id in actual or "backend.app.cli " in actual):
            return pid, actual
        return None
    # A worker started by the previous application version has no owner record.
    try:
        rows = subprocess.run(["ps", "-ax", "-o", "pid=", "-o", "command="],
                              capture_output=True, text=True, timeout=3).stdout.splitlines()
        matches = [int(row.split(None, 1)[0]) for row in rows if
                   re.search(r"-m backend\.app\.updates --worker " + re.escape(job.id) + r"(?:\s|$)", row)]
        if len(matches) == 1 and os.getpgid(matches[0]) == matches[0]:
            return matches[0], process_identity(matches[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def legacy_progress(job):
    path = DATA_DIR / "logs" / "web-update.log"
    if job.status != "publishing" or not path.exists() or path.stat().st_mtime < datetime.fromisoformat(job.started_at).timestamp():
        return None
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        tail = stream.read().decode(errors="replace").rsplit("[国债] 获取", 1)[-1]
    matches = re.findall(r"结果对象：(\d+)/(\d+)", tail)
    if matches:
        completed, total = map(int, matches[-1])
        return {"stage": "upload", "label": "核对并同步网页结果对象", "completed": completed, "total": total,
                "unit": "个对象", "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(), "legacy": True}
    return None


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
            can_stop = bool(busy and job and job.status in ("queued", "running", "publishing", "stopping")
                            and job.details.get("progress", {}).get("stage") != "finalize" and worker_for(job))
            if result and not result["details"].get("progress"):
                if progress := legacy_progress(job):
                    result["details"]["progress"] = progress
        if result and result["status"] == "stopping" and not busy:
            result = {**result, "status": "cancelled", "finished_at": result["finished_at"] or result["details"].get("stop_requested_at")}
        if result and result["status"] == "publishing" and not busy:
            result = {**result, "status": "publish_interrupted", "details": {**result["details"],
                "publication": {**result["details"].get("publication", {}), "status": "interrupted"}}}
        elif result and result["status"] in ("queued", "running") and not busy:
            # A crashed worker cannot leave the button spinning indefinitely.
            result = {**result, "status": "interrupted"}
        return {"busy": busy, "can_stop": can_stop, "job": result}
    finally:
        if lock:
            lock.close()


def start_update(trigger="web"):
    lock = acquire_update_lock()
    run_id = uuid.uuid4().hex
    try:
        with SessionLocal.begin() as session:
            job = JobRun(id=run_id, started_at=job_started_at(), status="queued",
                details={"indices": [spec.code for spec in INDICES], "trigger": trigger})
            session.add(job)
            result = serialize_job(job)
        logs = DATA_DIR / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / "web-update.log").open("a") as output:
            # Child inherits the SAME locked open-file description. Parent closing its
            # copy doesn't unlock the worker; CLI and other web workers remain excluded.
            worker = subprocess.Popen([sys.executable, "-m", "backend.app.updates", "--worker", run_id,
                "--lock-fd", str(lock.fileno())], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                pass_fds=(lock.fileno(),), start_new_session=True)
        if worker is not None:
            with SessionLocal.begin() as session:
                job = session.get(JobRun, run_id)
                job.details = {**job.details, "worker_pid": worker.pid, "worker_identity": process_identity(worker.pid)}
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


def stop_update(run_id):
    current = update_status()
    if not current["busy"] or not current["job"] or current["job"]["id"] != run_id:
        raise UpdateBusy("该任务已结束或已被新任务替代，请刷新状态")
    if current["job"]["status"] == "stopping":
        return current
    if not current["can_stop"]:
        raise UpdateBusy("当前任务正在确认最终发布或无法验证进程，暂不能停止")
    with SessionLocal.begin() as session:
        job = session.get(JobRun, run_id)
        owner = worker_for(job)
        if not owner or job.status not in ("queued", "running", "publishing"):
            raise UpdateBusy("任务状态已变化，请刷新状态")
        directory = DATA_DIR / "run"
        directory.mkdir(exist_ok=True)
        (directory / f"cancel-{run_id}").write_text(now_iso())
        job.status = "stopping"
        job.details = {**job.details, "stop_requested_at": now_iso()}
    pid, identity = owner
    if process_identity(pid) == identity:
        try:
            if "backend.app.updates --worker " + run_id in identity and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)  # Also stops an older worker's uploader.
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Legacy workers cannot persist cancellation themselves. Wait briefly for their
    # transaction rollback and inherited lock to close; never unlock on their behalf.
    for _ in range(20):
        try:
            lock = acquire_update_lock()
        except UpdateBusy:
            time.sleep(.05)
        else:
            lock.close()
            mark_cancelled(run_id, SessionLocal)
            (DATA_DIR / "run" / f"cancel-{run_id}").unlink(missing_ok=True)
            break
    return update_status()


def run_web_update(run_id):
    from .pipeline import run_pipeline
    result = run_pipeline(run_id=run_id)
    check_cancelled()
    enabled = os.getenv("CN_MONITOR_PUBLISH") == "1"
    with SessionLocal.begin() as session:
        job = session.get(JobRun, run_id)
        publication = {"status": "skipped" if result["errors"] else "publishing" if enabled else "disabled"}
        job.details = {**job.details, "publication": publication}
        if publication["status"] == "publishing":
            job.status, job.finished_at = "publishing", None
    if result["errors"] or not enabled:
        with SessionLocal.begin() as session:
            job = session.get(JobRun, run_id)
            job.details = {**job.details, "progress": {"stage": "failed" if result["errors"] else "local_complete",
                "label": "部分本机数据更新失败，本次未同步网页" if result["errors"] else "本机更新完成，未启用网页自动同步", "updated_at": now_iso()}}
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
        job.details = {**job.details, "publication": publication, "progress": {**job.details.get("progress", {}),
            "stage": "completed" if status == "completed" else "failed", "label": "网页同步完成" if status == "completed" else "网页同步失败",
            "updated_at": now_iso()}}
    return {**result, "publication": publication}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args()
    try:
        with control_job(args.worker, sessions=SessionLocal, directory=DATA_DIR, install_signals=True):
            result = run_web_update(args.worker)
        return 1 if result["errors"] or result.get("publication", {}).get("status") == "failed" else 0
    except UpdateCancelled:
        return 130
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
