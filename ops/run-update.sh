#!/bin/bash
set -euo pipefail
CN_MONITOR_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$CN_MONITOR_ROOT"
# Node is required by the audited Sina payload decoder.
export PATH="${CN_MONITOR_NODE_DIR:-/usr/local/bin}:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
exec .venv/bin/python -m backend.app.cli update
