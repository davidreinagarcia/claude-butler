#!/usr/bin/env python3
"""One-shot backfill: walks back day by day filling vo2max_history from
Garmin's per-date max-metrics endpoint (which only returns a row on days the
value actually changed). Safe to re-run: INSERT OR IGNORE skips existing dates.

Usage:
    python3 /opt/claude-butler/backfill_vo2max.py [days_back]
"""

import datetime
import sqlite3
import sys
import time

sys.path.insert(
    0, "/home/david/.local/share/uv/tools/garmin-mcp/lib/python3.12/site-packages"
)

TRAINING_DB = "/opt/claude-butler/state/training.db"
GARTH_HOME = "/home/david/.cache/garmin-mcp/garth"


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 365

    import garminconnect
    client = garminconnect.Garmin()
    client.login(GARTH_HOME)

    conn = sqlite3.connect(TRAINING_DB)
    inserted = 0
    today = datetime.date.today()

    for i in range(days_back):
        d = (today - datetime.timedelta(days=i)).isoformat()
        try:
            data = client.get_max_metrics(d)
        except Exception as e:
            print(f"{d}: ERROR {e}")
            continue
        if not data:
            continue
        entry = data[0] if isinstance(data, list) else data
        generic = entry.get("generic") or {}
        cycling = entry.get("cycling") or {}
        running_val = generic.get("vo2MaxValue")
        cycling_val = cycling.get("vo2MaxValue")
        measured_date = generic.get("calendarDate") or cycling.get("calendarDate") or d
        if running_val or cycling_val:
            cur = conn.execute(
                """INSERT OR IGNORE INTO vo2max_history
                   (date, vo2max_running, vo2max_cycling, fetched_at)
                   VALUES (?, ?, ?, ?)""",
                (measured_date, running_val, cycling_val,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            if cur.rowcount:
                inserted += 1
                print(f"{measured_date}: running={running_val} cycling={cycling_val}")
            conn.commit()
        time.sleep(0.3)

    conn.close()
    print(f"Done. {inserted} new rows inserted.")


if __name__ == "__main__":
    main()
