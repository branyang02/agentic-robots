import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from agentic_robots.camera_worker import CameraWorker


@pytest.fixture
def worker_factory(tmp_path, monkeypatch, rust_binary):
    monkeypatch.setenv("ROBOT_CAMERA_BINARY", rust_binary)
    monkeypatch.setenv("ROBOT_CAMERA_TEST_ONLY", "1")
    workers = []

    def create(role="left", fps=15, width=320, height=240, **faults):
        directory = tmp_path / str(len(workers))
        worker = CameraWorker(
            role,
            dict(device="synthetic", format="synthetic", width=width, height=height, fps=fps),
            directory,
        )
        workers.append(worker)
        worker.start(test=dict(color=[255, 0, 0], **faults))
        return worker

    yield create
    for worker in workers:
        with contextlib.suppress(Exception):
            worker.close()


def wait_ready(worker):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = worker.call("status")
        if status["ready"]:
            return status
        assert status["capture_error"] is None, status
        assert status["recording_error"] is None, status
        time.sleep(0.02)
    pytest.fail(f"worker did not become ready: {status}")


def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def frame_number(record):
    # Only the synthetic source encodes a frame number in its pixels.
    image = Image.open(record["path"])
    return int.from_bytes(bytes(image.getpixel((i, 0))[0] for i in range(8)), "little")


def test_snapshot_waits_for_new_content_and_keeps_previous_images(worker_factory, tmp_path):
    worker = worker_factory(fps=5)
    wait_ready(worker)
    first = worker.snapshot(tmp_path)
    image = Image.open(first["path"])
    assert set(first) == {"path"}
    assert image.size == (320, 240)
    assert image.getpixel((100, 100)) == (255, 0, 0)
    original = Path(first["path"]).read_bytes()
    next_dir = tmp_path / "next"
    next_dir.mkdir()
    second = worker.snapshot(next_dir)
    assert frame_number(second) > frame_number(first)
    assert Path(first["path"]).read_bytes() == original
    assert not list(worker.directory.glob("*.png")), "No continuous PNG export"
    final = worker.close()
    assert final == dict(ready=False, stopped=True, capture_error=None, recording_error=None)
    stream = probe(worker.config["output"])["streams"][0]
    assert (stream["width"], stream["height"]) == (320, 240)
    assert int(stream["nb_read_frames"]) > 0
    assert float(stream["start_time"]) == pytest.approx(0, abs=0.001)


def test_stalled_camera_times_out_instead_of_reusing_cached_frame(worker_factory, tmp_path):
    worker = worker_factory(stall_after_frames=4)
    wait_ready(worker)
    time.sleep(0.5)
    started = time.monotonic()
    with pytest.raises((RuntimeError, TimeoutError), match="timed out|timeout"):
        worker.snapshot(tmp_path)
    assert time.monotonic() - started < 3
    assert not list(tmp_path.glob("*.png"))
    assert worker.close()["recording_error"] is None  # The partial video is still usable.


def test_slow_encoder_skips_video_frames_but_snapshots_and_finish_succeed(worker_factory, tmp_path):
    worker = worker_factory(fps=30, encoder_delay_ms=100)
    wait_ready(worker)
    time.sleep(0.8)
    image = worker.snapshot(tmp_path)
    assert set(image) == {"path"}
    final = worker.close()
    assert final["recording_error"] is None
    stream = probe(worker.config["output"])["streams"][0]
    assert 0 < int(stream["nb_read_frames"]) < frame_number(image)


def test_worker_crash_is_isolated(worker_factory, tmp_path):
    first, second = worker_factory(), worker_factory("right")
    wait_ready(first)
    wait_ready(second)
    first.process.kill()
    first.process.wait(timeout=2)
    with pytest.raises(RuntimeError, match="not running"):
        first.snapshot(tmp_path)
    assert Image.open(second.snapshot(tmp_path)["path"]).size == (320, 240)
    assert second.close()["recording_error"] is None


def test_source_disconnect_is_reported_without_crashing_workers(worker_factory, tmp_path):
    failed = worker_factory(fail_after_frames=4)
    healthy = worker_factory("right")
    wait_ready(healthy)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = failed.call("status")
        if status["stopped"]:
            break
        time.sleep(0.02)
    assert status["stopped"] and not status["ready"]
    assert "disconnected" in status["capture_error"]
    assert failed.process.poll() is None  # Protocol still reports useful health.
    with pytest.raises(RuntimeError, match="capture failed"):
        failed.snapshot(tmp_path)
    assert Image.open(healthy.snapshot(tmp_path)["path"]).size == (320, 240)
    assert failed.close()["capture_error"]
    assert healthy.close()["recording_error"] is None


def test_worker_rejects_hardware_in_test_environment(tmp_path, rust_binary, monkeypatch):
    monkeypatch.setenv("ROBOT_CAMERA_TEST_ONLY", "1")
    config = dict(
        type="usb",
        device="/dev/video0",
        format="mjpeg",
        width=640,
        height=480,
        fps=30,
        output=str(tmp_path / "camera.mp4"),
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [rust_binary, str(path)], capture_output=True, text=True, env=os.environ, timeout=5
    )
    assert result.returncode != 0
    assert "hardware camera access is forbidden" in result.stderr
    assert not Path(config["output"]).exists()
