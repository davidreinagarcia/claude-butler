#!/usr/bin/env python3
"""Scheduled fitness coach - runs daily at 4:30am.
Reads Garmin data via MCP, analyzes training, sends Telegram report.
Daily on weekdays; Mondays add Training Status metrics + weekly plan."""

import json
import sqlite3
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse

BASE_DIR = "/opt/claude-butler"
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_DIR = os.path.join(BASE_DIR, "logs")
FITNESS_SESSION_FILE = os.path.join(STATE_DIR, "fitness_session_id.txt")
FITNESS_MEMORY_FILE = os.path.join(STATE_DIR, "fitness_memory.md")
FITNESS_LOG = os.path.join(LOG_DIR, "fitness_coach.log")
TRAINING_DB = os.path.join(STATE_DIR, "training.db")
FITNESS_LAST_REPORT = os.path.join(STATE_DIR, "fitness_last_report.txt")

CLAUDE_TIMEOUT_SECONDS = 900


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


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = f"[{ts}] {msg}\n"
    with open(FITNESS_LOG, "a") as f:
        f.write(entry)
    print(entry, end="", flush=True)


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


def get_session_id() -> str:
    try:
        with open(FITNESS_SESSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def set_session_id(sid: str) -> None:
    tmp = FITNESS_SESSION_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(sid)
    os.replace(tmp, FITNESS_SESSION_FILE)


def is_monday() -> bool:
    import datetime
    return datetime.datetime.now().weekday() == 0


DAILY_PROMPT = """Eres el entrenador personal de David. Esta es tu rutina diaria automatizada de las 4:30am.

PASO 1 - Lee la memoria:
Lee el archivo /opt/claude-butler/state/fitness_memory.md para cargar su perfil, objetivos actuales, lesiones, y el historial de entrenos recientes.

PASO 2 - Recoge datos de Garmin. Usa estas herramientas MCP:
- get_recent_activities(limit=5) + get_activity_details(id) para cada actividad de ayer
  → Los detalles incluyen: cadencia, dinámica de carrera (contacto suelo, zancada, oscilación vertical),
    training_effect_label (qué tipo de adaptación produjo), training_load, RPE, stamina inicio/fin, body_battery_drain
- get_sleep() para anoche
- get_body_battery() para hoy
- get_stress() para hoy
- get_hrv_status() para estado HRV
- get_training_readiness() para la puntuación de disposición al entreno de hoy
- get_resting_heart_rate(days=7) para ver tendencia de FC reposo

PASO 3 - Genera el informe diario en ESPAÑOL. Formato para Telegram (texto plano, sin headers markdown, máximo 400 palabras):

Línea 1: emoji de resumen del día (ej: 💪 Buen trabajo ayer / 😴 Día de recuperación / 🔥 Gran sesión)

Resumen de ayer: 2-3 líneas sobre lo que hizo, la calidad del entreno, y cómo durmió.

Estado de recuperación: cómo llega hoy David (muy descansado / recuperado / algo fatigado / necesita descanso). Sé directo y concreto, con los números que lo justifican.

ENTRENO DE HOY:
Tipo: [running/ciclismo/gym/descanso/descanso activo]
Duración: [tiempo]
Intensidad: [zona o descripción]
Detalles: [qué hacer exactamente - sé específico, como un entrenador de verdad]

Si recomiendas descanso, explica por qué con datos (Body Battery bajo, sueño malo, carga alta, etc).

PASO 4 - Actualiza la memoria:
Añade al archivo /opt/claude-butler/state/fitness_memory.md:
- Resumen compacto de ayer en el historial semanal (formato: "YYYY-MM-DD: [tipo] [distancia/duración] [FC media] [nota breve]")
- Si hay alguna observación relevante de patrón o progresión, añádela en las notas del entrenador
- Si el archivo supera 250 líneas, resume las semanas más antiguas en 1-2 líneas cada una
"""

WEEKLY_ADDENDUM = """

PASO ADICIONAL (HOY ES LUNES) - Training Status:
Antes del informe diario, consulta estas métricas de Garmin:
- VO2 Max: consulta la tabla vo2max_history en /opt/claude-butler/state/training.db
  (columnas date, vo2max_running, vo2max_cycling) en vez de fiarte de una sola
  lectura MCP — tiene histórico real desde enero 2026, se actualiza sola cada vez
  que Garmin recalcula el valor. Usa el valor más reciente y compáralo con el de
  hace ~30 días para dar la tendencia (subiendo/estable/bajando) con números reales.
- Training Status (Productive/Maintaining/Peaking/Recovery/Unproductive/Detraining)
- Training Load últimos 7 días vs los 7 anteriores
- Acute Load / Chronic Load ratio si está disponible
- HRV últimas 2 semanas

Incluye un bloque "Estado de forma esta semana:" antes del informe diario con esos datos interpretados (3-4 líneas, sin tecnicismos innecesarios).

Y al FINAL del mensaje, añade el PLAN SEMANAL (Lun-Dom) en formato compacto:
Lun: [tipo] - [duración] - [intensidad] - [foco]
Mar: ...
[etc]

Basa el plan en: carga acumulada de las últimas 3 semanas, training status actual, y los objetivos en la memoria.
Si hay entreno de gym, ponlo el lunes (es fijo para David).
"""


def extract_plan_block(report: str) -> str:
    marker = "ENTRENO DE HOY"
    idx = report.upper().find(marker)
    if idx == -1:
        return report[:600]
    block = report[idx:idx + 600]
    # Cut at first blank line after the block starts
    double_nl = block.find(chr(10) + chr(10), 80)
    if double_nl != -1:
        block = block[:double_nl]
    return block.strip()

def save_daily_plan(date_str: str, plan_text: str, full_report: str) -> None:
    try:
        conn = sqlite3.connect(TRAINING_DB)
        conn.execute(
            """INSERT OR REPLACE INTO daily_plans (date, plan_text, full_report, created_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (date_str, plan_text, full_report),
        )
        conn.commit()
        conn.close()
        log(f"Daily plan saved to DB for {date_str}")
    except Exception as exc:
        log(f"Error saving daily plan: {exc}")


def run() -> None:
    weekly = "--weekly" in sys.argv or is_monday()
    log(f"Starting fitness coach (weekly={weekly})")

    prompt = DAILY_PROMPT + (WEEKLY_ADDENDUM if weekly else "")
    session_id = get_session_id()

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    if session_id:
        cmd += ["--resume", session_id]

    log(f"Invoking claude (session={'new' if not session_id else session_id[:8]}...)")

    try:
        env = os.environ.copy()
        env["PATH"] = f"/home/david/.local/bin:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
        proc = subprocess.run(
            cmd, cwd=BASE_DIR, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        log("Timeout expired")
        send_telegram("⏱ El informe de entrenamiento tardó demasiado. Prueba mandando 'dame mi informe de hoy' al butler.")
        return

    raw = proc.stdout.strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        log(f"JSON parse error. raw={raw[:300]}, stderr={proc.stderr[:300]}")
        send_telegram("⚠️ Error al generar el informe de entrenamiento:\n\n" + (proc.stderr or raw)[:800])
        return

    new_sid = parsed.get("session_id")
    if new_sid:
        set_session_id(new_sid)

    result = parsed.get("result", "(sin respuesta)")
    if parsed.get("is_error"):
        log(f"Claude error: {result[:200]}")
        send_telegram("⚠️ Error en el análisis de entrenamiento:\n\n" + result[:800])
        return

    log("Report generated, sending to Telegram")
    date_str = time.strftime("%Y-%m-%d", time.localtime())
    with open(FITNESS_LAST_REPORT, "w") as f:
        f.write(f"# Informe fitness {date_str}\n\n{result}")
    send_telegram(result)
    plan_text = extract_plan_block(result)
    save_daily_plan(date_str, plan_text, result)
    log("Done")


if __name__ == "__main__":
    run()
