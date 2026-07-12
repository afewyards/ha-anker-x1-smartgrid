"""Guard: the addon container starts as root ONLY to fix perms on
Supervisor-written /data, then drops to non-root appuser before exec'ing
the app.

Root cause this guards against: Supervisor writes /data/options.json
root-only and restarts the addon on every options save. The old image ran
as `USER appuser` (uid 1000) for the whole lifetime, so read_options()
(health.py) hit PermissionError on that file and silently fell back to
defaults — options changed in the addon UI never took effect. See
addon/anker_x1_forecast/Dockerfile and run.sh for the fix.
"""

from __future__ import annotations

import re
from pathlib import Path

_ADDON = Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast"


def test_dockerfile_creates_appuser_but_does_not_switch_to_it():
    df = (_ADDON / "Dockerfile").read_text()
    assert "useradd" in df and "appuser" in df  # non-root user still created
    # Container must start as root (no USER line switching to appuser) so
    # run.sh can fix /data perms before dropping privileges itself.
    assert not re.search(r"(?m)^USER\s+appuser\s*$", df)


def test_run_sh_fixes_options_json_permissions_every_start():
    sh = (_ADDON / "run.sh").read_text()
    assert "options.json" in sh
    assert re.search(r"chown\s+root:appuser\s+.*options\.json", sh)
    assert re.search(r"chmod\s+0?640\s+.*options\.json", sh)


def test_run_sh_chowns_data_dir_for_appuser():
    sh = (_ADDON / "run.sh").read_text()
    assert re.search(r"chown\s+-R\s+appuser:appuser\s+/data", sh)


def test_run_sh_drops_privileges_and_execs_uvicorn():
    sh = (_ADDON / "run.sh").read_text()
    assert "setuid" in sh
    assert "setgid" in sh
    assert "execvp" in sh
    assert "uvicorn" in sh
    assert "8099" in sh
    assert "server:app" in sh


def test_run_sh_does_not_use_external_privilege_tools():
    sh = (_ADDON / "run.sh").read_text()
    for tool in ("gosu", "setpriv", "su "):
        assert tool not in sh
