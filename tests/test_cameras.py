import json
from types import SimpleNamespace

import pytest

from agentic_robots import cameras


@pytest.fixture
def camera_env(monkeypatch):
    for role in ("LEFT", "RIGHT", "TOP"):
        monkeypatch.setenv(f"{role}_CAMERA", f"/dev/{role.lower()}")
        for setting in ("WIDTH", "HEIGHT", "FPS"):
            monkeypatch.delenv(f"{role}_CAMERA_{setting}", raising=False)


def test_configured_modes_preserve_defaults(camera_env):
    configured = cameras.configured_cameras()
    assert [(c["width"], c["height"], c["fps"]) for c in configured.values()] == [
        (1280, 720, 30),
        (1280, 720, 30),
        (640, 480, 30),
    ]


def test_configured_modes_reach_ffmpeg_arguments(camera_env, monkeypatch):
    for role, height, fps in (("LEFT", 1200, 15), ("RIGHT", 1200, 15), ("TOP", 1080, 8)):
        monkeypatch.setenv(f"{role}_CAMERA_WIDTH", "1920")
        monkeypatch.setenv(f"{role}_CAMERA_HEIGHT", str(height))
        monkeypatch.setenv(f"{role}_CAMERA_FPS", str(fps))
    configured = cameras.configured_cameras()
    for role, camera in configured.items():
        args = cameras.input_args(camera)
        assert args[args.index("-framerate") + 1] == ("8" if role == "top" else "15")
        assert args[args.index("-video_size") + 1] == (
            "1920x1080" if role == "top" else "1920x1200"
        )


@pytest.mark.parametrize(
    ("setting", "value"),
    [("WIDTH", "0"), ("HEIGHT", "-1"), ("FPS", "0"), ("FPS", "nan"), ("FPS", "inf")],
)
def test_invalid_modes_rejected(camera_env, monkeypatch, setting, value):
    monkeypatch.setenv(f"TOP_CAMERA_{setting}", value)
    with pytest.raises(ValueError, match="positive and finite"):
        cameras.configured_cameras()


@pytest.mark.parametrize("role", ["left", "right", "top"])
@pytest.mark.parametrize(("kind", "fmt"), [("realsense", "yuyv422"), ("usb", "mjpeg")])
def test_json_camera_type_is_independent_of_role(monkeypatch, role, kind, fmt):
    setting = dict(type=kind, path="/dev/v4l/by-path/camera", width=1920, height=1080, fps=8)
    monkeypatch.setenv(f"{role.upper()}_CAMERA", json.dumps(setting))
    monkeypatch.setenv(f"{role.upper()}_CAMERA_FPS", "999")  # JSON is authoritative.
    config = cameras.configured_cameras((role,))[role]
    assert config == dict(
        type=kind, device=setting["path"], width=1920, height=1080, fps=8, format=fmt
    )
    assert cameras.input_args(config)[-1] == setting["path"]


@pytest.mark.parametrize(
    "change",
    [
        dict(path="relative"),
        dict(type="depth"),
        dict(fps=True),
        dict(width=1.5),
        dict(fps=0),
        dict(fps=float("nan")),
        dict(format="rgb24"),
        dict(typo=1),
        dict(type="realsense", format="mjpeg"),
    ],
)
def test_bad_camera_json_fails_before_opening_devices(change):
    config = dict(type="usb", path="/dev/fake", width=1280, height=720, fps=30)
    with pytest.raises(ValueError):
        cameras.camera_config(json.dumps({**config, **change}), "LEFT_CAMERA")


@pytest.mark.parametrize("value", ["[]", "null", "{}", '{"type":"usb"}', "{path: invalid}"])
def test_incomplete_camera_json_is_rejected(value):
    with pytest.raises(ValueError):
        cameras.camera_config(value, "LEFT_CAMERA")


def test_usb_uncompressed_format_override():
    config = cameras.camera_config(
        json.dumps(
            dict(type="usb", path="/dev/fake", width=640, height=480, fps=30, format="yuyv422")
        ),
        "TOP_CAMERA",
    )
    assert config["format"] == "yuyv422"


@pytest.mark.parametrize(("frames", "passed"), [(120, True), (90, False)])
def test_rate_check_uses_configured_target_without_opening_camera(
    tmp_path, monkeypatch, frames, passed
):
    device = tmp_path / "fake-camera"
    device.touch()
    monkeypatch.chdir(tmp_path)

    def fake_run(command, **kwargs):
        assert command[0] == "ffmpeg"
        assert command[command.index("-framerate") + 1] == "8"
        assert command[command.index("-fps_mode") + 1] == "passthrough"
        return SimpleNamespace(
            returncode=0, stdout=f"frame={frames}\nout_time_us=15000000\n", stderr=""
        )

    monkeypatch.setattr(cameras.subprocess, "run", fake_run)
    result = cameras.capture(
        "top", dict(device=str(device), format="yuyv422", width=1920, height=1080, fps=8), 15
    )
    assert result["requested_fps"] == 8
    assert (result["requested_width"], result["requested_height"]) == (1920, 1080)
    assert result["fps"] == frames / 15
    assert result["passed"] is passed
