import re
from pathlib import Path

_ADDON = Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast"

# ecd4b4c: base image is pinned by TAG (not digest) so the build survives Docker
# Hub garbage-collecting untagged digests when 3.12-slim is rebuilt for patch
# releases. A digest suffix is accepted if one is ever re-added, but the tag
# itself must always be an explicit version (never "latest", never tagless).
_BASE_IMAGE_RE = re.compile(r"FROM python:3\.12-slim(@sha256:[0-9a-f]{64})?\b")


def test_dockerfile_pins_base_and_creates_nonroot_user():
    df = (_ADDON / "Dockerfile").read_text()
    assert _BASE_IMAGE_RE.search(df)  # version-pinned tag, optional digest suffix
    assert "python:3.12-slim:latest" not in df  # never float on :latest
    # Container starts as root (no USER line) so run.sh can fix
    # Supervisor-written /data perms, then drops to appuser itself before
    # exec'ing the app — see tests_addon/test_runtime_privileges.py.
    assert "appuser" in df  # non-root user still created


def test_dockerignore_keeps_source_sha256():
    di = (_ADDON / ".dockerignore").read_text()
    assert "__pycache__" in di
    assert "SOURCE_SHA256" not in di  # manifest must remain in the image
