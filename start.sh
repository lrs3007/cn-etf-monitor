#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data/logs data/run
for CN_MONITOR_SERVICE in api web scheduler; do
  CN_MONITOR_PIDFILE="data/run/$CN_MONITOR_SERVICE.pid"
  if [ -f "$CN_MONITOR_PIDFILE" ]; then
    CN_MONITOR_PID="$(cat "$CN_MONITOR_PIDFILE")"
    if [[ "$CN_MONITOR_PID" =~ ^[0-9]+$ ]] && kill -0 "$CN_MONITOR_PID" 2>/dev/null; then
      if [ "${1:-}" != "--restart" ]; then
        echo "服务已在运行。如需更新，请使用 ./start.sh --restart"
        exit 1
      fi
      CN_MONITOR_CWD="$(lsof -a -p "$CN_MONITOR_PID" -d cwd -Fn 2>/dev/null | awk '/^n/{print substr($0,2);exit}')"
      CN_MONITOR_COMMAND="$(ps -p "$CN_MONITOR_PID" -o command=)"
      if [ "$CN_MONITOR_CWD" != "$PWD" ]; then
        echo "PID与当前项目目录不符，拒绝结束进程"
        exit 1
      fi
      case "$CN_MONITOR_COMMAND" in
        *"uvicorn backend.app.api:app"*|*"next-server"*|*"next/dist/bin/next start"*|*"backend.app.scheduler"*) kill "$CN_MONITOR_PID" ;;
        *) echo "PID不是本项目服务，拒绝结束进程"; exit 1 ;;
      esac
    fi
  fi
done
if [ "${1:-}" = "--restart" ]; then sleep 1; fi
if [ ! -x .venv/bin/python ]; then
  echo "请先创建Python环境并安装 requirements.txt"
  exit 1
fi
if [ ! -f .next-local/BUILD_ID ]; then npm run build; fi
.venv/bin/python -m uvicorn backend.app.api:app --host 127.0.0.1 --port 8000 >> data/logs/api.log 2>&1 &
API_PID=$!
node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3000 >> data/logs/web.log 2>&1 &
WEB_PID=$!
.venv/bin/python -m backend.app.scheduler >> data/logs/scheduler.log 2>&1 &
SCHEDULER_PID=$!
trap 'kill "$API_PID" "$WEB_PID" "$SCHEDULER_PID" 2>/dev/null || true' EXIT INT TERM
# Do not overwrite the previous PID records when a sandbox or occupied port
# prevents the new services from binding. Startup messages alone aren't readiness.
sleep 1
if ! kill -0 "$API_PID" 2>/dev/null || ! kill -0 "$WEB_PID" 2>/dev/null || ! kill -0 "$SCHEDULER_PID" 2>/dev/null; then
  echo "服务启动失败，请检查 data/logs/api.log、web.log 和 scheduler.log"
  exit 1
fi
echo "$API_PID" > data/run/api.pid
echo "$WEB_PID" > data/run/web.pid
echo "$SCHEDULER_PID" > data/run/scheduler.pid
echo "网页：http://localhost:3000"
echo "API：http://127.0.0.1:8000/docs"
while kill -0 "$API_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null && kill -0 "$SCHEDULER_PID" 2>/dev/null; do
  sleep 2
done
