#!/usr/bin/env python3
"""Activity monitor - runs every 30 min via cron.
Checks Garmin for new workouts, stores them in SQLite, analyzes with Claude,
and sends Telegram notifications.
"""

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.parse

# Add garminconnect from the garmin-mcp venv
GARMIN_SITE_PACKAGES = (
    "/home/david/.local/share/uv/tools/garmin-mcp/lib/python3.12/site-packages"
)
if GARMIN_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, GARMIN_SITE_PACKAGES)

BASE_DIR = "/opt/claude-butler"
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_DIR = os.path.join(BASE_DIR, "logs")
TRAINING_DB = os.path.join(STATE_DIR, "training.db")
FITNESS_MEMORY_FILE = os.path.join(STATE_DIR, "fitness_memory.md")
ACTIVITY_LOG = os.path.join(LOG_DIR, "activity_monitor.log")
LOCK_FILE = "/tmp/activity_monitor.lock"
VO2MAX_CHECK_FILE = os.path.join(STATE_DIR, "vo2max_last_check.txt")

GARTH_HOME = "/home/david/.cache/garmin-mcp/garth"

CLAUDE_TIMEOUT_SECONDS = 300

ALLOWED_TYPES = {
    "running", "cycling", "mountain_biking", "hiking", "walking", "swimming",
}


# ---------------------------------------------------------------------------
# Env loading (mirrors fitness_coach.py)
# ---------------------------------------------------------------------------

def _load_env() -> None:
    env_file = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_file):
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


