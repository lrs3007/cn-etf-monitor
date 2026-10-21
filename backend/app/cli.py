import argparse
import fcntl
import json
import os
import sys
import uuid

from .config import DATA_DIR, INDEX_MAP, INDICES, ROOT
from .pipeline import refresh_assessments, refresh_diagnostics, run_pipeline, seed
from .db import SessionLocal
from .models import JobRun
from .config import now_iso
from .job_control import UpdateCancelled, control_job


def main():
    parser = argparse.ArgumentParser(description="宽基指数：全历史采集、因子计算、IC研究")
    parser.add_argument("command", choices=["init", "backfill", "update", "recompute", "coverage", "assess", "diagnostics", "export", "publish"])
    parser.add_argument("--index", choices=list(INDEX_MAP), action="append")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-update", action="store_true", help="publish 使用当前快照，不重新采集计算")
    args = parser.parse_args()
    if args.skip_update and args.command != "publish":
        parser.error("--skip-update 仅用于 publish")
    if args.command in ("export", "publish") and args.index:
        parser.error("公开发布必须包含全部五个指数，不支持 --index")
    seed()
    if args.command == "init":
        print("数据库与指标定义初始化完成")
        return
    if args.command == "coverage":
        from .api import coverage_report
        print(json.dumps(coverage_report(), ensure_ascii=False, indent=2))
        return
    specs = [INDEX_MAP[code] for code in args.index] if args.index else INDICES
    with (DATA_DIR / "pipeline.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("另一个回填/更新任务正在运行")
        if args.command == "assess":
            refresh_assessments(specs)
            return
        if args.command == "diagnostics":
            refresh_diagnostics(specs)
            return
        run_id = uuid.uuid4().hex
        try:
            with control_job(run_id, sessions=SessionLocal, directory=DATA_DIR, install_signals=True):
                with SessionLocal.begin() as session:
                    job = session.get(JobRun, run_id)
                    job.details = {**job.details, "trigger": "cli", "indices": [s.code for s in specs]}
                result = run_command(args, specs, run_id)
        except UpdateCancelled:
            sys.exit(130)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        sys.exit(1)


def run_command(args, specs, run_id):
    from .publication import export_results, publish_current_results
    publish = args.command == "publish" or (args.command == "update" and os.getenv("CN_MONITOR_PUBLISH") == "1")
    if args.command == "export" or (args.command == "publish" and args.skip_update):
        result = {"errors": []}
    else:
        result = run_pipeline(specs, offline=args.command == "recompute", force=args.force, run_id=run_id)
    if publish and not result["errors"]:
        with SessionLocal.begin() as session:
            job = session.get(JobRun, run_id)
            job.status, job.finished_at = "publishing", None
            job.details = {**job.details, "publication": {"status": "publishing"}}
        try:
            publication = publish_current_results()
        except Exception:
            with SessionLocal.begin() as session:
                job = session.get(JobRun, run_id)
                job.status, job.finished_at = "publish_failed", now_iso()
                job.details = {**job.details, "publication": {"status": "failed"}}
            sys.exit("本机数据已保留，但网页同步失败，请检查本机日志后重试")
        with SessionLocal.begin() as session:
            job = session.get(JobRun, run_id)
            job.status, job.finished_at = "completed", now_iso()
            job.details = {**job.details, "publication": publication, "progress": {**job.details.get("progress", {}),
                "stage": "completed", "label": "网页同步完成", "updated_at": now_iso()}}
    elif args.command == "export":
        manifest = export_results()
        print(json.dumps({"export_id": manifest["export_id"], "objects": len(manifest["objects"])}), flush=True)
        with SessionLocal.begin() as session:
            job = session.get(JobRun, run_id)
            job.status, job.finished_at = "completed", now_iso()
            job.details = {**job.details, "progress": {"stage": "completed", "label": "本机导出完成", "updated_at": now_iso()}}
    elif publish:
        print("更新存在错误，本次未发布；线上保留此前完整结果", file=sys.stderr)
    return result


if __name__ == "__main__":
    main()
