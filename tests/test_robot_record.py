import asyncio
import io
import json
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from mcp import Client
from PIL import Image

from agentic_robots.bridge import Action
from agentic_robots.recording import ORDER, RecordingBridge, Rollout
from scripts.robot_record import recording_server
from tests.test_robot_http import http_robot  # noqa: F401


def cameras():
    return {
        role: {"format": "lavfi", "device": f"color=c={color}:s=640x480:r=30"}
        for role, color in zip(ORDER, ("red", "green", "blue"))
    }


@pytest.fixture
def rollout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentic_robots.recording.upstream_call",
        Mock(
            return_value={
                "arms": {"left": {"joints_rad": [0] * 6}},
                "errors": {},
                "faults": {},
            }
        ),
    )
    return Rollout(tmp_path / "rollout", "test prompt", cameras(), "unused-test-upstream")


def ready_fake(r):
    r.state = "recording"
    r.process = Mock()
    r.process.poll.return_value = None
    for role in ORDER:
        Image.new("RGB", (20, 10), "red").save(r.output / f"{role}.png")


def events(r):
    return [json.loads(line) for line in (r.output / "events.jsonl").read_text().splitlines()]


def test_concurrent_event_log_is_parseable_and_keeps_prompt(rollout):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: rollout.event("test", n=i, value=float("nan")), range(50)))
    records = events(rollout)
    assert records[0]["text"] == "test prompt"
    assert sorted(e["n"] for e in records[1:]) == list(range(50))
    assert all(e["value"] == "nan" for e in records[1:])
    assert all(e["elapsed_s"] >= 0 for e in records)


def test_observations_are_immutable_and_use_shared_frames(rollout):
    ready_fake(rollout)
    bridge = RecordingBridge(rollout)
    first = bridge.observe()
    Image.new("RGB", (20, 10), "blue").save(rollout.output / "left.png")
    second = bridge.observe()
    assert first["images"]["left"]["path"] != second["images"]["left"]["path"]
    assert Image.open(first["images"]["left"]["path"]).getpixel((0, 0)) == (255, 0, 0)
    assert Image.open(second["images"]["left"]["path"]).getpixel((0, 0)) == (0, 0, 255)
    (rollout.output / "top.png").unlink()
    partial = bridge.observe()
    assert "camera:top" in partial["errors"]
    assert "left" in partial["arms"]
    assert "published_unix" in first["images"]["left"]


def test_actions_forward_unchanged_and_preserve_rejection(rollout, monkeypatch):
    ready_fake(rollout)
    rejection = {"status": "rejected", "error": {"code": "joint_limits", "details": {"joint": 1}}}
    upstream = Mock(return_value=rejection)
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    action = Action(arm="left", kind="joint_target", joints_rad=[4, 0, 0, 0, 0, 0])
    result = RecordingBridge(rollout).execute(action)
    assert all(result[k] == v for k, v in rejection.items())
    assert "post_action" in result and result["diagnostics"]
    upstream.assert_any_call(
        "unused-test-upstream", "execute", {"action": action.model_dump(exclude_none=True)}
    )
    request, response = [e for e in events(rollout) if e.get("tool") == "execute"]
    assert request["request_id"] == response["request_id"]
    assert response["result"] == rejection
    assert rollout.in_flight == 0


def test_transport_failure_is_recorded_without_automatic_retry(rollout, monkeypatch):
    ready_fake(rollout)
    upstream = Mock(side_effect=RuntimeError("connection lost"))
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    result = RecordingBridge(rollout).execute(
        Action(arm="left", kind="joint_target", joints_rad=[0] * 6)
    )
    assert not result["error"]["retryable"]
    assert "unknown" in result["error"]["details"]["action_outcome"]
    assert sum(c.args[1] == "execute" for c in upstream.call_args_list) == 1
    assert upstream.call_args.args[1:] == ("session", {"operation": "status"})
    assert rollout.in_flight == 0


def test_dead_capture_does_not_forward_actions_but_stop_still_works(rollout, monkeypatch):
    ready_fake(rollout)
    rollout.process.poll.return_value = 1
    upstream = Mock(return_value={"status": "stop requested; torque is not released"})
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    bridge = RecordingBridge(rollout)
    result = bridge.execute(Action(arm="left", kind="joint_target", joints_rad=[0] * 6))
    assert result["error"]["code"] == "recording_unavailable"
    assert all(c.args[1:] == ("session", {"operation": "status"}) for c in upstream.call_args_list)
    bridge.session("stop")
    assert upstream.call_args.args[2]["operation"] == "stop"


