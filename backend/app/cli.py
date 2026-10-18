import argparse
import fcntl
import json
import sys

from .config import DATA_DIR, INDEX_MAP, INDICES
from .pipeline import refresh_assessments, refresh_diagnostics, run_pipeline, seed


def main():
    parser = argparse.ArgumentParser(description="宽基指数：全历史采集、因子计算、IC研究")
    parser.add_argument("command", choices=["init", "backfill", "update", "recompute", "coverage", "assess", "diagnostics"])
    parser.add_argument("--index", choices=list(INDEX_MAP), action="append")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
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
        result = run_pipeline(specs, offline=args.command == "recompute", force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
