from pathlib import Path

_ADDON = Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast"


def test_dockerfile_pins_base_and_drops_root():
    df = (_ADDON / "Dockerfile").read_text()
    assert "python:3.12-slim@sha256:" in df  # digest-pinned
    assert "\nUSER " in df  # non-root user set


def test_dockerignore_keeps_source_sha256():
    di = (_ADDON / ".dockerignore").read_text()
    assert "__pycache__" in di
    assert "SOURCE_SHA256" not in di  # manifest must remain in the image
