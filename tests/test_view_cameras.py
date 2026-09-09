import sys

import pytest

from scripts import view_cameras


def test_headless_rejected_before_camera_access(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(sys, "argv", ["view-cameras"])
    monkeypatch.setattr(view_cameras, "configured_cameras", lambda: pytest.fail("Opened cameras"))
    with pytest.raises(SystemExit, match="No display"):
        view_cameras.main()


def test_dummy_display_is_not_a_screen(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":unused")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    with pytest.raises(RuntimeError, match="headless video drivers"):
        view_cameras.require_display()


def test_capture_orders_cameras_and_preserves_aspect_ratio():
    cameras = {
        role: dict(device=f"/dev/{role}", format="mjpeg", width=1280, height=720)
        for role in ("left", "right", "top")
    }
    command = view_cameras.capture_command(cameras)
    assert [command[i + 1] for i, arg in enumerate(command) if arg == "-i"] == [
        "/dev/left",
        "/dev/top",
        "/dev/right",
    ]
    assert view_cameras.layout().count("force_original_aspect_ratio=decrease") == 3
