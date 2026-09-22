"""Real Rust/media/HTTP/CLI integration; synthetic cameras and CAN-forbidden arms."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp import Client
from PIL import Image

from agentic_robots.recording import RecordingBridge, Rollout
from tests.test_camera_worker import frame_number, probe  # noqa: F401
from tests.test_robot_http import http_robot  # noqa: F401
from tests.test_robot_record import cameras
from tests.test_robot_record_service import http_recorder  # noqa: F401


def test_partial_worker_failure_and_startup_cleanup(tmp_path, monkeypatch, rust_binary):  # noqa: F811
    monkeypatch.setenv("ROBOT_CAMERA_BINARY", rust_binary)
    monkeypatch.setenv("ROBOT_CAMERA_TEST_ONLY", "1")
    monkeypatch.setattr("agentic_robots.recording.upstream_call", Mock(return_value={"arms": {}}))
    config = cameras()
    rollout = Rollout(tmp_path / "partial", "test", config, "unused")
    rollout.start()
    worker = rollout.workers["top"]
    worker.process.kill()
    worker.process.wait(timeout=2)
    try:
        images, errors = rollout.snapshots()
        assert set(images) == {"left", "right"}
        assert set(errors) == {"camera:top"}
        assert not rollout.status()["ready"]
    finally:
        result = rollout.finish()
    assert result["status"] == "failed"
    assert all(w.process.poll() is not None for w in rollout.workers.values())
    assert probe(rollout.output / "left.mp4")["streams"][0]["width"] == 320

    bad = cameras()
    bad["top"]["width"] = 0
    broken = Rollout(tmp_path / "bad-start", "test", bad, "unused")
    with pytest.raises(RuntimeError, match="startup"):
        broken.start()
    assert all(w.process.poll() is not None for w in broken.workers.values())


def test_images_are_requested_after_slow_feedback(
    tmp_path,
    monkeypatch,
    rust_binary,  # noqa: F811
):
    monkeypatch.setenv("ROBOT_CAMERA_BINARY", rust_binary)
    monkeypatch.setenv("ROBOT_CAMERA_TEST_ONLY", "1")
    monkeypatch.setattr("agentic_robots.recording.upstream_call", Mock(return_value={"arms": {}}))
    rollout = Rollout(tmp_path / "slow", "test", cameras(), "unused")
    rollout.start()
    try:
        bridge = RecordingBridge(rollout)

        markers = {}

        def slow_feedback(*args):
            time.sleep(0.3)
            images, errors = rollout.snapshots()
            assert not errors
            markers.update({role: frame_number(image) for role, image in images.items()})
            return {"arms": {}, "faults": {}, "errors": {}}

        monkeypatch.setattr(bridge, "forward", slow_feedback)
        observation = bridge.observe()
        assert not observation["errors"]
        assert set(observation["images"]) == {"left", "top", "right"}
        for role, image in observation["images"].items():
            assert set(image) == {"path"}
            assert frame_number(image) > markers[role]
        assert "capture_started_unix" not in observation
    finally:
        rollout.finish()


@pytest.mark.e2e
@pytest.mark.parametrize("http_recorder", [0, 250], indirect=True)
def test_rust_http_cli_rollout_images_actions_review_and_repeat(http_recorder):  # noqa: F811
    call, url, output, service, controller, _ = http_recorder
    assert call("recording", {"operation": "status"})["status"] == "idle"
    assert call("observe")["images"] == {}
    first_files = None
    for attempt in range(2):
        started = call("recording", {"operation": "start", "text": "Synthetic camera E2E"})
        assert started["ready"]
        assert "camera_workers" not in started
        directory = Path(started["output"])
        for arm in ("left", "right"):
            if attempt == 0:
                call("session", dict(operation="start", arm=arm, supported=True))

        # Use native MCP concurrently; the CLI is exercised by lifecycle/observe above.
        async def paired(target):
            async def move(arm):
                async with Client(url) as client:
                    return await client.call_tool(
                        "execute",
                        {
                            "action": dict(
                                arm=arm, kind="joint_target", joints_rad=target, duration_s=0.1
                            )
                        },
                    )

            return await asyncio.gather(move("left"), move("right"))

        for target in ([0.04, 0, 0, 0, 0, 0], [0] * 6):
            for response in asyncio.run(paired(target)):
                result = response.structured_content
                assert result["status"] == "completed", result
                post = result["post_action"]
                assert not post["errors"], post
                assert len([c for c in response.content if c.type == "image"]) == 3
                for image in post["images"].values():
                    assert set(image) == {"path"}
                    assert Image.open(image["path"]).size == (320, 240)
                assert "action_response_monotonic_ns" not in post
                assert "action_response_unix" not in post
                assert all(a["ee_pose"] for a in post["arms"].values())

        # Concurrent observations preserve separate immutable files.
        def observe(_):
            async def request():
                async with Client(url) as client:
                    return (await client.call_tool("observe", {})).structured_content

            return asyncio.run(request())

        with ThreadPoolExecutor(max_workers=3) as pool:
            observations = list(pool.map(observe, range(3)))
        assert all(len(o["images"]) == 3 and not o["errors"] for o in observations)
        assert len({o["images"]["left"]["path"] for o in observations}) == 3
        finished = call("recording", dict(operation="finish"))
        assert finished["status"] == "finished", finished
        manifest = json.loads((directory / "manifest.json").read_text())
        assert set(manifest["native_videos"]) == {"left", "top", "right"}
        assert "camera_workers" not in manifest
        assert "timestamps" not in manifest
        assert finished["videos"] == manifest["native_videos"]
        events = [
            json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        timestamps = [event["monotonic_elapsed_s"] for event in events]
        assert timestamps == sorted(timestamps)
        assert not (directory / "rollout.mp4").exists()
        assert not list(directory.glob("*.png"))
        for role in ("left", "top", "right"):
            stream = probe(directory / f"{role}.mp4")["streams"][0]
            assert (stream["width"], stream["height"]) == (320, 240)
            assert int(stream["nb_read_frames"]) > 0
            config = json.loads((directory / f"{role}-worker.json").read_text())
            if config["test"]["encoder_delay_ms"]:
                # The slow encoder recorded fewer frames without failing completion.
                assert int(stream["nb_read_frames"]) < float(stream["duration"]) * 10 * 0.9
        reviewed = call(
            "recording",
            dict(
                operation="review",
                review=dict(
                    outcome="success",
                    summary="Simulated arms moved and returned",
                    evidence=["Three videos and final feedback"],
                ),
            ),
        )
        assert reviewed["task"]["phase"] == "success"
        if first_files is None:
            first_files = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        else:
            assert all(p.read_bytes() == data for p, data in first_files.items())
        assert call("observe")["images"] == {}
        assert service.poll() is None and controller.poll() is None
    assert len(list(output.iterdir())) == 2
