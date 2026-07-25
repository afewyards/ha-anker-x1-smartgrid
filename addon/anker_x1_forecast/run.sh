#!/usr/bin/env sh
set -eu

# Supervisor writes /data/options.json root-only and restarts the addon on
# every options save, so re-fixing perms at each start is always sufficient
# (no need to persist state across restarts).
if [ -f /data/options.json ]; then
    chown root:appuser /data/options.json
    chmod 0640 /data/options.json
fi

# Future model artifacts land under /data too.
chown -R appuser:appuser /data

# One source of truth for the listener port: uvicorn binds it and server.py reads
# the same env var to log the reachable URL at startup, so the two cannot drift.
FORECAST_PORT="${FORECAST_PORT:-8099}"
export FORECAST_PORT

# Drop privileges without relying on external privilege-drop binaries, whose
# presence in this base image is unverified. This inline python3 helper is
# exec'd so it replaces PID 1, then execvp replaces itself with uvicorn as
# appuser — no wrapper process stays alive.
exec python3 - <<'PYEOF'
import os
import pwd

pw = pwd.getpwnam("appuser")
os.setgroups([])
os.setgid(pw.pw_gid)
os.setuid(pw.pw_uid)
os.environ["HOME"] = pw.pw_dir
port = os.environ.get("FORECAST_PORT", "8099")
os.execvp("uvicorn", ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", port])
PYEOF
