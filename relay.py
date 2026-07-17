#!/usr/bin/env python3
"""Telegram <-> headless Claude Code relay. Long-polls Telegram, restricted to
a single chat id, invokes `claude -p` per message with session continuity,
and routes yes/no replies to the PreToolUse approval hook (hooks/guard.py)
instead of forwarding them to Claude when an approval is pending.
"""

import json
import os
import subprocess
import threading
import time
import urllib.request
import urllib.parse
import urllib.error

BASE_DIR = "/opt/claude-butler"
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_DIR = os.path.join(BASE_DIR, "logs")
OFFSET_FILE  = os.path.join(STATE_DIR, "offset.txt")
SESSION_FILE = os.path.join(STATE_DIR, "session_id.txt")
PENDING_FILE = os.path.join(STATE_DIR, "pending_approval.json")
RESUME_FILE  = os.path.join(STATE_DIR, "resume_task.json")
RESTART_FILE = os.path.join(STATE_DIR, "restart_requested.json")
AUDIT_LOG    = os.path.join(LOG_DIR, "audit.log")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
FILE_API_BASE = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
PHOTO_DIR = os.path.join(STATE_DIR, "photos")

CLAUDE_TIMEOUT_SECONDS = 1800
LAST_SEND_TS = [0.0]
SEND_LOCK = threading.Lock()

_START_TIME = time.time()

