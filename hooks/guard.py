#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool - gates genuinely destructive commands
behind a Telegram yes/no approval, fired even under --dangerously-skip-permissions.
Everything else is allowed instantly. See /opt/claude-butler/CLAUDE.md."""

import json
import os
import re
import sys
import time
import uuid
import urllib.request
import urllib.parse

BASE_DIR = "/opt/claude-butler"
PATTERNS_FILE = os.path.join(BASE_DIR, "hooks", "dangerous_patterns.json")
PENDING_FILE = os.path.join(BASE_DIR, "state", "pending_approval.json")
AUDIT_LOG = os.path.join(BASE_DIR, "logs", "audit.log")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TOTAL_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 1.5


def log_audit(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception:
        pass


def allow(reason: str = "") -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(out))
    sys.exit(0)


def deny(reason: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out))
    sys.exit(0)


def load_patterns() -> list:
    try:
        with open(PATTERNS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def match_dangerous(command: str, patterns: list) -> dict | None:
    for p in patterns:
        if re.search(p["pattern"], command, re.IGNORECASE):
            return p
    return None


def atomic_write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_pending() -> dict | None:
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def claim_slot(request_id: str, command: str) -> None:
    """Wait until the single pending-approval slot is free, then claim it."""
    deadline = time.time() + TOTAL_TIMEOUT_SECONDS
    while time.time() < deadline:
        current = read_pending()
        if current is None or current.get("status") != "pending":
            atomic_write(PENDING_FILE, {
                "request_id": request_id,
                "command": command,
                "status": "pending",
                "created_at": time.time(),
            })
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError("approval slot busy")


def wait_for_decision(request_id: str, deadline: float) -> str:
    while time.time() < deadline:
        current = read_pending()
        if current and current.get("request_id") == request_id:
            status = current.get("status")
            if status in ("approved", "denied"):
                return status
        time.sleep(POLL_INTERVAL_SECONDS)
    return "timeout"


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        allow("guard: could not parse hook input, defaulting to allow")
        return

    tool_name = payload.get("tool_name", "")
    command = payload.get("tool_input", {}).get("command", "")

    if tool_name != "Bash" or not command:
        allow()
        return

    patterns = load_patterns()
    match = match_dangerous(command, patterns)

    if not match:
        allow()
        return

    request_id = str(uuid.uuid4())
    log_audit({"event": "guard_flagged", "request_id": request_id, "command": command, "rule": match["name"]})

    # Butler self-restart: auto-deny and redirect to the safe protocol.
    # Asking David to approve would kill Claude before the response reaches him.
    if match["name"] == "butler_self_restart":
        send_telegram(
            "\U0001f6ab Reinicio directo del butler bloqueado.\n\n"
            "Usa en su lugar:\n"
            "/opt/claude-butler/safe_restart.sh \"descripcion\" \"pasos completados\" \"pasos pendientes\"\n\n"
            "Esto guarda un checkpoint y reinicia DESPUES de enviar la respuesta actual."
        )
        deny(
            "No uses 'systemctl restart claude-butler' directamente: te matas a ti mismo antes de "
            "enviar la respuesta. Usa /opt/claude-butler/safe_restart.sh con tres argumentos: "
            "(1) descripcion de la tarea, (2) pasos ya completados, (3) pasos pendientes. "
            "El relay reiniciara limpiamente despues de enviar la respuesta actual."
        )
        return

    try:
        claim_slot(request_id, command)
    except TimeoutError:
        log_audit({"event": "guard_slot_busy", "request_id": request_id, "command": command})
        deny("Another approval is already pending; please retry shortly.")
        return

    send_telegram(
        "⚠️ Disruptive command flagged:\n\n"
        f"{command}\n\n"
        f"Reason: {match['description']}\n\n"
        "Reply YES to approve or NO to deny. Auto-denies in 5 minutes."
    )

    deadline = time.time() + TOTAL_TIMEOUT_SECONDS
    decision = wait_for_decision(request_id, deadline)

    log_audit({"event": "guard_decision", "request_id": request_id, "command": command, "decision": decision})

    if decision == "approved":
        send_telegram(f"✅ Approved: {command}")
        allow(f"Telegram-approved by user (request {request_id})")
    elif decision == "denied":
        send_telegram(f"❌ Denied: {command}")
        deny(f"Denied via Telegram by user (request {request_id})")
    else:
        send_telegram(f"⏱️ Timed out, auto-denied: {command}")
        deny(f"No response within {TOTAL_TIMEOUT_SECONDS}s - auto-denied (request {request_id})")


if __name__ == "__main__":
    main()
