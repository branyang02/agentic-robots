"""Persistent recorder over real HTTP, Rust cameras, and simulated CAN-free hardware."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import robot_record
from tests.test_robot_http import http_robot  # noqa: F401
from tests.test_robot_record import cameras, frame

pytestmark = pytest.mark.e2e


@pytest.fixture
def http_recorder(http_robot, tmp_path, request, camera_runtime):  # noqa: F811
    _, command, controller, root = http_robot
    cmd, _ = command("session", {"operation": "status"})
    upstream = cmd[cmd.index("--url") + 1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    output = tmp_path / "tasks"
    encoder_delay_ms = getattr(request, "param", 0)
    camera_config = cameras()
    for camera in camera_config.values():
        camera["test"]["encoder_delay_ms"] = encoder_delay_ms
    environment = {**os.environ, "PYTHONPATH": str(root)}
    program = tmp_path / "recorder_service.py"
    program.write_text(
        "import socket\noriginal=socket.socket\n"
        "class NoCAN(original):\n"
        " def __init__(self,family=socket.AF_INET,*a,**kw):\n"
        "  if family==socket.AF_CAN: raise RuntimeError('CAN forbidden in test')\n"
        "  super().__init__(family,*a,**kw)\n"
        "socket.socket=NoCAN\n"
        "from agentic_robots import recording\n"
        "from scripts import robot_record\n"
        f"recording.configured_cameras=lambda:{camera_config!r}\n"
        "robot_record.main()\n"
    )
    log = (tmp_path / "recorder-service.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            str(program),
            "--output-root",
            str(output),
            "--upstream",
            upstream,
            "--port",
            str(port),
        ],
        cwd=root,
        env=environment,
        stdout=log,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}/mcp"

    def call(tool, arguments=None, error=False):
        cmd, result_path = command(tool, arguments)
        cmd[cmd.index("--url") + 1] = url
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == (1 if error else 0), result.stderr + result.stdout
        return json.loads(result_path.read_text())

    try:
        for _ in range(150):
            assert process.poll() is None, (tmp_path / "recorder-service.log").read_text()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("Recorder service did not start")
        yield call, url, output, process, controller, root
    finally:
        if process.poll() is None:
            process.terminate()  # Only this test's recorder; never the live controller.
            process.wait(timeout=10)
        log.close()


def test_two_tasks_reuse_http_service_and_preserve_hold(http_recorder):
    call, _, output, recorder, controller, _ = http_recorder
    assert call("recording", {"operation": "status"})["status"] == "idle"
    assert not output.exists()
    assert call("observe")["images"] == {}
    assert call("session", {"operation": "status"})["arms"] == {}
    directories = []
    for task in ("Close, move both arms, then neutral", "Repeat without restarting"):
        start = call("recording", {"operation": "start", "text": task})
        assert start["ready"]
        directories.append(start["output"])
        for arm in ("left", "right"):
            if not call("session", {"operation": "status"})["arms"].get(arm):
                call("session", {"operation": "start", "arm": arm, "supported": True})
            for target in ([0.15, 0, 0, 0, 0, 0], [0] * 6):
                result = call(
                    "execute",
                    {
                        "action": {
                            "arm": arm,
                            "kind": "joint_target",
                            "joints_rad": target,
                            "duration_s": 0.1,
                        }
                    },
                )
                assert result["status"] == "completed"
                post = result["post_action"]
                assert set(post["images"]) == {"left", "top", "right"}
                assert set(post["errors"]) <= {
                    f"arm:{side}" for side in ("left", "right") if side not in post["arms"]
                }
                assert all(set(img) == {"path"} for img in post["images"].values())
                measured = post["arms"][arm]
                assert measured["joints_rad"] == target
                assert measured["ee_pose"]["frame"] == f"{arm}_base"
                assert len(measured["ee_pose"]["position_m"]) == 3
                assert measured["gripper"]["measured_opening"] == measured["gripper_opening"]
                assert result["diagnostics"]
        assert len(call("observe")["images"]) == 3
        assert call("recording", {"operation": "finish"})["status"] == "finished"
        reviewed = call(
            "recording",
            {
                "operation": "review",
                "review": {
                    "outcome": "success",
                    "summary": "Simulated joints returned to neutral",
                    "evidence": ["Final numerical observation and synthetic video"],
                },
            },
        )
        assert reviewed["task"]["phase"] == "success"
        directory = Path(directories[-1])
        finished_files = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        assert call("observe")["images"] == {}
        result = call(
            "execute",
            {"action": {"arm": "left", "kind": "joint_target", "joints_rad": [0] * 6}},
            error=True,
        )
        assert result["error"]["code"] == "recording_unavailable"
        assert recorder.poll() is None and controller.poll() is None
        assert all(
            s["joints_rad"] == [0] * 6
            for s in call("session", {"operation": "status"})["arms"].values()
        )
        assert {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()} == finished_files
    assert len(set(directories)) == 2
    for directory in output.iterdir():
        assert all(
            frame(directory / f"{role}.mp4").size == (320, 240) for role in ("left", "top", "right")
        )
        assert (directory / "prompt.txt").read_text().strip()
        events = [
            json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        assert (
            len(
                [
                    e
                    for e in events
                    if e["kind"] == "request"
                    and e["tool"] == "execute"
                    and e.get("forwarded", True)
                ]
            )
            == 4
        )


@pytest.mark.parametrize("http_robot", ["TrackingSlipArm"], indirect=True)
def test_recorded_tracking_stop_recover_correct_and_return(http_recorder):
    call, _, _, recorder, controller, _ = http_recorder
    task = "Recover from a simulated tracking stop, correct the movement, then return to neutral"
    start = call("recording", {"operation": "start", "text": task})
    assert start["ready"]
    for arm in ("left", "right"):
        call("session", {"operation": "start", "arm": arm, "supported": True})
    call(
        "execute",
        {
            "action": {
                "arm": "left",
                "kind": "gripper_target",
                "gripper_opening": 0.2,
                "duration_s": 0.1,
            }
        },
    )
    action = {
        "arm": "left",
        "kind": "joint_target",
        "joints_rad": [0.2, 0, 0, 0, 0, 0],
        "duration_s": 0.2,
    }
    stopped = call("execute", {"action": action}, error=True)
    assert stopped["error"]["code"] == "tracking_error"
    assert stopped["error"]["recoverable"] and not stopped["error"]["retryable"]
    assert len(stopped["post_action"]["images"]) == 3
    assert stopped["post_action"]["arms"]["left"]["ee_pose"] is not None
    assert stopped["diagnostics"]
    observed = call("observe")
    assert len(observed["images"]) == 3
    held = observed["arms"]["left"]["joints_rad"]
    count = observed["arms"]["left"]["command_count"]
    blocked = call("execute", {"action": {**action, "joints_rad": [0] * 6}}, error=True)
    assert blocked["fault_latched"]
    recovered = call("session", {"operation": "recover", "arm": "left"})
    assert recovered["status"] == "recovered"
    assert recovered["actual"]["joints_rad"] == held
    assert recovered["actual"]["command_count"] == count + 1
    assert recovered["actual"]["gripper_opening"] == pytest.approx(0.2)
    assert recovered["cleared_fault"] == stopped["error"]
    observed = call("observe")
    assert not observed["faults"]
    correction = {
        **action,
        "kind": "joint_delta",
        "joints_rad": [0.02, 0, 0, 0, 0, 0],
        "duration_s": 0.4,
    }
    corrected = call("execute", {"action": correction})
    assert corrected["actual"]["joints_rad"][0] == pytest.approx(held[0] + 0.02)
    for arm in ("left", "right"):
        call("execute", {"action": {**action, "arm": arm, "joints_rad": [0] * 6}})
    final = call("observe")
    assert all(s["joints_rad"] == [0] * 6 for s in final["arms"].values())
    assert not final["faults"] and not final["errors"]
    assert final["arms"]["left"]["gripper_opening"] == pytest.approx(0.2)
    assert call("recording", {"operation": "finish"})["status"] == "finished"
    directory = Path(start["output"])
    assert all(
        frame(directory / f"{role}.mp4").size == (320, 240) for role in ("left", "top", "right")
    )
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    assert any(e.get("arguments", {}).get("operation") == "recover" for e in events)
    assert any(e.get("result", {}).get("status") == "recovered" for e in events)
    assert not any(e.get("arguments", {}).get("operation") == "release" for e in events)
    assert recorder.poll() is None and controller.poll() is None


@pytest.mark.parametrize("http_robot", ["ReturnSlipArm"], indirect=True)
def test_premature_end_after_return_fault_resumes_through_review(http_recorder, monkeypatch):
    call, _, _, recorder, controller, _ = http_recorder
    started = call("recording", {"operation": "start", "text": "Move, then neutral"})
    for side in ("left", "right"):
        call("session", {"operation": "start", "arm": side, "supported": True})
    action = {
        "arm": "left",
        "kind": "joint_target",
        "joints_rad": [0.2, 0, 0, 0, 0, 0],
        "duration_s": 0.2,
    }
    call("execute", {"action": action})
    call("recording", {"operation": "return", "text": "Task action complete"})
    stopped = call("execute", {"action": {**action, "joints_rad": [0] * 6}}, error=True)
    assert stopped["error"]["code"] == "tracking_error"
    rejected = call("recording", {"operation": "finish"}, error=True)
    assert rejected["error"]["code"] == "neutral_unverified"

    # The real HTTP recorder/bridge are driven by a fake desktop turn, never real CAN.
    def status():
        return call("recording", {"operation": "status"})

    adapter = SimpleNamespace(
        binding={"thread_id": "same-simulated-agent", "after_turn_id": "init"},
        rollout=SimpleNamespace(manifest={"task": status()["task"]}),
        status=status,
        session=lambda operation: call("session", {"operation": operation}),
    )
    sent = []

    async def app_call(client, thread, tool, arguments):
        assert thread == "same-simulated-agent"
        if tool == "read_thread":
            return {
                "thread": {"id": thread, "status": {"type": "idle"}},
                "turns": [{"id": "premature-final", "status": "completed"}],
            }
        sent.append(arguments)
        assert "tracking_error" in arguments["prompt"]
        call("observe")
        assert call("session", {"operation": "recover", "arm": "left"})["status"] == "recovered"
        call("observe")
        call("execute", {"action": {**action, "joints_rad": [0] * 6, "duration_s": 0.5}})
        call("observe")
        assert call("recording", {"operation": "finish"})["task"]["phase"] == "review"
        assert all(
            frame(Path(started["output"]) / f"{role}.mp4").size == (320, 240)
            for role in ("left", "top", "right")
        )
        reviewed = call(
            "recording",
            {
                "operation": "review",
                "review": {
                    "outcome": "success",
                    "summary": "Simulated return recovered and completed",
                    "evidence": [
                        "Decoded video frame; final observation contains both zero joint vectors"
                    ],
                },
            },
        )
        assert reviewed["task"]["phase"] == "success"
        return {}

    monkeypatch.setattr(robot_record, "app_call", app_call)
    asyncio.run(robot_record.watch_once(adapter, object()))
    asyncio.run(robot_record.watch_once(adapter, object()))
    assert len(sent) == 1
    assert controller.poll() is None and recorder.poll() is None