def test_finish_during_action_preserves_capture_and_hold(rollout):
    ready_fake(rollout)
    rollout.in_flight = 1
    result = rollout.finish()
    assert result["error"]["code"] == "actions_in_flight"
    rollout.process.send_signal.assert_not_called()
    assert rollout.state == "recording"


def test_idle_recorder_uses_no_cameras_and_reports_how_to_start(tmp_path, monkeypatch):
    camera_factory = Mock(side_effect=AssertionError("must not open cameras"))
    upstream = Mock(return_value={"arms": {}, "faults": {}, "errors": {}})
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    bridge = RecordingBridge(output_root=tmp_path / "absent", cameras=camera_factory)
    assert bridge.recording("status") == {"status": "idle", "ready": False}
    assert bridge.recording("finish")["status"] == "idle"
    assert bridge.recording("start", " ")["error"]["code"] == "task_required"
    assert bridge.recording("note", "note")["error"]["code"] == "recording_not_started"
    result = bridge.execute(Action(arm="left", kind="joint_target", joints_rad=[0] * 6))
    assert result["error"]["code"] == "recording_not_started"
    assert all(c.args[1:] == ("session", {"operation": "status"}) for c in upstream.call_args_list)
    assert bridge.observe()["images"] == {}
    bridge.session("stop")
    assert upstream.call_args.args[2]["operation"] == "stop"
    camera_factory.assert_not_called()
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("state", ["finished", "failed"])
def test_completed_recordings_stay_unchanged_during_idle_calls(rollout, monkeypatch, state):
    ready_fake(rollout)
    rollout.state = state
    rollout.process.poll.return_value = 0
    rollout.save_manifest()
    before = {p: p.read_bytes() for p in rollout.output.rglob("*") if p.is_file()}
    current = {"arms": {"left": {"joints_rad": [0.2, 0, 0, 0, 0, 0]}}}
    upstream = Mock(return_value=current)
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    bridge = RecordingBridge(rollout)

    observation = bridge.observe()
    assert observation["images"] == {}, "Do not pair old camera frames with live joint feedback"
    assert observation["arms"] == current["arms"]
    assert "recording" in observation["errors"]
    assert bridge.session("status")["arms"] == current["arms"]
    bridge.session("stop")
    assert upstream.call_args.args[2]["operation"] == "stop"
    upstream.reset_mock()
    rejected = bridge.execute(Action(arm="left", kind="joint_target", joints_rad=[0] * 6))
    assert rejected["error"]["code"] == "recording_unavailable"
    assert all(c.args[1:] == ("session", {"operation": "status"}) for c in upstream.call_args_list)
    after = {p: p.read_bytes() for p in rollout.output.rglob("*") if p.is_file()}
    assert after == before, "Idle calls must not modify a completed task's files"


def test_recording_rejects_overlapping_starts_and_finish_during_action(rollout):
    ready_fake(rollout)
    bridge = RecordingBridge(rollout)
    assert bridge.recording("start", "new")["error"]["code"] == "recording_active"
    rollout.in_flight = 1
    assert bridge.recording("finish")["error"]["code"] == "actions_in_flight"
    assert bridge.rollout is rollout
    assert rollout.state == "recording"
    with bridge.lifecycle:
        assert bridge.recording("start", "new")["error"]["code"] == "recording_busy"
        assert bridge.recording("status")["status"] == "recording"


def test_failed_start_reports_error_and_allows_correction(tmp_path, monkeypatch):
    camera_factory = Mock(side_effect=[OSError("camera config missing"), cameras()])
    monkeypatch.setattr(Rollout, "start", ready_fake)
    bridge = RecordingBridge(output_root=tmp_path, cameras=camera_factory)
    error = bridge.recording("start", "task")
    assert error["error"]["code"] == "recording_error"
    assert "camera config missing" in error["error"]["message"]
    assert bridge.recording("start", "corrected")["ready"]


def test_log_failure_does_not_block_stop_or_status(rollout, monkeypatch):
    ready_fake(rollout)
    monkeypatch.setattr(rollout, "event", Mock(side_effect=OSError("disk full")))
    upstream = Mock(return_value={"status": "completed"})
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    for operation in ("stop", "status"):
        result = RecordingBridge(rollout).session(operation)
        assert upstream.call_args.args[2]["operation"] == operation
        assert result["recording_error"] == "disk full"
        assert result["status"] == "completed"


