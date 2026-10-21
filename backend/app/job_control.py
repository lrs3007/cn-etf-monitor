"""Local job progress and cooperative cancellation, scoped to one persisted job."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import os
from pathlib import Path
import signal
import subprocess
import time

from .config import DATA_DIR, SHANGHAI, now_iso
from .db import SessionLocal
from .models import JobRun


class UpdateCancelled(BaseException):
    """Must escape broad provider/error handlers and roll back open transactions."""


_control = ContextVar("monitor_job_control", default=None)


def job_started_at():
    # A stop followed by an immediate refresh can create two jobs in one second.
    return datetime.now(SHANGHAI).isoformat(timespec="microseconds")


def process_identity(pid):
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                capture_output=True, text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class JobControl:
    def __init__(self, run_id, sessions, directory):
        self.run_id, self.sessions = run_id, sessions
        self.cancel_file = Path(directory) / "run" / f"cancel-{run_id}"
        self.last_write = 0
        self.stage = None

    def check(self):
        if self.cancel_file.exists():
            raise UpdateCancelled()

    def report(self, stage, label, completed=None, total=None, unit=None, **extra):
        self.check()
        now = time.monotonic()
        if stage == self.stage and now - self.last_write < .3 and completed != total:
            return
        progress = {"stage": stage, "label": label, "completed": completed, "total": total,
                    "unit": unit, "updated_at": now_iso(), **extra}
        with self.sessions.begin() as session:
            job = session.get(JobRun, self.run_id)
            if job:
                job.details = {**job.details, "progress": progress}
        self.last_write, self.stage = now, stage


def check_cancelled():
    if control := _control.get():
        control.check()


def report_progress(stage, label, completed=None, total=None, unit=None, **extra):
    if control := _control.get():
        control.report(stage, label, completed, total, unit, **extra)


def cancellation_file():
    return str(control.cancel_file) if (control := _control.get()) else None


def mark_cancelled(run_id, sessions=SessionLocal):
    with sessions.begin() as session:
        job = session.get(JobRun, run_id)
        if job and job.status not in ("completed", "publish_failed", "failed", "partial"):
            job.status, job.finished_at = "cancelled", now_iso()
            details = {**job.details, "progress": {**job.details.get("progress", {}),
                "stopped_stage": job.details.get("progress", {}).get("stage"),
                "stage": "cancelled", "label": "已停止同步，已完成的本机数据保留", "updated_at": now_iso()}}
            if details.get("publication", {}).get("status") == "publishing":
                details["publication"] = {**details["publication"], "status": "cancelled"}
            job.details = details


@contextmanager
def control_job(run_id, *, sessions=SessionLocal, directory=DATA_DIR, install_signals=False):
    control = JobControl(run_id, sessions, directory)
    identity = process_identity(os.getpid())
    with sessions.begin() as session:
        job = session.get(JobRun, run_id)
        if not job:
            job = JobRun(id=run_id, started_at=job_started_at(), status="queued", details={})
            session.add(job)
        job.details = {**job.details, "worker_pid": os.getpid(), "worker_identity": identity}
    token = _control.set(control)
    previous = {}
    if install_signals:
        def stop(_signum, _frame):
            raise UpdateCancelled()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
    try:
        control.check()
        yield control
    except UpdateCancelled:
        mark_cancelled(run_id, sessions)
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _control.reset(token)
        control.cancel_file.unlink(missing_ok=True)
