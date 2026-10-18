"""Separate unattended process. Restart-safe idempotency via date/slot stamp."""
import subprocess
import sys
import time
from datetime import datetime

from .config import DATA_DIR, SHANGHAI


def main():
    slots = ((8, 30), (16, 30), (20, 30))
    directory = DATA_DIR / "schedule"
    directory.mkdir(exist_ok=True)
    while True:
        now = datetime.now(SHANGHAI)
        if now.weekday() < 5:
            for hour, minute in slots:
                if now.hour == hour and now.minute >= minute:
                    stamp = directory / f"{now.date()}-{hour:02d}{minute:02d}.attempted"
                    if not stamp.exists():
                        result = subprocess.run([sys.executable, "-m", "backend.app.cli", "update"], check=False)
                        # At most one attempt per slot, including failures; retry at the next slot.
                        stamp.write_text(f"{datetime.now(SHANGHAI).isoformat()} exit={result.returncode}")
        time.sleep(30)


if __name__ == "__main__":
    main()
