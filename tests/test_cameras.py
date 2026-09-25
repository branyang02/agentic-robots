from types import SimpleNamespace

import pytest

from agentic_robots import cameras

# Abbreviated v4l2-ctl output from the USB wrists and RealSense RGB camera.
USB = """
    [0]: 'MJPG' (Motion-JPEG, compressed)
        Size: Discrete 1280x720
            Interval: Discrete 0.033s (30.000 fps)
            Interval: Discrete 0.200s (5.000 fps)
        Size: Discrete 1920x1200
            Interval: Discrete 0.011s (90.000 fps)
            Interval: Discrete 0.200s (5.000 fps)
    [1]: 'YUYV' (YUYV 4:2:2)
        Size: Discrete 1920x1200
            Interval: Discrete 0.200s (5.000 fps)
"""
REALSENSE = """
    [0]: 'YUYV' (YUYV 4:2:2)
        Size: Discrete 1920x1080
            Interval: Discrete 0.125s (8.000 fps)
        Size: Discrete 640x480
            Interval: Discrete 0.167s (6.000 fps)
            Interval: Discrete 0.067s (15.000 fps)
            Interval: Discrete 0.033s (30.000 fps)
    [1]: 'Z16 ' (16-bit Depth)
        Size: Discrete 4096x2160
            Interval: Discrete 0.033s (30.000 fps)
"""


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.delenv("CAMERA_RESOLUTION", raising=False)
    for role in ("LEFT", "RIGHT", "TOP"):
        monkeypatch.setenv(f"{role}_CAMERA", f"/dev/{role.lower()}")
    monkeypatch.setattr(cameras, "formats", lambda dev: REALSENSE if dev == "/dev/top" else USB)


@pytest.mark.parametrize("mode", ["current", "full"])
def test_resolution_selects_supported_modes_and_ffmpeg_inputs(configured, monkeypatch, mode):
    monkeypatch.setenv("CAMERA_RESOLUTION", mode)
    selected = cameras.configured_cameras()
    for role, camera in selected.items():
        if mode == "full":
            expected = (1920, 1080, 8) if role == "top" else (1920, 1200, 5)
        else:
            expected = (640, 480, 30) if role == "top" else (1280, 720, 30)
        assert (camera["width"], camera["height"], camera["fps"]) == expected
        args = cameras.input_args(camera)
        assert args[args.index("-framerate") + 1] == f"{expected[2]:g}"
        assert args[args.index("-video_size") + 1] == f"{expected[0]}x{expected[1]}"
        assert args[-1] == camera["device"]


def test_default_is_current_and_bad_setting_fails_before_device_access(configured, monkeypatch):
    assert all(c["resolution"] == "current" for c in cameras.configured_cameras().values())
    monkeypatch.setenv("CAMERA_RESOLUTION", "high")
    monkeypatch.setattr(cameras, "formats", lambda _: pytest.fail("Device accessed"))
    with pytest.raises(ValueError, match="Choose 'current' or 'full'"):
        cameras.configured_cameras()


def test_camera_type_is_discovered_not_assumed_from_role(configured, monkeypatch):
    monkeypatch.setenv("CAMERA_RESOLUTION", "full")
    monkeypatch.setattr(cameras, "formats", lambda _: REALSENSE)
    left = cameras.configured_cameras()["left"]
    assert (left["format"], left["width"], left["height"], left["fps"]) == (
        "yuyv422",
        1920,
        1080,
        8,
    )


def test_current_uses_advertised_rate_when_thirty_is_unavailable():
    modes = cameras.camera_modes(REALSENSE.replace("30.000 fps", "20.000 fps"))
    assert cameras.select_camera("test", modes, "current")["fps"] == 20


def test_full_ignores_sub_five_fps_and_prefers_compressed_usb_input():
    modes = cameras.camera_modes(USB.replace("90.000 fps", "2.000 fps"))
    selected = cameras.select_camera("test", modes, "full")
    assert selected["fps"] == 5 and selected["format"] == "mjpeg"


def test_full_does_not_silently_fall_back_to_smaller_size():
    modes = cameras.camera_modes(REALSENSE.replace("8.000 fps", "2.000 fps"))
    with pytest.raises(ValueError, match="no supported 'full' RGB mode"):
        cameras.select_camera("test", modes, "full")


def test_depth_and_stepwise_sizes_are_not_misparsed():
    assert len(cameras.camera_modes(REALSENSE)) == 4
    assert (
        cameras.camera_modes("""
        [0]: 'YUYV'
            Size: Stepwise 32x32 - 4096x2160 with step 2/2
                Interval: Discrete 0.033s (30.000 fps)
    """)
        == []
    )
    with pytest.raises(ValueError, match="no supported discrete"):
        cameras.select_camera("test", [], "full")


def test_setup_check_allows_dropped_frames_but_rejects_empty_capture(tmp_path, monkeypatch):
    device = tmp_path / "camera"
    device.touch()
    monkeypatch.chdir(tmp_path)
    camera = dict(device=str(device), format="yuyv422", width=1920, height=1080, fps=8)
    result = SimpleNamespace(returncode=0, stdout="frame=90\nout_time_us=15000000\n", stderr="")
    monkeypatch.setattr(cameras.subprocess, "run", lambda *a, **kw: result)
    assert cameras.capture("top", camera, 15)["passed"]
    result.stdout = ""
    with pytest.raises(RuntimeError, match="no measurable frames"):
        cameras.capture("top", camera, 15)
