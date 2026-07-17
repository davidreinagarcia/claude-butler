#!/usr/bin/env python3
"""Populates/updates daily_metrics in training.db from Garmin's daily wellness
endpoints (sleep, HRV, training readiness, training status, daily stats).

Column-wise upsert: if a date row already exists with some columns filled,
re-running this only fills in NULL columns (or overwrites with fresh data
when re-fetched) - it never creates a duplicate row and never blanks out
data we already have that a given API call didn't return this time.

Safe to re-run for any date range, including dates already populated.

Usage:
    python3 sync_daily_metrics.py [days_back] [--start YYYY-MM-DD]

    days_back: how many days back from today to sync (default 14, for the
               daily cron use-case). For the one-time historical backfill,
               pass a large number (e.g. 220 - see note below on Garmin's
               real data horizon for these metrics).
"""

import json
import sqlite3
import sys
import time
import datetime

GARMIN_SITE_PACKAGES = (
    "/home/david/.local/share/uv/tools/garmin-mcp/lib/python3.12/site-packages"
)
if GARMIN_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, GARMIN_SITE_PACKAGES)

BASE_DIR = "/opt/claude-butler"
STATE_DIR = f"{BASE_DIR}/state"
LOG_DIR = f"{BASE_DIR}/logs"
TRAINING_DB = f"{STATE_DIR}/training.db"
GARTH_HOME = "/home/david/.cache/garmin-mcp/garth"

CALL_SLEEP = 0.4  # seconds between individual Garmin API calls

COLUMNS = [
    "steps", "resting_hr", "calories_total", "calories_active",
    "intensity_minutes_moderate", "intensity_minutes_vigorous",
    "stress_avg", "stress_max",
    "body_battery_charged", "body_battery_drained",
    "body_battery_high", "body_battery_low",
    "spo2_avg", "spo2_lowest", "respiration_avg", "stats_raw_json",
    "sleep_duration_s", "deep_sleep_s", "light_sleep_s", "rem_sleep_s",
    "awake_sleep_s", "sleep_score", "sleep_avg_hr", "sleep_raw_json",
    "hrv_last_night_avg", "hrv_weekly_avg", "hrv_status", "hrv_raw_json",
    "readiness_score", "readiness_level", "readiness_feedback",
    "readiness_sleep_factor_pct", "readiness_hrv_factor_pct",
    "readiness_acwr_factor_pct", "readiness_stress_factor_pct",
    "readiness_recovery_time_h", "readiness_raw_json",
    "vo2max_running", "vo2max_cycling", "training_status_code",
    "training_status_raw_json",
]


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(f"{LOG_DIR}/sync_daily_metrics.log", "a") as f:
        f.write(line + "\n")


def connect_garmin():
    import garminconnect
    client = garminconnect.Garmin()
    client.login(GARTH_HOME)
    return client


def safe_call(client, fn_name, date_str):
    try:
        fn = getattr(client, fn_name)
        result = fn(date_str)
        time.sleep(CALL_SLEEP)
        return result
    except Exception as e:
        log(f"  {date_str}: {fn_name} failed: {e}")
        time.sleep(CALL_SLEEP)
        return None


def extract_stats(data):
    if not data:
        return {}
    return {
        "steps": data.get("totalSteps"),
        "resting_hr": data.get("restingHeartRate"),
        "calories_total": data.get("totalKilocalories"),
        "calories_active": data.get("activeKilocalories"),
        "intensity_minutes_moderate": data.get("moderateIntensityMinutes"),
        "intensity_minutes_vigorous": data.get("vigorousIntensityMinutes"),
        "stress_avg": data.get("averageStressLevel"),
        "stress_max": data.get("maxStressLevel"),
        "body_battery_charged": data.get("bodyBatteryChargedValue"),
        "body_battery_drained": data.get("bodyBatteryDrainedValue"),
        "body_battery_high": data.get("bodyBatteryHighestValue"),
        "body_battery_low": data.get("bodyBatteryLowestValue"),
        "spo2_avg": data.get("averageSpo2"),
        "spo2_lowest": data.get("lowestSpo2"),
        "respiration_avg": data.get("avgWakingRespirationValue"),
        "stats_raw_json": json.dumps(data, ensure_ascii=False),
    }


def extract_sleep(data):
    if not data or not data.get("dailySleepDTO"):
        return {}
    dto = data["dailySleepDTO"] or {}
    overall = ((dto.get("sleepScores") or {}).get("overall") or {})
    return {
        "sleep_duration_s": dto.get("sleepTimeSeconds"),
        "deep_sleep_s": dto.get("deepSleepSeconds"),
        "light_sleep_s": dto.get("lightSleepSeconds"),
        "rem_sleep_s": dto.get("remSleepSeconds"),
        "awake_sleep_s": dto.get("awakeSleepSeconds"),
        "sleep_score": overall.get("value"),
        "sleep_avg_hr": dto.get("avgHeartRate"),
        "sleep_raw_json": json.dumps(data, ensure_ascii=False),
    }