# Tracks the currently running Claude invocation (updated inside run_claude).
_claude_state: dict = {"active": False, "since_ts": 0.0, "prompt_preview": "", "proc": None}
_claude_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def log_audit(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_text(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def _write_text(path: str, value: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(value)
    os.replace(tmp, path)


def get_offset() -> int:
    v = _read_text(OFFSET_FILE, "0")
    return int(v) if v else 0


def set_offset(offset: int) -> None:
    _write_text(OFFSET_FILE, str(offset))


def get_session_id() -> str:
    return _read_text(SESSION_FILE, "")


def set_session_id(session_id: str) -> None:
    _write_text(SESSION_FILE, session_id)


def clear_session_id() -> None:
    try:
        os.remove(SESSION_FILE)
    except FileNotFoundError:
        pass


def read_pending() -> dict | None:
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def write_pending(data: dict) -> None:
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, PENDING_FILE)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def telegram_api(method: str, params: dict, timeout: int = 35) -> dict:
    url = f"{API_BASE}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def download_photo(file_id: str) -> str | None:
    """Download a Telegram photo by file_id, save to PHOTO_DIR, return local path."""
    try:
        os.makedirs(PHOTO_DIR, exist_ok=True)
        info = telegram_api("getFile", {"file_id": file_id})
        file_path = info.get("result", {}).get("file_path")
        if not file_path:
            return None
        url = f"{FILE_API_BASE}/{file_path}"
        ext = os.path.splitext(file_path)[1] or ".jpg"
        local_path = os.path.join(PHOTO_DIR, f"{file_id}{ext}")
        with urllib.request.urlopen(url, timeout=30) as resp:
            with open(local_path, "wb") as f:
                f.write(resp.read())
        return local_path
    except Exception as exc:
        log_audit({"event": "photo_download_error", "file_id": file_id, "error": str(exc)})
        return None


def send_message(text: str) -> None:
    # Respect a soft 2s minimum interval between sends and chunk at Telegram's limit.
    chunks = []
    remaining = text
    limit = 4000
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
        with SEND_LOCK:
            wait = 2.0 - (time.time() - LAST_SEND_TS[0])
            if wait > 0:
                time.sleep(wait)
            try:
                telegram_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
            except Exception as exc:
                log_audit({"event": "telegram_send_failed", "error": str(exc)})
            LAST_SEND_TS[0] = time.time()


# ---------------------------------------------------------------------------
# Restart / resume protocol
# ---------------------------------------------------------------------------

def check_restart_requested() -> None:
    """After a response is sent, execute any pending safe-restart request."""
    if not os.path.exists(RESTART_FILE):
        return
    try:
        with open(RESTART_FILE) as f:
            data = json.load(f)
        os.remove(RESTART_FILE)
        reason = data.get("reason", "sin motivo")
        log_audit({"event": "safe_restart_executing", "reason": reason})
        send_message("\U0001f504 Aplicando cambios - reiniciando el butler...")
        time.sleep(1)
        subprocess.Popen(["sudo", "systemctl", "restart", "claude-butler"])
    except Exception as exc:
        log_audit({"event": "safe_restart_error", "error": str(exc)})


def check_resume_task() -> None:
    """On startup, if a prior run saved a checkpoint, auto-resume it."""
    if not os.path.exists(RESUME_FILE):
        return
    try:
        with open(RESUME_FILE) as f:
            data = json.load(f)
        os.remove(RESUME_FILE)

        task      = data.get("task", "tarea pendiente")
        completed = data.get("completed", [])
        remaining = data.get("remaining", [])

        completed_str = "\n".join(f"  - {s}" for s in completed) or "  (no detallado)"
        remaining_str = "\n".join(f"  - {s}" for s in remaining) or "  (no detallado)"

        send_message(
            "\U0001f504 Servicio reiniciado - retomando tarea...\n\n"
            f"Tarea: {task}\n"
            f"Completado antes del reinicio:\n{completed_str}\n"
            f"Pendiente:\n{remaining_str}"
        )

        resume_prompt = (
            f"SISTEMA: El butler fue reiniciado intencionalmente mientras trabajabas en: \"{task}\".\n"
            f"Pasos ya completados antes del reinicio: {', '.join(completed) or 'no especificado'}.\n"
            f"Pasos que quedaban pendientes: {', '.join(remaining) or 'no especificado'}.\n\n"
            "Retoma la tarea desde donde la dejaste. Verifica el estado actual del sistema "
            "para confirmar que se aplico correctamente y que no antes de continuar."
        )
        log_audit({"event": "resume_task_triggered", "task": task, "completed": completed})
        threading.Thread(target=run_claude, args=(resume_prompt,), daemon=True).start()
    except Exception as exc:
        log_audit({"event": "resume_task_error", "error": str(exc)})
        send_message(f"Error al retomar tarea pendiente: {exc}")


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def run_claude(prompt: str) -> None:
    session_id = get_session_id()
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
    if session_id:
        cmd += ["--resume", session_id]

    log_audit({"event": "claude_invocation", "prompt": prompt[:200], "session_id": session_id})
    send_message("Pensando...")

    with _claude_state_lock:
        _claude_state["active"] = True
        _claude_state["since_ts"] = time.time()
        _claude_state["prompt_preview"] = prompt[:120]

    try:
        proc = subprocess.Popen(
            cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        with _claude_state_lock:
            _claude_state["proc"] = proc

        try:
            stdout, stderr = proc.communicate(timeout=CLAUDE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            log_audit({"event": "claude_timeout", "prompt": prompt[:200]})
            send_message(f"Timeout tras {CLAUDE_TIMEOUT_SECONDS}s sin respuesta. Prueba de nuevo o usa /cancelar.")
            return

        raw = stdout.strip()

    finally:
        with _claude_state_lock:
            _claude_state["active"] = False
            _claude_state["proc"] = None

    try:
        parsed = json.loads(raw)
    except Exception:
        log_audit({"event": "claude_parse_error", "raw": raw[:2000], "stderr": stderr[:2000]})
        send_message("Claude devolvio algo que no pude parsear:\n\n" + (raw or stderr)[:3500])
        return

    new_session_id = parsed.get("session_id")
    if new_session_id:
        set_session_id(new_session_id)

    result_text = parsed.get("result", "(sin resultado)")
    is_error = parsed.get("is_error", False)
    log_audit({"event": "claude_result", "session_id": new_session_id, "is_error": is_error})

    # Detect usage/rate limit exhaustion
    limit_keywords = ("usage limit", "rate limit", "too many requests", "overloaded",
                      "capacity", "quota", "unavailable", "paused until",
                      "limit reached", "context window")
    combined = (result_text + stderr).lower()
    if is_error and any(k in combined for k in limit_keywords):
        reset_ts = time.time() + 5 * 3600
        reset_str = time.strftime("%H:%M (hora servidor)", time.localtime(reset_ts))
        log_audit({"event": "usage_limit_detected"})
        send_message(
            "He llegado al limite de uso de Claude.\n\n"
            f"Los limites suelen resetarse ~5h desde que se alcanzan, aprox a las {reset_str}.\n\n"
            "Mandame un mensaje cuando quieras y retomo desde donde lo deje (sesion guardada)."
        )
        return

    send_message(result_text)
    check_restart_requested()


# ---------------------------------------------------------------------------
# Instant commands — respond immediately without invoking Claude
# ---------------------------------------------------------------------------

def _uptime_str() -> str:
    secs = int(time.time() - _START_TIME)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h}h {m}m {s}s"


def _handle_instant_command(cmd: str) -> None:

    if cmd == "/ping":
        with _claude_state_lock:
            active = _claude_state["active"]
            elapsed = int(time.time() - _claude_state["since_ts"]) if active else 0
        if active:
            send_message(f"Vivo. Uptime: {_uptime_str()}.\nClaude lleva {elapsed}s pensando en tu ultimo mensaje.")
        else:
            send_message(f"Vivo y libre. Uptime: {_uptime_str()}. Esperando tu siguiente mensaje.")
        return

    if cmd == "/que_haces":
        with _claude_state_lock:
            active = _claude_state["active"]
            elapsed = int(time.time() - _claude_state["since_ts"]) if active else 0
            preview = _claude_state["prompt_preview"]
        if active:
            send_message(
                f"Pensando... llevo {elapsed}s trabajando.\n\n"
                f"Tarea:\n\"{preview}{'...' if len(preview) >= 120 else ''}\""
            )
        else:
            send_message("No estoy haciendo nada ahora mismo. Esperando tu mensaje.")
        return

    if cmd == "/diagnostico":
        with _claude_state_lock:
            active = _claude_state["active"]
            elapsed = int(time.time() - _claude_state["since_ts"]) if active else 0
        sid = get_session_id()
        pending = read_pending()
        restart_pending = os.path.exists(RESTART_FILE)
        resume_pending = os.path.exists(RESUME_FILE)
        try:
            with open(AUDIT_LOG) as f:
                lines = f.readlines()
            last_event = json.loads(lines[-1]).get("event", "?") if lines else "ninguno"
            log_size = len(lines)
        except Exception:
            last_event = "error leyendo log"
            log_size = 0

        lines_out = [
            f"Uptime: {_uptime_str()}",
            f"Claude: {'PENSANDO (' + str(elapsed) + 's)' if active else 'libre'}",
            f"Sesion: {'activa (' + sid[:14] + '...)' if sid else 'ninguna'}",
            f"Aprobacion pendiente: {'SI' if pending and pending.get('status') == 'pending' else 'no'}",
            f"Restart pendiente: {'SI' if restart_pending else 'no'}",
            f"Resume pendiente: {'SI' if resume_pending else 'no'}",
            f"Ultimo evento: {last_event}",
            f"Entradas en log: {log_size}",
        ]
        send_message("Diagnostico del butler:\n\n" + "\n".join(lines_out))
        return

    if cmd == "/logs":
        try:
            with open(AUDIT_LOG) as f:
                lines = f.readlines()
            last = lines[-20:] if len(lines) >= 20 else lines
            parts = []
            for line in last:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    ts = e.get("ts", "")[-9:-1]  # HH:MM:SS from ISO
                    event = e.get("event", "?")
                    detail = (
                        e.get("command", "")
                        or e.get("text", "")[:60]
                        or e.get("prompt", "")[:60]
                        or e.get("error", "")[:60]
                        or e.get("task", "")[:60]
                    )
                    parts.append(f"{ts}  {event}" + (f"  {detail}" if detail else ""))
                except Exception:
                    parts.append(line[:80])
            send_message("Ultimas 20 entradas del log:\n\n" + "\n".join(parts))
        except Exception as exc:
            send_message(f"Error leyendo log: {exc}")
        return

    if cmd == "/sesion":
        sid = get_session_id()
        with _claude_state_lock:
            active = _claude_state["active"]
        if sid:
            send_message(
                f"Sesion activa: {sid[:20]}...\n"
                f"Claude: {'pensando ahora' if active else 'en espera'}.\n\n"
                "Usa /reset para borrar la sesion y empezar de cero."
            )
        else:
            send_message("Sin sesion activa. El siguiente mensaje empieza una nueva.")
        return

    if cmd == "/reiniciar":
        send_message("Reiniciando el butler en 2 segundos...")
        def do_restart():
            time.sleep(2)
            log_audit({"event": "manual_restart_via_command"})
            subprocess.Popen(["sudo", "systemctl", "restart", "claude-butler"])
        threading.Thread(target=do_restart, daemon=True).start()
        return

    if cmd == "/cancelar":
        with _claude_state_lock:
            active = _claude_state["active"]
            proc = _claude_state.get("proc")
        if active and proc:
            proc.terminate()
            log_audit({"event": "claude_cancelled_by_user"})
            send_message("Tarea cancelada. Claude interrumpido. Puedes mandarme otra cosa.")
        else:
            send_message("No hay ninguna tarea en curso ahora mismo.")
        return


# ---------------------------------------------------------------------------
# Slash commands that invoke Claude with an explicit prompt
# ---------------------------------------------------------------------------

_CLAUDE_COMMANDS = {
    "/informe": (
        "Dame el informe de entrenamiento de hoy. "
        "Lee la memoria en /opt/claude-butler/state/fitness_memory.md, recoge datos de Garmin "
        "(get_sleep, get_body_battery, get_stress, get_hrv_status, get_training_readiness, "
        "get_recent_activities con detalles) y genera el analisis de ayer + recomendacion de hoy. "
        "Formato Telegram: texto plano, parrafos cortos, sin headers markdown."
    ),
    "/estado": (
        "Dame un resumen rapido de mi estado de recuperacion ahora mismo. "
        "Usa get_body_battery, get_hrv_status, get_training_readiness y get_resting_heart_rate. "
        "Maximo 6-8 lineas. Texto plano, sin markdown."
    ),
    "/entreno": (
        "Basandote en mi estado actual de Garmin (get_body_battery, get_training_readiness, "
        "get_hrv_status, get_training_load) y mi memoria en /opt/claude-butler/state/fitness_memory.md, "
        "recomiendame el entreno optimo para hacer ahora. Se concreto: tipo, duracion, zonas, detalles. "
        "Texto plano estilo Telegram, sin markdown."
    ),
    "/objetivos": (
        "Leeme mis objetivos actuales de /opt/claude-butler/state/fitness_memory.md y explicame "
        "como actualizarlos. Si quiero cambiarlos, dimelo en el siguiente mensaje y los actualiza. "
        "Texto plano, sin markdown."
    ),
    "/semana": (
        "Dame el estado de forma de esta semana y el plan para los proximos 7 dias. "
        "Usa get_training_load(days=14), get_fitness_metrics, get_hrv_status, get_training_readiness "
        "y la memoria en /opt/claude-butler/state/fitness_memory.md. "
        "Incluye: estado de forma (Training Status, ATL/CTL), plan lun-dom con tipo/duracion/intensidad. "
        "Texto plano, sin markdown."
    ),
}

_INSTANT_COMMANDS = {
    "/ping", "/que_haces", "/diagnostico", "/logs",
    "/sesion", "/reiniciar", "/cancelar", "/reset",
}


# ---------------------------------------------------------------------------
# Message router
# ---------------------------------------------------------------------------

def handle_message(text: str) -> None:
    text = text.strip()

    pending = read_pending()
    if pending and pending.get("status") == "pending":
        answer = text.lower()
        if answer in ("yes", "y", "approve", "approved", "si", "s"):
            pending["status"] = "approved"
            write_pending(pending)
            log_audit({"event": "approval_received", "request_id": pending["request_id"], "decision": "approved"})
        elif answer in ("no", "n", "deny", "denied"):
            pending["status"] = "denied"
            write_pending(pending)
            log_audit({"event": "approval_received", "request_id": pending["request_id"], "decision": "denied"})
        else:
            send_message("Hay una aprobacion pendiente - responde SI o NO.")
        return

    cmd = text.split()[0].lower() if text.startswith("/") else None

    if cmd == "/reset":
        clear_session_id()
        send_message("Sesion borrada. El siguiente mensaje empieza conversacion nueva.")
        return

    if cmd in _INSTANT_COMMANDS:
        log_audit({"event": "inbound_instant_command", "command": cmd})
        _handle_instant_command(cmd)
        return

    if cmd and cmd in _CLAUDE_COMMANDS:
        log_audit({"event": "inbound_command", "command": cmd})
        threading.Thread(target=run_claude, args=(_CLAUDE_COMMANDS[cmd],), daemon=True).start()
        return

    log_audit({"event": "inbound_message", "text": text[:200]})
    threading.Thread(target=run_claude, args=(text,), daemon=True).start()


# ---------------------------------------------------------------------------
# Telegram poll loop
# ---------------------------------------------------------------------------

def poll_loop() -> None:
    offset = get_offset()
    while True:
        try:
            resp = telegram_api("getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
        except Exception as exc:
            log_audit({"event": "poll_error", "error": str(exc)})
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            set_offset(offset)

            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            chat_id = str(message.get("chat", {}).get("id", ""))
            if chat_id != str(TELEGRAM_CHAT_ID):
                log_audit({"event": "rejected_sender", "chat_id": chat_id})
                continue

            text = message.get("text", "")
            photos = message.get("photo")
            caption = message.get("caption", "")

            if photos:
                best = photos[-1]
                file_id = best.get("file_id")
                local_path = download_photo(file_id) if file_id else None
                if local_path:
                    photo_note = f"[Foto adjunta - leela con el Read tool: {local_path}]"
                    prompt = f"{photo_note}\n\n{caption}" if caption else photo_note
                else:
                    prompt = caption or "(foto recibida pero no se pudo descargar)"
                log_audit({"event": "inbound_photo", "caption": caption, "local_path": local_path})
                handle_message(prompt)
            elif text:
                handle_message(text)
            # else: sticker, voice, etc. — silently skip


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    log_audit({"event": "relay_started"})
    check_resume_task()
    poll_loop()
