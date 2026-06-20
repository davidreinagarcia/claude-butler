#!/bin/bash
# safe_restart.sh - restart claude-butler AFTER the current response is sent.
# Usage: safe_restart.sh "task description" "completed steps" "remaining steps"
#
# This saves a checkpoint and signals the relay to restart cleanly once it
# finishes sending the current Claude response. On next startup, the relay
# auto-resumes from the checkpoint.

set -a; source /opt/claude-butler/.env; set +a

TASK="${1:-Tarea no especificada}"
COMPLETED="${2:-}"
REMAINING="${3:-}"

python3 - "$TASK" "$COMPLETED" "$REMAINING" << 'PYEOF'
import json, pathlib, sys, time

task      = sys.argv[1] if len(sys.argv) > 1 else "Tarea no especificada"
completed = [s.strip() for s in sys.argv[2].split('\n') if s.strip()] if len(sys.argv) > 2 else []
remaining = [s.strip() for s in sys.argv[3].split('\n') if s.strip()] if len(sys.argv) > 3 else []

STATE = pathlib.Path('/opt/claude-butler/state')
now   = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

# 1) Checkpoint for auto-resume
(STATE / 'resume_task.json').write_text(json.dumps({
    'task': task, 'completed': completed, 'remaining': remaining, 'ts': now
}, indent=2, ensure_ascii=False))

# 2) Signal relay to restart cleanly after this response
(STATE / 'restart_requested.json').write_text(json.dumps({
    'reason': task, 'ts': now
}, indent=2))

print(f"Checkpoint guardado. El servicio se reiniciará en cuanto termine de enviar esta respuesta.")
print(f"Tarea: {task}")
if completed: print(f"Completado: {', '.join(completed)}")
if remaining: print(f"Pendiente al retomar: {', '.join(remaining)}")
PYEOF
