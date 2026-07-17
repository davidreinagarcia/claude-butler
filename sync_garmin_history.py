#!/usr/bin/env python3
"""One-shot backfill: fetches ALL historical Garmin activities into training.db.

No analysis, no Claude, no Telegram. Pure data population for ML use.
Safe to re-run: uses INSERT OR IGNORE so duplicates are skipped cleanly.

Usage:
    python3 /opt/claude-butler/sync_garmin_history.py
"""

import json
import os
import sqlite3
import sys
import time

GARMIN_SITE_PACKAGES = (
    "/home/david/.local/share/uv/tools/garmin-mcp/lib/python3.12/site-packages"
)
if GARMIN_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, GARMIN_SITE_PACKAGES)

BASE_DIR = "/opt/claude-butler"
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_DIR = os.path.join(BASE_DIR, "logs")
TRAINING_DB = os.path.join(STATE_DIR, "training.db")
GARTH_HOME = "/home/david/.cache/garmin-mcp/garth"

BATCH_SIZE = 100
DETAIL_SLEEP = 0.8   # seconds between detail API calls
BATCH_SLEEP = 1.5    # seconds between pagination calls


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG_DIR, "sync_garmin_history.log"), "a") as f:
        f.write(line + "\n")


def connect_garmin():
    import garminconnect
    client = garminconnect.Garmin()
    client.login(GARTH_HOME)
    log("Garmin connected via stored tokens")
    return client


def _normalise_type(raw_type: str) -> str:
    if not raw_type:
        return ""
    t = raw_type.lower().replace(" ", "_")
    if "run" in t:
        return "running"
    if "mountain_bik" in t or "mtb" in t:
        return "mountain_biking"
    if "cycl" in t or "bik" in t:
        return "cycling"
    if "hik" in t:
        return "hiking"
    if "walk" in t:
        return "walking"
    if "swim" in t:
        return "swimming"
    return t


def _extract_type(activity: dict) -> str:
    for key in ("activityType", "activity_type"):
        val = activity.get(key)
        if isinstance(val, dict):
            return _normalise_type(val.get("typeKey", "") or val.get("key", ""))
        if isinstance(val, str):
            return _normalise_type(val)
    return _normalise_type(activity.get("sportTypeKey", ""))


def _extract_fields(act: dict, raw_detail: dict) -> dict:
    duration = float(act.get("duration") or act.get("elapsedDuration") or 0)
    distance = float(act.get("distance") or raw_detail.get("distance") or 0) or None
    avg_hr = int(act.get("averageHR") or raw_detail.get("averageHR") or 0) or None
    max_hr = int(act.get("maxHR") or raw_detail.get("maxHR") or 0) or None
    calories = int(act.get("calories") or raw_detail.get("calories") or 0) or None

    training_load = None
    for key in ("activityTrainingLoad", "trainingLoad", "training_load"):
        v = raw_detail.get(key) or act.get(key)
        if v is not None:
            try:
                training_load = float(v)
                break
            except (TypeError, ValueError):
                pass

    aerobic_te = None
    for key in ("aerobicTrainingEffect", "aerobic_training_effect"):
        v = raw_detail.get(key) or act.get(key)
        if v is not None:
            try:
                aerobic_te = float(v)
                break
            except (TypeError, ValueError):
                pass

    te_label = None
    for key in ("aerobicTrainingEffectMessage", "trainingEffectLabel"):
        v = raw_detail.get(key) or act.get(key)
        if v:
            te_label = str(v)
            break

    rpe = None
    for key in ("perceivedExertion", "perceived_exertion", "rpe"):
        v = raw_detail.get(key) or act.get(key)
        if v is not None:
            try:
                rpe = int(v)
                break
            except (TypeError, ValueError):
                pass

    splits = raw_detail.get("splits") or raw_detail.get("splitSummaries") or []
    splits_json = json.dumps(splits, ensure_ascii=False) if splits else None

    hr_zones = raw_detail.get("heartRateZones") or raw_detail.get("hrTimeInZones") or []
    hr_zones_json = json.dumps(hr_zones, ensure_ascii=False) if hr_zones else None

    return {
        "duration": duration,
        "distance": distance,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "calories": calories,
        "training_load": training_load,
        "aerobic_te": aerobic_te,
        "te_label": te_label,
        "rpe": rpe,
        "splits_json": splits_json,
        "hr_zones_json": hr_zones_json,
    }


def main() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    log("=== sync_garmin_history started ===")

    client = connect_garmin()
    conn = sqlite3.connect(TRAINING_DB)

    total_inserted = 0
    total_skipped = 0
    offset = 0

    try:
        while True:
            log(f"Fetching activities offset={offset} limit={BATCH_SIZE}...")
            try:
                batch = client.get_activities(offset, BATCH_SIZE)
            except Exception as e:
                log(f"Error fetching batch at offset={offset}: {e}")
                break

            if not batch:
                log("Empty batch — reached end of history")
                break

            log(f"Got {len(batch)} activities in this batch")

            for act in batch:
                activity_id = str(act.get("activityId", ""))
                if not activity_id:
                    continue

                # Skip if already in DB
                existing = conn.execute(
                    "SELECT 1 FROM activities WHERE activity_id = ?", (activity_id,)
                ).fetchone()
                if existing:
                    total_skipped += 1
                    continue

                name = act.get("activityName", "")
                date_val = (act.get("startTimeLocal") or act.get("startTimeGMT") or "")[:10]
                act_type = _extract_type(act)

                # Fetch full details for splits, HR zones, training metrics
                raw_detail = {}
                try:
                    raw_detail = client.get_activity(activity_id) or {}
                    time.sleep(DETAIL_SLEEP)
                except Exception as e:
                    log(f"  Could not get details for {activity_id}, storing summary only: {e}")
                    raw_detail = act

                fields = _extract_fields(act, raw_detail)
                raw_json = json.dumps(raw_detail, ensure_ascii=False)

                conn.execute(
                    """INSERT OR IGNORE INTO activities
                       (activity_id, date, name, activity_type, duration_seconds,
                        distance_meters, avg_heart_rate, max_heart_rate, calories,
                        training_load, training_effect_aerobic, training_effect_label,
                        rpe, splits_json, hr_zones_json, analysis, analyzed_at, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                    (
                        activity_id, date_val, name, act_type,
                        fields["duration"], fields["distance"],
                        fields["avg_hr"], fields["max_hr"], fields["calories"],
                        fields["training_load"], fields["aerobic_te"], fields["te_label"],
                        fields["rpe"], fields["splits_json"], fields["hr_zones_json"],
                        raw_json,
                    ),
                )
                conn.commit()
                total_inserted += 1
                log(f"  Saved {activity_id}: {name} ({act_type}, {date_val})")

            if len(batch) < BATCH_SIZE:
                log("Partial batch — no more activities")
                break

            offset += BATCH_SIZE
            time.sleep(BATCH_SLEEP)

    finally:
        conn.close()

    log(f"=== Done: {total_inserted} inserted, {total_skipped} already existed ===")


if __name__ == "__main__":
    main()