def test_event_write_failure_does_not_send_an_unlogged_action(rollout, monkeypatch):
    ready_fake(rollout)
    (rollout.output / "events.jsonl").unlink()
    (rollout.output / "events.jsonl").mkdir()
    upstream = Mock(return_value={"arms": {}, "errors": {}, "faults": {}})
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    result = RecordingBridge(rollout).execute(
        Action(arm="left", kind="joint_target", joints_rad=[0] * 6)
    )
    assert all(c.args[1:] == ("session", {"operation": "status"}) for c in upstream.call_args_list)
    assert result["status"] == "rejected"
    assert "recording_error" in result
    assert not rollout.status()["ready"]


def test_response_log_failure_preserves_the_actual_controller_result(rollout, monkeypatch):
    ready_fake(rollout)
    original = rollout.event

    def event(kind, **kw):
        if kind == "response":
            raise OSError("disk full")
        return original(kind, **kw)

    monkeypatch.setattr(rollout, "event", event)
    monkeypatch.setattr(
        "agentic_robots.recording.upstream_call",
        Mock(return_value={"status": "completed", "actual": {"q": 0}}),
    )
    result = RecordingBridge(rollout).execute(
        Action(arm="left", kind="joint_target", joints_rad=[0] * 6)
    )
    assert result["status"] == "completed"
    assert result["actual"] == {"q": 0}
    assert result["recording_error"] == "disk full"


def frame(path):
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "-threads",
            "1",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