def extract_hrv(data):
    if not data or not data.get("hrvSummary"):
        return {}
    summary = data["hrvSummary"] or {}
    return {
        "hrv_last_night_avg": summary.get("lastNightAvg"),
        "hrv_weekly_avg": summary.get("weeklyAvg"),
        "hrv_status": summary.get("status"),
        "hrv_raw_json": json.dumps(data, ensure_ascii=False),
    }


def extract_readiness(data):
    if not data:
        return {}
    entry = data[0] if isinstance(data, list) else data
    if not entry:
        return {}
    return {
        "readiness_score": entry.get("score"),
        "readiness_level": entry.get("level"),
        "readiness_feedback": entry.get("feedbackLong"),
        "readiness_sleep_factor_pct": entry.get("sleepScoreFactorPercent"),
        "readiness_hrv_factor_pct": entry.get("hrvFactorPercent"),
        "readiness_acwr_factor_pct": entry.get("acwrFactorPercent"),
        "readiness_stress_factor_pct": entry.get("stressHistoryFactorPercent"),
        "readiness_recovery_time_h": entry.get("recoveryTime"),
        "readiness_raw_json": json.dumps(data, ensure_ascii=False),
    }


def extract_training_status(data):
    if not data:
        return {}
    vo2 = data.get("mostRecentVO2Max") or {}
    generic = vo2.get("generic") or {}
    cycling = vo2.get("cycling") or {}

    status_code = None
    latest = ((data.get("mostRecentTrainingStatus") or {})
              .get("latestTrainingStatusData") or {})
    if latest:
        first_device = next(iter(latest.values()), {})
        status_code = first_device.get("trainingStatus")

    return {
        "vo2max_running": generic.get("vo2MaxValue"),
        "vo2max_cycling": cycling.get("vo2MaxValue") if cycling else None,
        "training_status_code": status_code,
        "training_status_raw_json": json.dumps(data, ensure_ascii=False),
    }


def upsert_day(conn, date_str, fields: dict):
    fields = {k: v for k, v in fields.items() if k in COLUMNS}
    if not fields:
        return False

    fields["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cols = list(fields.keys())

    insert_cols = ", ".join(["date"] + cols)
    placeholders = ", ".join(["?"] * (len(cols) + 1))
    update_clause = ", ".join(
        f"{c} = COALESCE(excluded.{c}, daily_metrics.{c})"
        for c in cols if c != "fetched_at"
    )
    update_clause += ", fetched_at = excluded.fetched_at"

    sql = (
        f"INSERT INTO daily_metrics ({insert_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(date) DO UPDATE SET {update_clause}"
    )
    values = [date_str] + [fields[c] for c in cols]
    conn.execute(sql, values)
    conn.commit()
    return True


def sync_date(client, conn, date_str):
    fields = {}
    fields.update(extract_stats(safe_call(client, "get_stats", date_str)))
    fields.update(extract_sleep(safe_call(client, "get_sleep_data", date_str)))
    fields.update(extract_hrv(safe_call(client, "get_hrv_data", date_str)))
    fields.update(extract_readiness(safe_call(client, "get_training_readiness", date_str)))
    fields.update(extract_training_status(safe_call(client, "get_training_status", date_str)))

    non_null = sum(1 for k, v in fields.items() if v is not None and not k.endswith("_raw_json"))
    if upsert_day(conn, date_str, fields):
        log(f"{date_str}: upserted ({non_null} non-null fields)")
    else:
        log(f"{date_str}: no data from any endpoint")


def main():
    days_back = 14
    start_date = None
    args = sys.argv[1:]
    if args and not args[0].startswith("--"):
        days_back = int(args[0])
    if "--start" in args:
        start_date = args[args.index("--start") + 1]

    if start_date:
        start = datetime.date.fromisoformat(start_date)
        today = datetime.date.today()
        days_back = (today - start).days + 1

    log(f"=== sync_daily_metrics started (days_back={days_back}, start={start_date}) ===")

    client = connect_garmin()
    conn = sqlite3.connect(TRAINING_DB)

    today = datetime.date.today()
    for i in range(days_back):
        d = (today - datetime.timedelta(days=i)).isoformat()
        sync_date(client, conn, d)

    conn.close()
    log("=== sync_daily_metrics done ===")


if __name__ == "__main__":
    main()