_load_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = f"[{ts}] {msg}\n"
    with open(ACTIVITY_LOG, "a") as f:
        f.write(entry)
    print(entry, end="", flush=True)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    limit = 4000
    chunks, remaining = [], text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    for chunk in chunks:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": chunk}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        except Exception as e:
            log(f"Telegram send failed: {e}")
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# SQLite setup
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS activities (
            activity_id TEXT PRIMARY KEY,
            date TEXT,
            name TEXT,
            activity_type TEXT,
            duration_seconds REAL,
            distance_meters REAL,
            avg_heart_rate INTEGER,
            max_heart_rate INTEGER,
            calories INTEGER,
            training_load REAL,
            training_effect_aerobic REAL,
            training_effect_label TEXT,
            rpe INTEGER,
            splits_json TEXT,
            hr_zones_json TEXT,
            analysis TEXT,
            analyzed_at TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS weekly_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note TEXT,
            created_at TEXT,
            expires_at TEXT,
            promoted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS vo2max_history (
            date TEXT PRIMARY KEY,
            vo2max_running REAL,
            vo2max_cycling REAL,
            fetched_at TEXT
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Garmin connection (using garth tokens)
# ---------------------------------------------------------------------------

def connect_garmin():
    """Connect to Garmin using stored OAuth tokens."""
    try:
        import garminconnect

        client = garminconnect.Garmin()
        client.login(GARTH_HOME)
        log("Garmin connected via stored tokens")
        return client
    except Exception as e:
        log(f"Failed to connect to Garmin: {e}")
        return None


# ---------------------------------------------------------------------------
# Activity type normalisation
# ---------------------------------------------------------------------------

def _normalise_type(raw_type: str) -> str:
    """Map Garmin activity type strings to our allowed set."""
    if not raw_type:
        return ""
    t = raw_type.lower().replace(" ", "_")
    if "run" in t:
        return "running"
    if "mountain_bik" in t or "mtb" in t:
        return "mountain_biking"
    if "cycl" in t or "bik" in t or "cycling" in t:
        return "cycling"
    if "hik" in t:
        return "hiking"
    if "walk" in t:
        return "walking"
    if "swim" in t:
        return "swimming"
    return t


def _extract_type(activity: dict) -> str:
    """Pull activity type from various Garmin response structures."""
    # Try common keys in order of reliability
    for key in ("activityType", "activity_type"):
        val = activity.get(key)
        if isinstance(val, dict):
            return _normalise_type(val.get("typeKey", "") or val.get("key", ""))
        if isinstance(val, str):
            return _normalise_type(val)
    return _normalise_type(activity.get("sportTypeKey", ""))


# ---------------------------------------------------------------------------
# Build analysis prompt
# ---------------------------------------------------------------------------

def _build_prompt(data: dict) -> str:
    return f"""Eres el asistente personal de David. Acaba de terminar un entreno.

DATOS DEL ENTRENO:
{json.dumps(data, ensure_ascii=False, indent=2)}

Lee /opt/claude-butler/state/fitness_memory.md para contexto de objetivos.

Genera un analisis del entreno en ESPAÑOL para Telegram (texto plano, sin headers ##, sin **, max 200 palabras):
- Resumen rapido (tipo, distancia/tiempo, FC media)
- Si es running con splits: ritmo por repeticion, evolucion del ritmo y FC
- Calidad: Training Effect y lo que significa, RPE si hay
- 1-2 observaciones clave (no recomendaciones para mañana, eso lo hace el coach de las 4:30)

Empieza con un emoji relevante y el nombre del entreno.
"""


# ---------------------------------------------------------------------------
# Claude invocation (no --resume, isolated session per activity)
# ---------------------------------------------------------------------------

def run_claude_analysis(prompt: str) -> str | None:
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    try:
        env = os.environ.copy()
        env["PATH"] = f"/home/david/.local/bin:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
        proc = subprocess.run(
            cmd, cwd=BASE_DIR, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        log("Claude analysis timed out")
        return None

    raw = proc.stdout.strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        log(f"Claude JSON parse error: {raw[:300]}")
        return None

    if parsed.get("is_error"):
        log(f"Claude error: {parsed.get('result', '')[:200]}")
        return None

    return parsed.get("result", "").strip() or None


# ---------------------------------------------------------------------------
# VO2max — checked at most once/day via Garmin's training-status endpoint,
# which always surfaces the most recent known value (with its own
# calendarDate) regardless of the date queried. Stored under that date so
# the history reflects when Garmin actually recomputed it, not when we
# happened to poll.
# ---------------------------------------------------------------------------

def _should_check_vo2max() -> bool:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        with open(VO2MAX_CHECK_FILE) as f:
            return f.read().strip() != today
    except FileNotFoundError:
        return True


def sync_vo2max(conn: sqlite3.Connection, client) -> None:
    if not _should_check_vo2max():
        return
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        status = client.get_training_status(today)
        mv = (status or {}).get("mostRecentVO2Max") or {}
        generic = mv.get("generic") or {}
        cycling = mv.get("cycling") or {}
        running_val = generic.get("vo2MaxValue")
        cycling_val = cycling.get("vo2MaxValue")
        measured_date = generic.get("calendarDate") or cycling.get("calendarDate")

        if measured_date and (running_val or cycling_val):
            conn.execute(
                """INSERT OR IGNORE INTO vo2max_history
                   (date, vo2max_running, vo2max_cycling, fetched_at)
                   VALUES (?, ?, ?, ?)""",
                (measured_date, running_val, cycling_val,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            conn.commit()
            log(f"VO2max checked: running={running_val} cycling={cycling_val} (as of {measured_date})")
        else:
            log("VO2max checked: no data returned")

        with open(VO2MAX_CHECK_FILE, "w") as f:
            f.write(today)
    except Exception as e:
        log(f"Failed to sync VO2max: {e}")


# ---------------------------------------------------------------------------
# Expire weekly context into fitness_memory.md
# ---------------------------------------------------------------------------

def expire_weekly_context(conn: sqlite3.Connection) -> None:
    today = time.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT id, note FROM weekly_context WHERE expires_at < ? AND promoted = 0",
        (today,),
    ).fetchall()

    if not rows:
        return

    for row_id, note in rows:
        try:
            with open(FITNESS_MEMORY_FILE, "a") as f:
                f.write(f"\n- {today}: [contexto expirado] {note}\n")
            conn.execute(
                "UPDATE weekly_context SET promoted = 1 WHERE id = ?", (row_id,)
            )
            log(f"Promoted expired weekly_context id={row_id} to fitness_memory.md")
        except Exception as e:
            log(f"Error promoting weekly_context id={row_id}: {e}")

    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)

    # Flock to prevent overlapping runs
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another instance is running, exiting")
        return

    try:
        _main_inner()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _main_inner() -> None:
    log("Activity monitor started")

    conn = sqlite3.connect(TRAINING_DB)
    try:
        init_db(conn)
        expire_weekly_context(conn)
        _process_activities(conn)
    except Exception as e:
        log(f"Unhandled error in main: {e}")
    finally:
        conn.close()

    log("Activity monitor finished")


def _process_activities(conn: sqlite3.Connection) -> None:
    client = connect_garmin()
    if client is None:
        log("Garmin unavailable, skipping activity check")
        return

    # Fetch recent activities
    try:
        activities = client.get_activities(0, 10)
    except Exception as e:
        log(f"Failed to get activities: {e}")
        return

    sync_vo2max(conn, client)

    if not activities:
        log("No activities returned from Garmin")
        return

    log(f"Got {len(activities)} activities from Garmin")

    for act in activities:
        try:
            _handle_activity(conn, client, act)
        except Exception as e:
            act_id = str(act.get("activityId", "unknown"))
            log(f"Error handling activity {act_id}: {e}")


def _handle_activity(conn: sqlite3.Connection, client, act: dict) -> None:
    activity_id = str(act.get("activityId", ""))
    if not activity_id:
        return

    # Date filter: only process activities from the last 48h
    act_date_str = (act.get("startTimeLocal") or act.get("startTimeGMT") or "")[:10]
    if act_date_str:
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 48 * 3600))
        if act_date_str < cutoff:
            log(f"Skipping {activity_id}: date {act_date_str} older than 48h cutoff {cutoff}")
            return

    # Duration filter (> 600s)
    duration = float(act.get("duration", act.get("elapsedDuration", 0)) or 0)
    if duration <= 600:
        log(f"Skipping {activity_id}: duration {duration}s <= 600")
        return

    # Activity type filter
    act_type = _extract_type(act)
    if act_type not in ALLOWED_TYPES:
        log(f"Skipping {activity_id}: type '{act_type}' not in allowed list")
        return

    # Already in DB?
    existing = conn.execute(
        "SELECT activity_id FROM activities WHERE activity_id = ?", (activity_id,)
    ).fetchone()
    if existing:
        log(f"Activity {activity_id} already in DB, skipping")
        return

    log(f"New activity {activity_id}: {act.get('activityName', '?')} ({act_type}, {duration}s)")

    # Get full details
    raw_detail = {}
    try:
        raw_detail = client.get_activity(activity_id) or {}
    except Exception as e:
        log(f"Could not get details for {activity_id}: {e}")
        raw_detail = act

    # Extract fields
    name = act.get("activityName", raw_detail.get("activityName", "Entreno"))
    date_val = (act.get("startTimeLocal") or act.get("startTimeGMT") or "")[:10]
    avg_hr = int(act.get("averageHR") or raw_detail.get("averageHR") or 0) or None
    max_hr = int(act.get("maxHR") or raw_detail.get("maxHR") or 0) or None
    calories = int(act.get("calories") or raw_detail.get("calories") or 0) or None
    distance = float(act.get("distance") or raw_detail.get("distance") or 0) or None

    # Training metrics (may be nested)
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
    te_label = None
    for key in ("aerobicTrainingEffect", "aerobic_training_effect"):
        v = raw_detail.get(key) or act.get(key)
        if v is not None:
            try:
                aerobic_te = float(v)
                break
            except (TypeError, ValueError):
                pass
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

    # Splits and HR zones (store as JSON strings)
    splits = raw_detail.get("splits") or raw_detail.get("splitSummaries") or []
    splits_json = json.dumps(splits, ensure_ascii=False) if splits else None

    hr_zones = raw_detail.get("heartRateZones") or raw_detail.get("hrTimeInZones") or []
    hr_zones_json = json.dumps(hr_zones, ensure_ascii=False) if hr_zones else None

    raw_json = json.dumps(raw_detail, ensure_ascii=False)

    # Insert into DB
    conn.execute(
        """INSERT INTO activities
           (activity_id, date, name, activity_type, duration_seconds,
            distance_meters, avg_heart_rate, max_heart_rate, calories,
            training_load, training_effect_aerobic, training_effect_label,
            rpe, splits_json, hr_zones_json, analysis, analyzed_at, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
        (
            activity_id, date_val, name, act_type, duration,
            distance, avg_hr, max_hr, calories,
            training_load, aerobic_te, te_label,
            rpe, splits_json, hr_zones_json, raw_json,
        ),
    )
    conn.commit()
    log(f"Saved activity {activity_id} to DB")

    # Build Claude analysis prompt
    analysis_data = {
        "activity_id": activity_id,
        "name": name,
        "type": act_type,
        "date": date_val,
        "duration_seconds": duration,
        "distance_meters": distance,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "calories": calories,
        "training_load": training_load,
        "training_effect_aerobic": aerobic_te,
        "training_effect_label": te_label,
        "rpe": rpe,
        "splits": splits[:20] if splits else [],  # cap to avoid huge prompts
        "hr_zones": hr_zones,
    }

    prompt = _build_prompt(analysis_data)
    analysis = run_claude_analysis(prompt)

    if analysis:
        conn.execute(
            "UPDATE activities SET analysis = ?, analyzed_at = ? WHERE activity_id = ?",
            (analysis, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), activity_id),
        )
        conn.commit()
        log(f"Analysis saved for {activity_id}")
        send_telegram(analysis)
    else:
        # Fallback basic message
        dist_str = f" {distance/1000:.1f}km" if distance else ""
        msg = f"Nuevo entreno detectado: {name}{dist_str}"
        log(f"Claude analysis failed, sending basic notification")
        send_telegram(msg)


if __name__ == "__main__":
    main()
