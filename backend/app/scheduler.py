"""Separate unattended process. Restart-safe idempotency via date/slot stamp."""
import time
from datetime import datetime

from .config import DATA_DIR, SHANGHAI
from .updates import start_update, UpdateBusy


def main():
    from .pipeline import seed
    seed()
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
                        try:
                            result = start_update(trigger="scheduler")
                            outcome = f"job={result['job']['id']}"
                        except UpdateBusy:
                            outcome = "skipped=another_update_running"
                        except Exception as exc:
                            outcome = f"failed={type(exc).__name__}"
                        # At most one attempt per slot, including failures; retry at the next slot.
                        stamp.write_text(f"{datetime.now(SHANGHAI).isoformat()} {outcome}")
        time.sleep(30)


if __name__ == "__main__":
    main()
