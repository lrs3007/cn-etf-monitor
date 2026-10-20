import argparse
import fcntl
import json
import os
import subprocess
import sys

from .config import DATA_DIR, INDEX_MAP, INDICES, ROOT
from .pipeline import refresh_assessments, refresh_diagnostics, run_pipeline, seed


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
        publish = args.command == "publish" or (args.command == "update" and os.getenv("CN_MONITOR_PUBLISH") == "1")
        if args.command == "export" or (args.command == "publish" and args.skip_update):
            result = {"errors": []}
        else:
            result = run_pipeline(specs, offline=args.command == "recompute", force=args.force)
        if args.command == "export" or (publish and not result["errors"]):
            from .publication import export_results
            manifest = export_results()
            print(json.dumps({"export_id": manifest["export_id"], "objects": len(manifest["objects"]),
                "compressed_bytes": sum(item["bytes"] for item in manifest["objects"].values())}), flush=True)
            if publish:
                uploaded = subprocess.run(["node", "scripts/publish-results.mjs"], cwd=ROOT, check=False)
                if uploaded.returncode:
                    sys.exit(uploaded.returncode)
        elif publish:
            print("更新存在错误，本次未发布；线上保留此前完整结果", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