@pytest.mark.parametrize("high_resolution", [False, True])
def test_real_ffmpeg_records_all_panels_and_finalizes_mp4(rollout, high_resolution):
    # Independent camera clocks do not produce identical input frame timestamps.
    rollout.cameras["top"]["device"] = "color=c=green:s=640x480:r=29"
    sizes = dict.fromkeys(ORDER, (640, 480))
    if high_resolution:
        sizes = {"left": (1920, 1200), "top": (1920, 1080), "right": (1920, 1200)}
        for role, color in zip(ORDER, ("red", "green", "blue")):
            width, height = sizes[role]
            rate = 8 if role == "top" else 15
            rollout.cameras[role]["device"] = f"color=c={color}:s={width}x{height}:r={rate}"
    try:
        rollout.start()
        assert rollout.status()["ready"]
        before = (rollout.output / "top.png").stat().st_mtime
        rollout.note("Recording synthetic test streams")
        time.sleep(1)
        assert (rollout.output / "top.png").stat().st_mtime > before
        images, errors = rollout.snapshots(after=time.time())
        assert not errors
        for role, size in sizes.items():
            with Image.open(images[role]["path"]) as observation:
                assert observation.size == size
    finally:
        result = rollout.finish()
    assert result["status"] == "finished", (rollout.output / "ffmpeg.log").read_text()
    image = frame(rollout.output / "rollout.mp4")
    assert image.size == (1920, 516)
    for x, channel in [(320, 0), (960, 1), (1600, 2)]:
        pixel = image.getpixel((x, 240))
        assert pixel[channel] > 100
        assert all(value < 10 for i, value in enumerate(pixel) if i != channel)
    assert any(e["kind"] == "telemetry" for e in events(rollout))
    assert float(rollout.manifest["video"]["format"]["duration"]) > 1
    packets = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(rollout.output / "rollout.mp4"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    times = [float(line) for line in packets.stdout.splitlines()]
    # Matroska stores millisecond timecodes: a 30 fps cadence alternates 33/34 ms.
    assert all(b - a == pytest.approx(1 / 30, abs=0.001) for a, b in zip(times, times[1:]))
    assert rollout.finish()["status"] == "finished"


@pytest.mark.e2e
def test_mcp_recorded_rollout_against_http_robot_preserves_hold(http_robot, tmp_path):  # noqa: F811
    call, command, controller, _ = http_robot
    cmd, _ = command("session", {"operation": "status"})
    url = cmd[cmd.index("--url") + 1]
    r = Rollout(tmp_path / "recorded", "Close, move, and return", cameras(), url)

    async def run():
        async with Client(recording_server(RecordingBridge(r))) as client:
            for side in ("left", "right"):
                result = await client.call_tool(
                    "session", {"operation": "start", "arm": side, "supported": True}
                )
                assert not result.is_error
                for kind, extra in [
                    ("gripper_target", {"gripper_opening": 0}),
                    ("joint_target", {"joints_rad": [0.15, 0, 0, 0, 0, 0]}),
                    ("joint_target", {"joints_rad": [0] * 6}),
                ]:
                    result = await client.call_tool(
                        "execute",
                        {"action": {"arm": side, "kind": kind, "duration_s": 0.1, **extra}},
                    )
                    assert not result.is_error, result.structured_content
                    assert len([x for x in result.content if x.type == "image"]) == 3
                    assert result.structured_content["post_action"]["arms"][side]["ee_pose"]
            bad = await client.call_tool(
                "execute",
                {
                    "action": {
                        "arm": "left",
                        "kind": "joint_target",
                        "joints_rad": [4, 0, 0, 0, 0, 0],
                    }
                },
            )
            assert bad.structured_content["error"]["code"] == "joint_limits"
            assert len([x for x in bad.content if x.type == "image"]) == 3
            observation = await client.call_tool("observe", {})
            assert len([x for x in observation.content if x.type == "image"]) == 3
            assert set(observation.structured_content["images"]) == set(ORDER)
            note = await client.call_tool(
                "recording", {"operation": "note", "text": "Back at neutral"}
            )
            assert note.structured_content["ready"]
            finished = await client.call_tool("recording", {"operation": "finish"})
            assert finished.structured_content["status"] == "finished"

    try:
        r.start()
        asyncio.run(run())
        assert controller.poll() is None
        state = call("session", {"operation": "status"})
        assert set(state["arms"]) == {"left", "right"}
        assert all(
            s["joints_rad"] == [0] * 6 and s["gripper_opening"] == 0 for s in state["arms"].values()
        )
        log = events(r)
        assert sum(e["kind"] == "request" and e["tool"] == "execute" for e in log) == 7
        assert not any(e.get("arguments", {}).get("operation") == "release" for e in log)
        assert frame(r.output / "rollout.mp4").size == (1920, 516)
    finally:
        r.finish()


def test_unexpected_encoder_exit_is_reported_as_failed(rollout):
    rollout.start()
    rollout.process.send_signal(__import__("signal").SIGINT)
    rollout.process.wait(timeout=10)
    assert not rollout.status()["ready"]
    result = rollout.finish()
    assert result["status"] == "failed"
    assert "before finish" in result["error"]


@pytest.mark.e2e
def test_http_cli_recorder_shutdown_does_not_stop_controller(http_robot, tmp_path):  # noqa: F811
    call, command, controller, root = http_robot
    cmd, _ = command("session", {"operation": "status"})
    upstream = cmd[cmd.index("--url") + 1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    output = tmp_path / "http-recording"
    program = tmp_path / "recording_server.py"
    program.write_text(
        "import socket\noriginal=socket.socket\n"
        "class NoCAN(original):\n"
        " def __init__(self,family=socket.AF_INET,*a,**kw):\n"
        "  if family==socket.AF_CAN: raise RuntimeError('CAN forbidden in test')\n"
        "  super().__init__(family,*a,**kw)\n"
        "socket.socket=NoCAN\n"
        "from agentic_robots.recording import Rollout,RecordingBridge\n"
        "from scripts.robot_record import recording_server\n"
        f'r=Rollout({str(output)!r},"HTTP recording test",{cameras()!r},{upstream!r})\n'
        "try:\n"
        " r.start()\n"
        ' recording_server(RecordingBridge(r)).run(transport="streamable-http",'
        f'host="127.0.0.1",port={port})\n'
        "finally: r.finish()\n"
    )
    log = (tmp_path / "recorder.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, str(program)],
        cwd=root,
        env={**__import__("os").environ, "PYTHONPATH": str(root)},
        stdout=log,
        stderr=log,
    )

    def proxy(tool, arguments=None):
        cmd, result_path = command(tool, arguments)
        cmd[cmd.index("--url") + 1] = f"http://127.0.0.1:{port}/mcp"
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result_path.read_text())

    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert proc.poll() is None, (tmp_path / "recorder.log").read_text()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("Recorder did not start")
        proxy("session", {"operation": "start", "arm": "left", "supported": True})
        assert proxy("recording", {"operation": "note", "text": "Move and hold"})["ready"]
        result = proxy(
            "execute",
            {
                "action": {
                    "arm": "left",
                    "kind": "joint_target",
                    "joints_rad": [0.1, 0, 0, 0, 0, 0],
                    "duration_s": 0.1,
                }
            },
        )
        assert result["status"] == "completed"
        # Explicit test teardown preserves the video/hold without claiming neutral.
        assert (
            proxy(
                "recording",
                {
                    "operation": "review",
                    "review": {
                        "outcome": "paused",
                        "summary": "Test requests stop with the arm held",
                        "evidence": ["Test teardown; left joint 1 remains at 0.1 rad"],
                    },
                },
            )["status"]
            == "finished"
        )
        proc.terminate()
        proc.wait(timeout=10)
        assert controller.poll() is None
        assert call("session", {"operation": "status"})["arms"]["left"]["joints_rad"][0] == 0.1
        assert frame(output / "rollout.mp4").size == (1920, 516)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        log.close()
