"""Install exactly one project-owned launchd job; no existing job is replaced."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.cn-etf-monitor.daily-update"


def main():
    if sys.platform != "darwin":
        raise SystemExit("仅适用于macOS；Linux/服务器使用Docker scheduler或cron")
    node = shutil.which("node")
    if not node:
        raise SystemExit("未找到Node.js，无法运行新浪历史解码器")
    directory = Path.home() / "Library/LaunchAgents"
    target = directory / f"{LABEL}.plist"
    if target.exists():
        raise SystemExit(f"任务已存在，请先检查：{target}")
    logs = ROOT / "data/logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL, "ProgramArguments": ["/bin/bash", str(ROOT / "ops/run-update.sh")],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"CN_MONITOR_NODE_DIR": str(Path(node).parent)},
        "StartCalendarInterval": [{"Weekday": day, "Hour": hour, "Minute": 30}
            for day in range(1, 6) for hour in (8, 16, 20)],
        "StandardOutPath": str(logs / "scheduled-update.log"),
        "StandardErrorPath": str(logs / "scheduled-update-error.log"),
        "ProcessType": "Background", "LowPriorityIO": True,
    }
    content = plistlib.dumps(payload)
    if "--preview" in sys.argv:
        print(content.decode())
        return
    directory.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
    except subprocess.CalledProcessError:
        target.unlink()
        raise
    print(f"已安装 {target}")
    print("每周一至周五北京时间08:30、16:30、20:30更新；需电脑开机且当前用户登录。")
    print(f"查看：launchctl print gui/{os.getuid()}/{LABEL}")


if __name__ == "__main__":
    main()
